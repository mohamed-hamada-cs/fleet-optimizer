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
from app.solver import SolverError, solve_trip, trip_cost

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


class OptimizeRequest(BaseModel):
    stops: list[Stop] = Field(min_length=3)
    direction: Literal["am", "pm"] = "am"
    matrix_source: MatrixSource = "here"


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

    # --- 1. Travel-time matrix ---
    try:
        if body.matrix_source == "here":
            matrix = await fetch_matrix(here_key, coords)
        elif body.matrix_source == "osrm":
            matrix = await fetch_matrix_osrm(coords)
        else:
            matrix = haversine_matrix(coords)
    except (HereError, MatrixSourceError) as e:
        raise HTTPException(status_code=502, detail=str(e))

    # --- 2. Solve. AM: depot -> PAs -> students -> school; PM mirrored. ---
    if body.direction == "am":
        start, end, first_group, second_group = depots[0], schools[0], pas, students
    else:
        start, end, first_group, second_group = schools[0], depots[0], students, pas

    try:
        order_idx = solve_trip(matrix, start, end, first_group, second_group)
    except SolverError as e:
        raise HTTPException(status_code=500, detail=f"solver failed: {e}")

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
    )
