import json

import boto3

from server.core.config import settings

s3_client = boto3.client(
    "s3",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


async def upload_json(key: str, data: dict) -> str:
    """S3에 JSON 데이터 업로드"""
    s3_client.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=json.dumps(data, ensure_ascii=False),
        ContentType="application/json",
    )
    return f"s3://{settings.s3_bucket_name}/{key}"


async def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """S3에 바이너리 파일 업로드"""
    s3_client.put_object(
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=data,
        ContentType=content_type,
    )
    return f"s3://{settings.s3_bucket_name}/{key}"
