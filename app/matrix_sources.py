"""Travel-time matrix + path geometry sources.

Chain (decided 30 Jul 2026): 'here' is the PRODUCTION source (traffic-aware);
'osrm' uses the free public demo server — development/PoC and emergency
fallback only (demo usage policy, static times); 'haversine' is a last-resort
offline test mode (straight-line at an assumed urban speed, no geometry).

Never mix sources within one solve; orders computed on osrm/haversine should be
re-solved with HERE before production activation.
"""

from __future__ import annotations

import math

import httpx

OSRM_BASE_URL = "https://router.project-osrm.org"
REQUEST_TIMEOUT_SECONDS = 60.0
HAVERSINE_SPEED_KMH = 30.0  # assumed urban driving speed for the test mode

USER_AGENT = "fleet-optimizer/0.1 (school-transport stop ordering; low volume)"


class MatrixSourceError(Exception):
    pass


def haversine_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lng1, lat2, lng2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lng2 - lng1) / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def haversine_matrix(coords: list[tuple[float, float]]) -> dict:
    """Straight-line seconds/metres at HAVERSINE_SPEED_KMH. Offline test mode only."""
    seconds_per_km = 3600.0 / HAVERSINE_SPEED_KMH
    km = [[haversine_km(a, b) for b in coords] for a in coords]
    return {
        "durations": [[int(round(d * seconds_per_km)) for d in row] for row in km],
        "distances": [[int(round(d * 1000)) for d in row] for row in km],
    }


def _osrm_coord_path(coords: list[tuple[float, float]]) -> str:
    # OSRM wants lng,lat order
    return ";".join(f"{lng},{lat}" for lat, lng in coords)


async def fetch_matrix_osrm(coords: list[tuple[float, float]]) -> dict:
    """NxN duration (s) + distance (m) matrices from the public OSRM demo /table service."""
    url = f"{OSRM_BASE_URL}/table/v1/driving/{_osrm_coord_path(coords)}"
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        res = await client.get(url, params={"annotations": "duration,distance"})
    if res.status_code != 200:
        raise MatrixSourceError(f"osrm table failed ({res.status_code}): {res.text[:300]}")
    payload = res.json()
    if payload.get("code") != "Ok":
        raise MatrixSourceError(f"osrm table error: {str(payload)[:300]}")

    n = len(coords)

    def grid(key: str, required: bool) -> list[list[int]]:
        raw = payload.get(key)
        if not raw:
            if required:
                raise MatrixSourceError(f"osrm table response missing {key}")
            return [[0] * n for _ in range(n)]
        if len(raw) != n:
            raise MatrixSourceError("osrm table response malformed")
        out = []
        for row in raw:
            if len(row) != n or any(cell is None for cell in row):
                raise MatrixSourceError("osrm table has unroutable cells")
            out.append([int(round(cell)) for cell in row])
        return out

    return {"durations": grid("durations", True), "distances": grid("distances", False)}


async def fetch_route_osrm(ordered_coords: list[tuple[float, float]]) -> dict:
    """Drive route through the ordered stops via the public OSRM demo /route service.

    Returns {"legs": [{"duration_s", "length_m"}], "route_polyline": str,
    "polyline_format": "polyline5"} — one full-route polyline (per-leg geometry
    would need steps=true stitching; not worth it for a dev-mode source).
    """
    url = f"{OSRM_BASE_URL}/route/v1/driving/{_osrm_coord_path(ordered_coords)}"
    async with httpx.AsyncClient(
        timeout=REQUEST_TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT}
    ) as client:
        res = await client.get(
            url,
            params={"overview": "full", "geometries": "polyline", "steps": "false"},
        )
    if res.status_code != 200:
        raise MatrixSourceError(f"osrm route failed ({res.status_code}): {res.text[:300]}")
    payload = res.json()
    if payload.get("code") != "Ok" or not payload.get("routes"):
        raise MatrixSourceError(f"osrm route error: {str(payload)[:300]}")
    route = payload["routes"][0]
    legs = [
        {"duration_s": int(round(leg.get("duration", 0))), "length_m": int(round(leg.get("distance", 0)))}
        for leg in route.get("legs", [])
    ]
    expected = len(ordered_coords) - 1
    if len(legs) != expected:
        raise MatrixSourceError(f"expected {expected} osrm legs, got {len(legs)}")
    return {
        "legs": legs,
        "route_polyline": route.get("geometry", ""),
        "polyline_format": "polyline5",
    }
