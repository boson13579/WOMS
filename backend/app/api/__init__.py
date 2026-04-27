"""HTTP API layer.

Versioned routers live under `app.api.v1.*` and are aggregated in `app.main`.
This layer only handles HTTP concerns (parsing, routing, response shaping) and
delegates business logic to `app.services`.
"""
