"""
This stack builds everything that actually runs workloads:
an EKS Fargate cluster, the AWS Load Balancer Controller, and Argo CD.

Here's what it does in order:
  1. Creates the EKS cluster and Fargate profiles for the three namespaces
     we care about: default (the Flask app), kube-system (the ALB controller
     and CoreDNS), and argocd.
  2. Installs the AWS Load Balancer Controller via Helm. It gets its own IAM
     role via IRSA so it can talk to the ELB API without borrowing anyone
     else's credentials.
  3. Installs Argo CD from the community Helm chart into the argocd namespace.
  4. Drops in an Argo CD Application manifest pointing at k8s/overlays/dev.
     After this, deploying the app is just a git push — no kubectl from CI.

The cluster SG comes from NetworkStack, so all the firewall rules we set
up there automatically apply to Fargate pod ENIs.
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


# The repo Argo CD will watch. You can override this via CDK context
# (--context repoUrl=...) so forks don't need to edit this file.
DEFAULT_REPO_URL = "https://github.com/masterminders-maker/fincra-take-home.git"


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
        # A dedicated IAM role that gets kubectl access via the masters group.
        # In a real setup you'd map this to your GitHub Actions OIDC role or
        # SSO groups through aws-auth — no one should be using long-lived keys
        # to talk to the cluster.
        cluster_admin = iam.Role(
            self,
            "ClusterAdminRole",
            assumed_by=iam.AccountRootPrincipal(),
            description="Masters group role for the Fincra EKS cluster",
        )

        # --- The cluster --------------------------------------------------------
        # Fargate-only — no EC2 managed node group. default_capacity=0 stops CDK
        # from adding one automatically.
        cluster = eks.Cluster(
            self,
            "FincraCluster",
            version=eks.KubernetesVersion.V1_30,
            kubectl_layer=kubectl_v30.KubectlV30Layer(self, "KubectlLayer"),
            vpc=vpc,
            vpc_subnets=[ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)],
            default_capacity=0,
            masters_role=cluster_admin,
            security_group=cluster_security_group,
            cluster_name="fincra-cluster",
            endpoint_access=eks.EndpointAccess.PUBLIC_AND_PRIVATE,
        )

        # --- Fargate profiles ---------------------------------------------------
        # Fargate won't run a pod unless its namespace matches a profile, so we
        # need one for each namespace we actually use. Three namespaces, three profiles.
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
        # The controller needs to call AWS APIs to create ALBs. We give it its
        # own IAM role via IRSA (pod identity through OIDC) instead of sharing
        # whatever the node would have — least privilege, no credential leakage.
        alb_sa = cluster.add_service_account(
            "AwsLoadBalancerControllerSA",
            name="aws-load-balancer-controller",
            namespace="kube-system",
        )
        # AWS's managed policy works fine here. If this were going to prod, you'd
        # swap it for the scoped-down policy JSON from the controller's own repo.
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
        # Argo CD itself is the one thing we push imperatively on a fresh cluster.
        # Once it's up, it takes over — all subsequent deploys are pull-based.
        # CI commits a git-sha tag bump and Argo CD reconciles the rest.
        argocd_chart = cluster.add_helm_chart(
            "ArgoCd",
            chart="argo-cd",
            release="argocd",
            repository="https://argoproj.github.io/argo-helm",
            namespace="argocd",
            create_namespace=True,
            values={
                # The Argo CD UI stays internal — reach it with kubectl port-forward.
                # Not worth exposing it publicly for a take-home; the attack surface
                # isn't worth it and port-forward is fine for local access.
                "server": {
                    "service": {"type": "ClusterIP"},
                },
            },
        )

        # --- Argo CD Application ------------------------------------------------
        # This is the bootstrap manifest that tells Argo CD where to look.
        # Once it reconciles, Argo CD owns the app — we just commit and it deploys.
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
        # The Application CRD only exists after Argo CD is installed, so we
        # explicitly declare this dependency to make sure ordering is correct.
        argocd_app.node.add_dependency(argocd_chart)

        # --- Outputs ------------------------------------------------------------
        # Handy stack outputs — the update-kubeconfig command is copy-pasteable.
        CfnOutput(self, "ClusterName", value=cluster.cluster_name)
        CfnOutput(
            self,
            "UpdateKubeconfigCommand",
            value=(
                f"aws eks update-kubeconfig --name {cluster.cluster_name} "
                f"--region {self.region} --role-arn {cluster_admin.role_arn}"
            ),
        )