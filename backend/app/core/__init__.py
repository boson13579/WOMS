"""Infrastructure layer — config, database engine/session, logging, security primitives.

Per the project's strict layered architecture, `core/` owns infrastructure concerns
and MUST NOT import from `app.models`, `app.services`, or `app.api`.
"""
