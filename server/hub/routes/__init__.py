"""Hub HTTP route groups.

Each module under this package exposes an ``APIRouter`` named
``router`` which app.py registers via ``app.include_router(...)``.
Routes live with their helpers + dependencies; app.py shrinks to a
thin orchestrator that wires lifespan, middleware, the WebSocket
endpoints (which can't be APIRouter'd cleanly today), and the few
straggler routes still pending migration.

Active modules:
  settings.py   /settings       (GET / PUT)
  engines.py    /engines/...    (8 routes)

Planned:
  jobs.py       /jobs/...
  sessions.py   /sessions/...
  workers.py    /workers/...
  assets.py     /jobs/{id}/assets... + /ui/assets/...
  codegen.py    /codegen + /skills/... + /conventions/...
"""
