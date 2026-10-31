# Part 5. GPUs: one L40S, three vLLM servers

## Getting a real GPU into kind

kind runs Kubernetes nodes as Docker containers, so the GPU has to pass through two container layers: host Docker into
the node container, then the node's containerd into the pods.

**On the host,** make the NVIDIA runtime Docker's default, and let it select GPUs through a volume mount (kind can
mount files but can't pass `--gpus`):

```bash
sudo nvidia-ctk runtime configure --runtime=docker --set-as-default
sudo nvidia-ctk config --set accept-nvidia-visible-devices-as-volume-mounts=true --in-place
sudo systemctl restart docker
```

**In the kind config,** mounting `/dev/null` at a magic path asks the toolkit to inject every GPU into that node only:

```yaml
- role: worker
  labels: {docuflow.io/pool: gpu}
  extraMounts:
    - {hostPath: /dev/null, containerPath: /var/run/nvidia-container-devices/all}
    - {hostPath: /var/cache/docuflow-hf, containerPath: /var/cache/docuflow-hf}   # model weights survive rebuilds
```

**Inside the GPU node** (`scripts/cluster-up.sh` does this for you):
1. Install `nvidia-container-toolkit` and make it containerd's default runtime.
2. Unmount the masked `/proc/driver/nvidia/params`. The host toolkit masks it, and without it NVML fails in nested
   containers with `driverInitFileInfo ... result=11`.
3. Restart containerd.
4. Taint the node (`nvidia.com/gpu=present:NoSchedule`) so only GPU workloads land on it.
5. Label it `nvidia.com/gpu.present=true`. The device-plugin chart only schedules onto nodes that Node Feature
   Discovery has labelled.

Two host settings caught us here:
- kind runs several kubelets on one kernel, so the default inotify limits run out. The device plugin then crashes
  with `too many open files`. Raise `fs.inotify.max_user_instances` to 8192.
- Newer vLLM images are built for CUDA 13 and crash at import (`libnvrtc.so.13`) on a 570 driver. `v0.13.0` (CUDA
  12.9) runs on it.

## Sharing one GPU: time-slicing

The NVIDIA device plugin with time-slicing advertises one physical GPU as several `nvidia.com/gpu` units:

```yaml
config:
  map:
    default: |-
      version: v1
      sharing:
        timeSlicing:
          resources: [{name: nvidia.com/gpu, replicas: 6}]
```

The node then shows `nvidia.com/gpu: 6`, and each vLLM pod requests 1. Time-slicing shares *compute* by context
switching, but **memory isn't isolated**: every process sees all 46 GB, and nothing stops one from taking it all.
The budget has to be enforced by the servers themselves.

## The memory budget

vLLM normally sizes itself with `--gpu-memory-utilization` (a fraction of total memory) and works out how much KV
cache fits by profiling at startup. With several servers on one GPU, that profiling goes wrong. When we started all
three at once, the agent model measured the other servers' memory as its own usage and failed:

```
Available KV cache memory: -12.92 GiB
EngineCore failed to start.
```

The fix is to give each server a fixed KV cache with `--kv-cache-memory-bytes`. Then startup no longer depends on
what the other servers are doing at that moment. `--gpu-memory-utilization` remains as an upper bound that vLLM
checks against free memory at startup.

Budgets, as measured and then right-sized:

| Server | Model | Weights | KV cache | Batch slots | Approx. footprint |
|---|---|---|---|---|---|
| model-vlm (x1..2, x3 in ingest mode) | Qwen2.5-VL-3B | 7.5 GB | 3 → **1.5 GiB** (only 12–15% was used) | 16 | ~10 GB each |
| model-llm | Qwen3-4B-Instruct | 7.6 GB | 4 → **8 GiB** (81% used at 32 sequences) | 32 → **64** | ~17 GB |
| model-embed (x1..2) | Qwen3-Embedding-0.6B | 1.2 GB | 1 GiB → **512 MiB** | 64 | ~2.5 GB each |

Measured totals: the first three servers together used **27 GB**. With a second page reader it was **37–38 GB**,
and after the agent model's KV increase **43.7 GB of 46**. That was too full to start a second embedding server,
which is why the page-reader KV cache was cut to what it actually used.

The embedding server needed one more fix. With `--max-model-len 8192` it refused to start with a 512 MiB cache
("0.88 GiB KV cache is needed"). Chunks are at most 700 characters, so `--max-model-len 2048` is plenty.

## Rolling updates deadlock on a shared GPU

A Deployment's default `RollingUpdate` starts the new pod before stopping the old one. On a full GPU, the new pod
can't get memory and fails its startup check:

```
ValueError: Free memory on device (1.2/44.4 GiB) on startup is less than desired GPU memory utilization (0.06, 2.66 GiB)
```

It crash-loops, and the old pod stays up because the new one never becomes ready. On a shared GPU, model servers use
`strategy: Recreate`. That affects only updates (image, flags), not KEDA scaling. The prod overlay, with one whole
GPU per replica, patches it back to `RollingUpdate` with `maxSurge: 1`.

## Workers don't touch the GPU

docuflow's workers request no GPU. A page-reading worker is an HTTP client: it sends a page to `model-vlm` and waits.
This split is what makes queue-based scaling work with GPUs:
- **Worker pods are cheap and fast to start** (about 2 s), so KEDA can scale them from 0 to 48 freely.
- **Model servers are expensive and slow to start** (1–2 minutes, about 10 GB of GPU memory each), so they scale
  conservatively, on their own queue, with long cooldowns.
- **vLLM's continuous batching** turns N concurrent worker requests into one batched GPU pass. More workers means
  more throughput, up to the server's batch slots.

## When one GPU isn't enough

Autoscaling on one GPU only redistributes a fixed budget. At the end of the stress tests the GPU was at 100%
compute, 309 of 350 W, and 43.7 of 46 GB. Throughput beyond that needs more GPUs. The prod overlay gives each model
server whole GPUs, lets KEDA scale `model-vlm` and `model-llm` to 4 replicas each, and lets the GPU node pool's
cluster autoscaler add machines. When another GPU is too expensive, the remaining lever is deciding **what the GPU
is for at a given time**, the ingest mode in Part 6.
