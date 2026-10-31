"""Shared metrics for the evals: character error rate, token F1, normalisation."""
import re


def norm(s):
    return re.sub(r"[^\w\s]", "", (s or "").lower()).split()


def cer(ref, hyp):
    """Character error rate on normalised text (Levenshtein distance / reference length)."""
    a, b = " ".join(norm(ref)), " ".join(norm(hyp))
    if not a:
        return 0.0
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return min(1.0, prev[-1] / len(a))


def token_f1(ref, hyp):
    """Order-independent word overlap: robust to reading-order differences on forms."""
    r, h = norm(ref), norm(hyp)
    if not r or not h:
        return 0.0
    common = sum(min(r.count(w), h.count(w)) for w in set(r))
    if not common:
        return 0.0
    p, rc = common / len(h), common / len(r)
    return 2 * p * rc / (p + rc)


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(p / 100 * (len(xs) - 1))))] if xs else float("nan")
