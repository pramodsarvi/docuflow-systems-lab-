import importlib

from fastapi.testclient import TestClient


def test_api_key_required_when_set(monkeypatch):
    monkeypatch.setenv("DOCUFLOW_API_KEY", "s3cret")
    from docuflow import api
    importlib.reload(api)
    c = TestClient(api.app)
    assert c.get("/health").status_code == 200                  # probes stay open
    assert c.get("/workloads").status_code == 401
    assert c.get("/workloads", headers={"x-api-key": "wrong"}).status_code == 401
    assert c.get("/workloads", headers={"x-api-key": "s3cret"}).status_code == 200
    monkeypatch.delenv("DOCUFLOW_API_KEY")
    importlib.reload(api)
    assert TestClient(api.app).get("/workloads").status_code == 200
