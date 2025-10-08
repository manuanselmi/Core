from datetime import datetime, timezone
from app.models import db
from sqlalchemy import text

def get_thread(wa_id: str) -> str | None:
    row = db.session.execute(
        text("SELECT thread_id FROM assistant_threads WHERE wa_id = :wa_id"),
        {"wa_id": wa_id}
    ).first()
    return row[0] if row else None

def set_thread(wa_id: str, thread_id: str) -> None:
    db.session.execute(text("""
        INSERT INTO assistant_threads (wa_id, thread_id, updated_at)
        VALUES (:wa_id, :thread_id, NOW())
        ON CONFLICT (wa_id)
        DO UPDATE SET thread_id = EXCLUDED.thread_id, updated_at = NOW()
    """), {"wa_id": wa_id, "thread_id": thread_id})
    db.session.commit()
