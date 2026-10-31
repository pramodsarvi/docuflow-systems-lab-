# Kubernetes command reference for docuflow

Every command used to build, deploy, scale, inspect, debug and recover docuflow. Each entry explains what the
command does, why this project uses it, what to look for in the output, and any gotchas we hit. Commands are written
for the local cluster (`kind`, context `kind-docuflow`, namespace `docuflow`); the same commands work on any
cluster with the right context.

Conventions: `NS=docuflow`. `<pod>` means a pod name from `kubectl get pods`. Most `make` targets wrap these commands
(see the last section).

---

## 1. Cluster lifecycle (kind)

### Create the cluster
```bash
kind create cluster --name docuflow --config deploy/kind/cluster.yaml --image kindest/node:v1.31.0 --wait 180s
```
**What it does.** Starts a Kubernetes cluster whose nodes are Docker containers: one control plane, one GPU worker
and one CPU worker, as defined in `deploy/kind/cluster.yaml`.
**Why.** kind gives a real Kubernetes API, scheduler and kubelets on one machine, enough to exercise KEDA, HPA,
GPU scheduling and failure handling exactly as on a cloud cluster.
**Flags.**
- `--config`: node roles, labels (`docuflow.io/pool=gpu|cpu`), host port mappings (80/443 for ingress), and the
  `/dev/null` mount that passes GPUs into the GPU node.
- `--image`: pins the Kubernetes version.
- `--wait`: blocks until the control plane is ready.

**Output to expect.** `Ready` nodes in `kubectl get nodes` within about a minute.

### Delete the cluster
```bash
kind delete cluster --name docuflow
```
Removes the node containers and everything in the cluster. Model weights survive, because they live on the host in
`/var/cache/docuflow-hf`.

### Load a locally built image into the nodes
```bash
docker build -t docuflow:dev .
kind load docker-image docuflow:dev --name docuflow
```
**What it does.** Copies the image from the host's Docker into every node's containerd, so pods can start it with
`imagePullPolicy: IfNotPresent` and no registry.
**Gotcha.** For some large or multi-platform images (vLLM), `kind load` fails with `content digest ... not found`.
Let the node pull from the registry instead (`docker exec docuflow-worker crictl pull <image>`).

### Switch kubectl to the cluster
```bash
kubectl config use-context kind-docuflow
kubectl config current-context
```
kind writes a context named `kind-<cluster>` into `~/.kube/config`. Every `kubectl` command acts on the current
context, so check it before changing anything.

---

## 2. Preparing the GPU node

These run inside the GPU node container; `scripts/cluster-up.sh` does all of them.

### Run a command inside a kind node
```bash
docker exec docuflow-worker nvidia-smi -L
```
A kind node is a container, so `docker exec` gives a shell on the "machine". Here it confirms the host GPU is visible
inside the node: one line per GPU, `GPU 0: NVIDIA L40S (UUID: ...)`.

### Make the NVIDIA runtime containerd's default inside the node
```bash
docker exec docuflow-worker bash -c 'nvidia-ctk runtime configure --runtime=containerd --set-as-default && systemctl restart containerd'
```
**Why.** Pods get their containers from the node's containerd. Without the NVIDIA runtime as default, a pod that
requests `nvidia.com/gpu` is scheduled but can't see the device.

### Unmask the NVIDIA proc files
```bash
docker exec docuflow-worker bash -c 'for m in $(mount | awk "/\/proc\/driver\/nvidia/ {print \$3}"); do umount "$m"; done'
```
**Why.** The host toolkit masks `/proc/driver/nvidia/params` inside the node. NVML in nested containers then fails
(`driverInitFileInfo ... result=11`), and the device plugin can't enumerate GPUs.

### Taint the GPU node
```bash
kubectl taint nodes -l docuflow.io/pool=gpu nvidia.com/gpu=present:NoSchedule --overwrite
```
**What it does.** Adds a taint so that only pods with a matching toleration (the vLLM servers, device plugin, DCGM)
can run there. `-l` selects nodes by label; `--overwrite` makes it idempotent.
**Why.** It keeps CPU workloads (workers, API, database) off the GPU node, where they'd compete for CPU with vLLM.

### Label the GPU node for the device plugin
```bash
kubectl label nodes -l docuflow.io/pool=gpu nvidia.com/gpu.present=true --overwrite
```
**Why.** The NVIDIA device-plugin chart only schedules onto nodes labelled by Node Feature Discovery. Without the
label its DaemonSet has **0 pods** and no GPU is ever advertised.

### Check what each node offers
```bash
kubectl get nodes -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.docuflow\.io/pool,GPU:.status.allocatable.nvidia\.com/gpu'
```
**What it does.** Prints selected fields with JSONPath. Dots inside label and resource names are escaped with `\.`.
**Expect.** `GPU 6` on the GPU node: one L40S advertised as 6 time-sliced units.

---

## 3. Installing platform add-ons (Helm)

### Add chart repositories
```bash
helm repo add kedacore https://kedacore.github.io/charts
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin
helm repo update
```
Registers where charts come from and refreshes their indexes. Charts are versioned packages of Kubernetes manifests.

### Install or upgrade a release
```bash
helm upgrade --install keda kedacore/keda -n keda --create-namespace --version 2.15.1 --wait
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system --set 'args={--kubelet-insecure-tls}' --wait
helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
  --version 65.5.0 -f deploy/helm-values/kube-prometheus-stack.yaml --wait --timeout 10m
helm upgrade --install nvidia-device-plugin nvdp/nvidia-device-plugin -n nvidia --create-namespace \
  --version 0.17.0 -f deploy/helm-values/nvidia-device-plugin.yaml --wait
```
**What it does.** `upgrade --install` installs the release if it doesn't exist and upgrades it if it does, so scripts
can run it repeatedly.
- `-n` / `--create-namespace`: target namespace.
- `--version`: pins the chart version (unpinned charts drift).
- `-f`: a values file.
- `--set`: a single override.
- `--wait`: block until the release's pods are ready.

**What each one is for here.**
- **KEDA:** queue-driven autoscaling.
- **metrics-server:** CPU and memory metrics for HPA and `kubectl top`. `--kubelet-insecure-tls` is needed because
  kind kubelets use self-signed certificates.
- **kube-prometheus-stack:** Prometheus, Grafana, Alertmanager, kube-state-metrics.
- **device plugin:** advertises GPUs, with time-slicing configured in the values file.

**Gotcha.** The device-plugin chart refuses values that set both `config.name` and `config.map`. Use only `config.map`.

### Change one value of an installed release
```bash
helm upgrade kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --version 65.5.0 \
  --reuse-values --set "grafana.ingress.hosts={grafana.localtest.me,grafana.<ip>.nip.io}" --wait
```
`--reuse-values` keeps every previously set value and applies only the new `--set`. Without it, an upgrade resets
everything to chart defaults plus what you pass.

### See what is installed
```bash
helm ls -A
```
Lists every release in every namespace (`-A`), with chart version and status.

---

## 4. Deploying docuflow (Kustomize)

### Render an overlay without applying it
```bash
kustomize build k8s/overlays/local-gpu          # or: kubectl kustomize k8s/overlays/local-gpu
```
**What it does.** Merges `k8s/base` with the overlay's components (models, monitoring, ingress), patches and
generated Secret, and prints the final YAML.
**Why.** You can review exactly what will be applied, and validate it in CI:
```bash
kustomize build k8s/overlays/prod | kubeconform -strict -summary -kubernetes-version 1.31.0 -schema-location default \
  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json'
```
`kubeconform` checks every object against the Kubernetes schemas, and the CRD catalog covers KEDA and Prometheus
Operator types. The result should be `Invalid: 0, Errors: 0`.

### Apply an overlay
```bash
kubectl apply -k k8s/overlays/local-gpu
```
**What it does.** Renders the overlay and creates or updates every object (server-side diff against the
`last-applied-configuration` annotation).
**Output.** One line per object: `created`, `configured` (changed) or `unchanged`.
**Gotchas.**
- **Never put `replicas:` on a Deployment that KEDA manages.** Every apply would reset the live count. Removing the
  field later needs the fix in section 9.
- A full apply also resets anything you live-patched (ScaledObject maxima, ConfigMap values). Use the targeted
  commands in section 7 for runtime changes.
- `Warning: would violate PodSecurity "restricted:latest"` is informational. The namespace *enforces* `baseline`
  and only *warns* on `restricted`; vLLM and Postgres images need root.

### Apply only some objects from an overlay
```bash
kustomize build k8s/overlays/local-gpu | python3 -c "
import sys, yaml
docs = [d for d in yaml.safe_load_all(sys.stdin) if d and (d['kind'] == 'PrometheusRule' or d['metadata']['name'] == 'docuflow-grafana-dashboard')]
yaml.safe_dump_all(docs, sys.stdout)" | kubectl apply -f -
```
**Why.** It updates the Grafana dashboard and alert rules without touching Deployments or ScaledObjects. `-f -`
reads manifests from stdin.

### Apply a single file
```bash
kubectl apply -f deploy/some-object.yaml
```

### Remove docuflow
```bash
kubectl delete -k k8s/overlays/local-gpu --ignore-not-found
```
Deletes everything the overlay created. PersistentVolumes with `Retain` (the model cache) keep their data.

---

## 5. Seeing what's running

### Pods, ScaledObjects and HPAs at a glance
```bash
kubectl -n docuflow get pods,scaledobject,hpa -o wide
```
Several resource types in one call. `-o wide` adds node and IP columns.
**Read it like this.**
- Pod `STATUS` should be `Running` with `READY 1/1`.
- ScaledObjects: `READY=True`, and `ACTIVE=True` when there's work.
- HPA `TARGETS` shows the live metric against the target, e.g. `25667m/4 (avg)` = 25.7 queries per pod against a
  target of 4.

### Count pods by namespace or workload
```bash
kubectl get pods -A --no-headers | awk '{print $1}' | sort | uniq -c
kubectl -n docuflow get pods --no-headers | awk '{split($1,a,"-"); print a[1]"-"a[2]}' | sort | uniq -c
```
`-A` covers all namespaces, and `--no-headers` makes the output easy to pipe.

### Watch changes live
```bash
kubectl -n docuflow get pods -w
watch -n2 kubectl -n docuflow get scaledobject,hpa
```
`-w` streams changes as they happen, which is the easiest way to watch KEDA add and remove workers.

### Replica counts per Deployment
```bash
kubectl -n docuflow get deploy -o custom-columns=NAME:.metadata.name,WANT:.spec.replicas,READY:.status.readyReplicas
```
`WANT` is what KEDA/HPA asked for, and `READY` is what's serving. A persistent gap means pods can't start: check
pending pods and events.

### Why is a pod pending or failing?
```bash
kubectl -n docuflow describe pod <pod>
kubectl -n docuflow get pods --field-selector=status.phase=Pending
kubectl -n docuflow get events --sort-by=.lastTimestamp | tail -20
```
`describe` shows the scheduler's reason at the bottom (`Events`). Ones we saw:
- `pod has unbound immediate PersistentVolumeClaims`: the volume isn't bound yet. It resolves itself.
- `0/3 nodes are available: insufficient nvidia.com/gpu`: all GPU slots are taken.
- `CrashLoopBackOff`: the container keeps exiting. Read its logs.

### Logs
```bash
kubectl -n docuflow logs deploy/api --tail=50                 # one pod of a Deployment
kubectl -n docuflow logs <pod> --previous                      # the container that just crashed
kubectl -n docuflow logs -l app=worker-vlm --tail=20 --prefix  # all pods with a label, each line prefixed
kubectl -n keda logs deploy/keda-operator --since=5m | grep ERROR
```
**`--previous` is the key one for crash loops.** The current container may have just restarted and logged nothing
yet. For example, it showed vLLM's real error: `Free memory on device ... less than desired GPU memory utilization`.

### Resource usage
```bash
kubectl top nodes
kubectl top pods -A --sort-by=cpu | head
```
Live CPU and memory from metrics-server. This is how we found the three vLLM page readers using ~17 cores each:
thread oversubscription.

---

## 6. Autoscaling with KEDA and HPA

### Status of every ScaledObject
```bash
kubectl -n docuflow get scaledobject
kubectl -n docuflow get scaledobject -o custom-columns=NAME:.metadata.name,MIN:.spec.minReplicaCount,MAX:.spec.maxReplicaCount,READY:.status.conditions[0].status,ACTIVE:.status.conditions[1].status
```
- `READY=False` means KEDA can't read the trigger: bad host, DNS, auth or a missing TriggerAuthentication.
- `ACTIVE` means at least one trigger is above its activation value.

### Why is a ScaledObject not ready?
```bash
kubectl -n docuflow get scaledobject worker-vlm -o jsonpath='{range .status.conditions[*]}{.type}={.status} {.message}{"\n"}{end}'
kubectl -n keda logs deploy/keda-operator --since=5m | grep -i error | tail
```
Errors we hit, and their fixes:
- `lookup rabbitmq ... no such host`: KEDA runs in the `keda` namespace, so use FQDNs
  (`rabbitmq.docuflow.svc.cluster.local`).
- `TriggerAuthentication ... not found`: a creation-order race; apply TriggerAuthentications first.

### The HPA that KEDA created
```bash
kubectl -n docuflow get hpa
kubectl -n docuflow describe hpa keda-hpa-worker-vlm
```
KEDA creates one HPA per ScaledObject, named `keda-hpa-<name>`. `describe` shows each metric's current value, the
desired replica count, and scaling events with reasons (for example `New size: 12; reason: external metric
s0-rabbitmq-docuflow-vlm above target`).

### Force KEDA to reconcile now
```bash
kubectl -n docuflow annotate scaledobject --all reconcile=$(date +%s) --overwrite
kubectl -n keda rollout restart deploy/keda-operator
```
After fixing a broken trigger, the operator may be in a long error backoff. Changing an annotation triggers a
reconcile; restarting the operator clears all backoffs. We needed the restart once: the ScaledObjects went `READY`
within 30 s.

### Change autoscaling limits at runtime
```bash
kubectl -n docuflow patch scaledobject worker-vlm --type merge -p '{"spec":{"maxReplicaCount":72}}'
```
**What it does.** A JSON merge patch that changes only the listed field. KEDA updates its HPA within seconds.
**Why not edit the YAML and apply?** A full apply resets other live changes. A patch is targeted, which is what
`scripts/mode.sh` uses.
**Remember.** In-app limits (`PER_DATASET_CAP`, `LIMIT_VLM`) must stay above the new maximum, or the extra pods sit
idle (challenge 1).

### Scale a Deployment by hand
```bash
kubectl -n docuflow scale deploy/model-llm --replicas=0
```
Sets replicas directly. Use it on Deployments **without** a ScaledObject (`model-llm` locally). On a KEDA-managed
Deployment, the HPA overrides a manual scale within seconds.

---

## 7. Configuration and secrets

### Create or update a Secret idempotently
```bash
kubectl -n docuflow create secret generic docuflow-secrets \
  --from-literal=POSTGRES_PASSWORD="$POSTGRES_PASSWORD" --from-literal=DOCUFLOW_DSN="$DOCUFLOW_DSN" \
  --dry-run=client -o yaml | kubectl apply -f -
```
**Why this pattern.** `create` fails if the Secret exists. `--dry-run=client -o yaml` only renders it, and `apply`
then creates or updates it. Secret values never appear in a committed file.

### Change one key of a Secret or ConfigMap
```bash
kubectl -n docuflow patch secret docuflow-secrets --type merge -p '{"stringData":{"DOCUFLOW_API_KEY":"<key>"}}'
kubectl -n docuflow patch configmap docuflow-config --type merge -p '{"data":{"QUERY_PRESSURE_INFLIGHT":"0"}}'
```
`stringData` takes plain text, and Kubernetes base64-encodes it. **Pods read environment variables at start**, so
restart the Deployments that use them (section 8).

### Read a value
```bash
kubectl -n docuflow get cm docuflow-config -o jsonpath='{.data.LIMIT_VLM}'
kubectl -n docuflow get secret docuflow-secrets -o jsonpath='{.data.DOCUFLOW_DSN}' | base64 -d
```

### Change one argument of a container
```bash
kubectl -n docuflow get deploy model-vlm -o json | python3 -c "
import json, sys
d = json.load(sys.stdin); a = d['spec']['template']['spec']['containers'][0]['args']
i = a.index('--max-num-seqs'); a[i + 1] = '24'
print(json.dumps([{'op': 'replace', 'path': '/spec/template/spec/containers/0/args', 'value': a}]))" \
| xargs -0 -I{} kubectl -n docuflow patch deploy model-vlm --type json -p {}
```
A JSON Patch (`--type json`) replaces the whole `args` list with an edited copy. That's how ingest mode resizes the
vLLM page readers' KV cache and batch slots without editing manifests. Changing the pod template triggers a rollout.

---

## 8. Rollouts

### Restart pods to pick up a new image or config
```bash
kubectl -n docuflow rollout restart deploy/api deploy/worker-vlm
```
Adds a timestamp annotation to the pod template, which makes the Deployment replace its pods according to its
strategy. With the `:dev` tag and `IfNotPresent`, this is how a freshly loaded image takes effect.

### Wait for a rollout to finish
```bash
kubectl -n docuflow rollout status deploy/model-vlm --timeout=900s
```
Blocks until every new pod is ready, and fails on timeout. Scripts use it before the next step.

### Rollout strategies, and why they differ here
- **Workers and API (`RollingUpdate`):** new pods start before old ones stop, and each worker finishes its current
  job during its 180 s grace period.
- **vLLM on a shared GPU (`Recreate`):** a surge pod can't get GPU memory while the old ones hold it, so rolling
  updates deadlock (`Free memory on device (1.2/44.4 GiB) ... less than desired`). `Recreate` stops the old pods
  first. In-flight page reads are deferred (ADR-05), not lost.

### Inspect history and roll back
```bash
kubectl -n docuflow rollout history deploy/api
kubectl -n docuflow rollout undo deploy/api
```
`undo` returns to the previous ReplicaSet. `revisionHistoryLimit: 5` keeps five.

---

## 9. Fixing `kubectl apply` fighting KEDA

```bash
for d in api worker-io worker-vlm worker-embed model-vlm model-embed; do
  kubectl -n docuflow get deploy $d -o jsonpath='{.metadata.annotations.kubectl\.kubernetes\.io/last-applied-configuration}' \
  | python3 -c "import sys,json; c=json.load(sys.stdin); c['spec'].pop('replicas',None); print(json.dumps(c))" > /tmp/la.json
  kubectl -n docuflow annotate deploy $d --overwrite kubectl.kubernetes.io/last-applied-configuration="$(cat /tmp/la.json)"
done
```
**What it does.** Removes `replicas` from the `last-applied-configuration` annotation of each KEDA-managed
Deployment.
**Why.** `kubectl apply` compares the new manifest with this annotation. If `replicas` was applied before and is now
missing from the manifest, apply deletes the field, which resets the Deployment to 1 replica. After this one-time
fix, applies leave KEDA's counts alone.

---

## 10. Reaching services from your machine

### Port-forward a Service
```bash
kubectl -n docuflow port-forward svc/api 8792:8791
kubectl -n monitoring port-forward svc/kube-prometheus-stack-prometheus 19090:9090
kubectl -n docuflow port-forward svc/model-llm 18002:8000
```
**What it does.** Tunnels a local port to a Service through the API server; it runs until you stop it. Used here to
call the API without ingress, query Prometheus's HTTP API, and let the RAGAS judge reach the vLLM servers.
**Note.** Port-forwarding bypasses NetworkPolicies and only listens on localhost.

### Ingress routes
```bash
kubectl get ingress -A
```
Shows host → Service routing: `docuflow.localtest.me` → api, `grafana.localtest.me` → Grafana. `*.localtest.me`
resolves to 127.0.0.1, and kind maps host ports 80/443 to the ingress controller.

### A throwaway pod for in-cluster checks
```bash
kubectl -n docuflow run check --rm -i --restart=Never --image=docuflow:dev --image-pull-policy=Never --command -- \
  python -c "import httpx; print(httpx.get('http://model-vlm:8000/health').status_code)"
```
- `--rm -i`: attach, then delete the pod when the command ends.
- `--restart=Never`: create a bare Pod, not a Deployment.

This tests DNS, Services and NetworkPolicies from inside the namespace, where the workers run.

---

## 11. Inspecting state through running pods

### Query Postgres
```bash
kubectl -n docuflow exec db-0 -- psql -U docuflow -c "SELECT lane, status, count(*) FROM jobs GROUP BY 1,2 ORDER BY 1,2"
kubectl -n docuflow exec db-0 -- psql -U docuflow -At -c "SELECT count(*) FILTER (WHERE status='indexed'), count(*) FROM documents"
```
`exec` runs a command in an existing container. `-At` gives unaligned, tuples-only output for scripts. This is how
every ingest-progress number in the write-up was measured.

### RabbitMQ queue depths (KEDA's input)
```bash
kubectl -n docuflow exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages_ready messages_unacknowledged consumers
```
`messages_ready` on `docuflow.<lane>` is what the KEDA rabbitmq scaler reads. The `.delay` queues hold retry and
outage backoffs.

### vLLM's own load
```bash
kubectl -n docuflow exec <model-vlm-pod> -- python3 -c "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8000/metrics').read().decode())" \
  | grep -E '^vllm:(num_requests_running|num_requests_waiting|kv_cache_usage_perc)'
```
- `running`: requests in the batch (capped by `--max-num-seqs`).
- `waiting`: requests queued for a slot.
- `kv_cache_usage_perc`: cache fill.

This is how we found the load imbalance between replicas (16+6 waiting vs 2).

### Check a value inside a running pod
```bash
kubectl -n docuflow exec deploy/worker-vlm -- python -c "from docuflow import config; print(config.VLM_MAX_TOKENS)"
```
It confirms a rollout really runs the new code and config before you rely on it.

---

## 12. Access control

### Check what an identity may do
```bash
kubectl auth can-i list pods -n docuflow --as=system:serviceaccount:headlamp:viewer
kubectl auth can-i delete pods -n docuflow --as=system:serviceaccount:headlamp:viewer
```
`--as` impersonates a ServiceAccount, so you can verify RBAC before handing out its token. The read-only viewer
must answer `yes` to list and `no` to delete, patch and get secrets.

### Issue a short-lived token
```bash
kubectl -n headlamp create token viewer --duration=168h
```
Creates a time-bound token for a ServiceAccount (7 days here). Nothing is stored in a Secret; the token simply
expires.

---

## 13. More commands used along the way

### Is the cluster already there?
```bash
kind get clusters
```
Lists kind clusters on this machine. `cluster-up.sh` uses it to skip creation when `docuflow` already exists, so the
script can be re-run safely.

### Wait for nodes to become Ready
```bash
kubectl wait --for=condition=Ready nodes --all --timeout=120s
```
Blocks until every node reports `Ready`, or fails at the timeout. Used after restarting containerd on the GPU node:
the node flips to `NotReady` for a few seconds, and installing the device plugin before it recovers would race.
`kubectl wait` works on any condition: `--for=condition=Available deploy/api`, or `--for=delete pod/<pod>`.

### Create a namespace idempotently
```bash
kubectl create namespace docuflow --dry-run=client -o yaml | kubectl apply -f -
```
`create namespace` fails if it exists. Rendering with `--dry-run=client` and piping to `apply` makes it safe to
repeat. It's the same pattern as Secrets in section 7, used before creating production secrets.

### Delete a pod so its controller replaces it
```bash
kubectl -n docuflow delete pod -l app=model-embed --wait=false
```
The Deployment's ReplicaSet notices the missing pod and creates a fresh one. It's useful to retry a pod stuck in
`CrashLoopBackOff` once the cause is fixed: here, after freeing GPU memory for the embedding server. `--wait=false`
returns immediately instead of waiting for termination.

### Volumes
```bash
kubectl -n docuflow get pvc
kubectl get pv
```
- **PersistentVolumeClaims:** what pods ask for (`model-cache`, `data-db-0`, `data-rabbitmq-0`).
- **PersistentVolumes:** what satisfies them. Cluster-scoped, so no `-n`.

`STATUS Bound` means the claim has storage. A pod waiting on an unbound claim stays `Pending` with
`pod has unbound immediate PersistentVolumeClaims`, which is what the vLLM pods showed for their first few seconds.
The model cache is a pre-created hostPath PV with `Retain`, so ~20 GB of weights survive `kind delete cluster`.

### Which pods a Service sends traffic to
```bash
kubectl -n docuflow get endpointslices -l kubernetes.io/service-name=model-vlm -o wide
kubectl -n docuflow get svc
```
EndpointSlices list the ready pod IPs behind a Service. If a Service has no endpoints, its selector matches no ready
pod: check labels and readiness probes. For the vLLM servers, each replica appears only after `/health` passes,
which is why a loading model receives no traffic.

### StatefulSets: database and broker
```bash
kubectl -n docuflow rollout status sts/db sts/rabbitmq --timeout=300s
kubectl -n docuflow get sts
```
Postgres and RabbitMQ run as StatefulSets: stable names (`db-0`, `rabbitmq-0`), one volume per replica, ordered
start. `rollout status` works for them like it does for Deployments. `make deploy` waits on both before checking the
API, because the API's readiness depends on the database.

### Images inside a node
```bash
docker exec docuflow-worker crictl images | grep -E "docuflow|vllm"
docker exec docuflow-worker crictl pull docker.io/vllm/vllm-openai:v0.13.0
```
`crictl` talks to the node's containerd directly: list what's cached, or pre-pull a large image. Pre-pulling the
~8 GB vLLM image avoided a long `ContainerCreating` phase on first deploy, after `kind load` failed for that image.

### Platform status
```bash
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s
kubectl -n nvidia get pods
kubectl -n keda get pods
```
Health of the add-ons the app depends on. If KEDA's pods aren't running, no ScaledObject scales. If the device
plugin isn't running, no GPU pod schedules.

---

## 14. `make` targets and the commands behind them

| target | runs |
|---|---|
| `make cluster-up` | `scripts/cluster-up.sh`: sections 1–3 |
| `make deploy` | `docker build` + `kind load` + `kubectl apply -k` + `rollout status` |
| `make render` | `kustomize build` + `kubeconform` for every overlay |
| `make k8s-status` | `get pods,scaledobject,hpa` + node GPU columns + RabbitMQ `list_queues` |
| `make ingest-mode` / `query-mode` | `scripts/mode.sh`: API pause/resume, `scale`, `patch scaledobject`, `patch configmap`, vLLM arg patches |
| `make smoke`, `make stress`, `make quality-eval`, `make ragas` | HTTP checks, load generator and evals (they use `port-forward` where needed) |
| `make cluster-down` | `kind delete cluster` |

---

## Index

| command | section |
|---|---|
| `crictl images` / `crictl pull` (via `docker exec`) | 13 |
| `docker exec <node> ...` | 2 |
| `helm repo add` / `helm repo update` | 3 |
| `helm upgrade --install` / `--reuse-values` / `helm ls` | 3 |
| `kind create cluster` / `kind delete cluster` / `kind get clusters` | 1, 13 |
| `kind load docker-image` | 1 |
| `kubeconform` | 4 |
| `kubectl annotate` (force reconcile; last-applied fix) | 6, 9 |
| `kubectl apply -k` / `-f` / `-f -` | 4 |
| `kubectl auth can-i --as` | 12 |
| `kubectl config use-context` / `current-context` | 1 |
| `kubectl create namespace` / `create secret` (`--dry-run=client` pattern) | 7, 13 |
| `kubectl create token` | 12 |
| `kubectl delete -k` / `delete pod` | 4, 13 |
| `kubectl describe pod` / `describe hpa` | 5, 6 |
| `kubectl exec` (psql, rabbitmqctl, vLLM metrics, config check) | 11 |
| `kubectl get` (pods, deploy, sts, scaledobject, hpa, nodes, pvc, pv, svc, endpointslices, events, ingress) | 5, 6, 10, 13 |
| `kubectl get -o jsonpath` / `-o custom-columns` | 2, 5, 6, 7 |
| `kubectl label` / `kubectl taint` | 2 |
| `kubectl logs` (`--tail`, `--previous`, `-l`, `--prefix`, `--since`) | 5 |
| `kubectl patch` (merge: scaledobject, configmap, secret; json: container args) | 6, 7 |
| `kubectl port-forward` | 10 |
| `kubectl rollout restart` / `status` / `history` / `undo` | 8, 13 |
| `kubectl run --rm -i --restart=Never` | 10 |
| `kubectl scale` | 6 |
| `kubectl top nodes` / `top pods` | 5 |
| `kubectl wait --for=condition=...` | 13 |
| `kustomize build` / `kubectl kustomize` | 4 |
