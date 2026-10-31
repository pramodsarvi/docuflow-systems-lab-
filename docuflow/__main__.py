"""CLI: python -m docuflow <command>"""
import argparse
import json
import os
import time

import httpx

from . import config, db


def _api(path, method="GET", base=None, **kw):
    base = base or os.environ.get("DOCUFLOW_API", "http://127.0.0.1:8791")
    r = httpx.request(method, base + path, timeout=900, headers={"x-api-key": os.environ.get("DOCUFLOW_API_KEY", "")}, **kw)
    r.raise_for_status()
    return r.json()


def main():
    ap = argparse.ArgumentParser(prog="docuflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init", help="create schema")
    sub.add_parser("reset", help="wipe all data")
    u = sub.add_parser("up", help="start api + workers"); u.add_argument("--port", type=int, default=8791)
    w = sub.add_parser("worker"); w.add_argument("lane", choices=list(config.LANE_PROCS))
    i = sub.add_parser("ingest"); i.add_argument("dataset"); i.add_argument("-n", "--limit", type=int, default=10)
    i.add_argument("--offset", type=int, default=0); i.add_argument("--mode", default="vlm", choices=["vlm", "gt"])
    i.add_argument("--priority", type=int, default=0)
    a = sub.add_parser("ask"); a.add_argument("question"); a.add_argument("--dataset"); a.add_argument("--trace", action="store_true")
    sub.add_parser("status")
    wt = sub.add_parser("watch"); wt.add_argument("--every", type=float, default=3)
    ln = sub.add_parser("lane"); ln.add_argument("lane"); ln.add_argument("limit", type=int)
    for n in ("pause", "resume"):
        p = sub.add_parser(n); p.add_argument("kind", choices=["lanes", "datasets"]); p.add_argument("name")
    sub.add_parser("retry-dead")
    args = ap.parse_args()

    if args.cmd == "init":
        db.init_schema(); print("schema ready")
    elif args.cmd == "reset":
        db.init_schema(); db.reset(); print("all data removed")
    elif args.cmd == "up":
        from . import jobs, supervisor
        db.init_schema()
        n = jobs.requeue_running()
        if n:
            print(f"requeued {n} orphaned jobs")
        supervisor.main(port=args.port)
    elif args.cmd == "worker":
        from . import worker
        worker.run(args.lane)
    elif args.cmd == "ingest":
        print(json.dumps(_api("/ingest", "POST", json=dict(dataset=args.dataset, limit=args.limit, offset=args.offset, mode=args.mode, priority=args.priority))))
    elif args.cmd == "ask":
        r = _api("/query", "POST", json=dict(question=args.question, dataset=args.dataset))
        if args.trace:
            for t in r["trace"]:
                print(f"  {t['step']}. {t.get('action')}: {t.get('thought', '')[:140]}")
        print(f"\n{r['answer']}\n\ncitations: {', '.join(r['citations'])}  ({r['steps']} steps, {r['seconds']}s, during_ingest={r['during_ingest']})")
    elif args.cmd in ("status", "watch"):
        while True:
            _print_status(_api("/workloads"))
            if args.cmd == "status":
                break
            time.sleep(args.every)
    elif args.cmd == "lane":
        print(_api("/workloads/lane", "POST", json=dict(lane=args.lane, limit=args.limit)))
    elif args.cmd in ("pause", "resume"):
        print(_api(f"/workloads/{args.cmd}", "POST", json=dict(kind=args.kind, name=args.name)))
    elif args.cmd == "retry-dead":
        print(_api("/workloads/retry-dead", "POST"))


def _print_status(j):
    print(f"\n{'lane':6}{'limit':>6}{'queued':>8}{'running':>8}{'done':>7}{'dead':>6}{'avg_s':>8}")
    for l, v in j["lanes"].items():
        print(f"{l:6}{j['limits'][l]:>6}{v['queued']:>8}{v['running']:>8}{v['done']:>7}{v['dead']:>6}{v.get('avg_seconds', ''):>8}")
    print(f"\n{'dataset':8}{'fetched':>9}{'extracted':>10}{'indexed':>9}{'total':>7}")
    for p in j["progress"]:
        print(f"{p['dataset']:8}{p['fetched']:>9}{p['extracted']:>10}{p['indexed']:>9}{p['total']:>7}")
    print(f"queries in flight: {j['queries_in_flight']}  paused: {j['paused']}")


if __name__ == "__main__":
    main()
