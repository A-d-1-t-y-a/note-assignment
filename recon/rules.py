"""Config rule matching and record_ref construction (pure, no DB).

Matching semantics (documented in README "Config interpretation"):

* Every raw value and config value is compared through `match_key`
  (upper-case, all separators removed).
* transaction_type: exact match beats the catch-all rule with an EMPTY
  transaction_type. Empty-type rules are only used when no rule for the row's
  own transaction type exists for that amount field.
* description / amount_type / amount_description: exact > prefix > `any`.
  Prefix matching exists because Amazon appends variable data to some
  descriptions (e.g. config "TO_ACCOUNT_ENDING" vs raw
  "To account ending with: 334").
* The most specific rule wins. If several rules tie at the same specificity
  ALL of them are applied and the lines are flagged MULTI_RULE, so that a
  duplicated config row surfaces as a numeric variance instead of being
  silently hidden by "first match wins".
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .normalize import is_wildcard, match_key

# record_ref template tokens that resolve to row values. Anything else is a literal.
FIELD_TOKENS = {"txn_ref", "sku", "date", "settlement_id", "description",
                "shipment_id", "merchant_order_id"}
MIN_PREFIX_LEN = 6


@dataclass(frozen=True)
class Rule:
    table: str
    rule_id: int
    match_values: tuple[str, ...]      # config values of the match dimensions (raw)
    amount_field: str | None           # payments only
    record_ref: str
    positive_field: str | None
    negative_field: str | None
    match_keys: tuple[str, ...] = field(default=(), compare=False)

    def summary_field_for(self, amount) -> str | None:
        if amount > 0:
            return self.positive_field
        if amount < 0:
            return self.negative_field
        return None


def make_rule(table, rule_id, match_values, amount_field, record_ref, pos, neg) -> Rule:
    mv = tuple((v or "").strip() for v in match_values)
    keys = tuple("*" if is_wildcard(v) else match_key(v) for v in mv)
    return Rule(table, rule_id, mv, (amount_field or "").strip() or None,
                (record_ref or "").strip(), (pos or "").strip() or None,
                (neg or "").strip() or None, keys)


def _score_type(rule_key: str, value_key: str):
    if rule_key == value_key and rule_key not in ("", "*"):
        return (2, 0, "exact")
    if rule_key == "*":
        return (1, 0, "any")
    if rule_key == "":
        return (0, 0, "fallback")
    return None


def _score_desc(rule_key: str, value_key: str):
    if rule_key == value_key:
        return (3, len(rule_key), "exact")
    if rule_key in ("*", ""):
        return (1, 0, "any")
    if len(rule_key) >= MIN_PREFIX_LEN and value_key.startswith(rule_key):
        return (2, len(rule_key), "prefix")
    return None


class RuleSet:
    """Index over one mapping table. `match()` returns the winning rules."""

    def __init__(self, rules: list[Rule], uses_amount_field: bool):
        self.rules = sorted(rules, key=lambda r: r.rule_id)
        self.uses_amount_field = uses_amount_field
        self._cache: dict = {}
        self._by_field: dict[str | None, list[Rule]] = {}
        for r in self.rules:
            self._by_field.setdefault(r.amount_field if uses_amount_field else None, []).append(r)

    def match(self, values: tuple[str, ...], amount_field: str | None = None):
        """Return (rules, tier). rules is [] if nothing matches."""
        keys = tuple(match_key(v) for v in values)
        ck = (keys, amount_field)
        hit = self._cache.get(ck)
        if hit is not None:
            return hit
        candidates = self._by_field.get(amount_field if self.uses_amount_field else None, [])
        best_score, best, best_tier = None, [], ""
        for r in candidates:
            s0 = _score_type(r.match_keys[0], keys[0])
            if s0 is None:
                continue
            score, tiers = [s0[:2]], [f"type={s0[2]}"]
            ok = True
            for rk, vk in zip(r.match_keys[1:], keys[1:]):
                s = _score_desc(rk, vk)
                if s is None:
                    ok = False
                    break
                score.append(s[:2])
                tiers.append(s[2])
            if not ok:
                continue
            st = tuple(score)
            if best_score is None or st > best_score:
                best_score, best, best_tier = st, [r], ",".join(tiers)
            elif st == best_score:
                best.append(r)
        result = (best, best_tier)
        self._cache[ck] = result
        return result


def build_record_ref(template: str, values: dict[str, str]) -> str:
    """'txn_ref+sku+date' + row values -> '249-...|BIO-..|2026-07-17'."""
    parts = []
    for tok in template.split("+"):
        t = tok.strip()
        parts.append((values.get(t) or "") if t in FIELD_TOKENS else t)
    return "|".join(parts)


def template_unknown_tokens(template: str) -> list[str]:
    """Lower-case tokens that look like field references but are not known fields
    (e.g. 'record_type'). They would be treated as literals."""
    out = []
    for tok in template.split("+"):
        t = tok.strip()
        if t and t == t.lower() and t not in FIELD_TOKENS and any(c.isalpha() for c in t):
            out.append(t)
    return out
