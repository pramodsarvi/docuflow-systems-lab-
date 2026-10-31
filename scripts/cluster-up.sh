#!/usr/bin/env bash
# Create the local docuflow cluster: kind + real NVIDIA GPU passthrough + device plugin (time-slicing) + KEDA +
# metrics-server + ingress-nginx + kube-prometheus-stack (+ DCGM exporter). Idempotent: re-running upgrades the add-ons.
#
#   scripts/cluster-up.sh                 everything
#   GPU=0 scripts/cluster-up.sh           no GPU node setup (CI / laptops; use the ci-sim overlay)
#   MONITORING=0 scripts/cluster-up.sh    skip Prometheus/Grafana
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=${CLUSTER:-docuflow}
GPU=${GPU:-1}
MONITORING=${MONITORING:-1}
KEDA_VERSION=${KEDA_VERSION:-2.15.1}
METRICS_SERVER_VERSION=${METRICS_SERVER_VERSION:-3.12.2}
INGRESS_NGINX_VERSION=${INGRESS_NGINX_VERSION:-controller-v1.11.3}
KPS_VERSION=${KPS_VERSION:-65.5.0}
DEVICE_PLUGIN_VERSION=${DEVICE_PLUGIN_VERSION:-0.17.0}
DCGM_VERSION=${DCGM_VERSION:-3.6.1}
NODE_IMAGE=${NODE_IMAGE:-kindest/node:v1.31.0}

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
need() { command -v "$1" >/dev/null || { echo "missing: $1"; exit 1; }; }
for t in docker kind kubectl helm; do need "$t"; done

# kind runs many kubelets/controllers on one kernel: the default inotify limits make the device plugin and others fail
# with "too many open files".
if [ "$(sysctl -n fs.inotify.max_user_instances)" -lt 8192 ]; then
  log "raising inotify limits"
  sudo_() { if [ "$(id -u)" = 0 ]; then "$@"; else sudo "$@"; fi; }
  sudo_ sysctl -w fs.inotify.max_user_instances=8192 fs.inotify.max_user_watches=1048576 >/dev/null
fi

if [ "$GPU" = 1 ]; then
  log "checking the host GPU runtime"
  need nvidia-smi
  docker info 2>/dev/null | grep -q 'Default Runtime: nvidia' || {
    echo "Docker's default runtime must be nvidia. Run: sudo nvidia-ctk runtime configure --runtime=docker --set-as-default"
    echo "  && sudo nvidia-ctk config --set accept-nvidia-visible-devices-as-volume-mounts=true --in-place && sudo systemctl restart docker"
    exit 1; }
  grep -q '^accept-nvidia-visible-devices-as-volume-mounts = true' /etc/nvidia-container-runtime/config.toml || {
    echo "set accept-nvidia-visible-devices-as-volume-mounts=true (see above)"; exit 1; }
  mkdir -p /var/cache/docuflow-hf && chmod 777 /var/cache/docuflow-hf
  nvidia-smi -L
fi

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  log "creating kind cluster $CLUSTER"
  cfg=deploy/kind/cluster.yaml
  if [ "$GPU" != 1 ]; then   # drop the GPU mounts for CPU-only hosts
    cfg=$(mktemp); grep -v -e nvidia-container-devices -e docuflow-hf deploy/kind/cluster.yaml | sed 's/{docuflow.io\/pool: gpu}/{docuflow.io\/pool: cpu}/' > "$cfg"
  fi
  kind create cluster --name "$CLUSTER" --config "$cfg" --image "$NODE_IMAGE" --wait 180s
fi
kubectl config use-context "kind-$CLUSTER" >/dev/null

if [ "$GPU" = 1 ]; then
  log "installing the NVIDIA container runtime inside the GPU node"
  for node in $(kubectl get nodes -l docuflow.io/pool=gpu -o name | sed 's#node/##'); do
    docker exec "$node" bash -c '
      set -e
      if ! command -v nvidia-ctk >/dev/null; then
        apt-get update -qq && apt-get install -y -qq curl gpg >/dev/null
        curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor --yes -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg
        curl -sL https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list \
          | sed "s#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g" \
          > /etc/apt/sources.list.d/nvidia-container-toolkit.list
        apt-get update -qq && apt-get install -y -qq nvidia-container-toolkit >/dev/null
      fi
      nvidia-ctk runtime configure --runtime=containerd --set-as-default --config-source=command >/dev/null 2>&1 \
        || nvidia-ctk runtime configure --runtime=containerd --set-as-default
      # the host toolkit masks /proc/driver/nvidia inside the node; unmask it so nested containers can initialise
      for m in $(mount | awk "/\/proc\/driver\/nvidia/ {print \$3}" | sort -r); do umount "$m" || true; done
      systemctl restart containerd
      nvidia-smi -L'
  done
  kubectl taint nodes -l docuflow.io/pool=gpu nvidia.com/gpu=present:NoSchedule --overwrite
  # the device plugin chart schedules only on nodes Node Feature Discovery marked as having an NVIDIA GPU
  kubectl label nodes -l docuflow.io/pool=gpu nvidia.com/gpu.present=true --overwrite
  kubectl wait --for=condition=Ready nodes --all --timeout=120s
fi

log "helm repositories"
helm repo add kedacore https://kedacore.github.io/charts --force-update >/dev/null
helm repo add metrics-server https://kubernetes-sigs.github.io/metrics-server --force-update >/dev/null
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts --force-update >/dev/null
helm repo add nvdp https://nvidia.github.io/k8s-device-plugin --force-update >/dev/null
helm repo add gpu-helm-charts https://nvidia.github.io/dcgm-exporter/helm-charts --force-update >/dev/null
helm repo update >/dev/null

log "metrics-server"
helm upgrade --install metrics-server metrics-server/metrics-server -n kube-system --version "$METRICS_SERVER_VERSION" \
  --set 'args={--kubelet-insecure-tls}' --wait

log "ingress-nginx"
kubectl apply -f "https://raw.githubusercontent.com/kubernetes/ingress-nginx/${INGRESS_NGINX_VERSION}/deploy/static/provider/kind/deploy.yaml" >/dev/null
kubectl -n ingress-nginx rollout status deploy/ingress-nginx-controller --timeout=180s

if [ "$MONITORING" = 1 ]; then
  log "kube-prometheus-stack (Prometheus Operator, Prometheus, Grafana)"
  helm upgrade --install kube-prometheus-stack prometheus-community/kube-prometheus-stack -n monitoring --create-namespace \
    --version "$KPS_VERSION" -f deploy/helm-values/kube-prometheus-stack.yaml --wait --timeout 10m
fi

log "KEDA"
helm upgrade --install keda kedacore/keda -n keda --create-namespace --version "$KEDA_VERSION" \
  --set prometheus.operator.enabled=true --set prometheus.metricServer.enabled=true \
  $( [ "$MONITORING" = 1 ] && echo "--set prometheus.operator.serviceMonitor.enabled=true --set prometheus.metricServer.serviceMonitor.enabled=true" ) \
  --wait

if [ "$GPU" = 1 ]; then
  log "NVIDIA device plugin (time-slicing)"
  helm upgrade --install nvidia-device-plugin nvdp/nvidia-device-plugin -n nvidia --create-namespace \
    --version "$DEVICE_PLUGIN_VERSION" -f deploy/helm-values/nvidia-device-plugin.yaml --wait
  if [ "$MONITORING" = 1 ]; then
    log "DCGM exporter (GPU metrics)"
    helm upgrade --install dcgm-exporter gpu-helm-charts/dcgm-exporter -n nvidia --version "$DCGM_VERSION" \
      -f deploy/helm-values/dcgm-exporter.yaml --set 'tolerations[0].key=nvidia.com/gpu' --set 'tolerations[0].operator=Exists' \
      --set 'tolerations[0].effect=NoSchedule' --wait --timeout 5m || echo "dcgm-exporter failed to start (GPU metrics only; continuing)"
  fi
  log "waiting for the GPU to be advertised"
  for _ in $(seq 60); do
    n=$(kubectl get nodes -l docuflow.io/pool=gpu -o jsonpath='{.items[*].status.allocatable.nvidia\.com/gpu}')
    [ -n "$n" ] && [ "$n" != 0 ] && break; sleep 3
  done
  kubectl get nodes -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.docuflow\.io/pool,GPU:.status.allocatable.nvidia\.com/gpu'
fi

log "cluster ready"
echo "next: make deploy        (or: make deploy OVERLAY=ci-sim on a CPU-only cluster)"
