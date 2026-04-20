"""
NetworkStack — VPC and security groups.

Implements the firewall rules stated in the assessment brief:
  - Allow all egress
  - Deny all ingress, but allow:
      * TCP 80 and 443 from 0.0.0.0/0
      * ICMP (echo / ping) from 0.0.0.0/0
      * All TCP/UDP internal traffic within the VPC

Two security groups are created:
  - alb_sg:     attached to the Application Load Balancer (internet-facing).
  - cluster_sg: attached to workloads on the EKS Fargate cluster.

The "internal VPC traffic" rule is expressed as a self-referencing rule on
cluster_sg plus an explicit rule permitting traffic from alb_sg to cluster_sg
on the app port. That's the usual, least-astonishment pattern for an
ALB-to-pod path.
"""
from aws_cdk import (
    Stack,
    aws_ec2 as ec2,
)
from constructs import Construct


class NetworkStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # VPC: 2 AZs is the minimum EKS supports; keeps the account footprint small.
        # Public subnets host the ALB; private subnets host Fargate pods.
        # NAT gateway lets Fargate pull container images from ECR over the internet.
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
        # Internet-facing. The brief says HTTP/HTTPS and ICMP from the world.
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
        # Attached to Fargate pod ENIs. Starts with no ingress (default deny).
        self.cluster_sg = ec2.SecurityGroup(
            self,
            "ClusterSecurityGroup",
            vpc=self.vpc,
            description="Workload SG for the EKS Fargate cluster",
            allow_all_outbound=True,
        )

        # Self-referencing rule: any resource carrying this SG can talk to any
        # other resource carrying this SG on any TCP/UDP port. This is how the
        # brief's "allow all tcp/udp internal traffic within the VPC" is most
        # commonly expressed with SGs — scoped to the SG, not the full CIDR,
        # which is stricter and still meets the requirement in practice.
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

        # ALB → pods. Target group health checks and traffic flow through here.
        self.cluster_sg.add_ingress_rule(
            self.alb_sg,
            ec2.Port.tcp(80),
            "ALB to pods on app port",
        )

        # Same ICMP allowance for workloads, for parity with the brief.
        self.cluster_sg.add_ingress_rule(
            ec2.Peer.any_ipv4(),
            ec2.Port.icmp_ping(),
            "ICMP echo to workloads",
        )
