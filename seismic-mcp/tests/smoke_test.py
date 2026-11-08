"""End-to-end smoke test over Streamable HTTP.

Usage:
    python tests/smoke_test.py [URL] [--live]

URL defaults to http://localhost:8000/mcp. Without --live only the offline
analytics tools are called; with --live the USGS/TMD tools are called too
(the machine needs internet access to those APIs).
"""

import asyncio
import json
import sys

from mcp import Client
from mcp.shared.exceptions import MCPError

URL = next((a for a in sys.argv[1:] if a.startswith("http")), "http://localhost:8000/mcp")
LIVE = "--live" in sys.argv

EVENT = {
    "id": "TMD-660001",
    "time": "2026-03-07T09:30:15+07:00",
    "place": "Near Chiang Mai, Thailand",
    "magnitude": 5.7,
    "latitude": 18.79,
    "longitude": 98.98,
    "depth_km": 10.0,
    "source": "TMD_DailySeismicEvent",
    "url": None,
    "raw": {},
}
ASSETS = [
    {"asset_id": "ASSET-001", "name": "Warehouse Chiang Mai", "latitude": 18.80, "longitude": 99.00, "sum_insured": 1_000_000},
    {"asset_id": "ASSET-002", "name": "Factory Lamphun", "latitude": 18.20, "longitude": 99.20, "sum_insured": 2_000_000},
    {"asset_id": "ASSET-003", "name": "Office Bangkok", "latitude": 13.75, "longitude": 100.50, "sum_insured": 5_000_000},
]


async def main() -> None:
    async with Client(URL) as client:
        tools = await client.list_tools()
        names = sorted(t.name for t in tools.tools)
        print("tools:", names)
        assert len(names) == 6, names

        r = await client.call_tool("match_events_to_assets", {"events": [EVENT], "assets": ASSETS})
        assert not r.is_error, r
        matched = [m["asset_id"] for m in r.structured_content["matches"]]
        print("match_events_to_assets ->", matched)
        assert matched == ["ASSET-001", "ASSET-002"] or set(matched) == {"ASSET-001", "ASSET-002"}

        r = await client.call_tool("compute_parametric_payout", {"event": EVENT, "assets": ASSETS})
        assert not r.is_error, r
        sc = r.structured_content
        print("compute_parametric_payout ->", json.dumps(
            [(p["asset_id"], p["distance_km"], p["payout_percent"], p["payout_amount"]) for p in sc["asset_payouts"]]
        ), "total", sc["total_payout"])
        assert sc["asset_payouts"][0]["payout_percent"] == 0.5
        assert sc["asset_payouts"][2]["payout_amount"] == 0

        low = dict(EVENT, magnitude=4.0)
        r = await client.call_tool("compute_parametric_payout", {"event": low, "assets": ASSETS})
        assert r.structured_content["total_payout"] == 0
        print("below-trigger event -> total 0 ✓")

        try:
            await client.call_tool("usgs_search_earthquakes", {"starttime": "2026-03-01"})
            raise AssertionError("expected validation error")
        except MCPError as exc:
            print("validation error ✓", exc.error.code, exc.error.message)

        if LIVE:
            r = await client.call_tool(
                "usgs_search_earthquakes",
                {"starttime": "2026-03-01", "endtime": "2026-03-07", "minmagnitude": 4.5, "limit": 5},
            )
            print("usgs ->", "ERROR" if r.is_error else len(r.structured_content["events"]), "events")
            r = await client.call_tool("tmd_get_recent_earthquakes", {"days": 3})
            print("tmd recent ->", "ERROR" if r.is_error else len(r.structured_content["events"]), "events")

    print("ALL OK")


if __name__ == "__main__":
    asyncio.run(main())
