"""Load mapping configs into PostgreSQL, read them back as RuleSets, validate them."""
from __future__ import annotations

import csv
from collections import defaultdict
from pathlib import Path



from .rules import FIELD_TOKENS, RuleSet, make_rule, template_unknown_tokens

PAYMENT_COLS = ["transaction_type", "description", "amount_field", "record_ref",
                "to_summary_field_when_positive_amount", "to_summary_field_when_negative_amount"]
SETTLEMENT_COLS = ["transaction_type", "amount_type", "amount_description", "record_ref",
                   "to_summary_field_when_positive_amount", "to_summary_field_when_negative_amount"]
HERE = Path(__file__).resolve().parent


def init_schema(conn: psycopg.Connection) -> None:
    with conn.cursor() as cur:
        cur.execute((HERE / "schema.sql").read_text(encoding="utf-8"))
        cur.execute((HERE / "seed_reference.sql").read_text(encoding="utf-8"))
    conn.commit()


def _load_csv(conn, table: str, cols: list[str], path: Path) -> int:
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        header = [h.strip() for h in next(reader)]
        missing = [c for c in cols if c not in header]
        if missing:
            raise ValueError(f"{path.name}: missing columns {missing}")
        idx = [header.index(c) for c in cols]
        rows = []
        for row in reader:
            if not any(cell.strip() for cell in row):
                continue
            row = row + [""] * (len(header) - len(row))
            rows.append([reader.line_num] + [row[i].strip() for i in idx])
    with conn.cursor() as cur:
        cur.executemany(
            f"INSERT INTO recon.{table} (csv_line, {', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * (len(cols) + 1))})", rows)
    return len(rows)


def seed_configs(conn, payment_cfg: Path, settlement_cfg: Path) -> tuple[int, int]:
    """Replace config tables with the CSV contents and (re)attach audit triggers."""
    with conn.cursor() as cur:
        for t in ("payment_mapping_config", "settlement_mapping_config"):
            cur.execute(f"DROP TRIGGER IF EXISTS {t}_audit ON recon.{t}")
            cur.execute(f"TRUNCATE recon.{t} RESTART IDENTITY")
        cur.execute("TRUNCATE recon.mapping_config_audit RESTART IDENTITY")
    n_p = _load_csv(conn, "payment_mapping_config", PAYMENT_COLS, payment_cfg)
    n_s = _load_csv(conn, "settlement_mapping_config", SETTLEMENT_COLS, settlement_cfg)
    with conn.cursor() as cur:
        for t in ("payment_mapping_config", "settlement_mapping_config"):
            cur.execute(f"CREATE TRIGGER {t}_audit AFTER INSERT OR UPDATE OR DELETE ON recon.{t} "
                        f"FOR EACH ROW EXECUTE FUNCTION recon.audit_mapping()")
    conn.commit()
    return n_p, n_s


def load_rulesets(conn) -> tuple[RuleSet, RuleSet]:
    with conn.cursor() as cur:
        cur.execute(f"SELECT rule_id, {', '.join(PAYMENT_COLS)} FROM recon.payment_mapping_config")
        prules = [make_rule("payment_mapping_config", r[0], (r[1], r[2]), r[3], r[4], r[5], r[6])
                  for r in cur.fetchall()]
        cur.execute(f"SELECT rule_id, {', '.join(SETTLEMENT_COLS)} FROM recon.settlement_mapping_config")
        srules = [make_rule("settlement_mapping_config", r[0], (r[1], r[2], r[3]), None, r[4], r[5], r[6])
                  for r in cur.fetchall()]
    return RuleSet(prules, uses_amount_field=True), RuleSet(srules, uses_amount_field=False)


def config_hash(conn) -> str:
    with conn.cursor() as cur:
        cur.execute("""
            SELECT md5(coalesce((SELECT string_agg(t::text, '|' ORDER BY rule_id) FROM recon.payment_mapping_config t), '')
                    || coalesce((SELECT string_agg(t::text, '|' ORDER BY rule_id) FROM recon.settlement_mapping_config t), ''))
        """)
        return cur.fetchone()[0]


def validate_configs(conn) -> list[dict]:
    """Static checks on the config tables. Returns findings (also shown in the report)."""
    pset, sset = load_rulesets(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT summary_field FROM recon.summary_field_map")
        known_fields = {r[0] for r in cur.fetchall()}
    findings: list[dict] = []

    def add(table, rule_ids, kind, detail):
        findings.append({"table": table, "rule_ids": ",".join(map(str, rule_ids)),
                         "check": kind, "detail": detail})

    for rs, table in ((pset, "payment_mapping_config"), (sset, "settlement_mapping_config")):
        groups = defaultdict(list)
        for r in rs.rules:
            groups[(r.match_keys, r.amount_field)].append(r)
            for tok in template_unknown_tokens(r.record_ref):
                add(table, [r.rule_id], "unknown record_ref token",
                    f"'{tok}' in '{r.record_ref}' is not a known field; it is used as a literal")
            for f in {r.positive_field, r.negative_field}:
                if f and f not in known_fields:
                    add(table, [r.rule_id], "summary field not on Summary layout", f)
            if bool(r.positive_field) != bool(r.negative_field):
                add(table, [r.rule_id], "one-sided summary routing",
                    f"{'/'.join(r.match_values)} {r.amount_field or ''}: pos={r.positive_field} "
                    f"neg={r.negative_field} (amounts of the other sign are dropped from the summary)")
        for (keys, af), rules in groups.items():
            if len(rules) > 1:
                add(table, [r.rule_id for r in rules], "duplicate match key",
                    f"{'/'.join(rules[0].match_values)} {af or ''} -> "
                    + "; ".join(f"#{r.rule_id}:{r.positive_field}/{r.negative_field} ref={r.record_ref}"
                                for r in rules))
        # all rules for the same (type, description) should build the same record_ref
        by_td = defaultdict(set)
        for r in rs.rules:
            by_td[r.match_keys[:2] if table.startswith("payment") else r.match_keys].add(r.record_ref)
        for k, refs in by_td.items():
            if len(refs) > 1:
                add(table, [r.rule_id for r in rs.rules
                            if (r.match_keys[:2] if table.startswith("payment") else r.match_keys) == k],
                    "inconsistent record_ref", f"{k}: {sorted(refs)}")
    findings += cross_config_findings(pset, sset)
    return findings


def _anchor(template: str) -> str:
    """record_ref template with field tokens abstracted: the literal 'anchor' that
    identifies the kind of money (e.g. '{txn_ref}+FBA_DISPOSAL_FEE+{settlement_id}+{date}')."""
    return "+".join("{" + t.strip() + "}" if t.strip() in FIELD_TOKENS else t.strip()
                    for t in template.split("+"))


def cross_config_findings(pset: RuleSet, sset: RuleSet) -> list[dict]:
    """Pair payment and settlement rules on their record_ref anchor. Rules with the
    same anchor describe the same money on both sides, so (a) they must route to
    the same summary buckets and (b) an anchor present on only one side that is a
    near-miss of the other side's anchor is almost certainly a typo."""
    import difflib

    out = []
    groups = defaultdict(lambda: {"P": [], "S": []})
    for r in pset.rules:
        groups[_anchor(r.record_ref)]["P"].append(r)
    for r in sset.rules:
        groups[_anchor(r.record_ref)]["S"].append(r)
    for anchor, g in groups.items():
        if anchor == "{txn_ref}+{sku}+{date}" or not g["P"] or not g["S"]:
            continue  # order/refund lines are compared per component in the data instead
        routes = lambda rules: {(r.positive_field, r.negative_field) for r in rules
                                if r.positive_field or r.negative_field}
        if routes(g["P"]) and routes(g["S"]) and routes(g["P"]) != routes(g["S"]):
            out.append({"table": "both", "rule_ids": "P" + ",".join(str(r.rule_id) for r in g["P"])
                        + " S" + ",".join(str(r.rule_id) for r in g["S"]),
                        "check": "same record_ref anchor, different summary routing",
                        "detail": f"{anchor}: payment {sorted(routes(g['P']), key=str)} vs "
                                  f"settlement {sorted(routes(g['S']), key=str)}"})
    only_p = [a for a, g in groups.items() if g["P"] and not g["S"]]
    only_s = [a for a, g in groups.items() if g["S"] and not g["P"]]
    for a in only_p:
        for b in only_s:
            if difflib.SequenceMatcher(None, a, b).ratio() >= 0.95:
                out.append({"table": "both",
                            "rule_ids": "P" + ",".join(str(r.rule_id) for r in groups[a]["P"])
                            + " S" + ",".join(str(r.rule_id) for r in groups[b]["S"]),
                            "check": "near-miss record_ref anchor (keys can never match)",
                            "detail": f"payment '{a}' vs settlement '{b}'"})
    return out
