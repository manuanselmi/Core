from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import current_app
from zoneinfo import ZoneInfo
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import db, ScheduledMessage, Reminder, Customer
from app.services.scheduled_message_service import ScheduledMessageService
from app.utils.whatsapp_utils import (
    get_event_reminder_template_input,
    send_message,
)

# ────────────────────────────────────────────────────────────────────
# Config & Globals
# ────────────────────────────────────────────────────────────────────

_app = None          # se setea en init_scheduler(app)
_initialized = False

# Parametrizable por env
BATCH_LIMIT = int(os.getenv("SCHED_BATCH_LIMIT", "100"))
LOCAL_TZ_NAME = os.getenv("TZ", "America/Montevideo")


class _NoopScheduler:
    """Stub compatible con la interfaz usada en el código legacy."""
    def add_job(self, *args, **kwargs):
        if _app:
            _app.logger.debug("[NoopScheduler] add_job(*%s, **%s) ignorado", args, kwargs)

    def get_job(self, job_id: str):
        return None

    def remove_job(self, job_id: str):
        if _app:
            _app.logger.debug("[NoopScheduler] remove_job(%s) ignorado", job_id)


# Atributo público para mantener compat
scheduler = _NoopScheduler()


# ────────────────────────────────────────────────────────────────────
# Boot
# ────────────────────────────────────────────────────────────────────

def init_scheduler(app):
    """
    Compat: guarda la referencia de app y fija TZ.
    En AWS NO se inicia ningún thread ni APScheduler.
    """
    global _app, _initialized
    _app = app
    _initialized = True
    tz = ZoneInfo(LOCAL_TZ_NAME)
    if _app:
        _app.logger.info("[SchedulerService] (EventBridge) iniciado. TZ=%s", tz)
    return scheduler


def _require_app():
    if not _initialized or _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")
    return _app


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(LOCAL_TZ_NAME))


# ────────────────────────────────────────────────────────────────────
# API de programación (compat)
# ────────────────────────────────────────────────────────────────────

def schedule_event_reminder(_ignored_scheduler, reminder_id: int, advance: timedelta = timedelta(minutes=1)) -> None:
    """
    Persiste lo necesario en BD; el envío real lo hará run_due_jobs().
    - (Opcional) genera un ScheduledMessage de 'pre-aviso' si corresponde.
    """
    app = _require_app()
    with app.app_context():
        rem: Reminder | None = (
            db.session.query(Reminder)
            .join(Customer)
            .filter(Reminder.id == reminder_id)
            .first()
        )
        if not rem:
            return

        local_tz = ZoneInfo(LOCAL_TZ_NAME)
        rdt = rem.date
        rdt_local = rdt.replace(tzinfo=local_tz) if rdt.tzinfo is None else rdt.astimezone(local_tz)

        if advance and advance.total_seconds() > 0:
            pre_dt_local = rdt_local - advance
            if pre_dt_local > _now_local():
                text = f"⏰ Recordatorio próximo: «{rem.titulo}» a las {rdt_local.strftime('%H:%M')}."
                ScheduledMessageService.create(
                    customer_id=rem.customer_id,
                    target_phone=rem.customer.phone,
                    text=text,
                    send_at=pre_dt_local.astimezone(timezone.utc),
                )


def schedule_scheduled_message(_ignored_scheduler, sm_id: int, send_at: datetime) -> None:
    """
    Asegura la fecha esperada; el envío lo hará run_due_jobs().
    """
    app = _require_app()
    with app.app_context():
        sm = db.session.get(ScheduledMessage, sm_id)
        if not sm:
            return
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=ZoneInfo(LOCAL_TZ_NAME)).astimezone(timezone.utc)
        else:
            send_at = send_at.astimezone(timezone.utc)

        if sm.send_at != send_at:
            sm.send_at = send_at
            db.session.commit()


def cancel_scheduled_message(sm_id: int) -> bool:
    """Eliminar de BD (si no existe, no se enviará)."""
    app = _require_app()
    with app.app_context():
        sm = db.session.get(ScheduledMessage, sm_id)
        if not sm:
            return False
        db.session.delete(sm)
        db.session.commit()
        return True


# ────────────────────────────────────────────────────────────────────
# Claim & Send (idempotente, concurrente-safe)
# ────────────────────────────────────────────────────────────────────

def _claim_pending_scheduled_messages(sess: Session, now_utc: datetime, limit: int) -> list[ScheduledMessage]:
    """
    Toma en exclusiva (SKIP LOCKED) un batch de mensajes pendientes.
    Evita doble envío si hay dos Lambdas corriendo en paralelo.
    """
    # Bloqueamos filas PENDING con send_at vencido
    q = (
        select(ScheduledMessage)
        .where(ScheduledMessage.status == "pending", ScheduledMessage.send_at <= now_utc)
        .order_by(ScheduledMessage.send_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    rows = sess.execute(q).scalars().all()
    # Marcamos 'sending' en la misma transacción
    for r in rows:
        r.status = "sending"
        r.claimed_at = now_utc
    return rows


def _process_scheduled_messages(sess: Session, rows: list[ScheduledMessage], now_utc: datetime) -> int:
    sent = 0
    for sm in rows:
        try:
            ScheduledMessageService.send(sm.id)  # se encarga de construir y enviar
            sm.status = "sent"
            sm.sent_at = now_utc
            sent += 1
        except Exception:
            logging.exception("[SchedulerService] Error enviando ScheduledMessage id=%s", sm.id)
            sm.status = "error"
            sm.error_at = now_utc
    return sent


def _claim_due_reminders(sess: Session, now_local: datetime, limit: int) -> list[Reminder]:
    """
    Selecciona recordatorios vencidos. Usamos SKIP LOCKED via select+delete por ítem.
    Si tu tabla crece, agregá índice por `date`.
    """
    q = (
        select(Reminder)
        .join(Customer)
        .order_by(Reminder.date.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    )
    candidates = sess.execute(q).scalars().all()
    due: list[Reminder] = []
    local_tz = ZoneInfo(LOCAL_TZ_NAME)
    for r in candidates:
        rdt = r.date
        rdt_local = rdt.replace(tzinfo=local_tz) if rdt.tzinfo is None else rdt.astimezone(local_tz)
        if rdt_local <= now_local:
            due.append(r)
    return due


def _process_reminders(sess: Session, rows: list[Reminder]) -> int:
    sent = 0
    for r in rows:
        try:
            payload = get_event_reminder_template_input(
                recipient=r.customer.phone,
                titulo=r.titulo,
            )
            send_message(payload)
            sess.delete(r)  # idempotencia: no vuelve a aparecer en próximas corridas
            sent += 1
        except Exception:
            logging.exception("[SchedulerService] Error enviando Reminder id=%s", r.id)
    return sent


# ────────────────────────────────────────────────────────────────────
# Entry point ejecutado por EventBridge
# ────────────────────────────────────────────────────────────────────

def run_due_jobs() -> dict:
    """
    Llamar sólo cuando la invocación provenga de EventBridge.
    Retorna métricas simples para logs/CloudWatch.
    """
    app = _require_app()
    now_utc = _now_utc()
    now_local = _now_local()

    with app.app_context():
        # Usamos una única sesión/tx por batch para reducir round-trips
        # 1) Scheduled Messages
        with db.session.begin():
            rows_sm = _claim_pending_scheduled_messages(db.session, now_utc, BATCH_LIMIT)
        # Fuera del "claim" (liberamos locks), procesamos y persistimos resultado
        with db.session.begin():
            sent_msgs = _process_scheduled_messages(db.session, rows_sm, now_utc)

        # 2) Reminders
        with db.session.begin():
            rows_rem = _claim_due_reminders(db.session, now_local, BATCH_LIMIT)
            sent_rem = _process_reminders(db.session, rows_rem)

    if app:
        app.logger.info(
            "[SchedulerService] run_due_jobs: scheduled_sent=%s, reminders_sent=%s",
            sent_msgs, sent_rem,
        )

    return {"ok": True, "scheduled_sent": sent_msgs, "reminders_sent": sent_rem}
