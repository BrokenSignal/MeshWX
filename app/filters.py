"""Alert include/exclude filtering.

Default policy:
  INCLUDE  event exactly "Tornado Watch" (and "Tornado Warning")
  INCLUDE  any event ending in "Warning"
  EXCLUDE  everything else: advisories, statements, outlooks, and all
           non-tornado watches (Severe Thunderstorm Watch, etc.)

The rules are data-driven so the Settings page can edit them.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FilterRules:
    include_exact: list[str]
    include_suffix: list[str]
    exclude_exact: list[str]

    @classmethod
    def from_settings(cls, settings: dict) -> "FilterRules":
        return cls(
            include_exact=list(settings.get("filter_include_exact", [])),
            include_suffix=list(settings.get("filter_include_suffix", [])),
            exclude_exact=list(settings.get("filter_exclude_exact", [])),
        )


def should_include(event: str, rules: FilterRules) -> bool:
    """Return True if an alert with this event name should be broadcast."""
    event = (event or "").strip()
    if not event:
        return False
    if event in rules.exclude_exact:
        return False
    if event in rules.include_exact:
        return True
    for suffix in rules.include_suffix:
        if suffix and event.endswith(suffix):
            return True
    return False
