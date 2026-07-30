"""HERE API clients: travel-time matrix (Matrix Routing v8) and path geometry (Routing v8).

Both are called once per optimization run — never during live trips (live ETA
comes from Samsara webhooks; see fleet-manager docs/planning/ROUTE_OPTIMIZATION_IMPLEMENTATION_PLAN.md).
"""

from __future__ import annotations

import httpx

MATRIX_URL = "https://matrix.router.hereapi.com/v8/matrix"
ROUTES_URL = "https://router.hereapi.com/v8/routes"

REQUEST_TIMEOUT_SECONDS = 60.0


class HereError(Exception):
    pass


async def fetch_matrix(api_key: str, coords: list[tuple[float, float]]) -> dict:
    """NxN travel-time (s) + distance (m) matrices between coords [(lat, lng), ...].

    Uses synchronous mode (async=false) — fine for our sizes (<= ~25 stops).
    regionDefinition autoCircle covers a compact UK service area.
    """
    origins = [{"lat": lat, "lng": lng} for lat, lng in coords]
    body = {
        "origins": origins,
        "regionDefinition": {"type": "autoCircle", "margin": 10000},
        "matrixAttributes": ["travelTimes", "distances"],
    }
    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        res = await client.post(f"{MATRIX_URL}?async=false&apiKey={api_key}", json=body)
    if res.status_code != 200:
        raise HereError(f"matrix request failed ({res.status_code}): {res.text[:300]}")

    payload = res.json()
    matrix = payload.get("matrix", {})
    travel_times = matrix.get("travelTimes")
    distances = matrix.get("distances")
    error_codes = matrix.get("errorCodes")
    n = len(coords)
    if not travel_times or len(travel_times) != n * n:
        raise HereError("matrix response malformed or incomplete")
    if error_codes and any(code != 0 for code in error_codes):
        bad = [i for i, code in enumerate(error_codes) if code != 0]
        raise HereError(f"matrix has unroutable cells at flat indices {bad[:10]}")

    def unflatten(flat: "list[int] | None") -> list[list[int]]:
        if not flat or len(flat) != n * n:
            return [[0] * n for _ in range(n)]
        return [list(flat[row * n : (row + 1) * n]) for row in range(n)]

    return {"durations": unflatten(travel_times), "distances": unflatten(distances)}


async def fetch_route(
    api_key: str, ordered_coords: list[tuple[float, float]]
) -> list[dict]:
    """Drive route through the ordered stops.

    Returns one entry per leg: {"polyline": <flexible-polyline str>,
    "duration_s": int, "length_m": int}. The polyline is HERE's flexible
    polyline encoding — decoded client-side when drawing.
    """
    if len(ordered_coords) < 2:
        raise HereError("need at least origin and destination")

    params: list[tuple[str, str]] = [
        ("transportMode", "car"),
        ("origin", f"{ordered_coords[0][0]},{ordered_coords[0][1]}"),
        ("destination", f"{ordered_coords[-1][0]},{ordered_coords[-1][1]}"),
        ("return", "polyline,travelSummary"),
        ("apiKey", api_key),
    ]
    for lat, lng in ordered_coords[1:-1]:
        params.append(("via", f"{lat},{lng}"))

    async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
        res = await client.get(ROUTES_URL, params=params)
    if res.status_code != 200:
        raise HereError(f"route request failed ({res.status_code}): {res.text[:300]}")

    payload = res.json()
    routes = payload.get("routes", [])
    if not routes:
        raise HereError(f"no route found: {str(payload)[:300]}")

    legs = []
    for section in routes[0].get("sections", []):
        summary = section.get("travelSummary", {})
        legs.append(
            {
                "polyline": section.get("polyline", ""),
                "duration_s": int(summary.get("duration", 0)),
                "length_m": int(summary.get("length", 0)),
            }
        )
    expected_legs = len(ordered_coords) - 1
    if len(legs) != expected_legs:
        raise HereError(f"expected {expected_legs} route sections, got {len(legs)}")
    return legs
