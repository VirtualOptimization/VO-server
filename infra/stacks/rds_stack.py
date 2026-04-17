"""CDK stack for a development PostgreSQL RDS instance."""

from __future__ import annotations

import os
from typing import Final

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2
from aws_cdk import aws_rds as rds
from constructs import Construct

DEFAULT_ALLOWED_IPV4: Final[str] = "0.0.0.0/0"


def env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    return int(value)


class VoRdsStack(Stack):
    """Provision a small PostgreSQL RDS instance for VO-server."""

    def __init__(self, scope: Construct, construct_id: str, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        db_name = os.getenv("VO_RDS_DB_NAME", "vo_db")
        db_username = os.getenv("VO_RDS_DB_USERNAME", "vo_admin")
        instance_identifier = os.getenv("VO_RDS_INSTANCE_IDENTIFIER", "vo-rds-dev")
        publicly_accessible = env_bool("VO_RDS_PUBLICLY_ACCESSIBLE", True)
        allowed_ipv4 = os.getenv("VO_RDS_ALLOWED_IPV4", DEFAULT_ALLOWED_IPV4)
        allocated_storage = env_int("VO_RDS_ALLOCATED_STORAGE_GB", 20)
        max_allocated_storage = env_int("VO_RDS_MAX_ALLOCATED_STORAGE_GB", allocated_storage)

        if publicly_accessible:
            nat_gateways = 0
            subnet_configuration = [
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                )
            ]
            selected_subnet_type = ec2.SubnetType.PUBLIC
        else:
            nat_gateways = 1
            subnet_configuration = [
                ec2.SubnetConfiguration(
                    name="public",
                    subnet_type=ec2.SubnetType.PUBLIC,
                    cidr_mask=24,
                ),
                ec2.SubnetConfiguration(
                    name="private-egress",
                    subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS,
                    cidr_mask=24,
                ),
            ]
            selected_subnet_type = ec2.SubnetType.PRIVATE_WITH_EGRESS

        vpc = ec2.Vpc(
            self,
            "VoRdsVpc",
            max_azs=2,
            nat_gateways=nat_gateways,
            subnet_configuration=subnet_configuration,
        )

        security_group = ec2.SecurityGroup(
            self,
            "VoRdsSecurityGroup",
            vpc=vpc,
            description="Security group for VO PostgreSQL RDS",
            allow_all_outbound=True,
        )

        security_group.add_ingress_rule(
            peer=ec2.Peer.ipv4(allowed_ipv4),
            connection=ec2.Port.tcp(5432),
            description="PostgreSQL access for development",
        )

        instance = rds.DatabaseInstance(
            self,
            "VoPostgres",
            engine=rds.DatabaseInstanceEngine.postgres(
                version=rds.PostgresEngineVersion.VER_16_3
            ),
            instance_type=ec2.InstanceType.of(
                ec2.InstanceClass.BURSTABLE4_GRAVITON,
                ec2.InstanceSize.MICRO,
            ),
            vpc=vpc,
            vpc_subnets=ec2.SubnetSelection(subnet_type=selected_subnet_type),
            credentials=rds.Credentials.from_generated_secret(db_username),
            database_name=db_name,
            allocated_storage=allocated_storage,
            max_allocated_storage=max_allocated_storage,
            storage_encrypted=True,
            multi_az=False,
            publicly_accessible=publicly_accessible,
            backup_retention=Duration.days(1),
            deletion_protection=False,
            removal_policy=RemovalPolicy.DESTROY,
            delete_automated_backups=True,
            security_groups=[security_group],
            instance_identifier=instance_identifier,
        )

        CfnOutput(self, "RdsEndpoint", value=instance.instance_endpoint.hostname)
        CfnOutput(self, "RdsPort", value=str(instance.instance_endpoint.port))
        CfnOutput(self, "RdsDatabaseName", value=db_name)
        CfnOutput(self, "RdsSecretArn", value=instance.secret.secret_arn)
        CfnOutput(self, "RdsSecurityGroupId", value=security_group.security_group_id)
        CfnOutput(self, "RdsVpcId", value=vpc.vpc_id)
        CfnOutput(self, "RdsPubliclyAccessible", value=str(publicly_accessible).lower())
        CfnOutput(self, "RdsAllowedIpv4", value=allowed_ipv4)
        CfnOutput(self, "RdsNatGateways", value=str(nat_gateways))
        CfnOutput(self, "RdsAllocatedStorageGb", value=str(allocated_storage))
        CfnOutput(self, "RdsMaxAllocatedStorageGb", value=str(max_allocated_storage))
