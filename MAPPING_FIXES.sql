-- =====================================================================
-- MAPPING_FIXES.sql
-- Fixes to the mapping configs, applied as DATA changes to
--   recon.payment_mapping_config     (from amazon_payment_configs_au_old.csv)
--   recon.settlement_mapping_config  (from amazon_settlement_configs_au.csv)
--
-- rule_id = data-row number in the CSV (CSV line = rule_id + 1).
-- Every statement keys on rule_id AND the rule's business columns, so it
-- cannot hit the wrong row if the table is re-seeded in another order.
-- Every change is captured by trigger in recon.mapping_config_audit and is
-- listed on the "Config Findings" sheet of the after-fix report.
--
-- Amounts quoted are for settlement 12395580393 (in scope), before -> after.
-- Run:  python -m recon apply-sql MAPPING_FIXES.sql   (then ingest/reconcile/report)
-- =====================================================================
BEGIN;
SET search_path = recon, public;

-- ---------------------------------------------------------------------
-- DEFECT 1  payment rule #64 (CSV line 65)
--   ORDER / any / promotional_rebates -> expenses_other / expenses_other
-- Evidence : Summary "Expenses / Promo rebates" P 0.00 vs S -11,273.53 and
--            "Expenses / Other" P -11,273.53 vs S 0.00. 3,935 reconciled
--            records differ on exactly these two lines. The settlement side
--            routes the same money (Promotion/Principal -4,098.30 +
--            Promotion/Shipping -7,175.23 = -11,273.53, settlement rules
--            #71/#72) to expenses_promotional_rebates. The payments
--            promotional_rebates column equals that sum to the cent.
-- Change   : route to expenses_promotional_rebates on both signs.
--            (REFUND promotional_rebates, rule #16, already mirrors the
--            settlement refund-promotion rules, so it is left as is.)
-- Closes   : Promo rebates 11,273.53 -> 0 ; Expenses/Other -11,273.53 -> 0
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET to_summary_field_when_positive_amount = 'expenses_promotional_rebates',
       to_summary_field_when_negative_amount = 'expenses_promotional_rebates'
 WHERE rule_id = 64 AND transaction_type = 'ORDER' AND description = 'any'
   AND amount_field = 'promotional_rebates';

-- ---------------------------------------------------------------------
-- DEFECT 2  payment rules #70 and #71 (CSV lines 71-72): duplicate rule
--   ORDER / any / sales_tax_collected -> sales_product_charges   (#70)
--   ORDER / any / sales_tax_collected -> sales_shipping          (#71)
-- Evidence : the same 5,235 tax amounts (13,675.59) were counted twice,
--            once in Product Charges and again in Shipping. In-scope payment
--            Order rows summed to 104,507.61 against row totals of
--            91,028.64 (13,478.97 over-routed = this +13,675.59 and
--            defect 3's -196.62). "Sales / Shipping" P 21,773.13 vs
--            S 8,200.96.
-- Change   : delete #71 and keep #70. The settlement config routes
--            ItemPrice/Tax, the bulk of this column (13,765.71), to
--            sales_product_charges.
-- Closes   : 13,675.59 of the Shipping variance. What remains is
--            NOT-FIXABLE A below.
-- ---------------------------------------------------------------------
DELETE FROM payment_mapping_config
 WHERE rule_id = 71 AND transaction_type = 'ORDER' AND description = 'any'
   AND amount_field = 'sales_tax_collected'
   AND to_summary_field_when_positive_amount = 'sales_shipping';

-- ---------------------------------------------------------------------
-- DEFECT 3  payment rules #4 and #5 (CSV lines 5-6): duplicate rule
--   ORDER / any / low_value_goods -> sales_shipping              (#4)
--   ORDER / any / low_value_goods -> sales_product_charges       (#5)
-- Evidence : 50 low-value-goods tax amounts (-196.62) counted twice. The
--            settlement splits them as LowValueGoodsTax-Principal -193.90
--            (-> sales_product_charges, settlement #118) and
--            LowValueGoodsTax-Shipping -2.72 (-> sales_shipping, #117).
-- Change   : delete #4 and keep #5, since almost all of the amount is the
--            principal component.
-- Closes   : -196.62 double count. The -2.72 shipping portion goes to
--            NOT-FIXABLE A.
-- ---------------------------------------------------------------------
DELETE FROM payment_mapping_config
 WHERE rule_id = 4 AND transaction_type = 'ORDER' AND description = 'any'
   AND amount_field = 'low_value_goods'
   AND to_summary_field_when_positive_amount = 'sales_shipping';

-- ---------------------------------------------------------------------
-- DEFECT 4  payment rule #13 (CSV line 14)
--   REFUND / any / sales_tax_collected -> '' / ''   (not summarised)
-- Evidence : "Refunds / Refund expenses" P 258.87 vs S 216.28 (+42.59).
--            16 reconciled refund records differ. The payments
--            sales_tax_collected on those 16 refund rows = -42.59, which
--            dropped out of the payment summary. The settlement routes the
--            same money (Refund ItemPrice/Tax -41.09 + ItemPrice/ShippingTax
--            -3.19 + Promotion/TaxDiscount +1.69 = -42.59, settlement rules
--            #3/#10/#19) to refunded_expenses.
-- Change   : route to refunded_expenses on both signs.
-- Closes   : Refund expenses 42.59 -> 0
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET to_summary_field_when_positive_amount = 'refunded_expenses',
       to_summary_field_when_negative_amount = 'refunded_expenses'
 WHERE rule_id = 13 AND transaction_type = 'REFUND' AND description = 'any'
   AND amount_field = 'sales_tax_collected';

-- ---------------------------------------------------------------------
-- DEFECT 5  payment rule #69 (CSV line 70)
--   ORDER / any / gift_wrap_credits -> sales_gift_wrap_credits
-- Evidence : sales_gift_wrap_credits is not a field of the Summary and is
--            not used by any settlement rule, so 11.61 of gift-wrap income
--            fell off the payment summary ("Sales / Other" P 0.00 vs
--            S 11.97). The settlement routes ItemPrice/GiftWrap (11.61,
--            settlement #65) to sales_other.
-- Change   : route to sales_other.
-- Closes   : 11.61 of Sales/Other. The remaining -0.36 (GiftWrapTax) goes
--            to NOT-FIXABLE A.
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET to_summary_field_when_positive_amount = 'sales_other',
       to_summary_field_when_negative_amount = 'sales_other'
 WHERE rule_id = 69 AND transaction_type = 'ORDER' AND description = 'any'
   AND amount_field = 'gift_wrap_credits';

-- =====================================================================
-- LATENT DEFECTS. These rules do not fire on this settlement's data, so no
-- Summary number moves. I found them by pairing every payment rule with
-- its settlement counterpart on the record_ref literal anchor (the same
-- underlying money) and checking that both build the same key and route
-- to the same bucket. Left in place, each one breaks reconciliation the
-- first time such a transaction appears.
-- =====================================================================

-- ---------------------------------------------------------------------
-- DEFECT 6  AWD storage fee record_ref mismatch (would never reconcile)
--   payment    #6  SERVICE_FEE / AWD_STORAGE_FEES / total    txn_ref+AWD_STORAGE_FEES+settlement_id+date
--   payment    #92 SERVICE_FEE / AWD_STORAGE_FEES / fba_fees txn_ref+AWD_STORAGE_FEES+settlement_id+date
--   settlement #47 OTHER-TRANSACTION / AMAZON_WAREHOUSING_&_DISTRIBUTION_(AWD) / AWD_STORAGE_FEE
--                                                             txn_ref+AWD_STORAGE_FEE+settlement_id+date
-- Evidence : "FEES" vs "FEE" makes the two keys different strings. Both
--            sibling AWD fees use one anchor on both sides
--            (SERVICE_FEE_AWD_PROCESSING_FEES, SERVICE_FEE_AWD_TRANSPORTATION_FEES).
-- Change   : align all three to txn_ref+SERVICE_FEE_AWD_STORAGE_FEES+settlement_id+date.
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET record_ref = 'txn_ref+SERVICE_FEE_AWD_STORAGE_FEES+settlement_id+date'
 WHERE rule_id IN (6, 92) AND transaction_type = 'SERVICE_FEE' AND description = 'AWD_STORAGE_FEES';
UPDATE settlement_mapping_config
   SET record_ref = 'txn_ref+SERVICE_FEE_AWD_STORAGE_FEES+settlement_id+date'
 WHERE rule_id = 47 AND amount_type = 'AMAZON_WAREHOUSING_&_DISTRIBUTION_(AWD)'
   AND amount_description = 'AWD_STORAGE_FEE';

-- ---------------------------------------------------------------------
-- DEFECT 7  SAFE-T reimbursement routed to opposite buckets
--   payment    #58 SAFE-T_REIMBURSEMENT / SAFE-T_CLAIM_ID / other -> expenses_fba_fees
--   settlement #78 SAFE-T_REIMBURSEMENT / OTHER_TRANSACTIONS / SAFE-T_REIMBURSEMENT
--                                                         -> sales_inventory_reimbursements
-- Evidence : both rules build the same record_ref (txn_ref+sku+settlement_id+date),
--            so they describe the same money, but one side books it as an FBA
--            expense and the other as reimbursement income. A SAFE-T claim
--            pays the seller back, which is income, so the settlement side is
--            right.
-- Change   : payment #58 -> sales_inventory_reimbursements on both signs.
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET to_summary_field_when_positive_amount = 'sales_inventory_reimbursements',
       to_summary_field_when_negative_amount = 'sales_inventory_reimbursements'
 WHERE rule_id = 58 AND transaction_type = 'SAFE-T_REIMBURSEMENT' AND amount_field = 'other';

-- ---------------------------------------------------------------------
-- DEFECT 8  settlement rule #137 (CSV line 138): malformed record_ref
--   OTHER-TRANSACTION / OTHER-TRANSACTION / PROMOTION_ADJUSTMENT
--   record_ref = ADJUSTMENT_OTHER+settlement_id+date+record_type+settlement_id
-- Evidence : 'record_type' is not a field of either report, so it would be
--            treated as a literal. settlement_id also appears twice. The key
--            can never equal the payment-side key for "Adjustment / Other"
--            (payment #37/#38: ADJUSTMENT_OTHER+settlement_id+date), which
--            uses the same summary field (total_adjustment_other_buyer_recharge_amt).
-- Change   : ADJUSTMENT_OTHER+settlement_id+date
-- ---------------------------------------------------------------------
UPDATE settlement_mapping_config
   SET record_ref = 'ADJUSTMENT_OTHER+settlement_id+date'
 WHERE rule_id = 137 AND amount_description = 'PROMOTION_ADJUSTMENT';

-- ---------------------------------------------------------------------
-- DEFECT 9  payment rule #30 (CSV line 31): one-sided routing
--   ADJUSTMENT / FBA_INVENTORY_REIMBURSEMENT_-_FEE_CORRECTION / other
--   -> positive: sales_other, negative: ''   (negative amounts silently dropped)
-- Evidence : the only "other"-column rule in the config that routes one
--            sign and drops the other. Every comparable adjustment routes
--            both signs, e.g. SHIPPING_SERVICES/ADJUSTMENT #107:
--            sales_other / expenses_other. A negative fee correction would
--            disappear from the summary while still being in the row total.
-- Change   : negative -> expenses_other.
-- ---------------------------------------------------------------------
UPDATE payment_mapping_config
   SET to_summary_field_when_negative_amount = 'expenses_other'
 WHERE rule_id = 30 AND description = 'FBA_INVENTORY_REIMBURSEMENT_-_FEE_CORRECTION'
   AND amount_field = 'other';

COMMIT;

-- =====================================================================
-- FIXES THAT BELONG IN THE MATCHING LOGIC, NOT THE CONFIG
-- (implemented in recon/rules.py, recon/ingest.py, recon/reconcile.py)
-- =====================================================================
-- M1  Date used in record_ref (recon/ingest.py)
--     The payments "date/time" is the order posting time in GMT+9. The
--     settlement "posted-date" is the date the funds were released, in UTC.
--     Building payment keys from date/time matched 35 of 13,426 released
--     order/refund rows. Building them from "Transaction Release Date"
--     converted to UTC matches every settlement Order/Refund line
--     (54,979 of 54,979). Deferred rows have no release date and fall back
--     to date/time; they are not in this settlement anyway (see M4).
--
-- M2  Separator- and case-insensitive key comparison (recon/normalize.py)
--     Raw "Fulfilment by Amazon transaction fees", "other-transaction",
--     "FBA Removal Order: Disposal Fee" have to match config
--     "FULFILMENT_BY_AMAZON_TRANSACTION_FEES", "OTHER-TRANSACTION",
--     "FBA_REMOVAL_ORDER:_DISPOSAL_FEE". Both sides are reduced to [A-Z0-9]
--     before comparing. This also absorbs the en-dash in payment rule #137
--     ("..._REIMBURSEMENT_–_LOST") and the hyphen/underscore differences
--     (FBA_LONG_TERM vs FBA_LONG-TERM).
--
-- M3  Description prefix match (recon/rules.py)
--     Payment rule #62 TRANSFER / TO_ACCOUNT_ENDING has to match the raw
--     "To account ending with: 334", whose tail carries the bank account
--     digits. With exact matching only, the -133,756.51 payout fell through
--     to the catch-all rule #109 (',any,total' -> expenses_amazon_fees) and
--     would have shown as -133,756.51 of Amazon fees. Precedence is exact >
--     longest prefix (minimum 6 chars) > 'any', and a rule for the row's own
--     transaction_type always beats the empty-type catch-all.
--
-- M4  Reconciliation scope (recon/reconcile.py)
--     The payments report spans 4 settlement IDs and includes Deferred rows.
--     A payment row is in scope when its settlement ID is in the settlement
--     file AND its status is Released. In-scope payments net to 78,362.44.
--     Adding back the -133,756.51 payout (Transfer row) gives 212,118.95,
--     which equals the settlement header total-amount to the cent.
--
-- M5  Column alias (data: recon.payment_column_alias)
--     The config says fba_fees and the report header says "fulfilment by
--     amazon fees". This report has no "marketplace withheld tax" column
--     (it has "low value goods" instead), so payment rules #12, #74, #77
--     (marketplace_withheld_tax) can never fire on this format. They are
--     harmless and left in place.
--
-- M6  Duplicate equally-specific rules are all applied and flagged
--     MULTI_RULE rather than resolved by "first match wins". That is what
--     made defects 2 and 3 visible as numbers.

-- =====================================================================
-- NOT FIXABLE IN THE CONFIG (remaining, fully explained differences)
-- =====================================================================
-- A   Product Charges -92.84 / Shipping +93.20 / Sales Other -0.36  (net 0.00)
--     The payments report has ONE "sales tax collected" column and ONE "low
--     value goods" column. The settlement report splits the same money:
--        ItemPrice/ShippingTax      +581.68  -> sales_shipping        (settlement #62)
--        Promotion/TaxDiscount      -672.16  -> sales_shipping        (settlement #94)
--        ItemPrice/GiftWrapTax        +0.36  -> sales_other           (settlement #60)
--        LowValueGoodsTax-Shipping    -2.72  -> sales_shipping        (settlement #117)
--     On the payment side all four sit in columns routed to
--     sales_product_charges. Hence Product Charges P - S = -(581.68 - 672.16
--     + 0.36 - 2.72) = -92.84, offset by Shipping +93.20 and Other -0.36.
--     This is a classification difference inside Sales. Every record still
--     reconciles at net 0.00.
--     Why it cannot be closed: a payment rule maps one source column to one
--     summary field, and the payment data does not contain the split. Using
--     the settlement rows to split it would break the rule that the Payment
--     column comes only from payment records.
--     What would be needed: either (i) a payments report that carries the
--     tax split (Amazon's older "shipping credits tax" column), plus a new
--     payment rule per tax column; or (ii) a config schema change that
--     lets a payment rule carry an allocation formula across summary
--     fields. Neither is a plain config edit.
--     Optional alternative (NOT applied): route settlement #62, #94, #60,
--     #117 to sales_product_charges. The columns would then tie, but the
--     more granular source (settlement) would lose its correct shipping
--     classification just to match the coarser one. I chose not to do it.
--
--     -- UPDATE settlement_mapping_config SET to_summary_field_when_positive_amount='sales_product_charges',
--     --        to_summary_field_when_negative_amount='sales_product_charges' WHERE rule_id IN (60,62,94,117);
--
-- B   25 zero-value payment Order rows (all amount columns 0.00) have no
--     settlement counterpart. Amazon does not emit zero amount lines in the
--     settlement file. They carry no money and stay as unreconciled payments.
--
-- C   1 payment Transfer row (-133,756.51, "To account ending with: 334") is
--     the payout to the bank. In the settlement report it is the deposit
--     itself, not a line, so it is unreconciled by nature. It is shown on
--     the tie-out lines of the Summary.

-- =====================================================================
-- SUSPECTED BUT NOT CHANGED (no data in this file to confirm; documented
-- so the owner can confirm against a settlement that contains them)
-- =====================================================================
-- S1  payment #7 DEALS/any/total ref txn_ref+settlement_id+date. The settlement
--     side keys deals fees as DEALS_PROMOTION_FEE(+_SPECIAL)+settlement_id+date
--     (settlement #91/#92/#114/#115), so the keys will not match. Fixing it
--     needs the payment description that separates PROMOTION_FEE from
--     PROMOTION_FEE_SPECIAL: two DEALS rules with specific descriptions.
-- S2  payment #101 SERVICE_FEE/VINE_ENROLLMENT_FEE (txn_ref+VINE_ENROLLMENT_FEE+...)
--     vs settlement #98 VINE_ENROLLMENT_FEE/.../VINE_ENROLLMENT_FEE (txn_ref+sku+date).
--     The anchors differ.
-- S3  payment #82/#95 use txn_ref while settlement #48/#23 use shipment_id for
--     inbound defect / placement fees. They match only if the payments
--     "order ID" carries the shipment id for these rows, which is Amazon's
--     usual behaviour but is unverified here.
