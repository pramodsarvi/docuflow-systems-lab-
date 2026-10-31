# docuflow quality eval (2026-10-31 23:41)

Knowledge base: 11,560 of 14,614 fetched pages indexed. No LLM involved in these numbers.

## Extraction (vision-model reading vs dataset ground truth)

| dataset | pages | CER median | CER mean | token F1 | near-perfect (CER<5%) | missed (CER>=90%) | regions labelled handwritten | fallback pages |
|---|---|---|---|---|---|---|---|---|
| iam | 500 | 0.0% | 8.1% | 0.881 | 73% | 4.0% | 67% | 0 |
| funsd | 199 | 25.8% | 27.5% | 0.825 | 4% | 0.0% | 1% | 0 |
| xfund | 495 | 37.0% | 38.0% | 0.774 | 1% | 0.4% | 1% | 5 |
| synthetic | 479 | 0.0% | 0.1% | 0.999 | 100% | 0.0% | 0% | 21 |

## Handwriting (IAM: every line is handwritten)

- read near-perfectly: **73%** of lines; missed entirely: **4.0%**
- handwriting label recall: **67%** of regions labelled `handwritten` (should be ~100%)
- CER median 0.0%, mean 8.1%; token F1 0.881

## Retrieval (search only, corpus-wide)

200 questions naming a synthetic document number, against the whole knowledge base.

| hit@1 | hit@5 | search p50 | search p95 |
|---|---|---|---|
| 99.0% | 100.0% | 90 ms | 108 ms |

CER is measured on lower-cased, punctuation-free text; on forms it also penalises reading order, so token F1 is the better content measure there.
