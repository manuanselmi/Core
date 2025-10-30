from __future__ import annotations

import os
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from flask import current_app
from zoneinfo import ZoneInfo

from app.db import RepositoryProvider
from app.utils.whatsapp_utils import (
    get_event_reminder_template_input,
    send_message,
)

# ────────────────────────────────────────────────────────────────────
# Config & Globals
# ────────────────────────────────────────────────────────────────────

_app = None          # se setea en init_scheduler(app)
_initialized = False
_repo_provider = None  # RepositoryProvider para DynamoDB

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
    global _app, _initialized, _repo_provider
    _app = app
    _initialized = True
    _repo_provider = RepositoryProvider()
    tz = ZoneInfo(LOCAL_TZ_NAME)
    if _app:
        _app.logger.info("[SchedulerService] (EventBridge) iniciado. TZ=%s", tz)
    return scheduler


def _require_app():
    if not _initialized or _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")
    return _app


def _require_repo() -> RepositoryProvider:
    """Obtiene RepositoryProvider (DynamoDB) para acceso a datos."""
    if _repo_provider is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")
    return _repo_provider


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────

def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _now_local() -> datetime:
    return datetime.now(ZoneInfo(LOCAL_TZ_NAME))


def _now_ms() -> int:
    """Epoch timestamp en milisegundos (UTC)."""
    return int(_now_utc().timestamp() * 1000)


# ────────────────────────────────────────────────────────────────────
# API de programación (compat)
# ────────────────────────────────────────────────────────────────────

def schedule_event_reminder(_ignored_scheduler, reminder_id: int, advance: timedelta = timedelta(minutes=1)) -> None:
    """
    DEPRECADO: Esta función era para SQL-based reminders.
    Con DynamoDB, los reminders se crean directamente via repos.
    Se mantiene por compatibilidad pero no hace nada.
    """
    app = _require_app()
    app.logger.warning(
        "[SchedulerService] schedule_event_reminder(%s) deprecado. "
        "Los reminders se crean directamente con repos DynamoDB.",
        reminder_id
    )


def schedule_scheduled_message(_ignored_scheduler, sm_id: int, send_at: datetime) -> None:
    """
    DEPRECADO: Esta función era para SQL-based scheduled messages.
    Con DynamoDB, los mensajes se encolan directamente via repos.
    Se mantiene por compatibilidad pero no hace nada.
    """
    app = _require_app()
    app.logger.warning(
        "[SchedulerService] schedule_scheduled_message(%s) deprecado. "
        "Los mensajes se encolan directamente con repos DynamoDB.",
        sm_id
    )


def cancel_scheduled_message(sm_id: int) -> bool:
    """
    DEPRECADO: Con DynamoDB no tenemos un sm_id numérico simple.
    Los mensajes se cancelan directamente via repos DynamoDB con pk/sk.
    """
    app = _require_app()
    app.logger.warning(
        "[SchedulerService] cancel_scheduled_message(%s) deprecado. "
        "Usar repos DynamoDB directamente con pk/sk.",
        sm_id
    )
    return False


# ────────────────────────────────────────────────────────────────────
# Claim & Send (idempotente, concurrente-safe) - DynamoDB
# ────────────────────────────────────────────────────────────────────

def _claim_pending_scheduled_messages(repo: RepositoryProvider, now_ms: int, limit: int) -> list[dict]:
    """
    Reclama batch de mensajes programados pendientes usando DynamoDB.
    
    Notes:
        - Usa claim_pending_batch() del repo con ConditionExpression atómico
        - Retorna lista de mensajes reclamados con status='sending'
    """
    return repo.scheduled_messages.claim_pending_batch(
        now_ms=now_ms,
        n=limit,
        stale_after_ms=60000  # 1 min stale threshold
    )


def _process_scheduled_messages(repo: RepositoryProvider, rows: list[dict], now_ms: int) -> int:
    """
    Procesa mensajes programados reclamados: envía y marca como sent/error.
    """
    sent = 0
    for msg in rows:
        try:
            # Enviar mensaje
            target_phone = msg.get('target_phone')
            text = msg.get('text')
            
            if not target_phone or not text:
                logging.warning("[SchedulerService] Mensaje sin target_phone o text: %s", msg)
                continue
            
            # Construir payload y enviar
            from app.utils.whatsapp_utils import send_comida_template
            customer_name = msg.get('customer_phone', target_phone)  # fallback
            
            result = send_comida_template(
                recipient=target_phone,
                name=customer_name,
                phone=target_phone,
                message=text
            )
            
            wa_msg_id = result.get('messages', [{}])[0].get('id', 'unknown')
            
            # Marcar como sent
            key = {'pk': msg['pk'], 'sk': msg['sk']}
            repo.scheduled_messages.mark_sent(key=key, wa_msg_id=wa_msg_id, at_ms=now_ms)
            sent += 1
            
        except Exception:
            logging.exception("[SchedulerService] Error enviando ScheduledMessage: %s", msg)
            # Marcar como error (si existe método mark_failed en repo)
            try:
                key = {'pk': msg['pk'], 'sk': msg['sk']}
                repo.scheduled_messages.update_conditional(
                    key=key,
                    update_expr='SET #st = :error, error_at = :at',
                    expr_attr_names={'#st': 'status'},
                    expr_attr_values={':error': 'error', ':at': now_ms}
                )
            except Exception:
                logging.exception("[SchedulerService] Error marcando mensaje como error")
    
    return sent


def _claim_due_reminders(repo: RepositoryProvider, now_ms: int, limit: int) -> list[dict]:
    """
    Selecciona recordatorios standalone vencidos (no vinculados a appointments).
    
    Notes:
        - En DynamoDB, los reminders standalone se consultan por customer
        - No hay un query global directo, pero podemos usar GSI si está disponible
        - Por ahora, esta función retorna lista vacía porque los reminders
          vinculados a appointments se procesan en _claim_due_appointments
    """
    # TODO: Implementar query global de reminders standalone si es necesario
    # Por ahora, los reminders standalone no tienen GSI de vencimiento
    # Se procesan manualmente o se eliminan de la funcionalidad
    return []


def _process_reminders(repo: RepositoryProvider, rows: list[dict]) -> int:
    """
    Procesa reminders standalone vencidos.
    
    Notes:
        - Esta función no se usa actualmente porque los reminders
          standalone no tienen query global en DynamoDB
    """
    sent = 0
    for r in rows:
        try:
            phone = r.get('customer_phone')
            titulo = r.get('titulo')
            
            if not phone or not titulo:
                logging.warning("[SchedulerService] Reminder sin phone o titulo: %s", r)
                continue
            
            payload = get_event_reminder_template_input(
                recipient=phone,
                titulo=titulo,
            )
            send_message(payload)
            
            # Eliminar reminder (idempotencia)
            key = {'pk': r['pk'], 'sk': r['sk']}
            repo.reminders.delete_conditional(key)
            sent += 1
            
        except Exception:
            logging.exception("[SchedulerService] Error enviando Reminder: %s", r)
    
    return sent


def _claim_due_appointments(repo: RepositoryProvider, now_ms: int, limit: int) -> list[dict]:
    """
    Selecciona appointments con recordatorio pendiente usando DynamoDB GSI.
    
    Notes:
        - Usa query_due_reminders() del appointment_repo
        - GSI ApptReminderQueue: PK='pending#scheduled', SK=remind_at_epoch
        - No requiere SKIP LOCKED porque DynamoDB es event-driven
    """
    return repo.appointments.query_due_reminders(now_ms=now_ms, limit=limit)


def _process_appointments(repo: RepositoryProvider, rows: list[dict], now_ms: int) -> tuple[int, int, int]:
    """
    Procesa appointments para enviar recordatorios.
    Valida existencia del evento en Google Calendar antes de enviar.
    Retorna (sent, skipped, canceled_detected).
    """
    from app.services.google_calendar_service import GoogleCalendarService
    from app.config.settings import SETTINGS
    
    sent = 0
    skipped = 0
    canceled_detected = 0
    
    try:
        gcal = GoogleCalendarService()
    except Exception:
        logging.warning("[SchedulerService] Google Calendar no disponible para validar eventos")
        gcal = None
    
    for appt in rows:
        try:
            # Validar existencia del evento en Google Calendar
            google_event_id = appt.get('google_event_id')
            google_calendar_id = appt.get('google_calendar_id')
            
            if gcal and google_event_id:
                event = gcal.get_event(google_event_id, google_calendar_id)
                if not event or event.get("status") == "cancelled":
                    # Evento no existe o fue cancelado externamente
                    logging.info(
                        "[SchedulerService] Evento cancelado/inexistente en Google: appt=%s, event_id=%s",
                        appt.get('appointment_id'), google_event_id
                    )
                    
                    # Actualizar status en DynamoDB
                    key = {'pk': appt['pk'], 'sk': appt['sk']}
                    repo.appointments.update_conditional(
                        key=key,
                        update_expr='SET #st = :canceled, #rs = :skipped, cancel_reason = :reason',
                        expr_attr_names={'#st': 'status', '#rs': 'reminder_status'},
                        expr_attr_values={
                            ':canceled': 'canceled',
                            ':skipped': 'skipped',
                            ':reason': 'Cancelado externamente (detectado al enviar recordatorio)'
                        }
                    )
                    canceled_detected += 1
                    continue
            
            # Obtener customer phone
            customer_phone = appt.get('customer_phone')
            if not customer_phone:
                logging.warning("[SchedulerService] Appointment sin customer_phone: %s", appt)
                key = {'pk': appt['pk'], 'sk': appt['sk']}
                repo.appointments.update_reminder_status(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    new_status='error'
                )
                continue
            
            # Construir mensaje de recordatorio
            agent_name = SETTINGS.AGENT_NAME
            titulo = f"Tenés turno con {agent_name} en una hora."
            if appt.get('title'):
                titulo = f"Recordatorio: {appt['title']}"
            
            # Enviar recordatorio
            payload = get_event_reminder_template_input(
                recipient=customer_phone,
                titulo=titulo
            )
            send_message(payload)
            
            # Marcar como enviado
            repo.appointments.update_reminder_status(
                pk=appt['pk'],
                sk=appt['sk'],
                new_status='sent'
            )
            
            # Actualizar last_reminder_sent_at
            key = {'pk': appt['pk'], 'sk': appt['sk']}
            repo.appointments.update_conditional(
                key=key,
                update_expr='SET last_reminder_sent_at = :at',
                expr_attr_names={},
                expr_attr_values={':at': now_ms}
            )
            
            sent += 1
            logging.info(
                "[SchedulerService] Recordatorio enviado: appt=%s, customer=%s",
                appt.get('appointment_id'), customer_phone
            )
            
        except Exception:
            logging.exception("[SchedulerService] Error procesando Appointment: %s", appt)
            try:
                repo.appointments.update_reminder_status(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    new_status='error'
                )
            except Exception:
                logging.exception("[SchedulerService] Error marcando appointment como error")
    
    return (sent, skipped, canceled_detected)


# ────────────────────────────────────────────────────────────────────
# Entry point ejecutado por EventBridge
# ────────────────────────────────────────────────────────────────────

def run_due_jobs() -> dict:
    """
    Llamar sólo cuando la invocación provenga de EventBridge.
    Retorna métricas simples para logs/CloudWatch.
    
    Notes:
        - Usa DynamoDB repos exclusivamente (sin SQLAlchemy)
        - Procesa: scheduled messages, appointments, reminders standalone
    """
    app = _require_app()
    repo = _require_repo()
    now_ms = _now_ms()

    with app.app_context():
        # 1) Scheduled Messages
        rows_sm = _claim_pending_scheduled_messages(repo, now_ms, BATCH_LIMIT)
        sent_msgs = _process_scheduled_messages(repo, rows_sm, now_ms)

        # 2) Appointments (recordatorios de citas)
        rows_appt = _claim_due_appointments(repo, now_ms, BATCH_LIMIT)
        sent_appt, skipped_appt, canceled_appt = _process_appointments(repo, rows_appt, now_ms)

        # 3) Reminders (standalone, no vinculados a appointments)
        # NOTA: Actualmente deshabilitado porque no hay GSI global para reminders standalone
        rows_rem = _claim_due_reminders(repo, now_ms, BATCH_LIMIT)
        sent_rem = _process_reminders(repo, rows_rem)

    if app:
        app.logger.info(
            "[SchedulerService] run_due_jobs: scheduled_sent=%s, appointments_sent=%s, "
            "appointments_skipped=%s, appointments_canceled_detected=%s, reminders_sent=%s",
            sent_msgs, sent_appt, skipped_appt, canceled_appt, sent_rem,
        )

    return {
        "ok": True,
        "scheduled_sent": sent_msgs,
        "appointments_sent": sent_appt,
        "appointments_skipped": skipped_appt,
        "appointments_canceled_detected": canceled_appt,
        "reminders_sent": sent_rem
    }
