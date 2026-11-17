"""End-to-end test on small fixtures, against a real PostgreSQL database
(`recon_test` on the same server as DATABASE_URL / the embedded pgserver)."""
import os
from decimal import Decimal
from pathlib import Path

import openpyxl
import pytest

import recon  # noqa: F401  (exposes libpq)
from recon import configs, db
from recon.ingest import ingest
from recon.reconcile import reconcile, running_summary_by_line, summary_by_line
from recon.report import write_report

PAY_HEADER = ('"date/time","settlement ID","type","order ID","sku","description","quantity","marketplace",'
              '"fulfilment","order city","order state","order postal","product sales","shipping credits",'
              '"gift wrap credits","promotional rebates","sales tax collected","low value goods","selling fees",'
              '"fulfilment by amazon fees","other transaction fees","other","total","Transaction status",'
              '"Transaction Release Date"')


def prow(dt, sid, typ, oid, sku, desc, amounts, total, status, rel):
    ps, sh, pr, tx, sf, fba, other = amounts
    return (f'"{dt}","{sid}","{typ}","{oid}","{sku}","{desc}","1","amazon.com.au","Amazon","X","NSW","2000",'
            f'"{ps}","{sh}","0","{pr}","{tx}","0","{sf}","{fba}","0","{other}","{total}","{status}","{rel}"')


PAYMENTS = "﻿" + "\n".join([
    '"Includes Amazon Marketplace transactions"',
    '"All amounts in AUD, unless specified"',
    PAY_HEADER,
    prow("9 July 2026 12:00:56 am GMT+9", "S1", "Order", "A", "SKU1", "Mask", (20, 3, -3, 2, -3, -6, 0), "13.00",
         "Released", "17 July 2026 4:26:32 pm GMT+9"),
    prow("30 July 2026 1:00:00 pm GMT+9", "S1", "Order", "B", "SKU1", "Mask", (10, 0, 0, 0, 0, 0, 0), "10.00",
         "Deferred", ""),
    prow("18 July 2026 4:17:09 pm GMT+9", "S1", "Transfer", "", "", "To account ending with: 334",
         (0, 0, 0, 0, 0, 0, "-1,000.00"), "-1,000.00", "Released", "18 July 2026 4:17:09 pm GMT+9"),
    prow("2 July 2026 1:00:00 pm GMT+9", "S0", "Order", "C", "SKU1", "Mask", (5, 0, 0, 0, 0, 0, 0), "5.00",
         "Released", "2 July 2026 1:00:00 pm GMT+9"),
    prow("20 July 2026 1:00:00 pm GMT+9", "S1", "Order", "Z", "SKU1", "Mask", (0, 0, 0, 0, 0, 0, 0), "0",
         "Released", "20 July 2026 1:00:00 pm GMT+9"),
]) + "\n"

SET_HEADER = ["settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date", "total-amount",
              "currency", "transaction-type", "order-id", "merchant-order-id", "adjustment-id", "shipment-id",
              "marketplace-name", "amount-type", "amount-description", "amount", "fulfillment-id", "posted-date",
              "posted-date-time", "order-item-code", "merchant-order-item-id", "merchant-adjustment-item-id",
              "sku", "quantity-purchased", "promotion-id"]


def srow(**kw):
    return "\t".join(str(kw.get(h.replace("-", "_"), "")) for h in SET_HEADER)


def order_line(at, ad, amt):
    return srow(settlement_id="S1", transaction_type="Order", order_id="A", amount_type=at,
                amount_description=ad, amount=amt, posted_date="17.07.2026",
                posted_date_time="17.07.2026 07:26:32 UTC", sku="SKU1", quantity_purchased=1)


SETTLEMENTS = "\n".join([
    "\t".join(SET_HEADER),
    srow(settlement_id="S1", settlement_start_date="17.07.2026 07:06:34 UTC",
         settlement_end_date="31.07.2026 07:06:35 UTC", deposit_date="02.08.2026 07:06:35 UTC",
         total_amount="8.00", currency="AUD"),
    order_line("ItemPrice", "Principal", "20.00"),
    order_line("ItemPrice", "Shipping", "3.00"),
    order_line("ItemPrice", "Tax", "2.00"),
    order_line("Promotion", "Shipping", "-3.00"),
    order_line("ItemFees", "Commission", "-3.00"),
    order_line("ItemFees", "FBAPerUnitFulfillmentFee", "-6.00"),
    srow(settlement_id="S1", transaction_type="other-transaction", amount_type="other-transaction",
         amount_description="Subscription Fee", amount="-5.00", posted_date="26.07.2026",
         posted_date_time="26.07.2026 03:59:44 UTC"),
]) + "\n"

PAY_CFG = """transaction_type,description,amount_field,record_ref,to_summary_field_when_positive_amount,to_summary_field_when_negative_amount
ORDER,any,product_sales,txn_ref+sku+date,sales_product_charges,sales_product_charges
ORDER,any,shipping_credits,txn_ref+sku+date,sales_shipping,sales_shipping
ORDER,any,promotional_rebates,txn_ref+sku+date,expenses_promotional_rebates,expenses_promotional_rebates
ORDER,any,sales_tax_collected,txn_ref+sku+date,sales_product_charges,sales_product_charges
ORDER,any,sales_tax_collected,txn_ref+sku+date,sales_shipping,sales_shipping
ORDER,any,selling_fees,txn_ref+sku+date,expenses_amazon_fees,expenses_amazon_fees
ORDER,any,fba_fees,txn_ref+sku+date,expenses_amazon_fees,expenses_amazon_fees
ORDER,any,total,txn_ref+sku+date,,
TRANSFER,TO_ACCOUNT_ENDING,total,TRANSFER+description+settlement_id+date,,
,any,total,txn_ref+settlement_id+date,expenses_amazon_fees,expenses_amazon_fees
,any,other,txn_ref+settlement_id+date,,
"""
SET_CFG = """transaction_type,amount_type,amount_description,record_ref,to_summary_field_when_positive_amount,to_summary_field_when_negative_amount
ORDER,ITEMPRICE,PRINCIPAL,txn_ref+sku+date,sales_product_charges,sales_product_charges
ORDER,ITEMPRICE,SHIPPING,txn_ref+sku+date,sales_shipping,sales_shipping
ORDER,ITEMPRICE,TAX,txn_ref+sku+date,sales_product_charges,sales_product_charges
ORDER,PROMOTION,SHIPPING,txn_ref+sku+date,expenses_promotional_rebates,expenses_promotional_rebates
ORDER,ITEMFEES,COMMISSION,txn_ref+sku+date,expenses_amazon_fees,expenses_amazon_fees
ORDER,ITEMFEES,FBAPERUNITFULFILLMENTFEE,txn_ref+sku+date,expenses_amazon_fees,expenses_amazon_fees
OTHER-TRANSACTION,OTHER-TRANSACTION,SUBSCRIPTION_FEE,SERVICE_FEE_SUBSCRIPTION+settlement_id+date,expenses_amazon_fees,expenses_amazon_fees
"""


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    import psycopg
    from psycopg.conninfo import conninfo_to_dict, make_conninfo

    base = db.database_url()
    params = conninfo_to_dict(base)
    with psycopg.connect(base, autocommit=True) as c:
        if not c.execute("SELECT 1 FROM pg_database WHERE datname='recon_test'").fetchone():
            c.execute("CREATE DATABASE recon_test")
    params["dbname"] = "recon_test"
    url = make_conninfo(**params)
    old = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    d = tmp_path_factory.mktemp("fx")
    (d / "p.csv").write_text(PAYMENTS, encoding="utf-8")
    (d / "s.txt").write_text(SETTLEMENTS, encoding="utf-8")
    (d / "pc.csv").write_text(PAY_CFG, encoding="utf-8")
    (d / "sc.csv").write_text(SET_CFG, encoding="utf-8")
    with psycopg.connect(url) as c:
        configs.init_schema(c)
        configs.seed_configs(c, d / "pc.csv", d / "sc.csv")
        c.fx = d
        yield c
    if old is None:
        os.environ.pop("DATABASE_URL", None)
    else:
        os.environ["DATABASE_URL"] = old


def _run(c):
    stats = ingest(c, c.fx / "p.csv", c.fx / "s.txt")
    reconcile(c)
    return stats


def q(c, sql):
    return c.execute(sql).fetchall()


def test_ingest_single_table_with_traceability(conn):
    stats = _run(conn)
    assert stats["PAYMENT"]["rows"] == 5
    assert stats["SETTLEMENT"]["rows"] == 8           # incl. header row
    assert stats["PAYMENT"]["multi_rule_lines"] == 2  # duplicated sales_tax_collected rule
    rows = q(conn, "SELECT source, count(*) FROM recon.raw_record GROUP BY 1 ORDER BY 1")
    assert rows == [("PAYMENT", 5), ("SETTLEMENT", 8)]
    # line numbers point at the physical file line (3 preamble/header lines first)
    ln, raw = q(conn, "SELECT line_no, raw FROM recon.raw_record WHERE source='PAYMENT' AND txn_ref='A'")[0]
    assert ln == 4 and raw["date/time"] == "9 July 2026 12:00:56 am GMT+9"


def test_release_date_in_utc_drives_the_key(conn):
    assert q(conn, "SELECT record_ref FROM recon.raw_record WHERE source='PAYMENT' AND txn_ref='A'")[0][0] \
        == "A|SKU1|2026-07-17"


def test_reconciliation_statuses_and_reasons(conn):
    res = dict(q(conn, "SELECT record_ref, status || ' / ' || coalesce(reason,'') FROM recon.recon_result"))
    assert res["A|SKU1|2026-07-17"] == "RECONCILED / "
    assert res["Z|SKU1|2026-07-20"].startswith("UNRECONCILED_PAYMENT / NO SETTLEMENT COUNTERPART")
    assert res["TRANSFER|TOACCOUNTENDINGWITH334|S1|2026-07-18"].startswith("UNRECONCILED_PAYMENT / PAYOUT")
    assert res["B|SKU1|2026-07-30"].startswith("UNRECONCILED_PAYMENT / DEFERRED")
    assert res["C|SKU1|2026-07-02"].startswith("UNRECONCILED_PAYMENT / OTHER SETTLEMENT")
    assert res["SERVICE_FEE_SUBSCRIPTION|S1|2026-07-26"].startswith("UNRECONCILED_SETTLEMENT")


def test_summary_scope_routing_and_running_summary(conn):
    s = summary_by_line(conn)
    assert s["S_PRODUCT"] == {"PAYMENT": Decimal("22.00"), "SETTLEMENT": Decimal("22.00")}
    # duplicated tax rule double counts 2.00 into Shipping on the payment side
    assert s["S_SHIPPING"] == {"PAYMENT": Decimal("5.00"), "SETTLEMENT": Decimal("3.00")}
    # the transfer is NOT routed into Amazon fees by the catch-all rule (prefix match)
    assert s["E_AMAZON_FEES"]["PAYMENT"] == Decimal("-9.00")
    assert s["E_AMAZON_FEES"]["SETTLEMENT"] == Decimal("-14.00")
    assert running_summary_by_line(conn) == s


def test_config_fix_is_a_data_change_and_reingest_is_idempotent(conn):
    findings = configs.validate_configs(conn)
    assert any(f["check"] == "duplicate match key" for f in findings)
    conn.execute("DELETE FROM recon.payment_mapping_config WHERE amount_field='sales_tax_collected' "
                 "AND to_summary_field_when_positive_amount='sales_shipping'")
    conn.commit()
    _run(conn)
    first = q(conn, "SELECT count(*), sum(amount) FROM recon.record_line")
    _run(conn)
    assert q(conn, "SELECT count(*), sum(amount) FROM recon.record_line") == first
    s = summary_by_line(conn)
    assert s["S_SHIPPING"] == {"PAYMENT": Decimal("3.00"), "SETTLEMENT": Decimal("3.00")}
    audit = q(conn, "SELECT op, rule_id FROM recon.mapping_config_audit")
    assert audit == [("DELETE", 5)]


def test_report_is_written(conn, tmp_path):
    out = tmp_path / "r.xlsx"
    write_report(conn, out, "test")
    wb = openpyxl.load_workbook(out, read_only=True)
    assert wb.sheetnames[:2] == ["Summary", "Consolidated Data"]
    summary = {r[1]: r[2:5] for r in wb["Summary"].iter_rows(values_only=True) if r and len(r) > 4 and r[1]}
    assert summary["Product Charges"] == (22, 22, 0)
    assert summary["Check: proceeds - Amazon total-amount (must be 0)"][1] == 0
    cons = list(wb["Consolidated Data"].iter_rows(min_row=11, values_only=True))
    statuses = [r[next(i for i, h in enumerate(next(wb["Consolidated Data"].iter_rows(min_row=10, max_row=10,
                                                                                          values_only=True)))
                     if h == "Reconciliation Status")] for r in cons]
    # reconciled first, then unreconciled payments, then unreconciled settlements
    assert statuses == sorted(statuses, key=["RECONCILED", "UNRECONCILED_PAYMENT", "UNRECONCILED_SETTLEMENT"].index)


def test_unknown_payment_status_is_excluded_from_summary(conn, tmp_path):
    """Only Released payments belong to the settlement accounting period."""
    pending = tmp_path / "pending.csv"
    pending.write_text(PAYMENTS.replace(
        '"Released","17 July 2026 4:26:32 pm GMT+9"',
        '"Pending","17 July 2026 4:26:32 pm GMT+9"'), encoding="utf-8")
    assert pending.read_text(encoding="utf-8") != PAYMENTS
    ingest(conn, pending, conn.fx / "s.txt")
    reconcile(conn)
    summary = summary_by_line(conn)
    assert summary.get("S_PRODUCT", {}).get("PAYMENT", Decimal("0")) == 0
    assert summary["S_PRODUCT"]["SETTLEMENT"] == Decimal("22.00")
    assert running_summary_by_line(conn) == summary
