"""Stream both Amazon files into recon.raw_record (ONE table) and recon.record_line.

* Files are read exactly as delivered (BOM, preamble lines, thousands
  separators, GMT+9 vs UTC timestamps are all handled here).
* Every raw row is stored with its file, physical line number and untouched
  payload (jsonb) so any report number can be traced to input rows.
* The mapping configs are read from the DB, applied row by row, and the
  summary is accumulated while streaming (recon.running_summary is upserted
  per batch inside the same transaction).
* Idempotent: each ingest replaces the previous run's data atomically.
"""
from __future__ import annotations

import csv
import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from .configs import config_hash, load_rulesets
from .normalize import (fmt_date, match_key, parse_amount, parse_payment_datetime,
                        parse_settlement_datetime)
from .rules import RuleSet, build_record_ref

BATCH = 5000
ZERO = Decimal("0.00")

RAW_COLS = ["record_id", "run_id", "file_id", "source", "line_no", "is_header", "raw",
            "settlement_id", "transaction_type", "description", "amount_type",
            "transaction_type_key", "description_key", "amount_type_key", "txn_ref", "sku",
            "posted_at", "recon_date", "transaction_status", "net_amount",
            "component_check_diff", "record_ref"]
LINE_COLS = ["run_id", "record_id", "source", "amount_field", "amount", "rule_table", "rule_id",
             "match_tier", "status", "record_ref", "summary_field", "is_net"]


class IngestError(Exception):
    pass


@dataclass
class Stats:
    rows: int = 0
    lines: int = 0
    unmapped_lines: int = 0
    multi_rule_lines: int = 0
    component_mismatch_rows: int = 0


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# File readers -> (line_no, dict) with the original headers as keys
# ---------------------------------------------------------------------------
def read_payments(path: Path):
    """Amazon 'Date Range' payments CSV: free-text preamble, then the header row."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = None
        prev_end = 0
        for row in reader:
            start = prev_end + 1
            prev_end = reader.line_num
            if header is None:
                low = [c.strip().lower() for c in row]
                if "date/time" in low and "settlement id" in low and "total" in low:
                    header = [c.strip() for c in row]
                continue
            if not any(c.strip() for c in row):
                continue
            if len(row) != len(header):
                raise IngestError(f"{path.name}:{start}: expected {len(header)} columns, got {len(row)}")
            yield start, dict(zip(header, row))
        if header is None:
            raise IngestError(f"{path.name}: payments header row not found")


def read_settlements(path: Path):
    """Amazon V2 flat-file settlement report: tab separated, no quoting."""
    with open(path, encoding="utf-8-sig", newline="") as fh:
        header = None
        for line_no, line in enumerate(fh, 1):
            line = line.rstrip("\r\n")
            if header is None:
                header = [c.strip() for c in line.split("\t")]
                continue
            if not line.strip():
                continue
            cells = line.split("\t")
            if len(cells) < len(header):
                cells += [""] * (len(header) - len(cells))
            elif len(cells) > len(header):
                raise IngestError(f"{path.name}:{line_no}: {len(cells)} cells for {len(header)} columns")
            yield line_no, dict(zip(header, cells))


# ---------------------------------------------------------------------------
class Ingestor:
    def __init__(self, conn, label: str | None = None):
        self.conn = conn
        self.label = label
        self.pset, self.sset = load_rulesets(conn)
        with conn.cursor() as cur:
            cur.execute("SELECT lower(source_header), amount_field FROM recon.payment_column_alias")
            self.aliases = dict(cur.fetchall())
        self.next_record_id = 1
        self.summary = defaultdict(lambda: [ZERO, 0])   # batch-local running summary
        self.stats = {"PAYMENT": Stats(), "SETTLEMENT": Stats()}

    # -- summary-while-ingesting -------------------------------------------
    def _acc(self, source, settlement_id, status, summary_field, amount):
        if summary_field is None or amount == 0:
            return
        sign = "POS" if amount > 0 else "NEG"
        k = (source, settlement_id or "", status or "", summary_field, sign)
        acc = self.summary[k]
        acc[0] += amount
        acc[1] += 1

    def _flush(self, cur, raw_rows, line_rows):
        if raw_rows:
            with cur.copy(f"COPY recon.raw_record ({', '.join(RAW_COLS)}) FROM STDIN") as cp:
                for r in raw_rows:
                    cp.write_row(r)
        if line_rows:
            with cur.copy(f"COPY recon.record_line ({', '.join(LINE_COLS)}) FROM STDIN") as cp:
                for r in line_rows:
                    cp.write_row(r)
        if self.summary:
            cur.executemany(
                """INSERT INTO recon.running_summary AS t
                     (run_id, source, settlement_id, transaction_status, summary_field, sign, amount, line_count)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (run_id, source, settlement_id, transaction_status, summary_field, sign)
                   DO UPDATE SET amount = t.amount + EXCLUDED.amount,
                                 line_count = t.line_count + EXCLUDED.line_count""",
                [(self.run_id, *k, v[0], v[1]) for k, v in self.summary.items()])
            self.summary.clear()
        raw_rows.clear()
        line_rows.clear()

    # -- line creation -----------------------------------------------------
    def _lines_for(self, source, rid, rs: RuleSet, match_values, amount_field, amount,
                   ref_values, default_ref, is_net, settlement_id, status, out):
        rules, tier = rs.match(match_values, amount_field if source == "PAYMENT" else None)
        st = self.stats[source]
        if not rules:
            st.unmapped_lines += 1
            out.append((self.run_id, rid, source, amount_field, amount, None, None, None,
                        "UNMAPPED", default_ref, None, is_net))
            return
        status_txt = "MULTI_RULE" if len(rules) > 1 else "MAPPED"
        for i, rule in enumerate(rules):
            if len(rules) > 1:
                st.multi_rule_lines += 1
            sf = rule.summary_field_for(amount)
            ref = build_record_ref(rule.record_ref, ref_values)
            out.append((self.run_id, rid, source, amount_field, amount, rule.table, rule.rule_id,
                        tier, status_txt, ref, sf, is_net and i == 0))
            self._acc(source, settlement_id, status, sf, amount)
        st.lines += len(rules)

    def _primary_ref(self, rs, match_values, amount_field, ref_values, fallback_parts):
        rules, _ = rs.match(match_values, amount_field)
        if rules:
            return build_record_ref(rules[0].record_ref, ref_values)
        return "UNMAPPED|" + "|".join(fallback_parts)

    # -- payments ----------------------------------------------------------
    def _payment_row(self, file_id, line_no, row, raw_out, line_out):
        g = lambda k: (row.get(k) or "").strip()
        rid = self.next_record_id
        self.next_record_id += 1
        posted = parse_payment_datetime(g("date/time"))
        released = parse_payment_datetime(g("Transaction Release Date"))
        # The settlement report posts a transaction when its funds are RELEASED,
        # so the release timestamp (in UTC) is the date that matches the
        # settlement's posted-date. Deferred rows have no release date yet.
        key_dt = released or posted
        recon_date = key_dt.date() if key_dt else None
        status = g("Transaction status") or None
        settlement_id = g("settlement ID")
        ttype, desc = g("type"), g("description")
        amounts: dict[str, Decimal] = {}
        for header, value in row.items():
            af = self.aliases.get(header.strip().lower())
            if af:
                amounts[af] = amounts.get(af, ZERO) + parse_amount(value)
        total = amounts.get("total", ZERO)
        comp_diff = sum((v for k, v in amounts.items() if k != "total"), ZERO) - total
        if comp_diff != 0:
            self.stats["PAYMENT"].component_mismatch_rows += 1
        ref_values = {"txn_ref": g("order ID"), "sku": g("sku"), "date": fmt_date(recon_date),
                      "settlement_id": settlement_id, "description": match_key(desc),
                      "shipment_id": "", "merchant_order_id": g("order ID")}
        mv = (ttype, desc)
        primary = self._primary_ref(self.pset, mv, "total", ref_values,
                                    [match_key(ttype), match_key(desc), g("order ID"), g("sku"),
                                     settlement_id, fmt_date(recon_date)])
        raw_out.append((rid, self.run_id, file_id, "PAYMENT", line_no, False,
                        json.dumps(row, ensure_ascii=False), settlement_id, ttype, desc, None,
                        match_key(ttype), match_key(desc), None, g("order ID") or None,
                        g("sku") or None, posted, recon_date, status, total, comp_diff, primary))
        for af, amt in amounts.items():
            if amt == 0:
                continue
            self._lines_for("PAYMENT", rid, self.pset, mv, af, amt, ref_values, primary,
                            af == "total", settlement_id, status, line_out)
        self.stats["PAYMENT"].rows += 1

    # -- settlements -------------------------------------------------------
    def _settlement_row(self, file_id, line_no, row, raw_out, line_out):
        g = lambda k: (row.get(k) or "").strip()
        rid = self.next_record_id
        self.next_record_id += 1
        settlement_id = g("settlement-id")
        is_header = bool(g("total-amount")) and not g("transaction-type")
        posted = parse_settlement_datetime(g("posted-date-time")) or parse_settlement_datetime(g("posted-date"))
        recon_date = posted.date() if posted else None
        ttype, atype, adesc = g("transaction-type"), g("amount-type"), g("amount-description")
        amount = parse_amount(g("total-amount") if is_header else g("amount"))
        ref_values = {"txn_ref": g("order-id"), "sku": g("sku"), "date": fmt_date(recon_date),
                      "settlement_id": settlement_id, "description": match_key(adesc),
                      "shipment_id": g("shipment-id"), "merchant_order_id": g("merchant-order-id")}
        mv = (ttype, atype, adesc)
        primary = None
        if not is_header:
            primary = self._primary_ref(self.sset, mv, None, ref_values,
                                        [match_key(ttype), match_key(atype), match_key(adesc),
                                         g("order-id"), g("sku"), settlement_id, fmt_date(recon_date)])
        raw_out.append((rid, self.run_id, file_id, "SETTLEMENT", line_no, is_header,
                        json.dumps(row, ensure_ascii=False), settlement_id, ttype or None,
                        adesc or None, atype or None, match_key(ttype), match_key(adesc),
                        match_key(atype), g("order-id") or None, g("sku") or None, posted,
                        recon_date, None, amount, None, primary))
        if not is_header and amount != 0:
            self._lines_for("SETTLEMENT", rid, self.sset, mv, "amount", amount, ref_values,
                            primary, True, settlement_id, None, line_out)
        self.stats["SETTLEMENT"].rows += 1

    # -- driver ------------------------------------------------------------
    def run(self, payment_path: Path, settlement_path: Path) -> dict:
        conn = self.conn
        with conn.transaction():
            with conn.cursor() as cur:
                # idempotent re-ingestion: wipe previous run(s) atomically
                cur.execute("TRUNCATE recon.ingest_run, recon.source_file, recon.raw_record, "
                            "recon.record_line, recon.running_summary, recon.recon_result "
                            "RESTART IDENTITY CASCADE")
                cur.execute("INSERT INTO recon.ingest_run(label, config_hash) VALUES (%s,%s) RETURNING run_id",
                            (self.label, config_hash(conn)))
                self.run_id = cur.fetchone()[0]
                for source, path, reader, handler in (
                        ("PAYMENT", payment_path, read_payments, self._payment_row),
                        ("SETTLEMENT", settlement_path, read_settlements, self._settlement_row)):
                    cur.execute("INSERT INTO recon.source_file(run_id, source, file_name, sha256) "
                                "VALUES (%s,%s,%s,%s) RETURNING file_id",
                                (self.run_id, source, Path(path).name, sha256(Path(path))))
                    file_id = cur.fetchone()[0]
                    raw_rows, line_rows = [], []
                    for line_no, row in reader(Path(path)):
                        try:
                            handler(file_id, line_no, row, raw_rows, line_rows)
                        except (ValueError, IngestError) as exc:
                            raise IngestError(f"{Path(path).name}:{line_no}: {exc}") from exc
                        if len(raw_rows) >= BATCH:
                            self._flush(cur, raw_rows, line_rows)
                    self._flush(cur, raw_rows, line_rows)
                    cur.execute("UPDATE recon.source_file SET data_rows=%s WHERE file_id=%s",
                                (self.stats[source].rows, file_id))
                cur.execute("UPDATE recon.ingest_run SET finished_at=now() WHERE run_id=%s", (self.run_id,))
        return {k: vars(v) for k, v in self.stats.items()}


def ingest(conn, payment_path, settlement_path, label=None) -> dict:
    return Ingestor(conn, label).run(Path(payment_path), Path(settlement_path))
