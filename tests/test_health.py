from fastapi.testclient import TestClient

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app


def test_health_returns_success() -> None:
    settings = Settings(_env_file=None, app_name="Test service", environment="test", log_level="INFO")
    with TestClient(create_app(settings)) as client:
        response = client.get("/health")

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/json"
    assert response.json() == {"status": "ok"}
