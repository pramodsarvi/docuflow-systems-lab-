"""Stress test: ingest a dataset and fire concurrent queries at the same time, while sampling autoscaling.

  python scripts/stress.py --api http://docuflow.localtest.me --dataset synthetic --pages 600 \
      --query-concurrency 8 --duration 900 --out docs/stress-report.md

What it does
  1. queues an ingest of --pages pages (mode vlm: the vision model really reads every page)
  2. runs --query-concurrency closed-loop query clients for the whole test (each sends a question, waits, repeats)
  3. every --sample seconds records: KEDA-driven replicas per deployment (kubectl), pending pods, RabbitMQ tokens per
     lane, jobs queued/running/done per lane, pages indexed, queries in flight, GPU memory/utilisation (nvidia-smi)
  4. --query-steps ramps the query clients (e.g. 8,24,48 every --step-seconds) to find where latency bends
  5. stops when ingest is finished (and --min-duration has passed) or at --duration; then, with --observe-after, stops
     all load and keeps sampling so the report shows KEDA scaling back down (workers to zero, vLLM and API to minimum)
  6. writes a markdown report: ingest throughput, query latency per load step, errors, and the scaling timeline
Questions are generated from the synthetic form fields (names, cities, document numbers) so answers can be checked.
"""
import argparse
import json
import os
import random
import shutil
import subprocess
import sys
import threading
import time

import httpx

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from docuflow import sources  # noqa: E402

DEPLOYS = ["api", "worker-io", "worker-vlm", "worker-embed", "model-vlm", "model-llm", "model-embed"]


def sh(cmd):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout.strip()
    except Exception:
        return ""


def k8s_snapshot(ns):
    out = sh(f"kubectl -n {ns} get deploy -o json")
    reps = {}
    try:
        for d in json.loads(out)["items"]:
            n = d["metadata"]["name"]
            if n in DEPLOYS:
                reps[n] = (d["status"].get("readyReplicas", 0) or 0, d["spec"].get("replicas", 0) or 0)
    except Exception:
        pass
    pending = sh(f"kubectl -n {ns} get pods --field-selector=status.phase=Pending --no-headers 2>/dev/null | wc -l")
    mq = sh(f"kubectl -n {ns} exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages_ready messages_unacknowledged 2>/dev/null")
    queues = {}
    for line in mq.splitlines():
        p = line.split()
        if len(p) == 3 and p[0].startswith("docuflow.") and p[1].isdigit():
            queues[p[0]] = int(p[1]) + int(p[2])
    return reps, int(pending or 0), queues


def gpu_snapshot():
    if not shutil.which("nvidia-smi"):
        return None
    q = sh("nvidia-smi --query-gpu=utilization.gpu,memory.used,memory.total --format=csv,noheader,nounits")
    try:
        u, used, tot = [float(x) for x in q.splitlines()[0].split(",")]
        return {"util": u, "mem_gb": round(used / 1024, 1), "mem_total_gb": round(tot / 1024, 1)}
    except Exception:
        return None


def make_questions(pages):
    qs = []
    for i in range(min(pages, 400)):
        _, gt = sources.synthetic_page(i)
        f = dict(line.split(": ", 1) for line in gt.splitlines()[1:])
        qs += [(f"Who signed document {f['Number']}?", f["Signed by"]),
               (f"What is the amount on {f['Number']}?", f["Amount"].split()[0]),
               (f"Which city is listed on {f['Number']}?", f["City"])]
    return qs


class Load:
    def __init__(self, api, headers, questions, concurrency):
        self.api, self.headers, self.qs, self.n = api, headers, questions, concurrency
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.results = []          # (t_start, seconds, ok_http, correct, during_ingest, clients_at_start)
        self.errors = []
        self.threads = []

    def client(self, seed):
        r = random.Random(seed)
        with httpx.Client(timeout=600) as c:
            while not self.stop.is_set():
                q, gold = r.choice(self.qs)
                t0 = time.time()
                try:
                    resp = c.post(self.api + "/query", json={"question": q, "max_steps": 4}, headers=self.headers)
                    ok = resp.status_code == 200
                    j = resp.json() if ok else {}
                    correct = ok and gold.lower() in (j.get("answer") or "").lower()
                    with self.lock:
                        self.results.append((t0, time.time() - t0, ok, correct, j.get("during_ingest"), len(self.threads)))
                        if not ok:
                            self.errors.append(f"HTTP {resp.status_code}: {resp.text[:120]}")
                except Exception as e:
                    with self.lock:
                        self.results.append((t0, time.time() - t0, False, False, None, len(self.threads)))
                        self.errors.append(f"{type(e).__name__}: {str(e)[:120]}")
                    time.sleep(1)

    def scale_to(self, n):
        """Add closed-loop clients until there are n (clients are never removed before the end)."""
        while len(self.threads) < n:
            t = threading.Thread(target=self.client, args=(len(self.threads),), daemon=True)
            with self.lock:
                self.threads.append(t)
            t.start()

    def start(self):
        self.scale_to(self.n)


def pct(xs, p):
    if not xs:
        return float("nan")
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=os.environ.get("DOCUFLOW_API", "http://docuflow.localtest.me"))
    ap.add_argument("--dataset", default="synthetic")
    ap.add_argument("--pages", type=int, default=600)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--mode", default="vlm", choices=["vlm", "gt"])
    ap.add_argument("--query-concurrency", type=int, default=8)
    ap.add_argument("--query-steps", default="", help="ramp, e.g. 8,24,48 (overrides --query-concurrency)")
    ap.add_argument("--step-seconds", type=int, default=300)
    ap.add_argument("--observe-after", type=int, default=0, help="seconds to keep sampling after load stops (scale-down)")
    ap.add_argument("--duration", type=int, default=1200, help="hard stop (s)")
    ap.add_argument("--min-duration", type=int, default=120)
    ap.add_argument("--sample", type=int, default=10)
    ap.add_argument("--namespace", default="docuflow")
    ap.add_argument("--out", default="docs/stress-report.md")
    a = ap.parse_args()
    steps = [int(x) for x in a.query_steps.split(",") if x.strip()] or [a.query_concurrency]
    a.query_concurrency = steps[-1]
    headers = {"x-api-key": os.environ.get("DOCUFLOW_API_KEY", "")}
    api = a.api.rstrip("/")

    print(f"ingest {a.pages} pages of {a.dataset} (mode={a.mode}) + query clients {steps} (step {a.step_seconds}s)", flush=True)
    w0 = httpx.get(api + "/workloads", headers=headers, timeout=30).json()
    base = {p["dataset"]: p for p in w0["progress"]}.get(a.dataset, {}).get("indexed", 0)   # pages from earlier runs
    httpx.post(api + "/ingest", json={"dataset": a.dataset, "limit": a.pages, "offset": a.offset, "mode": a.mode},
               headers=headers, timeout=60).raise_for_status()
    load = Load(api, headers, make_questions(a.offset + a.pages) if a.dataset == "synthetic" else [("What is this document about?", "")],
                steps[0])
    t0 = time.time()
    load.start()
    phase = "ingest+queries"
    samples, first_indexed, done_at = [], None, None
    try:
        while time.time() - t0 < a.duration:
            time.sleep(a.sample)
            t = round(time.time() - t0)
            if phase == "ingest+queries":
                load.scale_to(steps[min(len(steps) - 1, t // a.step_seconds)])
            try:
                w = httpx.get(api + "/workloads", headers=headers, timeout=30).json()
            except Exception as e:
                print(f"t={t}s /workloads failed: {e}", flush=True)
                continue
            reps, pending, queues = k8s_snapshot(a.namespace)
            prog = {p["dataset"]: p for p in w["progress"]}.get(a.dataset, {})
            indexed = prog.get("indexed", 0) - base
            if indexed and first_indexed is None:
                first_indexed = t
            with load.lock:
                recent = [r for r in load.results if r[0] >= time.time() - a.sample - 60]
            s = dict(t=t, phase=phase, clients=len(load.threads) if phase != "scale-down" else 0, reps=reps, pending=pending, queues=queues, lanes=w["lanes"], indexed=indexed,
                     inflight=w["queries_in_flight"], gpu=gpu_snapshot(), q_p50=pct([r[1] for r in recent if r[2]], 50),
                     q_n=len(load.results))
            samples.append(s)
            rs = " ".join(f"{k.replace('worker-', 'w-').replace('model-', 'm-')}={v[0]}/{v[1]}" for k, v in reps.items())
            qd = " ".join(f"{k.split('.', 1)[1]}={v}" for k, v in queues.items() if not k.endswith(".delay"))
            g = s["gpu"] or {}
            print(f"t={t:>4}s [{phase} c={s['clients']}] indexed={indexed:>4}/{a.pages} queues[{qd}] replicas[{rs}] pending={pending} "
                  f"inflight={s['inflight']} queries={s['q_n']} p50(60s)={s['q_p50']:.1f}s gpu={g.get('util', '-')}% {g.get('mem_gb', '-')}GB",
                  flush=True)
            busy = any(v["queued"] or v["running"] for v in w["lanes"].values())
            if indexed >= a.pages and not busy and done_at is None:
                done_at = t
            if phase == "ingest+queries" and done_at is not None and t >= a.min_duration:
                if not a.observe_after:
                    break
                print(f"--- load stopped at t={t}s; observing scale-down for {a.observe_after}s", flush=True)
                load.stop.set()
                phase, stop_at = "scale-down", t + a.observe_after
            if phase == "scale-down" and t >= stop_at:
                break
    finally:
        load.stop.set()
    for th in load.threads:
        th.join(timeout=600)
    report(a, samples, load, t0, first_indexed, done_at)


def report(a, samples, load, t0, first_indexed, done_at):
    res = load.results
    during = [r for r in res if r[4]]
    lat = [r[1] for r in res if r[2]]
    lat_d = [r[1] for r in during if r[2]]
    total = time.time() - t0
    last = samples[-1] if samples else {}
    indexed = last.get("indexed", 0)
    peak = {}
    for s in samples:
        for k, (ready, want) in s["reps"].items():
            peak[k] = max(peak.get(k, 0), ready, want)
    lines = [f"# docuflow stress test ({time.strftime('%Y-%m-%d %H:%M')})", "",
             f"Ingest of **{a.pages} {a.dataset} pages** (mode `{a.mode}`) with **{a.query_concurrency} concurrent query clients** "
             f"running the whole time (ramp: {a.query_steps or a.query_concurrency}, {a.step_seconds}s per step). Duration {total:.0f}s"
             f"{f', then {a.observe_after}s of observation with all load stopped' if a.observe_after else ''}.", "",
             "## Results", "", "| metric | value |", "|---|---|",
             f"| pages indexed | {indexed} / {a.pages} |",
             f"| ingest finished at | {f'{done_at}s' if done_at else 'not finished in time'} |",
             f"| first page queryable at | {first_indexed}s |" if first_indexed else "| first page queryable at | - |",
             f"| ingest throughput | {indexed / done_at * 60:.1f} pages/min |" if done_at else
             f"| ingest throughput | {indexed / max(1, last.get('t', 1)) * 60:.1f} pages/min (partial) |",
             f"| queries answered | {len(lat)} ok, {len(res) - len(lat)} failed ({(len(res) - len(lat)) / max(1, len(res)):.1%} errors) |",
             f"| queries with a correct answer | {sum(r[3] for r in res)} ({sum(r[3] for r in res) / max(1, len(res)):.0%}) |",
             f"| query latency during ingest p50 / p95 / max | {pct(lat_d, 50):.1f}s / {pct(lat_d, 95):.1f}s / {max(lat_d, default=float('nan')):.1f}s |",
             f"| query latency overall p50 / p95 | {pct(lat, 50):.1f}s / {pct(lat, 95):.1f}s |",
             f"| query throughput | {len(lat) / total * 60:.1f} queries/min |", "",
             "## Query latency by load step", "", "| concurrent clients | queries ok | failed | p50 | p95 | max | queries/min |", "|---|---|---|---|---|---|---|"]
    for n in sorted({r[5] for r in res}):
        rs = [r for r in res if r[5] == n]
        ok = [r[1] for r in rs if r[2]]
        span = max(1.0, max(r[0] + r[1] for r in rs) - min(r[0] for r in rs))
        lines.append(f"| {n} | {len(ok)} | {len(rs) - len(ok)} | {pct(ok, 50):.1f}s | {pct(ok, 95):.1f}s | {max(ok, default=float('nan')):.1f}s | {len(ok) / span * 60:.1f} |")
    lines += ["", "## Peak replicas (KEDA)", "", "| deployment | peak | at end |", "|---|---|---|"]
    lines += [f"| {k} | {v} | {last.get('reps', {}).get(k, (0, 0))[1]} |" for k, v in sorted(peak.items())]
    lines += ["", "## Timeline", "", "| t (s) | phase | clients | indexed | tokens io/vlm/embed | w-io | w-vlm | w-embed | api | m-vlm | pending | in-flight | GPU util / mem |",
              "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in samples:
        q = s["queues"]; r = s["reps"]; g = s["gpu"] or {}
        rr = lambda k, r=r: f"{r.get(k, (0, 0))[0]}/{r.get(k, (0, 0))[1]}"
        lines.append(f"| {s['t']} | {s['phase']} | {s['clients']} | {s['indexed']} | {q.get('docuflow.io', '-')}/{q.get('docuflow.vlm', '-')}/{q.get('docuflow.embed', '-')} | "
                     f"{rr('worker-io')} | {rr('worker-vlm')} | {rr('worker-embed')} | {rr('api')} | {rr('model-vlm')} | {s['pending']} | "
                     f"{s['inflight']} | {g.get('util', '-')}% / {g.get('mem_gb', '-')} GB |")
    if load.errors:
        lines += ["", "## Errors (first 10)", ""] + [f"- `{e}`" for e in load.errors[:10]]
    lines += ["", "Replicas are `ready/desired`. Tokens are RabbitMQ messages (ready + unacked) per lane queue: the KEDA input."]
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    open(a.out, "w").write("\n".join(lines) + "\n")
    print("\n".join(lines[:22]))
    print(f"\nreport written to {a.out}")


if __name__ == "__main__":
    main()
