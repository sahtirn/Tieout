# Bank Reconciliation: TieOut Demo Co. (fictional)

| | |
| --- | --- |
| Account | Operating Cash - Demo Bank x0912 |
| Period | 2026-09 |
| Status | **TIED** |
| Difference | **0.00** |
| Run ID | `c7ce29c9-5b13-4503-9244-e24c81647829` |
| Generated | 2026-10-16 19:00 UTC |

## Reconciliation schedule

| Bank side | Amount |
| --- | ---: |
| Balance per bank statement | 529,552.77 |
| Add: deposits in transit | 47,125.89 |
| Less: outstanding checks | (38,869.47) |
| Add/(less): bank errors | (10.74) |
| **Adjusted bank balance** | **537,798.45** |

| Book side | Amount |
| --- | ---: |
| Balance per general ledger | 539,037.08 |
| Add: unrecorded bank credits | 510.01 |
| Less: unrecorded bank debits | (20,550.32) |
| Add/(less): book errors | 18,801.68 |
| **Adjusted book balance** | **537,798.45** |

Difference: **0.00**. The reconciliation ties.

## Reconciling items

### Deposits in transit (3)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `GL-00391` | 13,647.14 | GL receipt GL-00391 for 13,647.14 is dated 2026-09-29, at period end, and no matching bank deposit exists. It is a deposit in transit that should clear early next month. | GL-00391 | AUTO_ACCEPTED (agent, 0.92) |
| `GL-00399` | 13,764.23 | GL receipt GL-00399 for 13,764.23 is dated 2026-09-29, at period end, and no matching bank deposit exists. It is a deposit in transit that should clear early next month. | GL-00399 | AUTO_ACCEPTED (agent, 0.92) |
| `GL-00421` | 19,714.52 | GL receipt GL-00421 for 19,714.52 is dated 2026-09-30, at period end, and no matching bank deposit exists. It is a deposit in transit that should clear early next month. | GL-00421 | AUTO_ACCEPTED (agent, 0.92) |

### Outstanding checks (4)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `GL-00310` | (5,625.12) | Check 10649 (GL-00310) for 5,625.12 was issued on 2026-09-23 and has not cleared the bank. It is an outstanding check. | GL-00310 | AUTO_ACCEPTED (agent, 0.92) |
| `GL-00312` | (11,470.79) | Check 10648 (GL-00312) for 11,470.79 was issued on 2026-09-23 and has not cleared the bank. It is an outstanding check. | GL-00312 | AUTO_ACCEPTED (agent, 0.92) |
| `GL-00348` | (10,930.53) | Check 10653 (GL-00348) for 10,930.53 was issued on 2026-09-24 and has not cleared the bank. It is an outstanding check. | GL-00348 | AUTO_ACCEPTED (agent, 0.92) |
| `GL-00356` | (10,843.03) | Check 10658 (GL-00356) for 10,843.03 was issued on 2026-09-25 and has not cleared the bank. It is an outstanding check. | GL-00356 | AUTO_ACCEPTED (agent, 0.92) |

### Bank errors (2)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00103` | 1,445.35 | Bank line BNK-00103 for 1,445.35 carries bank adjustment notice ADJN770285. The notice confirms the bank posted this item to our account in error. No GL entry is needed. | BNK-00103 | AUTO_ACCEPTED (agent, 0.93) |
| `BNK-00354` | (1,434.61) | Bank line BNK-00354 for 1,434.61 carries bank adjustment notice ADJN611594. The notice confirms the bank posted this item to our account in error. No GL entry is needed. | BNK-00354 | AUTO_ACCEPTED (agent, 0.93) |

### Interest not yet recorded (1)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00393` | 510.01 | Bank line BNK-00393 is interest of 510.01 credited by the bank. Interest is known only from the statement, so it is not yet in the GL. | BNK-00393 | AUTO_ACCEPTED (agent, 0.95) |

### Bank fees not yet recorded (3)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00191` | (165.24) | Bank line BNK-00191 is a bank fee of 165.24 (ACCOUNT ANALYSIS FEE). Fees are charged by the bank and are not yet recorded in the GL. | BNK-00191 | AUTO_ACCEPTED (agent, 0.95) |
| `BNK-00199` | (198.47) | Bank line BNK-00199 is a bank fee of 198.47 (WIRE TRANSFER FEE). Fees are charged by the bank and are not yet recorded in the GL. | BNK-00199 | AUTO_ACCEPTED (agent, 0.95) |
| `BNK-00342` | (308.97) | Bank line BNK-00342 is a bank fee of 308.97 (MONTHLY SERVICE CHARGE). Fees are charged by the bank and are not yet recorded in the GL. | BNK-00342 | AUTO_ACCEPTED (agent, 0.95) |

### NSF returns not yet recorded (2)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00045` | (11,855.18) | Bank line BNK-00045 is a returned item for non-sufficient funds of 11,855.18. It reverses customer receipt GL-00014 deposited on 2026-09-02. The GL still shows the receipt as collected. | BNK-00045, GL-00014 | AUTO_ACCEPTED (agent, 0.95) |
| `BNK-00301` | (8,022.46) | Bank line BNK-00301 is a returned item for non-sufficient funds of 8,022.46. It reverses customer receipt GL-00268 deposited on 2026-09-18. The GL still shows the receipt as collected. | BNK-00301, GL-00268 | AUTO_ACCEPTED (agent, 0.95) |

### GL duplicate postings (2)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `GL-00214` | (7,082.40) | GL entry GL-00214 for 7,082.40 repeats GL-00162 with the same reference and payee. Only one payment cleared the bank (BNK-00160), so GL-00214 is a duplicate posting. | GL-00214, GL-00162, BNK-00160 | AUTO_ACCEPTED (agent, 0.95) |
| `GL-00270` | (8,294.94) | GL entry GL-00270 for 8,294.94 repeats GL-00227 with the same reference and payee. Only one payment cleared the bank (BNK-00203), so GL-00270 is a duplicate posting. | GL-00270, GL-00227, BNK-00203 | AUTO_ACCEPTED (agent, 0.95) |

### GL transposition errors (2)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00215` `GL-00228` | (3,202.81) / (3,022.81) | Bank cleared BNK-00215 for 3,202.81, but the GL recorded GL-00228 for 3,022.81. The amounts differ by two swapped digits (difference 180.00, divisible by 9) and the payee and identifiers agree. | BNK-00215, GL-00228 | AUTO_ACCEPTED (agent, 0.92) |
| `BNK-00238` `GL-00239` | (5,925.72) / (9,525.72) | Bank cleared BNK-00238 for 5,925.72, but the GL recorded GL-00239 for 9,525.72. The amounts differ by two swapped digits (difference 3600.00, divisible by 9) and the payee and identifiers agree. | BNK-00238, GL-00239 | AUTO_ACCEPTED (agent, 0.92) |

### GL wrong amounts (2)

| Item(s) | Amount(s) | Explanation | Evidence | Status |
| --- | ---: | --- | --- | --- |
| `BNK-00031` `GL-00029` | (6,334.68) / (6,810.73) | Bank cleared BNK-00031 for 6,334.68, and GL entry GL-00029 for the same payee shows an invoice amount of 6,334.68 in its memo but was booked at 6,810.73. The GL amount was keyed incorrectly. | BNK-00031, GL-00029 | AUTO_ACCEPTED (agent, 0.90) |
| `BNK-00211` `GL-00253` | (12,719.85) / (12,248.14) | Bank cleared BNK-00211 for 12,719.85, and GL entry GL-00253 for the same payee shows an invoice amount of 12,719.85 in its memo but was booked at 12,248.14. The GL amount was keyed incorrectly. | BNK-00211, GL-00253 | AUTO_ACCEPTED (agent, 0.90) |

## Proposed adjusting journal entries

**AJE-001** (PENDING_APPROVAL): Correct GL amount to invoice and bank-cleared amount (BNK-00031, GL-00029)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1010 | Cash - Operating Account | 476.05 |  |
| 2000 | Accounts Payable |  | 476.05 |

**AJE-002** (PENDING_APPROVAL): Reinstate receivable for customer check returned NSF (BNK-00045)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1200 | Accounts Receivable | 11,855.18 |  |
| 1010 | Cash - Operating Account |  | 11,855.18 |

**AJE-003** (PENDING_APPROVAL): Record bank service charge per bank statement (BNK-00191)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 6110 | Bank Service Charges | 165.24 |  |
| 1010 | Cash - Operating Account |  | 165.24 |

**AJE-004** (PENDING_APPROVAL): Record bank service charge per bank statement (BNK-00199)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 6110 | Bank Service Charges | 198.47 |  |
| 1010 | Cash - Operating Account |  | 198.47 |

**AJE-005** (PENDING_APPROVAL): Correct GL amount to invoice and bank-cleared amount (BNK-00211, GL-00253)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 2000 | Accounts Payable | 471.71 |  |
| 1010 | Cash - Operating Account |  | 471.71 |

**AJE-006** (PENDING_APPROVAL): Correct transposed GL amount to bank-cleared amount (BNK-00215, GL-00228)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 2000 | Accounts Payable | 180.00 |  |
| 1010 | Cash - Operating Account |  | 180.00 |

**AJE-007** (PENDING_APPROVAL): Correct transposed GL amount to bank-cleared amount (BNK-00238, GL-00239)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1010 | Cash - Operating Account | 3,600.00 |  |
| 2000 | Accounts Payable |  | 3,600.00 |

**AJE-008** (PENDING_APPROVAL): Reinstate receivable for customer check returned NSF (BNK-00301)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1200 | Accounts Receivable | 8,022.46 |  |
| 1010 | Cash - Operating Account |  | 8,022.46 |

**AJE-009** (PENDING_APPROVAL): Record bank service charge per bank statement (BNK-00342)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 6110 | Bank Service Charges | 308.97 |  |
| 1010 | Cash - Operating Account |  | 308.97 |

**AJE-010** (PENDING_APPROVAL): Record interest earned per bank statement (BNK-00393)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1010 | Cash - Operating Account | 510.01 |  |
| 4910 | Interest Income |  | 510.01 |

**AJE-011** (PENDING_APPROVAL): Reverse duplicate GL posting (GL-00214)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1010 | Cash - Operating Account | 7,082.40 |  |
| 2000 | Accounts Payable |  | 7,082.40 |

**AJE-012** (PENDING_APPROVAL): Reverse duplicate GL posting (GL-00270)

| Account | Name | Debit | Credit |
| --- | --- | ---: | ---: |
| 1010 | Cash - Operating Account | 8,294.94 |  |
| 2000 | Accounts Payable |  | 8,294.94 |

## Needs review queue

No findings are awaiting review.

## Telemetry

| Metric | Value |
| --- | --- |
| Matches by rule | EXACT 297, MANY_TO_ONE 12, NEAR 72 |
| LLM calls | 85 (0 failed) |
| Tokens in / out | 137391 / 2737 |
| Estimated cost (USD, estimate) | 0.000000 |
| Average latency | 0 ms |
| Analyst minutes saved (ASSUMPTION) | 649.5 |

## Run metadata

| Field | Value |
| --- | --- |
| Provider / model | mock / mock |
| Prompt version | investigator_v1 |
| Git commit | 3446aab204cfe11a535a0ffd2b4bd31398324d9c |
| bank_statement | `bank_statement.csv` (393 rows), sha256 `6740cefd2b8615279895ac028a5324009f99c0ea3395758a4b2355985e68284e` |
| gl_cash | `gl_cash.csv` (427 rows), sha256 `208bb3253207a4597bc0397ff5feed324004e2c714acbcf0e09fc886d1f2ad9f` |

Prepared by: A. Preparer (tieout-agent) ______________________ Date ________

Reviewed by: ______________________ Date ________