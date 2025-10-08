from __future__ import annotations

"""
SchedulerService (EventBridge edition)
--------------------------------------
Reemplaza APScheduler por un modelo "polling" disparado por EventBridge Scheduler.

- NO mantiene un scheduler en memoria. En su lugar:
  - `schedule_event_reminder(...)` y `schedule_scheduled_message(...)` sólo persisten en BD.
  - Un cron de EventBridge invoca la Lambda cada N minutos, que llama a `run_due_jobs()`.
  - Idempotencia: Reminders se eliminan tras enviar; ScheduledMessage se envían con
    `ScheduledMessageService.send()` que marca `sent` o `error` y también elimina la fila si corresponde.

Compatibilidad:
- Se expone un atributo `scheduler` "no-op" con `.add_job()/.get_job()/.remove_job()`
  para no romper imports/llamadas existentes. Es inofensivo y sólo loguea.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from typing import Optional

from flask import current_app as app
from sqlalchemy import select

from app.models import db, Reminder, Customer, ScheduledMessage
from app.services.scheduled_message_service import ScheduledMessageService
from app.utils.whatsapp_utils import (
    get_event_reminder_template_input,
    send_message,
)

# ────────────────────────────────────────────────────────────────────
# Globals
# ────────────────────────────────────────────────────────────────────

LOCAL_TZ = ZoneInfo(app.config.get("TZ", "America/Montevideo")) if app else ZoneInfo("America/Montevideo")
_app = None  # se setea en init_scheduler()


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
# Init / Boot
# ────────────────────────────────────────────────────────────────────

def init_scheduler(app_):
    """Compat: guarda `app` y fija TZ. No arranca nada en memoria."""
    global _app, LOCAL_TZ
    _app = app_
    LOCAL_TZ = ZoneInfo(_app.config.get("TZ", "America/Montevideo"))
    if _app:
        _app.logger.info("[SchedulerService] (EventBridge) iniciado. TZ=%s", LOCAL_TZ)
    return scheduler  # por compat (quien lo llame puede ignorarlo)


def reschedule_all_on_boot():
    """
    Compat: antes reprogramábamos todos los jobs en el arranque del proceso.
    Con EventBridge ya NO es necesario. Dejamos el hook por si algún módulo lo invoca.
    """
    if _app:
        _app.logger.info("[SchedulerService] reschedule_all_on_boot(): no-op (EventBridge).")


# ────────────────────────────────────────────────────────────────────
# API de programación
# ────────────────────────────────────────────────────────────────────

def schedule_event_reminder(
    _ignored_scheduler: _NoopScheduler,   # compat con firma legacy
    reminder_id: int,
    advance: timedelta = timedelta(minutes=1),
) -> None:
    """
    Prepara el envío del recordatorio:
      - (Opcional) «pre-aviso» `advance` antes (como ScheduledMessage de texto).
      - Notificación en hora: se resuelve en `run_due_jobs()` (lee `reminders` vencidos).
    """
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")

    with _app.app_context():
        rem: Reminder | None = (
            db.session.query(Reminder)
            .join(Customer)
            .filter(Reminder.id == reminder_id)
            .first()
        )
        if not rem:
            return

        # Normalizar fecha a TZ local
        rdt = rem.date
        if rdt.tzinfo is None:
            rdt_local = rdt.replace(tzinfo=LOCAL_TZ)
        else:
            rdt_local = rdt.astimezone(LOCAL_TZ)

        # Programar pre-aviso como ScheduledMessage (texto simple)
        if advance and advance.total_seconds() > 0:
            pre_dt = rdt_local - advance
            now_local = datetime.now(LOCAL_TZ)
            if pre_dt > now_local:
                text = f"⏰ Recordatorio próximo: «{rem.titulo}» a las {rdt_local.strftime('%H:%M')}."
                ScheduledMessageService.create(
                    customer_id=rem.customer_id,
                    target_phone=rem.customer.phone,
                    text=text,
                    send_at=pre_dt.astimezone(timezone.utc),
                )
                # NOTA: no generamos un job; EventBridge hará polling y enviará.


def schedule_scheduled_message(
    _ignored_scheduler: _NoopScheduler,
    sm_id: int,
    send_at: datetime,
) -> None:
    """
    Compat: asegura que el ScheduledMessage existe con `send_at` esperado.
    El envío lo hará `run_due_jobs()` cuando venza.
    """
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")

    with _app.app_context():
        sm = db.session.get(ScheduledMessage, sm_id)
        if not sm:
            return
        # Normalizamos a UTC para comparación/consistencia
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=LOCAL_TZ).astimezone(timezone.utc)
        else:
            send_at = send_at.astimezone(timezone.utc)

        if sm.send_at != send_at:
            sm.send_at = send_at
            db.session.commit()


def cancel_scheduled_message(sm_id: int) -> bool:
    """Elimina el registro: al no existir, no será enviado por `run_due_jobs()`."""
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")
    with _app.app_context():
        sm = db.session.get(ScheduledMessage, sm_id)
        if not sm:
            return False
        db.session.delete(sm)
        db.session.commit()
        return True


# ────────────────────────────────────────────────────────────────────
# Loop de ejecución invocado por EventBridge
# ────────────────────────────────────────────────────────────────────

def _send_due_scheduled_messages(now_utc: datetime) -> int:
    """Envia ScheduledMessage pendientes (<= now_utc)."""
    # Selección conservadora: hasta 100 por tirada para evitar picos.
    # (Si necesitás más, hacemos batch + paginación)
    pending: list[ScheduledMessage] = (
        db.session.query(ScheduledMessage)
        .filter(ScheduledMessage.status == "pending",
                ScheduledMessage.send_at <= now_utc)
        .order_by(ScheduledMessage.send_at.asc())
        .limit(100)
        .all()
    )
    sent = 0
    for sm in pending:
        try:
            ScheduledMessageService.send(sm.id)
            sent += 1
        except Exception:
            # ScheduledMessageService ya marca error; seguimos con el siguiente
            if _app:
                _app.logger.exception("[SchedulerService] Error enviando ScheduledMessage id=%s", sm.id)
    return sent


def _send_due_reminders(now_local: datetime) -> int:
    """
    Envía notificaciones de recordatorios cuya `date` ≤ now_local.
    - Si la fecha es naive, se asume `LOCAL_TZ`.
    - Idempotencia: se elimina la fila tras enviar.
    """
    # Traemos un subset cercano (±1 día) para minimizar scanning si crecen tablas.
    # Si tus volúmenes crecen, mover a condición por rangos + índices.
    approx_start = now_local - timedelta(days=1)
    approx_end   = now_local + timedelta(minutes=1)

    reminders: list[Reminder] = (
        db.session.query(Reminder)
        .join(Customer)
        .filter(Reminder.date >= approx_start.replace(tzinfo=None),  # naive compare
                Reminder.date <= approx_end.replace(tzinfo=None))
        .all()
    )

    sent = 0
    for r in reminders:
        # Verificación precisa en tz local
        rdt = r.date
        rdt_local = rdt.replace(tzinfo=LOCAL_TZ) if rdt.tzinfo is None else rdt.astimezone(LOCAL_TZ)
        if rdt_local <= now_local:
            try:
                payload = get_event_reminder_template_input(
                    recipient=r.customer.phone,
                    titulo=r.titulo,
                )
                send_message(payload)
                db.session.delete(r)
                db.session.commit()
                sent += 1
            except Exception:
                if _app:
                    _app.logger.exception("[SchedulerService] Error enviando Reminder id=%s", r.id)
    return sent


def run_due_jobs() -> dict:
    """
    Punto único que debería invocar la Lambda cuando `event['source']=='aws.events'`.
    Retorna métricas simples para logging/CloudWatch.
    """
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")

    now_utc = datetime.now(timezone.utc)
    now_local = datetime.now(LOCAL_TZ)

    with _app.app_context():
        sent_msgs = _send_due_scheduled_messages(now_utc)
        sent_rem  = _send_due_reminders(now_local)

    if _app:
        _app.logger.info("[SchedulerService] run_due_jobs: scheduled=%s, reminders=%s", sent_msgs, sent_rem)

    return {"ok": True, "scheduled_sent": sent_msgs, "reminders_sent": sent_rem}
