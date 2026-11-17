"""Pure normalisation helpers (no DB). Unit-tested in tests/test_normalize.py."""
from __future__ import annotations

import re
from functools import lru_cache
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation

WILDCARD = "ANY"

_NON_ALNUM = re.compile(r"[^A-Z0-9]")


@lru_cache(maxsize=65536)
def match_key(value: str | None) -> str:
    """Key used to compare raw values with config values.

    Case-insensitive and separator-insensitive: 'Service fee', 'SERVICE_FEE',
    'service-fee' and 'ServiceFee' all become 'SERVICEFEE'. Non-ASCII separators
    such as the en-dash in one config row are dropped too.
    """
    if value is None:
        return ""
    return _NON_ALNUM.sub("", value.strip().upper())


def is_wildcard(value: str | None) -> bool:
    return (value or "").strip().upper() == WILDCARD


def parse_amount(value: str | None) -> Decimal:
    """'1,234.50' / '-133,756.51' / '' / '0' -> Decimal, quantised to cents."""
    s = (value or "").strip().replace(",", "")
    if s in ("", "-"):
        return Decimal("0.00")
    try:
        return Decimal(s).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"unparseable amount {value!r}") from exc


_MONTH_NAMES = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
                "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
# full names and the usual abbreviations only ('Jul', 'Sept'); anything else is rejected
_MONTHS = {**{n: i for i, n in enumerate(_MONTH_NAMES, 1)},
           **{n[:3]: i for i, n in enumerate(_MONTH_NAMES, 1)}, "SEPT": 9}
_PAY_DT = re.compile(
    r"^\s*(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})\s+(\d{1,2}):(\d{2}):(\d{2})\s*([ap]m)?\s*"
    r"(?:(?:GMT|UTC)\s*([+-]\d{1,2})(?::?(\d{2}))?)?\s*$",
    re.IGNORECASE,
)


def parse_payment_datetime(value: str | None) -> datetime | None:
    """'29 June 2026 5:39:32 pm GMT+9' / '1 Aug 2026 9:06:35 am GMT+9' -> aware UTC datetime."""
    if not value or not value.strip():
        return None
    m = _PAY_DT.match(value)
    if not m:
        raise ValueError(f"unparseable payments date {value!r}")
    d, mon, y, hh, mm, ss, ampm, off_h, off_m = m.groups()
    month = _MONTHS.get(mon.upper().rstrip('.'))
    if not month:
        raise ValueError(f"unknown month in {value!r}")
    hour = int(hh)
    if ampm:
        hour = hour % 12 + (12 if ampm.lower() == "pm" else 0)
    sign = -1 if (off_h or "+0").startswith("-") else 1
    offset = timedelta(hours=abs(int(off_h or 0)), minutes=int(off_m or 0)) * sign
    local = datetime(int(y), month, int(d), hour, int(mm), int(ss), tzinfo=timezone(offset))
    return local.astimezone(timezone.utc)


def parse_settlement_datetime(value: str | None) -> datetime | None:
    """'17.07.2026 07:26:32 UTC' -> aware UTC datetime; '17.07.2026' -> midnight UTC."""
    if not value or not value.strip():
        return None
    s = value.strip()
    if s.upper().endswith("UTC"):
        s = s[:-3].strip()
    for fmt in ("%d.%m.%Y %H:%M:%S", "%d.%m.%Y"):
        try:
            return datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    raise ValueError(f"unparseable settlement date {value!r}")


def fmt_date(d: date | None) -> str:
    return d.isoformat() if d else ""
