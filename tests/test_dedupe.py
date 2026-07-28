"""Dedupe: sent / duplicate / update / cancelled detection."""
from app.config import DEFAULT_SETTINGS
from app.dedupe import decide
from app.filters import FilterRules
from app.models import Alert

RULES = FilterRules.from_settings(DEFAULT_SETTINGS)


class FakeState:
    """In-memory stand-in for the db state lookup."""

    def __init__(self):
        self.rows = {}

    def lookup(self, nws_id):
        return self.rows.get(nws_id)

    def record(self, alert: Alert, disposition: str):
        self.rows[alert.nws_id] = {
            "disposition": disposition,
            "msg_hash": alert.content_hash(),
            "headline": alert.headline,
            "expires": alert.expires,
        }


def test_new_alert_is_sent(feature):
    state = FakeState()
    a = Alert.from_feature(feature("flash_flood_warning"))
    d = decide(a, RULES, state.lookup)
    assert d.disposition == "sent" and d.transmit is True


def test_same_alert_reseen_is_duplicate(feature):
    state = FakeState()
    a = Alert.from_feature(feature("flash_flood_warning"))
    state.record(a, "sent")
    # Re-poll returns the identical feature.
    d = decide(a, RULES, state.lookup)
    assert d.disposition == "duplicate" and d.transmit is False


def test_update_reference_chain_is_update(feature):
    state = FakeState()
    original = Alert.from_feature(feature("flash_flood_warning"))
    state.record(original, "sent")
    upd = Alert.from_feature(feature("flash_flood_warning_update"))
    # New id, references the sent one, expiry + headline changed.
    d = decide(upd, RULES, state.lookup)
    assert d.disposition == "update" and d.transmit is True


def test_update_without_material_change_is_duplicate(feature):
    state = FakeState()
    original = Alert.from_feature(feature("flash_flood_warning"))
    state.record(original, "sent")
    # An "update" that did not change headline/expiry -> duplicate.
    upd_feature = feature("flash_flood_warning_update")
    upd_feature["properties"]["headline"] = original.headline
    upd_feature["properties"]["expires"] = original.expires
    upd_feature["properties"]["ends"] = original.expires
    upd = Alert.from_feature(upd_feature)
    d = decide(upd, RULES, state.lookup)
    assert d.disposition == "duplicate" and d.transmit is False


def test_cancel_of_sent_alert_notifies(feature):
    state = FakeState()
    original = Alert.from_feature(feature("flash_flood_warning"))
    state.record(original, "sent")
    cancel = Alert.from_feature(feature("flash_flood_warning_cancel"))
    d = decide(cancel, RULES, state.lookup)
    assert d.disposition == "cancelled" and d.transmit is True


def test_cancel_of_never_sent_alert_is_filtered(feature):
    state = FakeState()
    cancel = Alert.from_feature(feature("flash_flood_warning_cancel"))
    d = decide(cancel, RULES, state.lookup)
    assert d.disposition == "filtered" and d.transmit is False


def test_excluded_event_is_filtered(feature):
    state = FakeState()
    a = Alert.from_feature(feature("severe_tstorm_watch"))
    d = decide(a, RULES, state.lookup)
    assert d.disposition == "filtered" and d.transmit is False
