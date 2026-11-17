# Progress log

How I got from the raw files to a tied-out report: what I tried, what I ruled out, and how each
variance was isolated. Amounts are AUD for settlement 12395580393 unless stated otherwise.

## 1. Profiling the inputs (before writing any pipeline code)

- **Payments CSV.** UTF-8 with a BOM, 9 preamble lines before the header, 23,026 rows. There are 6
  transaction types (Order 22,783, Refund 139, Adjustment 97, Service fee 3, Transfer 2, FBA
  transaction fees 2), spread over **4 settlement IDs** and **Released / Deferred** statuses. Two
  amounts use thousands separators (`-133,756.51`). Dates look like `29 June 2026 5:39:32 pm GMT+9`
  and `1 Aug 2026 …` (full and abbreviated month names both occur).
- **Settlement TXT.** Tab separated, 54,980 rows, all one settlement (12395580393). Row 2 is a header
  row with `total-amount = 212,118.95`, and the 54,979 lines sum to exactly that.
- **Payment config.** 149 rules. On a first read: duplicate keys for `ORDER/any/sales_tax_collected`
  (#70 → product charges, #71 → shipping) and `ORDER/any/low_value_goods` (#4 → shipping,
  #5 → product). `ORDER promotional_rebates → expenses_other`, while the settlement config sends
  promotions to `expenses_promotional_rebates`.
- **Sample report.** The Summary has 3 sections + Paid To Amazon. The Consolidated sheet layout has
  105 columns (payment block, settlement block, status, diffs).

## 2. Finding the date that makes keys match

The first attempt keyed payments on `order ID + sku + date(date/time)`. Only **35 of 13,426** released
order/refund rows matched a settlement line.

Looking at one order (`249-3364825-5895055`): the payments `date/time` is 9 Jul (order posted), but the
settlement `posted-date-time` is `17.07.2026 07:26:32 UTC`, which is exactly the payments
`Transaction Release Date` `17 July 2026 4:26:32 pm GMT+9` converted to UTC. Switching the key date
to *release date → UTC* matched **54,979 / 54,979** settlement Order/Refund lines. I also ruled out
using the GMT+9 calendar date without conversion: adjustments near midnight (e.g. 18 Jul 2:14 am
GMT+9 vs settlement 17.07 17:14 UTC) need the UTC conversion.

## 3. Finding the scope that ties to Amazon's number

Payment totals by settlement × status:

| settlement | status | total |
|---|---|---:|
| 12395580393 | Released | 78,362.44 |
| 12395580393 | Deferred | 43,540.07 |
| others | any | 28,306.64 |

78,362.44 includes the payout Transfer of -133,756.51. **78,362.44 + 133,756.51 = 212,118.95**,
which is Amazon's settlement `total-amount` to the cent. So the in-scope payment population is
*this settlement ID AND Released*, and the payout belongs in the tie-out rather than the P&L. I
ruled out "all payment rows" (it can never tie: deferred and other-settlement money) and "this
settlement including Deferred" (off by exactly 43,540.07 of held funds).

## 4. First full run (original configs), matching-logic issues

The pipeline was built (streaming ingest, config tables, rule matcher, reconcile, report). On the
first run:
- The Transfer row `To account ending with: 334` did not match `TRANSFER/TO_ACCOUNT_ENDING` under
  exact matching. It fell through to the catch-all `,any,total → expenses_amazon_fees` and would have
  put **-133,756.51** into Amazon fees (verified by toggling prefix matching off). Fix in **matching
  logic**: description prefix matching with precedence exact > prefix > any. This is not a config
  defect; the config intentionally stores the stable prefix.
- The config uses `fba_fees` but the report header is `fulfilment by amazon fees`. Handled as data in
  `payment_column_alias`.
- `marketplace_withheld_tax` rules can never fire (this report has `low value goods` instead).
  Documented and left alone.
- Ingestion checks: 0 unmapped lines; 0 payment rows where Σcomponents ≠ total; 18,414 MULTI_RULE
  lines (the duplicates, as expected). The running summary equals the SQL re-aggregation.

Before-fix Summary diffs (P − S): Shipping **+13,572.17**, Promo rebates **+11,273.53**, Expenses/Other
**-11,273.53**, Sales/Other **-11.97**, Refund expenses **+42.59**, Product Charges **-92.84**. Every
other line is 0.

Reconciliation was already complete at net level: 13,289 records RECONCILED with **net diff 0.00 for
every record**. So every variance is a *routing* problem (which bucket), not a *population* problem
(which rows). That ruled out record_ref/date issues for this data and pointed at the configs.

## 5. Isolating each variance

Method: compare the in-scope payment component columns against the settlement amount types on the
reconciled records, per rule (`Variance Drilldown` sheet / `recon trace`). Then a per-record check
(for each reconciled `record_ref`, the payment vs settlement amount per Summary line) to make sure
nothing was hiding behind netting.

| Variance | Records that differ | Decomposition | Rule(s) |
|---|---:|---|---|
| Promo +11,273.53 / Other -11,273.53 | 3,935 | payments `promotional_rebates` = -11,273.53 = settlement Promotion/Principal -4,098.30 + Promotion/Shipping -7,175.23 | payment #64 → `expenses_other` (defect 1) |
| Shipping +13,572.17 | 5,151 | +13,675.59 (tax counted twice) − 196.62 (LVG counted twice) + 93.20 residual | payment #71 dup of #70 (defect 2), #4 dup of #5 (defect 3) |
| Refund expenses +42.59 | 16 | payments refund `sales_tax_collected` -42.59 not summarised; settlement Tax -41.09 + ShippingTax -3.19 + TaxDiscount +1.69 → refunded_expenses | payment #13 (defect 4) |
| Sales/Other -11.97 | 3 | gift wrap 11.61 routed to `sales_gift_wrap_credits` (no such Summary field) + 0.36 GiftWrapTax | payment #69 (defect 5) |
| Product -92.84 | 805 | see residual below | not fixable in config |

I cross-checked with the row-level routing check: in-scope payment Order rows summed to 104,507.61
routed vs 91,028.64 row totals. The over-routing of 13,478.97 equals 13,675.59 − 196.62, i.e. the two
duplicates exactly.

### Ruled out: settlement #94 (ORDER/PROMOTION/TAXDISCOUNT → sales_shipping)

At first this looked like a defect (other promotion parts go to promo rebates). The numbers say it is
deliberate. Free-shipping promotions produce ShippingTax +x and TaxDiscount -x, so routing TaxDiscount
to shipping nets shipping tax to ~0 (581.68 − 672.16). Moving it to product charges would have *grown*
the Product/Shipping split from ~93 to ~579, and moving it to promo rebates would open a new 672.16
Promo variance against the payments column. Left unchanged.

## 6. Applying fixes 1–5 and re-running

After-fix diffs: Shipping 13,572.17 → **93.20**, Promo / Expenses-Other / Refund expenses → **0.00**,
Sales/Other → **-0.36**, Product → **-92.84** (unchanged, as predicted). 0 MULTI_RULE lines. The only
unrouted in-scope amount is the payout. Summary net = 212,118.95 on both sides = Amazon total.

### The residual (-92.84 / +93.20 / -0.36, net 0.00)

Payments `sales_tax_collected` (13,675.59) = settlement Tax 13,765.71 + ShippingTax 581.68 + GiftWrapTax
0.36 + TaxDiscount -672.16, verified to the cent. Payments `low_value_goods` (-196.62) = LVG-Principal
-193.90 + LVG-Shipping -2.72. The settlement routes ShippingTax / TaxDiscount / LVG-Shipping to
Shipping and GiftWrapTax to Other. The payment side has a single column for each, routed to Product
Charges. −(581.68 − 672.16 + 0.36 − 2.72) = −92.84 ✔.

This cannot be closed in the payment config, because one column maps to one field. Closing it from
settlement data would break "Payment column purely from payment records". Coarsening the settlement
config would close it, but it would misstate the more granular source, so I documented that option
in `MAPPING_FIXES.sql` and did not apply it.

## 7. Looking where the Summary cannot show problems

"A Summary whose columns agree is not sufficient." I checked three more places:

1. **Unreconciled in-scope payments (26).** One is the payout Transfer; the other 25 are Order rows
   where every amount column is 0.00. Amazon doesn't emit zero settlement lines. No money, so nothing
   to close.
2. **Per-record bucket differences inside reconciled records.** After the fixes the only non-zero
   per-record differences are the tax-split residual above.
3. **Latent config findings** that this settlement never exercises. I paired every payment rule with
   its settlement counterpart via the record_ref *anchor* (the template with field tokens
   abstracted). Same anchor = same money, so the keys and the routing must agree. This found:
   - AWD storage fee `AWD_STORAGE_FEES` (payment #6/#92) vs `AWD_STORAGE_FEE` (settlement #47): the
     keys can never match (defect 6).
   - SAFE-T reimbursement: payment #58 → `expenses_fba_fees`, settlement #78 →
     `sales_inventory_reimbursements` (defect 7).
   - Settlement #137 record_ref contains a non-field token `record_type` and a duplicated
     `settlement_id`. The intended counterpart is unproven here, so this rule remains unchanged.
   - Payment #30 routes only positive FEE_CORRECTION amounts. There is no negative example or
     accounting policy in these files to prove where a negative belongs, so it remains unchanged.
   - Suspected, not changed for lack of data: DEALS (#7) vs DEALS_PROMOTION_FEE anchors, VINE
     (#101 vs settlement #98), shipment_id vs txn_ref for inbound fees.

   These checks are now built into `python -m recon validate` (near-miss anchors via string
   similarity; same anchor with different routing). The two unresolved checks remain visible in
   `recon validate` and the report's Config Findings sheet rather than being suppressed with
   speculative updates. Defects 1 and 4 are well-formed but semantically wrong, so only the data
   variance could reveal them.

## 8. Hardening

- Idempotent re-ingest (truncate and reload in one transaction), config hash stamped on each run,
  trigger-based config audit.
- The summary is accumulated during ingestion and cross-checked against SQL on every report.
- 24 pytest tests (33 after section 10): parsing edge cases (GMT+9 midnight crossing, abbreviated months, thousands
  separators), rule precedence / prefix / duplicates / sign routing, and an end-to-end fixture run
  against a real Postgres database covering scope, statuses and reasons, running summary, a config
  fix as a data change, idempotency, and report ordering.
- Performance: 78k source rows → 163k record lines in ~18 s using COPY in 5,000-row batches.
- An environment problem I hit: the Windows Application Control policy blocked the `psycopg_binary`
  DLL. I fell back to psycopg's pure-Python implementation over the libpq that ships with the
  embedded server. Separately, `pg_dump` deadlocked behind my own idle read transaction; fixed by
  committing before the dump.

## 9. Final review and reproduction

- Tightened the accounting scope to require an explicit `Released` payment status. A new fixture
  verifies that an unexpected `Pending` status contributes nothing to either summary query.
- Kept the two unproven latent changes (rules #137 and #30) as documented investigations rather
  than applying a guessed key or accounting bucket. They remain visible on the Config Findings sheet.
- Ran all 24 tests against a fresh PostgreSQL 16 instance: all passed. Then ran `run-all` against
  another fresh instance and regenerated both workbooks, the dump, and `output/run_all.log`.
- Independently summed the supplied raw files: settlement lines and header both equal 212,118.95;
  released payment rows total 78,362.44 and the bank transfer is -133,756.51, giving proceeds of
  212,118.95. The regenerated after-fix workbook has 13,289 reconciled records, 9,448 unreconciled
  payments (26 in scope), no unreconciled settlements, and a zero Amazon proceeds check.

## 10. Verifying the bonus items

- **Fresh clone.** I cloned the pushed repo into a new folder, created a new venv and database, and
  ran it: input sha256s matched the originals, all tests passed, and `run-all` reproduced the same
  numbers (before-fix 13,572.17 / ±11,273.53 / 42.59 / -11.97; after-fix only -92.84 / +93.20 / -0.36;
  Amazon check 0; 13,289 reconciled).
- **Summary during ingestion.** I added a test that runs with a batch size of 1 and inspects
  `running_summary` after each flush, inside the ingest transaction. The totals grow batch by batch
  and finish equal to the SQL re-aggregation. A second test shows batch size has no effect on results.
- **Error handling.** I added tests for a corrupt amount, an invalid month, a short row, a file with
  no header, and a corrupt settlement amount. Each fails with `file:line: reason`, and the previous
  good load is left intact (the truncate rolls back). The invalid-month test **failed at first**:
  the parser took the first three letters of the month, so "Julember" became July. Fixed by
  accepting only full month names and standard abbreviations.
- **Performance.** `scripts/make_large.py` builds a 10× dataset by duplicating rows with suffixed
  order IDs, scaling the settlement header total by 10. Profiling showed Python parsing is linear
  (~15 s per 1×), with key normalisation as the top hot spot (1.09M calls at 1×). I cached it, and
  moved secondary index builds to after the COPY load. Under the same machine load, 1× ingest took
  45 s and 10× took 423 s (~9.4×, linear). An earlier 18 s vs 413 s comparison looked super-linear
  only because the machine was idle during the first measurement; the Postgres log shows antivirus
  "sharing violation" retries on the data directory during the slow period. At 10× every Summary line
  is exactly 10× the real figure, and both sources tie to the scaled header (2,121,189.50).
  Record counts scale ×10 except for keys without an order ID (Damaged:Warehouse, subscription,
  transfer), whose copies share a key. That is expected for synthetic duplicates.
