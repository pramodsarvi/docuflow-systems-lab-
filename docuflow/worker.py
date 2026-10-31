"""Worker process: polls one lane, runs handlers, records success/failure."""
import os
import signal
import time
import traceback

import psycopg

from . import db, jobs, pipeline

_stop = False


def run(lane):
    global _stop
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", True))
    name = f"{lane}-{os.environ.get('HOSTNAME') or os.getpid()}"
    while not _stop:
        try:
            db.init_schema()      # pods can start before the API or database is ready
            break
        except psycopg.OperationalError as e:
            print(f"[{name}] waiting for database: {e}", flush=True)
            time.sleep(3)
    last_reap = 0
    print(f"[{name}] started", flush=True)
    while not _stop:
        if time.time() - last_reap > 30:
            try:
                n = jobs.reap()
                if n:
                    print(f"[{name}] requeued {n} expired jobs", flush=True)
            except psycopg.OperationalError:
                pass
            last_reap = time.time()
        try:
            job = jobs.claim(lane, name)
        except psycopg.OperationalError as e:       # database restarting: wait and let the pool reconnect
            print(f"[{name}] database unavailable ({e}); retrying", flush=True)
            time.sleep(3)
            continue
        if not job:
            time.sleep(0.5)
            continue
        t0 = time.perf_counter()
        try:
            out = pipeline.HANDLERS[job["stage"]](job)
            jobs.complete(job["id"])
            print(f"[{name}] {job['stage']} #{job['id']} {job['dataset']} ok ({out}) {time.perf_counter() - t0:.1f}s", flush=True)
        except Exception as e:
            try:
                dead = jobs.fail(job, f"{type(e).__name__}: {e}")
            except psycopg.OperationalError:
                dead = False        # lease expiry will requeue it
            print(f"[{name}] {job['stage']} #{job['id']} FAILED{' (dead)' if dead else ''}: {e}", flush=True)
            if os.environ.get("DOCUFLOW_DEBUG"):
                traceback.print_exc()
    print(f"[{name}] stopped", flush=True)
