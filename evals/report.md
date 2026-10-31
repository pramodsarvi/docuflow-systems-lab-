# docuflow eval report

## Page reading (vision model vs dataset ground truth)

| dataset | pages | CER | token F1 | s/page | handwritten share | GT fallbacks |
|---|---|---|---|---|---|---|
| funsd | 6 | 0.17 | 0.87 | 33.3 | 2% | 0 |
| iam | 12 | 0.02 | 0.91 | 3.7 | 33% | 0 |
| xfund | 6 | 0.37 | 0.86 | 40.3 | 6% | 0 |

## Agentic RAG on DocVQA (answer contains a gold answer)

- questions: 6, accuracy: **33%**, cited the source page: 67%
- mean steps: 2.5

| result | question | gold | predicted |
|---|---|---|---|
| miss | What is the ‘actual’ value per 1000, during the year 1975? | 0.28 | 0.24 |
| miss | What is name of university? | university of california | William Marsh Rice University |
| ok | What is the name of the company? | itc limited | ITC Limited |
| miss | What the location address of NSDA? | 1128 SIXTEENTH ST., N. W., WAS | The location address of NSDA cannot be found in the availabl |
| ok | What is the name of foundation? | The Robert A. Welch Foundation | The name of the foundation is the Robert A. Welch Foundation |
| miss | What time is the ‘coffee break’? | 11:14 to 11:39 a.m. | The coffee break is from 11:14 a.m. to 11:39 a.m. |

## Query latency (seconds)

| during ingest | queries | mean | p50 | max |
|---|---|---|---|---|
| False | 6 | 26.5 | 26.3 | 38.0 |
| True | 3 | 48.5 | 46.9 | 60.5 |
