import asyncio
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
    """JSON 데이터를 S3에 업로드하고 s3 URI를 반환한다."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    await asyncio.to_thread(
        s3_client.put_object,
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=body,
        ContentType="application/json",
    )

    return f"s3://{settings.s3_bucket_name}/{key}"


async def upload_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    """바이너리 파일을 S3에 업로드하고 s3 URI를 반환한다."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    await asyncio.to_thread(
        s3_client.put_object,
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=data,
        ContentType=content_type,
    )

    return f"s3://{settings.s3_bucket_name}/{key}"
