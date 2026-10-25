"""`wait_for`: a thin proxy to `GET /api/automation/events` (contract C3).

Offline: the real `_api` runs against an httpx mock transport; the endpoint itself is story 04.
"""
from __future__ import annotations

import httpx
import pytest

import mcp_server as m


@pytest.fixture()
def wire(monkeypatch):
    seen: list[tuple[httpx.Request, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"events": [], "next": "boot:3", "timed_out": True,
                                         "gap": False, "reset": False})

    client = httpx.Client(transport=httpx.MockTransport(handler))

    def fake_request(method, url, **kw):
        resp = client.request(method, url, params=kw.get("params"), json=kw.get("json"),
                              headers=kw.get("headers"))
        seen.append((resp.request, kw))
        return resp

    monkeypatch.setattr(m.httpx, "request", fake_request)
    return seen


def test_full_call_maps_to_the_endpoint(wire):
    res = m.wait_for("recording:rec_1", events=["live_segment", "status"], after="boot:2", timeout=5)
    req, kw = wire[0]
    assert (req.method, req.url.path) == ("GET", "/api/automation/events")
    assert dict(req.url.params) == {"channel": "recording:rec_1", "events": "live_segment,status",
                                    "after": "boot:2", "timeout": "5"}
    assert kw["timeout"] == 15
    assert "X-Recall-Actor" not in req.headers
    assert res["next"] == "boot:3"


def test_none_values_are_omitted_and_default_timeout(wire):
    m.wait_for("doc_import")
    req, kw = wire[0]
    assert dict(req.url.params) == {"channel": "doc_import", "timeout": "25"}
    assert kw["timeout"] == 35


def test_entry_is_a_system_read():
    e = m._TOOL_CLASS["wait_for"]
    assert (e["class"], e["provenance_kind"], e["open_world"], e["limits"]) == ("R", "system", False, None)
