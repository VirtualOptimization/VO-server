import os
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    env: str = os.getenv("ENV", "local")

    database_url: str = Field(validation_alias="DATABASE_URL")
    async_database_url: str = Field(validation_alias="ASYNC_DATABASE_URL")

    aws_region: str = Field(default="ap-southeast-2", validation_alias="AWS_REGION")
    s3_bucket_name: str = Field(default="", validation_alias="S3_BUCKET_NAME")
    aws_access_key_id: str = Field(default="", validation_alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: str = Field(default="", validation_alias="AWS_SECRET_ACCESS_KEY")
    s3_presigned_expiration_seconds: int = Field(
        default=3600,
        validation_alias="S3_PRESIGNED_EXPIRATION_SECONDS",
    )
    step_functions_state_machine_arn: str = Field(
        default="",
        validation_alias="STEP_FUNCTIONS_STATE_MACHINE_ARN",
    )
    scan_pipeline_mode: str = Field(default="step_functions", validation_alias="SCAN_PIPELINE_MODE")
    local_pipeline_timeout_seconds: int = Field(
        default=1800,
        validation_alias="LOCAL_PIPELINE_TIMEOUT_SECONDS",
    )
    jwt_secret_key: str = Field(default="", validation_alias="JWT_SECRET_KEY")
    jwt_algorithm: str = Field(default="HS256", validation_alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=10080,
        validation_alias="ACCESS_TOKEN_EXPIRE_MINUTES",
    )
    smtp_host: str = Field(default="", validation_alias="SMTP_HOST")
    smtp_port: int = Field(default=587, validation_alias="SMTP_PORT")
    smtp_username: str = Field(default="", validation_alias="SMTP_USERNAME")
    smtp_password: str = Field(default="", validation_alias="SMTP_PASSWORD")
    smtp_from_email: str = Field(default="", validation_alias="SMTP_FROM_EMAIL")
    smtp_from_name: str = Field(default="V-O", validation_alias="SMTP_FROM_NAME")
    email_verification_debug: bool = Field(
        default=False,
        validation_alias="EMAIL_VERIFICATION_DEBUG",
    )
    material_chat_enabled: bool = Field(default=False, validation_alias="MATERIAL_CHAT_ENABLED")
    gemini_api_key: str = Field(default="", validation_alias="GEMINI_API_KEY")
    material_chat_per_minute: int = Field(default=3, validation_alias="MATERIAL_CHAT_PER_MINUTE")
    material_chat_per_day: int = Field(default=20, validation_alias="MATERIAL_CHAT_PER_DAY")
    gemini_material_text_model: str = Field(
        default="gemini-3.5-flash-lite",
        validation_alias="GEMINI_MATERIAL_TEXT_MODEL",
    )
    gemini_material_image_model: str = Field(
        default="gemini-2.5-flash-image",
        validation_alias="GEMINI_MATERIAL_IMAGE_MODEL",
    )
    material_chat_timeout_seconds: int = Field(
        default=30,
        validation_alias="MATERIAL_CHAT_TIMEOUT_SECONDS",
    )
    api_docs_enabled: bool = Field(default=True, validation_alias="API_DOCS_ENABLED")

    model_config = SettingsConfigDict(
        env_file=".env" if os.getenv("ENV", "local") == "local" else None,
        env_file_encoding="utf-8",
        extra="ignore",
    )


settings = Settings()
