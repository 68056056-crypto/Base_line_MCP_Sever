"""Unit tests with mocked upstream APIs (no network). Run: pytest -q"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import seismic_mcp_server as srv  # noqa: E402
from mcp.shared.exceptions import MCPError  # noqa: E402

USGS_PAYLOAD = {
    "type": "FeatureCollection",
    "features": [
        {
            "type": "Feature",
            "id": "us7000abcd",
            "properties": {"mag": 5.1, "place": "10 km N of Somewhere", "time": 1772361000000, "url": "https://x/us7000abcd"},
            "geometry": {"type": "Point", "coordinates": [98.5, 18.2, 12.3]},
        }
    ],
}


FIXTURE = Path(__file__).parent / "fixtures" / "tmd_real_sample.json"
TMD_PAYLOAD = json.loads(FIXTURE.read_text(encoding="utf-8"))  # real response captured 2026-11-08


@pytest.fixture
def mock_http(monkeypatch):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if request.url.host == "earthquake.usgs.gov":
            return httpx.Response(200, json=USGS_PAYLOAD)
        if request.url.host == "data.tmd.go.th":
            return httpx.Response(200, json=TMD_PAYLOAD)
        return httpx.Response(404)

    monkeypatch.setattr(srv, "new_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    monkeypatch.setattr(srv, "today_local", lambda: srv.date(2026, 11, 8))
    srv._tmd_cache.clear()
    return calls


def run(coro):
    return asyncio.run(coro)


def call(name, args):
    params = srv.types.CallToolRequestParams(name=name, arguments=args)
    return run(srv.handle_call_tool(None, params))


def test_usgs_mapping(mock_http):
    r = call("usgs_search_earthquakes", {"starttime": "2026-03-01", "endtime": "2026-03-07"})
    ev = r.structured_content["events"][0]
    assert ev["id"] == "us7000abcd" and ev["source"] == "USGS"
    assert ev["latitude"] == 18.2 and ev["longitude"] == 98.5 and ev["depth_km"] == 12.3
    assert ev["time"].endswith("Z")
    q = mock_http[0].url.params
    assert q["format"] == "geojson" and q["minmagnitude"] == "4.5" and q["limit"] == "20"
    assert json.loads(r.content[0].text) == r.structured_content


def test_usgs_partial_radius_rejected(mock_http):
    with pytest.raises(MCPError):
        call("usgs_search_earthquakes", {"starttime": "a", "endtime": "b", "latitude": 1})


def test_tmd_real_payload_mapping():
    items = srv.extract_tmd_items(TMD_PAYLOAD)
    assert len(items) == 7  # key is "DailyEarthquakes" in the real API
    ev = srv.tmd_item_to_event(items[0])
    assert ev["time"] == "2026-11-08T15:36:35+07:00"
    assert ev["magnitude"] == 3.2 and ev["depth_km"] == 10.0 and ev["latitude"] == 18.824
    assert ev["place"] == "ประเทศเมียนมา (Myanmar)"
    assert ev["id"] == "TMD-20261108T083635Z_18.824_96.436"
    # truncated OriginThai is replaced by the full TitleThai
    assert srv.tmd_item_to_event(items[2])["place"].endswith("(Andaman Islands, India Region)")


def test_tmd_spec_shape_still_supported():
    spec = {"DailySeismicEvent": [{"EventID": "660001", "DateTime": "2026-03-07T09:30:15+07:00", "Magnitude": 4.6,
                                   "DepthKm": 10.0, "Latitude": 18.1, "Longitude": 98.4, "Location": "Near Chiang Mai"}]}
    ev = srv.tmd_item_to_event(srv.extract_tmd_items(spec)[0])
    assert ev["id"] == "TMD-660001" and ev["place"] == "Near Chiang Mai" and ev["time"] == "2026-03-07T09:30:15+07:00"


def test_tmd_daily_filters_by_thai_date(mock_http):
    r = call("tmd_get_daily_earthquakes", {"date": "2026-11-07"})
    assert r.structured_content["query"] == {"date": "2026-11-07"}
    assert len(r.structured_content["events"]) == 2
    assert all(e["time"].startswith("2026-11-07") for e in r.structured_content["events"])
    q = mock_http[0].url.params
    assert q["uid"] == "api" and q["ukey"] == "api12345" and q["Date"] == "2026-11-07"


def test_tmd_daily_defaults_to_today(mock_http):
    r = call("tmd_get_daily_earthquakes", {})
    assert r.structured_content["query"]["date"] == "2026-11-08"
    assert len(r.structured_content["events"]) == 4


def test_tmd_bad_date(mock_http):
    with pytest.raises(MCPError):
        call("tmd_get_daily_earthquakes", {"date": "07/03/2026"})


def test_tmd_recent_single_fetch_sorted(mock_http):
    r = call("tmd_get_recent_earthquakes", {"days": 2})
    events = r.structured_content["events"]
    assert len(mock_http) == 1 and len(events) == 6
    times = [srv.parse_time(e["time"]) for e in events]
    assert times == sorted(times, reverse=True)
    call("tmd_get_recent_earthquakes", {"days": 3})
    assert len(mock_http) == 1  # served from cache


def test_tmd_search_filters(mock_http):
    r = call("tmd_search_earthquakes", {"place_contains": "เชียงใหม่"}).structured_content
    assert r["total_matched"] == 1 and r["events"][0]["magnitude"] == 1.8
    r = call("tmd_search_earthquakes", {"minmagnitude": 4.0}).structured_content
    assert {e["magnitude"] for e in r["events"]} == {4.2, 4.1}
    r = call("tmd_search_earthquakes", {"latitude": 18.79, "longitude": 98.98, "maxradiuskm": 50}).structured_content
    assert r["total_matched"] == 1 and r["events"][0]["distance_km"] < 50
    r = call("tmd_search_earthquakes", {"startdate": "2026-11-07", "enddate": "2026-11-07", "limit": 1}).structured_content
    assert r["total_matched"] == 2 and len(r["events"]) == 1


def test_upstream_error_is_tool_error(monkeypatch):
    monkeypatch.setattr(
        srv, "new_client",
        lambda: httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(503, text="down"))),
    )
    srv._tmd_cache.clear()
    r = call("tmd_get_daily_earthquakes", {})
    assert r.is_error and "503" in r.content[0].text


def test_unknown_tool_and_extra_args():
    with pytest.raises(MCPError):
        call("nope", {})
    with pytest.raises(MCPError):
        call("tmd_get_recent_earthquakes", {"days": 3, "extra": 1})
    with pytest.raises(MCPError):
        call("tmd_get_recent_earthquakes", {"days": 30})


def test_payout_custom_curve():
    event = {"id": "e", "magnitude": 6.0, "latitude": 0.0, "longitude": 0.0}
    assets = [{"asset_id": "A", "latitude": 0.0, "longitude": 0.5, "sum_insured": 100.0}]  # ~55.6 km
    r = call("compute_parametric_payout", {
        "event": event, "assets": assets,
        "payout_curve": [{"max_distance_km": 100, "payout_percent": 0.3}, {"max_distance_km": 50, "payout_percent": 0.9}],
    })
    p = r.structured_content["asset_payouts"][0]
    assert 55 < p["distance_km"] < 56 and p["payout_percent"] == 0.3 and p["payout_amount"] == 30.0


def test_payout_beyond_trigger_distance():
    event = {"id": "e", "magnitude": 6.0, "latitude": 0.0, "longitude": 0.0}
    assets = [{"asset_id": "A", "latitude": 0.0, "longitude": 1.0, "sum_insured": 100.0}]  # ~111 km
    r = call("compute_parametric_payout", {"event": event, "assets": assets, "max_distance_km_trigger": 100})
    assert r.structured_content["total_payout"] == 0
