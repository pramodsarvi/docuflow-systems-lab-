# Kubernetes for Docker users, using docuflow as the example

This tutorial assumes you know Docker (`docker run`, images, ports, volumes, env vars, `docker compose`) and nothing
about Kubernetes (k8s). Every concept is tied to a file in this repo, so you can open the file and see the real thing.

Paths are relative to the repo root. Links open the file at the relevant line.

**Contents**
1. [The mental shift](#1-the-mental-shift)
2. [Core objects](#2-core-objects-one-at-a-time)
3. [Making it robust](#3-making-it-robust)
4. [Autoscaling and custom resources](#4-autoscaling-and-custom-resources)
5. [Kustomize: how this repo avoids copy-paste](#5-kustomize-how-this-repo-avoids-copy-paste)
6. [Commands you will actually use](#6-commands-you-will-actually-use)
7. [Hands-on: run docuflow on a laptop cluster](#7-hands-on-run-docuflow-on-a-laptop-cluster)
8. [Following one request through every object](#8-following-one-request-through-every-object)
9. [Suggested reading order](#9-suggested-reading-order)
10. [Cheat sheet](#10-cheat-sheet)

---

## 1. The mental shift

**Docker** runs containers. You say "run this image now," and it runs once.

**Kubernetes** keeps a **desired state** true. You write down "I want 3 copies of this container running, always," and
Kubernetes starts them, watches them, and replaces any that die. It does this in a loop that never stops.

| Docker | Kubernetes |
|---|---|
| `docker run` (imperative: do this now) | `kubectl apply -f file.yaml` (declarative: make reality match this file) |
| One machine | A cluster of machines (**nodes**) treated as one pool |
| A container that dies stays dead (unless `--restart`) | A container that dies is recreated automatically |
| `docker compose up` | `kubectl apply -k k8s/overlays/ci-sim` |
| `docker-compose.yml` | A set of YAML files, one object per document |

This repo contains both versions of docuflow, which makes it a good comparison:
- [docker-compose.yml](../docker-compose.yml) is the Docker version.
- [k8s/](../k8s/) is the Kubernetes version of the same system.

**How it works internally** (you do not need to memorise this):
- You send YAML to the **API server**, which stores your desired state.
- **Controllers** compare desired state with actual state and fix any difference, forever.
- The **scheduler** picks which node each container runs on.
- The **kubelet**, an agent on every node, starts and stops the containers.

---

## 2. Core objects, one at a time

Every Kubernetes object is YAML with the same skeleton:

```yaml
apiVersion: apps/v1       # which API family
kind: Deployment          # what type of object
metadata: {name: api}     # its name
spec: ...                 # what you want
```

### Pod: the smallest unit
A Pod is one or more containers that share a network and run together. Usually it is one container. Think of it as
`docker run` wrapped in a labelled envelope.

You almost never create Pods directly, because a bare Pod that dies is not recreated. You create a Deployment, which
creates Pods for you.

### Deployment: "keep N copies running"
This is the object you will see most. Open [k8s/base/api.yaml:9](../k8s/base/api.yaml#L9):

```yaml
kind: Deployment
metadata: {name: api}
spec:
  selector: {matchLabels: {app: api}}      # "I own the Pods labelled app=api"
  template:                                # the Pod blueprint
    spec:
      containers:
        - name: api
          image: docuflow                  # like `docker run docuflow`
          command: ["uvicorn", "docuflow.api:app", ...]   # like overriding CMD
          ports: [{name: http, containerPort: 8791}]
```

The `template` is the blueprint, and the Deployment stamps Pods out of it. When you change the image tag and apply, it
does a **rolling update**: start a new Pod, wait until it is healthy, stop an old one, repeat. The
`strategy: {maxUnavailable: 0, maxSurge: 1}` line ([api.yaml:14](../k8s/base/api.yaml#L14)) means "never drop below the
current capacity during an update."

Docuflow's Deployments all run the **same Docker image** with a different `command`, like several services in compose
sharing one image ([Dockerfile](../Dockerfile), where the `CMD` is overridden per Deployment):

| Deployment | File | Runs |
|---|---|---|
| `api` | [k8s/base/api.yaml:9](../k8s/base/api.yaml#L9) | `uvicorn docuflow.api:app` |
| `worker-io` | [k8s/base/workers.yaml:7](../k8s/base/workers.yaml#L7) | `python -m docuflow worker io` |
| `worker-vlm` | [k8s/base/workers.yaml:39](../k8s/base/workers.yaml#L39) | `python -m docuflow worker vlm` |
| `worker-embed` | [k8s/base/workers.yaml:71](../k8s/base/workers.yaml#L71) | `python -m docuflow worker embed` |
| `worker-ragas` | [k8s/base/workers.yaml:103](../k8s/base/workers.yaml#L103) | `python -m docuflow worker ragas` |
| `model-vlm` / `model-llm` / `model-embed` | [k8s/components/models/](../k8s/components/models/) | vLLM model servers |

### Labels and selectors: how objects find each other
Objects are not wired together by name. They are wired by **labels**, which are `key=value` tags.
- A Deployment says "I manage Pods labelled `app=api`."
- A Service says "send traffic to Pods labelled `app=api`."

If the labels do not match, nothing connects. A mismatched label is the most common beginner mistake. In
[api.yaml](../k8s/base/api.yaml), the Service selector (line 5), the Deployment selector (line 15) and the Pod template
labels (line 18) all say `app: api`.

### Service: a stable address
Pods get a new IP every time they are recreated. A **Service** gives a group of Pods one fixed name and load-balances
across them. See [k8s/base/api.yaml:2](../k8s/base/api.yaml#L2):

```yaml
kind: Service
metadata: {name: api}
spec:
  selector: {app: api}
  ports: [{name: http, port: 8791, targetPort: http}]
```

Now any Pod in the cluster can call `http://api:8791`. This is the same idea as compose's service-name DNS, but with
load balancing across many Pods. In docuflow:
- Workers reach Postgres at `db:5432` ([k8s/base/db.yaml:4](../k8s/base/db.yaml#L4)).
- Workers reach RabbitMQ at `rabbitmq:5672` ([k8s/base/rabbitmq.yaml:5](../k8s/base/rabbitmq.yaml#L5)).
- The API and workers reach the models at `http://model-llm:8000/v1`, set in
  [k8s/components/models/kustomization.yaml:17](../k8s/components/models/kustomization.yaml#L17).

### Ingress: the front door from outside
Services are internal. An **Ingress** routes outside HTTP traffic (such as `docuflow.localtest.me`) to a Service. It
needs an *ingress controller* installed in the cluster; here that is ingress-nginx, installed by
[scripts/cluster-up.sh](../scripts/cluster-up.sh). It is roughly the `-p 80:80` that lets outsiders in, plus hostname
routing. See [k8s/components/ingress/ingress.yaml](../k8s/components/ingress/ingress.yaml).

### ConfigMap and Secret: configuration out of the image
In Docker you use `-e FOO=bar` or `--env-file`. In Kubernetes:
- A **ConfigMap** holds non-secret settings: [k8s/base/config.yaml](../k8s/base/config.yaml).
- A **Secret** holds passwords and keys. It is only base64-encoded, not encrypted by default, so treat it as "hidden from
  casual view," not as strong security.

Pods consume them like this ([api.yaml:33](../k8s/base/api.yaml#L33)):

```yaml
envFrom:
  - configMapRef: {name: docuflow-config}   # every key becomes an env var
  - secretRef: {name: docuflow-secrets}
```

That is `--env-file`, but pointing at cluster objects. [docuflow/config.py](../docuflow/config.py) then reads them with
`os.environ.get(...)`.

Two facts that surprise people:
- **Environment variables are read when the container starts.** Editing a ConfigMap does nothing to running Pods until
  they restart (`kubectl -n docuflow rollout restart deploy/api`).
- **Environments patch the same ConfigMap instead of copying it.** For example
  [k8s/components/models/kustomization.yaml:13](../k8s/components/models/kustomization.yaml#L13) switches the backend to
  `openai` and sets the model URLs, and [k8s/overlays/prod/kustomization.yaml:44](../k8s/overlays/prod/kustomization.yaml#L44)
  raises the vlm limits. See [Kustomize](#5-kustomize-how-this-repo-avoids-copy-paste).

How secrets get created differs by environment:
- Dev overlays generate them with a `secretGenerator`
  ([k8s/overlays/local-gpu/kustomization.yaml:12](../k8s/overlays/local-gpu/kustomization.yaml#L12)).
- Production creates them out of band with `make prod-secrets` ([Makefile](../Makefile)), so they never enter git.

### Namespace: a folder
Everything docuflow lives in the namespace `docuflow` ([k8s/base/namespace.yaml](../k8s/base/namespace.yaml)). That is
why every command has `-n docuflow`. A namespace is for organisation and permissions; it does not isolate the network by
itself.

### ServiceAccount: a Pod's identity
Pods run as a ServiceAccount, which is their identity when talking to the Kubernetes API. Docuflow has one
([k8s/base/serviceaccount.yaml](../k8s/base/serviceaccount.yaml)). It is not bound to any permissions, since the app
never calls the Kubernetes API.

### Volumes: persistent data
A container's filesystem disappears with the container, as in Docker. Kubernetes has several answers:

- **`emptyDir`:** scratch space that lives as long as the Pod. Workers mount one at `/tmp`
  ([workers.yaml:35](../k8s/base/workers.yaml#L35)) because the root filesystem is read-only. Postgres uses a
  memory-backed one for `/dev/shm` ([db.yaml:42](../k8s/base/db.yaml#L42)).
- **PersistentVolumeClaim (PVC):** "I need 5Gi of disk that survives." Kubernetes finds or creates a
  **PersistentVolume (PV)** that satisfies it. It is the equivalent of a named Docker volume. The model cache is a
  PV plus PVC pair: [k8s/components/models/model-cache.yaml](../k8s/components/models/model-cache.yaml).

### StatefulSet: Deployments for things that remember
A Deployment's Pods are interchangeable and disposable. A database is not. A **StatefulSet** gives each Pod a stable
name (`db-0`) and its own disk that follows it around.

- Postgres: [k8s/base/db.yaml:11](../k8s/base/db.yaml#L11)
- RabbitMQ: [k8s/base/rabbitmq.yaml:15](../k8s/base/rabbitmq.yaml#L15)

The `volumeClaimTemplates` section ([db.yaml:43](../k8s/base/db.yaml#L43)) stamps out one PVC per Pod.

### CronJob: run to completion on a schedule
A **Job** runs a container until it exits successfully. A **CronJob** does that on a schedule.
[k8s/base/gc-cronjob.yaml](../k8s/base/gc-cronjob.yaml) runs `python -m docuflow gc` at minute 17 of every hour to
delete old jobs and query traces. It is a crontab entry that Kubernetes manages for you. A second CronJob in
[k8s/overlays/prod/backup-cronjob.yaml:12](../k8s/overlays/prod/backup-cronjob.yaml#L12) runs `pg_dump` for backups.

---

## 3. Making it robust

### Probes: health checks
Docker has `HEALTHCHECK`. Kubernetes has three kinds:

| Probe | Meaning | If it fails |
|---|---|---|
| `startupProbe` | "Give it time to boot." | Keep waiting, then restart |
| `readinessProbe` | "Is it ready for traffic?" | Removed from the Service, **not** killed |
| `livenessProbe` | "Is it stuck?" | Container is restarted |

Examples:
- API: [api.yaml:37-39](../k8s/base/api.yaml#L37) uses `/ready` for startup and readiness, and `/health` for liveness.
  `/ready` checks the database ([docuflow/api.py](../docuflow/api.py)).
- Workers: [workers.yaml:31](../k8s/base/workers.yaml#L31) probes the worker's own `/health` on port 9100, served by
  [docuflow/worker.py](../docuflow/worker.py). It goes unhealthy if a job runs longer than `LEASE_SECONDS`, so a stuck
  worker gets restarted.

### Resources: requests and limits
```yaml
resources: {requests: {cpu: 200m, memory: 256Mi}, limits: {memory: 1Gi}}
```
([api.yaml:41](../k8s/base/api.yaml#L41))
- **requests** are what the scheduler reserves when picking a node (`200m` is 0.2 of a CPU core).
- **limits** are the hard ceiling. A container above its memory limit is killed and shows `OOMKilled`.

### Graceful shutdown
When Kubernetes stops a Pod it sends SIGTERM, waits `terminationGracePeriodSeconds`, then sends SIGKILL.
- Workers get 180 seconds to finish the current job ([workers.yaml:20](../k8s/base/workers.yaml#L20)). The SIGTERM
  handler is in [docuflow/worker.py](../docuflow/worker.py) (`run`).
- The API has a `preStop: sleep 5` ([api.yaml:40](../k8s/base/api.yaml#L40)), which gives the network time to stop sending
  it traffic first.

### PodDisruptionBudget (PDB)
It says "during voluntary operations such as node upgrades, never take down more than N of these at once."
- API: [api.yaml:47](../k8s/base/api.yaml#L47)
- Database: [db.yaml:48](../k8s/base/db.yaml#L48)

### PriorityClass
When the cluster is full, higher-priority Pods win and lower-priority ones may be evicted.
[k8s/base/priorityclasses.yaml](../k8s/base/priorityclasses.yaml) defines `docuflow-interactive` and `docuflow-batch`.
The API and models use the first; the workers use the second. So a user's question beats background ingestion.

### topologySpreadConstraints
Spreads API replicas across nodes so one node failure does not take out all of them
([api.yaml:25](../k8s/base/api.yaml#L25)).

### NetworkPolicy: a firewall between Pods
By default every Pod can talk to every other Pod. These policies restrict that:
- Only docuflow Pods and KEDA may reach Postgres: [networkpolicy.yaml:4](../k8s/base/networkpolicy.yaml#L4)
- Workers accept only Prometheus scraping: [networkpolicy.yaml:17](../k8s/base/networkpolicy.yaml#L17)
- The API accepts traffic from its own namespace, the ingress controller and Prometheus:
  [networkpolicy.yaml:28](../k8s/base/networkpolicy.yaml#L28)
- RabbitMQ: [rabbitmq.yaml:51](../k8s/base/rabbitmq.yaml#L51)
- Model servers: [k8s/components/models/networkpolicy.yaml](../k8s/components/models/networkpolicy.yaml)

A NetworkPolicy only has an effect if the cluster's network plugin enforces it.

### Security context
`runAsNonRoot`, `readOnlyRootFilesystem`, `capabilities: drop: [ALL]`, `seccompProfile: RuntimeDefault`. These are the
Kubernetes spelling of `docker run --user 10001 --read-only --cap-drop ALL`. See
[api.yaml:24](../k8s/base/api.yaml#L24) and [api.yaml:42](../k8s/base/api.yaml#L42). The image creates user 10001 in the
[Dockerfile](../Dockerfile).

### Scheduling onto the right machines
- **`nodeSelector`:** "only run on nodes labelled `docuflow.io/pool=gpu`."
  [k8s/components/models/vlm.yaml:26](../k8s/components/models/vlm.yaml#L26)
- **`tolerations`:** permission to run on tainted nodes. GPU nodes are tainted so ordinary Pods stay off them, and the
  model servers tolerate it ([vlm.yaml:27](../k8s/components/models/vlm.yaml#L27)).
- **GPU request:** `nvidia.com/gpu: 1` ([vlm.yaml:64](../k8s/components/models/vlm.yaml#L64)). The NVIDIA device plugin
  (Helm values in [deploy/helm-values/nvidia-device-plugin.yaml](../deploy/helm-values/nvidia-device-plugin.yaml)) makes
  the GPU a schedulable resource.
- The `local-gpu` overlay keeps app Pods off the GPU node:
  [overlays/local-gpu/kustomization.yaml:24](../k8s/overlays/local-gpu/kustomization.yaml#L24).

---

## 4. Autoscaling and custom resources

### What a CRD is
Kubernetes only knows built-in kinds such as Deployment. A **CustomResourceDefinition (CRD)** teaches it a new kind. A
tool installs the CRD plus a controller that acts on objects of that kind.

**This repo defines no CRDs of its own.** It uses ones installed by other tools:

| Kind | API group | Installed by | File |
|---|---|---|---|
| `ScaledObject`, `TriggerAuthentication` | `keda.sh` | KEDA | [k8s/base/keda.yaml](../k8s/base/keda.yaml), [k8s/components/models/autoscaling.yaml](../k8s/components/models/autoscaling.yaml) |
| `ServiceMonitor`, `PodMonitor` | `monitoring.coreos.com` | kube-prometheus-stack | [k8s/components/monitoring/monitors.yaml](../k8s/components/monitoring/monitors.yaml) |
| `PrometheusRule` | `monitoring.coreos.com` | kube-prometheus-stack | [k8s/components/monitoring/alerts.yaml](../k8s/components/monitoring/alerts.yaml) |

Both tools are installed by [scripts/cluster-up.sh](../scripts/cluster-up.sh) (versions pinned at the top of that file;
Helm values in [deploy/helm-values/](../deploy/helm-values/)).

### KEDA ScaledObject: the key idea
Read [k8s/base/keda.yaml:25](../k8s/base/keda.yaml#L25) (`worker-vlm`):

```yaml
kind: ScaledObject
spec:
  scaleTargetRef: {name: worker-vlm}   # which Deployment to scale
  minReplicaCount: 0                   # scale to zero when idle
  maxReplicaCount: 24
  triggers:
    - type: rabbitmq                   # watch the work queue
      metadata: {queueName: docuflow.vlm, mode: QueueLength, value: "2"}
    - type: postgresql                 # also: never scale away a busy worker
      metadata: {query: "SELECT count(*) FROM jobs WHERE lane='vlm' AND status='running'", ...}
```

In words: look at the RabbitMQ queue `docuflow.vlm`; add about one worker Pod per two waiting jobs, up to 24; when the
queue is empty, scale to zero.

Under the hood KEDA creates a built-in **HorizontalPodAutoscaler (HPA)** for each ScaledObject. This is why the worker
Deployments have **no `replicas:` field** ([workers.yaml:10](../k8s/base/workers.yaml#L10)): KEDA owns the count, and a
value in the file would reset it on every `kubectl apply`.

Other scalers:
- `worker-embed`: [keda.yaml:57](../k8s/base/keda.yaml#L57)
- `worker-io`: [keda.yaml:84](../k8s/base/keda.yaml#L84)
- `api`, on CPU plus queries in flight: [keda.yaml:111](../k8s/base/keda.yaml#L111)
- `model-vlm` / `model-embed`, on vLLM load from Prometheus:
  [models/autoscaling.yaml:8](../k8s/components/models/autoscaling.yaml#L8)

`TriggerAuthentication` ([keda.yaml:11](../k8s/base/keda.yaml#L11)) lets KEDA read credentials from the Secret without
inlining them.

The exception is `worker-ragas`, which has a fixed `replicas: 2` and no ScaledObject
([workers.yaml:103](../k8s/base/workers.yaml#L103)).

### Why RabbitMQ and Postgres both exist
This is an app design point that explains the scaling. **Postgres is the source of truth** for every job. A RabbitMQ
message is only a *token* meaning "a runnable job exists in this lane." Workers take a token, then claim the real job row
in Postgres. KEDA scales on the token count. See [docuflow/broker.py](../docuflow/broker.py) and
[docuflow/jobs.py](../docuflow/jobs.py) (`claim`).

### Monitoring
- Workers and the API expose `/metrics`. The `prometheus.io/*` annotations on the Pods and the monitors in
  [monitors.yaml](../k8s/components/monitoring/monitors.yaml) tell Prometheus to scrape them.
- The Grafana dashboard is [k8s/components/monitoring/dashboard.json](../k8s/components/monitoring/dashboard.json),
  generated by [build_dashboard.py](../k8s/components/monitoring/build_dashboard.py) and loaded through a
  `configMapGenerator` ([monitoring/kustomization.yaml:6](../k8s/components/monitoring/kustomization.yaml#L6)).

### Outside the cluster
[k8s/extras/mac-llm.yaml](../k8s/extras/mac-llm.yaml) uses a Service plus an `EndpointSlice` to make an LLM running on a
laptop outside the cluster look like an in-cluster Service named `mac-llm`.

---

## 5. Kustomize: how this repo avoids copy-paste

Without it you would need nearly identical YAML for a laptop, CI and production. **Kustomize** layers changes on a shared
base with no templating. It is built into `kubectl`.

```
k8s/
  base/             the common core: api, workers, db, rabbitmq, keda, gc, policies
  components/       optional add-ons you can mix in
    models/           real vLLM model servers
    simulator/        a fake model server for CPU-only clusters
    monitoring/       Prometheus monitors, alerts, Grafana dashboard
    ingress/          the Ingress
  overlays/         a complete environment = base + chosen components + patches
    ci-sim/           base + simulator + ingress           (CPU only, fake models)
    local-gpu/        base + models + monitoring + ingress (real GPU, laptop)
    prod/             base + models + monitoring + ingress + production patches
```

How it fits together:
1. [k8s/base/kustomization.yaml](../k8s/base/kustomization.yaml) lists the base files, sets the namespace and adds the
   `part-of: docuflow` label to everything.
2. An overlay such as [k8s/overlays/ci-sim/kustomization.yaml](../k8s/overlays/ci-sim/kustomization.yaml) says
   `resources: [../../base]`, adds `components:`, generates the Secret and applies `patches:`.
3. Patches are small edits to named objects. For example, prod
   ([overlays/prod/kustomization.yaml:25](../k8s/overlays/prod/kustomization.yaml#L25)) sets the `api` ScaledObject to
   2..6 replicas and the `db` volume to 200Gi.

Preview any environment without deploying:

```bash
kubectl kustomize k8s/overlays/prod        # prints the final combined YAML
kubectl apply -k k8s/overlays/ci-sim       # builds and applies it
```

CI validates all three overlays with `make render` ([Makefile](../Makefile), `render` target), which runs `kustomize build`
plus `kubeconform`, including the KEDA and Prometheus CRD schemas.

Think of it as "compose override files," but with named, targeted patches.

---

## 6. Commands you will actually use

`kubectl` is the CLI, the equivalent of `docker`. Most commands need `-n docuflow`.

| Docker | kubectl |
|---|---|
| `docker ps` | `kubectl -n docuflow get pods` |
| `docker logs -f X` | `kubectl -n docuflow logs -f deploy/api` |
| `docker exec -it X sh` | `kubectl -n docuflow exec -it deploy/api -- sh` |
| `docker inspect X` | `kubectl -n docuflow describe pod X` |
| `docker run ...` | `kubectl apply -f file.yaml` |
| `docker restart X` | `kubectl -n docuflow rollout restart deploy/api` |
| `docker compose down` | `kubectl delete -k k8s/overlays/ci-sim` |
| `-p 8791:8791` | `kubectl -n docuflow port-forward svc/api 8791:8791` |
| `docker stats` | `kubectl -n docuflow top pods` |

Generic patterns work for every kind: `kubectl get <kind>`, `kubectl describe <kind> <name>`,
`kubectl delete <kind> <name>`, and `kubectl get <kind> -o yaml` to see the full object.

Project shortcuts from the [Makefile](../Makefile):

| Command | What it does |
|---|---|
| `make k8s-status` | Pods, ScaledObjects, HPAs, GPUs and RabbitMQ queue depths in one view |
| `make deploy OVERLAY=ci-sim` | Build the image, load it into kind, apply an overlay, wait for readiness |
| `make smoke` | End-to-end check against the deployed cluster ([scripts/smoke.sh](../scripts/smoke.sh)) |
| `make undeploy` | Remove docuflow, keep the cluster |
| `make cluster-down` | Delete the whole kind cluster |

### Debugging recipe
1. `kubectl -n docuflow get pods` and look at the **STATUS** column.
2. `kubectl -n docuflow describe pod <name>` and read the **Events** at the bottom (scheduling, image and probe errors).
3. `kubectl -n docuflow logs <pod>`, adding `--previous` if it is crash-looping.

| Status | Usual cause |
|---|---|
| `Pending` | No node has room (check requests), or a PVC is unbound, or no node matches the `nodeSelector` |
| `ImagePullBackOff` | Wrong image name or tag, or the image was not loaded into kind |
| `CrashLoopBackOff` | The process starts and exits. Read the logs |
| `OOMKilled` (in `describe`) | Exceeded its memory limit |
| `Running`, but `0/1` ready | The readiness probe is failing |
| No worker Pods at all | Normal here: KEDA scaled them to zero because the queues are empty |

---

## 7. Hands-on: run docuflow on a laptop cluster

No GPU is needed. The `ci-sim` overlay uses a fake model server ([k8s/components/simulator/](../k8s/components/simulator/),
code in [docuflow/simmodel.py](../docuflow/simmodel.py)) so the whole system, including queues and autoscaling, runs on
CPU.

**Prerequisites:** Docker, `kind`, `kubectl`, `helm`. `kind` ("Kubernetes in Docker") runs a whole cluster inside Docker
containers, so it is the easiest way to start.

**1. Create the cluster**
```bash
GPU=0 MONITORING=0 scripts/cluster-up.sh
```
This creates a kind cluster ([deploy/kind/cluster.yaml](../deploy/kind/cluster.yaml)) and installs the add-ons the
manifests rely on: KEDA (provides the `ScaledObject` kind), metrics-server and ingress-nginx.

**2. Build the image and deploy**
```bash
make deploy OVERLAY=ci-sim
```
This builds the image, loads it into kind with `kind load docker-image` (the cluster cannot see your local Docker images
otherwise, see [Makefile:56](../Makefile#L56)), applies `k8s/overlays/ci-sim`, and waits for the database, RabbitMQ and
API.

**3. Look around**
```bash
kubectl -n docuflow get pods,svc,scaledobject,hpa
kubectl -n docuflow logs deploy/api
make k8s-status
```
There will be no worker Pods yet. That is scale-to-zero working.

**4. Watch autoscaling happen**
In one terminal: `kubectl -n docuflow get pods -w`. In another, run the smoke test, which ingests pages and asks
questions:
```bash
MODE=gt scripts/smoke.sh http://docuflow.localtest.me
```
Worker Pods appear when the queues fill and disappear after the cooldown (`cooldownPeriod: 60` in
[keda.yaml](../k8s/base/keda.yaml)).

**5. Break things on purpose**
```bash
kubectl -n docuflow delete pod -l app=api      # the Deployment recreates it immediately
kubectl -n docuflow exec -it rabbitmq-0 -- rabbitmqctl list_queues   # see the work tokens
kubectl -n docuflow port-forward svc/api 8791:8791   # then browse http://localhost:8791
```

**6. Clean up**
```bash
make undeploy OVERLAY=ci-sim     # remove docuflow
make cluster-down                # delete the cluster
```

The same flow with real GPUs is `make cluster-up` then `make deploy OVERLAY=local-gpu`. Production steps are in the
`prod-*` targets of the [Makefile](../Makefile) and the header of
[k8s/overlays/prod/kustomization.yaml](../k8s/overlays/prod/kustomization.yaml).

---

## 8. Following one request through every object

Ingesting a page:

1. **You** send `POST /ingest` to `docuflow.localtest.me`.
2. The **Ingress** ([ingress.yaml](../k8s/components/ingress/ingress.yaml)) forwards it to the `api` **Service**, which
   picks one of the `api` **Pods** (created by the Deployment).
3. The API ([docuflow/api.py](../docuflow/api.py), `ingest`) writes jobs into **Postgres**, reached through the `db`
   Service and the `db-0` StatefulSet Pod with its PVC-backed disk. It then publishes a token to **RabbitMQ**
   ([docuflow/broker.py](../docuflow/broker.py), `notify`).
4. **KEDA** polls the queue every 5 seconds ([keda.yaml](../k8s/base/keda.yaml)), sees a backlog, and raises the
   `worker-io` Deployment from 0 to a few Pods via an HPA.
5. A `worker-io` **Pod** takes a token and claims the job in Postgres
   ([docuflow/jobs.py](../docuflow/jobs.py), `claim`). It fetches the page and enqueues an `extract` job
   ([docuflow/pipeline.py](../docuflow/pipeline.py), `run_fetch`).
6. KEDA scales `worker-vlm`. Each Pod calls `http://model-vlm:8000/v1`, a **Service** in front of the GPU **Pods**
   ([k8s/components/models/vlm.yaml](../k8s/components/models/vlm.yaml)), which KEDA scales on vLLM load
   ([autoscaling.yaml](../k8s/components/models/autoscaling.yaml)).
7. `worker-embed` embeds the text and marks the page indexed.
8. All Pods get their settings from the **ConfigMap** and passwords from the **Secret**. **Prometheus** scrapes `/metrics`
   from the Pods through the **PodMonitors**. The hourly **CronJob** cleans up old rows. When the queues are empty, KEDA
   scales the workers back to zero.

---

## 9. Suggested reading order

1. [docker-compose.yml](../docker-compose.yml), which you already understand.
2. [k8s/base/api.yaml](../k8s/base/api.yaml): Deployment, Service, probes, PDB.
3. [k8s/base/config.yaml](../k8s/base/config.yaml): ConfigMap.
4. [k8s/base/db.yaml](../k8s/base/db.yaml): StatefulSet and PVC.
5. [k8s/base/workers.yaml](../k8s/base/workers.yaml): the same pattern repeated, plus priority and security.
6. [k8s/base/keda.yaml](../k8s/base/keda.yaml): autoscaling.
7. [k8s/overlays/ci-sim/kustomization.yaml](../k8s/overlays/ci-sim/kustomization.yaml): how layers combine.
8. [k8s/base/networkpolicy.yaml](../k8s/base/networkpolicy.yaml): the firewall rules.
9. [k8s/components/models/vlm.yaml](../k8s/components/models/vlm.yaml): GPUs, node selection, tolerations.
10. [docs/DOCUFLOW.md](DOCUFLOW.md): the full system design.

---

## 10. Cheat sheet

| Concept | One line | Where in this repo |
|---|---|---|
| Pod | A running container (or a few) | created by every Deployment |
| Deployment | Keep N identical Pods alive | [api.yaml](../k8s/base/api.yaml), [workers.yaml](../k8s/base/workers.yaml) |
| StatefulSet | Same, for things with their own disk | [db.yaml](../k8s/base/db.yaml), [rabbitmq.yaml](../k8s/base/rabbitmq.yaml) |
| Service | Stable internal address and load balancer | [api.yaml:2](../k8s/base/api.yaml#L2), [db.yaml:4](../k8s/base/db.yaml#L4) |
| Ingress | Front door from outside | [ingress.yaml](../k8s/components/ingress/ingress.yaml) |
| ConfigMap | Non-secret env config | [config.yaml](../k8s/base/config.yaml) |
| Secret | Passwords and keys | generated in overlays, `make prod-secrets` in prod |
| Namespace | A named folder (`docuflow`) | [namespace.yaml](../k8s/base/namespace.yaml) |
| PV / PVC | Disk that outlives a Pod | [model-cache.yaml](../k8s/components/models/model-cache.yaml), [db.yaml:43](../k8s/base/db.yaml#L43) |
| CronJob | Scheduled run-to-completion | [gc-cronjob.yaml](../k8s/base/gc-cronjob.yaml) |
| Probes | startup / readiness / liveness | [api.yaml:37](../k8s/base/api.yaml#L37) |
| requests / limits | Reserved vs maximum resources | [api.yaml:41](../k8s/base/api.yaml#L41) |
| PDB | Cap simultaneous voluntary disruptions | [api.yaml:47](../k8s/base/api.yaml#L47) |
| PriorityClass | Who wins when the cluster is full | [priorityclasses.yaml](../k8s/base/priorityclasses.yaml) |
| NetworkPolicy | Pod-to-Pod firewall | [networkpolicy.yaml](../k8s/base/networkpolicy.yaml) |
| nodeSelector / tolerations | Pick the right machines | [vlm.yaml:26](../k8s/components/models/vlm.yaml#L26) |
| CRD | A new kind added by a tool | none defined here, see [section 4](#4-autoscaling-and-custom-resources) |
| ScaledObject (KEDA) | Autoscale on queues and metrics, down to zero | [keda.yaml](../k8s/base/keda.yaml) |
| Kustomize | Layer environments on a base | [k8s/overlays/](../k8s/overlays/) |
| kubectl | `docker` for the cluster | [section 6](#6-commands-you-will-actually-use) |

## Known gaps worth knowing about

These are facts about the current manifests that are easy to trip over:
- `worker-ragas` runs 2 replicas, but `LIMIT_RAGAS` and `PROCS_RAGAS` are not in the ConfigMap and default to 1 in
  [docuflow/config.py](../docuflow/config.py), so the second Pod sits idle.
- There are no RBAC objects, no egress NetworkPolicies, no `ResourceQuota` or `LimitRange`, and no init containers.
- Postgres and RabbitMQ are single-replica StatefulSets. The comments in [db.yaml](../k8s/base/db.yaml) and
  [rabbitmq.yaml](../k8s/base/rabbitmq.yaml) point to managed services or operators for HA.
