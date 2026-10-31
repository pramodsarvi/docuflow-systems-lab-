"""HTTP API: ingest, query, workload control, and a small dashboard."""
from contextlib import asynccontextmanager
from typing import Optional

import hmac
import json
import os

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from . import agent, config, db, jobs, llm, retrieval, service


@asynccontextmanager
async def lifespan(_):
    db.init_schema()
    yield


app = FastAPI(title="docuflow", lifespan=lifespan)
API_KEY = os.environ.get("DOCUFLOW_API_KEY", "")   # empty = open (local dev); set it before exposing the API
OPEN_PATHS = {"/health", "/ready", "/metrics", "/"}   # probes and scraping stay open; put the Service behind the cluster network


@app.exception_handler(llm.ModelUnavailable)
async def model_unavailable(_request, exc):
    return Response(json.dumps({"detail": f"model server unavailable, retry shortly: {str(exc)[:200]}"}), status_code=503,
                    media_type="application/json", headers={"Retry-After": "30"})


@app.middleware("http")
async def require_key(request: Request, call_next):
    if API_KEY and request.url.path not in OPEN_PATHS:
        given = request.headers.get("x-api-key", "")
        if not hmac.compare_digest(given, API_KEY):
            return Response('{"detail":"invalid or missing X-API-Key"}', status_code=401, media_type="application/json")
    return await call_next(request)


class IngestReq(BaseModel):
    dataset: str
    limit: int = 10
    offset: int = 0
    split: Optional[str] = None
    mode: str = "vlm"          # vlm = read pages with the vision model; gt = use the dataset's own transcription (no GPU)
    priority: int = 0


class QueryReq(BaseModel):
    question: str
    dataset: Optional[str] = None
    lang: Optional[str] = None
    max_steps: Optional[int] = None


class LaneReq(BaseModel):
    lane: str
    limit: int


class PauseReq(BaseModel):
    kind: str   # lanes | datasets
    name: str


@app.get("/health")
def health():
    return {"ok": True, "models": {"vlm": config.VLM_MODEL, "llm": config.LLM_MODEL, "embed": config.EMBED_MODEL}}


@app.get("/ready")
def ready():
    try:
        with db.pool().connection() as c:
            c.execute("SELECT 1")
    except Exception as e:
        raise HTTPException(503, f"database unavailable: {e}")
    return {"ready": True}


@app.get("/metrics")
def metrics():
    """Prometheus text format, computed from the database at scrape time."""
    out = []

    def gauge(name, help_, rows):
        out.append(f"# HELP {name} {help_}\n# TYPE {name} gauge")
        out.extend(f"{name}{{{labels}}} {v}" if labels else f"{name} {v}" for labels, v in rows)

    with db.pool().connection() as c:
        jobs_ = c.execute("SELECT lane, status, count(*) n FROM jobs GROUP BY 1,2").fetchall()
        docs = c.execute("SELECT dataset, status, count(*) n FROM documents GROUP BY 1,2").fetchall()
        oldest = c.execute("SELECT lane, extract(epoch FROM now()-min(created_at)) s FROM jobs WHERE status='queued' GROUP BY 1").fetchall()
        q = c.execute("SELECT during_ingest, count(*) n, coalesce(sum(seconds),0) total FROM queries GROUP BY 1").fetchall()
    gauge("docuflow_jobs", "Jobs by lane and status", [(f'lane="{r["lane"]}",status="{r["status"]}"', r["n"]) for r in jobs_])
    gauge("docuflow_documents", "Documents by dataset and status", [(f'dataset="{r["dataset"]}",status="{r["status"]}"', r["n"]) for r in docs])
    gauge("docuflow_oldest_queued_age_seconds", "Age of the oldest queued job per lane", [(f'lane="{r["lane"]}"', round(r["s"])) for r in oldest])
    gauge("docuflow_queries_in_flight", "Interactive queries currently running", [("", jobs.queries_in_flight())])
    gauge("docuflow_queries_total", "Answered queries", [(f'during_ingest="{r["during_ingest"]}"', r["n"]) for r in q])
    gauge("docuflow_query_seconds_sum", "Total query latency", [(f'during_ingest="{r["during_ingest"]}"', round(r["total"], 2)) for r in q])
    with db.pool().connection() as c:
        lat = c.execute("""SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY seconds) p50,
                                  percentile_cont(0.95) WITHIN GROUP (ORDER BY seconds) p95,
                                  percentile_cont(0.99) WITHIN GROUP (ORDER BY seconds) p99, count(*) n
                           FROM queries WHERE created_at > now() - interval '5 minutes'""").fetchone()
        reads = c.execute("""SELECT d.dataset, avg(e.seconds) s, count(*) n FROM extractions e JOIN documents d ON d.id=e.doc_id
                             WHERE d.indexed_at > now() - interval '15 minutes' AND NOT e.used_fallback GROUP BY 1""").fetchall()
        fb = c.execute("""SELECT d.dataset, count(*) n FROM extractions e JOIN documents d ON d.id=e.doc_id
                          WHERE e.used_fallback GROUP BY 1""").fetchall()
        deferrals = c.execute("SELECT lane, coalesce(sum(outages),0) n FROM jobs GROUP BY 1").fetchall()
    gauge("docuflow_query_latency_seconds", "Query latency over the last 5 minutes",
          [(f'quantile="{k}"', round(lat[f"p{k[2:]}"] or 0, 2)) for k in ("0.50", "0.95", "0.99")] if lat["n"] else [])
    gauge("docuflow_queries_last_5m", "Queries answered in the last 5 minutes", [("", lat["n"])])
    gauge("docuflow_read_seconds_avg", "Mean vision-model read time per page over the last 15 minutes",
          [(f'dataset="{r["dataset"]}"', round(r["s"] or 0, 2)) for r in reads])
    gauge("docuflow_fallback_pages", "Pages indexed from the dataset's own transcription instead of a vision-model read",
          [(f'dataset="{r["dataset"]}"', r["n"]) for r in fb])
    gauge("docuflow_outage_deferrals", "Jobs deferred because a model server was unavailable (cumulative)",
          [(f'lane="{r["lane"]}"', r["n"]) for r in deferrals])
    gauge("docuflow_queries_paused", "1 while queries are paused (ingest-priority mode)", [("", int(bool(db.control_get("queries_paused", False))))])
    agent = "gpu" if "model-llm" in (config.ENDPOINTS["llm"][0] or "") else ("mac" if "mac-llm" in (config.ENDPOINTS["llm"][0] or "") else "other")
    gauge("docuflow_info", "Which models serve each role", [(f'agent_backend="{agent}",llm_model="{config.LLM_MODEL}",vlm_model="{config.VLM_MODEL}"', 1)])
    return Response("\n".join(out) + "\n", media_type="text/plain; version=0.0.4")


@app.post("/ingest")
def ingest(r: IngestReq):
    try:
        return service.ingest(r.dataset, r.limit, r.offset, r.split, r.mode, r.priority)
    except ValueError as e:
        raise HTTPException(400, str(e))


class QueriesReq(BaseModel):
    paused: bool


@app.post("/workloads/queries")
def pause_queries(r: QueriesReq):
    """Ingest-priority mode: refuse interactive queries (503) so the GPU can go to page reading."""
    db.control_set("queries_paused", r.paused)
    return {"queries_paused": r.paused}


@app.post("/query")
async def query(r: QueryReq):
    if db.control_get("queries_paused", False):
        raise HTTPException(503, "queries are paused (ingest-priority mode); resume with POST /workloads/queries {\"paused\": false}",
                            headers={"Retry-After": "300"})
    # Runs in a thread so queries are served while workers keep ingesting.
    return await run_in_threadpool(agent.run, r.question, r.dataset, r.max_steps, r.lang)


@app.get("/search")
def search(q: str, k: int = 5, dataset: Optional[str] = None, kind: Optional[str] = None):
    return retrieval.search(q, k, dataset, kind)


@app.get("/workloads")
def workloads():
    return {**jobs.snapshot(), "progress": service.progress(), "queries_paused": bool(db.control_get("queries_paused", False))}


@app.post("/workloads/lane")
def lane(r: LaneReq):
    try:
        return service.set_lane_limit(r.lane, r.limit)
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/workloads/pause")
def pause(r: PauseReq):
    return service.pause(r.kind, r.name, True)


@app.post("/workloads/resume")
def resume(r: PauseReq):
    return service.pause(r.kind, r.name, False)


@app.post("/workloads/reread-fallbacks")
def reread_fallbacks(dataset: Optional[str] = None):
    """Re-read with the vision model every page that was indexed from the dataset's own transcription."""
    return service.reread_fallbacks(dataset)


@app.post("/workloads/retry-dead")
def retry_dead(dataset: Optional[str] = None):
    return {"requeued": jobs.retry_dead(dataset)}


@app.get("/jobs")
def list_jobs(status: Optional[str] = None, limit: int = 50):
    with db.pool().connection() as c:
        return c.execute("SELECT id,dataset,stage,lane,status,attempts,priority,error,locked_by,created_at,finished_at FROM jobs "
                         "WHERE (%s::text IS NULL OR status=%s) ORDER BY id DESC LIMIT %s", (status, status, limit)).fetchall()


@app.get("/stats")
def stats():
    return retrieval.corpus_stats()


@app.get("/documents")
def list_documents(dataset: Optional[str] = None, status: Optional[str] = None, ids: Optional[str] = None, limit: int = 100):
    """Document ids and pipeline status. `ids` is a comma-separated list (used by the smoke test)."""
    id_list = [i for i in (ids or "").split(",") if i] or None
    with db.pool().connection() as c:
        return c.execute("""SELECT id, dataset, status, lang, created_at, indexed_at FROM documents
                            WHERE (%(ds)s::text IS NULL OR dataset=%(ds)s) AND (%(st)s::text IS NULL OR status=%(st)s)
                              AND (%(ids)s::text[] IS NULL OR id = ANY(%(ids)s))
                            ORDER BY created_at DESC LIMIT %(lim)s""",
                         {"ds": dataset, "st": status, "ids": id_list, "lim": min(limit, 1000)}).fetchall()


@app.get("/documents/{doc_id:path}/image")
def doc_image(doc_id: str):
    with db.pool().connection() as c:
        r = c.execute("SELECT image FROM documents WHERE id=%s", (doc_id,)).fetchone()
    if not r:
        raise HTTPException(404)
    return Response(bytes(r["image"]), media_type="image/jpeg")


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD


DASHBOARD = """<!doctype html><meta charset=utf-8><title>docuflow</title>
<style>body{font:14px system-ui;margin:2rem;max-width:960px}table{border-collapse:collapse;margin:.5rem 0}td,th{border:1px solid #ccc;padding:.25rem .6rem;text-align:right}
th:first-child,td:first-child{text-align:left}pre{background:#f4f4f4;padding:.6rem;white-space:pre-wrap}input{width:70%;padding:.4rem}</style>
<h2>docuflow</h2><div id=w>loading…</div><h3>Ask</h3><input id=q placeholder="e.g. Who is the fax addressed to?"> <button onclick=ask()>Ask</button><pre id=a></pre>
<script>
const H=()=>({'x-api-key':localStorage.k||''});
async function tick(){const r=await fetch('/workloads',{headers:H()});if(r.status==401){localStorage.k=prompt('API key')||'';return}const j=await r.json();let h='<b>lanes</b><table><tr><th>lane<th>limit<th>queued<th>running<th>done<th>dead<th>done/5m<th>avg s</tr>';
for(const [l,v] of Object.entries(j.lanes))h+=`<tr><td>${l}<td>${j.limits[l]}<td>${v.queued}<td>${v.running}<td>${v.done}<td>${v.dead}<td>${v.done_5m||0}<td>${v.avg_seconds||''}</tr>`;
h+='</table><b>documents</b><table><tr><th>dataset<th>fetched<th>extracted<th>indexed<th>total</tr>';
for(const p of j.progress)h+=`<tr><td>${p.dataset}<td>${p.fetched}<td>${p.extracted}<td>${p.indexed}<td>${p.total}</tr>`;
h+=`</table>queries in flight: ${j.queries_in_flight} &nbsp; paused: ${JSON.stringify(j.paused)}${j.queries_paused?' &nbsp; <b>queries paused (ingest priority)</b>':''}`;document.getElementById('w').innerHTML=h}
async function ask(){const a=document.getElementById('a');a.textContent='thinking…';const r=await fetch('/query',{method:'POST',headers:{...H(),'content-type':'application/json'},body:JSON.stringify({question:document.getElementById('q').value})});const j=await r.json();
a.textContent=j.answer+'\\n\\ncitations: '+(j.citations||[]).join(', ')+'\\n('+j.steps+' steps, '+j.seconds+'s, during_ingest='+j.during_ingest+')\\n\\n'+j.trace.map(t=>`${t.step}. ${t.action}: ${t.thought||''}`).join('\\n')}
tick();setInterval(tick,2000)</script>"""
