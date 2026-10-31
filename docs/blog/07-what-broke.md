# Part 7. What broke, and the fixes

Fourteen failures from building, stress-testing and running the system at full ingest load. Most of them sat *around* the autoscaler rather than in it,
and none showed up in unit tests or a quiet cluster.

## 1. KEDA started pods that sat idle

**Symptom.** 12 page-reading workers were running, but only 8 jobs were. The database showed `vlm running = 8` no
matter how many pods KEDA added.

**Cause.** The app enforces its own concurrency rules at claim time, including a per-dataset cap for fair share
(`PER_DATASET_CAP=8`). With a single dataset being ingested, the cap allowed only 8 running jobs. KEDA's maximum had
been raised to 12 without raising the cap, so 4 pods polled forever.

**Fix.** On Kubernetes the pod count is the real limit, so every in-app limit must sit above KEDA's
`maxReplicaCount`. The config now says so next to the values. **Ingest went from 39.7 to 56.5 pages/min.**

**General lesson.** Any limit inside the app (connection pools, semaphores, per-tenant caps, broker prefetch) is a
second autoscaling ceiling. KEDA can't see it.

## 2. A scaling threshold that could never be reached

**Symptom.** `model-vlm` stayed at 1 replica through a whole run with a backlog of 700 pages.

**Cause.** Its trigger was "running + waiting requests, 6 per replica", and at most 6 workers sent requests. Total
demand could never exceed 6, so the desired count was always `ceil(6/6) = 1`. The prod overlay had the same flaw at
larger numbers: a threshold of 48 with 24 workers.

**Fix.** Size thresholds across layers: the upstream layer's maximum concurrency must be able to exceed the
downstream threshold. Workers went to 24, the threshold to 4. The second replica started within a minute.

## 3. A new replica that got no traffic

**Symptom.** After KEDA added a second vLLM page-reading server, replica A showed **16 running + 6 waiting**;
replica B showed **2 running**.

**Cause.** A Kubernetes Service balances per **connection** (kube-proxy picks a backend when a TCP connection
opens). The workers' HTTP client kept connections alive, so every worker stayed on whichever replica it had first
reached, and that was the only replica that existed when they started.

**Fix.** No keep-alive for model calls (`max_keepalive_connections=0`). Each call is seconds of GPU work, so a new TCP
connection per request costs nothing measurable. Load went to **16 / 12**. Alternatives for larger systems: an L7
proxy, a service mesh, or an inference gateway that routes per request (and can route on KV-cache state).

## 4. `kubectl apply` undid the autoscaler

**Symptom.** After a config change, `worker-vlm` dropped from 24 to 1 and `model-vlm` from 2 to 1. KEDA scaled them
back up, and the second model server had to reload its weights.

**Cause.** The manifests set `replicas:` on Deployments KEDA manages. Every `kubectl apply` reset the live count to
that value.

**Fix.** Remove `replicas` from every KEDA-managed Deployment. There's a trap in the fix itself: `kubectl apply`
remembers the previously applied `replicas` in the `last-applied-configuration` annotation, and when the field
disappears it *deletes* it, which defaults the count to 1. Strip it from the annotation once first:

```bash
kubectl -n docuflow get deploy worker-vlm -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
  | python3 -c "import sys,json; c=json.load(sys.stdin); c['spec'].pop('replicas',None); print(json.dumps(c))" > la.json
kubectl -n docuflow annotate deploy worker-vlm --overwrite kubectl.kubernetes.io/last-applied-configuration="$(cat la.json)"
```

After that, an apply left every count unchanged (verified before and after).

## 5. Rolling updates deadlocked on GPU memory

**Symptom.** After changing vLLM flags, the new pods crash-looped with "Free memory on device (1.2/44.4 GiB) …
is less than desired", and the old pods never went away.

**Cause.** `RollingUpdate` starts the new pod first, and a shared GPU has no room for it.

**Fix.** `strategy: Recreate` for model servers on a shared GPU (Part 5). Prod, with whole GPUs, keeps rolling updates.

## 6. KEDA couldn't resolve the queue's hostname

**Symptom.** Every worker ScaledObject showed `READY=False`; the KEDA operator logged
`dial tcp: lookup rabbitmq on 10.96.0.10:53: no such host`.

**Cause.** The connection strings used short service names (`rabbitmq`, `db`). They resolve inside the `docuflow`
namespace, but KEDA's scalers run in the `keda` namespace.

**Fix.** Fully qualified names in every URL a scaler reads: `rabbitmq.docuflow.svc.cluster.local`.

## 7. Creation-order race, then a silent backoff

**Symptom.** Right after the first deploy, the ScaledObjects failed with `TriggerAuthentication ... not found`. Even
after the cause was fixed, KEDA logged nothing for minutes.

**Cause.** The ScaledObjects reconciled before their TriggerAuthentication existed, and the operator's per-object
error backoff then grew to minutes.

**Fix.** Apply TriggerAuthentications before ScaledObjects, or annotate the ScaledObjects to force a reconcile. In a
hurry, `kubectl -n keda rollout restart deploy/keda-operator`. All five ScaledObjects went `READY=True` within 30 s.

## 8. Concurrent vLLM startups miscounted memory

**Symptom.** `Available KV cache memory: -12.92 GiB`, and the agent model failed whenever it started at the same time
as the others.

**Cause.** vLLM sizes its KV cache by profiling memory at startup, and other processes loading on the same GPU at the
same moment distort the measurement.

**Fix.** A fixed `--kv-cache-memory-bytes` per server (Part 5).

## 9. KEDA scaled the API, and latency got worse

**Symptom.** Under 96 concurrent queries, KEDA scaled the API from 4 to 8 pods. p50 latency kept rising: 17 s, then
25 s.

**Cause.** The API wasn't the bottleneck. Each API pod used about 5 millicores. The agent model had 31 of 32 batch
slots busy and 64 requests waiting. More API pods only meant more concurrent waiters.

**Fix.** Scale the component whose own queue is growing. The changes:
- the agent model gets more batch slots and a larger KV cache, plus a KEDA trigger on its vLLM queue (in prod)
- the agent makes one fewer LLM call per query
- ingest backs off when query pressure is high

The API's queries-in-flight trigger is still useful for spreading connections, but its maximum should stay modest.

## 10. The deployment overwrote the experiment

**Symptom.** Applying an unrelated fix in the middle of a stress run reset replica counts (failure 4), and a running
model server restarted. The run's numbers before and after that moment can't be compared.

**Fix.** Stop applying manifests during a measured run, or use a dedicated "live patch" path (`scripts/mode.sh` uses
`kubectl patch` on ScaledObjects and never touches Deployments). Stress-test reports should record every manual
intervention with its timestamp.

## 11. Dense pages were thrown away whole

**Symptom.** Five DocVQA pages (financial statements, ingredient tables) went `dead` after three attempts with
`unparseable VLM output: Expecting ',' delimiter`.

**Cause.** A dense page has hundreds of text regions. The vision model's output reached the 3,000-token limit in the
middle of the JSON, and the parser rejected the entire page, including the hundreds of regions it had already read.
Retrying can't help: the page is just as dense every time.

**Fix.**
- Keep every region object that was completed before the cut-off (`extract.salvage_regions`).
- Raise the output limit to 6,000 tokens; with a ~2,100-token page image that still fits the reader's 12,288-token
  context.

All five pages were then indexed. A regression test covers a cut-off region list with escaped quotes.

## 12. A one-minute outage downgraded 264 pages

**Symptom.** 264 synthetic pages were indexed from the dataset's own transcription instead of a vision read, all
within 52 seconds, exactly while the page-reading servers were crash-looping during a configuration change.

**Cause.** A page gets 3 attempts with 5 s and 10 s backoff, and on the last attempt it falls back to the
dataset's transcription when one exists. That rule was meant for pages the model can't read. During an outage, every
page in flight used up its attempts in about 15 seconds and fell back, even though nothing was wrong with the pages.

**Fix.** Separate "the model server is unavailable" from "this page failed":
- The model client raises `ModelUnavailable` for connection errors, timeouts, 429 and 5xx once its own quick
  retries are exhausted.
- Workers **defer** such jobs: the attempt is refunded, there's never a fallback, and the backoff grows from 15 s to
  5 minutes per consecutive outage.
- The API maps the same error to `503 Retry-After`.
- `POST /workloads/reread-fallbacks` queues a real vision read for every downgraded page.

The next restart of the page readers produced **590 deferrals, 0 dead jobs and 0 new fallbacks.**

## 13. Two good changes combined into a bad one

**Symptom.** After fixes 11 and 12, ingest slowed and per-page read time grew: XFUND from 53 s to 78 s, DocVQA from
37 s to 49 s. The page readers' KV cache was 68–88% full, their 16 batch slots were full, and requests were waiting.

**Cause.** The page readers' KV cache had been cut to 1.5 GiB to make room for other servers (Part 5), and the
output limit had just been doubled. Longer outputs filled the small cache.

**Fix, and what it showed.** In ingest mode the agent model is scaled to zero, which frees ~17 GB. `mode.sh ingest`
now gives that memory to page reading: 3 GiB KV cache and 24 batch slots per reader, and 72 workers to fill them.
KV use dropped to 27–75%, so memory pressure was gone. **But throughput didn't change** (~74 pages/min before and
after), because the GPU was compute-bound at 99–100%. Memory was a symptom, not the bottleneck. Check compute
utilisation before giving a server more memory or more concurrency.

## 14. Per-connection balancing is uneven for long requests

**Symptom.** With keep-alive off (failure 3), load across the three page readers was still uneven. One had
**24 running + 11 waiting** while another had **12 running and free slots**.

**Cause.** kube-proxy picks a backend at random for each new connection. With requests lasting 25–80 s, random
placement doesn't even out over a short window, and it ignores how busy each backend already is.

**Fix (recommended, not yet implemented).** Least-request routing: kube-proxy in IPVS mode with the `lc` scheduler,
an Envoy-based gateway, or an inference gateway that routes on each server's queue and KV-cache state. Random
placement wastes a fraction of the batch capacity that autoscaling paid for.

## The pattern

| Layer | What it limited | Failures |
|---|---|---|
| app limits | concurrency KEDA couldn't see | 1 |
| model output | limits that discard work instead of degrading | 11 |
| failure semantics | treating infrastructure outages as bad input | 12 |
| thresholds | demand that could never reach the target | 2 |
| networking | load balancing per connection | 3, 14 |
| deployment tooling | replica counts, rollout strategy | 4, 5, 10 |
| KEDA plumbing | DNS, ordering, backoff | 6, 7 |
| model server | GPU memory budget, and memory vs compute | 8, 13 |
| capacity planning | scaling the wrong tier | 9 |

KEDA computed what it was told to compute every single time. The work is in making sure what it reads reflects real
demand, and that the capacity it adds can actually be used.
