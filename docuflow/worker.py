"""Worker process: polls one lane, runs handlers, records success/failure.

Each worker also serves a tiny HTTP endpoint (WORKER_METRICS_PORT, default 9100):
  /health   200 while the claim loop is alive (idle loop seen in the last 60s, or the current job is within its lease)
  /metrics  Prometheus counters for this process: jobs by stage and result, time spent, busy flag
Kubernetes uses /health as the liveness probe, so a worker stuck past its lease is restarted (and lease expiry requeues
the job), and Prometheus scrapes /metrics through a PodMonitor."""
import os
import signal
import threading
import time
import traceback
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg

from . import broker, config, db, jobs, pipeline

_stop = False
IDLE_STALE_SECONDS = 60


class _State:
    def __init__(self, lane):
        self.lane = lane
        self.lock = threading.Lock()
        self.last_loop = time.time()
        self.busy_since = None
        self.started = time.time()
        self.counts = defaultdict(int)       # (stage, result) -> n
        self.seconds = defaultdict(float)    # stage -> total seconds

    def healthy(self):
        now = time.time()
        with self.lock:
            if self.busy_since is not None:
                return now - self.busy_since < config.LEASE_SECONDS
            return now - self.last_loop < IDLE_STALE_SECONDS

    def record(self, stage, result, secs):
        with self.lock:
            self.counts[(stage, result)] += 1
            self.seconds[stage] += secs

    def render(self):
        lane = self.lane
        with self.lock:
            out = ["# HELP docuflow_worker_jobs_total Jobs finished by this worker", "# TYPE docuflow_worker_jobs_total counter"]
            out += [f'docuflow_worker_jobs_total{{lane="{lane}",stage="{s}",result="{r}"}} {n}' for (s, r), n in sorted(self.counts.items())]
            out += ["# HELP docuflow_worker_job_seconds_total Seconds spent running jobs", "# TYPE docuflow_worker_job_seconds_total counter"]
            out += [f'docuflow_worker_job_seconds_total{{lane="{lane}",stage="{s}"}} {v:.3f}' for s, v in sorted(self.seconds.items())]
            busy = 1 if self.busy_since is not None else 0
        out += ["# HELP docuflow_worker_busy 1 while a job is running", "# TYPE docuflow_worker_busy gauge",
                f'docuflow_worker_busy{{lane="{lane}"}} {busy}',
                "# HELP docuflow_worker_start_time_seconds Process start time", "# TYPE docuflow_worker_start_time_seconds gauge",
                f'docuflow_worker_start_time_seconds{{lane="{lane}"}} {self.started:.0f}']
        return "\n".join(out) + "\n"


def _serve(state, port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/health":
                ok = state.healthy()
                body, code, ctype = (b'{"ok":true}' if ok else b'{"ok":false}'), (200 if ok else 503), "application/json"
            elif self.path == "/metrics":
                body, code, ctype = state.render().encode(), 200, "text/plain; version=0.0.4"
            else:
                body, code, ctype = b"not found", 404, "text/plain"
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _next_job(lane, name):
    """With RabbitMQ: take a token, then claim the best eligible job in Postgres. Without: poll Postgres."""
    if not broker.enabled():
        job = jobs.claim(lane, name)
        if not job:
            time.sleep(0.5)
        return job
    tag = broker.take(lane)
    if tag is None:
        time.sleep(0.5)
        return None
    try:
        job = jobs.claim(lane, name)
    except Exception:
        broker.give_back(tag)
        raise
    if job:
        broker.done(tag)          # the job row (lease, retries) now tracks the work; the token has done its job
        return job
    if broker.runnable(lane) == 0:
        broker.done(tag)          # surplus token (duplicate publish, paused dataset): drop it
    else:
        broker.give_back(tag)     # work exists but rules hold it back (lane limit, query yield): try again shortly
        time.sleep(1)
    return None


def run(lane):
    global _stop
    signal.signal(signal.SIGTERM, lambda *_: globals().__setitem__("_stop", True))
    signal.signal(signal.SIGINT, lambda *_: globals().__setitem__("_stop", True))
    name = f"{lane}-{os.environ.get('HOSTNAME') or os.getpid()}"
    state = _State(lane)
    if config.WORKER_METRICS_PORT:
        try:
            _serve(state, config.WORKER_METRICS_PORT)
        except OSError as e:     # several local workers share a host (supervisor mode): only the first gets the port
            print(f"[{name}] metrics server disabled: {e}", flush=True)
    while not _stop:
        state.last_loop = time.time()
        try:
            db.init_schema()      # pods can start before the API or database is ready
            break
        except psycopg.OperationalError as e:
            print(f"[{name}] waiting for database: {e}", flush=True)
            time.sleep(3)
    last_reap = 0
    print(f"[{name}] started", flush=True)
    while not _stop:
        state.last_loop = time.time()
        if time.time() - last_reap > 30:
            try:
                n = jobs.reap()
                if n:
                    print(f"[{name}] requeued {n} expired jobs", flush=True)
                n = broker.reconcile(lane)
                if n:
                    print(f"[{name}] reconciled: published {n} missing tokens", flush=True)
            except Exception as e:
                print(f"[{name}] maintenance skipped: {e}", flush=True)
            last_reap = time.time()
        try:
            job = _next_job(lane, name)
        except psycopg.OperationalError as e:       # database restarting: wait and let the pool reconnect
            print(f"[{name}] database unavailable ({e}); retrying", flush=True)
            time.sleep(3)
            continue
        except Exception as e:                      # broker restarting
            print(f"[{name}] broker unavailable ({e}); retrying", flush=True)
            time.sleep(3)
            continue
        if not job:
            continue
        t0 = time.perf_counter()
        with state.lock:
            state.busy_since = time.time()
        try:
            out = pipeline.HANDLERS[job["stage"]](job)
            jobs.complete(job["id"])
            state.record(job["stage"], "ok", time.perf_counter() - t0)
            print(f"[{name}] {job['stage']} #{job['id']} {job['dataset']} ok ({out}) {time.perf_counter() - t0:.1f}s", flush=True)
        except Exception as e:
            try:
                dead = jobs.fail(job, f"{type(e).__name__}: {e}")
            except psycopg.OperationalError:
                dead = False        # lease expiry will requeue it
            state.record(job["stage"], "dead" if dead else "retry", time.perf_counter() - t0)
            print(f"[{name}] {job['stage']} #{job['id']} FAILED{' (dead)' if dead else ''}: {e}", flush=True)
            if os.environ.get("DOCUFLOW_DEBUG"):
                traceback.print_exc()
        finally:
            with state.lock:
                state.busy_since = None
    print(f"[{name}] stopped", flush=True)
