"""fleet-optimizer — stop-order optimization service for County Cars fleet-manager.

POST /optimize   Bearer SOLVER_SECRET; body: stops + direction + matrix_source ->
                 optimal order, per-leg times, path geometry. Called by the
                 fleet-manager Supabase Edge Function `optimize-route`.
GET  /health     liveness for Coolify.

Matrix sources: 'here' (production, traffic-aware; needs HERE_API_KEY),
'osrm' (free public demo server — dev/PoC + emergency fallback),
'haversine' (offline last-resort test mode, no geometry).

Env: SOLVER_SECRET (required), HERE_API_KEY (required for matrix_source=here).
"""

from __future__ import annotations

import os
from typing import Literal, Optional

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.here_client import HereError, fetch_matrix, fetch_route
from app.matrix_sources import (
    MatrixSourceError,
    fetch_matrix_osrm,
    fetch_route_osrm,
    haversine_matrix,
)
from app.solver import SolverError, check_order_structure, solve_trip, trip_cost

app = FastAPI(title="fleet-optimizer", docs_url=None, redoc_url=None)

SERVICE_VERSION = "0.2.0"


@app.middleware("http")
async def auth_before_anything(request, call_next):
    """Reject bad bearers before body parsing — auth must precede validation."""
    if request.url.path != "/health":
        secret = os.environ.get("SOLVER_SECRET")
        if not secret:
            return JSONResponse({"detail": "SOLVER_SECRET not configured"}, status_code=503)
        token = (request.headers.get("authorization") or "").removeprefix("Bearer ").strip()
        if token != secret:
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)

MatrixSource = Literal["here", "osrm", "haversine"]


class Stop(BaseModel):
    id: str
    kind: Literal["depot", "pa", "student", "school"]
    lat: float
    lng: float
    # Human name used in violation messages; falls back to the id when absent.
    label: Optional[str] = None


class OptimizeRequest(BaseModel):
    stops: list[Stop] = Field(min_length=3)
    direction: Literal["am", "pm"] = "am"
    matrix_source: MatrixSource = "here"
    # What to minimise. MVP uses distance: it is time-independent, so the PM order can be
    # the AM order reversed, results are reproducible run to run, and every km saved is
    # margin (the council pays a fixed price per trip). Switch to "time" for traffic-aware
    # punctuality — that will also require solving PM separately.
    objective: Literal["time", "distance"] = "distance"
    # Optional: a manually-written stop order (full list of stop ids) to validate
    # and score against the optimized one — "is the manager's order better?"
    compare_order: Optional[list[str]] = None


class OrderComparison(BaseModel):
    """Manual order vs optimized, both measured on the SAME matrix (fair comparison)."""

    valid: bool
    violations: list[str]
    manual_duration_s: Optional[int] = None
    manual_distance_m: Optional[int] = None
    optimized_duration_s: int
    optimized_distance_m: int
    saving_duration_s: Optional[int] = None
    saving_distance_m: Optional[int] = None
    saving_pct: Optional[float] = None
    verdict: Optional[str] = None


class Leg(BaseModel):
    from_id: str
    to_id: str
    duration_s: int
    length_m: int
    polyline: str = ""  # per-leg geometry (HERE mode only)


class OptimizeResponse(BaseModel):
    order: list[str]
    legs: list[Leg]
    total_duration_s: int
    total_distance_m: int
    matrix_source: MatrixSource
    route_polyline: Optional[str] = None  # full-route geometry (OSRM mode)
    polyline_format: Optional[Literal["here-flexible", "polyline5"]] = None
    comparison: Optional[OrderComparison] = None
    solver_version: str = SERVICE_VERSION


def _require_auth(authorization: "Optional[str]") -> None:
    secret = os.environ.get("SOLVER_SECRET")
    if not secret:
        raise HTTPException(status_code=503, detail="SOLVER_SECRET not configured")
    token = (authorization or "").removeprefix("Bearer ").strip()
    if token != secret:
        raise HTTPException(status_code=401, detail="unauthorized")


@app.get("/health")
def health() -> dict:
    return {"ok": True, "version": SERVICE_VERSION}


class PathRequest(BaseModel):
    """Road geometry for stops in a GIVEN order — no reordering."""

    points: list = Field(min_length=2)  # [[lat, lng], ...] in visiting order
    matrix_source: MatrixSource = "osrm"


class PathResponse(BaseModel):
    polyline: str
    polyline_format: Literal["here-flexible", "polyline5"]
    duration_s: int
    distance_m: int


@app.post("/path", response_model=PathResponse)
async def path(
    body: PathRequest, authorization: Optional[str] = Header(default=None)
) -> PathResponse:
    """Draw the road path a vehicle actually drives for an existing stop order.

    Needed because the dashboard must show a real route line for every route, not
    only ones somebody has optimized and activated.
    """
    _require_auth(authorization)
    coords = [(float(p[0]), float(p[1])) for p in body.points]

    here_key = os.environ.get("HERE_API_KEY")
    if body.matrix_source == "here" and not here_key:
        raise HTTPException(status_code=503, detail="HERE_API_KEY not configured")

    try:
        if body.matrix_source == "here":
            legs = await fetch_route(here_key, coords)
            return PathResponse(
                polyline="".join(leg["polyline"] for leg in legs),
                polyline_format="here-flexible",
                duration_s=sum(leg["duration_s"] for leg in legs),
                distance_m=sum(leg["length_m"] for leg in legs),
            )
        osrm = await fetch_route_osrm(coords)
        return PathResponse(
            polyline=osrm["route_polyline"],
            polyline_format="polyline5",
            duration_s=sum(leg["duration_s"] for leg in osrm["legs"]),
            distance_m=sum(leg["length_m"] for leg in osrm["legs"]),
        )
    except (HereError, MatrixSourceError) as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/optimize", response_model=OptimizeResponse)
async def optimize(
    body: OptimizeRequest, authorization: Optional[str] = Header(default=None)
) -> OptimizeResponse:
    _require_auth(authorization)

    depots = [i for i, s in enumerate(body.stops) if s.kind == "depot"]
    schools = [i for i, s in enumerate(body.stops) if s.kind == "school"]
    pas = [i for i, s in enumerate(body.stops) if s.kind == "pa"]
    students = [i for i, s in enumerate(body.stops) if s.kind == "student"]
    if len(depots) != 1 or len(schools) != 1:
        raise HTTPException(status_code=422, detail="exactly one depot and one school required")
    if not students:
        raise HTTPException(status_code=422, detail="at least one student stop required")

    here_key = os.environ.get("HERE_API_KEY")
    if body.matrix_source == "here" and not here_key:
        raise HTTPException(
            status_code=503,
            detail="HERE_API_KEY not configured — use matrix_source 'osrm' for dev/testing",
        )

    coords = [(s.lat, s.lng) for s in body.stops]

    # --- 1. Travel-time + distance matrices ---
    try:
        if body.matrix_source == "here":
            grids = await fetch_matrix(here_key, coords)
        elif body.matrix_source == "osrm":
            grids = await fetch_matrix_osrm(coords)
        else:
            grids = haversine_matrix(coords)
    except (HereError, MatrixSourceError) as e:
        raise HTTPException(status_code=502, detail=str(e))
    matrix = grids["durations"]
    dist_matrix = grids["distances"]

    # The solver minimises whichever matrix it is handed; the comparison still reports
    # both time and distance. Fall back to time if the source returned no distances
    # rather than optimising on an all-zero matrix.
    cost_matrix = matrix
    if body.objective == "distance" and any(any(row) for row in dist_matrix):
        cost_matrix = dist_matrix

    # --- 2. Solve. AM: depot -> PAs -> students -> school; PM mirrored. ---
    if body.direction == "am":
        start, end, first_group, second_group = depots[0], schools[0], pas, students
    else:
        start, end, first_group, second_group = schools[0], depots[0], students, pas

    try:
        order_idx = solve_trip(cost_matrix, start, end, first_group, second_group)
    except SolverError as e:
        raise HTTPException(status_code=500, detail=f"solver failed: {e}")

    # --- 2b. Optional: score the manager's manual order on the same matrix ---
    comparison: Optional[OrderComparison] = None
    if body.compare_order is not None:
        index_of = {s.id: i for i, s in enumerate(body.stops)}
        label_of = {i: (s.label or s.id) for i, s in enumerate(body.stops)}
        unknown = [sid for sid in body.compare_order if sid not in index_of]
        manual_idx = [index_of[sid] for sid in body.compare_order if sid in index_of]
        violations = check_order_structure(
            manual_idx, start, end, first_group, second_group, label_of
        )
        if unknown:
            violations.insert(0, f"unknown stop ids: {', '.join(unknown)}")

        opt_duration = trip_cost(matrix, order_idx)
        opt_distance = trip_cost(dist_matrix, order_idx)
        if violations:
            comparison = OrderComparison(
                valid=False,
                violations=violations,
                optimized_duration_s=opt_duration,
                optimized_distance_m=opt_distance,
                verdict="manual order breaks the trip rules — cannot be compared",
            )
        else:
            man_duration = trip_cost(matrix, manual_idx)
            man_distance = trip_cost(dist_matrix, manual_idx)
            saved = man_duration - opt_duration
            pct = round(saved / man_duration * 100, 1) if man_duration else 0.0
            if saved > 0:
                verdict = f"optimized is {saved // 60} min faster ({pct}% better)"
            elif saved == 0:
                verdict = "manual order is already optimal"
            else:
                verdict = "manual order is faster — check the matrix source"
            comparison = OrderComparison(
                valid=True,
                violations=[],
                manual_duration_s=man_duration,
                manual_distance_m=man_distance,
                optimized_duration_s=opt_duration,
                optimized_distance_m=opt_distance,
                saving_duration_s=saved,
                saving_distance_m=man_distance - opt_distance,
                saving_pct=pct,
                verdict=verdict,
            )

    ordered_coords = [coords[i] for i in order_idx]
    pairs = list(zip(order_idx, order_idx[1:]))

    # --- 3. Path geometry + per-leg travel summary (source-dependent) ---
    route_polyline: Optional[str] = None
    polyline_format: Optional[str] = None
    try:
        if body.matrix_source == "here":
            here_legs = await fetch_route(here_key, ordered_coords)
            polyline_format = "here-flexible"
            legs = [
                Leg(
                    from_id=body.stops[a].id,
                    to_id=body.stops[b].id,
                    duration_s=here_legs[i]["duration_s"],
                    length_m=here_legs[i]["length_m"],
                    polyline=here_legs[i]["polyline"],
                )
                for i, (a, b) in enumerate(pairs)
            ]
        elif body.matrix_source == "osrm":
            osrm_route = await fetch_route_osrm(ordered_coords)
            route_polyline = osrm_route["route_polyline"]
            polyline_format = "polyline5"
            legs = [
                Leg(
                    from_id=body.stops[a].id,
                    to_id=body.stops[b].id,
                    duration_s=osrm_route["legs"][i]["duration_s"],
                    length_m=osrm_route["legs"][i]["length_m"],
                )
                for i, (a, b) in enumerate(pairs)
            ]
        else:  # haversine: durations from the matrix, no geometry
            legs = [
                Leg(
                    from_id=body.stops[a].id,
                    to_id=body.stops[b].id,
                    duration_s=matrix[a][b],
                    length_m=0,
                )
                for a, b in pairs
            ]
    except (HereError, MatrixSourceError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    return OptimizeResponse(
        order=[body.stops[i].id for i in order_idx],
        legs=legs,
        total_duration_s=sum(leg.duration_s for leg in legs) or trip_cost(matrix, order_idx),
        total_distance_m=sum(leg.length_m for leg in legs),
        matrix_source=body.matrix_source,
        route_polyline=route_polyline,
        polyline_format=polyline_format,
        comparison=comparison,
    )
