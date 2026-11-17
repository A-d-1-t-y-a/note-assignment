"""Match payment and settlement records on record_ref.

Granularity: both sides are aggregated to ONE row per record_ref before
matching (payments: sum of the row 'total'; settlement: sum of 'amount').
A record_ref present on both sides is RECONCILED (the difference, if any, is
reported); otherwise it is an unreconciled payment / settlement.

Scope: the settlement(s) present in the settlement file. A payment row is in
scope when its settlement ID is in scope AND it is Released (Deferred rows are
held by Amazon and are not part of this settlement's cash).
"""
from __future__ import annotations

RECONCILE_SQL = """
TRUNCATE recon.recon_result;
INSERT INTO recon.recon_result
SELECT record_ref,
       CASE WHEN p_rows > 0 AND s_rows > 0 THEN 'RECONCILED'
            WHEN p_rows > 0 THEN 'UNRECONCILED_PAYMENT'
            ELSE 'UNRECONCILED_SETTLEMENT' END,
       in_scope,
       CASE
         WHEN p_rows > 0 AND s_rows > 0 THEN
              CASE WHEN p_net <> s_net THEN 'AMOUNT DIFFERS' END
         WHEN p_rows > 0 AND all_deferred THEN 'DEFERRED: not yet released into a settlement'
         WHEN p_rows > 0 AND NOT any_scope_settlement THEN
              'OTHER SETTLEMENT: ' || p_settlements || ' is not in the settlement file'
         WHEN p_rows > 0 AND all_transfer THEN 'PAYOUT: bank transfer, shown as the settlement deposit, not as a line'
         WHEN p_rows > 0 THEN 'NO SETTLEMENT COUNTERPART'
         ELSE 'NO PAYMENT COUNTERPART'
       END,
       p_rows, s_rows, p_net, s_net, coalesce(p_net, 0) - coalesce(s_net, 0)
FROM (
  SELECT r.record_ref,
         count(*) FILTER (WHERE r.source = 'PAYMENT')    AS p_rows,
         count(*) FILTER (WHERE r.source = 'SETTLEMENT') AS s_rows,
         sum(r.net_amount) FILTER (WHERE r.source = 'PAYMENT')    AS p_net,
         sum(r.net_amount) FILTER (WHERE r.source = 'SETTLEMENT') AS s_net,
         bool_or(sc.settlement_id IS NOT NULL
                 AND (r.source = 'SETTLEMENT' OR r.transaction_status = 'Released')) AS in_scope,
         bool_and(r.transaction_status = 'Deferred') FILTER (WHERE r.source = 'PAYMENT') AS all_deferred,
         bool_or(sc.settlement_id IS NOT NULL) AS any_scope_settlement,
         bool_and(r.transaction_type_key = 'TRANSFER') FILTER (WHERE r.source = 'PAYMENT') AS all_transfer,
         string_agg(DISTINCT r.settlement_id, ',') AS p_settlements
  FROM recon.raw_record r
  LEFT JOIN recon.recon_scope sc ON sc.settlement_id = r.settlement_id
  WHERE NOT r.is_header
  GROUP BY r.record_ref
) g;
"""

# In-scope predicate on raw_record r (used by summary / report queries)
IN_SCOPE = ("(r.settlement_id IN (SELECT settlement_id FROM recon.recon_scope) "
            "AND NOT r.is_header "
            "AND (r.source = 'SETTLEMENT' OR r.transaction_status = 'Released'))")


def reconcile(conn, settlement_ids: list[str] | None = None) -> dict:
    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("TRUNCATE recon.recon_scope")
            if settlement_ids:
                cur.executemany("INSERT INTO recon.recon_scope VALUES (%s)", [(s,) for s in settlement_ids])
            else:
                cur.execute("INSERT INTO recon.recon_scope SELECT DISTINCT settlement_id "
                            "FROM recon.raw_record WHERE source='SETTLEMENT' AND settlement_id <> ''")
            cur.execute(RECONCILE_SQL)
            cur.execute("SELECT status, in_scope, count(*), sum(payment_rows), sum(settlement_rows), "
                        "sum(diff) FROM recon.recon_result GROUP BY 1,2 ORDER BY 1,2")
            rows = cur.fetchall()
    return {f"{s}{'' if sc else ' (out of scope)'}": {"records": n, "payment_rows": pr,
                                                       "settlement_rows": sr, "net_diff": d}
            for s, sc, n, pr, sr, d in rows}


def summary_by_line(conn) -> dict:
    """{line_code: {'PAYMENT': amt, 'SETTLEMENT': amt}} for in-scope rows, from record_line."""
    sql = f"""
      SELECT m.line_code, r.source, sum(l.amount)
      FROM recon.record_line l
      JOIN recon.raw_record r ON r.record_id = l.record_id
      JOIN recon.summary_field_map m
        ON m.summary_field = l.summary_field
       AND (m.sign = 'ANY' OR (m.sign = 'POS' AND l.amount > 0) OR (m.sign = 'NEG' AND l.amount < 0))
      WHERE {IN_SCOPE}
      GROUP BY 1, 2"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for line, src, amt in cur.fetchall():
            out.setdefault(line, {})[src] = amt
    return out


def running_summary_by_line(conn) -> dict:
    """Same numbers, but from the summary accumulated during ingestion."""
    sql = """
      SELECT m.line_code, s.source, sum(s.amount)
      FROM recon.running_summary s
      JOIN recon.summary_field_map m
        ON m.summary_field = s.summary_field AND (m.sign = 'ANY' OR m.sign = s.sign)
      WHERE s.settlement_id IN (SELECT settlement_id FROM recon.recon_scope)
        AND (s.source = 'SETTLEMENT' OR s.transaction_status = 'Released')
      GROUP BY 1, 2"""
    out: dict = {}
    with conn.cursor() as cur:
        cur.execute(sql)
        for line, src, amt in cur.fetchall():
            out.setdefault(line, {})[src] = amt
    return out
