# Part 2. HPA and KEDA, precisely

KEDA is often described as "an alternative to the HPA". It isn't one: KEDA **creates and drives an HPA**. Knowing
exactly what each component does explains every scaling behaviour in the later parts.

## The HPA on its own

The Horizontal Pod Autoscaler is a control loop in `kube-controller-manager`. Every 15 s it reads a metric for a
target (Deployment or StatefulSet) and computes:

```
desiredReplicas = ceil( currentReplicas × currentMetricValue / targetMetricValue )
```

For **external metrics with an `AverageValue` target**, the form KEDA uses, `currentMetricValue` is already the
total divided by current replicas, so this reduces to:

```
desiredReplicas = ceil( totalMetricValue / targetPerPod )
```

A few properties matter in practice:

- It never goes below `minReplicas`, and `minReplicas` must be at least 1. **The HPA cannot scale to zero.**
- With several metrics, it computes a desired count for each and takes the **maximum**.
- It ignores changes within a 10% tolerance.
- `behavior.scaleUp` / `behavior.scaleDown` set stabilisation windows and rate limits (for example "add at most
  3 pods per 10 s").
- Out of the box it can read only resource metrics (CPU, memory) through metrics-server. Anything else needs an
  adapter that serves the `external.metrics.k8s.io` API.

## What KEDA adds

KEDA runs two components:

- **The operator** watches `ScaledObject` resources. For each one it creates an HPA named `keda-hpa-<name>`, and it
  owns the **0 ↔ 1** transition, which the HPA cannot do.
- **The metrics server** implements `external.metrics.k8s.io`. When the HPA asks for a metric, KEDA's metrics server
  runs the scaler, for example a RabbitMQ queue-length check or a SQL query, and returns the number.

So responsibility is split by range:

| Transition | Who does it | Driven by |
|---|---|---|
| 0 → 1 (activation) | KEDA operator | any trigger above its `activationValue`, checked every `pollingInterval` |
| 1 → N and N → 1 | the HPA KEDA created | `ceil(metric / target)`, max over triggers, `behavior` policies |
| 1 → 0 (deactivation) | KEDA operator | all triggers at or below activation for `cooldownPeriod` seconds |

A minimal example from this project. The page-reading workers scale on a RabbitMQ queue:

```yaml
apiVersion: keda.sh/v1alpha1
kind: ScaledObject
metadata: {name: worker-vlm}
spec:
  scaleTargetRef: {name: worker-vlm}     # the Deployment
  minReplicaCount: 0                     # KEDA may scale it to zero
  maxReplicaCount: 24
  pollingInterval: 5                     # activation check every 5 s
  cooldownPeriod: 60                     # 60 s with nothing to do -> scale to zero
  triggers:
    - type: rabbitmq
      authenticationRef: {name: docuflow-rabbitmq}
      metadata: {protocol: amqp, queueName: docuflow.vlm, mode: QueueLength, value: "2"}
```

With 300 tokens in `docuflow.vlm` and `value: "2"`, the HPA wants `ceil(300 / 2) = 150` replicas, capped at
`maxReplicaCount: 24`. With 5 tokens it wants 3. With 0 tokens for 60 s, the operator scales the Deployment to zero.

## The knobs, and what each one actually controls

| Field | Controls | Common mistake |
|---|---|---|
| `value` / `targetQueryValue` / `threshold` | work per replica in steady state | treating it as a global threshold; it is **per pod** |
| `activationValue` | when to go 0 → 1 | leaving it unset on a scaler whose default activation is high |
| `pollingInterval` | how often KEDA checks activation | expecting it to also speed up 1 → N (the HPA's 15 s loop does that) |
| `cooldownPeriod` | delay before **scaling to zero** only | expecting it to slow down N → 1 (use `behavior.scaleDown`) |
| `advanced.horizontalPodAutoscalerConfig.behavior` | HPA rate limits and stabilisation | none: this is the right tool for N → 1 damping |
| `minReplicaCount` / `maxReplicaCount` | bounds | a max that the app can't actually use (Part 7, failure 1) |

## Multiple triggers

When a ScaledObject has several triggers, the HPA takes the **maximum** desired count across them. This project
relies on that twice:

- **Workers** pair a *backlog* trigger (RabbitMQ queue length) with a *running* trigger (Postgres count of running
  jobs, target 1). When the queue empties, the backlog trigger drops to zero but the running trigger still asks for
  one pod per in-flight job, so KEDA never scales away a worker in the middle of a 20 s page read.
- **The API** pairs `cpu` (the classic HPA metric) with "queries in flight": whichever is under more pressure wins.

## Authentication

Scaler credentials should live in a Secret, referenced by a `TriggerAuthentication`, never inlined in the
ScaledObject:

```yaml
apiVersion: keda.sh/v1alpha1
kind: TriggerAuthentication
metadata: {name: docuflow-rabbitmq}
spec:
  secretTargetRef:
    - {parameter: host, name: docuflow-secrets, key: RABBITMQ_URL}
```

One consequence is easy to miss: **KEDA connects from its own namespace.** `amqp://rabbitmq:5672` resolves inside
`docuflow` but not inside `keda`. Use fully qualified service names in any URL a scaler reads (Part 7, failure 6).

## Seeing what KEDA decided

```bash
kubectl -n docuflow get scaledobject                # READY / ACTIVE per ScaledObject
kubectl -n docuflow get hpa                         # the HPAs KEDA created, with current/target metric values
kubectl -n docuflow describe hpa keda-hpa-worker-vlm
kubectl -n keda logs deploy/keda-operator | grep ERROR
```

The HPA's `TARGETS` column shows the live per-pod value against the target. For example, `25667m/4 (avg)` means
25.7 queries in flight per API pod against a target of 4, which is why that HPA was scaling up.
