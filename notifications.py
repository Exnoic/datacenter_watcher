from datetime import UTC, datetime

from sqlmodel import Session, col, select

from db import engine
from models import Notification


def send_notification(key: str, message: str):
    with Session(engine) as session:
        active = session.exec(
            select(Notification)
            .where(col(Notification.key) == key)
            .where(col(Notification.resolved_at).is_(None))
        ).first()

        if active is not None:
            return False

        session.add(Notification(key=key, message=message))
        session.commit()

    return True

def resolve_notification(key: str, message: str | None = None):
    with Session(engine) as session:
        active = session.exec(
            select(Notification)
            .where(col(Notification.key) == key)
            .where(col(Notification.resolved_at).is_(None))
        ).first()

        if active is None:
            return False

        active.resolved_at = datetime.now(UTC)
        session.add(active)
        session.commit()

    return True
