"""Seismic MCP Server — seismic data & parametric insurance analytics.

Transports
----------
* streamable-http (default) — MCP Streamable HTTP endpoint at ``http://HOST:PORT/mcp``
* stdio                     — Claude Desktop style (``MCP_TRANSPORT=stdio``)

Data sources (per spec): USGS FDSN Event API and TMD DailySeismicEvent demo API.

Environment variables
---------------------
MCP_TRANSPORT     streamable-http | stdio            (default: streamable-http)
HOST              bind address                       (default: 0.0.0.0)
PORT              bind port                          (default: 8000)
MCP_PATH          MCP endpoint path                  (default: /mcp)
MCP_STATELESS     "true" → stateless HTTP sessions   (default: false)
MCP_JSON_RESPONSE "true" → JSON instead of SSE body  (default: false)
ALLOWED_HOSTS     comma list for DNS-rebinding guard, e.g. "localhost:*,127.0.0.1:*"
                  (empty → guard disabled; set it when exposed beyond localhost)
ALLOWED_ORIGINS   comma list of allowed Origin headers (used with ALLOWED_HOSTS)
TMD_CACHE_TTL     seconds to cache the TMD feed       (default: 60)
SEISMIC_TZ        timezone used for "today" in TMD tools (default: Asia/Bangkok)
HTTP_TIMEOUT      upstream HTTP timeout in seconds   (default: 20)
LOG_LEVEL         DEBUG | INFO | WARNING ...         (default: INFO)

All logs go to stderr (never stdout), so stdio mode stays protocol-clean.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import httpx
import jsonschema
import mcp_types as types
from mcp.server import Server
from mcp.shared.exceptions import MCPError
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

SERVER_NAME = "seismic-mcp-server"
SERVER_VERSION = "0.1.0"

USGS_URL = "https://earthquake.usgs.gov/fdsnws/event/1/query"
TMD_URL = "https://data.tmd.go.th/api/DailySeismicEvent/v1/"
TMD_DEMO_PARAMS = {"uid": "api", "ukey": "api12345", "format": "json"}

LOCAL_TZ = ZoneInfo(os.environ.get("SEISMIC_TZ", "Asia/Bangkok"))
HTTP_TIMEOUT = float(os.environ.get("HTTP_TIMEOUT", "20"))
USER_AGENT = f"{SERVER_NAME}/{SERVER_VERSION}"

logging.basicConfig(
    stream=sys.stderr,
    level=os.environ.get("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("seismic-mcp")


class UpstreamError(Exception):
    """Failure calling or parsing an external API (reported as a tool error)."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088  # mean Earth radius (km)
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def today_local() -> date:
    return datetime.now(LOCAL_TZ).date()


def parse_time(value: str | None) -> datetime:
    """Parse ISO8601 for sorting; naive times are treated as SEISMIC_TZ."""
    if not value:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return dt if dt.tzinfo else dt.replace(tzinfo=LOCAL_TZ)


def to_float(value: Any) -> float | None:
    try:
        return None if value is None or value == "" else float(value)
    except (TypeError, ValueError):
        return None


def pick(obj: dict, *keys: str) -> Any:
    """Case-insensitive lookup over several candidate keys (TMD field names vary)."""
    lowered = {str(k).lower(): v for k, v in obj.items()}
    for key in keys:
        if key.lower() in lowered and lowered[key.lower()] not in (None, ""):
            return lowered[key.lower()]
    return None


def validate_date(value: str, field: str) -> str:
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise MCPError(types.INVALID_PARAMS, f"'{field}' must be YYYY-MM-DD, got {value!r}") from None


async def http_get_json(client: httpx.AsyncClient, url: str, params: dict[str, Any]) -> Any:
    try:
        resp = await client.get(url, params=params)
    except httpx.HTTPError as exc:
        raise UpstreamError(f"Request to {url} failed: {exc}") from exc
    if resp.status_code >= 400:
        raise UpstreamError(f"{url} returned HTTP {resp.status_code}: {resp.text[:300]}")
    try:
        return resp.json()
    except ValueError as exc:
        raise UpstreamError(f"{url} returned non-JSON body: {resp.text[:300]}") from exc


def new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=HTTP_TIMEOUT, headers={"User-Agent": USER_AGENT})


# ---------------------------------------------------------------------------
# Normalisation → EarthquakeEvent
# ---------------------------------------------------------------------------

def usgs_feature_to_event(feature: dict) -> dict:
    props = feature.get("properties") or {}
    coords = (feature.get("geometry") or {}).get("coordinates") or [None, None, None]
    coords = list(coords) + [None] * (3 - len(coords))
    t = props.get("time")
    iso = (
        datetime.fromtimestamp(t / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if isinstance(t, (int, float))
        else None
    )
    return {
        "id": str(feature.get("id")),
        "time": iso,
        "place": props.get("place"),
        "magnitude": to_float(props.get("mag")),
        "latitude": to_float(coords[1]),
        "longitude": to_float(coords[0]),
        "depth_km": to_float(coords[2]),
        "source": "USGS",
        "url": props.get("url"),
        "raw": feature,
    }


TH_TZ = timezone(timedelta(hours=7))


def _tmd_time(item: dict) -> str | None:
    """Return ISO8601 with offset.

    Real TMD payload (verified 2026-11-08) gives naive strings like
    ``"2026-11-08 08:36:35.000"`` in ``DateTimeUTC`` and ``DateTimeThai`` (UTC+7).
    The spec example uses ``DateTime`` with an explicit offset — both are handled.
    """
    candidates = (("DateTime", None), ("DateTimeThai", TH_TZ), ("DateTimeUTC", timezone.utc), ("OriginTime", None))
    for key, naive_tz in candidates:
        raw = pick(item, key)
        if raw is None:
            continue
        try:
            dt = datetime.fromisoformat(str(raw).strip().replace(" ", "T").replace("Z", "+00:00"))
        except ValueError:
            return str(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=naive_tz or TH_TZ)
        return dt.astimezone(TH_TZ).isoformat(timespec="seconds")
    return None


def _tmd_place(item: dict) -> str | None:
    # OriginThai is truncated at ~50 chars by TMD; TitleThai carries the full name prefixed with "แผ่นดินไหว ".
    title = pick(item, "TitleThai")
    if title:
        title = str(title).strip()
        return title.removeprefix("แผ่นดินไหว").strip() or title
    return pick(item, "Location", "OriginThai", "OriginEnglish", "Region", "Place")


def tmd_item_to_event(item: dict) -> dict:
    t = _tmd_time(item)
    lat = to_float(pick(item, "Latitude", "Lat"))
    lon = to_float(pick(item, "Longitude", "Lon", "Long"))
    event_id = pick(item, "EventID", "EventId", "ID")
    if event_id is None:
        # Real TMD payload has no id → stable synthetic id from UTC time + epicentre.
        stamp = parse_time(t).astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ") if t else "unknown"
        event_id = f"{stamp}_{lat:.3f}_{lon:.3f}" if lat is not None and lon is not None else stamp
    return {
        "id": f"TMD-{event_id}",
        "time": t,
        "place": _tmd_place(item),
        "magnitude": to_float(pick(item, "Magnitude", "Mag")),
        "latitude": lat,
        "longitude": lon,
        "depth_km": to_float(pick(item, "DepthKm", "Depth", "DepthKM")),
        "source": "TMD_DailySeismicEvent",
        "url": pick(item, "URL", "Url", "Link"),
        "raw": item,
    }


def extract_tmd_items(payload: Any) -> list[dict]:
    """Find the event list. Real API uses ``DailyEarthquakes``; spec example uses ``DailySeismicEvent``."""
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if not isinstance(payload, dict):
        raise UpstreamError("Unexpected TMD response shape")
    items = pick(payload, "DailyEarthquakes", "DailySeismicEvent", "DailyEarthquake", "Earthquakes")
    if items is None:
        # fall back to the first list-of-objects value
        items = next((v for v in payload.values() if isinstance(v, list)), [])
    if isinstance(items, dict):  # single item serialised as an object
        items = [items]
    if not isinstance(items, list):
        raise UpstreamError("TMD event list is not an array")
    return [x for x in items if isinstance(x, dict)]


# ---------------------------------------------------------------------------
# Tool schemas (verbatim from spec)
# ---------------------------------------------------------------------------

ASSET_PROPS = {
    "asset_id": {"type": "string"},
    "name": {"type": "string"},
    "latitude": {"type": "number"},
    "longitude": {"type": "number"},
}

TOOL_SCHEMAS: dict[str, tuple[str, dict]] = {
    "usgs_search_earthquakes": (
        "Search earthquakes from the USGS FDSN API with basic filters.",
        {
            "type": "object",
            "properties": {
                "starttime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "endtime": {"type": "string", "description": "YYYY-MM-DD or ISO8601."},
                "minmagnitude": {"type": "number", "description": "Minimum magnitude.", "default": 4.5},
                "maxmagnitude": {"type": "number", "description": "Maximum magnitude, optional."},
                "latitude": {"type": "number", "description": "Center latitude for radius search, optional."},
                "longitude": {"type": "number", "description": "Center longitude for radius search, optional."},
                "maxradiuskm": {"type": "number", "description": "Search radius in kilometers, optional."},
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 200,
                },
            },
            "required": ["starttime", "endtime"],
            "additionalProperties": False,
        },
    ),
    "tmd_get_daily_earthquakes": (
        "Get daily earthquakes from the TMD DailySeismicEvent demo API.",
        {
            "type": "object",
            "properties": {
                "date": {"type": "string", "description": "YYYY-MM-DD. If omitted, use today's date."}
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    "tmd_get_recent_earthquakes": (
        "Get recent earthquakes from TMD over the last N days using DailySeismicEvent.",
        {
            "type": "object",
            "properties": {
                "days": {
                    "type": "integer",
                    "description": "Number of days back from today (including today).",
                    "default": 3,
                    "minimum": 1,
                    "maximum": 7,
                }
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    "tmd_search_earthquakes": (
        "Search earthquakes recorded by the Thai Meteorological Department (กรมอุตุนิยมวิทยา) "
        "since 1 January of the current year — Thailand and the surrounding region "
        "(Myanmar, Laos, Andaman Sea, Sumatra, ...). Filter by Thai-local date range, magnitude, "
        "radius around a point, or a place substring (Thai or English, e.g. 'เชียงใหม่', 'Myanmar'). "
        "Prefer this for questions about earthquakes in or near Thailand.",
        {
            "type": "object",
            "properties": {
                "startdate": {"type": "string", "description": "YYYY-MM-DD (Thai local date), inclusive. Optional."},
                "enddate": {"type": "string", "description": "YYYY-MM-DD (Thai local date), inclusive. Optional."},
                "minmagnitude": {"type": "number", "description": "Minimum magnitude.", "default": 0},
                "maxmagnitude": {"type": "number", "description": "Maximum magnitude, optional."},
                "latitude": {"type": "number", "description": "Center latitude for radius search, optional."},
                "longitude": {"type": "number", "description": "Center longitude for radius search, optional."},
                "maxradiuskm": {"type": "number", "description": "Search radius in kilometers, optional."},
                "place_contains": {
                    "type": "string",
                    "description": "Case-insensitive substring of the place name (Thai or English), optional.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of events returned (newest first).",
                    "default": 50,
                    "minimum": 1,
                    "maximum": 500,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    ),
    "match_events_to_assets": (
        "Match earthquake events to insured assets within a given radius and magnitude threshold.",
        {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "description": "List of EarthquakeEvent objects.",
                    "items": {"type": "object"},
                },
                "assets": {
                    "type": "array",
                    "description": "Insured assets with coordinates.",
                    "items": {
                        "type": "object",
                        "properties": {
                            **ASSET_PROPS,
                            "sum_insured": {"type": "number", "description": "Optional total insured value."},
                        },
                        "required": ["asset_id", "latitude", "longitude"],
                        "additionalProperties": True,
                    },
                },
                "max_distance_km": {
                    "type": "number",
                    "description": "Max distance to consider an asset affected.",
                    "default": 100,
                },
                "min_magnitude": {"type": "number", "description": "Minimum magnitude to consider.", "default": 4.5},
            },
            "required": ["events", "assets"],
            "additionalProperties": False,
        },
    ),
    "compute_parametric_payout": (
        "Compute a simple parametric payout for an earthquake event and a set of assets, "
        "based on distance and magnitude thresholds.",
        {
            "type": "object",
            "properties": {
                "event": {"type": "object", "description": "Single EarthquakeEvent to evaluate."},
                "assets": {
                    "type": "array",
                    "description": "Assets with coordinates and sum_insured.",
                    "items": {
                        "type": "object",
                        "properties": {**ASSET_PROPS, "sum_insured": {"type": "number"}},
                        "required": ["asset_id", "latitude", "longitude", "sum_insured"],
                        "additionalProperties": True,
                    },
                },
                "min_magnitude_trigger": {
                    "type": "number",
                    "description": "Minimum magnitude to trigger any payout.",
                    "default": 5.0,
                },
                "max_distance_km_trigger": {
                    "type": "number",
                    "description": "Max distance for eligibility.",
                    "default": 200,
                },
                "payout_curve": {
                    "type": "array",
                    "description": "Optional distance-based payout curve.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "max_distance_km": {"type": "number"},
                            "payout_percent": {
                                "type": "number",
                                "description": "Fraction of sum_insured (0.0–1.0).",
                            },
                        },
                        "required": ["max_distance_km", "payout_percent"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["event", "assets"],
            "additionalProperties": False,
        },
    ),
}

DEFAULT_PAYOUT_CURVE = [
    {"max_distance_km": 50, "payout_percent": 0.5},
    {"max_distance_km": 100, "payout_percent": 0.25},
    {"max_distance_km": 200, "payout_percent": 0.1},
]


def validate_and_fill(name: str, arguments: dict | None) -> dict:
    """Validate against the tool's inputSchema and apply top-level defaults."""
    schema = TOOL_SCHEMAS[name][1]
    args = dict(arguments or {})
    try:
        jsonschema.Draft202012Validator(schema).validate(args)
    except jsonschema.ValidationError as exc:
        where = "/".join(str(p) for p in exc.absolute_path) or "(root)"
        raise MCPError(types.INVALID_PARAMS, f"Invalid arguments for '{name}' at {where}: {exc.message}") from None
    for key, prop in schema["properties"].items():
        if key not in args and "default" in prop:
            args[key] = prop["default"]
    return args


# ---------------------------------------------------------------------------
# Tool implementations
# ---------------------------------------------------------------------------

async def usgs_search_earthquakes(args: dict) -> dict:
    radius = [args.get(k) for k in ("latitude", "longitude", "maxradiuskm")]
    if any(v is not None for v in radius) and not all(v is not None for v in radius):
        raise MCPError(
            types.INVALID_PARAMS, "Radius search needs all of latitude, longitude and maxradiuskm."
        )
    params: dict[str, Any] = {"format": "geojson"}
    for key in ("starttime", "endtime", "minmagnitude", "maxmagnitude", "latitude", "longitude", "maxradiuskm", "limit"):
        if args.get(key) is not None:
            params[key] = args[key]
    async with new_client() as client:
        data = await http_get_json(client, USGS_URL, params)
    if not isinstance(data, dict) or not isinstance(data.get("features"), list):
        raise UpstreamError("USGS response is not a GeoJSON FeatureCollection")
    events = [usgs_feature_to_event(f) for f in data["features"] if isinstance(f, dict)]
    return {"events": events, "query": params}


# The real TMD endpoint ignores ``Date`` and always returns every event since 1 Jan of the
# current year (~1,400 items in Nov 2026). We still send ``Date`` (as the spec asks), but
# filter by Thai local date ourselves, and cache the feed briefly to avoid refetching it.
TMD_CACHE_TTL = float(os.environ.get("TMD_CACHE_TTL", "60"))
_tmd_cache: dict[str, tuple[float, list[dict]]] = {}
_tmd_lock = asyncio.Lock()


async def fetch_tmd_events(day: str | None = None) -> list[dict]:
    """Fetch TMD feed → de-duplicated EarthquakeEvent list, newest first."""
    key = day or ""
    async with _tmd_lock:
        hit = _tmd_cache.get(key)
        if hit and time.monotonic() - hit[0] < TMD_CACHE_TTL:
            return hit[1]
        params = dict(TMD_DEMO_PARAMS)
        if day:
            params["Date"] = day
        async with new_client() as client:
            items = extract_tmd_items(await http_get_json(client, TMD_URL, params))
        seen: dict[str, dict] = {}
        for item in items:
            event = tmd_item_to_event(item)
            seen.setdefault(event["id"], event)
        events = sorted(seen.values(), key=lambda e: parse_time(e["time"]), reverse=True)
        _tmd_cache[key] = (time.monotonic(), events)
        return events


def thai_date(event: dict) -> str | None:
    return parse_time(event["time"]).astimezone(TH_TZ).date().isoformat() if event.get("time") else None


async def tmd_get_daily_earthquakes(args: dict) -> dict:
    day = validate_date(args["date"], "date") if args.get("date") else today_local().isoformat()
    events = [e for e in await fetch_tmd_events(day) if thai_date(e) == day]
    return {"events": events, "query": {"date": day}}


async def tmd_get_recent_earthquakes(args: dict) -> dict:
    days = args["days"]
    today = today_local()
    dates = {(today - timedelta(days=d)).isoformat() for d in range(days)}
    events = [e for e in await fetch_tmd_events() if thai_date(e) in dates]
    return {"events": events, "query": {"days": days, "dates": sorted(dates, reverse=True)}}


async def tmd_search_earthquakes(args: dict) -> dict:
    start = validate_date(args["startdate"], "startdate") if args.get("startdate") else None
    end = validate_date(args["enddate"], "enddate") if args.get("enddate") else None
    radius = [args.get(k) for k in ("latitude", "longitude", "maxradiuskm")]
    if any(v is not None for v in radius) and not all(v is not None for v in radius):
        raise MCPError(types.INVALID_PARAMS, "Radius search needs all of latitude, longitude and maxradiuskm.")
    text = (args.get("place_contains") or "").strip().lower()
    out = []
    for e in await fetch_tmd_events():
        d = thai_date(e)
        mag = e.get("magnitude")
        if start and (d is None or d < start):
            continue
        if end and (d is None or d > end):
            continue
        if mag is None or mag < args["minmagnitude"]:
            continue
        if args.get("maxmagnitude") is not None and mag > args["maxmagnitude"]:
            continue
        if text and text not in (e.get("place") or "").lower():
            continue
        if radius[0] is not None:
            if e.get("latitude") is None or e.get("longitude") is None:
                continue
            dist = haversine_km(radius[0], radius[1], e["latitude"], e["longitude"])
            if dist > radius[2]:
                continue
            e = {**e, "distance_km": round(dist, 2)}
        out.append(e)
    total = len(out)
    query = {k: args.get(k) for k in TOOL_SCHEMAS["tmd_search_earthquakes"][1]["properties"] if args.get(k) is not None}
    return {"events": out[: args["limit"]], "total_matched": total, "query": query}


def _event_coords(event: dict, label: str) -> tuple[float, float, float | None]:
    lat, lon = to_float(event.get("latitude")), to_float(event.get("longitude"))
    if lat is None or lon is None:
        raise MCPError(types.INVALID_PARAMS, f"{label} is missing numeric latitude/longitude.")
    return lat, lon, to_float(event.get("magnitude"))


async def match_events_to_assets(args: dict) -> dict:
    min_mag, max_dist = args["min_magnitude"], args["max_distance_km"]
    by_asset: dict[str, dict] = {}
    for idx, event in enumerate(args["events"]):
        lat, lon, mag = _event_coords(event, f"events[{idx}]")
        if mag is None or mag < min_mag:
            continue
        for asset in args["assets"]:
            dist = haversine_km(lat, lon, asset["latitude"], asset["longitude"])
            if dist <= max_dist:
                entry = by_asset.setdefault(
                    asset["asset_id"],
                    {
                        "asset_id": asset["asset_id"],
                        "name": asset.get("name"),
                        "sum_insured": asset.get("sum_insured"),
                        "events": [],
                    },
                )
                entry["events"].append({"event": event, "distance_km": round(dist, 2)})
    for entry in by_asset.values():
        entry["events"].sort(key=lambda m: m["distance_km"])
    return {
        "matches": list(by_asset.values()),
        "parameters": {"max_distance_km": max_dist, "min_magnitude": min_mag},
    }


async def compute_parametric_payout(args: dict) -> dict:
    event = args["event"]
    lat, lon, mag = _event_coords(event, "event")
    if mag is None:
        raise MCPError(types.INVALID_PARAMS, "event is missing numeric magnitude.")
    min_mag, max_dist = args["min_magnitude_trigger"], args["max_distance_km_trigger"]
    curve = sorted(args.get("payout_curve") or DEFAULT_PAYOUT_CURVE, key=lambda b: b["max_distance_km"])
    for band in curve:
        if not 0.0 <= band["payout_percent"] <= 1.0:
            raise MCPError(types.INVALID_PARAMS, "payout_percent must be between 0.0 and 1.0.")

    triggered = mag >= min_mag
    payouts, total = [], 0.0
    for asset in args["assets"]:
        dist = haversine_km(lat, lon, asset["latitude"], asset["longitude"])
        pct = 0.0
        if triggered and dist <= max_dist:
            pct = next((b["payout_percent"] for b in curve if dist <= b["max_distance_km"]), 0.0)
        amount = round(asset["sum_insured"] * pct, 2)
        total += amount
        payouts.append(
            {
                "asset_id": asset["asset_id"],
                "name": asset.get("name"),
                "distance_km": round(dist, 2),
                "sum_insured": asset["sum_insured"],
                "payout_percent": pct,
                "payout_amount": amount,
            }
        )
    return {
        "event_id": event.get("id"),
        "event_magnitude": mag,
        "triggered": triggered,
        "parameters": {
            "min_magnitude_trigger": min_mag,
            "max_distance_km_trigger": max_dist,
            "payout_curve": curve,
        },
        "asset_payouts": payouts,
        "total_payout": round(total, 2),
    }


TOOL_IMPLS: dict[str, Callable[[dict], Awaitable[dict]]] = {
    "usgs_search_earthquakes": usgs_search_earthquakes,
    "tmd_get_daily_earthquakes": tmd_get_daily_earthquakes,
    "tmd_get_recent_earthquakes": tmd_get_recent_earthquakes,
    "tmd_search_earthquakes": tmd_search_earthquakes,
    "match_events_to_assets": match_events_to_assets,
    "compute_parametric_payout": compute_parametric_payout,
}


# ---------------------------------------------------------------------------
# MCP handlers
# ---------------------------------------------------------------------------

async def handle_list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
    return types.ListToolsResult(
        tools=[
            types.Tool(name=name, description=desc, input_schema=schema)
            for name, (desc, schema) in TOOL_SCHEMAS.items()
        ]
    )


async def handle_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
    name = params.name
    if name not in TOOL_IMPLS:
        # Protocol error (JSON-RPC): unknown tool.
        raise MCPError(types.INVALID_PARAMS, f"Unknown tool: {name}")
    args = validate_and_fill(name, params.arguments)  # raises JSON-RPC -32602 on bad input
    log.info("tools/call %s %s", name, json.dumps(args, ensure_ascii=False)[:500])
    try:
        result = await TOOL_IMPLS[name](args)
    except UpstreamError as exc:
        # Execution error: returned inside the result so the model can see and react to it.
        log.warning("tool %s upstream error: %s", name, exc)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Upstream error: {exc}")],
            is_error=True,
        )
    return types.CallToolResult(
        content=[types.TextContent(type="text", text=json.dumps(result, ensure_ascii=False))],
        structured_content=result,
    )


def tool_input_schema(name: str) -> dict | None:
    entry = TOOL_SCHEMAS.get(name)
    return entry[1] if entry else None


server = Server(
    SERVER_NAME,
    version=SERVER_VERSION,
    instructions=(
        "Seismic data (USGS, TMD) and parametric insurance analytics. "
        "For earthquakes in or near Thailand, use the Thai Meteorological Department (TMD) tools "
        "first: tmd_get_daily_earthquakes, tmd_get_recent_earthquakes, tmd_search_earthquakes "
        "(TMD times are Thai local time, UTC+7). Use usgs_search_earthquakes for global events "
        "or dates before this year. Cite the source field (TMD or USGS) when answering. "
        "Pass returned EarthquakeEvent objects to match_events_to_assets or compute_parametric_payout."
    ),
    get_tool_input_schema=tool_input_schema,
    on_list_tools=handle_list_tools,
    on_call_tool=handle_call_tool,
)


# ---------------------------------------------------------------------------
# Entrypoints
# ---------------------------------------------------------------------------

def _csv_env(name: str) -> list[str]:
    return [x.strip() for x in os.environ.get(name, "").split(",") if x.strip()]


def _bool_env(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


async def health(_: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "server": SERVER_NAME, "version": SERVER_VERSION})


def build_http_app():
    from mcp.server.transport_security import TransportSecuritySettings

    host = os.environ.get("HOST", "0.0.0.0")
    allowed_hosts = _csv_env("ALLOWED_HOSTS")
    security = (
        TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=allowed_hosts,
            allowed_origins=_csv_env("ALLOWED_ORIGINS"),
        )
        if allowed_hosts
        else None
    )
    return server.streamable_http_app(
        streamable_http_path=os.environ.get("MCP_PATH", "/mcp"),
        stateless_http=_bool_env("MCP_STATELESS"),
        json_response=_bool_env("MCP_JSON_RESPONSE"),
        transport_security=security,
        host=host,
        custom_starlette_routes=[Route("/health", health, methods=["GET"])],
    )


async def run_stdio() -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "streamable-http").lower()
    if transport == "stdio":
        log.info("Starting %s %s on stdio", SERVER_NAME, SERVER_VERSION)
        asyncio.run(run_stdio())
        return
    if transport not in {"streamable-http", "http"}:
        sys.exit(f"Unknown MCP_TRANSPORT: {transport}")
    import uvicorn

    host, port = os.environ.get("HOST", "0.0.0.0"), int(os.environ.get("PORT", "8000"))
    log.info(
        "Starting %s %s on http://%s:%d%s", SERVER_NAME, SERVER_VERSION, host, port, os.environ.get("MCP_PATH", "/mcp")
    )
    uvicorn.run(build_http_app(), host=host, port=port, log_level=os.environ.get("LOG_LEVEL", "info").lower())


if __name__ == "__main__":
    main()
