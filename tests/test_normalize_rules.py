from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from recon.normalize import (match_key, parse_amount, parse_payment_datetime,
                             parse_settlement_datetime)
from recon.rules import RuleSet, build_record_ref, make_rule, template_unknown_tokens


# ---------------------------------------------------------------- normalize
@pytest.mark.parametrize("raw,cfg", [
    ("Service fee", "SERVICE_FEE"),
    ("other-transaction", "OTHER-TRANSACTION"),
    ("Fulfilment by Amazon transaction fees", "FULFILMENT_BY_AMAZON_TRANSACTION_FEES"),
    ("FBA Removal Order: Disposal Fee", "FBA_REMOVAL_ORDER:_DISPOSAL_FEE"),
    ("FBA Long-Term Storage Fee", "FBA_LONG_TERM_STORAGE_FEE"),
    ("Multi-Channel Fulfilment Inventory Reimbursement - Lost",
     "MULTI-CHANNEL_FULFILMENT_INVENTORY_REIMBURSEMENT_–_LOST"),
])
def test_match_key_is_case_and_separator_insensitive(raw, cfg):
    assert match_key(raw) == match_key(cfg)


def test_parse_amount():
    assert parse_amount("-133,756.51") == Decimal("-133756.51")
    assert parse_amount("") == Decimal("0.00")
    assert parse_amount("24.5") == Decimal("24.50")
    with pytest.raises(ValueError):
        parse_amount("abc")


def test_payment_datetime_converts_gmt9_to_utc():
    dt = parse_payment_datetime("17 July 2026 4:26:32 pm GMT+9")
    assert dt == datetime(2026, 7, 17, 7, 26, 32, tzinfo=timezone.utc)
    # crosses midnight backwards into the previous UTC day
    assert parse_payment_datetime("18 July 2026 2:14:53 am GMT+9").date() == date(2026, 7, 17)
    # abbreviated month, 12 am edge
    assert parse_payment_datetime("1 Aug 2026 12:05:00 am GMT+9") == datetime(2026, 7, 31, 15, 5, tzinfo=timezone.utc)
    assert parse_payment_datetime("") is None
    assert parse_payment_datetime("3 Sept 2026 1:00:00 pm GMT+9").month == 9
    with pytest.raises(ValueError):
        parse_payment_datetime("9 Julember 2026 12:00:56 am GMT+9")


def test_settlement_datetime():
    assert parse_settlement_datetime("17.07.2026 07:26:32 UTC") == datetime(2026, 7, 17, 7, 26, 32, tzinfo=timezone.utc)
    assert parse_settlement_datetime("17.07.2026").date() == date(2026, 7, 17)


# ---------------------------------------------------------------- rules
def _p(rid, tt, desc, field, ref="txn_ref+sku+date", pos="x", neg="x"):
    return make_rule("payment_mapping_config", rid, (tt, desc), field, ref, pos, neg)


def test_specific_beats_wildcard_beats_fallback():
    rs = RuleSet([_p(1, "ORDER", "any", "total", pos="wild"),
                  _p(2, "ORDER", "SPECIAL_THING", "total", pos="exact"),
                  _p(3, "", "any", "total", pos="fallback")], True)
    assert [r.rule_id for r in rs.match(("Order", "Special thing"), "total")[0]] == [2]
    assert [r.rule_id for r in rs.match(("Order", "whatever"), "total")[0]] == [1]
    assert [r.rule_id for r in rs.match(("Brand new type", "x"), "total")[0]] == [3]


def test_prefix_match_for_variable_descriptions():
    rs = RuleSet([_p(62, "TRANSFER", "TO_ACCOUNT_ENDING", "total", ref="TRANSFER+description"),
                  _p(109, "", "any", "total", pos="expenses_amazon_fees")], True)
    rules, tier = rs.match(("Transfer", "To account ending with: 334"), "total")
    assert [r.rule_id for r in rules] == [62]
    assert "prefix" in tier


def test_duplicate_rules_are_all_returned():
    rs = RuleSet([_p(70, "ORDER", "any", "sales_tax_collected", pos="sales_product_charges"),
                  _p(71, "ORDER", "any", "sales_tax_collected", pos="sales_shipping")], True)
    assert {r.rule_id for r in rs.match(("Order", "x"), "sales_tax_collected")[0]} == {70, 71}


def test_amount_field_must_match():
    rs = RuleSet([_p(1, "ORDER", "any", "product_sales")], True)
    assert rs.match(("Order", "x"), "fba_fees")[0] == []


def test_sign_routing():
    r = _p(1, "ADJUSTMENT", "X", "other", pos="sales_inventory_reimbursements",
           neg="expenses_reversed_reimbursements")
    assert r.summary_field_for(Decimal("1")) == "sales_inventory_reimbursements"
    assert r.summary_field_for(Decimal("-1")) == "expenses_reversed_reimbursements"
    assert r.summary_field_for(Decimal("0")) is None
    assert _p(2, "A", "B", "other", pos="", neg="").summary_field_for(Decimal("5")) is None


def test_settlement_three_key_match():
    s = make_rule("settlement_mapping_config", 1, ("ORDER", "ITEMPRICE", "PRINCIPAL"), None,
                  "txn_ref+sku+date", "sales_product_charges", "sales_product_charges")
    w = make_rule("settlement_mapping_config", 2, ("OTHER-TRANSACTION", "INBOUND_DEFECT_FEE", "any"), None,
                  "shipment_id+INBOUND_DEFECT_FEE", "e", "e")
    rs = RuleSet([s, w], False)
    assert rs.match(("Order", "ItemPrice", "Principal"))[0] == [s]
    assert rs.match(("other-transaction", "Inbound Defect Fee", "anything"))[0] == [w]
    assert rs.match(("Order", "ItemPrice", "Tax"))[0] == []


def test_build_record_ref_resolves_fields_and_keeps_literals():
    v = {"txn_ref": "249-1", "sku": "SKU1", "date": "2026-07-17", "settlement_id": "123"}
    assert build_record_ref("txn_ref+sku+date", v) == "249-1|SKU1|2026-07-17"
    assert build_record_ref("LOST:WAREHOUSE+sku+settlement_id+date", v) == "LOST:WAREHOUSE|SKU1|123|2026-07-17"
    assert build_record_ref("GENERAL ADJUSTMENT+sku", v) == "GENERAL ADJUSTMENT|SKU1"


def test_unknown_token_detection():
    assert template_unknown_tokens("ADJUSTMENT_OTHER+settlement_id+date+record_type+settlement_id") == ["record_type"]
    assert template_unknown_tokens("txn_ref+FBA_DISPOSAL_FEE+settlement_id+date") == []
