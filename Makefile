PY := .venv/bin/python

setup:            ## venv + deps
	uv venv --python 3.12 .venv && uv pip install --python $(PY) -r requirements.txt

db:               ## pgvector on :5434 (plus the test database)
	docker compose up -d --wait
	-docker exec docuflow-db psql -U docuflow -c "CREATE DATABASE docuflow_test" 2>/dev/null

models:           ## local models used by default
	ollama pull gemma4:e2b-mlx && ollama pull qwen3:4b-instruct && ollama pull qwen3-embedding:0.6b

up:               ## api (:8791) + worker pools; Ctrl-C to stop
	PROCS_VLM=1 $(PY) -m docuflow up

ingest-all:       ## all four datasets at once (N pages each)
	$(PY) -m docuflow ingest iam -n $(or $(N),12) && $(PY) -m docuflow ingest funsd -n $(or $(N),6) && \
	$(PY) -m docuflow ingest xfund -n $(or $(N),6) && $(PY) -m docuflow ingest docvqa -n $(or $(N),8)

status:
	$(PY) -m docuflow status

ask:              ## make ask Q="who is the fax addressed to?"
	$(PY) -m docuflow ask "$(Q)" --trace

eval:
	$(PY) evals/run_eval.py --qa $(or $(QA),12)

test:
	$(PY) -m pytest -q tests

.PHONY: setup db models up ingest-all status ask eval test

# ---- Kubernetes (existing kind cluster `llmd`, fake GPUs, KEDA) ----
k8s-build:        ## build the image and load it (plus pgvector) into the kind nodes
	docker build -t docuflow:dev .
	# single-platform archives: `kind load docker-image` fails on multi-platform images from Docker's containerd store
	for i in docuflow:dev pgvector/pgvector:pg16; do f=$$(mktemp -t img).tar; docker save --platform linux/arm64 -o $$f $$i && kind load image-archive $$f --name llmd; rm -f $$f; done

k8s-up: k8s-build ## deploy db, api, sim model, KEDA-scaled workers
	kubectl apply -f k8s/base/namespace.yaml && kubectl apply -k k8s/base
	kubectl -n docuflow rollout status sts/db deploy/api deploy/sim-model --timeout=300s

k8s-key:          ## protect the API: creates the secret and restarts the api (export DOCUFLOW_API_KEY=<same value> for the CLI)
	@test -n "$$KEY" || (echo 'usage: make k8s-key KEY=<secret>'; exit 1)
	kubectl -n docuflow create secret generic docuflow-api-key --from-literal=key=$$KEY --dry-run=client -o yaml | kubectl apply -f -
	kubectl -n docuflow rollout restart deploy/api

k8s-ui:           ## API on http://127.0.0.1:8792 (CLI: DOCUFLOW_API=http://127.0.0.1:8792 python -m docuflow status)
	kubectl -n docuflow port-forward svc/api 8792:8791

k8s-status:
	@kubectl -n docuflow get pods,scaledobject,hpa -o wide; kubectl get nodes -o custom-columns=NODE:.metadata.name,GPU:.status.allocatable.nvidia\\.com/gpu

k8s-demo:         ## burst of ingest work, then watch KEDA scale the vlm workers
	DOCUFLOW_API=http://127.0.0.1:8792 sh k8s/demo.sh

k8s-real:         ## use real Ollama on the Mac host instead of the simulator
	kubectl -n docuflow patch cm docuflow-config --type merge -p '{"data":{"OLLAMA_URL":"http://host.docker.internal:11434"}}'
	kubectl -n docuflow rollout restart deploy/api deploy/worker-io deploy/worker-embed deploy/worker-vlm

k8s-down:
	kubectl delete namespace docuflow

.PHONY: k8s-key k8s-build k8s-up k8s-ui k8s-status k8s-demo k8s-real k8s-down

# ---- Production on the AKS GPU cluster (see README "Production on GPUs") ----
# Prereqs: `make gpu-up` from the repo root (AKS, GPU operator, vLLM models, LiteLLM), a registry (ACR), a LiteLLM virtual key.
gpu-docuflow-image:   ## ACR=<name>  build linux/amd64 (AKS GPU nodes) and push
	@test -n "$(ACR)" || (echo 'usage: make gpu-docuflow-image ACR=<registry>'; exit 1)
	docker buildx build --platform linux/amd64 -t $(ACR).azurecr.io/docuflow:prod --push .

gpu-docuflow-secrets: ## needs DOCUFLOW_DSN, LLM_API_KEY (LiteLLM virtual key), DOCUFLOW_API_KEY in the environment
	@for v in DOCUFLOW_DSN LLM_API_KEY DOCUFLOW_API_KEY; do eval "test -n \"\$$$$v\"" || (echo "set $$v"; exit 1); done
	kubectl apply -f k8s/base/namespace.yaml
	kubectl -n docuflow create secret generic docuflow-secrets --from-literal=DOCUFLOW_DSN="$$DOCUFLOW_DSN" \
	  --from-literal=LLM_API_KEY="$$LLM_API_KEY" --from-literal=DOCUFLOW_API_KEY="$$DOCUFLOW_API_KEY" --dry-run=client -o yaml | kubectl apply -f -

gpu-docuflow-up:      ## ACR=<name>
	@test -n "$(ACR)" || (echo 'usage: make gpu-docuflow-up ACR=<registry>'; exit 1)
	kubectl kustomize k8s/overlays/gpu | sed 's/ACR_NAME/$(ACR)/' | kubectl apply -f -
	kubectl -n docuflow rollout status deploy/api --timeout=300s

.PHONY: gpu-docuflow-image gpu-docuflow-secrets gpu-docuflow-up
