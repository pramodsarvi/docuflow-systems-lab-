"""Page reading with a vision model: transcribe every region and label it printed vs handwritten."""
import time

from . import config, llm

PROMPT = """You are reading a scanned document page that may mix printed (typed) text and handwriting.
Transcribe ALL visible text in reading order, one region per printed label, field value, line or note.
For each region give:
  "text": the exact transcription (keep the original language, do not translate or fix spelling),
  "kind": "printed" or "handwritten" (use "handwritten" for pen/pencil writing, signatures and scribbles),
  "role": one of "header", "label", "value", "body", "note", "signature", "table".
Return only JSON: {"regions":[{"text":"...","kind":"printed","role":"label"}]}"""

SCHEMA = {"type": "object", "required": ["regions"], "properties": {"regions": {"type": "array", "items": {
    "type": "object", "required": ["text", "kind"], "properties": {
        "text": {"type": "string"}, "kind": {"type": "string", "enum": ["printed", "handwritten"]},
        "role": {"type": "string"}}}}}}


def read_page(image_bytes, hint_lang=None):
    """Returns (regions, seconds). Raises ValueError if the model returns nothing usable."""
    t0 = time.perf_counter()
    prompt = PROMPT + (f"\nThe page language is probably '{hint_lang}'." if hint_lang and hint_lang != "en" else "")
    text, _ = llm.chat([{"role": "user", "content": prompt}], model=config.VLM_MODEL, role="vlm", images=[image_bytes],
                       fmt=SCHEMA, max_tokens=3000, num_ctx=12288)
    try:
        regions = llm.parse_json(text).get("regions", [])
    except Exception as e:
        raise ValueError(f"unparseable VLM output: {e}: {text[:200]}")
    regions = [{"text": r["text"].strip(), "kind": r.get("kind", "printed"), "role": r.get("role", "body")}
               for r in regions if isinstance(r, dict) and str(r.get("text", "")).strip()]
    if not regions:
        raise ValueError("VLM returned no regions")
    return regions, time.perf_counter() - t0


def answer_from_image(image_bytes, question):
    """Targeted re-read of one page; used by the agent when retrieved text is unclear or missing."""
    text, _ = llm.chat([{"role": "user", "content": f"Look carefully at this document page (printed text and handwriting) "
                         f"and answer the question using only what is written on it. Be concise; if the page does not "
                         f"contain the answer say 'not on this page'.\nQuestion: {question}"}],
                       model=config.VLM_MODEL, role="vlm", images=[image_bytes], max_tokens=300, num_ctx=8192)
    return text
