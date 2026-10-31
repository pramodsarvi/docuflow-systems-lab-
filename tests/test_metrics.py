from fastapi.testclient import TestClient

from docuflow import api, jobs


def test_ready_and_metrics():
    jobs.enqueue("extract", "funsd", {})
    c = TestClient(api.app)
    assert c.get("/ready").json() == {"ready": True}
    body = c.get("/metrics").text
    assert 'docuflow_jobs{lane="vlm",status="queued"} 1' in body
    assert "docuflow_queries_in_flight" in body
