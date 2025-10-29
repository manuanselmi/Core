from __future__ import annotations
from typing import Optional
from contextlib import nullcontext
from datetime import datetime, timezone
import logging
from sqlalchemy.exc import IntegrityError
from sqlalchemy import select, update

from app.models import db, AssistantThread
from app.utils.phone_utils import normalize_phone_e164
from app.services.openai_client import client as openai_client

def get_active_thread_id(wa_phone_raw: str) -> Optional[str]:
    wa_phone = normalize_phone_e164(wa_phone_raw)
    if not wa_phone:
        return None
    q = select(AssistantThread.thread_id).where(
        AssistantThread.wa_phone == wa_phone,
        AssistantThread.status == "active"
    )
    row = db.session.execute(q).first()
    return row[0] if row else None

def ensure_thread(
    wa_phone_raw: str,
    customer_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    last_wa_msg_id: Optional[str] = None,
) -> str:
    """
    Idempotente y seguro ante carreras.
    - Retorna el thread_id activo para `wa_phone`. Si no existe, lo crea en OpenAI y lo persiste.
    - Garantiza unicidad por índice único parcial (wa_phone WHERE status='active').
    """
    wa_phone = normalize_phone_e164(wa_phone_raw)
    if not wa_phone:
        raise ValueError("wa_phone inválido")

    # 1) Read-fast path
    tid = get_active_thread_id(wa_phone)
    if tid:
        _touch(wa_phone, last_wa_msg_id)
        return tid

    # 2) Create path con control de carreras
    # Creamos el thread primero para minimizar la ventana entre insert/select
    new_thread = openai_client.beta.threads.create()
    new_tid = new_thread.id

    try:
        at = AssistantThread(
            wa_phone=wa_phone,
            customer_id=customer_id,
            thread_id=new_tid,
            status="active",
            last_wa_msg_id=last_wa_msg_id,
            metadata=None,
        )
        db.session.add(at)
        db.session.commit()
        logging.info(
            "[threads.ensure_thread] created",
            extra={"correlation_id": correlation_id, "wa_phone": wa_phone, "thread_id": new_tid}
        )
        return new_tid
    except IntegrityError:
        db.session.rollback()
        # Otro request ganó la carrera → leer el thread activo existente
        tid = get_active_thread_id(wa_phone)
        if tid:
            _touch(wa_phone, last_wa_msg_id)
            return tid
        # Edge raro: no hay registro activo → volver a intentar una vez
        at = AssistantThread(
            wa_phone=wa_phone,
            customer_id=customer_id,
            thread_id=new_tid,
            status="active",
            last_wa_msg_id=last_wa_msg_id,
        )
        db.session.add(at)
        db.session.commit()
        return new_tid

def _touch(wa_phone: str, last_wa_msg_id: Optional[str]) -> None:
    values = {"last_used_at": datetime.now(timezone.utc)}
    if last_wa_msg_id:
        values["last_wa_msg_id"] = last_wa_msg_id
    db.session.execute(
        update(AssistantThread)
        .where(AssistantThread.wa_phone == wa_phone, AssistantThread.status == "active")
        .values(**values)
    )
    db.session.commit()

# Compatibilidad con el código existente:
def get_thread(wa_id: str) -> Optional[str]:
    return get_active_thread_id(wa_id)

def set_thread(wa_id: str, thread_id: str) -> None:
    # Mantener API legacy: upsert simple al activo
    # Si ya existe activo → actualizamos thread_id
    db.session.execute(
        update(AssistantThread)
        .where(AssistantThread.wa_phone == normalize_phone_e164(wa_id), AssistantThread.status == "active")
        .values(thread_id=thread_id, last_used_at=datetime.now(timezone.utc))
    )
    db.session.commit()