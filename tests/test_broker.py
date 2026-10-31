"""RabbitMQ work tokens against a real broker (docker compose up -d rabbitmq, or RABBITMQ_TEST_URL). Skipped otherwise."""
import os
import time

import pytest

from docuflow import broker, config, db, jobs, service, worker

URL = os.environ.get("RABBITMQ_TEST_URL", "amqp://docuflow:docuflow@localhost:5673/%2F")


def _up():
    try:
        import pika
        pika.BlockingConnection(pika.URLParameters(URL)).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _up(), reason="RabbitMQ not running")


@pytest.fixture(autouse=True)
def mq(monkeypatch):
    monkeypatch.setattr(config, "RABBITMQ_URL", URL)
    broker._reset()
    ch = broker._channel()
    for lane in broker.LANES:
        ch.queue_purge(broker.qname(lane)); ch.queue_purge(broker.qname(lane) + ".delay")
    yield
    broker._reset()


def test_enqueue_publishes_and_worker_consumes_token():
    service.ingest("synthetic", limit=45)                     # 3 fetch jobs in one transaction -> 3 tokens after commit
    assert broker.depth("io") == 3
    db.control_set("lane_limits", {"io": 5})
    job = worker._next_job("io", "w")
    assert job["stage"] == "fetch" and broker.depth("io") == 2


def test_surplus_token_is_dropped_and_blocked_token_returned():
    broker.notify("vlm", 1)                                   # token without a job
    assert worker._next_job("vlm", "w") is None and broker.depth("vlm") == 0
    jobs.enqueue("extract", "a", {})
    service.pause("lanes", "vlm")
    assert worker._next_job("vlm", "w") is None and broker.depth("vlm") == 0     # paused: not runnable -> dropped
    service.pause("lanes", "vlm", False)
    assert broker.reconcile("vlm") == 1                        # resume: reconciler restores the token
    with db.pool().connection() as c:
        c.execute("INSERT INTO inflight (id) VALUES ('q')")    # vlm yields to queries: token goes back
    assert worker._next_job("vlm", "w") is None and broker.depth("vlm") == 1


def test_failed_job_token_returns_after_backoff():
    jobs.enqueue("embed", "a", {})
    j = worker._next_job("embed", "w")
    with db.pool().connection() as c:                          # make the backoff tiny for the test
        c.execute("UPDATE jobs SET max_attempts=5 WHERE id=%s", (j["id"],))
    jobs.fail({**j, "attempts": 0}, "boom")                    # delay = min(300, 5 * 2**-1) = 2.5s
    assert broker.depth("embed") == 0
    time.sleep(3.5)
    assert broker.depth("embed") == 1
