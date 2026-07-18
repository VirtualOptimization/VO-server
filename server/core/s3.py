import asyncio
import json
from functools import lru_cache

from botocore.exceptions import ClientError

from server.core.config import settings


@lru_cache(maxsize=1)
def get_s3_client():
    import boto3

    client_kwargs = {"region_name": settings.aws_region}
    if settings.aws_access_key_id and settings.aws_secret_access_key:
        client_kwargs.update(
            aws_access_key_id=settings.aws_access_key_id,
            aws_secret_access_key=settings.aws_secret_access_key,
        )
    return boto3.client("s3", **client_kwargs)


def build_s3_uri(key: str) -> str:
    return f"s3://{settings.s3_bucket_name}/{key}"


def parse_s3_uri(uri: str | None) -> tuple[str, str] | None:
    """Return (bucket, key) for s3://bucket/key values."""
    if not uri or not uri.startswith("s3://"):
        return None

    value = uri.removeprefix("s3://")
    if "/" not in value:
        return None

    bucket, key = value.split("/", 1)
    if not bucket or not key:
        return None
    return bucket, key


def generate_presigned_url_for_uri(uri: str | None, expires_in: int = 3600) -> str | None:
    """Presign s3:// URIs and pass through already-public URLs."""
    if not uri:
        return None

    parsed = parse_s3_uri(uri)
    if parsed is None:
        return uri

    bucket, key = parsed
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": bucket, "Key": key},
        ExpiresIn=expires_in,
    )


async def upload_json(key: str, data: dict) -> str:
    """JSON 데이터를 S3에 업로드하고 s3 URI를 반환한다."""
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    body = json.dumps(data, ensure_ascii=False).encode("utf-8")

    await asyncio.to_thread(
        get_s3_client().put_object,
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
        get_s3_client().put_object,
        Bucket=settings.s3_bucket_name,
        Key=key,
        Body=data,
        ContentType=content_type,
    )

    return build_s3_uri(key)


async def head_object(key: str) -> dict | None:
    """S3 객체 메타데이터를 반환한다. 없으면 None."""
    try:
        response = await asyncio.to_thread(
            get_s3_client().head_object,
            Bucket=settings.s3_bucket_name,
            Key=key,
        )
        return response
    except ClientError:
        return None
    except Exception:
        return None


def delete_objects(keys: list[str]) -> None:
    """S3 객체 여러 개를 한 번에 삭제한다."""
    if not keys:
        return
    get_s3_client().delete_objects(
        Bucket=settings.s3_bucket_name,
        Delete={"Objects": [{"Key": k} for k in keys]},
    )


def list_keys(prefix: str) -> list[str]:
    """S3 prefix 하위의 모든 객체 key 목록을 반환한다."""
    response = get_s3_client().list_objects_v2(
        Bucket=settings.s3_bucket_name,
        Prefix=prefix,
    )
    return [obj["Key"] for obj in response.get("Contents", [])]


def generate_presigned_url(key: str, expires_in: int = 3600) -> str:
    """S3 객체의 Presigned URL을 반환한다."""
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.s3_bucket_name, "Key": key},
        ExpiresIn=expires_in,
    )


async def get_json(key: str) -> dict:
    """S3에서 JSON 파일을 읽어 dict로 반환한다."""
    response = await asyncio.to_thread(
        get_s3_client().get_object,
        Bucket=settings.s3_bucket_name,
        Key=key,
    )
    body = await asyncio.to_thread(response["Body"].read)
    return json.loads(body.decode("utf-8"))


async def generate_presigned_put_url(key: str, content_type: str) -> str:
    if not settings.s3_bucket_name:
        raise ValueError("S3_BUCKET_NAME is not configured")

    return await asyncio.to_thread(
        get_s3_client().generate_presigned_url,
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
            get_s3_client().head_object,
            Bucket=settings.s3_bucket_name,
            Key=key,
        )
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code")
        if error_code in {"404", "NoSuchKey", "NotFound"}:
            return False
        raise
