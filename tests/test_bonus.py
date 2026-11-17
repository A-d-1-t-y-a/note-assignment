"""Bonus items: summary computed DURING ingestion, error handling with
rollback, and idempotent re-ingestion. Uses the fixtures of test_pipeline."""
from decimal import Decimal

import pytest

from recon import ingest as ingest_mod
from recon.ingest import IngestError, Ingestor, ingest
from recon.reconcile import reconcile, running_summary_by_line, summary_by_line
from tests.test_pipeline import PAYMENTS, SETTLEMENTS, conn  # noqa: F401  (fixture)


def _snapshot(c):
    return (c.execute("SELECT count(*) FROM recon.raw_record").fetchone()[0],
            c.execute("SELECT count(*), coalesce(sum(amount),0) FROM recon.record_line").fetchone(),
            c.execute("SELECT source, summary_field, sign, amount, line_count FROM recon.running_summary "
                      "ORDER BY 1,2,3").fetchall())


# ------------------------------------------------------------ summary while ingesting
def test_summary_is_accumulated_batch_by_batch_during_ingestion(conn, monkeypatch):
    """With a batch size of 1 the running summary is upserted after every row.
    Spy on each flush: the summary table must already hold partial totals
    while rows are still being streamed (inside the ingest transaction)."""
    monkeypatch.setattr(ingest_mod, "BATCH", 1)
    observed = []
    real_flush = Ingestor._flush

    def spy(self, cur, raw_rows, line_rows):
        real_flush(self, cur, raw_rows, line_rows)
        cur.execute("SELECT coalesce(sum(line_count),0) FROM recon.running_summary")
        observed.append(cur.fetchone()[0])

    monkeypatch.setattr(Ingestor, "_flush", spy)
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    reconcile(conn)
    # summary grew monotonically while ingesting, i.e. it was not computed at the end
    assert len(observed) > 5
    assert observed == sorted(observed)
    assert 0 < observed[2] < observed[-1]
    # and the streamed result equals the SQL re-aggregation over record_line
    assert running_summary_by_line(conn) == summary_by_line(conn)


def test_batch_size_does_not_change_results(conn, monkeypatch):
    monkeypatch.setattr(ingest_mod, "BATCH", 1)
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    small = _snapshot(conn)
    monkeypatch.setattr(ingest_mod, "BATCH", 100000)
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    assert _snapshot(conn) == small


# ------------------------------------------------------------ idempotency
def test_reingest_is_idempotent(conn):
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    first = _snapshot(conn)
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    assert _snapshot(conn) == first
    assert conn.execute("SELECT count(*) FROM recon.ingest_run").fetchone()[0] == 1


# ------------------------------------------------------------ error handling
@pytest.mark.parametrize("bad_payments, expected", [
    # unparseable amount on the first data row (physical line 4)
    (PAYMENTS.replace('"13.00"', '"13.OO"', 1), r"p_bad\.csv:4: .*unparseable amount"),
    # unparseable date
    (PAYMENTS.replace("9 July 2026 12:00:56 am GMT+9", "9 Julember 2026 12:00:56 am GMT+9", 1),
     r"p_bad\.csv:4: .*month"),
    # a row with a missing column
    (PAYMENTS.replace(',"Released","17 July 2026 4:26:32 pm GMT+9"', ',"Released"', 1),
     r"p_bad\.csv:4: expected 25 columns, got 24"),
    # no header row at all
    ('"just a preamble"\n"nothing else"\n', r"payments header row not found"),
])
def test_bad_input_fails_with_file_and_line_and_rolls_back(conn, bad_payments, expected):
    ingest(conn, conn.fx / "p.csv", conn.fx / "s.txt")
    reconcile(conn)
    before = _snapshot(conn)
    bad = conn.fx / "p_bad.csv"
    bad.write_text(bad_payments, encoding="utf-8")
    with pytest.raises(IngestError, match=expected):
        ingest(conn, bad, conn.fx / "s.txt")
    # the failed run left the previous good data untouched (single transaction)
    assert _snapshot(conn) == before
    assert summary_by_line(conn)["S_PRODUCT"]["PAYMENT"] == Decimal("22.00")


def test_bad_settlement_row_reports_line(conn):
    bad = conn.fx / "s_bad.txt"
    bad.write_text(SETTLEMENTS.replace("\t-6.00\t", "\tsix\t", 1), encoding="utf-8")
    with pytest.raises(IngestError, match=r"s_bad\.txt:8: .*unparseable amount"):
        ingest(conn, conn.fx / "p.csv", bad)


def test_missing_file_is_reported(conn):
    with pytest.raises(FileNotFoundError):
        ingest(conn, conn.fx / "does_not_exist.csv", conn.fx / "s.txt")
