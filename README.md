# Amazon Payments vs Settlement Reconciliation (PortOne SDE II assignment)

A Python + PostgreSQL pipeline. It ingests Amazon's **Payments** report and **Settlement** report for
the same settlement, applies the production mapping configs (stored as data in Postgres), reconciles
the two sources on `record_ref`, and writes an accounting Summary / Consolidated Excel report that
ties out to Amazon's own settlement total.

> Language note: Python was used instead of Go, as requested for this submission. The structure
> (ingest / rules / reconcile / report) maps one-to-one onto a Go package layout.

## Results (settlement 12395580393, 17.07.2026 to 31.07.2026, AUD)

| | Payments | Settlement | Diff |
|---|---:|---:|---:|
| Settlement proceeds (in-scope rows, excl. bank transfer) | 212,118.95 | 212,118.95 | 0.00 |
| Amazon `total-amount` (settlement header row) | 212,118.95 | 212,118.95 | **0.00** |
| Summary net (Sales + Refunds + Expenses + Paid To Amazon), after fix | 212,118.95 | 212,118.95 | **0.00** |

Summary lines, **before and after** `MAPPING_FIXES.sql`. Every other line is 0.00 on both sides,
before and after.

| Summary line | Diff before fix | Diff after fix | Root cause |
|---|---:|---:|---|
| Sales / Product Charges | -92.84 | -92.84 | not fixable in config (tax split granularity, net 0 within Sales) |
| Sales / Shipping | **13,572.17** | 93.20 | defects 2 and 3 (duplicate rules); remainder same as above |
| Sales / Other | **-11.97** | -0.36 | defect 5 (`sales_gift_wrap_credits`); remainder GiftWrapTax |
| Refunds / Refund expenses | **42.59** | 0.00 | defect 4 (refund tax not summarised) |
| Expenses / Promo rebates | **11,273.53** | 0.00 | defect 1 (promo routed to expenses_other) |
| Expenses / Other | **-11,273.53** | 0.00 | defect 1 |

The residual -92.84 / +93.20 / -0.36 nets to **0.00**. It is a classification difference inside Sales:
the payments report has one `sales tax collected` column, while the settlement report splits it into
Tax / ShippingTax / GiftWrapTax / TaxDiscount (and low-value-goods tax into Principal / Shipping). It
cannot be closed without using settlement data in the Payments column. The full account is in
[MAPPING_FIXES.sql](MAPPING_FIXES.sql) ("NOT FIXABLE A") and [PROGRESS.md](PROGRESS.md).

**Record counts** (one record = one `record_ref`, with both sides aggregated):

| Status | Records | Payment rows | Settlement rows | Net diff |
|---|---:|---:|---:|---:|
| Reconciled | **13,289** | 13,446 | 54,979 | 0.00 |
| Unreconciled payment, in scope | **26** | 26 | 0 | 25 zero-value orders + 1 payout transfer (-133,756.51) |
| Unreconciled payment, out of scope | 9,422 | 9,554 | 0 | Deferred (2,508) or belongs to another settlement (6,914) |
| Unreconciled settlement | **0** | 0 | 0 | |

Every settlement line (54,979 of 54,979) is reconciled. Totals: 13,289 reconciled,
9,448 unreconciled payments (26 in scope), 0 unreconciled settlements.

**Config defects found: 9** (5 visible in this data, 4 latent), **6 matching-logic fixes**,
1 difference that cannot be closed and is explained, and 3 suspicions documented but not changed.
See [MAPPING_FIXES.sql](MAPPING_FIXES.sql).

Deliverables in [output/](output):
- `amazon_recon_report_before_fix.xlsx`: original configs
- `amazon_recon_report_after_fix.xlsx`: after `MAPPING_FIXES.sql`
- `recon_dump_after_fix.sql.gz`: `pg_dump` of schema `recon` after the after-fix run (restore with `gunzip -c ... | psql`)
- `run_all.log`: console output of the full run

---

## Run it end to end

Requirements: Python 3.11+. PostgreSQL is optional: if `DATABASE_URL` is not set, an embedded
PostgreSQL 16 (`pgserver`) is started automatically with its data in `./.pgdata`.

```bash
python -m venv .venv
```
```bash
.venv/Scripts/pip install -r requirements.txt      # Linux/macOS: .venv/bin/pip
```
```bash
# optional: use your own Postgres instead of the embedded one
export DATABASE_URL=postgresql://user:pass@localhost:5432/recon
```
```bash
# full flow: init -> ingest -> reconcile -> report(before) -> MAPPING_FIXES.sql -> ingest -> reconcile -> report(after) -> dump
.venv/Scripts/python -m recon run-all
```

Takes about 2 minutes on a laptop (ingest about 18 s per pass; most of the rest is writing the
23k-row Consolidated sheet).

Individual steps:

```bash
python -m recon init                       # schema + reference data + configs from data/*.csv
python -m recon validate                   # static config checks (duplicates, bad tokens, cross-config conflicts)
python -m recon ingest                     # both files -> recon.raw_record (one table); idempotent
python -m recon reconcile                  # match on record_ref -> recon.recon_result
python -m recon summary                    # Summary to stdout (also checks the running summary)
python -m recon report output/report.xlsx
python -m recon apply-sql MAPPING_FIXES.sql
python -m recon trace E_PROMO              # every rule / row group behind a Summary line
python -m recon dump output/dump.sql.gz
```

Input paths default to `data/`. Override them with `--payments`, `--settlements`, `--payment-cfg` and
`--settlement-cfg`. `--settlement-id` sets the reconciliation scope; by default it is the
settlement(s) present in the settlement file.

Tests (23 tests; unit tests plus an end-to-end test on small fixtures against a real `recon_test` database):

```bash
.venv/Scripts/python -m pytest -q
```

## Layout

```
recon/
  normalize.py        pure parsing: amounts, GMT+9/UTC dates, separator-insensitive match keys
  rules.py            config rule matching (specificity, wildcard, fallback, prefix) + record_ref builder
  configs.py          load configs into Postgres, RuleSets, static + cross-config validation
  ingest.py           streaming ingestion of both files into ONE table, summary accumulated during ingestion
  reconcile.py        scope + record_ref matching + summary queries
  report.py           Excel writer (read-only over the DB, no business logic or adjustments)
  cli.py              commands above
  schema.sql          schema "recon"
  seed_reference.sql  column aliases + Summary layout (data, not code)
MAPPING_FIXES.sql     config fixes, one commented block per defect
tests/                pytest
data/                 the 5 input files exactly as provided (not edited)
output/               reports, dump, run log
```

## Schema, and why

All objects live in schema `recon` ([recon/schema.sql](recon/schema.sql)).

| Table | Purpose |
|---|---|
| `payment_mapping_config`, `settlement_mapping_config` | The two configs. Columns mirror the CSVs exactly, plus `rule_id` (= CSV data row) and `csv_line`. Fixes are `UPDATE/INSERT/DELETE` on these tables. |
| `mapping_config_audit` | Trigger-filled history (old row / new row jsonb) of every config change. The after-fix report lists it. |
| `payment_column_alias` | Payments column header → config `amount_field` (e.g. `fulfilment by amazon fees` → `fba_fees`). This is data, so a new report format needs no code change. |
| `summary_line`, `summary_field_map` | The Summary sheet layout and the routing of each config summary field (optionally split by sign) onto its lines. This is data too. |
| `ingest_run`, `source_file` | One run per ingest, with each file's name, sha256 and row count. |
| **`raw_record`** | **Both files in one table** (`source` = PAYMENT / SETTLEMENT). Every row keeps `line_no` (physical line in the file), `file_id` and the untouched row as `raw jsonb`, next to a typed, normalised projection (`transaction_type_key`, `description_key`, `recon_date` in UTC, `net_amount`, `record_ref`, …). The settlement header row is kept with `is_header = true`. |
| `record_line` | One row per (raw row × non-zero amount component × applied rule): amount, `rule_id`, how it matched (`match_tier`), `status` (MAPPED / UNMAPPED / MULTI_RULE), `record_ref`, `summary_field`. This is the grain the Summary is built from, and each line points to its exact config rule and source row. |
| `running_summary` | Summary accumulated **while ingesting** (bonus requirement). Upserted per 5,000-row batch in the ingest transaction. The report re-aggregates from `record_line` and shows whether both agree (they do). |
| `recon_scope`, `recon_result` | The settlement(s) being reconciled, and one row per `record_ref` with status, reason, row counts per side, nets and difference. |
| view `v_line_trace` | Every record line joined to its file, line number, raw payload and Summary line. This is the audit trail in one query. |

Why this shape:
- **A single raw table with a typed projection plus raw jsonb.** The assignment requires one table.
  Keeping the original payload means no source information is lost to normalisation, and the typed
  columns keep matching fast and indexable.
- **A separate `record_line` grain.** Payments spread one row's money across up to 11 columns, while
  the settlement report has one amount per row. Exploding payments into components that carry their
  rule id gives both sources the same grain for summarising. It also makes "which rule produced this
  number" a `GROUP BY`.
- **Configs and layout as tables, with an audit trigger.** Step-5 fixes are data changes. The audit
  table records exactly what changed, and `config_hash` on `ingest_run` ties each output to the
  config version that produced it.
- **Idempotent ingest.** Each ingest truncates and reloads in one transaction, so a failed run leaves
  the previous state intact and a re-run gives identical results (tested).

## How the configs are interpreted

Implemented in [recon/rules.py](recon/rules.py) and [recon/ingest.py](recon/ingest.py):

1. **Normalisation before matching.** Raw values and config values are both reduced to upper-case
   `[A-Z0-9]` (`Service fee` = `SERVICE_FEE`, `other-transaction` = `OTHER-TRANSACTION`, the en-dash in
   one config row is absorbed, and so is `FBA_LONG_TERM` vs `FBA_LONG-TERM`).
2. **Payment rules** match on (`transaction_type`, `description`, `amount_field`): one rule lookup per
   non-zero amount column. **Settlement rules** match on (`transaction_type`, `amount_type`, `amount_description`).
3. **Precedence.** For transaction_type, an exact match beats the empty-type catch-all. For the
   description columns, exact beats the longest prefix (minimum 6 characters), which beats `any`.
   Prefix matching is needed because Amazon appends variable text, e.g. `TO_ACCOUNT_ENDING` vs
   `To account ending with: 334`. Without it the 133,756.51 payout fell to the catch-all
   `,any,total → expenses_amazon_fees`.
4. **Ties are not hidden.** If several rules are equally specific, all of them apply and the lines are
   flagged `MULTI_RULE`. A duplicated config row therefore shows up as a number instead of being
   silently resolved by "first match wins". This is how defects 2 and 3 surfaced.
5. **Sign-based routing.** `amount > 0` uses `to_summary_field_when_positive_amount`, `< 0` uses the
   negative one, and an empty field means "not summarised" (the line is still stored). Zero amounts
   create no lines.
6. **record_ref.** Tokens `txn_ref` (order id), `sku`, `date`, `settlement_id`, `description`,
   `shipment_id` and `merchant_order_id` resolve to row values. Any other token is a literal. Parts
   are joined with `|`.
7. **The `date` token.** Settlement: `posted-date-time` (UTC). Payments: `Transaction Release Date`
   converted from GMT+9 to UTC, falling back to `date/time` when there is no release date. The
   settlement posts a transaction on the day its funds are released. Using `date/time` matched only
   35 of 13,426 rows; using the release date matches 100% of settlement lines.

## Reconciliation design

- **Granularity.** One payments row (e.g. an order line) corresponds to 2–8 settlement rows
  (Principal, Tax, Shipping, Commission, FBA fee, Promotion…), and a settlement record can also
  aggregate several payment rows. Both sides are therefore **aggregated to one row per `record_ref`
  before matching**: payments `Σ total`, settlement `Σ amount`. The per-Summary-line breakdown is
  aggregated the same way, so the Consolidated sheet shows payment vs settlement per bucket on the
  same row.
- **Status.** A `record_ref` found on both sides is RECONCILED (with the difference shown; it is 0.00
  for all 13,289). One found on only one side is an UNRECONCILED_PAYMENT / UNRECONCILED_SETTLEMENT,
  with a machine-assigned reason: DEFERRED, OTHER SETTLEMENT, PAYOUT, NO … COUNTERPART.
- **Scope.** The payments report covers four settlement IDs, including Deferred rows. The Summary
  compares settlement `12395580393` (the one in the settlement file). Payments are filtered to that
  settlement ID with status **Released**, since Deferred funds are held by Amazon and are not in this
  settlement. Each column is still computed purely from its own source's records. The scope choice is
  proven by the tie-out: in-scope payments excluding the payout = 212,118.95 = settlement header
  `total-amount`.

## Summary layout ([recon/seed_reference.sql](recon/seed_reference.sql))

| Summary line | Config summary field(s) |
|---|---|
| Sales / Product Charges | `sales_product_charges` |
| Sales / Tax | `sales_tax` |
| Sales / Shipping | `sales_shipping` |
| Sales / Amazon fees | `sales_amazon_fees` |
| Sales / Inventory Reimbursements | `sales_inventory_reimbursements` |
| Sales / Other | `sales_other`, `total_adjustment_other_buyer_recharge_amt` (positive) |
| Sales / Micro Deposit (Failed) | `bank_account_transfer_round_off` (positive) |
| Refunds / Refund expenses | `refunded_expenses`, `total_refund_expense_or_sales_amt` (positive) |
| Refunds / Refunded sales | `refunded_sales`, `total_refund_expense_or_sales_amt` (negative) |
| Expenses / Promo rebates | `expenses_promotional_rebates` |
| Expenses / FBA fees | `expenses_fba_fees` |
| Expenses / Cost of Advertising | `expenses_cost_of_advertising` |
| Expenses / Amazon fees | `expenses_amazon_fees` |
| Expenses / Reversed Reimbursements | `expenses_reversed_reimbursements` |
| Expenses / Other | `expenses_other`, `total_adjustment_other_buyer_recharge_amt` (negative) |
| Expenses / Micro Deposit | `bank_account_transfer_round_off` (negative) |
| Paid To Amazon | `paid_to_amazon` |
| Memo lines (balance items, not P&L) | `current_reserve_amount`, `beginning_balance`, `amazon_carried_forward` |

Section rows (Sales / Refunds / Expenses) are subtotals of their lines. No config field feeds
Cross-account Debt Adjustment, Sales FBA Fees or Expenses Shipping Charges, so those lines stay 0 on
both sides. A config summary field that is not in this map is reported by `recon validate` (this is
how defect 5, `sales_gift_wrap_credits`, was caught).

## The report

| Sheet | What it shows |
|---|---|
| **Summary** | Same layout as the sample (Payments, Settlements, Payments − Settlements), then a tie-out block: Summary net → memo lines → amounts not routed → net of all in-scope rows → less payouts → **Amazon `total-amount` check = 0**. |
| **Consolidated Data** | One row per `record_ref`: reconciled first, then unreconciled payments, then unreconciled settlements. Uses the sample's column layout (payment fields + 21 summary buckets, settlement fields + 21 buckets, status, total diff, 21 bucket diffs), prefixed with `record_ref`. Audit columns are appended: in scope, reason, **source file:line for every contributing row on each side**, and routing (`field=amount -> summary field [rule #]`). |
| **Variance Drilldown** | For every Summary line, every source / transaction type / amount type / rule combination behind it, with line diffs. Any variance points straight at the rule that produced it. |
| **Tie-out & Counts** | Settlement header vs lines; payments population by settlement ID × status with the scope flag; record counts by status and reason; amounts not routed to the Summary; payment rows whose routed components don't sum to the row total; ingestion checks (UNMAPPED, MULTI_RULE, component checks, file hashes, config hash). |
| **Config Findings** | Static validation results, MULTI_RULE rules that fired, and the config change audit trail. |

## Tracing any number back to source rows

```sql
-- all rows behind "Expenses / Promo rebates", payment side, with file and line
SELECT file_name, line_no, record_ref, amount_field, amount, rule_table, rule_id, raw
FROM recon.v_line_trace
WHERE summary_line = 'Promo rebates' AND source = 'PAYMENT';

-- one record_ref, both sides
SELECT source, file_name, line_no, amount_field, amount, summary_field, rule_id
FROM recon.v_line_trace WHERE record_ref = '249-3364825-5895055|BIO-S000004059_AU|2026-07-17';
```

Or run `python -m recon trace E_PROMO PAYMENT`. In Excel, the Consolidated sheet's
"source rows (file:line)" columns give the same trail.

## Assumptions

- Files are ingested exactly as delivered. All normalisation (BOM, preamble, thousands separators,
  GMT+9 → UTC, separators and case) happens in code.
- The Summary compares one settlement: the one(s) present in the settlement file. Payment rows are
  in scope when they belong to that settlement ID and are **Released**.
- The payout Transfer row is not a P&L item. It is unrouted by design (the TRANSFER rule has no
  summary field) and appears as the payout line in the tie-out.
- Summary fields named `total_*_amt` are split across two Summary lines by the sign of each line
  (positive / negative), as listed in the layout table above. This is configurable in
  `summary_field_map`.
- Settlement rows with a zero amount produce no record line. They are still stored in `raw_record`.
- Amounts use `numeric(14,2)` / Python `Decimal` throughout. There are no floats until the Excel cell.
- `psycopg[binary]` was blocked by Windows Application Control on the development machine. The
  package then falls back to libpq from the embedded server (see `recon/__init__.py`). On a normal
  machine nothing special happens.
