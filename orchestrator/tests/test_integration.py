"""Integration tests for the full sandbox lifecycle.

These tests require Docker and LocalStack to be running:
    docker compose up -d

Run with:
    pytest orchestrator/tests/test_integration.py -m integration -v

Skipped in CI unless Docker is available.
"""

from __future__ import annotations

import os
import time

import pytest

# Skip entire module if Docker is not available or MATRX_RUN_INTEGRATION is not set
pytestmark = pytest.mark.integration

ORCHESTRATOR_URL = os.getenv("MATRX_TEST_ORCHESTRATOR_URL", "http://localhost:8000")
API_KEY = os.getenv("MATRX_API_KEY", "")


def _headers() -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["X-API-Key"] = API_KEY
    return headers


@pytest.fixture(scope="module")
def check_docker():
    """Skip all integration tests if Docker is not reachable."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        client.close()
    except Exception:
        pytest.skip("Docker daemon not reachable — skipping integration tests")


@pytest.fixture(scope="module")
def check_orchestrator():
    """Skip all integration tests if orchestrator is not running."""
    import httpx
    try:
        resp = httpx.get(f"{ORCHESTRATOR_URL}/health", timeout=5)
        if resp.status_code != 200:
            pytest.skip("Orchestrator not healthy — skipping integration tests")
    except httpx.ConnectError:
        pytest.skip("Orchestrator not reachable — skipping integration tests")


@pytest.fixture
def http_client():
    import httpx
    with httpx.Client(base_url=ORCHESTRATOR_URL, headers=_headers(), timeout=120) as client:
        yield client


@pytest.mark.integration
class TestSandboxLifecycle:
    """Full lifecycle: create -> exec -> heartbeat -> complete -> verify cleanup."""

    def test_health_check(self, check_orchestrator, http_client):
        resp = http_client.get("/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"

    def test_create_sandbox(self, check_docker, check_orchestrator, http_client):
        resp = http_client.post(
            "/sandboxes",
            json={"user_id": "integration-test-user"},
        )
        assert resp.status_code == 201
        data = resp.json()
        assert data["sandbox_id"].startswith("sbx-")
        assert data["user_id"] == "integration-test-user"
        assert data["status"] in ("ready", "starting")

        sandbox_id = data["sandbox_id"]

        # Wait for ready
        for _ in range(30):
            resp = http_client.get(f"/sandboxes/{sandbox_id}")
            if resp.json()["status"] == "ready":
                break
            time.sleep(2)
        else:
            pytest.fail(f"Sandbox {sandbox_id} never became ready")

    def test_full_lifecycle(self, check_docker, check_orchestrator, http_client):
        # Create
        resp = http_client.post(
            "/sandboxes",
            json={"user_id": "integration-lifecycle"},
        )
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandbox_id"]

        # Wait for ready
        for _ in range(30):
            resp = http_client.get(f"/sandboxes/{sandbox_id}")
            if resp.json()["status"] == "ready":
                break
            time.sleep(2)

        # Exec command
        resp = http_client.post(
            f"/sandboxes/{sandbox_id}/exec",
            json={"command": "echo 'hello integration'", "timeout": 30},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["exit_code"] == 0
        assert "hello integration" in data["stdout"]

        # Heartbeat
        resp = http_client.post(f"/sandboxes/{sandbox_id}/heartbeat")
        assert resp.status_code == 200
        assert resp.json()["acknowledged"] is True

        # Complete (triggers graceful shutdown)
        resp = http_client.post(f"/sandboxes/{sandbox_id}/complete")
        assert resp.status_code == 200
        assert resp.json()["status"] == "shutting_down"

        # Verify sandbox is gone or stopped
        time.sleep(5)
        resp = http_client.get(f"/sandboxes/{sandbox_id}")
        if resp.status_code == 200:
            assert resp.json()["status"] in ("stopped", "shutting_down")
        else:
            assert resp.status_code == 404

    def test_exec_in_nonexistent_sandbox(self, check_orchestrator, http_client):
        resp = http_client.post(
            "/sandboxes/sbx-doesnotexist/exec",
            json={"command": "echo hello"},
        )
        assert resp.status_code == 404

    def test_destroy_nonexistent_sandbox(self, check_orchestrator, http_client):
        resp = http_client.delete("/sandboxes/sbx-doesnotexist")
        assert resp.status_code == 404

    def test_list_sandboxes_filters_by_user(
        self, check_docker, check_orchestrator, http_client
    ):
        resp = http_client.get("/sandboxes", params={"user_id": "no-such-user-xyz"})
        assert resp.status_code == 200
        assert resp.json()["total"] == 0


@pytest.mark.integration
class TestS3Storage:
    """Verify S3 storage operations via LocalStack."""

    def test_user_storage_created_on_sandbox_create(
        self, check_docker, check_orchestrator, http_client
    ):
        resp = http_client.post(
            "/sandboxes",
            json={"user_id": "s3-test-user"},
        )
        assert resp.status_code == 201
        sandbox_id = resp.json()["sandbox_id"]

        # Verify S3 structure exists via LocalStack
        try:
            import boto3
            s3 = boto3.client(
                "s3",
                endpoint_url=os.getenv("AWS_ENDPOINT_URL_S3", "http://localhost:4566"),
                region_name="us-east-1",
                aws_access_key_id="test",
                aws_secret_access_key="test",
            )
            bucket = os.getenv("MATRX_S3_BUCKET", "matrx-sandbox-dev")

            hot_resp = s3.list_objects_v2(
                Bucket=bucket, Prefix="users/s3-test-user/hot/", MaxKeys=1
            )
            cold_resp = s3.list_objects_v2(
                Bucket=bucket, Prefix="users/s3-test-user/cold/", MaxKeys=1
            )

            assert hot_resp.get("KeyCount", 0) > 0, "Hot storage prefix not created"
            assert cold_resp.get("KeyCount", 0) > 0, "Cold storage prefix not created"

        except ImportError:
            pytest.skip("boto3 not installed")
        finally:
            # Cleanup
            http_client.delete(f"/sandboxes/{sandbox_id}")
