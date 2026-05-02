"""Domain entity layer.

All SQLAlchemy entities live here as siblings of the declarative Base. This
package is the single place Alembic introspects to discover schema changes —
adding a new model means adding a file here and importing it below so its
metadata is registered.

Layered architecture rule: entities know nothing about FastAPI, Celery, or
HTTP. They are pure domain objects mapped to database tables.
"""

from app.models.base_class import Base

# When new entities are added (e.g., `from app.models.order import Order`),
# import them here so Alembic's autogenerate detects them. This file is the
# single registration site referenced by `alembic/env.py`.
from app.models.user import User, UserRole

__all__ = ["Base", "User", "UserRole"]
