from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    # AWS
    aws_region: str = "ap-northeast-2"
    s3_bucket_name: str = ""

    # DB (A가 스키마 확정 후 채울 예정)
    database_url: str = ""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
