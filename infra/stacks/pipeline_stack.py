"""CDK stack for scan upload orchestration and GLB conversion worker."""

from __future__ import annotations

import os

from aws_cdk import CfnOutput, Duration, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_ecr as ecr
from aws_cdk import aws_ecs as ecs
from aws_cdk import aws_iam as iam
from aws_cdk import aws_logs as logs
from aws_cdk import aws_s3 as s3
from aws_cdk import aws_stepfunctions as sfn
from aws_cdk import aws_stepfunctions_tasks as tasks
from constructs import Construct


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


class VoPipelineStack(Stack):
    """Provision ECS and Step Functions resources for scan processing."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        bucket_name = os.getenv("VO_PIPELINE_BUCKET_NAME")
        if not bucket_name:
            raise ValueError("VO_PIPELINE_BUCKET_NAME must be set to synth/deploy VoPipelineStack")

        image_uri = os.getenv("VO_GLB_CONVERTER_IMAGE_URI", "")
        create_repository = env_bool("VO_PIPELINE_CREATE_ECR_REPOSITORY", True)
        cpu = int(os.getenv("VO_GLB_CONVERTER_CPU", "1024"))
        memory = int(os.getenv("VO_GLB_CONVERTER_MEMORY_MIB", "2048"))

        vpc = ec2.Vpc(
            self,
            "VoPipelineVpc",
            max_azs=2,
            nat_gateways=0,
            subnet_configuration=[
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ],
        )

        cluster = ecs.Cluster(
            self,
            "VoPipelineCluster",
            vpc=vpc,
            cluster_name=os.getenv("VO_PIPELINE_CLUSTER_NAME", "vo-pipeline-cluster"),
        )

        bucket = s3.Bucket.from_bucket_name(self, "VoPipelineBucket", bucket_name)

        repository = None
        if create_repository:
            repository = ecr.Repository(
                self,
                "VoGlbConverterRepository",
                repository_name=os.getenv("VO_GLB_CONVERTER_REPOSITORY_NAME", "vo-glb-converter"),
            )

        task_definition = ecs.FargateTaskDefinition(
            self,
            "VoGlbConverterTaskDefinition",
            cpu=cpu,
            memory_limit_mib=memory,
        )

        bucket.grant_read_write(task_definition.task_role)

        task_definition.task_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogStream",
                    "logs:PutLogEvents",
                ],
                resources=["*"],
            )
        )

        log_group = logs.LogGroup(
            self,
            "VoGlbConverterLogs",
            retention=logs.RetentionDays.ONE_WEEK,
        )

        if image_uri:
            container_image = ecs.ContainerImage.from_registry(image_uri)
        elif repository is not None:
            container_image = ecs.ContainerImage.from_ecr_repository(repository, tag="latest")
        else:
            raise ValueError("Provide VO_GLB_CONVERTER_IMAGE_URI or enable repository creation")

        container = task_definition.add_container(
            "VoGlbConverterContainer",
            image=container_image,
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="glb-converter",
                log_group=log_group,
            ),
        )

        container.add_environment("S3_BUCKET_NAME", bucket.bucket_name)

        run_task = tasks.EcsRunTask(
            self,
            "RunGlbConverterTask",
            integration_pattern=sfn.IntegrationPattern.RUN_JOB,
            cluster=cluster,
            task_definition=task_definition,
            assign_public_ip=True,
            launch_target=tasks.EcsFargateLaunchTarget(),
            container_overrides=[
                tasks.ContainerOverride(
                    container_definition=container,
                    command=[
                        "python3",
                        "/app/workers/converter/run_glb_conversion.py",
                        "--bucket",
                        bucket.bucket_name,
                        "--input-s3-key",
                        sfn.JsonPath.string_at("$.inputs.room_usdz"),
                        "--output-s3-key",
                        sfn.JsonPath.string_at("$.outputs.glb"),
                    ],
                )
            ],
            subnets=ec2.SubnetSelection(subnet_type=ec2.SubnetType.PUBLIC),
        )

        definition = sfn.Chain.start(run_task)

        state_machine = sfn.StateMachine(
            self,
            "VoPipelineStateMachine",
            definition_body=sfn.DefinitionBody.from_chainable(definition),
            timeout=Duration.minutes(30),
            state_machine_name=os.getenv("VO_PIPELINE_STATE_MACHINE_NAME", "vo-scan-pipeline"),
        )

        CfnOutput(self, "PipelineBucketName", value=bucket.bucket_name)
        CfnOutput(self, "PipelineClusterArn", value=cluster.cluster_arn)
        CfnOutput(self, "PipelineTaskDefinitionArn", value=task_definition.task_definition_arn)
        CfnOutput(self, "PipelineStateMachineArn", value=state_machine.state_machine_arn)
        if repository is not None:
            CfnOutput(self, "PipelineRepositoryUri", value=repository.repository_uri)
