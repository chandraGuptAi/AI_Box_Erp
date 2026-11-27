from fastapi.testclient import TestClient

from app.main import create_app


def test_health_needs_no_tokens():
    with TestClient(create_app()) as client:
        res = client.get("/health")
    assert res.status_code == 200 and res.json()["status"] == "ok"


def test_service_token_is_enforced(monkeypatch):
    from app.config import get_settings
    monkeypatch.setenv("SERVICE_TOKEN", "gateway-secret")
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            assert client.post("/api/ai-box-erp/generate", json={}).status_code == 403
            assert client.get("/health").status_code == 200
    finally:
        monkeypatch.delenv("SERVICE_TOKEN")
        get_settings.cache_clear()
