#!/usr/bin/env python3
"""
CDK entrypoint for the Fincra DevOps take-home.

Two stacks:
  - NetworkStack: VPC + security groups (the firewall rules from the brief).
  - EksStack:     EKS Fargate cluster, AWS Load Balancer Controller, and Argo CD.

Region/account come from the CDK environment (CDK_DEFAULT_ACCOUNT /
CDK_DEFAULT_REGION), which GitHub Actions populates via OIDC credentials.
"""
import os

import aws_cdk as cdk

from stacks.network_stack import NetworkStack
from stacks.eks_stack import EksStack


app = cdk.App()

env = cdk.Environment(
    account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
    region=os.environ.get("CDK_DEFAULT_REGION", "us-east-1"),
)

network = NetworkStack(app, "FincraNetworkStack", env=env)

EksStack(
    app,
    "FincraEksStack",
    vpc=network.vpc,
    cluster_security_group=network.cluster_sg,
    env=env,
)

app.synth()
