"""Command line entry point:  python -m recon <command> [...]

  init                         create schema + reference data, seed configs from CSV
  apply-sql FILE               run a SQL file (e.g. MAPPING_FIXES.sql) against the DB
  validate                     static checks on the mapping config tables
  ingest                       ingest both source files (idempotent)
  reconcile                    match on record_ref, print counts
  summary                      print the Summary (payments vs settlement) to stdout
  report OUT.xlsx              write the Excel report
  trace LINE_CODE [SOURCE]     list the rules/rows behind a summary line (in scope)
  dump OUT.sql                 pg_dump of schema recon
  run-all                      init -> ingest -> reconcile -> report (before) ->
                               MAPPING_FIXES.sql -> ingest -> reconcile -> report (after)
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import psycopg

from . import configs, db
from .ingest import IngestError, ingest
from .reconcile import reconcile, running_summary_by_line, summary_by_line

ROOT = db.ROOT
DATA = ROOT / "data"
DEFAULTS = {
    "payments": DATA / "amazon_payments_data.csv",
    "settlements": DATA / "amazon_settlements_data.txt",
    "payment_cfg": DATA / "amazon_payment_configs_au_old.csv",
    "settlement_cfg": DATA / "amazon_settlement_configs_au.csv",
}


def _p(obj):
    print(json.dumps(obj, indent=2, default=str))


def cmd_init(conn, a):
    configs.init_schema(conn)
    n = configs.seed_configs(conn, Path(a.payment_cfg), Path(a.settlement_cfg))
    print(f"schema created; payment rules={n[0]} settlement rules={n[1]}")


def cmd_apply_sql(conn, a):
    db.run_sql_file(conn, Path(a.file))
    conn.commit()
    print(f"applied {a.file}")


def cmd_validate(conn, a):
    for f in configs.validate_configs(conn):
        print(f"[{f['table']} #{f['rule_ids']}] {f['check']}: {f['detail']}")


def cmd_ingest(conn, a):
    t = time.time()
    stats = ingest(conn, a.payments, a.settlements, label=a.label, force=getattr(a, "force", False))
    _p(stats)
    if "skipped" not in stats:
        print(f"ingested in {time.time() - t:.1f}s")


def cmd_reconcile(conn, a):
    _p(reconcile(conn, a.settlement_id))


def print_summary(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT line_code, section, label FROM recon.summary_line ORDER BY sort_order")
        lines = cur.fetchall()
    s = summary_by_line(conn)
    rs = running_summary_by_line(conn)
    print(f"{'line':<16}{'label':<34}{'payments':>14}{'settlement':>14}{'diff':>12}")
    z = Decimal("0")
    for code, sec, label in lines:
        p, st = s.get(code, {}).get("PAYMENT", z), s.get(code, {}).get("SETTLEMENT", z)
        flag = "" if rs.get(code, {}) == s.get(code, {}) else "  !! running summary differs"
        print(f"{code:<16}{(sec + '/' + label)[:33]:<34}{p:>14,.2f}{st:>14,.2f}{p - st:>12,.2f}{flag}")


def cmd_summary(conn, a):
    print_summary(conn)


def cmd_report(conn, a):
    from .report import write_report
    write_report(conn, Path(a.out), title=a.title)
    conn.commit()
    print(f"wrote {a.out}")


def cmd_trace(conn, a):
    from .reconcile import IN_SCOPE
    src = f"AND l.source = '{a.source.upper()}'" if a.source else ""
    sql = f"""
      SELECT l.source, l.rule_table, l.rule_id, r.transaction_type, r.amount_type, r.description_key,
             l.amount_field, l.summary_field, count(*), sum(l.amount)
      FROM recon.record_line l JOIN recon.raw_record r ON r.record_id = l.record_id
      JOIN recon.summary_field_map m ON m.summary_field = l.summary_field
       AND (m.sign='ANY' OR (m.sign='POS' AND l.amount>0) OR (m.sign='NEG' AND l.amount<0))
      WHERE {IN_SCOPE} AND m.line_code = %s {src}
      GROUP BY 1,2,3,4,5,6,7,8 ORDER BY 1, 10"""
    with conn.cursor() as cur:
        cur.execute(sql, (a.line_code,))
        for row in cur.fetchall():
            print(" | ".join("" if v is None else str(v) for v in row))


def cmd_dump(conn, a):
    conn.commit()  # release locks held by our own read transaction before pg_dump
    bindir = db.pg_bin_dir()
    exe = str(bindir / "pg_dump") if bindir else "pg_dump"
    out = Path(a.out)
    plain = out.with_suffix("") if out.suffix == ".gz" else out
    subprocess.run([exe, "--dbname", db.database_url(), "--schema", "recon", "--no-owner",
                    "--file", str(plain)], check=True)
    if out.suffix == ".gz":
        import gzip
        import shutil
        with open(plain, "rb") as src, gzip.open(out, "wb", compresslevel=9) as dst:
            shutil.copyfileobj(src, dst)
        plain.unlink()
    print(f"wrote {out}")


def cmd_run_all(conn, a):
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    from .report import write_report
    cmd_init(conn, a)
    for phase, fixes in (("before_fix", None), ("after_fix", ROOT / "MAPPING_FIXES.sql")):
        if fixes:
            db.run_sql_file(conn, fixes)
            conn.commit()
            print(f"applied {fixes.name}")
        a.label = phase
        cmd_ingest(conn, a)
        _p(reconcile(conn, a.settlement_id))
        print_summary(conn)
        write_report(conn, out / f"amazon_recon_report_{phase}.xlsx", title=phase)
        conn.commit()
        print(f"wrote {out / f'amazon_recon_report_{phase}.xlsx'}")
    if not a.no_dump:
        a.out = str(out / "recon_dump_after_fix.sql.gz")
        cmd_dump(conn, a)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="recon", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--payments", default=str(DEFAULTS["payments"]))
    ap.add_argument("--settlements", default=str(DEFAULTS["settlements"]))
    ap.add_argument("--payment-cfg", default=str(DEFAULTS["payment_cfg"]))
    ap.add_argument("--settlement-cfg", default=str(DEFAULTS["settlement_cfg"]))
    ap.add_argument("--settlement-id", action="append",
                    help="settlement(s) to reconcile (default: those in the settlement file)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("init")
    p = sub.add_parser("apply-sql"); p.add_argument("file")
    sub.add_parser("validate")
    p = sub.add_parser("ingest"); p.add_argument("--label")
    p.add_argument("--force", action="store_true", help="reload even if files and config are unchanged")
    sub.add_parser("reconcile")
    sub.add_parser("summary")
    p = sub.add_parser("report"); p.add_argument("out"); p.add_argument("--title", default="")
    p = sub.add_parser("trace"); p.add_argument("line_code"); p.add_argument("source", nargs="?")
    p = sub.add_parser("dump"); p.add_argument("out")
    p = sub.add_parser("run-all"); p.add_argument("--outdir", default=str(ROOT / "output"))
    p.add_argument("--no-dump", action="store_true")
    a = ap.parse_args(argv)
    try:
        with db.connect() as conn:
            globals()["cmd_" + a.cmd.replace("-", "_")](conn, a)
    except (IngestError, FileNotFoundError, ValueError) as exc:
        # expected input problems: one clear line, no traceback, non-zero exit
        print(f"error: {exc}", file=sys.stderr)
        if isinstance(exc, IngestError):
            print("nothing was changed: the ingest runs in one transaction and was rolled back",
                  file=sys.stderr)
        return 2
    except psycopg.Error as exc:
        print(f"database error: {exc}".strip(), file=sys.stderr)
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
