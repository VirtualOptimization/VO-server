import asyncio
import json

import boto3
from botocore.exceptions import ClientError

from server.core.config import settings

s3_client = boto3.client(
    "s3",
    region_name=settings.aws_region,
    aws_access_key_id=settings.aws_access_key_id,
    aws_secret_access_key=settings.aws_secret_access_key,
)


def build_s3_uri(key: str) -> str:
    return f"s3://{settings.s3_bucket_name}/{key}"


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

    return build_s3_uri(key)


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

    return build_s3_uri(key)


async def get_json(key: str) -> dict:
    """S3에서 JSON 파일을 읽어 dict로 반환한다."""
    response = await asyncio.to_thread(
        s3_client.get_object,
        Bucket=settings.s3_bucket_name,
        Key=key,
    )
    body = await asyncio.to_thread(response["Body"].read)
    return json.loads(body.decode("utf-8"))


async def generate_presigned_put_url(key: str, content_type: str) -> str:
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    return await asyncio.to_thread(
        s3_client.generate_presigned_url,
        "put_object",
        Params={
            "Bucket": settings.s3_bucket_name,
            "Key": key,
            "ContentType": content_type,
        },
        ExpiresIn=settings.s3_presigned_expiration_seconds,
    )


async def object_exists(key: str) -> bool:
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    try:
        await asyncio.to_thread(
            s3_client.head_object,
            Bucket=settings.s3_bucket_name,
            Key=key,
        )
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
