"""Dataset adapters. Each yields dicts: id, dataset, split, lang, image (PIL), gt_text, gt_qa, meta."""
import io
import itertools

from datasets import load_dataset

from . import config

XFUND_LANG = {"de": "de", "es": "es", "fr": "fr", "it": "it", "ja": "ja", "pt": "pt", "zh": "zh"}


def _words_to_text(words, bboxes):
    """Group tokens into lines by vertical position so ground truth reads like a page."""
    items = sorted(zip(bboxes, words), key=lambda p: (p[0][1], p[0][0]))
    lines, cur, cur_y = [], [], None
    for (x0, y0, x1, y1), w in items:
        mid = (y0 + y1) / 2
        if cur_y is not None and abs(mid - cur_y) > (y1 - y0) * 0.6:
            lines.append(" ".join(cur))
            cur = []
        cur.append(w)
        cur_y = mid
    if cur:
        lines.append(" ".join(cur))
    return "\n".join(lines)


def iter_dataset(name, split=None, offset=0, limit=10):
    spec = config.DATASETS[name]
    ds = load_dataset(spec["repo"], spec["config"], split=split or spec["split"], streaming=True)
    gen = {"funsd": _funsd, "xfund": _xfund, "docvqa": _docvqa, "iam": _iam}[name](ds, split or spec["split"])
    return itertools.islice(gen, offset, offset + limit)


def _funsd(ds, split):
    for r in ds:
        yield dict(id=f"funsd/{split}/{r['id']}", dataset="funsd", split=split, lang="en", image=r["image"],
                   gt_text=_words_to_text(r["tokens"], r["bboxes"]), gt_qa=None, meta={})


def _xfund(ds, split):
    for r in ds:
        lang = r["id"].split("_")[0]
        yield dict(id=f"xfund/{r['id']}", dataset="xfund", split=split, lang=XFUND_LANG.get(lang, lang),
                   image=r["image"], gt_text=_words_to_text(r["words"], r["bboxes"]), gt_qa=None, meta={})


def _docvqa(ds, split):
    """DocVQA has one row per question; fold rows of the same page into one document with a QA list."""
    cur, qa = None, []
    for r in ds:
        key = (r["docId"], r["ucsf_document_page_no"])   # docId alone spans several pages of one document
        if cur is not None and key != cur["key"]:
            yield _docvqa_doc(cur, qa, split)
            qa = []
        if cur is None or key != cur["key"]:
            cur = dict(key=key, image=r["image"], ucsf=r["ucsf_document_id"], page=r["ucsf_document_page_no"])
        qa.append({"question": r["question"], "answers": r["answers"], "types": r["question_types"]})
    if cur is not None:
        yield _docvqa_doc(cur, qa, split)


def _docvqa_doc(cur, qa, split):
    return dict(id=f"docvqa/{split}/{cur['key'][0]}_{cur['key'][1]}", dataset="docvqa", split=split, lang="en", image=cur["image"],
                gt_text=None, gt_qa=qa, meta={"ucsf": cur["ucsf"], "page": cur["page"]})


def _iam(ds, split):
    for i, r in enumerate(ds):
        yield dict(id=f"iam/{split}/{i}", dataset="iam", split=split, lang="en", image=r["image"],
                   gt_text=r["text"], gt_qa=None, meta={"note": "line image (handwriting only)"})


def encode_image(img):
    """Downscale and JPEG-encode; returns (bytes, width, height)."""
    img = img.convert("RGB")
    s = config.MAX_IMAGE_SIDE / max(img.size)
    if s < 1:
        img = img.resize((round(img.width * s), round(img.height * s)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), img.width, img.height
