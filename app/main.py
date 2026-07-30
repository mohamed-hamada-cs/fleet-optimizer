"""fleet-optimizer — stop-order optimization service for County Cars fleet-manager.

POST /optimize   Bearer SOLVER_SECRET; body: stops + direction -> optimal order,
                 per-leg times, path polylines. Called by the fleet-manager
                 Supabase Edge Function `optimize-route` (never by browsers).
GET  /health     liveness for Coolify.

Env: SOLVER_SECRET (required), HERE_API_KEY (required for real solves).
"""

from __future__ import annotations

import os
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from app.here_client import HereError, fetch_matrix, fetch_route
from app.solver import SolverError, solve_trip, trip_cost

app = FastAPI(title="fleet-optimizer", docs_url=None, redoc_url=None)

SERVICE_VERSION = "0.1.0"


class Stop(BaseModel):
    id: str
    kind: Literal["depot", "pa", "student", "school"]
    lat: float
    lng: float


class OptimizeRequest(BaseModel):
    stops: list[Stop] = Field(min_length=3)
    direction: Literal["am", "pm"] = "am"


class Leg(BaseModel):
    from_id: str
    to_id: str
    duration_s: int
    length_m: int
    polyline: str


class OptimizeResponse(BaseModel):
    order: list[str]
    legs: list[Leg]
    total_duration_s: int
    total_distance_m: int
    matrix_source: str = "here"
    solver_version: str = SERVICE_VERSION


def _require_auth(authorization: str | None) -> None:
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
    body: OptimizeRequest, authorization: str | None = Header(default=None)
) -> OptimizeResponse:
    _require_auth(authorization)

    here_key = os.environ.get("HERE_API_KEY")
    if not here_key:
        raise HTTPException(status_code=503, detail="HERE_API_KEY not configured")

    depots = [i for i, s in enumerate(body.stops) if s.kind == "depot"]
    schools = [i for i, s in enumerate(body.stops) if s.kind == "school"]
    pas = [i for i, s in enumerate(body.stops) if s.kind == "pa"]
    students = [i for i, s in enumerate(body.stops) if s.kind == "student"]
    if len(depots) != 1 or len(schools) != 1:
        raise HTTPException(status_code=422, detail="exactly one depot and one school required")
    if not students:
        raise HTTPException(status_code=422, detail="at least one student stop required")

    coords = [(s.lat, s.lng) for s in body.stops]
    try:
        matrix = await fetch_matrix(here_key, coords)
    except HereError as e:
        raise HTTPException(status_code=502, detail=str(e))

    # AM: depot -> PAs -> students -> school. PM mirror: school -> students -> PAs -> depot.
    if body.direction == "am":
        start, end, first_group, second_group = depots[0], schools[0], pas, students
    else:
        start, end, first_group, second_group = schools[0], depots[0], students, pas

    try:
        order_idx = solve_trip(matrix, start, end, first_group, second_group)
    except SolverError as e:
        raise HTTPException(status_code=500, detail=f"solver failed: {e}")

    try:
        route_legs = await fetch_route(here_key, [coords[i] for i in order_idx])
    except HereError as e:
        raise HTTPException(status_code=502, detail=str(e))

    legs = [
        Leg(
            from_id=body.stops[a].id,
            to_id=body.stops[b].id,
            duration_s=route_legs[i]["duration_s"],
            length_m=route_legs[i]["length_m"],
            polyline=route_legs[i]["polyline"],
        )
        for i, (a, b) in enumerate(zip(order_idx, order_idx[1:]))
    ]
    return OptimizeResponse(
        order=[body.stops[i].id for i in order_idx],
        legs=legs,
        total_duration_s=sum(leg.duration_s for leg in legs) or trip_cost(matrix, order_idx),
        total_distance_m=sum(leg.length_m for leg in legs),
    )
