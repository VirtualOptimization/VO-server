"""AWS CDK app entrypoint for VO-server infrastructure."""

from __future__ import annotations

import os

import aws_cdk as cdk

from infra.stacks import VoPipelineStack, VoRdsStack


app = cdk.App()

env = cdk.Environment(
    account=os.getenv("CDK_DEFAULT_ACCOUNT"),
    region=os.getenv("CDK_DEFAULT_REGION", "ap-southeast-2"),
)

VoRdsStack(
    app,
    "VoRdsStack",
    env=env,
    stack_name=os.getenv("VO_RDS_STACK_NAME", "vo-rds-stack"),
)

if os.getenv("VO_PIPELINE_STACK_ENABLED", "").lower() in {"1", "true", "yes", "on"}:
    VoPipelineStack(
        app,
        "VoPipelineStack",
        env=env,
        stack_name=os.getenv("VO_PIPELINE_STACK_NAME", "vo-pipeline-stack"),
    )

app.synth()
