"""Stand-in for Ollama (the /api/chat and /api/embed endpoints docuflow uses) with configurable latency.

Used on the fake-GPU kind cluster, where there is no real accelerator: it lets the worker pods, queue and KEDA scaling be
exercised at realistic job durations. Outputs are synthetic: the "vision model" cannot read the image, it returns
deterministic placeholder regions derived from a hash of the image bytes, and embeddings are hashed bags of words (so
keyword overlap still produces retrieval hits). Point OLLAMA_URL at a real Ollama to use real models.
"""
import asyncio
import hashlib
import json
import math
import os
import re

from fastapi import FastAPI, Request

VLM_LATENCY = float(os.environ.get("SIM_VLM_SECONDS", 8))
LLM_LATENCY = float(os.environ.get("SIM_LLM_SECONDS", 1.5))
EMBED_LATENCY = float(os.environ.get("SIM_EMBED_SECONDS", 0.3))
DIM = 1024
app = FastAPI()


def _vec(text):
    v = [0.0] * DIM
    for w in re.findall(r"\w{3,}", text.lower()):
        v[int(hashlib.md5(w.encode()).hexdigest(), 16) % DIM] += 1.0
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


def _reply(content):
    return {"message": {"role": "assistant", "content": content}, "total_duration": 10**9, "eval_count": 50, "eval_duration": 10**9}


def _openai_to_native(body):
    """Translate an OpenAI chat body to the native shape the handlers above expect (images lifted out of content parts)."""
    msgs = []
    for m in body["messages"]:
        m = dict(m)
        if isinstance(m["content"], list):
            m["images"] = [p["image_url"]["url"].split(",", 1)[-1] for p in m["content"] if p.get("type") == "image_url"]
            m["content"] = " ".join(p["text"] for p in m["content"] if p.get("type") == "text")
        msgs.append(m)
    rf = (body.get("response_format") or {}).get("json_schema", {}).get("schema", {})
    return {"messages": msgs, "format": rf}


@app.post("/v1/chat/completions")
async def chat_v1(r: Request):
    native = _openai_to_native(await r.json())

    class _R:                       # reuse the native handler
        async def json(self):
            return native
    out = await chat(_R())
    return {"choices": [{"message": out["message"], "finish_reason": "stop"}], "usage": {"completion_tokens": 50}}


@app.post("/v1/embeddings")
async def embeddings_v1(r: Request):
    await asyncio.sleep(EMBED_LATENCY)
    inputs = (await r.json())["input"]
    return {"data": [{"index": i, "embedding": _vec(t)} for i, t in enumerate(inputs)]}


@app.get("/health")
def health():
    return {"ok": True, "sim": True}


@app.post("/api/embed")
async def embed(r: Request):
    await asyncio.sleep(EMBED_LATENCY)
    inputs = (await r.json())["input"]
    return {"embeddings": [_vec(t) for t in inputs]}


@app.post("/api/chat")
async def chat(r: Request):
    body = await r.json()
    msgs = body["messages"]
    if msgs[-1].get("images"):                                   # page reading
        await asyncio.sleep(VLM_LATENCY)
        h = hashlib.md5(msgs[-1]["images"][0].encode()).hexdigest()[:8]
        regions = [{"text": f"SIM FORM {h}", "kind": "printed", "role": "header"},
                   {"text": "Name:", "kind": "printed", "role": "label"},
                   {"text": f"Sample Person {h}", "kind": "handwritten", "role": "value"}]
        if "action" in json.dumps(body.get("format", {})):       # targeted page re-read from the agent
            return _reply(f"Sample Person {h}")
        return _reply(json.dumps({"regions": regions}))
    await asyncio.sleep(LLM_LATENCY)                             # agent step
    question = next((m["content"] for m in msgs if m["role"] == "user"), "")
    if sum(m["role"] == "assistant" for m in msgs) == 0:
        q = question.split("Question:")[-1].strip()
        return _reply(json.dumps({"thought": "sim: search first", "action": "search", "query": q}))
    last = msgs[-1]["content"]
    doc = re.search(r"\[([^\]\s]+/[^\]\s]+)\]", last)
    return _reply(json.dumps({"thought": "sim: evidence found" if doc else "sim: nothing found", "action": "answer",
                              "answer": last.split("\n")[1][:160] if doc and "\n" in last else "Not found (simulated).",
                              "citations": [doc.group(1)] if doc else []}))


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=11434)
