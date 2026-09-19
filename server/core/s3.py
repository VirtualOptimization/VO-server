import boto3

from server.core.config import settings

s3_client = boto3.client("s3", region_name=settings.aws_region)


async def upload_json(key: str, data: dict) -> str:
    """S3에 JSON 데이터 업로드 (다음주 실제 구현 예정)"""
    # TODO: 실제 S3 업로드 구현
    # s3_client.put_object(Bucket=settings.s3_bucket_name, Key=key, Body=json.dumps(data))
    print(f"[S3 stub] upload_json key={key}")
    return f"s3://{settings.s3_bucket_name}/{key}"
