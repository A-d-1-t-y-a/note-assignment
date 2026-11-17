-- Reference data that is not part of the Amazon mapping configs but is still
-- data, not code: report column aliases and the Summary sheet layout.
SET search_path = recon, public;

-- Payments report header -> amount_field name used in amazon_payment_configs_*.csv
INSERT INTO payment_column_alias (source_header, amount_field) VALUES
 ('product sales',             'product_sales'),
 ('shipping credits',          'shipping_credits'),
 ('gift wrap credits',         'gift_wrap_credits'),
 ('promotional rebates',       'promotional_rebates'),
 ('sales tax collected',       'sales_tax_collected'),
 ('low value goods',           'low_value_goods'),
 ('marketplace withheld tax',  'marketplace_withheld_tax'),
 ('selling fees',              'selling_fees'),
 ('fba fees',                  'fba_fees'),
 ('fulfilment by amazon fees', 'fba_fees'),
 ('fulfillment by amazon fees','fba_fees'),
 ('other transaction fees',    'other_transaction_fees'),
 ('other',                     'other'),
 ('total',                     'total');

-- Summary sheet lines, in the order of amazon_sample_output_report.xlsx
INSERT INTO summary_line (line_code, section, label, sort_order) VALUES
 ('S_PRODUCT',      'Sales',    'Product Charges',               1),
 ('S_TAX',          'Sales',    'Tax',                           2),
 ('S_SHIPPING',     'Sales',    'Shipping',                      3),
 ('S_AMAZON_FEES',  'Sales',    'Amazon fees',                   4),
 ('S_INV_REIMB',    'Sales',    'Inventory Reimbursements',      5),
 ('S_CROSS_DEBT',   'Sales',    'Cross-account Debt Adjustment', 6),
 ('S_OTHER',        'Sales',    'Other',                         7),
 ('S_FBA_FEES',     'Sales',    'FBA Fees',                      8),
 ('S_MICRO_FAILED', 'Sales',    'Micro Deposit (Failed)',        9),
 ('R_EXPENSES',     'Refunds',  'Refund expenses',              10),
 ('R_SALES',        'Refunds',  'Refunded sales',               11),
 ('E_PROMO',        'Expenses', 'Promo rebates',                12),
 ('E_FBA',          'Expenses', 'FBA fees',                     13),
 ('E_ADS',          'Expenses', 'Cost of Advertising',          14),
 ('E_SHIPPING',     'Expenses', 'Shipping Charges',             15),
 ('E_AMAZON_FEES',  'Expenses', 'Amazon fees',                  16),
 ('E_REV_REIMB',    'Expenses', 'Reversed Reimbursements',      17),
 ('E_CROSS_DEBT',   'Expenses', 'Cross-account Debt Adjustment',18),
 ('E_OTHER',        'Expenses', 'Other',                        19),
 ('E_MICRO',        'Expenses', 'Micro Deposit',                20),
 ('P_PAID',         'Paid To Amazon', 'Paid To Amazon',         21),
 -- balance-sheet style items: shown as memo lines below the summary
 ('M_RESERVE',      'Memo',     'Current reserve amount',       22),
 ('M_BEGINNING',    'Memo',     'Beginning balance (previous reserve)', 23),
 ('M_CARRIED',      'Memo',     'Amazon carried forward',       24);

-- summary field (as written in the mapping configs) -> Summary line.
-- sign = ANY routes both signs; POS/NEG split one field over two lines.
INSERT INTO summary_field_map (summary_field, sign, line_code) VALUES
 ('sales_product_charges',                  'ANY', 'S_PRODUCT'),
 ('sales_tax',                              'ANY', 'S_TAX'),
 ('sales_shipping',                         'ANY', 'S_SHIPPING'),
 ('sales_amazon_fees',                      'ANY', 'S_AMAZON_FEES'),
 ('sales_inventory_reimbursements',         'ANY', 'S_INV_REIMB'),
 ('sales_cross_account_debt_adjustment',    'ANY', 'S_CROSS_DEBT'),
 ('sales_other',                            'ANY', 'S_OTHER'),
 ('sales_fba_fees',                         'ANY', 'S_FBA_FEES'),
 ('bank_account_transfer_round_off',        'POS', 'S_MICRO_FAILED'),
 ('bank_account_transfer_round_off',        'NEG', 'E_MICRO'),
 ('refunded_expenses',                      'ANY', 'R_EXPENSES'),
 ('refunded_sales',                         'ANY', 'R_SALES'),
 ('total_refund_expense_or_sales_amt',      'POS', 'R_EXPENSES'),
 ('total_refund_expense_or_sales_amt',      'NEG', 'R_SALES'),
 ('total_adjustment_other_buyer_recharge_amt','POS','S_OTHER'),
 ('total_adjustment_other_buyer_recharge_amt','NEG','E_OTHER'),
 ('expenses_promotional_rebates',           'ANY', 'E_PROMO'),
 ('expenses_fba_fees',                      'ANY', 'E_FBA'),
 ('expenses_cost_of_advertising',           'ANY', 'E_ADS'),
 ('expenses_shipping_charges',              'ANY', 'E_SHIPPING'),
 ('expenses_amazon_fees',                   'ANY', 'E_AMAZON_FEES'),
 ('expenses_reversed_reimbursements',       'ANY', 'E_REV_REIMB'),
 ('expenses_cross_account_debt_adjustment', 'ANY', 'E_CROSS_DEBT'),
 ('expenses_other',                         'ANY', 'E_OTHER'),
 ('paid_to_amazon',                         'ANY', 'P_PAID'),
 ('current_reserve_amount',                 'ANY', 'M_RESERVE'),
 ('beginning_balance',                      'ANY', 'M_BEGINNING'),
 ('amazon_carried_forward',                 'ANY', 'M_CARRIED');
