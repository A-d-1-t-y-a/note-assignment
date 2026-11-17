"""Excel report: Summary, Consolidated Data, Variance Drilldown, Tie-out, Config Findings.

The report only reads from the DB; it contains no business adjustments.
"""
from __future__ import annotations

import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from .configs import validate_configs
from .reconcile import IN_SCOPE, running_summary_by_line, summary_by_line

Z = Decimal("0")
NUM = '#,##0.00;[Red]-#,##0.00'
BOLD = Font(bold=True)
HDR_FILL = PatternFill("solid", fgColor="DDEBF7")
SEC_FILL = PatternFill("solid", fgColor="F2F2F2")
BAD_FILL = PatternFill("solid", fgColor="FCE4D6")

PAYMENT_COLS = [  # (consolidated header, payments raw header or special)
    ("Date", "@recon_date"), ("date/time", "date/time"), ("Transaction Status", "Transaction status"),
    ("Transaction Release Date", "Transaction Release Date"), ("settlement id", "settlement ID"),
    ("type", "type"), ("order id", "order ID"), ("sku", "sku"), ("description", "description"),
    ("Quantity", "@qty:quantity"), ("marketplace", "marketplace"), ("account type", "account type"),
    ("fulfillment", "fulfilment"), ("order city", "order city"), ("order state", "order state"),
    ("order postal", "order postal"), ("tax collection model", "tax collection model"),
]
SETTLEMENT_COLS = [
    "settlement-id", "settlement-start-date", "settlement-end-date", "deposit-date", "total-amount",
    "currency", "transaction-type", "order-id", "merchant-order-id", "adjustment-id", "shipment-id",
    "marketplace-name", "fulfillment-id", "posted-date", "posted-date-time", "order-item-code",
    "merchant-order-item-id", "merchant-adjustment-item-id", "sku", "@qty:quantity-purchased", "promotion-id",
]
SETTLEMENT_HEADER_FIELDS = {"settlement-start-date", "settlement-end-date", "deposit-date",
                            "total-amount", "currency"}
CONSOLIDATED_LABELS = {  # consolidated column captions per line_code (sample wording)
    "S_PRODUCT": "Product Charges", "S_TAX": "Tax", "S_SHIPPING": "Shipping",
    "S_AMAZON_FEES": "Amazon Fees", "S_INV_REIMB": "Inventory Reimbursements",
    "S_CROSS_DEBT": "Cross-Account Debt Adjustment", "S_OTHER": "Other", "S_FBA_FEES": "FBA Fees",
    "S_MICRO_FAILED": "Micro Deposit (Failed)", "R_EXPENSES": "Refund Expenses",
    "R_SALES": "Refunded Sales", "E_PROMO": "Promo Rebates", "E_FBA": "FBA Fees",
    "E_ADS": "Cost of Advertising", "E_SHIPPING": "Shipping Charges", "E_AMAZON_FEES": "Amazon Fees",
    "E_REV_REIMB": "Reversed Reimbursements", "E_CROSS_DEBT": "Cross-Account Debt Adjustment",
    "E_OTHER": "Other", "E_MICRO": "Micro Deposit", "P_PAID": "Paid To Amazon",
}
STATUS_ORDER = {"RECONCILED": 0, "UNRECONCILED_PAYMENT": 1, "UNRECONCILED_SETTLEMENT": 2}


def _q(conn, sql, params=None):
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def _cell(ws, value, bold=False, num=False, fill=None, wrap=False):
    c = WriteOnlyCell(ws, value=float(value) if isinstance(value, Decimal) else value)
    if bold:
        c.font = BOLD
    if num or isinstance(value, Decimal):
        c.number_format = NUM
    if fill:
        c.fill = fill
    if wrap:
        c.alignment = Alignment(wrap_text=True, vertical="top")
    return c


# ---------------------------------------------------------------------------
def _lines(conn):
    return _q(conn, "SELECT line_code, section, label FROM recon.summary_line ORDER BY sort_order")


def write_summary(wb, conn, title):
    ws = wb.create_sheet("Summary")
    ws.column_dimensions["A"].width = 4
    ws.column_dimensions["B"].width = 58
    for col in "CDE":
        ws.column_dimensions[col].width = 18
    s = summary_by_line(conn)
    lines = _lines(conn)
    get = lambda code, src: s.get(code, {}).get(src, Z)

    def row(label, p, st, bold=False, fill=None, note=None):
        cells = [_cell(ws, ""), _cell(ws, label, bold, fill=fill)]
        if p is None:
            cells += [_cell(ws, None), _cell(ws, None), _cell(ws, None)]
        else:
            diff = (p or Z) - (st or Z)
            cells += [_cell(ws, p, bold, True, fill), _cell(ws, st, bold, True, fill),
                      _cell(ws, diff, bold, True, BAD_FILL if diff != 0 else fill)]
        if note:
            cells.append(_cell(ws, note))
        ws.append(cells)

    ws.append([_cell(ws, ""), _cell(ws, title, True), _cell(ws, "Payments", True, fill=HDR_FILL),
               _cell(ws, "Settlements", True, fill=HDR_FILL),
               _cell(ws, "Payments - Settlements", True, fill=HDR_FILL)])
    ws.append([])
    ws.append([])
    by_sec = defaultdict(list)
    for code, sec, label in lines:
        by_sec[sec].append((code, label))
    totals = {"PAYMENT": Z, "SETTLEMENT": Z}
    for sec in ("Sales", "Refunds", "Expenses"):
        sp = sum((get(c, "PAYMENT") for c, _ in by_sec[sec]), Z)
        ss = sum((get(c, "SETTLEMENT") for c, _ in by_sec[sec]), Z)
        totals["PAYMENT"] += sp
        totals["SETTLEMENT"] += ss
        row(sec, sp, ss, bold=True, fill=SEC_FILL)
        for code, label in by_sec[sec]:
            row(label, get(code, "PAYMENT"), get(code, "SETTLEMENT"))
        ws.append([])
    ws.append([])
    ws.append([])  # rows 29-31 as in the sample
    pp, ps = get("P_PAID", "PAYMENT"), get("P_PAID", "SETTLEMENT")
    totals["PAYMENT"] += pp
    totals["SETTLEMENT"] += ps
    row("Paid To Amazon", pp, ps, bold=True, fill=SEC_FILL)
    ws.append([])
    ws.append([])

    # ---- tie-out block (below the sample structure) -------------------------
    memo = {src: sum((get(c, src) for c, _ in by_sec["Memo"]), Z) for src in totals}
    net = dict(_q(conn, f"""SELECT r.source, sum(r.net_amount) FROM recon.raw_record r
                            WHERE {IN_SCOPE} GROUP BY 1"""))
    transfers = dict(_q(conn, f"""SELECT r.source, sum(r.net_amount) FROM recon.raw_record r
                                  WHERE {IN_SCOPE} AND r.transaction_type_key = 'TRANSFER' GROUP BY 1"""))
    header_total = _q(conn, """SELECT coalesce(sum(net_amount),0) FROM recon.raw_record
                               WHERE source='SETTLEMENT' AND is_header
                                 AND settlement_id IN (SELECT settlement_id FROM recon.recon_scope)""")[0][0]
    row("Tie-out to Amazon's own settlement total", None, None, bold=True, fill=HDR_FILL)
    row("Summary net (Sales + Refunds + Expenses + Paid To Amazon)", totals["PAYMENT"], totals["SETTLEMENT"])
    for code, label in by_sec["Memo"]:
        row(f"Memo (balance item, not P&L): {label}", get(code, "PAYMENT"), get(code, "SETTLEMENT"))
    not_routed = {src: net.get(src, Z) - totals[src] - memo[src] for src in totals}
    row("Not routed to any Summary line (see Tie-out sheet)", not_routed["PAYMENT"], not_routed["SETTLEMENT"])
    row("= Net of all in-scope records", net.get("PAYMENT", Z), net.get("SETTLEMENT", Z), bold=True)
    row("  less bank transfers / payouts (Transfer rows)", transfers.get("PAYMENT", Z), transfers.get("SETTLEMENT", Z))
    np_ = net.get("PAYMENT", Z) - transfers.get("PAYMENT", Z)
    ns_ = net.get("SETTLEMENT", Z) - transfers.get("SETTLEMENT", Z)
    row("= Settlement proceeds per source", np_, ns_, bold=True)
    row("Amazon settlement total-amount (settlement header row)", header_total, header_total)
    row("Check: proceeds - Amazon total-amount (must be 0)", np_ - header_total, ns_ - header_total, bold=True)
    ws.append([])
    scope = ", ".join(r[0] for r in _q(conn, "SELECT settlement_id FROM recon.recon_scope ORDER BY 1"))
    ws.append([_cell(ws, ""), _cell(ws, f"Scope: settlement {scope}. Payments column = payment rows of that "
                                        f"settlement with status Released; Settlements column = settlement "
                                        f"rows. Each column is computed only from its own source.")])
    rs = running_summary_by_line(conn)
    ok = all(rs.get(c, {}) == s.get(c, {}) for c, _, _ in lines)
    ws.append([_cell(ws, ""), _cell(ws, "Summary accumulated during ingestion equals SQL re-aggregation: "
                                        + ("YES" if ok else "NO - investigate"))])
    return totals


# ---------------------------------------------------------------------------
def _join_distinct(values):
    seen = []
    for v in values:
        v = "" if v is None else str(v).strip()
        if v and v not in seen:
            seen.append(v)
    return "; ".join(seen)


def write_consolidated(wb, conn):
    ws = wb.create_sheet("Consolidated Data")
    line_codes = [c for c, sec, _ in _lines(conn) if sec != "Memo"]
    sections = {c: sec for c, sec, _ in _lines(conn)}

    results = {r[0]: r for r in _q(conn, """SELECT record_ref, status, in_scope, reason, payment_rows,
                                            settlement_rows, payment_net, settlement_net, diff
                                            FROM recon.recon_result""")}
    headers = {r[0]: r[1] for r in _q(conn, """SELECT settlement_id, raw FROM recon.raw_record
                                              WHERE source='SETTLEMENT' AND is_header""")}
    rows = defaultdict(lambda: {"PAYMENT": [], "SETTLEMENT": []})
    for ref, src, fname, line_no, raw, net, rdate in _q(conn, """
            SELECT r.record_ref, r.source, f.file_name, r.line_no, r.raw, r.net_amount, r.recon_date
            FROM recon.raw_record r JOIN recon.source_file f USING (file_id)
            WHERE NOT r.is_header ORDER BY r.record_id"""):
        rows[ref][src].append((fname, line_no, raw if isinstance(raw, dict) else json.loads(raw), net, rdate))
    amounts = defaultdict(Z.__class__)
    for ref, src, code, amt in _q(conn, """
            SELECT l.record_ref, l.source, m.line_code, sum(l.amount)
            FROM recon.record_line l JOIN recon.summary_field_map m ON m.summary_field = l.summary_field
             AND (m.sign='ANY' OR (m.sign='POS' AND l.amount>0) OR (m.sign='NEG' AND l.amount<0))
            GROUP BY 1,2,3"""):
        amounts[(ref, src, code)] = amt
    routing = defaultdict(list)
    for ref, src, desc in _q(conn, """
            SELECT l.record_ref, l.source,
                   string_agg(coalesce(CASE WHEN l.source='SETTLEMENT'
                                            THEN r.amount_type || '/' || r.description ELSE l.amount_field END, '')
                              || '=' || l.amount::text || ' -> ' || coalesce(l.summary_field, '(not summarised)')
                              || ' [' || coalesce(split_part(l.rule_table,'_',1) || ' rule #' || l.rule_id, 'NO RULE') || ']',
                              '; ' ORDER BY l.line_id)
            FROM recon.record_line l JOIN recon.raw_record r USING (record_id)
            GROUP BY 1,2"""):
        routing[(ref, src)] = desc

    # banner rows 1-10, same structure as the sample (shifted right by 1 for record_ref)
    n_pay = len(PAYMENT_COLS) + 1 + len(line_codes)
    n_set = len(SETTLEMENT_COLS) + 1 + len(line_codes)
    ws.append([_cell(ws, "      Data From Amazon", True)])
    ws.append([])
    ws.append([_cell(ws, "DS1 ="), _cell(ws, "Payment Report")])
    ws.append([_cell(ws, "DS2 ="), _cell(ws, "Settlement Report")])
    ws.append([])
    ws.append([])
    ws.append([""] + ["Payment Report"] * n_pay + ["Settlement Report"] * n_set + [""] * (2 + len(line_codes) + 6))
    ws.append([""] + [""] * (len(PAYMENT_COLS) + 1) + ["Summary"] * len(line_codes)
              + [""] * (len(SETTLEMENT_COLS) + 1) + ["Summary"] * len(line_codes)
              + ["", ""] + ["Summary Mismatch"] * len(line_codes) + ["Audit"] * 6)
    sec_row = [sections[c] if sections[c] != "Paid To Amazon" else "Paid To Amazon" for c in line_codes]
    ws.append([""] + [""] * (len(PAYMENT_COLS) + 1) + sec_row + [""] * (len(SETTLEMENT_COLS) + 1) + sec_row
              + ["", ""] + sec_row + [""] * 6)
    hdr = (["record_ref"] + [h for h, _ in PAYMENT_COLS] + ["Total"]
           + [CONSOLIDATED_LABELS[c] for c in line_codes]
           + [c.replace("@qty:", "").replace("quantity-purchased", "quantity") for c in SETTLEMENT_COLS] + ["Total"]
           + [CONSOLIDATED_LABELS[c] for c in line_codes]
           + ["Reconciliation Status", "total (DS1-DS2)"] + [f"{CONSOLIDATED_LABELS[c]} (DS1-DS2)" for c in line_codes]
           + ["In Scope", "Reason", "Payment source rows (file:line)", "Settlement source rows (file:line)",
              "Payment routing (field=amount -> summary field [rule])",
              "Settlement routing (amount-type/description=amount -> summary field [rule])"])
    ws.append([_cell(ws, h, True, fill=HDR_FILL) for h in hdr])

    def sort_key(ref):
        r = results[ref]
        any_rows = rows[ref]["PAYMENT"] or rows[ref]["SETTLEMENT"]
        return (STATUS_ORDER[r[1]], not r[2], str(any_rows[0][4] or ""), ref)

    for ref in sorted(results, key=sort_key):
        res = results[ref]
        prow, srow = rows[ref]["PAYMENT"], rows[ref]["SETTLEMENT"]
        out = [ref]
        # payment block
        for _, key in PAYMENT_COLS:
            if not prow:
                out.append(None)
            elif key == "@recon_date":
                out.append(_join_distinct(str(p[4]) for p in prow))
            elif key.startswith("@qty:"):
                out.append(sum(int(p[2].get(key[5:], 0) or 0) for p in prow))
            else:
                out.append(_join_distinct(p[2].get(key) for p in prow))
        out.append(sum((p[3] for p in prow), Z) if prow else None)
        out += [amounts.get((ref, "PAYMENT", c), Z) if prow else None for c in line_codes]
        # settlement block
        for key in SETTLEMENT_COLS:
            if not srow:
                out.append(None)
            elif key.startswith("@qty:"):
                out.append(sum(int(s[2].get(key[5:], 0) or 0) for s in srow))
            elif key in SETTLEMENT_HEADER_FIELDS:
                out.append(_join_distinct((headers.get(s[2].get("settlement-id")) or {}).get(key) for s in srow))
            else:
                out.append(_join_distinct(s[2].get(key) for s in srow))
        out.append(sum((s[3] for s in srow), Z) if srow else None)
        out += [amounts.get((ref, "SETTLEMENT", c), Z) if srow else None for c in line_codes]
        # comparison
        out += [res[1], res[8]]
        out += [amounts.get((ref, "PAYMENT", c), Z) - amounts.get((ref, "SETTLEMENT", c), Z) for c in line_codes]
        out += ["Y" if res[2] else "N", res[3],
                ", ".join(f"{p[0]}:{p[1]}" for p in prow), ", ".join(f"{s[0]}:{s[1]}" for s in srow),
                routing.get((ref, "PAYMENT"), ""), routing.get((ref, "SETTLEMENT"), "")]
        ws.append([_cell(ws, v) if isinstance(v, Decimal) else v for v in out])


# ---------------------------------------------------------------------------
def write_drilldown(wb, conn):
    ws = wb.create_sheet("Variance Drilldown")
    for col, w in zip("ABCDEFGHIJ", (28, 12, 36, 22, 34, 26, 34, 10, 16, 16)):
        ws.column_dimensions[col].width = w
    ws.append([_cell(ws, "Every in-scope amount behind each Summary line, by source and by the config rule that "
                         "routed it. A line with a variance shows which rule on which side produced it.", True)])
    ws.append([_cell(ws, h, True, fill=HDR_FILL) for h in
               ["Summary line", "Source", "Transaction type", "Amount type", "Amount field / description",
                "Rule", "Summary field", "Lines", "Amount", "Line diff (P-S)"]])
    data = _q(conn, f"""
        SELECT sl.line_code, sl.section || ' / ' || sl.label, l.source, r.transaction_type,
               coalesce(r.amount_type, ''),
               CASE WHEN l.source='SETTLEMENT' THEN r.description ELSE l.amount_field END,
               split_part(l.rule_table, '_', 1) || ' #' || l.rule_id, l.summary_field, count(*), sum(l.amount)
        FROM recon.record_line l JOIN recon.raw_record r USING (record_id)
        JOIN recon.summary_field_map m ON m.summary_field = l.summary_field
         AND (m.sign='ANY' OR (m.sign='POS' AND l.amount>0) OR (m.sign='NEG' AND l.amount<0))
        JOIN recon.summary_line sl ON sl.line_code = m.line_code
        WHERE {IN_SCOPE}
        GROUP BY 1,2,3,4,5,6,7,8, sl.sort_order ORDER BY sl.sort_order,3,4,5,6,7,8""")
    s = summary_by_line(conn)
    last = None
    for code, line, src, tt, at, fd, rule, sf, n, amt in data:
        if line != last:
            d = s.get(code, {}).get("PAYMENT", Z) - s.get(code, {}).get("SETTLEMENT", Z)
            ws.append([_cell(ws, line, True, fill=SEC_FILL)] + [_cell(ws, "", fill=SEC_FILL)] * 8
                      + [_cell(ws, d, True, True, BAD_FILL if d else SEC_FILL)])
            last = line
        ws.append([line, src, tt, at, fd, rule, sf, n, _cell(ws, amt)])


# ---------------------------------------------------------------------------
def write_tieout(wb, conn):
    ws = wb.create_sheet("Tie-out & Counts")
    ws.column_dimensions["A"].width = 44
    for col in "BCDEFGH":
        ws.column_dimensions[col].width = 18

    def table(title, header, rows):
        ws.append([_cell(ws, title, True, fill=HDR_FILL)])
        ws.append([_cell(ws, h, True) for h in header])
        for r in rows:
            ws.append([_cell(ws, v) if isinstance(v, Decimal) else v for v in r])
        ws.append([])

    table("Amazon settlement header vs ingested settlement lines",
          ["settlement-id", "start", "end", "deposit-date", "total-amount (Amazon)", "sum of lines", "difference"],
          _q(conn, """SELECT h.settlement_id, h.raw->>'settlement-start-date', h.raw->>'settlement-end-date',
                             h.raw->>'deposit-date', h.net_amount, sum(r.net_amount), h.net_amount - sum(r.net_amount)
                      FROM recon.raw_record h JOIN recon.raw_record r
                        ON r.settlement_id = h.settlement_id AND r.source='SETTLEMENT' AND NOT r.is_header
                      WHERE h.is_header GROUP BY 1,2,3,4,5"""))
    table("Payments report population (scope = settlement in file AND Released)",
          ["settlement ID", "status", "in scope", "rows", "total", "of which Transfer", "total excl. Transfer"],
          _q(conn, """SELECT r.settlement_id, r.transaction_status,
                             CASE WHEN r.settlement_id IN (SELECT settlement_id FROM recon.recon_scope)
                                   AND r.transaction_status = 'Released' THEN 'Y' ELSE 'N' END,
                             count(*), sum(r.net_amount),
                             coalesce(sum(r.net_amount) FILTER (WHERE r.transaction_type_key='TRANSFER'),0),
                             sum(r.net_amount) - coalesce(sum(r.net_amount) FILTER (WHERE r.transaction_type_key='TRANSFER'),0)
                      FROM recon.raw_record r WHERE r.source='PAYMENT' GROUP BY 1,2,3 ORDER BY 3 DESC,1,2"""))
    table("Reconciliation result (one record = one record_ref, both sides aggregated)",
          ["status", "in scope", "records", "payment rows", "settlement rows", "payment net", "settlement net", "net diff"],
          _q(conn, """SELECT status, CASE WHEN in_scope THEN 'Y' ELSE 'N' END, count(*), sum(payment_rows),
                             sum(settlement_rows), sum(payment_net), sum(settlement_net), sum(diff)
                      FROM recon.recon_result GROUP BY 1,2 ORDER BY 1,2 DESC"""))
    table("Unreconciled records by reason",
          ["status", "in scope", "reason", "records", "rows", "net amount"],
          _q(conn, """SELECT status, CASE WHEN in_scope THEN 'Y' ELSE 'N' END,
                             regexp_replace(reason, ': [0-9,]+ is', ': <id> is'),
                             count(*), sum(payment_rows + settlement_rows), sum(coalesce(payment_net, settlement_net))
                      FROM recon.recon_result WHERE status <> 'RECONCILED' GROUP BY 1,2,3 ORDER BY 1,2 DESC,3"""))
    table("Amounts not routed to any Summary line (in scope, net lines excluded)",
          ["source", "transaction type", "description", "amount field", "rule", "lines", "amount"],
          _q(conn, f"""SELECT l.source, r.transaction_type,
                              CASE WHEN r.source='PAYMENT' AND r.transaction_type_key IN ('ORDER','REFUND')
                                   THEN '(product rows)' ELSE coalesce(r.amount_type || '/', '') || r.description END,
                              l.amount_field, coalesce(split_part(l.rule_table,'_',1) || ' #' || l.rule_id, 'NO RULE'),
                              count(*), sum(l.amount)
                       FROM recon.record_line l JOIN recon.raw_record r USING (record_id)
                       WHERE {IN_SCOPE} AND l.summary_field IS NULL AND NOT (l.source='PAYMENT' AND l.amount_field='total')
                       GROUP BY 1,2,3,4,5 ORDER BY 1,2,3,4,5"""))
    table("Payment rows whose routed components do not add up to the row total (in scope)",
          ["transaction type", "rows", "sum(total)", "sum(routed to summary)", "difference"],
          _q(conn, f"""SELECT r.transaction_type, count(*), sum(r.net_amount), sum(x.routed), sum(r.net_amount - x.routed)
                       FROM recon.raw_record r
                       JOIN (SELECT record_id, coalesce(sum(amount) FILTER (WHERE summary_field IS NOT NULL),0) routed
                             FROM recon.record_line GROUP BY 1) x USING (record_id)
                       WHERE {IN_SCOPE} AND r.source='PAYMENT' AND r.net_amount <> x.routed
                       GROUP BY 1 ORDER BY 1"""))
    table("Ingestion checks",
          ["check", "value"],
          # explicit ordinal: UNION ALL alone does not guarantee row order, and the
          # report must be byte-for-byte reproducible between runs
          [r[1:] for r in _q(conn, """
              SELECT 1, 'payment rows', count(*)::text FROM recon.raw_record WHERE source='PAYMENT'
              UNION ALL SELECT 2, 'settlement rows (incl. header)', count(*)::text FROM recon.raw_record WHERE source='SETTLEMENT'
              UNION ALL SELECT 3, 'record lines', count(*)::text FROM recon.record_line
              UNION ALL SELECT 4, 'UNMAPPED lines (no rule)', count(*)::text FROM recon.record_line WHERE status='UNMAPPED'
              UNION ALL SELECT 5, 'MULTI_RULE lines (amount routed by >1 rule)', count(*)::text FROM recon.record_line WHERE status='MULTI_RULE'
              UNION ALL SELECT 6, 'payment rows where components <> total', count(*)::text FROM recon.raw_record WHERE component_check_diff <> 0
              UNION ALL SELECT 7, 'source files', string_agg(file_name || ' sha256=' || left(sha256,12), ', ' ORDER BY file_id) FROM recon.source_file
              UNION ALL SELECT 8, 'config hash at ingest', config_hash FROM recon.ingest_run
              ORDER BY 1""")])


def write_config_findings(wb, conn):
    ws = wb.create_sheet("Config Findings")
    ws.column_dimensions["A"].width = 28
    ws.column_dimensions["B"].width = 12
    ws.column_dimensions["C"].width = 36
    ws.column_dimensions["D"].width = 120
    ws.append([_cell(ws, "Static config checks (recon validate)", True, fill=HDR_FILL)])
    ws.append([_cell(ws, h, True) for h in ["table", "rule ids", "check", "detail"]])
    for f in validate_configs(conn):
        ws.append([f["table"], f["rule_ids"], f["check"], f["detail"]])
    ws.append([])
    ws.append([_cell(ws, "Rules that fired on in-scope data with more than one rule (MULTI_RULE)", True, fill=HDR_FILL)])
    ws.append([_cell(ws, h, True) for h in ["table", "rule id", "amount field", "summary field", "lines / amount"]])
    for r in _q(conn, f"""SELECT l.rule_table, l.rule_id, l.amount_field, l.summary_field,
                                 count(*) || ' / ' || sum(l.amount)
                          FROM recon.record_line l JOIN recon.raw_record r USING (record_id)
                          WHERE l.status='MULTI_RULE' AND {IN_SCOPE} GROUP BY 1,2,3,4 ORDER BY 1,2,3,4"""):
        ws.append(list(r))
    ws.append([])
    ws.append([_cell(ws, "Config changes applied since the CSVs were loaded (mapping_config_audit)", True, fill=HDR_FILL)])
    ws.append([_cell(ws, h, True) for h in ["table", "op", "rule id", "old -> new"]])
    for t, op, rid, old, new in _q(conn, """SELECT table_name, op, rule_id, old_row, new_row
                                           FROM recon.mapping_config_audit ORDER BY audit_id"""):
        if old and new:
            ch = {k: f"{old[k]!r} -> {new[k]!r}" for k in new if old.get(k) != new.get(k)}
            detail = json.dumps(ch, ensure_ascii=False)
        else:
            detail = json.dumps(old or new, ensure_ascii=False)
        ws.append([t, op, rid, detail])


def write_report(conn, out: Path, title: str = "") -> None:
    wb = Workbook(write_only=True)
    write_summary(wb, conn, f"Amazon Payments vs Settlement {('- ' + title) if title else ''}".strip())
    write_consolidated(wb, conn)
    write_drilldown(wb, conn)
    write_tieout(wb, conn)
    write_config_findings(wb, conn)
    out.parent.mkdir(parents=True, exist_ok=True)
    wb.save(out)
