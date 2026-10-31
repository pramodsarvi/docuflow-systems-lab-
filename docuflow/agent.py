"""Agentic RAG. A small LLM loop that decides what to do next: search (with filters), rewrite and search again,
read a whole page, look at the page image with the vision model, or answer with citations.

Every step is returned in `trace`, so a wrong answer can be diagnosed from what the agent saw and did."""
import time
import uuid

from . import config, db, extract, llm, retrieval, tracing

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
- search: {action, query, dataset?, kind?}  hybrid search. dataset is one of {datasets}; kind is printed or handwritten.
- get_page: {action, doc_id}  full extracted text of one page.
- read_page_image: {action, doc_id, query}  have the vision model look at the page image and answer `query`. Use it when the
  extracted text is garbled, incomplete or ambiguous (especially handwriting).
- corpus_stats: {action}  what is indexed and what is still pending.
- answer: {action, answer, citations}  final answer; citations are doc_ids you actually saw.

Never decide that something is absent from the corpus status alone: always run at least one search first (the documents
can be about anything, including songs, letters, prices or names).
Always put your reasoning in "thought": say whether the evidence so far is sufficient. If a search returns nothing useful,
rewrite the query (different keywords, drop filters) before giving up. Only answer from evidence; if the evidence is not
there, say so (and mention if ingestion may still be running). Answer in one short sentence that restates what was
asked, e.g. "INV-1005 was signed by Maria Lopez." or "The amount on PO-1200 is 450 dollars."
If the first search does not show the exact document or identifier asked about, search again with a different query
(for example just the identifier) before concluding that it is missing."""


def _fmt_hits(hits):
    if not hits:
        return "No results."
    return "\n".join(f"[{h['doc_id']}] ({h['dataset']}, {h['lang']}, {'+'.join(h['kinds'])}) {h['text'][:500]}" for h in hits)


def _search_obs(query, dataset, kind, lang):
    hits = retrieval.search(query, k=5, dataset=dataset if dataset in config.DATASETS else None,
                            kind=kind if kind in ("printed", "handwritten") else None, lang=lang)
    return hits, _fmt_hits(hits)


def run(question, dataset=None, max_steps=None, lang=None):
    max_steps = max_steps or config.AGENT_MAX_STEPS
    qid, t0, trace, seen = str(uuid.uuid4()), time.perf_counter(), [], set()
    with db.pool().connection() as c:
        c.execute("INSERT INTO inflight (id) VALUES (%s)", (qid,))
        ingesting = c.execute("SELECT EXISTS (SELECT 1 FROM jobs WHERE status IN ('queued','running')) b").fetchone()["b"]
    answer, cites, searches, retried, trace_id = None, [], 0, False, None
    with tracing.span("docuflow.agent", "AGENT", gen_ai__agent__name="docuflow-agent", input__value=question,
                      session__id=qid, docuflow__dataset=dataset, docuflow__during_ingest=ingesting) as root:
        trace_id = root.trace_id()
        try:
            stats = retrieval.corpus_stats_cached()
            hint = f"\nRestrict searches to dataset '{dataset}'." if dataset else ""
            msgs = [{"role": "system", "content": SYSTEM.replace("{datasets}", ", ".join(config.DATASETS)) + hint}]
            first = f"Corpus status: {stats}\n\nQuestion: {question}"
            if config.AGENT_PRESEARCH:
                # Retrieve first, then reason: the agent's first action is a search on the question in practically every
                # run, so doing it up front saves one LLM round trip per query (the slowest part under load).
                with tracing.span("tool.search", "TOOL", tool__name="search", input__value=question) as ts:
                    hits, obs = _search_obs(question, dataset, None, lang)
                    ts.set(output__value=obs)
                seen.update(h["doc_id"] for h in hits)
                searches += 1
                trace.append({"step": "pre", "action": "search", "thought": "automatic first search on the question",
                              "query": question, "doc_ids": [h["doc_id"] for h in hits], "observation": obs[:1500]})
                first += f"\n\nI already searched for the question. Observation:\n{obs[:3500]}"
            msgs.append({"role": "user", "content": first})
            for step in range(max_steps):
                with db.pool().connection() as c:
                    c.execute("UPDATE inflight SET beat=now() WHERE id=%s", (qid,))
                last = step == max_steps - 1
                if last:
                    msgs.append({"role": "user", "content": "Step budget exhausted: give your final answer now (action=answer)."})
                with tracing.span(f"llm.step.{step}", "LLM", llm__model_name=config.LLM_MODEL,
                                  input__value=msgs[-1]["content"]) as ls:
                    raw, st = llm.chat(msgs, fmt=SCHEMA, max_tokens=config.AGENT_MAX_TOKENS)
                    ls.set(output__value=raw, llm__token_count__completion=st.get("output_tokens"))
                    try:
                        a = llm.parse_json(raw)
                        ls.set(docuflow__action=a.get("action"), docuflow__thought=a.get("thought"))
                    except Exception:
                        a = None
                if a is None:
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
                if act == "answer" and not retried and searches < 2 and not a.get("citations") and step < max_steps - 2:
                    # "Not found" after a single search is usually a retrieval miss, not a missing document: try once more.
                    retried = True
                    msgs += [{"role": "assistant", "content": raw},
                             {"role": "user", "content": "Before concluding it is missing, search once more with a different query "
                                                         "(e.g. only the document number or the key names from the question)."}]
                    trace.append({**entry, "rejected": "gave up after one search"})
                    continue
                if act == "answer" or last and act not in ACTIONS:
                    answer = a.get("answer") or "I could not find this in the indexed documents."
                    cites = [d for d in a.get("citations", []) if d in seen]
                    entry.update(answer=answer, citations=cites)
                    trace.append(entry)
                    break
                with tracing.span(f"tool.{act}", "TOOL", tool__name=act,
                                  input__value=a.get("query") or a.get("doc_id")) as ts:
                    if act == "search":
                        ds = a.get("dataset") or dataset
                        hits, obs = _search_obs(a.get("query") or question, ds, a.get("kind"), lang)
                        seen.update(h["doc_id"] for h in hits)
                        searches += 1
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
                    ts.set(output__value=obs)
                entry["observation"] = obs[:1500]
                trace.append(entry)
                msgs += [{"role": "assistant", "content": raw}, {"role": "user", "content": f"Observation:\n{obs[:3500]}"}]
            if answer is None:
                answer = "I could not find this in the indexed documents."
        finally:
            with db.pool().connection() as c:
                c.execute("DELETE FROM inflight WHERE id=%s", (qid,))
        root.set(output__value=answer, docuflow__citations=", ".join(cites), docuflow__steps=len(trace))
    secs = time.perf_counter() - t0
    with db.pool().connection() as c:
        c.execute("INSERT INTO queries (question,answer,trace,seconds,during_ingest) VALUES (%s,%s,%s,%s,%s)",
                  (question, answer, db.jb(trace), secs, ingesting))
    return {"answer": answer, "citations": cites, "steps": len(trace), "seconds": round(secs, 2),
            "during_ingest": ingesting, "trace_id": trace_id, "query_id": qid, "trace": trace}
