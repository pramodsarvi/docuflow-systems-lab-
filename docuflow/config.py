"""Runtime configuration, all overridable by environment variables."""
import os

DSN = os.environ.get("DOCUFLOW_DSN", "postgresql://docuflow:docuflow@localhost:5434/docuflow")
# Model backend. "ollama" talks to one Ollama server; "openai" talks to OpenAI-compatible servers (vLLM on GPU nodes,
# LiteLLM, or Ollama's /v1) with a separate endpoint per role so each model can live on its own GPU deployment.
BACKEND = os.environ.get("LLM_BACKEND", "ollama")
OLLAMA = os.environ.get("OLLAMA_URL", "http://localhost:11434")
_KEY = os.environ.get("LLM_API_KEY", "none")
ENDPOINTS = {   # role -> (base url ending in /v1, bearer token)
    "vlm": (os.environ.get("VLM_BASE_URL", "").rstrip("/"), os.environ.get("VLM_API_KEY", _KEY)),
    "llm": (os.environ.get("LLM_BASE_URL", "").rstrip("/"), os.environ.get("LLM_API_KEY", _KEY)),
    "embed": (os.environ.get("EMBED_BASE_URL", "").rstrip("/"), os.environ.get("EMBED_API_KEY", _KEY)),
}
# Work tokens on RabbitMQ (what KEDA scales workers on). Empty = workers poll Postgres directly.
RABBITMQ_URL = os.environ.get("RABBITMQ_URL", "")
HTTP_RETRIES = int(os.environ.get("HTTP_RETRIES", 3))
VLM_MODEL = os.environ.get("VLM_MODEL", "gemma4:e2b-mlx")       # reads page images (printed + handwriting)
LLM_MODEL = os.environ.get("LLM_MODEL", "qwen3:4b-instruct")    # agent / answer model
EMBED_MODEL = os.environ.get("EMBED_MODEL", "qwen3-embedding:0.6b")
EMBED_DIM = 1024

# Worker processes started per lane (upper bound) and the live default for how many may run at once.
LANE_PROCS = {"io": int(os.environ.get("PROCS_IO", 2)), "vlm": int(os.environ.get("PROCS_VLM", 2)),
              "embed": int(os.environ.get("PROCS_EMBED", 1))}
LANE_LIMIT_DEFAULT = {"io": int(os.environ.get("LIMIT_IO", 2)), "vlm": int(os.environ.get("LIMIT_VLM", os.environ.get("VLM_CONCURRENCY", 1))),
                      "embed": int(os.environ.get("LIMIT_EMBED", 1))}   # on Kubernetes the pod count is the real limit, so set these high
PER_DATASET_CAP = int(os.environ.get("PER_DATASET_CAP", 1))     # max running jobs per dataset per lane (vlm)
YIELD_TO_QUERIES = os.environ.get("YIELD_TO_QUERIES", "1") == "1"
LEASE_SECONDS = int(os.environ.get("LEASE_SECONDS", 900))
MAX_ATTEMPTS = int(os.environ.get("MAX_ATTEMPTS", 3))
MAX_IMAGE_SIDE = int(os.environ.get("MAX_IMAGE_SIDE", 1280))
AGENT_MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", 6))
WORKER_METRICS_PORT = int(os.environ.get("WORKER_METRICS_PORT", 9100))   # 0 disables the worker /health + /metrics server
# Retention (`python -m docuflow gc`, run by a CronJob on Kubernetes)
GC_DONE_JOBS_DAYS = int(os.environ.get("GC_DONE_JOBS_DAYS", 7))
GC_QUERIES_DAYS = int(os.environ.get("GC_QUERIES_DAYS", 30))

DATASETS = {
    # name: (hf repo, hf config, split, priority)
    "funsd":  dict(repo="nielsr/funsd-layoutlmv3", config=None, split="test"),
    "xfund":  dict(repo="nnul/xfund-multilingual", config=None, split="train"),
    "docvqa": dict(repo="lmms-lab/DocVQA", config="DocVQA", split="validation"),
    "iam":    dict(repo="Teklia/IAM-line", config=None, split="test"),
    # Generated locally (no download): forms with printed labels and handwriting-style values. Used by the smoke
    # tests, CI and the scaling demo, so the whole system can run offline.
    "synthetic": dict(repo=None, config=None, split="gen"),
}
