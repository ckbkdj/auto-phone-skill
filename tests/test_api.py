from pathlib import Path

from fastapi.testclient import TestClient

from lobster_phone_agent.app import create_app
from lobster_phone_agent.config import Settings


def test_health_and_contract_routes(tmp_path: Path) -> None:
    settings = Settings(
        config_dir=Path("config"),
        artifact_dir=tmp_path,
        enable_a2a=False,
        api_key="test-key",
    )
    with TestClient(create_app(settings)) as client:
        health = client.get("/healthz")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"
        unauthorized = client.get("/v1/apps")
        assert unauthorized.status_code == 401
        apps = client.get("/v1/apps", headers={"Authorization": "Bearer test-key"})
        assert apps.status_code == 200
        assert apps.json()["count"] >= 60
        schema = client.get(
            "/v1/contracts/action-plan",
            headers={"X-API-Key": "test-key"},
        )
        assert schema.status_code == 200
        assert schema.json()["title"] == "ActionPlan"
