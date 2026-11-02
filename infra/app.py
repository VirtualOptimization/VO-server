"""AWS CDK app entrypoint for VO-server infrastructure."""

from __future__ import annotations

import os

import aws_cdk as cdk

from infra.stacks import VoRdsStack


app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION", "ap-northeast-2"),
)

VoRdsStack(
    app,
    "VoRdsStack",
    env=env,
    stack_name=os.getenv("VO_RDS_STACK_NAME", "vo-rds-stack"),
)

app.synth()
