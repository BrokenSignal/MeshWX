"""Filter logic: which events are broadcast vs dropped."""
from app.config import DEFAULT_SETTINGS
from app.filters import FilterRules, should_include
from app.models import Alert

RULES = FilterRules.from_settings(DEFAULT_SETTINGS)


def _event(feature_loader, name):
    return Alert.from_feature(feature_loader(name)).event


def test_tornado_watch_included(feature):
    assert should_include(_event(feature, "tornado_watch"), RULES) is True


def test_tornado_warning_included(feature):
    assert should_include(_event(feature, "tornado_warning"), RULES) is True


def test_severe_thunderstorm_watch_excluded(feature):
    assert should_include(_event(feature, "severe_tstorm_watch"), RULES) is False


def test_lake_wind_advisory_excluded(feature):
    assert should_include(_event(feature, "lake_wind_advisory"), RULES) is False


def test_flash_flood_warning_included(feature):
    assert should_include(_event(feature, "flash_flood_warning"), RULES) is True


def test_special_weather_statement_excluded(feature):
    assert should_include(_event(feature, "special_weather_statement"), RULES) is False


def test_generic_warning_suffix_included():
    assert should_include("Winter Storm Warning", RULES) is True


def test_advisory_and_outlook_excluded():
    assert should_include("Wind Advisory", RULES) is False
    assert should_include("Hazardous Weather Outlook", RULES) is False


def test_exclude_exact_overrides_include():
    rules = FilterRules(
        include_exact=[],
        include_suffix=["Warning"],
        exclude_exact=["Test Warning"],
    )
    assert should_include("Test Warning", rules) is False
    assert should_include("Flood Warning", rules) is True


def test_empty_event_excluded():
    assert should_include("", RULES) is False
