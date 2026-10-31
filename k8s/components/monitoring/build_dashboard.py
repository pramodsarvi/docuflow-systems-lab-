"""Generate dashboard.json (Grafana, picked up by the kube-prometheus-stack sidecar).

    python k8s/components/monitoring/build_dashboard.py

Rows answer an operator's questions in order: how far along is ingest and is anything wrong (Overview); where is the
work (Ingest pipeline); what is KEDA deciding and why (Autoscaling); are the model servers saturated (vLLM); how are
users served (Queries); is the hardware the limit (GPU & cluster).

Gauges computed from the database are reported identically by every API replica, so they are aggregated with max().
"""
import json
import os

DS = {"type": "prometheus", "uid": "${datasource}"}
NS = 'namespace="docuflow"'
panels = []
y = 0


def T(expr, legend="", instant=False):
    t = {"expr": expr, "legendFormat": legend, "datasource": DS, "refId": chr(65 + len(_targets))}
    if instant:
        t["instant"] = True
        t["range"] = False
    _targets.append(t)
    return t


_targets = []


def _take():
    global _targets
    t, _targets = _targets, []
    return t


def row(title):
    global y
    panels.append({"type": "row", "title": title, "collapsed": False, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1}, "panels": []})
    y += 1


def stat(title, x, w, unit="short", thresholds=None, desc="", decimals=None, mappings=None, text_mode="value", color_mode="background"):
    th = thresholds or [{"color": "green", "value": None}]
    p = {"type": "stat", "title": title, "description": desc, "gridPos": {"x": x, "y": y, "w": w, "h": 4}, "datasource": DS,
         "targets": _take(),
         "fieldConfig": {"defaults": {"unit": unit, "thresholds": {"mode": "absolute", "steps": th}, "mappings": mappings or [],
                                      **({"decimals": decimals} if decimals is not None else {})}, "overrides": []},
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}, "colorMode": color_mode, "graphMode": "area",
                     "textMode": text_mode, "justifyMode": "center"}}
    panels.append(p)


def ts(title, x, w, h=8, unit="short", stack=False, desc="", fill=12, max_=None, overrides=None):
    d = {"unit": unit, "custom": {"fillOpacity": fill, "lineWidth": 2, "showPoints": "never",
                                  "stacking": {"mode": "normal" if stack else "none"}}}
    if max_ is not None:
        d["max"] = max_
    panels.append({"type": "timeseries", "title": title, "description": desc, "gridPos": {"x": x, "y": y, "w": w, "h": h},
                   "datasource": DS, "targets": _take(), "fieldConfig": {"defaults": d, "overrides": overrides or []},
                   "options": {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                               "tooltip": {"mode": "multi", "sort": "desc"}}})


def bars(title, x, w, h=8, unit="percent", thresholds=None, desc="", max_=100):
    panels.append({"type": "bargauge", "title": title, "description": desc, "gridPos": {"x": x, "y": y, "w": w, "h": h},
                   "datasource": DS, "targets": _take(),
                   "fieldConfig": {"defaults": {"unit": unit, "min": 0, "max": max_,
                                                "thresholds": {"mode": "absolute", "steps": thresholds or [
                                                    {"color": "blue", "value": None}]}}, "overrides": []},
                   "options": {"displayMode": "gradient", "orientation": "horizontal", "showUnfilled": True,
                               "reduceOptions": {"calcs": ["lastNotNull"], "fields": ""}, "namePlacement": "left"}})


RED = lambda at: [{"color": "green", "value": None}, {"color": "red", "value": at}]
AMBER = lambda at: [{"color": "green", "value": None}, {"color": "orange", "value": at}]
INDEXED = 'sum(max by (dataset) (docuflow_documents{status="indexed"}))'
LEFT = 'sum(max by (dataset, status) (docuflow_documents{status!="indexed"}))'
RATE = 'sum(rate(docuflow_worker_jobs_total{stage="embed",result="ok"}[10m])) * 60'
HPA = lambda m: f'label_replace({m}{{{NS}}}, "target", "$1", "horizontalpodautoscaler", "keda-hpa-(.*)")'
APP = lambda e: f'sum by (app) (label_replace({e}, "app", "$1", "pod", "(.+)-[a-z0-9]{{8,10}}-[a-z0-9]{{5}}|(.+)-[0-9]+"))'

# ---------------------------------------------------------------- Overview
row("Overview")
T(INDEXED); stat("Pages indexed", 0, 4, desc="Pages searchable now (all datasets)")
T(LEFT); stat("Pages waiting to be read", 4, 4, desc="Fetched pages not yet indexed", thresholds=[{"color": "blue", "value": None}])
T(RATE); stat("Ingest rate", 8, 4, unit="short", decimals=0, desc="Pages indexed per minute (10 min average)",
              thresholds=[{"color": "blue", "value": None}])
panels[-1]["fieldConfig"]["defaults"]["unit"] = "suffix: pages/min"
T(f"({LEFT}) / ({RATE})"); stat("ETA for the backlog", 12, 4, unit="m", decimals=0, desc="Pages waiting / current ingest rate",
                               thresholds=[{"color": "blue", "value": None}])
T('sum(max by (lane) (docuflow_jobs{status="dead"})) or vector(0)'); stat("Dead jobs", 16, 4, thresholds=RED(1),
                                                                           desc="Jobs that used all retries: GET /jobs?status=dead")
T('sum(max by (dataset) (docuflow_fallback_pages)) or vector(0)'); stat("Fallback pages", 20, 4, thresholds=AMBER(1),
                                                                      desc="Indexed from dataset text, not a vision read: POST /workloads/reread-fallbacks")
y += 4
T("max(docuflow_queries_paused)")
stat("Queries", 0, 4, mappings=[{"type": "value", "options": {"0": {"text": "open", "color": "green"},
                                                               "1": {"text": "paused (ingest mode)", "color": "orange"}}}])
T("max by (agent_backend) (docuflow_info)", "{{agent_backend}}")
stat("Agent model on", 4, 4, text_mode="name", color_mode="none", desc="gpu = vLLM model-llm, mac = Ollama via tunnel")
T('max(docuflow_query_latency_seconds{quantile="0.95"})'); stat("Query p95 (5 min)", 8, 4, unit="s", decimals=1,
                                                               thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 15},
                                                                           {"color": "red", "value": 30}])
T("max(docuflow_queries_in_flight)"); stat("Queries in flight", 12, 4, thresholds=[{"color": "blue", "value": None}])
T("avg(DCGM_FI_DEV_GPU_UTIL)"); stat("GPU utilisation", 16, 4, unit="percent", thresholds=[{"color": "blue", "value": None}])
T("sum(DCGM_FI_DEV_FB_USED) / (sum(DCGM_FI_DEV_FB_USED) + sum(DCGM_FI_DEV_FB_FREE)) * 100")
stat("GPU memory used", 20, 4, unit="percent", decimals=0, thresholds=[{"color": "green", "value": None}, {"color": "orange", "value": 90},
                                                                       {"color": "red", "value": 97}])
y += 4

# ---------------------------------------------------------------- Ingest
row("Ingest pipeline")
T('max by (dataset) (docuflow_documents{status="indexed"}) / sum by (dataset) (max by (dataset, status) (docuflow_documents)) * 100',
  "{{dataset}}")
bars("Progress per dataset (indexed / fetched)", 0, 8, desc="Share of each dataset's fetched pages that are searchable")
T('max by (dataset) (deriv(docuflow_documents{status="indexed"}[10m])) * 60 > 0', "{{dataset}}")
ts("Pages indexed per minute, by dataset", 8, 8, stack=True, desc="10 min derivative of indexed pages")
T("max by (dataset) (docuflow_read_seconds_avg)", "{{dataset}}")
ts("Vision read time per page, by dataset", 16, 8, unit="s", desc="Mean seconds per page over 15 min; dense scans take ~10x a handwriting line")
y += 8
T('sum by (queue) (rabbitmq_queue_messages_ready{queue=~"docuflow.*"})', "{{queue}} ready")
T('sum by (queue) (rabbitmq_queue_messages_unacked{queue=~"docuflow.*"})', "{{queue}} unacked")
ts("RabbitMQ work tokens (KEDA input)", 0, 8, desc="Lane queues drive worker scaling; .delay queues hold retry/outage backoffs")
T('sum by (result) (rate(docuflow_worker_jobs_total[2m])) * 60', "{{result}}")
ts("Job outcomes per minute", 8, 8, stack=True, desc="ok, retry (page problem), deferred (model server unavailable), dead",
   overrides=[{"matcher": {"id": "byName", "options": n}, "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]}
              for n, c in (("ok", "green"), ("retry", "orange"), ("deferred", "yellow"), ("dead", "red"))])
T("max by (lane) (docuflow_oldest_queued_age_seconds)", "{{lane}}")
T('max by (lane) (docuflow_jobs{status="running"})', "{{lane}} running")
ts("Oldest waiting job, and jobs running", 16, 8, unit="s", desc="Age of the oldest queued job per lane (left axis)",
   overrides=[{"matcher": {"id": "byRegexp", "options": ".*running"},
               "properties": [{"id": "unit", "value": "short"}, {"id": "custom.axisPlacement", "value": "right"}]}])
y += 8

# ---------------------------------------------------------------- Autoscaling
row("Autoscaling (KEDA -> HPA)")
T('max by (scaledObject, scaler) (keda_scaler_metrics_value{exported_namespace="docuflow"})', "{{scaledObject}} / {{scaler}}")
ts("KEDA trigger values (what each scaler reads)", 0, 12, desc="RabbitMQ tokens, running jobs, queries in flight, vLLM load: the numbers the HPAs divide by their targets")
T(HPA("kube_horizontalpodautoscaler_status_current_replicas"), "{{target}}")
ts("Replicas per autoscaled Deployment", 12, 12, stack=False, desc="Current replicas, as set by each KEDA-created HPA")
y += 8
T(f'{HPA("kube_horizontalpodautoscaler_status_desired_replicas")} / on (horizontalpodautoscaler) '
  f'kube_horizontalpodautoscaler_spec_max_replicas{{{NS}}} * 100', "{{target}}")
bars("Desired replicas as % of max (100% = capped)", 0, 8, thresholds=[{"color": "blue", "value": None}, {"color": "orange", "value": 90},
                                                                       {"color": "red", "value": 100}],
     desc="At 100% KEDA wants more than maxReplicaCount allows: raise the max or the downstream capacity")
T(f'sum by (app) (label_replace(kube_pod_status_phase{{{NS},phase="Pending"}}, "app", "$1", "pod", "(.+)-[a-z0-9]+-[a-z0-9]+")) > 0', "{{app}}")
ts("Pending pods", 8, 8, desc="Pods KEDA asked for that cannot schedule (GPU slots, memory)")
T('sum by (scaledObject) (increase(keda_scaler_detail_errors_total{exported_namespace="docuflow"}[10m])) or '
  'sum by (scaledObject) (increase(keda_scaled_object_errors_total{exported_namespace="docuflow"}[10m]))', "{{scaledObject}}")
ts("KEDA scaler errors (10 min)", 16, 8, desc="Unreachable queue/DB/Prometheus, bad auth, DNS (use FQDNs: KEDA runs in its own namespace)")
y += 8

# ---------------------------------------------------------------- vLLM
row("Model servers (vLLM)")
T(f'sum by (pod) (vllm:num_requests_running{{{NS}}})', "{{pod}} running")
T(f'sum by (pod) (vllm:num_requests_waiting{{{NS}}})', "{{pod}} waiting")
ts("Requests running / waiting per server", 0, 8, desc="running = in the batch (capped by --max-num-seqs); waiting = queued for a slot",
   overrides=[{"matcher": {"id": "byRegexp", "options": ".*waiting"}, "properties": [{"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [6, 4]}}]}])
T(f'max by (pod) (vllm:kv_cache_usage_perc{{{NS}}}) * 100', "{{pod}}")
ts("KV-cache usage", 8, 8, unit="percent", max_=100, desc="Near 100% the server preempts requests; raise --kv-cache-memory-bytes")
T(f'sum by (model_name) (rate(vllm:generation_tokens_total{{{NS}}}[2m]))', "{{model_name}} generated")
T(f'sum by (model_name) (rate(vllm:prompt_tokens_total{{{NS}}}[2m]))', "{{model_name}} prompt")
ts("Tokens per second", 16, 8)
y += 8
for i, (metric, name) in enumerate((("time_to_first_token_seconds", "Time to first token p95"), ("request_queue_time_seconds", "Queue time p95"),
                                    ("e2e_request_latency_seconds", "End-to-end request latency p95"))):
    T(f'histogram_quantile(0.95, sum by (le, model_name) (rate(vllm:{metric}_bucket{{{NS}}}[5m])))', "{{model_name}}")
    ts(name, i * 6, 6, unit="s")
T(f'sum by (pod) (rate(vllm:num_preemptions_total{{{NS}}}[5m])) * 60', "{{pod}}")
ts("Preemptions per minute", 18, 6, desc="Requests evicted for lack of KV cache; should be 0")
y += 8

# ---------------------------------------------------------------- Queries
row("Queries")
T("max by (quantile) (docuflow_query_latency_seconds)", "p{{quantile}}")
ts("Query latency (5 min window)", 0, 8, unit="s")
T("max(docuflow_queries_last_5m) / 5", "queries/min")
ts("Queries per minute", 8, 8)
T("max(docuflow_queries_in_flight)", "in flight")
T(f'kube_deployment_status_replicas_available{{{NS},deployment="api"}}', "api replicas")
ts("In flight vs API replicas", 16, 8, desc="API scales on in-flight queries (4 per pod) and CPU")
y += 8

# ---------------------------------------------------------------- GPU & cluster
row("GPU & cluster")
T("avg by (gpu) (DCGM_FI_DEV_GPU_UTIL)", "gpu {{gpu}} util")
ts("GPU utilisation", 0, 6, unit="percent", max_=100)
T("sum by (gpu) (DCGM_FI_DEV_FB_USED) * 1024 * 1024", "used")
T("(sum by (gpu) (DCGM_FI_DEV_FB_USED) + sum by (gpu) (DCGM_FI_DEV_FB_FREE)) * 1024 * 1024", "total")
ts("GPU memory", 6, 6, unit="bytes")
T("avg by (gpu) (DCGM_FI_DEV_POWER_USAGE)", "power W")
T("avg by (gpu) (DCGM_FI_DEV_GPU_TEMP)", "temp °C")
ts("GPU power and temperature", 12, 6)
T(APP(f'sum by (pod) (rate(container_cpu_usage_seconds_total{{{NS},container!=""}}[2m]))'), "{{app}}")
ts("CPU by workload (cores)", 18, 6, stack=True)
y += 8
T(APP(f'sum by (pod) (container_memory_working_set_bytes{{{NS},container!=""}})'), "{{app}}")
ts("Memory by workload", 0, 12, unit="bytes", stack=True)
T(f'count by (app) (label_replace(kube_pod_status_phase{{{NS},phase="Running"}} == 1, "app", "$1", "pod", "(.+)-[a-z0-9]+-[a-z0-9]+|(.+)-[0-9]+"))', "{{app}}")
ts("Running pods by workload", 12, 12, stack=True)
y += 8

for i, p in enumerate(panels):
    p["id"] = i + 1
dash = {"uid": "docuflow", "title": "docuflow", "tags": ["docuflow", "keda", "vllm"], "timezone": "browser", "schemaVersion": 39,
        "refresh": "10s", "time": {"from": "now-1h", "to": "now"}, "graphTooltip": 1,
        "templating": {"list": [{"name": "datasource", "type": "datasource", "query": "prometheus", "label": "Data source"}]},
        "panels": panels}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.json")
json.dump(dash, open(out, "w"), indent=1)
print(f"wrote {out}: {len(panels)} panels")
