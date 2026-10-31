"""Operations shared by the API and the CLI."""
from . import config, db, jobs

FETCH_SLICE = 20   # documents per fetch job; slices from different datasets interleave in the io lane


def ingest(dataset, limit=10, offset=0, split=None, mode="vlm", priority=0):
    if dataset not in config.DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}; choose from {list(config.DATASETS)}")
    req = dict(dataset=dataset, limit=limit, offset=offset, split=split, mode=mode, priority=priority)
    with db.pool().connection() as c:
        bid = c.execute("INSERT INTO batches (dataset, request) VALUES (%s,%s) RETURNING id", (dataset, db.jb(req))).fetchone()["id"]
        n = 0
        for o in range(offset, offset + limit, FETCH_SLICE):
            jobs.enqueue("fetch", dataset, dict(dataset=dataset, split=split, offset=o, limit=min(FETCH_SLICE, offset + limit - o),
                                                mode=mode, priority=priority), bid, priority, conn=c)
            n += 1
    return {"batch_id": bid, "fetch_jobs": n, "request": req}


def set_lane_limit(lane, limit):
    if lane not in config.LANE_PROCS:
        raise ValueError(f"unknown lane {lane!r}")
    limit = max(0, min(int(limit), config.LANE_PROCS[lane]))
    cur = db.control_get("lane_limits", {})
    cur[lane] = limit
    db.control_set("lane_limits", cur)
    return cur


def pause(kind, name, paused=True):
    cur = db.control_get("paused", {"lanes": [], "datasets": []})
    lst = cur[kind]
    if paused and name not in lst:
        lst.append(name)
    if not paused and name in lst:
        lst.remove(name)
    db.control_set("paused", cur)
    return cur


def progress():
    with db.pool().connection() as c:
        return c.execute("""SELECT dataset, count(*) FILTER (WHERE status='fetched') fetched,
                                   count(*) FILTER (WHERE status='extracted') extracted,
                                   count(*) FILTER (WHERE status='indexed') indexed, count(*) total
                            FROM documents GROUP BY dataset ORDER BY dataset""").fetchall()
