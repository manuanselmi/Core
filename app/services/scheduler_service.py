# app/services/scheduler_service.py
from __future__ import annotations
import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import current_app
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from app.models import (
    db,
    Booking,
    BookingReminder,
    Reminder,
    ScheduledMessage,
)
# Opcional: si existe, lo usamos. Si no, caemos a logs.
try:
    # Procura que estos helpers existan o adáptalos cuando integremos el adapter.
    from app.utils.whatsapp_utils import send_text_message as _wa_send_text  # (phone: str, text: str) -> dict con 'wamid'
except Exception:
    _wa_send_text = None

logger = logging.getLogger(__name__)

# -----------------------------
# Helpers de tiempo/UTC
# -----------------------------
def _utcnow() -> datetime:
    return datetime.utcnow().replace(tzinfo=timezone.utc)

def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

# -----------------------------
# Inicio/Singleton del scheduler
# -----------------------------
def init_scheduler(app) -> Optional[BackgroundScheduler]:
    """
    Inicializa APScheduler una sola vez por proceso.
    Respeta:
      - SCHEDULER_ENABLED (bool, default True)
      - Evita doble start con el reloader (WERKZEUG_RUN_MAIN check)
    Reprograma en boot los pendientes de:
      - BookingReminder (status=pending)
      - Reminder (legacy genéricos)
      - ScheduledMessage (legacy)
    """
    enabled = str(app.config.get("SCHEDULER_ENABLED", "true")).lower() in ("1", "true", "yes")
    if not enabled:
        logger.warning("Scheduler deshabilitado por config SCHEDULER_ENABLED.")
        return None

    # Evitar doble arranque con el reloader de Flask
    if app.config.get("ENV") == "development":
        if os.environ.get("WERKZEUG_RUN_MAIN") != "true":
            logger.info("Evito iniciar scheduler en proceso de carga (reloader).")
            return None

    # Evitar reinicios múltiples por error
    if app.extensions.get("scheduler_started"):
        logger.info("Scheduler ya iniciado, omito.")
        return app.extensions.get("scheduler")

    scheduler = BackgroundScheduler(
        timezone="UTC",
        job_defaults={
            "coalesce": True,             # si hay backlog junta a 1 ejecución
            "max_instances": 1,
            "misfire_grace_time": 60 * 10 # 10 min de gracia
        },
    )
    scheduler.start()
    app.extensions["scheduler"] = scheduler
    app.extensions["scheduler_started"] = True

    logger.info("Scheduler iniciado. Reprogramando trabajos pendientes...")
    with app.app_context():
        try:
            _reschedule_all_pending(scheduler)
            logger.info("Reprogramación inicial completada.")
        except Exception as e:
            logger.exception("Fallo al reprogramar pendientes en boot: %s", e)

    return scheduler

def get_scheduler() -> Optional[BackgroundScheduler]:
    return current_app.extensions.get("scheduler")

# -----------------------------
# Reprogramación en arranque
# -----------------------------
def _reschedule_all_pending(scheduler: BackgroundScheduler) -> None:
    now = _utcnow()

    # BookingReminder (CORE T-24/T-3)
    brs = BookingReminder.query.filter(BookingReminder.status == "pending").all()
    for br in brs:
        if not br.send_at:
            continue
        send_at = _ensure_aware_utc(br.send_at)
        _schedule_booking_reminder_job(scheduler, br.id, run_at=send_at, allow_past=True)

    # Reminder genéricos (legacy)
    rems = Reminder.query.all()
    for r in rems:
        if not r.date:
            continue
        run_at = _ensure_aware_utc(r.date)
        # Si ya pasó por mucho, no lo resucitamos para evitar spam
        if run_at < (now - timedelta(days=1)):
            continue
        _schedule_reminder_job(scheduler, r.id, run_at=run_at, allow_past=True)

    # ScheduledMessage (legacy: mensajes a terceros)
    sms = ScheduledMessage.query.filter(ScheduledMessage.status == "pending").all()
    for sm in sms:
        if not sm.send_at:
            continue
        run_at = _ensure_aware_utc(sm.send_at)
        if run_at < (now - timedelta(days=1)):
            continue
        _schedule_scheduled_message_job(scheduler, sm.id, run_at=run_at, allow_past=True)

# -----------------------------
# API pública: encolar trabajos
# -----------------------------
def schedule_booking_reminder(booking_reminder_id: int) -> None:
    """
    Encola (si no existe) un job de envío para un BookingReminder.
    """
    scheduler = get_scheduler()
    if not scheduler:
        logger.warning("schedule_booking_reminder: scheduler no inicializado.")
        return
    br = BookingReminder.query.get(booking_reminder_id)
    if not br or not br.send_at or br.status != "pending":
        return
    _schedule_booking_reminder_job(scheduler, br.id, run_at=_ensure_aware_utc(br.send_at))

def schedule_reminder(reminder_id: int) -> None:
    """
    Encola (si no existe) un job de envío para un Reminder (legacy).
    """
    scheduler = get_scheduler()
    if not scheduler:
        logger.warning("schedule_reminder: scheduler no inicializado.")
        return
    r = Reminder.query.get(reminder_id)
    if not r or not r.date:
        return
    _schedule_reminder_job(scheduler, r.id, run_at=_ensure_aware_utc(r.date))

def schedule_scheduled_message(scheduled_message_id: int) -> None:
    """
    Encola (si no existe) un job de envío para un ScheduledMessage (legacy).
    """
    scheduler = get_scheduler()
    if not scheduler:
        logger.warning("schedule_scheduled_message: scheduler no inicializado.")
        return
    sm = ScheduledMessage.query.get(scheduled_message_id)
    if not sm or not sm.send_at or sm.status != "pending":
        return
    _schedule_scheduled_message_job(scheduler, sm.id, run_at=_ensure_aware_utc(sm.send_at))

# -----------------------------
# Internos para crear jobs
# -----------------------------
def _schedule_booking_reminder_job(
    scheduler: BackgroundScheduler,
    booking_reminder_id: int,
    *,
    run_at: datetime,
    allow_past: bool = False,
) -> None:
    job_id = f"booking_reminder:{booking_reminder_id}"
    if scheduler.get_job(job_id):
        return
    now = _utcnow()
    if run_at < now and not allow_past:
        return
    trigger = DateTrigger(run_date=run_at)
    scheduler.add_job(
        func=_job_send_booking_reminder,
        trigger=trigger,
        id=job_id,
        args=[booking_reminder_id],
        replace_existing=False,
        max_instances=1,
    )
    logger.info("Job encolado %s para %s", job_id, run_at.isoformat())

def _schedule_reminder_job(
    scheduler: BackgroundScheduler,
    reminder_id: int,
    *,
    run_at: datetime,
    allow_past: bool = False,
) -> None:
    job_id = f"reminder:{reminder_id}"
    if scheduler.get_job(job_id):
        return
    now = _utcnow()
    if run_at < now and not allow_past:
        return
    trigger = DateTrigger(run_date=run_at)
    scheduler.add_job(
        func=_job_send_legacy_reminder,
        trigger=trigger,
        id=job_id,
        args=[reminder_id],
        replace_existing=False,
        max_instances=1,
    )
    logger.info("Job encolado %s para %s", job_id, run_at.isoformat())

def _schedule_scheduled_message_job(
    scheduler: BackgroundScheduler,
    scheduled_message_id: int,
    *,
    run_at: datetime,
    allow_past: bool = False,
) -> None:
    job_id = f"scheduled_message:{scheduled_message_id}"
    if scheduler.get_job(job_id):
        return
    now = _utcnow()
    if run_at < now and not allow_past:
        return
    trigger = DateTrigger(run_date=run_at)
    scheduler.add_job(
        func=_job_send_scheduled_message,
        trigger=trigger,
        id=job_id,
        args=[scheduled_message_id],
        replace_existing=False,
        max_instances=1,
    )
    logger.info("Job encolado %s para %s", job_id, run_at.isoformat())

# -----------------------------
# Funciones de job (envío real)
# -----------------------------
def _job_send_booking_reminder(booking_reminder_id: int) -> None:
    """
    Envío T-24/T-3 para BookingReminder. 
    Intenta usar whatsapp_utils.send_text_message si existe; si no, loguea y marca como 'sent' para evitar loops.
    """
    with current_app.app_context():
        br = BookingReminder.query.get(booking_reminder_id)
        if not br:
            logger.warning("BookingReminder %s no existe.", booking_reminder_id)
            return
        if br.status != "pending":
            logger.info("BookingReminder %s estado=%s, omito.", booking_reminder_id, br.status)
            return

        booking: Optional[Booking] = Booking.query.get(br.booking_id)
        if not booking:
            logger.warning("Booking %s inexistente para reminder %s.", br.booking_id, booking_reminder_id)
            br.status = "error"
            db.session.commit()
            return

        cust = booking.customer
        if not cust or not cust.phone:
            logger.warning("Cliente sin teléfono para booking %s.", booking.id)
            br.status = "error"
            db.session.commit()
            return

        # Mensaje simple placeholder. Luego lo cambiamos por plantilla por tenant.
        kind = br.kind
        when = booking.starts_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        text = f"Recordatorio ({kind}): tenés un turno el {when}."

        try:
            wamid = None
            if _wa_send_text:
                resp = _wa_send_text(cust.phone, text)  # se espera {'wamid': '...'}
                wamid = (resp or {}).get("wamid")
            else:
                logger.info("[DRY] Enviaría WA a %s: %s", cust.phone, text)

            br.sent_at = _utcnow()
            br.status = "sent"
            if wamid:
                br.wa_msg_id = wamid
            db.session.commit()
            logger.info("BookingReminder %s enviado.", booking_reminder_id)
        except Exception as e:
            logger.exception("Error enviando BookingReminder %s: %s", booking_reminder_id, e)
            br.status = "error"
            db.session.commit()

def _job_send_legacy_reminder(reminder_id: int) -> None:
    """
    Envío para Reminder (legacy, genérico).
    """
    with current_app.app_context():
        r = Reminder.query.get(reminder_id)
        if not r:
            logger.warning("Reminder %s no existe.", reminder_id)
            return
        cust = r.customer
        if not cust or not cust.phone:
            logger.warning("Reminder %s sin phone.", reminder_id)
            return

        text = f"Recordatorio: {r.titulo}"
        try:
            wamid = None
            if _wa_send_text:
                resp = _wa_send_text(cust.phone, text)
                wamid = (resp or {}).get("wamid")
            else:
                logger.info("[DRY] Enviaría WA a %s: %s", cust.phone, text)

            # Legacy Reminder no tiene status; si querés, podríamos crear un log.
            if wamid and not r.wa_msg_id:
                r.wa_msg_id = wamid
            db.session.commit()
            logger.info("Reminder %s enviado.", reminder_id)
        except Exception as e:
            logger.exception("Error enviando Reminder %s: %s", reminder_id, e)

def _job_send_scheduled_message(scheduled_message_id: int) -> None:
    """
    Envío para ScheduledMessage (legacy: mensajes a terceros).
    """
    with current_app.app_context():
        sm = ScheduledMessage.query.get(scheduled_message_id)
        if not sm:
            logger.warning("ScheduledMessage %s no existe.", scheduled_message_id)
            return
        if sm.status != "pending":
            logger.info("ScheduledMessage %s estado=%s, omito.", scheduled_message_id, sm.status)
            return
        if not sm.target_phone:
            logger.warning("ScheduledMessage %s sin target_phone.", scheduled_message_id)
            sm.mark_error()
            return

        try:
            wamid = None
            if _wa_send_text:
                resp = _wa_send_text(sm.target_phone, sm.text)
                wamid = (resp or {}).get("wamid")
            else:
                logger.info("[DRY] Enviaría WA a %s: %s", sm.target_phone, sm.text)

            if wamid:
                sm.wa_msg_id = wamid
            sm.mark_sent()
            logger.info("ScheduledMessage %s enviado.", scheduled_message_id)
        except Exception as e:
            logger.exception("Error enviando ScheduledMessage %s: %s", scheduled_message_id, e)
            sm.mark_error()
