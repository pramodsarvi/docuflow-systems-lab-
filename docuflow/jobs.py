"""Postgres-backed job queue plus the workload-management rules applied at claim time.

Rules enforced when a worker claims a job (all adjustable live through the `control` table):
  * lane limit      max jobs running at once in a lane, across all worker processes
  * dataset cap     max running jobs per dataset in a lane, so one dataset cannot starve the others
  * pause           lanes or datasets can be paused; running jobs finish, nothing new starts
  * priority        higher first; ties go to the dataset with the fewest running jobs (fair share)
  * query yield     while interactive queries are in flight the vlm lane starts no new work
"""
import time

from . import broker, config, db

LANE_OF = {"fetch": "io", "extract": "vlm", "embed": "embed", "eval": "ragas"}
STAGE_PRIORITY = {"embed": 20, "extract": 10, "fetch": 0, "eval": 0}   # downstream stages first, so documents finish and become queryable


def enqueue(stage, dataset, payload, batch_id=None, priority=0, conn=None):
    sql = ("INSERT INTO jobs (batch_id, dataset, stage, lane, priority, payload) VALUES (%s,%s,%s,%s,%s,%s) RETURNING id")
    args = (batch_id, dataset, stage, LANE_OF[stage], priority + STAGE_PRIORITY[stage], db.jb(payload))
    if conn is not None:      # caller's transaction: the caller publishes the token after commit (notify_after)
        return conn.execute(sql, args).fetchone()["id"]
    with db.pool().connection() as c:
        jid = c.execute(sql, args).fetchone()["id"]
    broker.notify(LANE_OF[stage])
    return jid


def notify_after(stage, n=1):
    """Publish tokens for jobs enqueued inside a caller's transaction, once it has committed."""
    broker.notify(LANE_OF[stage], n)


def queries_in_flight():
    with db.pool().connection() as c:
        return c.execute("SELECT count(*) n FROM inflight WHERE beat > now() - interval '120 seconds'").fetchone()["n"]


def lane_limits():
    lim = dict(config.LANE_LIMIT_DEFAULT)
    lim.update(db.control_get("lane_limits", {}))
    return lim


def claim(lane, worker):
    """Atomically claim one eligible job in `lane`, or return None."""
    limits = lane_limits()
    paused = db.control_get("paused", {"lanes": [], "datasets": []})
    if lane in paused["lanes"]:
        return None
    limit = limits.get(lane, 1)
    if lane == "vlm" and (config.YIELD_TO_QUERIES or config.QUERY_PRESSURE_INFLIGHT):
        q = queries_in_flight()
        if config.YIELD_TO_QUERIES and q:
            return None                       # single-machine mode: one model server, queries go first
        if config.QUERY_PRESSURE_INFLIGHT and q >= config.QUERY_PRESSURE_INFLIGHT:
            limit = min(limit, config.VLM_LIMIT_UNDER_QUERY_LOAD)   # shared GPU: give the agent model room
    cap = config.PER_DATASET_CAP if lane == "vlm" else 1000
    with db.pool().connection() as c:
        c.execute("SELECT pg_advisory_xact_lock(hashtext(%s))", (lane,))
        running = c.execute("SELECT count(*) n FROM jobs WHERE lane=%s AND status='running'", (lane,)).fetchone()["n"]
        if running >= limit:
            return None
        return c.execute("""
            WITH cand AS (
              SELECT j.id FROM jobs j
              WHERE j.status='queued' AND j.lane=%(lane)s AND j.run_after <= now() AND NOT (j.dataset = ANY(%(paused)s))
                AND (SELECT count(*) FROM jobs r WHERE r.status='running' AND r.lane=j.lane AND r.dataset=j.dataset) < %(cap)s
              ORDER BY j.priority DESC,
                       (SELECT count(*) FROM jobs r WHERE r.status='running' AND r.lane=j.lane AND r.dataset=j.dataset),
                       j.id
              FOR UPDATE SKIP LOCKED LIMIT 1)
            UPDATE jobs SET status='running', locked_by=%(w)s, locked_at=now(), started_at=now(), attempts=attempts+1
            FROM cand WHERE jobs.id = cand.id RETURNING jobs.*""",
            {"lane": lane, "paused": paused["datasets"], "cap": cap, "w": worker}).fetchone()


def heartbeat(job_id):
    with db.pool().connection() as c:
        c.execute("UPDATE jobs SET locked_at=now() WHERE id=%s", (job_id,))


def complete(job_id):
    with db.pool().connection() as c:
        c.execute("UPDATE jobs SET status='done', finished_at=now(), error=NULL WHERE id=%s", (job_id,))


def fail(job, err):
    """Retry with exponential backoff until max_attempts, then park the job as 'dead'."""
    dead = job["attempts"] >= job["max_attempts"]
    delay = min(300, 5 * 2 ** (job["attempts"] - 1))
    with db.pool().connection() as c:
        c.execute("""UPDATE jobs SET status=%s, error=%s, locked_by=NULL, finished_at=CASE WHEN %s THEN now() END,
                     run_after = now() + make_interval(secs => %s) WHERE id=%s""",
                  ("dead" if dead else "queued", str(err)[:2000], dead, delay, job["id"]))
    if not dead:
        broker.notify(job["lane"], 1, delay_seconds=delay)   # comes back to the lane queue when the backoff expires
    return dead


def defer(job, err):
    """Model server unavailable: requeue without spending one of the job's attempts, backing off 15 s, 30 s, 60 s ...
    up to 5 min per consecutive outage. A brief outage delays pages instead of failing or downgrading them."""
    delay = min(300, 15 * 2 ** job.get("outages", 0))
    with db.pool().connection() as c:
        c.execute("""UPDATE jobs SET status='queued', attempts=greatest(attempts-1, 0), outages=outages+1, error=%s,
                     locked_by=NULL, run_after = now() + make_interval(secs => %s) WHERE id=%s""",
                  (str(err)[:2000], delay, job["id"]))
    broker.notify(job["lane"], 1, delay_seconds=delay)
    return delay


def reap():
    """Requeue jobs whose worker vanished (lease expired)."""
    with db.pool().connection() as c:
        rows = c.execute("""UPDATE jobs SET status='queued', locked_by=NULL, error='lease expired'
                            WHERE status='running' AND locked_at < now() - make_interval(secs => %s) RETURNING lane""",
                         (config.LEASE_SECONDS,)).fetchall()
    _notify_rows(rows)
    return len(rows)


def _notify_rows(rows):
    for lane in {r["lane"] for r in rows}:
        broker.notify(lane, sum(r["lane"] == lane for r in rows))


def requeue_running():
    """Startup recovery: no workers exist yet, so anything still 'running' was orphaned by an unclean shutdown."""
    with db.pool().connection() as c:
        rows = c.execute("UPDATE jobs SET status='queued', locked_by=NULL, error='orphaned at startup' WHERE status='running' RETURNING lane").fetchall()
    _notify_rows(rows)
    return len(rows)


def gc(done_days=None, query_days=None):
    """Retention: drop finished jobs and old query traces so the queue tables stay small. Dead jobs are kept for
    inspection until someone retries or deletes them."""
    done_days = config.GC_DONE_JOBS_DAYS if done_days is None else done_days
    query_days = config.GC_QUERIES_DAYS if query_days is None else query_days
    with db.pool().connection() as c:
        j = c.execute("DELETE FROM jobs WHERE status='done' AND finished_at < now() - make_interval(days => %s)", (done_days,)).rowcount
        q = c.execute("DELETE FROM queries WHERE created_at < now() - make_interval(days => %s)", (query_days,)).rowcount
        i = c.execute("DELETE FROM inflight WHERE beat < now() - interval '1 hour'").rowcount
    return {"jobs_deleted": j, "queries_deleted": q, "stale_inflight_deleted": i}


def retry_dead(dataset=None):
    with db.pool().connection() as c:
        rows = c.execute("UPDATE jobs SET status='queued', attempts=0, run_after=now(), error=NULL "
                         "WHERE status='dead' AND (%s::text IS NULL OR dataset=%s) RETURNING lane", (dataset, dataset)).fetchall()
    _notify_rows(rows)
    return len(rows)


def snapshot():
    """Queue depth, running jobs, and recent throughput per lane; used by the API and the CLI."""
    with db.pool().connection() as c:
        rows = c.execute("""SELECT lane, status, count(*) n FROM jobs GROUP BY lane, status""").fetchall()
        done = c.execute("""SELECT lane, count(*) n, avg(extract(epoch FROM finished_at - started_at)) avg_s
                            FROM jobs WHERE status='done' AND finished_at > now() - interval '5 minutes' GROUP BY lane""").fetchall()
        by_ds = c.execute("""SELECT dataset, status, count(*) n FROM jobs GROUP BY dataset, status""").fetchall()
    lanes = {l: {"queued": 0, "running": 0, "done": 0, "dead": 0} for l in ("io", "vlm", "embed", "ragas")}
    for r in rows:
        lanes[r["lane"]][r["status"]] = r["n"]
    for r in done:
        lanes[r["lane"]]["done_5m"] = r["n"]
        lanes[r["lane"]]["avg_seconds"] = round(r["avg_s"] or 0, 2)
    datasets = {}
    for r in by_ds:
        datasets.setdefault(r["dataset"], {})[r["status"]] = r["n"]
    return {"lanes": lanes, "limits": lane_limits(), "paused": db.control_get("paused", {"lanes": [], "datasets": []}),
            "queries_in_flight": queries_in_flight(), "datasets": datasets, "ts": time.time()}
