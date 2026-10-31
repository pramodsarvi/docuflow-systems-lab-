from docuflow import config, db, jobs, service


def test_priority_then_fair_share():
    jobs.enqueue("extract", "a", {"n": 1}); jobs.enqueue("extract", "a", {"n": 2}); jobs.enqueue("extract", "b", {"n": 3})
    db.control_set("lane_limits", {"vlm": 3})
    first = jobs.claim("vlm", "w1")
    assert first["dataset"] == "a"                      # same priority: oldest first
    second = jobs.claim("vlm", "w2")
    assert second["dataset"] == "b"                     # dataset 'a' already running -> fair share goes to 'b'


def test_lane_limit_and_dataset_cap():
    for _ in range(3):
        jobs.enqueue("extract", "a", {})
    db.control_set("lane_limits", {"vlm": 2})
    assert jobs.claim("vlm", "w1")
    assert jobs.claim("vlm", "w2") is None              # PER_DATASET_CAP=1 for vlm
    jobs.enqueue("extract", "b", {})
    assert jobs.claim("vlm", "w2")["dataset"] == "b"
    jobs.enqueue("extract", "c", {})
    assert jobs.claim("vlm", "w3") is None              # lane limit of 2 reached


def test_downstream_stage_outranks_upstream():
    jobs.enqueue("fetch", "a", {}); jobs.enqueue("embed", "a", {})
    db.control_set("lane_limits", {"io": 5, "embed": 5})
    assert jobs.claim("embed", "w")["stage"] == "embed"


def test_pause_lane_and_dataset():
    jobs.enqueue("extract", "a", {}); jobs.enqueue("extract", "b", {})
    service.pause("datasets", "a")
    assert jobs.claim("vlm", "w")["dataset"] == "b"
    service.pause("lanes", "vlm")
    assert jobs.claim("vlm", "w2") is None
    service.pause("lanes", "vlm", False); service.pause("datasets", "a", False)
    db.control_set("lane_limits", {"vlm": 3})
    assert jobs.claim("vlm", "w3")["dataset"] == "a"


def test_vlm_yields_to_queries_but_embed_does_not():
    jobs.enqueue("extract", "a", {}); jobs.enqueue("embed", "a", {})
    with db.pool().connection() as c:
        c.execute("INSERT INTO inflight (id) VALUES ('q1')")
    assert jobs.claim("vlm", "w") is None
    assert jobs.claim("embed", "w2")
    with db.pool().connection() as c:
        c.execute("DELETE FROM inflight")
    assert jobs.claim("vlm", "w")


def test_retry_backoff_then_dead_then_retry_dead():
    jobs.enqueue("embed", "a", {})
    for attempt in range(config.MAX_ATTEMPTS):
        with db.pool().connection() as c:
            c.execute("UPDATE jobs SET run_after=now() WHERE status='queued'")
        j = jobs.claim("embed", "w")
        assert j["attempts"] == attempt + 1
        dead = jobs.fail(j, "boom")
    assert dead
    assert jobs.claim("embed", "w") is None
    assert jobs.retry_dead() == 1
    assert jobs.claim("embed", "w")["attempts"] == 1


def test_expired_lease_is_requeued():
    jobs.enqueue("embed", "a", {})
    j = jobs.claim("embed", "w")
    with db.pool().connection() as c:
        c.execute("UPDATE jobs SET locked_at = now() - interval '1 hour' WHERE id=%s", (j["id"],))
    assert jobs.reap() == 1
    assert jobs.claim("embed", "w2")["id"] == j["id"]


def test_ingest_slices_fetch_jobs():
    r = service.ingest("funsd", limit=45)
    assert r["fetch_jobs"] == 3
    snap = jobs.snapshot()
    assert snap["lanes"]["io"]["queued"] == 3
