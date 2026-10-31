# Part 8. Recreate it

From a bare Linux machine with an NVIDIA GPU (driver 570 or newer) to a stress report. Every step is a script or
make target in the repository.

## 1. Host prerequisites

```bash
# Docker + the NVIDIA container toolkit (Ubuntu)
sudo apt-get install -y docker.io
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | sudo gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
  | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' \
  | sudo tee /etc/apt/sources.list.d/nvidia-container-toolkit.list
sudo apt-get update && sudo apt-get install -y nvidia-container-toolkit

# GPUs into kind nodes
sudo nvidia-ctk runtime configure --runtime=docker --set-as-default
sudo nvidia-ctk config --set accept-nvidia-visible-devices-as-volume-mounts=true --in-place
sudo systemctl restart docker

# CLIs: kind 0.24, kubectl, helm 3, kustomize 5 (binaries into ~/.local/bin)
```

Check that the GPU passes through: `docker run --rm -v /dev/null:/var/run/nvidia-container-devices/all ubuntu nvidia-smi -L`.

## 2. Cluster and platform

```bash
git clone https://github.com/pramodsarvi/docuflow-systems-lab-.git && cd docuflow-systems-lab-
make cluster-up
```

`scripts/cluster-up.sh` does the following:
- raises the inotify limits
- creates the kind cluster (control plane with ingress ports, GPU worker, CPU worker)
- sets up the NVIDIA runtime inside the GPU node, then taints and labels it
- installs, through Helm:
  - metrics-server
  - ingress-nginx
  - kube-prometheus-stack
  - KEDA
  - the NVIDIA device plugin with time-slicing x6
  - the DCGM exporter

At the end the GPU node should show `nvidia.com/gpu: 6`.

## 3. Deploy docuflow

```bash
make deploy          # builds the image, loads it into kind, applies k8s/overlays/local-gpu, waits for vLLM
make smoke           # 6 pages through VLM reading, then an agent question with a checked citation
make k8s-status      # pods, ScaledObjects, HPAs, GPUs, RabbitMQ queues
```

The first start downloads about 20 GB of model weights into `/var/cache/docuflow-hf`; later starts take about a
minute. The API is at `http://docuflow.localtest.me`, Grafana at `http://grafana.localtest.me`.

Without a GPU, run `GPU=0 MONITORING=0 make cluster-up && make deploy OVERLAY=ci-sim`. It replaces vLLM with a
simulator that has fixed latency and synthetic output. The queue, KEDA and the API behave identically, which is what
CI uses.

## 4. Watch autoscaling

In separate terminals:

```bash
watch -n2 kubectl -n docuflow get scaledobject,hpa
watch -n2 "kubectl -n docuflow exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages_ready messages_unacknowledged"
kubectl -n docuflow get pods -w
```

Then trigger a burst:

```bash
curl -XPOST http://docuflow.localtest.me/ingest -H 'content-type: application/json' \
     -d '{"dataset":"synthetic","limit":500,"offset":20000}'
```

You should see `docuflow.vlm` jump to about 500 tokens. `worker-vlm` goes 0 → 1 within ~5 s (KEDA activation), then
climbs 3 pods per 10 s to its maximum (HPA scale-up policy). `model-vlm` adds a replica about a minute later
(Prometheus trigger, 30 s stabilisation, ~60 s model load). When the queue drains, workers step down one per 20 s and
reach zero 60 s after the last job.

## 5. Stress test

```bash
make stress PAGES=800 C=12                         # run 1 equivalent
.venv/bin/python scripts/stress.py --pages 2000 --offset 2000 \
    --query-steps 8,24,48 --step-seconds 300 --observe-after 420     # ramp + scale-down
```

Reports land in `docs/`. For before/after comparisons, change one thing at a time and don't `kubectl apply` during a
run (Part 7, failure 10).

## 6. Evaluate answer quality

```bash
make ragas N=40      # faithfulness, answer relevancy, context precision/recall, judged by the cluster's own models
```

## 7. Choose what the GPU is for

```bash
make ingest-mode     # pause queries, stop the agent model, page reading gets the whole GPU (~170 pages/min here)
make query-mode      # agent model back, queries open
```

## Cost and capacity notes

- **Worker pods are close to free.** They're CPU-light HTTP clients, so let KEDA scale them to zero and to large
  maxima. Size their maximum against the model servers' batch slots, not against cluster CPU.
- **GPU replicas are where the money is.** On one GPU, extra vLLM replicas add batching concurrency but no compute.
  The second page reader helped mainly because the first one's batch was full, not because the GPU had spare FLOPs.
- **Before buying another GPU,** check four things:
  1. Are batch slots full while the KV cache is not? Raise `--max-num-seqs`.
  2. Is KV cache reserved but unused? Shrink it and give the memory to the bottleneck.
  3. Can ingest and queries take turns (ingest mode overnight, query mode by day)?
  4. Are you scaling the tier that's actually queueing?
- **When you do add GPUs,** dedicate them by role. Put page reading on one GPU and the agent plus embeddings on
  another, so ingest bursts can't touch query latency. The `prod` overlay does this with whole GPUs per model server
  and KEDA on each server's queue.

## Repository map

| Path | Contents |
|---|---|
| `scripts/cluster-up.sh` | cluster + GPU + platform bootstrap |
| `k8s/base/` | namespace, priority classes, Postgres, RabbitMQ, API, workers, KEDA ScaledObjects, NetworkPolicies |
| `k8s/components/models/` | vLLM Deployments, model-cache volume, KEDA for vLLM |
| `k8s/components/monitoring/` | PodMonitors, alert rules, Grafana dashboard |
| `k8s/overlays/` | `local-gpu`, `ci-sim`, `prod` |
| `docuflow/broker.py` | RabbitMQ tokens, delayed retries, reconciliation |
| `docuflow/jobs.py` | Postgres job queue and claim-time workload rules |
| `scripts/stress.py`, `evals/ragas_eval.py` | load generator, RAGAS evaluation |
