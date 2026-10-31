"""Turn labelled regions into chunks. Each line keeps its printed/handwritten tag so provenance survives retrieval."""

MAX_CHARS = 700


def regions_to_chunks(regions):
    chunks, cur, size = [], [], 0
    for r in regions:
        line = f"({r['kind']}{'/' + r['role'] if r.get('role') else ''}) {r['text']}"
        if cur and size + len(line) > MAX_CHARS:
            chunks.append(cur)
            cur, size = [], 0
        cur.append((line, r["kind"]))
        size += len(line) + 1
    if cur:
        chunks.append(cur)
    return [{"text": "\n".join(l for l, _ in c), "kinds": sorted({k for _, k in c})} for c in chunks]
