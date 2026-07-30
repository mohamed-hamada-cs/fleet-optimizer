# fleet-optimizer

Stop-order optimization service for the County Cars fleet-manager. Solves each
school run's stop order (AM: depot → PAs → students → school; PM mirrored) as a
single open TSP with group precedence, using OR-Tools on a HERE travel-time
matrix. Called only by the fleet-manager Supabase Edge Function
`optimize-route` — never by browsers.

Feature context, architecture and status:
`fleet-manager/docs/planning/ROUTE_OPTIMIZATION_IMPLEMENTATION_PLAN.md`.

## API

| Endpoint | Auth | Purpose |
|---|---|---|
| `GET /health` | none | liveness (Coolify health check) |
| `POST /optimize` | `Authorization: Bearer $SOLVER_SECRET` | body `{stops: [{id, kind: depot\|pa\|student\|school, lat, lng}], direction: am\|pm}` → `{order, legs (duration/length/polyline per leg), total_duration_s, total_distance_m}` |

Polylines are HERE flexible-polyline strings, one per leg — decode client-side.

## Local dev

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt pytest
pytest                     # solver tests: fixed matrices, no network needed
cp .env.example .env       # fill secrets
SOLVER_SECRET=dev HERE_API_KEY=... uvicorn app.main:app --port 8080
```

## First run on kvm1 (terminal, before Coolify)

```bash
git clone <this repo> && cd fleet-optimizer
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
SOLVER_SECRET=test HERE_API_KEY=... .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8080
# separate shell:
curl http://127.0.0.1:8080/health
```

## Production hosting — Coolify on kvm1

The service needs INBOUND HTTPS from Supabase Edge Functions; kvm1 exposes only
22/80/443 with Traefik (Coolify) owning 80/443 — so this runs as a Coolify app,
not PM2 (PM2 suits the outbound-only fleet-poller).

1. DNS: `optimizer.<domain>` A-record → kvm1 IP.
2. Coolify → new resource → this GitHub repo → build via Dockerfile (port 8080).
3. Set env `SOLVER_SECRET` (generate long random) + `HERE_API_KEY`.
4. Attach the domain — Traefik issues Let's Encrypt automatically.
5. Health check path `/health`. Verify from outside:
   `curl https://optimizer.<domain>/health`.
6. Put the same values in fleet-manager Supabase secrets: `SOLVER_URL`,
   `SOLVER_SECRET`.

## Cost model (why this exists)

Matrix + polyline are fetched ONCE per route optimization (routes are stable);
live ETA during trips comes from Samsara webhooks, free. HERE free tiers:
Geocoding 30k/mo · Routing 5k/mo · Matrix 2.5k/mo. RAM footprint ~200 MB.
