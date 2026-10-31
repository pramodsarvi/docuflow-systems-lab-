"""Agentic RAG. A small LLM loop that decides what to do next: search (with filters), rewrite and search again,
read a whole page, look at the page image with the vision model, or answer with citations.

Every step is returned in `trace`, so a wrong answer can be diagnosed from what the agent saw and did."""
import time
import uuid

from . import config, db, extract, llm, retrieval

ACTIONS = ["search", "get_page", "read_page_image", "corpus_stats", "answer"]
SCHEMA = {"type": "object", "required": ["thought", "action"], "properties": {
    "thought": {"type": "string"}, "action": {"type": "string", "enum": ACTIONS},
    "query": {"type": "string"}, "dataset": {"type": "string"}, "kind": {"type": "string"},
    "doc_id": {"type": "string"}, "answer": {"type": "string"},
    "citations": {"type": "array", "items": {"type": "string"}}}}

SYSTEM = """You answer questions about a collection of scanned documents (forms, letters, memos, handwritten notes).
Text was extracted by OCR and every line is tagged (printed) or (handwritten). The collection is still being
ingested, so some pages may not be indexed yet.

On each turn reply with ONE JSON action:
- search: {action, query, dataset?, kind?}  hybrid search. dataset is one of funsd, xfund, docvqa, iam; kind is printed or handwritten.
- get_page: {action, doc_id}  full extracted text of one page.
- read_page_image: {action, doc_id, query}  have the vision model look at the page image and answer `query`. Use it when the
  extracted text is garbled, incomplete or ambiguous (especially handwriting).
- corpus_stats: {action}  what is indexed and what is still pending.
- answer: {action, answer, citations}  final answer; citations are doc_ids you actually saw.

Never decide that something is absent from the corpus status alone: always run at least one search first (the documents
can be about anything, including songs, letters, prices or names).
Always put your reasoning in "thought": say whether the evidence so far is sufficient. If a search returns nothing useful,
rewrite the query (different keywords, drop filters) before giving up. Only answer from evidence; if the evidence is not
there, say so (and mention if ingestion may still be running). Keep answers short and exact."""


def _fmt_hits(hits):
    if not hits:
        return "No results."
    return "\n".join(f"[{h['doc_id']}] ({h['dataset']}, {h['lang']}, {'+'.join(h['kinds'])}) {h['text'][:500]}" for h in hits)


def run(question, dataset=None, max_steps=None, lang=None):
    max_steps = max_steps or config.AGENT_MAX_STEPS
    qid, t0, trace, seen = str(uuid.uuid4()), time.perf_counter(), [], set()
    stats = retrieval.corpus_stats()
    with db.pool().connection() as c:
        c.execute("INSERT INTO inflight (id) VALUES (%s)", (qid,))
        ingesting = c.execute("SELECT count(*) n FROM jobs WHERE status IN ('queued','running')").fetchone()["n"] > 0
    hint = f"\nRestrict searches to dataset '{dataset}'." if dataset else ""
    msgs = [{"role": "system", "content": SYSTEM + hint},
            {"role": "user", "content": f"Corpus status: {stats}\n\nQuestion: {question}"}]
    answer, cites = None, []
    try:
        for step in range(max_steps):
            with db.pool().connection() as c:
                c.execute("UPDATE inflight SET beat=now() WHERE id=%s", (qid,))
            last = step == max_steps - 1
            if last:
                msgs.append({"role": "user", "content": "Step budget exhausted: give your final answer now (action=answer)."})
            raw, _ = llm.chat(msgs, fmt=SCHEMA, max_tokens=500)
            try:
                a = llm.parse_json(raw)
            except Exception:
                msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": "Invalid JSON. Reply with one valid action."}]
                trace.append({"step": step, "error": "invalid json", "raw": raw[:200]})
                continue
            act = a.get("action")
            entry = {"step": step, "thought": a.get("thought", ""), "action": act}
            if act == "answer" and not seen and not last:
                # Small models like to give up early; make them look at the evidence first.
                msgs += [{"role": "assistant", "content": raw},
                         {"role": "user", "content": "You have not searched yet. Run a search with keywords from the question first."}]
                trace.append({**entry, "rejected": "answered before searching"})
                continue
            if act == "answer" or last and act not in ACTIONS:
                answer = a.get("answer") or "I could not find this in the indexed documents."
                cites = [d for d in a.get("citations", []) if d in seen]
                entry.update(answer=answer, citations=cites)
                trace.append(entry)
                break
            if act == "search":
                ds = a.get("dataset") or dataset
                hits = retrieval.search(a.get("query") or question, k=5, dataset=ds if ds in config.DATASETS else None,
                                        kind=a.get("kind") if a.get("kind") in ("printed", "handwritten") else None, lang=lang)
                seen.update(h["doc_id"] for h in hits)
                obs = _fmt_hits(hits)
                entry.update(query=a.get("query"), dataset=ds, kind=a.get("kind"), doc_ids=[h["doc_id"] for h in hits])
            elif act == "get_page":
                obs = retrieval.page_text(a.get("doc_id", "")) or "Unknown or not-yet-indexed doc_id."
                seen.add(a.get("doc_id", ""))
                entry["doc_id"] = a.get("doc_id")
            elif act == "read_page_image":
                with db.pool().connection() as c:
                    row = c.execute("SELECT image FROM documents WHERE id=%s", (a.get("doc_id", ""),)).fetchone()
                obs = extract.answer_from_image(bytes(row["image"]), a.get("query") or question) if row else "Unknown doc_id."
                seen.add(a.get("doc_id", ""))
                entry.update(doc_id=a.get("doc_id"), query=a.get("query"))
            elif act == "corpus_stats":
                obs = str(retrieval.corpus_stats())
            else:
                obs = f"Unknown action. Use one of {ACTIONS}."
            entry["observation"] = obs[:1500]
            trace.append(entry)
            msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": f"Observation:\n{obs[:3500]}"}]
        if answer is None:
            answer = "I could not find this in the indexed documents."
    finally:
        with db.pool().connection() as c:
            c.execute("DELETE FROM inflight WHERE id=%s", (qid,))
    secs = time.perf_counter() - t0
    with db.pool().connection() as c:
        c.execute("INSERT INTO queries (question,answer,trace,seconds,during_ingest) VALUES (%s,%s,%s,%s,%s)",
                  (question, answer, db.jb(trace), secs, ingesting))
    return {"answer": answer, "citations": cites, "steps": len(trace), "seconds": round(secs, 2),
            "during_ingest": ingesting, "trace": trace}
