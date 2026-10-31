# Part 7. What broke, and the fixes

Ten failures from building and stress-testing the system. Most of them sat *around* the autoscaler rather than in it,
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

## The pattern

| Layer | What it limited | Failures |
|---|---|---|
| app limits | concurrency KEDA couldn't see | 1 |
| thresholds | demand that could never reach the target | 2 |
| networking | load balancing per connection | 3 |
| deployment tooling | replica counts, rollout strategy | 4, 5, 10 |
| KEDA plumbing | DNS, ordering, backoff | 6, 7 |
| model server | GPU memory budget | 8 |
| capacity planning | scaling the wrong tier | 9 |

KEDA computed what it was told to compute every single time. The work is in making sure what it reads reflects real
demand, and that the capacity it adds can actually be used.
