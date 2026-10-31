"""The OpenAI-compatible backend (what vLLM serves) against the simulator, in-process on a local port."""
import threading
import time

import httpx
import pytest
import uvicorn

from docuflow import config, extract, llm, simmodel


@pytest.fixture(scope="module")
def sim():
    simmodel.VLM_LATENCY = simmodel.LLM_LATENCY = simmodel.EMBED_LATENCY = 0
    server = uvicorn.Server(uvicorn.Config(simmodel.app, port=18434, log_level="error"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(50):
        try:
            httpx.get("http://127.0.0.1:18434/health"); break
        except httpx.ConnectError:
            time.sleep(0.1)
    old = (config.BACKEND, dict(config.ENDPOINTS))
    config.BACKEND = "openai"
    config.ENDPOINTS.update({r: ("http://127.0.0.1:18434/v1", "k") for r in ("vlm", "llm", "embed")})
    yield
    config.BACKEND, _ = old[0], config.ENDPOINTS.update(old[1])
    server.should_exit = True


def test_embeddings_and_similarity(sim):
    a, b, c = llm.embed(["invoice total dollars", "invoice total dollars paid", "weather in oslo"])
    assert a.shape == (1024,) and a @ b > a @ c


def test_vision_regions_roundtrip(sim):
    regions, _ = extract.read_page(b"\xff\xd8fakejpeg", "en")
    assert {r["kind"] for r in regions} == {"printed", "handwritten"}


def test_agent_step_json(sim):
    out, _ = llm.chat([{"role": "user", "content": "Corpus status: x\n\nQuestion: who?"}], fmt={"type": "object"})
    assert llm.parse_json(out)["action"] == "search"


def test_missing_endpoint_is_a_clear_error():
    old = config.BACKEND, config.ENDPOINTS["llm"]
    config.BACKEND, config.ENDPOINTS["llm"] = "openai", ("", "x")
    try:
        with pytest.raises(RuntimeError, match="LLM_BASE_URL"):
            llm.chat([{"role": "user", "content": "hi"}])
    finally:
        config.BACKEND, config.ENDPOINTS["llm"] = old
