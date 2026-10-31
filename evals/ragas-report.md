# docuflow RAGAS evaluation (2026-10-31 21:42)

40 questions through the live agent (`POST /query`, max 4 steps). Judge: `standard-model`, embeddings: `embed-model` (the cluster's own vLLM servers).

| metric | score | scored |
|---|---|---|
| faithfulness | 0.975 | 40/40 |
| answer relevancy | 0.808 | 40/40 |
| context precision | 0.975 | 40/40 |
| context recall | 0.975 | 40/40 |
| exact match (reference value in answer) | 0.975 | 40 |
| cited the source page | 0.975 | 40 |
| mean agent steps / latency | 2.2 / 3.5s | |

## Per question

| ok | question | reference | answer | faith | relev | c-prec | c-rec |
|---|---|---|---|---|---|---|---|
| ✓ | Which city is listed on PUR-1331? | Lyon | The city listed on PUR-1331 is Lyon. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document DEL-1154? | Maria Garcia | Maria Garcia signed document DEL-1154. | 1.00 | 0.55 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1404? | Oslo | The city listed on DEL-1404 is Oslo. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | What is the amount on PUR-1666? | 4694 | The amount on PUR-1666 is 4694 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1049? | Quito | The city listed on DEL-1049 is Quito. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1074? | Lagos | The city listed on DEL-1074 is Lagos. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document EXP-1548? | Priya Chen | Priya Chen signed document EXP-1548. | 1.00 | 0.56 | 1.00 | 1.00 |
| ✓ | Who signed document PUR-1096? | Lars Lopez | Lars Lopez signed document PUR-1096. | 1.00 | 0.56 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1374? | Delhi | Delhi | 1.00 | 0.45 | 1.00 | 1.00 |
| ✓ | Which city is listed on PUR-1596? | Pune | The city listed on PUR-1596 is Pune. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1059? | Bergen | The city listed on DEL-1059 is Bergen. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document DEL-1519? | Fatima Chen | Fatima Chen signed document DEL-1519. | 1.00 | 0.51 | 1.00 | 1.00 |
| ✓ | What is the amount on DEL-1219? | 5435 | The amount on DEL-1219 is 5435 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Who signed document EXP-1038? | Carlos Berg | Carlos Berg signed document EXP-1038. | 1.00 | 0.57 | 1.00 | 1.00 |
| ✓ | Which city is listed on EXP-1088? | Austin | The city listed on EXP-1088 is Austin. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1444? | Oslo | The city listed on DEL-1444 is Oslo. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document EXP-1428? | Fatima Khan | Fatima Khan signed document EXP-1428. | 1.00 | 0.54 | 1.00 | 1.00 |
| ✓ | Which city is listed on PUR-1071? | Porto | The city listed on PUR-1071 is Porto. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document PUR-1246? | Carlos Tanaka | Carlos Tanaka signed document PUR-1246. | 1.00 | 0.57 | 1.00 | 1.00 |
| ✓ | Which city is listed on FAX-1092? | Porto | The city listed on FAX-1092 is Porto. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document DEL-1564? | Lars Khan | Lars Khan signed document DEL-1564. | 1.00 | 0.55 | 1.00 | 1.00 |
| ✓ | What is the amount on DEL-1434? | 3270 | The amount on DEL-1434 is 3270 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Which city is listed on INV-1060? | Porto | The city listed on INV-1060 is Porto. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1579? | Bergen | The city listed on DEL-1579 is Bergen. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | What is the amount on PUR-1126? | 7526 | The amount on PUR-1126 is 7526 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | What is the amount on EXP-1228? | 3635 | The amount on EXP-1228 is 3635 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | What is the amount on INV-1645? | 6888 | The amount on INV-1645 is 6888 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Which city is listed on FAX-1642? | Delhi | Delhi | 1.00 | 0.40 | 1.00 | 1.00 |
| ✓ | What is the amount on EXP-1063? | 4194 | The amount on EXP-1063 is 4194 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | What is the amount on INV-1590? | 8040 | The amount on INV-1590 is 8040 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | What is the amount on DEL-1599? | 7145 | The amount on DEL-1599 is 7145 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Who signed document PUR-1406? | Aiko Lopez | Aiko Lopez signed document PUR-1406. | 1.00 | 0.57 | 1.00 | 1.00 |
| ✓ | Who signed document INV-1050? | Lars Smith | Lars Smith signed document INV-1050. | 1.00 | 0.54 | 1.00 | 1.00 |
| ✓ | Which city is listed on PUR-1226? | Delhi | The city listed on PUR-1226 is Delhi. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | Who signed document FAX-1047? | Fatima Chen | Fatima Chen signed document FAX-1047. | 1.00 | 0.61 | 1.00 | 1.00 |
| ✓ | Who signed document INV-1570? | Fatima Berg | Fatima Berg signed document INV-1570. | 1.00 | 0.50 | 1.00 | 1.00 |
| ✓ | Which city is listed on PUR-1136? | Delhi | The city listed on PUR-1136 is Delhi. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✓ | What is the amount on PUR-1296? | 8267 | The amount on PUR-1296 is 8267 dollars. | 1.00 | 1.00 | 1.00 | 1.00 |
| ✓ | Which city is listed on DEL-1429? | Oslo | The city listed on DEL-1429 is Oslo. | 1.00 | 0.99 | 1.00 | 1.00 |
| ✗ | What is the amount on FAX-1147? | 7250 | The amount on FAX-1147 is not available in the corpus. | 0.00 | 0.00 | 0.00 | 0.00 |
