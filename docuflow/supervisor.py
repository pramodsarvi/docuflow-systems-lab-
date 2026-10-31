"""Starts the API plus the worker processes for every lane, and restarts anything that dies."""
import os
import signal
import subprocess
import sys
import time

from . import config


def main(host="127.0.0.1", port=8791):
    py = sys.executable
    procs = {}

    def spawn(key):
        if key == "api":
            return subprocess.Popen([py, "-m", "uvicorn", "docuflow.api:app", "--host", host, "--port", str(port), "--log-level", "warning"])
        # Local mode runs many workers on one host; only the first per lane would get a metrics port, so turn it off.
        return subprocess.Popen([py, "-m", "docuflow", "worker", key.split(":")[0]],
                                env={**os.environ, "WORKER_METRICS_PORT": os.environ.get("WORKER_METRICS_PORT", "0")})

    keys = ["api"] + [f"{lane}:{i}" for lane, n in config.LANE_PROCS.items() for i in range(n)]
    for k in keys:
        procs[k] = spawn(k)
    print(f"docuflow up: api http://{host}:{port}  workers {config.LANE_PROCS}", flush=True)
    stopping = []
    signal.signal(signal.SIGINT, lambda *_: stopping.append(1))
    signal.signal(signal.SIGTERM, lambda *_: stopping.append(1))
    while not stopping:
        for k, p in list(procs.items()):
            if p.poll() is not None:
                print(f"[supervisor] {k} exited {p.returncode}, restarting", flush=True)
                procs[k] = spawn(k)
        time.sleep(1)
    for p in procs.values():
        p.terminate()
    for p in procs.values():
        try:
            p.wait(timeout=20)
        except subprocess.TimeoutExpired:
            p.kill()
