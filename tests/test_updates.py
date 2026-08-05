"""Update-check: version comparison + the /settings/check-updates endpoint."""
import pytest

from app.web.routes import _parse_version, _is_newer


def test_parse_version_strips_v_and_suffix():
    assert _parse_version("v1.2.10") == (1, 2, 10)
    assert _parse_version("1.2.0-rc1") == (1, 2, 0)
    assert _parse_version("V2.0") == (2, 0)
    assert _parse_version("") == ()


@pytest.mark.parametrize("latest,current,expected", [
    ("1.1.2", "1.1.1", True),
    ("v1.2.0", "1.1.9", True),
    ("1.1.1", "1.1.1", False),     # same
    ("1.1.0", "1.1.1", False),     # older
    ("1.2", "1.1.9", True),        # uneven lengths: 1.2.0 > 1.1.9
    ("1.10.0", "1.9.0", True),     # numeric, not lexical
    ("", "1.1.1", False),          # unparseable latest -> no update
])
def test_is_newer(latest, current, expected):
    assert _is_newer(latest, current) is expected


# ---- endpoint (mock GitHub) ------------------------------------------------

class _Resp:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload or {}

    def json(self):
        return self._payload


class _Client:
    resp = _Resp(200, {})

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        return _Client.resp


@pytest.fixture
def client(monkeypatch):
    import app.web.routes as routes
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    monkeypatch.setattr(routes.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(routes, "__version__", "1.1.1")

    class _FakeDb:
        def get_setting(self, k, default=None):
            return default
    app = FastAPI()
    app.state.db = _FakeDb()
    app.include_router(routes.router)
    return TestClient(app)


def test_endpoint_reports_update_available(client):
    _Client.resp = _Resp(200, {"tag_name": "v1.2.0",
                               "html_url": "https://github.com/BrokenSignal/MeshWX/releases/tag/v1.2.0"})
    r = client.get("/settings/check-updates")
    assert r.status_code == 200
    assert "Update available" in r.text
    assert "v1.2.0" in r.text
    assert "releases/tag/v1.2.0" in r.text


def test_endpoint_reports_up_to_date(client):
    _Client.resp = _Resp(200, {"tag_name": "v1.1.1", "html_url": "x"})
    r = client.get("/settings/check-updates")
    assert "Up to date" in r.text


def test_endpoint_handles_no_releases(client):
    _Client.resp = _Resp(404)
    r = client.get("/settings/check-updates")
    assert "Up to date" in r.text  # 404 = no releases yet, treated as current


def test_endpoint_handles_github_error(client):
    _Client.resp = _Resp(503)
    r = client.get("/settings/check-updates")
    assert "Check failed" in r.text
    assert "releases" in r.text.lower()   # link to the releases page still offered
