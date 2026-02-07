"""Orchestrator configuration — loaded from environment variables."""

from __future__ import annotations

import re

from pydantic import field_validator
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings, loaded from environment variables.

    All settings are prefixed with MATRX_ (e.g., MATRX_S3_BUCKET).
    """

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False
    log_level: str = "INFO"

    # Docker
    sandbox_image: str = "matrx-sandbox:latest"
    docker_network: str = "bridge"
    container_cpu_limit: float = 2.0        # CPU cores
    container_memory_limit: str = "4g"
    container_disk_limit: str = "20g"

    # AWS / S3
    s3_bucket: str = ""
    s3_region: str = "us-east-1"

    # Sandbox defaults
    max_session_duration_seconds: int = 7200  # 2 hours
    shutdown_timeout_seconds: int = 30
    healthcheck_interval_seconds: int = 30
    max_command_length: int = 10000
    command_timeout_seconds: int = 300

    model_config = {"env_prefix": "MATRX_"}

    @field_validator("s3_bucket")
    @classmethod
    def validate_s3_bucket(cls, v: str) -> str:
        if not v:
            # Allow empty for local dev with LocalStack
            return v
        if len(v) < 3 or len(v) > 63:
            raise ValueError("s3_bucket must be 3-63 characters")
        if not re.match(r"^[a-z0-9][a-z0-9.\-]*[a-z0-9]$", v):
            raise ValueError("s3_bucket contains invalid characters")
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        valid = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
        if v.upper() not in valid:
            raise ValueError(f"log_level must be one of {valid}")
        return v.upper()


settings = Settings()
