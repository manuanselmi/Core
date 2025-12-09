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


def _format_datetime_for_reminder(epoch_ms: int, tz_name: str = None) -> tuple[str, str]:
    """
    Formatea un timestamp epoch_ms a fecha y hora separadas.
    
    Args:
        epoch_ms: Timestamp en milisegundos (epoch UTC)
        tz_name: Nombre de timezone (ej: 'America/Montevideo'). Si es None, usa LOCAL_TZ_NAME
    
    Returns:
        Tupla (fecha, hora) formateadas para el recordatorio
        Ejemplo: ("04/11/2025", "14:30")
    """
    if tz_name is None:
        tz_name = LOCAL_TZ_NAME
    
    tz = ZoneInfo(tz_name)
    dt = datetime.fromtimestamp(epoch_ms / 1000, tz=tz)
    
    # Formato: DD/MM/YYYY
    fecha = dt.strftime("%d/%m/%Y")
    # Formato: HH:MM
    hora = dt.strftime("%H:%M")
    
    return (fecha, hora)


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
    Procesa mensajes programados reclamados: envía y borra el item de la base.
    
    Notes:
        - Ventana exacta: solo envía si now_ms está en [send_at_ms, send_at_ms + 60s)
        - De-dup intra-tick: usa set con pk#sk para evitar duplicados en mismo barrido
        - CAMBIO CRÍTICO: después de enviar exitosamente, se BORRA el item (no solo marca sent)
    """
    sent = 0
    seen_keys = set()
    
    for msg in rows:
        try:
            # 1) De-dup intra-tick
            key_str = f"{msg['pk']}#{msg['sk']}"
            if key_str in seen_keys:
                logging.debug("[SchedulerService] Duplicado intra-tick (SM): %s", key_str)
                continue
            seen_keys.add(key_str)
            
            # 2) Verificar ventana exacta (send_at_ms <= now_ms < send_at_ms + 60s)
            send_at_ms = msg.get('send_at_epoch')
            if not send_at_ms:
                logging.warning("[SchedulerService] ScheduledMessage sin send_at_epoch: %s", msg)
                continue
            
            window_end = send_at_ms + (60 * 1000)  # 60 segundos
            
            if now_ms < send_at_ms:
                # Todavía no es tiempo (no debería pasar con query correcto)
                logging.debug(
                    "[SchedulerService] Mensaje antes de tiempo: sm=%s, now=%s, send_at=%s",
                    msg.get('sm_id'), now_ms, send_at_ms
                )
                continue
            
            if now_ms >= window_end:
                # Fuera de ventana: marcar expirado
                logging.warning(
                    "[SchedulerService] Mensaje fuera de ventana: sm=%s, now=%s, send_at=%s",
                    msg.get('sm_id'), now_ms, send_at_ms
                )
                key = {'pk': msg['pk'], 'sk': msg['sk']}
                repo.scheduled_messages.update_conditional(
                    key=key,
                    update_expr='SET #st = :expired, expired_at = :now REMOVE claimed_at',
                    expr_attr_names={'#st': 'status'},
                    expr_attr_values={
                        ':expired': 'expired',
                        ':now': now_ms
                    }
                )
                continue
            
            # 3) Enviar mensaje
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
            
            # 4) Extraer wa_msg_id de la respuesta
            wa_msg_id = 'unknown'
            if result and 'messages' in result and len(result['messages']) > 0:
                wa_msg_id = result['messages'][0].get('id', 'unknown')
            
            # 5) BORRAR el item inmediatamente después del envío exitoso
            repo.scheduled_messages.delete_item(pk=msg['pk'], sk=msg['sk'])
            sent += 1
            
            logging.info(
                "[SchedulerService] ScheduledMessage enviado y borrado: sm=%s, target=%s, wa_msg_id=%s",
                msg.get('sm_id'), target_phone, wa_msg_id
            )
            
        except Exception:
            logging.exception("[SchedulerService] Error enviando ScheduledMessage: %s", msg)
            # Marcar como error
            try:
                key = {'pk': msg['pk'], 'sk': msg['sk']}
                repo.scheduled_messages.update_conditional(
                    key=key,
                    update_expr='SET #st = :error, error_at = :at REMOVE claimed_at',
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
            remind_at_ms = r.get('remind_at_epoch')
            tz_name = r.get('timezone', LOCAL_TZ_NAME)
            
            if not phone or not titulo or not remind_at_ms:
                logging.warning("[SchedulerService] Reminder sin phone, titulo o remind_at_epoch: %s", r)
                continue
            
            # Formatear fecha y hora
            fecha, hora = _format_datetime_for_reminder(remind_at_ms, tz_name)
            
            payload = get_event_reminder_template_input(
                recipient=phone,
                nombre_sesion=titulo,
                fecha=fecha,
                hora=hora
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
        - Usa query_reminders_due() del appointment_repo
        - GSI ApptReminderQueue: PK='pending#scheduled', SK=remind_at_epoch
        - No requiere SKIP LOCKED porque DynamoDB es event-driven
    """
    return repo.appointments.query_reminders_due(now_ms=now_ms, limit=limit)


def _process_appointments(repo: RepositoryProvider, rows: list[dict], now_ms: int) -> tuple[int, int, int]:
    """
    Procesa appointments para enviar recordatorios.
    Valida existencia del evento en Google Calendar antes de enviar.
    Retorna (sent, skipped, canceled_detected).
    
    Notes:
        - Claim atómico: pending -> sending antes de enviar
        - Ventana exacta: solo envía si now_ms está en [remind_at_ms, remind_at_ms + 60s)
        - De-dup intra-tick: usa set con pk#sk para evitar duplicados en mismo barrido
        - CAMBIO CRÍTICO: después de enviar exitosamente, se BORRA el recordatorio del appointment
    """
    from app.services.google_calendar_service import GoogleCalendarService
    from app.config.settings import SETTINGS
    
    sent = 0
    skipped = 0
    canceled_detected = 0
    
    # De-dup intra-tick
    seen_keys = set()
    
    try:
        gcal = GoogleCalendarService()
    except Exception:
        logging.warning("[SchedulerService] Google Calendar no disponible para validar eventos")
        gcal = None
    
    for appt in rows:
        try:
            # 1) De-dup intra-tick
            key_str = f"{appt['pk']}#{appt['sk']}"
            if key_str in seen_keys:
                logging.debug("[SchedulerService] Duplicado intra-tick: %s", key_str)
                continue
            seen_keys.add(key_str)
            
            # 2) Verificar ventana exacta (remind_at_ms <= now_ms < remind_at_ms + 60s)
            remind_at_ms = appt.get('remind_at_epoch')
            if not remind_at_ms:
                logging.warning("[SchedulerService] Appointment sin remind_at_epoch: %s", appt)
                skipped += 1
                continue
            
            window_end = remind_at_ms + (60 * 1000)  # 60 segundos
            
            if now_ms < remind_at_ms:
                # Todavía no es tiempo (no debería pasar con query correcto)
                logging.debug(
                    "[SchedulerService] Reminder antes de tiempo: appt=%s, now=%s, remind_at=%s",
                    appt.get('appointment_id'), now_ms, remind_at_ms
                )
                skipped += 1
                continue
            
            if now_ms >= window_end:
                # Fuera de ventana: expirar
                logging.warning(
                    "[SchedulerService] Reminder fuera de ventana: appt=%s, now=%s, remind_at=%s",
                    appt.get('appointment_id'), now_ms, remind_at_ms
                )
                repo.appointments.expire_reminder(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    now_ms=now_ms
                )
                skipped += 1
                continue
            
            # 3) Claim atómico
            claimed = repo.appointments.claim_reminder(
                pk=appt['pk'],
                sk=appt['sk'],
                now_ms=now_ms
            )
            if not claimed:
                logging.debug(
                    "[SchedulerService] Claim fallido (ya reclamado): appt=%s",
                    appt.get('appointment_id')
                )
                skipped += 1
                continue
            
            # 4) Validar existencia del evento en Google Calendar
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
                        update_expr='SET #st = :canceled, #rs = :skipped, cancel_reason = :reason REMOVE claimed_at',
                        expr_attr_names={'#st': 'status', '#rs': 'reminder_status'},
                        expr_attr_values={
                            ':canceled': 'canceled',
                            ':skipped': 'skipped',
                            ':reason': 'Cancelado externamente (detectado al enviar recordatorio)'
                        }
                    )
                    
                    # Limpiar reminder de la cola (GSI ApptReminderQueue)
                    repo.appointments.delete_reminder(
                        pk=appt['pk'],
                        sk=appt['sk'],
                        now_ms=now_ms
                    )
                    
                    canceled_detected += 1
                    continue
            
            # 5) Obtener customer phone
            customer_phone = appt.get('customer_phone')
            if not customer_phone:
                logging.warning("[SchedulerService] Appointment sin customer_phone: %s", appt)
                repo.appointments.release_claim(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    error_msg='Sin customer_phone'
                )
                skipped += 1
                continue
            
            # 6) Construir mensaje de recordatorio
            nombre_sesion = appt.get('title', 'Sesión')
            starts_at_ms = appt.get('starts_at_epoch')
            tz_name = appt.get('timezone', LOCAL_TZ_NAME)
            
            if not starts_at_ms:
                logging.warning("[SchedulerService] Appointment sin starts_at_epoch: %s", appt)
                repo.appointments.release_claim(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    error_msg='Sin starts_at_epoch'
                )
                skipped += 1
                continue
            
            # Formatear fecha y hora del appointment
            fecha, hora = _format_datetime_for_reminder(starts_at_ms, tz_name)
            
            # 7) Enviar recordatorio
            payload = get_event_reminder_template_input(
                recipient=customer_phone,
                nombre_sesion=nombre_sesion,
                fecha=fecha,
                hora=hora
            )
            result = send_message(payload)
            
            # 8) Extraer wa_msg_id de la respuesta
            wa_msg_id = 'unknown'
            if result and 'messages' in result and len(result['messages']) > 0:
                wa_msg_id = result['messages'][0].get('id', 'unknown')
            
            # 9) BORRAR el recordatorio del appointment inmediatamente después del envío exitoso
            repo.appointments.delete_reminder(
                pk=appt['pk'],
                sk=appt['sk'],
                now_ms=now_ms
            )
            
            sent += 1
            logging.info(
                "[SchedulerService] Recordatorio enviado y borrado: appt=%s, customer=%s, wa_msg_id=%s",
                appt.get('appointment_id'), customer_phone, wa_msg_id
            )
            
        except Exception:
            logging.exception("[SchedulerService] Error procesando Appointment: %s", appt)
            try:
                # Liberar claim para reintentar (o mantener en error)
                repo.appointments.release_claim(
                    pk=appt['pk'],
                    sk=appt['sk'],
                    error_msg='Error al procesar recordatorio'
                )
            except Exception:
                logging.exception("[SchedulerService] Error liberando claim")
    
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
        - De-dup intra-tick para evitar envíos duplicados en mismo barrido
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
            "[SCHEDULER] Completed: scheduled_messages=%s, appointment_reminders=%s, "
            "appointments_skipped=%s, appointments_canceled_detected=%s, standalone_reminders=%s",
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
