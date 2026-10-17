# TieOut eval summary: PASS

Provider `mock` / model `mock` / prompt `investigator_v1` / seeds 1 to 25 (25 runs, 2026-09) / generated 2026-10-16 19:00 UTC

| Metric | Result | Threshold |
| --- | ---: | ---: |
| Tie-out rate | 100.00% | 100.00% |
| Match precision | 1.0000 | |
| Match recall | 1.0000 | |
| Match F1 | 1.0000 | 0.98 |
| Classification accuracy | 1.0000 | 0.95 |
| Adjusted balance accuracy | 100.00% | |
| NEEDS_REVIEW rate | 0.00% | |
| LLM calls per run | 85.0 | |
| Est. LLM cost per run (USD) | 0.000000 | |
| Avg run wall time (ms) | 498 | |

## Accuracy by break type

| True type | Items | Correct | Accuracy |
| --- | ---: | ---: | ---: |
| BANK_ERROR | 50 | 50 | 100.00% |
| BANK_FEE_UNRECORDED | 75 | 75 | 100.00% |
| DEPOSIT_IN_TRANSIT | 75 | 75 | 100.00% |
| GL_DUPLICATE | 50 | 50 | 100.00% |
| GL_TRANSPOSITION | 100 | 100 | 100.00% |
| GL_WRONG_AMOUNT | 100 | 100 | 100.00% |
| INTEREST_UNRECORDED | 25 | 25 | 100.00% |
| NSF_RETURN_UNRECORDED | 50 | 50 | 100.00% |
| OUTSTANDING_CHECK | 100 | 100 | 100.00% |

## Confusion matrix (rows = truth, columns = predicted)

| Truth | BANK_ERROR | BANK_FEE_UNRECORDED | DEPOSIT_IN_TRANSIT | GL_DUPLICATE | GL_TRANSPOSITION | GL_WRONG_AMOUNT | INTEREST_UNRECORDED | NSF_RETURN_UNRECORDED | OUTSTANDING_CHECK |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| BANK_ERROR | 50 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| BANK_FEE_UNRECORDED | 0 | 75 | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| DEPOSIT_IN_TRANSIT | 0 | 0 | 75 | 0 | 0 | 0 | 0 | 0 | 0 |
| GL_DUPLICATE | 0 | 0 | 0 | 50 | 0 | 0 | 0 | 0 | 0 |
| GL_TRANSPOSITION | 0 | 0 | 0 | 0 | 100 | 0 | 0 | 0 | 0 |
| GL_WRONG_AMOUNT | 0 | 0 | 0 | 0 | 0 | 100 | 0 | 0 | 0 |
| INTEREST_UNRECORDED | 0 | 0 | 0 | 0 | 0 | 0 | 25 | 0 | 0 |
| NSF_RETURN_UNRECORDED | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 50 | 0 |
| OUTSTANDING_CHECK | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 0 | 100 |
