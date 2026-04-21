"""
This stack handles all the networking — the VPC and the two security groups
that enforce the firewall rules from the brief.

What's allowed in:
  - HTTP (80) and HTTPS (443) from anywhere on the ALB
  - ICMP / ping from anywhere (both the ALB and the pods)
  - All TCP and UDP between resources inside the VPC
  - Everything else is blocked by default

We end up with two security groups:
  - alb_sg:     sits on the internet-facing load balancer
  - cluster_sg: sits on every Fargate pod ENI

For the "allow internal VPC traffic" rule we use a self-referencing SG rule
rather than opening the whole VPC CIDR. Anything wearing cluster_sg can talk
to anything else wearing cluster_sg — same practical effect, tighter blast
radius, and it's the pattern AWS themselves recommend for this topology.
"""
from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
)
from constructs import Construct


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # 2 AZs is the floor EKS requires, and it keeps costs down for a take-home.
        # The ALB lives in the public subnets; Fargate pods stay in the private ones.
        # The single NAT gateway is how the pods reach ECR to pull images — without
        # it they'd have no route out. One NAT is fine here; prod would want one per AZ.
        # Hardcoding the CIDR keeps the synth fast and avoids unnecessary AWS lookups.
        self.vpc = ec2.Vpc(
            self,
            "FincraVpc",
            max_azs=2,
            nat_gateways=1,
            ip_addresses=ec2.IpAddresses.cidr("10.0.0.0/16"),
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ],
        )

        # --- ALB security group -------------------------------------------------
        # This one faces the internet, so we open HTTP, HTTPS, and ping from anywhere.
        self.alb_sg = ec2.SecurityGroup(
            self,
            "AlbSecurityGroup",
            vpc=self.vpc,
            description="Ingress for the public ALB in front of the Fincra app",
            allow_all_outbound=True,
        )
        self.alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(80), "HTTP from the internet"
        )
        self.alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(), ec2.Port.tcp(443), "HTTPS from the internet"
        )
        self.alb_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.icmp_ping(),
            "ICMP echo from the internet",
        )

        # --- Cluster / workload security group ----------------------------------
        # Goes on every Fargate pod. No ingress rules by default — we add only
        # what's explicitly needed below.
        self.cluster_sg = ec2.SecurityGroup(
            self,
            "ClusterSecurityGroup",
            vpc=self.vpc,
            description="Workload SG for the EKS Fargate cluster",
            allow_all_outbound=True,
        )

        # Pods need to talk to each other freely — CoreDNS, service mesh, whatever.
        # The cleanest way to express this is a self-referencing rule: if you're
        # carrying this SG you can reach anything else carrying this SG. It's
        # tighter than opening the whole VPC CIDR and covers the brief's requirement.
        self.cluster_sg.add_ingress_rule(
            self.cluster_sg,
            ec2.Port.all_tcp(),
            "Internal TCP between workloads",
        )
        self.cluster_sg.add_ingress_rule(
            self.cluster_sg,
            ec2.Port.all_udp(),
            "Internal UDP between workloads",
        )

        # Let the ALB actually reach the pods — health checks and real traffic both
        # come through this rule.
        self.cluster_sg.add_ingress_rule(
            self.alb_sg,
            ec2.Port.tcp(80),
            "ALB to pods on app port",
        )

        # Pods should be pingable too, not just the ALB.
        self.cluster_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.icmp_ping(),
            "ICMP echo to workloads",
        )