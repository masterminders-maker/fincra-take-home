"""
EksStack — EKS Fargate cluster, AWS Load Balancer Controller, Argo CD.

Responsibilities:
  1. Provision an EKS cluster with Fargate profiles for the namespaces we use
     (default, kube-system, argocd).
  2. Install the AWS Load Balancer Controller via Helm, wired to IRSA so the
     controller pod can call the AWS ELB API with least privilege.
  3. Install Argo CD via its official Helm chart into the `argocd` namespace.
  4. Register an Argo CD Application resource that watches k8s/overlays/dev in
     this repo — from this point on, app deploys happen by committing to main.

The cluster SG created in NetworkStack is attached to the cluster so the
firewall rules from the brief apply to Fargate pod ENIs.
"""
from aws_cdk import (
    Stack,
    CfnOutput,
    aws_ec2 as ec2,
    aws_eks as eks,
    aws_iam as iam,
    lambda_layer_kubectl_v30 as kubectl_v30,
)
from constructs import Construct


# Repo URL that Argo CD will watch. Overridden via CDK context / env var in CI
# so the same stack works for forks.
DEFAULT_REPO_URL = "https://github.com/YOUR_GITHUB_USER/fincra-devops-takehome.git"


class EksStack(Stack):
    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        vpc: ec2.IVpc,
        cluster_security_group: ec2.ISecurityGroup,
        **kwargs,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)

        repo_url = self.node.try_get_context("repoUrl") or DEFAULT_REPO_URL

        # --- Cluster admin role -------------------------------------------------
        # A dedicated role that has kubectl access. In a real deployment this
        # would be mapped to the GitHub Actions OIDC role (or to SSO groups)
        # via aws-auth so humans never use long-lived keys.
        cluster_admin = iam.Role(
            self,
            "ClusterAdminRole",
            assumed_by=iam.AccountRootPrincipal(),
            description="Masters group role for the Fincra EKS cluster",
        )

        # --- The cluster --------------------------------------------------------
        cluster = eks.Cluster(
            self,
            "FincraCluster",
            version=eks.KubernetesVersion.V1_30,
            kubectl_layer=kubectl_v30.KubectlV30Layer(self, "KubectlLayer"),
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            default_capacity=0,              # no managed node group; Fargate only
            masters_role=cluster_admin,
            security_group=cluster_security_group,
            cluster_name="fincra-cluster",
            endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
        )

        # --- Fargate profiles ---------------------------------------------------
        # One profile per namespace we intend to run pods in. `default` is for
        # the Flask app, `kube-system` covers the ALB controller and CoreDNS,
        # and `argocd` is Argo CD itself.
        cluster.add_fargate_profile(
            "DefaultFargateProfile",
            selectors=[eks.Selector(namespace="default")],
        )
        cluster.add_fargate_profile(
            "KubeSystemFargateProfile",
            selectors=[
                eks.Selector(namespace="kube-system"),
            ],
        )
        cluster.add_fargate_profile(
            "ArgoCdFargateProfile",
            selectors=[eks.Selector(namespace="argocd")],
        )

        # --- AWS Load Balancer Controller --------------------------------------
        # IRSA (IAM Roles for Service Accounts): the controller SA in kube-system
        # assumes this role via OIDC so we don't share node creds.
        alb_sa = cluster.add_service_account(
            "AwsLoadBalancerControllerSA",
            name="aws-load-balancer-controller",
            namespace="kube-system",
        )
        # ElasticLoadBalancingFullAccess is the managed policy for the controller.
        # In production, pin to the official policy JSON from the
        # aws-load-balancer-controller repo for least privilege.
        alb_sa.role.add_managed_policy(
            iam.ManagedPolicy.from_aws_managed_policy_name("ElasticLoadBalancingFullAccess")
        )

        cluster.add_helm_chart(
            "AwsLoadBalancerController",
            chart="aws-load-balancer-controller",
            release="aws-load-balancer-controller",
            repository="https://aws.github.io/eks-charts",
            namespace="kube-system",
            values={
                "clusterName": cluster.cluster_name,
                "serviceAccount": {
                    "create": False,
                    "name": "aws-load-balancer-controller",
                },
                "region": self.region,
                "vpcId": vpc.vpc_id,
            },
        )

        # --- Argo CD ------------------------------------------------------------
        # Installed via the community Helm chart. On a fresh cluster this is the
        # only thing CI pushes imperatively — everything after this point is
        # pull-based GitOps.
        argocd_chart = cluster.add_helm_chart(
            "ArgoCd",
            chart="argo-cd",
            release="argocd",
            repository="https://argoproj.github.io/argo-helm",
            namespace="argocd",
            create_namespace=True,
            values={
                # Server not exposed publicly; access via `kubectl port-forward`
                # or add an Ingress in a follow-up. Keeping the attack surface
                # minimal for a fintech-adjacent take-home.
                "server": {
                    "service": {"type": "ClusterIP"},
                },
            },
        )

        # --- Argo CD Application ------------------------------------------------
        # A bootstrap Application that points Argo CD at k8s/overlays/dev in
        # this repo. Once reconciled, Argo CD owns the Flask app's lifecycle.
        argocd_app = cluster.add_manifest(
            "FincraAppArgoCdApplication",
            {
                "apiVersion": "argoproj.io/v1alpha1",
                "kind": "Application",
                "metadata": {
                    "name": "fincra-app",
                    "namespace": "argocd",
                    "finalizers": ["resources-finalizer.argocd.argoproj.io"],
                },
                "spec": {
                    "project": "default",
                    "source": {
                        "repoURL": repo_url,
                        "targetRevision": "main",
                        "path": "k8s/overlays/dev",
                    },
                    "destination": {
                        "server": "https://kubernetes.default.svc",
                        "namespace": "default",
                    },
                    "syncPolicy": {
                        "automated": {"prune": True, "selfHeal": True},
                        "syncOptions": ["CreateNamespace=true"],
                    },
                },
            },
        )
        # Argo CD CRDs must exist before the Application manifest is applied.
        argocd_app.node.add_dependency(argocd_chart)

        # --- Outputs ------------------------------------------------------------
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(
            self,
            "UpdateKubeconfigCommand",
            value=(
                f"aws eks update-kubeconfig --name {cluster.cluster_name} "
                f"--region {self.region} --role-arn {cluster_admin.role_arn}"
            ),
        )