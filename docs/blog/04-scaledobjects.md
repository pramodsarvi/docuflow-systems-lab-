# Part 4. Every ScaledObject, explained

The full manifests are in `k8s/base/keda.yaml` and `k8s/components/models/autoscaling.yaml`. This part shows the
final versions and the reasoning behind each number.

## Workers: backlog plus running

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: worker-vlm}
spec:
  scaleTargetRef: {name: worker-vlm}
  minReplicaCount: 0
  maxReplicaCount: 24
  pollingInterval: 5
  cooldownPeriod: 60
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp: {stabilizationWindowSeconds: 0, policies: [{type: Pods, value: 3, periodSeconds: 10}]}
        scaleDown: {stabilizationWindowSeconds: 30, policies: [{type: Pods, value: 1, periodSeconds: 20}]}
  triggers:
    - type: rabbitmq                       # backlog: tokens waiting = runnable, unclaimed jobs
      name: vlm-queue
      authenticationRef: {name: docuflow-rabbitmq}
      metadata: {protocol: amqp, queueName: docuflow.vlm, mode: QueueLength, value: "2", activationValue: "0"}
    - type: postgresql                     # in progress: never scale away a pod mid-job
      name: vlm-running
      authenticationRef: {name: docuflow-pg}
      metadata:
        query: "SELECT count(*) FROM jobs WHERE lane='vlm' AND status='running'"
        targetQueryValue: "1"
        activationTargetQueryValue: "0"
```

- **`value: "2"`**: two waiting pages per worker. Each worker handles one page at a time, so a target of 2 makes the
  backlog clear in two rounds while bursts still scale out fast.
- **`maxReplicaCount: 24`**: about the number of concurrent page reads that two vLLM replicas (16 batch slots each)
  can keep busy. Workers beyond that only lengthen vLLM's internal queue.
- **Fast up, slow down**: add up to 3 pods per 10 s, remove 1 per 20 s. Scale-up latency matters for a burst;
  scale-down flapping wastes pod starts.
- **`terminationGracePeriodSeconds: 180`** on the Deployment: on scale-down, a worker gets SIGTERM, finishes its
  current page and exits. Anything cut off is requeued by lease expiry.

`worker-io` (target 2, max 3) and `worker-embed` (target 8, max 3) follow the same pattern. Embedding is a ~0.1 s call,
so one worker clears many tokens per second and the target is higher.

## API: CPU and concurrency

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: api}
spec:
  scaleTargetRef: {name: api}
  minReplicaCount: 1
  maxReplicaCount: 8
  triggers:
    - type: cpu                            # the classic HPA metric
      metricType: Utilization
      metadata: {value: "60"}
    - type: postgresql                     # interactive queries currently in flight
      authenticationRef: {name: docuflow-pg}
      metadata:
        query: "SELECT count(*) FROM inflight WHERE beat > now() - interval '120 seconds'"
        targetQueryValue: "4"
```

Each agent query registers itself in an `inflight` table and heartbeats it between steps. The 120 s window means a
crashed query stops counting quickly. A `cpu` trigger needs CPU *requests* on the pod and can't scale to zero, so
`minReplicaCount` is 1.

This is the ScaledObject that taught the most (Part 7, failure 9). It scaled correctly, to 8 pods under 96
concurrent queries, and latency didn't improve at all, because the API wasn't the bottleneck.

## vLLM servers: scale on the model server's own queue

vLLM exposes `vllm:num_requests_running` (sequences in the current batch) and `vllm:num_requests_waiting` (requests
queued for a batch slot). Prometheus scrapes them through a PodMonitor, and KEDA reads them with the `prometheus`
scaler:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: model-vlm}
spec:
  scaleTargetRef: {name: model-vlm}
  minReplicaCount: 1
  maxReplicaCount: 2                       # 4 in prod (whole GPUs)
  pollingInterval: 15
  cooldownPeriod: 300                      # a model server takes ~1 min to start: don't flap
  advanced:
    horizontalPodAutoscalerConfig:
      behavior:
        scaleUp: {stabilizationWindowSeconds: 30, policies: [{type: Pods, value: 1, periodSeconds: 60}]}
        scaleDown: {stabilizationWindowSeconds: 300, policies: [{type: Pods, value: 1, periodSeconds: 120}]}
  triggers:
    - type: prometheus
      metadata:
        serverAddress: http://kube-prometheus-stack-prometheus.monitoring.svc:9090
        query: >-
          sum(vllm:num_requests_running{namespace="docuflow", pod=~"model-vlm-.*"})
          + sum(vllm:num_requests_waiting{namespace="docuflow", pod=~"model-vlm-.*"}) or vector(0)
        threshold: "4"
```

- **Running + waiting, not waiting alone.** Waiting is zero until the batch is full, and by then latency is already
  hurting. Running + waiting measures total demand on the server.
- **The threshold must be reachable.** The first version used 6 per replica with at most 6 workers sending requests:
  total demand could never exceed 6, so the second replica never started (Part 7, failure 2).
- **`or vector(0)`** makes the query return 0 instead of "no data" when no pod is up yet.
- The embedding server (`model-embed`) uses the same trigger with threshold 8. The agent model (`model-llm`) gets the
  same treatment in prod (threshold 96 against 128 batch slots on a whole GPU). Locally one GPU can't hold a second
  copy of it next to the others.

## Two chains of scaling

Put together, demand flows through two autoscaling layers:

```
ingest burst ─► RabbitMQ tokens ─► KEDA ─► worker-vlm pods ─► concurrent requests ─► vLLM running+waiting
                                                                                     ─► KEDA ─► model-vlm replicas
query burst  ─► inflight rows ─► KEDA ─► api pods ─► agent requests ─► model-llm queue (─► KEDA in prod)
```

The worker layer converts backlog into concurrency, and the model layer converts concurrency into GPU replicas.
Each layer's maximum has to be sized against the next layer's capacity. Mismatches between them caused failures 1, 2
and 9 in Part 7.
