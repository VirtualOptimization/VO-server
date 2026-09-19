"""S3에서 특정 confirm_code 데이터 삭제"""

import os
import sys

import boto3
from dotenv import load_dotenv

load_dotenv()

if len(sys.argv) < 2:
    print("사용법: python scripts/delete_scan.py <confirm_code>")
    print("예시:  python scripts/delete_scan.py 3466AB")
    sys.exit(1)

confirm_code = sys.argv[1].upper()
bucket = os.getenv("S3_BUCKET_NAME", "project7-65-sydney-vo-s3")
prefix = f"{confirm_code}/"

s3 = boto3.client(
    "s3",
    aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("AWS_REGION", "ap-southeast-2"),
)

deleted = 0
key_marker = None
version_id_marker = None

while True:
    kwargs = {"Bucket": bucket, "Prefix": prefix}
    if key_marker:
        kwargs["KeyMarker"] = key_marker
    if version_id_marker:
        kwargs["VersionIdMarker"] = version_id_marker

    resp = s3.list_object_versions(**kwargs)

    objects = [
        {"Key": item["Key"], "VersionId": item["VersionId"]}
        for item in resp.get("Versions", []) + resp.get("DeleteMarkers", [])
    ]

    if objects:
        s3.delete_objects(Bucket=bucket, Delete={"Objects": objects, "Quiet": True})
        deleted += len(objects)

    if not resp.get("IsTruncated"):
        break

    key_marker = resp.get("NextKeyMarker")
    version_id_marker = resp.get("NextVersionIdMarker")

print(f"삭제 완료: {deleted}개 객체 삭제 (s3://{bucket}/{prefix})")
