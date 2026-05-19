"""Domain entity layer.

All SQLAlchemy entities live here as siblings of the declarative Base. This
package is the single place Alembic introspects to discover schema changes —
adding a new model means adding a file here and importing it below so its
metadata is registered.

Layered architecture rule: entities know nothing about FastAPI, Celery, or
HTTP. They are pure domain objects mapped to database tables.
"""

from app.models.audit_log import AuditLog
from app.models.base_class import Base
from app.models.notification import Notification
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole

__all__ = ["AuditLog", "Base", "Notification", "Order", "OrderStatus", "User", "UserRole"]
