"""Dedupe / update / cancel decision logic.

Pure function over an alert plus a lookup of prior state, so it is unit
testable without a database. Dispositions:

  sent       first broadcast of a new, included alert
  duplicate  already broadcast, nothing material changed
  update     supersedes a previously-sent alert; headline or expiry changed
  cancelled  early cancellation of a previously-sent alert
  filtered   event not selected by the include rules
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .filters import FilterRules, should_include
from .models import Alert

# A "prior state" is any mapping with these keys (sqlite3.Row or dict):
#   disposition, msg_hash, headline, expires
StateLookup = Callable[[str], Optional[dict]]

_SENT_DISPOSITIONS = ("sent", "update")


@dataclass
class Decision:
    disposition: str
    transmit: bool
    detail: str = ""


def _is_sent(state: Optional[dict]) -> bool:
    return bool(state) and state["disposition"] in _SENT_DISPOSITIONS


def decide(alert: Alert, rules: FilterRules, lookup: StateLookup) -> Decision:
    if not should_include(alert.event, rules):
        return Decision("filtered", False, "event not in include rules")

    new_hash = alert.content_hash()

    if alert.message_type == "Cancel":
        candidates = [lookup(alert.nws_id)] + [lookup(r) for r in alert.references]
        if any(_is_sent(s) for s in candidates):
            return Decision("cancelled", True, "early cancellation")
        return Decision("filtered", False, "cancel of alert never sent")

    prior = lookup(alert.nws_id)
    if _is_sent(prior):
        if prior["msg_hash"] == new_hash:
            return Decision("duplicate", False, "same alert id, unchanged")
        return Decision("update", True, "same alert id, content changed")

    # An Update carries a new id but references the id(s) it supersedes.
    for ref_id in alert.references:
        ref = lookup(ref_id)
        if _is_sent(ref):
            material = (
                ref["headline"] != alert.headline
                or ref["expires"] != alert.expires
            )
            if material and ref["msg_hash"] != new_hash:
                return Decision("update", True, "supersedes an earlier alert")
            return Decision("duplicate", False, "duplicate of an earlier alert")

    return Decision("sent", True, "new alert")
