#!/usr/bin/env python3
"""
This is where everything boots up.

We spin up two stacks in order:
  - NetworkStack first — it owns the VPC and security groups, so EksStack
    needs it to exist before it can do anything useful.
  - EksStack second — gets the VPC and cluster SG handed to it from above.

We don't hardcode the AWS account or region here. CDK picks those up from
CDK_DEFAULT_ACCOUNT and CDK_DEFAULT_REGION, which GitHub Actions sets
automatically when it federates into AWS via OIDC.
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
