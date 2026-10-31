from fastapi.testclient import TestClient

from docuflow import api, jobs


def test_ready_and_metrics():
    jobs.enqueue("extract", "funsd", {})
    c = TestClient(api.app)
    assert c.get("/ready").json() == {"ready": True}
    body = c.get("/metrics").text
    assert 'docuflow_jobs{lane="vlm",status="queued"} 1' in body
    assert "docuflow_queries_in_flight" in body


def test_list_documents_filters():
    from docuflow import db
    with db.pool().connection() as c:
        c.execute("INSERT INTO documents (id, dataset, status) VALUES ('s/1','synthetic','indexed'), ('s/2','synthetic','fetched'), ('f/1','funsd','indexed')")
    c = TestClient(api.app)
    assert {d["id"] for d in c.get("/documents?dataset=synthetic").json()} == {"s/1", "s/2"}
    assert [d["id"] for d in c.get("/documents?ids=s/2,f/1&status=indexed").json()] == ["f/1"]


def test_queries_can_be_paused_for_ingest_priority():
    c = TestClient(api.app)
    assert c.post("/workloads/queries", json={"paused": True}).json() == {"queries_paused": True}
    r = c.post("/query", json={"question": "anything"})
    assert r.status_code == 503 and r.headers["retry-after"] == "300"
    assert c.get("/workloads").json()["queries_paused"] is True
    c.post("/workloads/queries", json={"paused": False})
    assert c.get("/workloads").json()["queries_paused"] is False
