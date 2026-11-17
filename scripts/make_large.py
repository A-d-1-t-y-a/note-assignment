"""Build N-times larger copies of the two Amazon files for performance testing.

Each copy k (k = 0..N-1) duplicates every data row with order / adjustment IDs
suffixed "-k" (so record_refs stay unique and still pair up), and the settlement
header total-amount is multiplied by N, so the scaled run must still tie out.

    python scripts/make_large.py 10 bench/
"""
import sys
from decimal import Decimal
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "data"


def main(n: int, out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    # payments: keep the preamble + header once, repeat data rows
    lines = (SRC / "amazon_payments_data.csv").read_text(encoding="utf-8-sig").splitlines()
    hdr = next(i for i, l in enumerate(lines) if l.startswith('"date/time"'))
    with open(out / f"payments_x{n}.csv", "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines[: hdr + 1]) + "\n")
        cols = lines[hdr].split('","')
        oid = [c.strip('"') for c in cols].index("order ID")
        for k in range(n):
            for line in lines[hdr + 1:]:
                if not line.strip():
                    continue
                cells = line.split('","')
                if cells[oid]:
                    cells[oid] = f"{cells[oid]}-{k}"
                fh.write('","'.join(cells) + "\n")
    # settlement: header row once (total x N), repeat data rows
    slines = (SRC / "amazon_settlements_data.txt").read_text(encoding="utf-8").splitlines()
    head = slines[0].split("\t")
    i_oid, i_moid, i_tot = head.index("order-id"), head.index("merchant-order-id"), head.index("total-amount")
    with open(out / f"settlements_x{n}.txt", "w", encoding="utf-8", newline="\n") as fh:
        fh.write(slines[0] + "\n")
        for line in slines[1:]:
            c = line.split("\t")
            if c[i_tot]:
                c[i_tot] = str(Decimal(c[i_tot]) * n)
                fh.write("\t".join(c) + "\n")
        for k in range(n):
            for line in slines[1:]:
                c = line.split("\t")
                if c[i_tot]:
                    continue
                for i in (i_oid, i_moid):
                    if c[i]:
                        c[i] = f"{c[i]}-{k}"
                fh.write("\t".join(c) + "\n")


if __name__ == "__main__":
    main(int(sys.argv[1]), Path(sys.argv[2]))
