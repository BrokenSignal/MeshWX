"""Normalized representation of an NWS alert feature."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field


@dataclass
class Alert:
    nws_id: str
    event: str
    headline: str
    area_desc: str
    effective: str
    expires: str
    message_type: str  # "Alert", "Update", "Cancel"
    ends: str = ""      # when the HAZARD ends (for "until"); falls back to expires
    onset: str = ""     # when the hazard STARTS (for the upcoming-window display)
    detail: str = ""    # SPS threat summary, e.g. "Strong thunderstorm (60 mph wind)"
    references: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)

    @classmethod
    def from_feature(cls, feature: dict) -> "Alert":
        props = feature.get("properties", {}) or {}
        refs = []
        for ref in props.get("references", []) or []:
            rid = ref.get("@id") or ref.get("identifier")
            if rid:
                refs.append(rid)
        event = (props.get("event") or "").strip()
        return cls(
            nws_id=feature.get("id") or props.get("id") or props.get("@id") or "",
            event=event,
            headline=(props.get("headline") or "").strip(),
            area_desc=(props.get("areaDesc") or "").strip(),
            effective=props.get("effective") or props.get("onset") or "",
            expires=props.get("expires") or props.get("ends") or "",
            message_type=(props.get("messageType") or "Alert").strip(),
            ends=props.get("ends") or props.get("expires") or "",
            onset=props.get("onset") or props.get("effective") or "",
            detail=cls._sps_detail(props) if event == "Special Weather Statement" else "",
            references=refs,
            raw=feature,
        )

    @staticmethod
    def _sps_detail(props: dict) -> str:
        """Condense a Special Weather Statement's NWSheadline into a short threat
        summary, e.g. "Strong thunderstorm (60 mph wind, 0.75in hail)". Returns ""
        when nothing useful is present (caller falls back to the event name)."""
        params = props.get("parameters", {}) or {}
        hl = params.get("NWSheadline") or []
        threat = ""
        if hl:
            text = re.split(r"\bWILL\b", hl[0], maxsplit=1)[0].strip()
            text = re.sub(r"^(A|AN|THE)\s+", "", text, flags=re.IGNORECASE).strip()
            if text:
                threat = text[0].upper() + text[1:].lower()
        impacts = []
        wind = params.get("maxWindGust") or []
        if wind:
            impacts.append(f"{str(wind[0]).strip().lower()} wind")
        hail = params.get("maxHailSize") or []
        if hail:
            try:
                impacts.append(f"{('%g' % float(hail[0]))}in hail")
            except (ValueError, TypeError):
                pass
        if impacts:
            threat = (threat + " " if threat else "") + "(" + ", ".join(impacts) + ")"
        return threat.strip()

    def content_hash(self) -> str:
        """Hash of the fields that determine whether a rebroadcast is warranted."""
        basis = f"{self.event}|{self.headline}|{self.expires}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]
