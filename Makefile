# docuflow: run `make help` for the list of targets.
PY      ?= .venv/bin/python
OVERLAY ?= local-gpu
CLUSTER ?= docuflow
IMAGE   ?= docuflow:dev
API     ?= http://docuflow.localtest.me
export PATH := $(HOME)/.local/bin:$(PATH)

help:             ## list targets
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## ' $(MAKEFILE_LIST) | awk 'BEGIN{FS=":.*?## "}{printf "  \033[36m%-18s\033[0m %s\n",$$1,$$2}'

# ---- local development (no Kubernetes) ----
setup:            ## venv + dev dependencies
	python3 -m venv .venv && .venv/bin/pip install -q -r requirements-dev.txt

db:               ## Postgres+pgvector on :5434 and RabbitMQ on :5673 (plus the test database)
	docker compose up -d --wait db rabbitmq
	-docker exec docuflow-db psql -U docuflow -c "CREATE DATABASE docuflow_test" 2>/dev/null

stack:            ## everything in containers (db, rabbitmq, model simulator, api :8791, workers)
	docker compose --profile full up -d --build

models:           ## local Ollama models used by `make up`
	ollama pull gemma4:e2b-mlx && ollama pull qwen3:4b-instruct && ollama pull qwen3-embedding:0.6b

up:               ## api (:8791) + worker pools in one supervisor; Ctrl-C to stop
	PROCS_VLM=1 $(PY) -m docuflow up

lint:             ## ruff
	$(PY) -m ruff check .

test:             ## unit + integration tests (needs `make db`)
	$(PY) -m pytest -q tests

ingest-all:       ## all four Hub datasets (N pages each)
	$(PY) -m docuflow ingest iam -n $(or $(N),12) && $(PY) -m docuflow ingest funsd -n $(or $(N),6) && \
	$(PY) -m docuflow ingest xfund -n $(or $(N),6) && $(PY) -m docuflow ingest docvqa -n $(or $(N),8)

status:           ## queue and progress
	$(PY) -m docuflow status

ask:              ## make ask Q="who is the fax addressed to?"
	$(PY) -m docuflow ask "$(Q)" --trace

eval:             ## extraction + QA eval -> evals/report.md
	$(PY) evals/run_eval.py --qa $(or $(QA),12)

# ---- Kubernetes ----
cluster-up:       ## kind + real NVIDIA GPU + device plugin + KEDA + ingress + Prometheus/Grafana (GPU=0 for CPU-only)
	scripts/cluster-up.sh

cluster-down:     ## delete the kind cluster
	kind delete cluster --name $(CLUSTER)

image:            ## build the app image and load it into kind
	docker build -t $(IMAGE) --build-arg VERSION=$$(git describe --always --dirty 2>/dev/null || echo dev) .
	kind load docker-image $(IMAGE) --name $(CLUSTER)

deploy: image     ## deploy OVERLAY (local-gpu | ci-sim) and wait for it
	kubectl apply -k k8s/overlays/$(OVERLAY)
	kubectl -n docuflow rollout status sts/db sts/rabbitmq deploy/api --timeout=300s
	@if [ "$(OVERLAY)" = local-gpu ]; then echo "waiting for the vLLM servers (first start downloads ~20 GB)"; \
	  kubectl -n docuflow rollout status deploy/model-embed deploy/model-llm deploy/model-vlm --timeout=1800s; fi
	@echo "API: $(API)   Grafana: http://grafana.localtest.me (admin/docuflow)"

render:           ## render every overlay and validate it (kubeconform, incl. KEDA / Prometheus CRDs)
	@for o in local-gpu ci-sim prod; do kustomize build k8s/overlays/$$o | kubeconform -strict -summary \
	  -kubernetes-version 1.31.0 -schema-location default \
	  -schema-location 'https://raw.githubusercontent.com/datreeio/CRDs-catalog/main/{{.Group}}/{{.ResourceKind}}_{{.ResourceAPIVersion}}.json' \
	  || exit 1; done

smoke:            ## end-to-end check against a deployed cluster
	scripts/smoke.sh $(API)

stress:           ## ingest + concurrent queries while sampling autoscaling -> docs/stress-report.md
	$(PY) scripts/stress.py --api $(API) --pages $(or $(PAGES),800) --query-concurrency $(or $(C),12) --out docs/stress-report.md

ragas:            ## RAGAS eval (faithfulness, relevancy, context precision/recall) judged by the cluster's vLLM -> evals/ragas-report.md
	test -x .venv-eval/bin/python || (python3 -m venv .venv-eval && .venv-eval/bin/pip install -q -r requirements-eval.txt)
	kubectl -n docuflow port-forward svc/model-llm 18002:8000 >/dev/null & pf1=$$!; \
	kubectl -n docuflow port-forward svc/model-embed 18000:8000 >/dev/null & pf2=$$!; sleep 3; \
	.venv-eval/bin/python evals/ragas_eval.py --api $(API) -n $(or $(N),40); rc=$$?; kill $$pf1 $$pf2; exit $$rc

ingest-mode:      ## pause queries, stop the agent model, give the whole GPU to page reading
	scripts/mode.sh ingest

query-mode:       ## back to normal: agent model up, queries open
	scripts/mode.sh query

k8s-status:       ## pods, KEDA ScaledObjects, HPAs, GPUs, RabbitMQ queues
	@kubectl -n docuflow get pods,scaledobject,hpa -o wide
	@kubectl get nodes -o custom-columns='NODE:.metadata.name,POOL:.metadata.labels.docuflow\.io/pool,GPU:.status.allocatable.nvidia\.com/gpu'
	@kubectl -n docuflow exec rabbitmq-0 -- rabbitmqctl -q list_queues name messages_ready messages_unacknowledged consumers

grafana:          ## Grafana on http://grafana.localtest.me (admin/docuflow); dashboard "docuflow"
	@echo http://grafana.localtest.me

k8s-key:          ## protect the API: KEY=<secret> (export DOCUFLOW_API_KEY=<same> for the CLI)
	@test -n "$$KEY" || (echo 'usage: make k8s-key KEY=<secret>'; exit 1)
	kubectl -n docuflow patch secret docuflow-secrets --type merge -p "{\"stringData\":{\"DOCUFLOW_API_KEY\":\"$$KEY\"}}"
	kubectl -n docuflow rollout restart deploy/api

undeploy:         ## remove docuflow (keeps the cluster and the model cache)
	kubectl delete -k k8s/overlays/$(OVERLAY) --ignore-not-found

# ---- Production (GPU cluster with GPU Operator, KEDA, kube-prometheus-stack, ingress-nginx, cert-manager) ----
prod-image:       ## REGISTRY=<registry> TAG=<tag>: build linux/amd64 and push
	@test -n "$(REGISTRY)" -a -n "$(TAG)" || (echo 'usage: make prod-image REGISTRY=... TAG=...'; exit 1)
	docker buildx build --platform linux/amd64 --build-arg VERSION=$(TAG) -t $(REGISTRY)/docuflow:$(TAG) --push .

prod-secrets:     ## needs POSTGRES_PASSWORD, RABBITMQ_PASSWORD, DOCUFLOW_API_KEY (and optional HF_TOKEN) in the environment
	@for v in POSTGRES_PASSWORD RABBITMQ_PASSWORD DOCUFLOW_API_KEY; do eval "test -n \"\$$$$v\"" || (echo "set $$v"; exit 1); done
	kubectl create namespace docuflow --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n docuflow create secret generic docuflow-secrets \
	  --from-literal=POSTGRES_PASSWORD="$$POSTGRES_PASSWORD" \
	  --from-literal=DOCUFLOW_DSN="$${DOCUFLOW_DSN:-postgresql://docuflow:$$POSTGRES_PASSWORD@db.docuflow.svc.cluster.local:5432/docuflow}" \
	  --from-literal=RABBITMQ_PASSWORD="$$RABBITMQ_PASSWORD" \
	  --from-literal=RABBITMQ_URL="$${RABBITMQ_URL:-amqp://docuflow:$$RABBITMQ_PASSWORD@rabbitmq.docuflow.svc.cluster.local:5672/%2F}" \
	  --from-literal=DOCUFLOW_API_KEY="$$DOCUFLOW_API_KEY" --from-literal=LLM_API_KEY=none \
	  --from-literal=HF_TOKEN="$${HF_TOKEN:-}" --dry-run=client -o yaml | kubectl apply -f -

prod-deploy:      ## REGISTRY=... TAG=... HOST=docuflow.example.com
	@test -n "$(REGISTRY)" -a -n "$(TAG)" -a -n "$(HOST)" || (echo 'usage: make prod-deploy REGISTRY=... TAG=... HOST=...'; exit 1)
	kustomize build k8s/overlays/prod | sed -e 's#REGISTRY/docuflow:TAG#$(REGISTRY)/docuflow:$(TAG)#' -e 's#\bHOST\b#$(HOST)#g' | kubectl apply -f -
	kubectl -n docuflow rollout status deploy/api --timeout=600s

.PHONY: help setup db stack models up lint test ingest-all status ask eval cluster-up cluster-down image deploy render \
        smoke stress ragas ingest-mode query-mode k8s-status grafana k8s-key undeploy prod-image prod-secrets prod-deploy
