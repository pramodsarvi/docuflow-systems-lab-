"""Pipeline + retrieval with the dataset's own transcription (mode=gt), so no vision model is needed.
Embeddings still use the local embedding model; skipped if Ollama is not reachable."""
import io

import pytest
from PIL import Image

from docuflow import chunk, db, jobs, llm, pipeline, retrieval


def _ollama_up():
    try:
        llm.embed(["x"])
        return True
    except Exception:
        return False


def test_chunker_keeps_provenance_and_splits():
    regions = [{"text": "Name:", "kind": "printed", "role": "label"}, {"text": "J. Smith", "kind": "handwritten", "role": "value"}]
    c = chunk.regions_to_chunks(regions)
    assert len(c) == 1 and c[0]["kinds"] == ["handwritten", "printed"]
    assert "(handwritten/value) J. Smith" in c[0]["text"]
    long = [{"text": "x" * 300, "kind": "printed", "role": "body"}] * 5
    assert len(chunk.regions_to_chunks(long)) >= 2


@pytest.mark.skipif(not _ollama_up(), reason="Ollama not running")
def test_gt_ingest_then_hybrid_search_and_filters():
    buf = io.BytesIO(); Image.new("RGB", (10, 10)).save(buf, "JPEG")
    for did, text in [("t/1", "Invoice total 4500 dollars\nSigned by Maria Lopez"), ("t/2", "Quarterly weather report for Oslo")]:
        with db.pool().connection() as c:
            c.execute("INSERT INTO documents (id,dataset,split,lang,image,gt_text) VALUES (%s,'funsd','t','en',%s,%s)", (did, buf.getvalue(), text))
            jobs.enqueue("extract", "funsd", {"doc_id": did, "mode": "gt"}, conn=c)
    db.control_set("lane_limits", {"vlm": 2, "embed": 2})
    for lane in ("vlm", "embed", "vlm", "embed"):
        j = jobs.claim(lane, "t")
        assert j
        pipeline.HANDLERS[j["stage"]](j); jobs.complete(j["id"])
    with db.pool().connection() as c:
        assert c.execute("SELECT count(*) n FROM documents WHERE status='indexed'").fetchone()["n"] == 2
    hits = retrieval.search("who signed the invoice", k=2)
    assert hits[0]["doc_id"] == "t/1"
    assert retrieval.search("weather", dataset="xfund") == []
