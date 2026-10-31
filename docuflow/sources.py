"""Dataset adapters. Each yields dicts: id, dataset, split, lang, image (PIL), gt_text, gt_qa, meta."""
import io
import itertools
import random

from PIL import Image, ImageDraw, ImageFont

from . import config

XFUND_LANG = {"de": "de", "es": "es", "fr": "fr", "it": "it", "ja": "ja", "pt": "pt", "zh": "zh"}


def _words_to_text(words, bboxes):
    """Group tokens into lines by vertical position so ground truth reads like a page."""
    items = sorted(zip(bboxes, words, strict=False), key=lambda p: (p[0][1], p[0][0]))
    lines, cur, cur_y = [], [], None
    for (_x0, y0, _x1, y1), w in items:
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
    if name == "synthetic":
        return itertools.islice(_synthetic(split or spec["split"]), offset, offset + limit)
    from datasets import load_dataset   # heavy import, only needed for the Hub datasets
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


FIRST = ["Maria", "John", "Aiko", "Pierre", "Fatima", "Lars", "Priya", "Carlos", "Mei", "Olu"]
LAST = ["Lopez", "Smith", "Tanaka", "Dubois", "Khan", "Berg", "Sharma", "Garcia", "Chen", "Adeyemi"]
CITY = ["Oslo", "Lagos", "Pune", "Lyon", "Osaka", "Austin", "Quito", "Bergen", "Delhi", "Porto"]
KINDS = ["Invoice", "Purchase order", "Fax cover sheet", "Expense claim", "Delivery note"]


def synthetic_page(i):
    """One deterministic form page: (PIL image, gt_text). Printed labels in a regular font, values in a larger
    slanted-looking font so the page mixes two visual styles."""
    r = random.Random(i)
    kind, name = KINDS[i % len(KINDS)], f"{r.choice(FIRST)} {r.choice(LAST)}"
    fields = [("Number", f"{kind[:3].upper()}-{1000 + i}"), ("Name", name), ("City", r.choice(CITY)),
              ("Amount", f"{r.randint(50, 9000)} dollars"), ("Date", f"2026-{r.randint(1, 12):02d}-{r.randint(1, 28):02d}"),
              ("Signed by", f"{r.choice(FIRST)} {r.choice(LAST)}")]
    img = Image.new("RGB", (900, 620), "white")
    d = ImageDraw.Draw(img)
    printed, hand = ImageFont.load_default(size=26), ImageFont.load_default(size=30)
    d.text((40, 30), f"{kind.upper()} {i}", fill="black", font=ImageFont.load_default(size=36))
    for row, (label, value) in enumerate(fields):
        y = 110 + row * 80
        d.text((40, y), f"{label}:", fill="black", font=printed)
        d.text((260, y - 4), value, fill=(20, 40, 150), font=hand)
        d.line((250, y + 34, 860, y + 34), fill="gray")
    gt = [f"{kind.upper()} {i}"] + [f"{label}: {value}" for label, value in fields]
    return img, "\n".join(gt)


def _synthetic(split):
    for i in itertools.count():
        img, gt = synthetic_page(i)
        yield dict(id=f"synthetic/{split}/{i}", dataset="synthetic", split=split, lang="en", image=img,
                   gt_text=gt, gt_qa=None, meta={"generated": True})


def encode_image(img):
    """Downscale and JPEG-encode; returns (bytes, width, height)."""
    img = img.convert("RGB")
    s = config.MAX_IMAGE_SIDE / max(img.size)
    if s < 1:
        img = img.resize((round(img.width * s), round(img.height * s)))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=85)
    return buf.getvalue(), img.width, img.height
