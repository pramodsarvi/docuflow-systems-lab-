"""Model client: vision chat (page reading), text chat (agent), embeddings.

Two backends behind one interface (see config.BACKEND): Ollama's native API, or OpenAI-compatible servers such as vLLM.
Transient failures (connection errors, 429, 5xx) are retried with backoff; the job queue retries anything beyond that."""
import base64
import json
import os
import re
import time

import httpx
import numpy as np

from . import config

# Model servers sit behind a Kubernetes Service, which balances per *connection*. With keep-alive, every worker stays
# pinned to whichever replica it first reached, so a replica added by autoscaling gets almost no traffic (measured: one
# vLLM replica at 16 running + 6 waiting while the new one had 2). Each call is seconds of GPU work, so a fresh
# connection per request costs nothing noticeable and spreads load over every replica. MODEL_KEEPALIVE=1 restores pooling.
_KEEPALIVE = os.environ.get("MODEL_KEEPALIVE", "0") == "1"
_http = httpx.Client(timeout=httpx.Timeout(900, connect=10),
                     limits=httpx.Limits(max_connections=64, max_keepalive_connections=20 if _KEEPALIVE else 0))
_THINK = re.compile(r"<think>.*?</think>", re.S)
QUERY_INSTRUCT = ("Instruct: Given a question about scanned documents (forms, letters, handwritten notes), "
                  "retrieve passages that answer it\nQuery: ")


def _post(url, body, headers=None):
    for attempt in range(config.HTTP_RETRIES):
        try:
            r = _http.post(url, json=body, headers=headers)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < config.HTTP_RETRIES - 1:
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError):
            if attempt == config.HTTP_RETRIES - 1:
                raise
            time.sleep(2 ** attempt)


def _endpoint(role):
    base, key = config.ENDPOINTS[role]
    if not base:
        raise RuntimeError(f"{role.upper()}_BASE_URL is not set (LLM_BACKEND=openai needs one endpoint per role)")
    return base, {"Authorization": f"Bearer {key}"}


def _model(role, model):
    return model or {"vlm": config.VLM_MODEL, "llm": config.LLM_MODEL, "embed": config.EMBED_MODEL}[role]


def chat(messages, model=None, images=None, fmt=None, max_tokens=1024, temperature=0.0, num_ctx=8192, role="llm"):
    """Returns (text, stats). `images` are raw bytes attached to the last user message. `fmt` is 'json' or a JSON schema."""
    if config.BACKEND == "openai":
        return _chat_openai(messages, model, images, fmt, max_tokens, temperature, role)
    if images:
        messages = [dict(m) for m in messages]
        messages[-1]["images"] = [base64.b64encode(i).decode() for i in images]
    body = {"model": _model(role, model), "messages": messages, "stream": False, "think": False,
            "options": {"temperature": temperature, "num_predict": max_tokens, "num_ctx": num_ctx}}
    if fmt:
        body["format"] = fmt
    j = _post(f"{config.OLLAMA}/api/chat", body)
    text = _THINK.sub("", j["message"]["content"]).strip()
    return text, {"seconds": j.get("total_duration", 0) / 1e9, "output_tokens": j.get("eval_count", 0)}


def _chat_openai(messages, model, images, fmt, max_tokens, temperature, role):
    base, headers = _endpoint(role)
    msgs = [dict(m) for m in messages]
    if images:
        last = msgs[-1]
        parts = [{"type": "text", "text": last["content"]}]
        parts += [{"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(i).decode()}} for i in images]
        last["content"] = parts
    body = {"model": _model(role, model), "messages": msgs, "temperature": temperature, "max_tokens": max_tokens,
            "chat_template_kwargs": {"enable_thinking": False}}
    if isinstance(fmt, dict):
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "schema": fmt}}
    elif fmt:
        body["response_format"] = {"type": "json_object"}
    t0 = time.perf_counter()
    j = _post(f"{base}/chat/completions", body, headers)
    text = _THINK.sub("", j["choices"][0]["message"]["content"] or "").strip()
    return text, {"seconds": time.perf_counter() - t0, "output_tokens": (j.get("usage") or {}).get("completion_tokens", 0)}


def embed(texts, query=False, batch=16):
    out = []
    for i in range(0, len(texts), batch):
        part = texts[i:i + batch]
        if query:
            part = [QUERY_INSTRUCT + t for t in part]
        if config.BACKEND == "openai":
            base, headers = _endpoint("embed")
            j = _post(f"{base}/embeddings", {"model": config.EMBED_MODEL, "input": part}, headers)
            out.extend(d["embedding"] for d in sorted(j["data"], key=lambda d: d["index"]))
        else:
            out.extend(_post(f"{config.OLLAMA}/api/embed", {"model": config.EMBED_MODEL, "input": part})["embeddings"])
    return np.array(out, dtype=np.float32)


def parse_json(text):
    """Best-effort JSON parse: strips code fences and trailing prose."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", t, re.S)
        if m:
            return json.loads(m.group(0))
        raise
