"""Orchestrator configuration — loaded from environment variables."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    """Application settings, loaded from environment variables."""

    # API
    host: str = "0.0.0.0"
    port: int = 8000
    debug: bool = False

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

    model_config = {"env_prefix": "MATRX_"}


settings = Settings()
