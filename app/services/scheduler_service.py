from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import os
from multiprocessing import current_process
from flask import current_app as app
from flask import current_app
from datetime import date, timedelta
from app.services.habit_reports_service import generate_weekly_report, generate_monthly_report
from app.models import Customer, Habito


from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import func

from app.models import db, Reminder, Customer
from app.utils.whatsapp_utils import (
    send_message,
    get_text_message_input,
    get_event_reminder_template_input,
)

# ────────────────────────────────────────────────────────────────────
# Configuración general
# ────────────────────────────────────────────────────────────────────
LOCAL_TZ = ZoneInfo("America/Montevideo")          # ajustá si lo necesitás
scheduler = BackgroundScheduler(timezone=LOCAL_TZ)

__all__ = ["scheduler", "LOCAL_TZ"]

_app: "Flask|None" = None          # se setea en init_scheduler()
_habitos_init = False              # evita llamar init_habitos dos veces
_scheduler_started = False         # garantiza un solo .start()


# ────────────────────────────────────────────────────────────────────
# Helpers
# ────────────────────────────────────────────────────────────────────
def _should_start_scheduler(app) -> bool:
    """
    True si ESTE proceso debe lanzar el scheduler.
    - Evita duplicación con el reloader de Flask.
    - Evita duplicación con los N workers de Gunicorn.
    """
    # 1) Dev-reloader          (flask run --debug)
    if app.debug and os.getenv("WERKZEUG_RUN_MAIN") != "true":
        app.logger.info("[Scheduler] ⏩  Skip – proceso maestro del reloader")
        return False

    # 2) Workers de Gunicorn   (gunicorn --workers N ...)
    worker_id = os.getenv("GUNICORN_WORKER_ID")    # la define Gunicorn ≥21
    if worker_id and worker_id != "0":             # solo el worker 0 lo arranca
        app.logger.info(f"[Scheduler] ⏩  Skip – worker {worker_id}")
        return False

    return True


# ────────────────────────────────────────────────────────────────────
# API pública
# ────────────────────────────────────────────────────────────────────
def init_scheduler(app):
    """
    Llamar una sola vez (desde create_app).  
    - Arranca APScheduler si corresponde.  
    - Hace lazy-load de habitos_services para evitar import circular.
    """
    global _app, _habitos_init, _scheduler_started
    _app = app

    # ── Lanzar scheduler ───────────────────────────────────────────
    if not _scheduler_started and _should_start_scheduler(app):
        scheduler.start(paused=False)
        _scheduler_started = True
        app.logger.info("[Scheduler] ✅  Iniciado en PID %s", current_process().pid)

    # ── Inicializar módulo de hábitos una sola vez ────────────────
    if not _habitos_init:
        from app.services import habitos_services  # import tardío
        habitos_services.init_habitos(app)
        _habitos_init = True


def schedule_event_reminder(
    scheduler,
    reminder_id: int,
    advance: timedelta = timedelta(seconds=30),
):
    """
    Programa dos jobs para un recordatorio:
      1. Recordatorio `advance` antes.
      2. Notificación justo a la hora del recordatorio.
    """
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado. Llama init_scheduler(app)")

    with _app.app_context():
        evento: Reminder | None = (
            db.session.query(Reminder)
            .join(Customer)
            .filter(Reminder.id == reminder_id)
            .first()
        )

        if not evento:
            return

        # ------------------------------------------------------------------
        # 1. Fechas timezone-aware en la zona local
        # ------------------------------------------------------------------
        reminder_dt: datetime = evento.date
        if reminder_dt.tzinfo is None:
            # Si llega naive, la asumimos local
            reminder_dt = reminder_dt.replace(tzinfo=LOCAL_TZ)

        # ------------------------------------------------------------------
        # 3. NOTIFICACIÓN AL INICIO
        # ------------------------------------------------------------------
        notify_dt = reminder_dt
        if notify_dt < datetime.now(LOCAL_TZ):
            notify_dt = datetime.now(LOCAL_TZ) + timedelta(seconds=10)

        def _send_notification(ev_id: int):
            with _app.app_context():
                reminder = (
                    db.session.query(Reminder)
                    .join(Customer)
                    .filter(Reminder.id == ev_id)
                    .first()
                )
                if not reminder:           
                    return

                titulo = f"🚀«{reminder.titulo}» "

                payload = get_event_reminder_template_input(
                    recipient=reminder.customer.phone,
                    titulo=titulo,
                )
                send_message(payload)    
                db.session.delete(reminder)
                db.session.commit()

        notify_job_id = f"notify_event_{reminder_id}"
        scheduler.add_job(
            _send_notification,
            trigger="date",
            run_date=notify_dt,
            args=[reminder_id],
            id=notify_job_id,
            replace_existing=True,
        )


def reschedule_all_reminders(scheduler):
    """Reprograma todo al arrancar la aplicación."""
    if _app is None:
        raise RuntimeError("SchedulerService no iniciado")

    with _app.app_context():
        eventos = (
            db.session.query(Reminder)
            .join(Customer)
            .filter(Reminder.date >= func.now())   # func.now() es UTC → OK
            .all()
        )
        

    for evento in eventos:
        schedule_event_reminder(
            scheduler=scheduler,
            reminder_id=evento.id,
            advance=timedelta(seconds=30),
        )

# ------------------------------------------------------------
# 💬  Programar mensajes a terceros
# ------------------------------------------------------------

def schedule_scheduled_message(scheduler, sm_id: int, send_at):
    """
    Agenda el envío del mensaje programado `sm_id` para la fecha/hora `send_at`.
    """
    job_id = f"scheduled_msg_{sm_id}"

    # Lazy import para evitar ciclos
    from app.services.scheduled_message_service import ScheduledMessageService
    app.logger.info(
        f"[Scheduler] Programando mensaje {sm_id} para {send_at.strftime('%d/%m/%Y %H:%M')}"
    )

    scheduler.add_job(
        ScheduledMessageService.run,
        "date",
        run_date=send_at,
        args=[sm_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=60,
    )
# ------------------------------------------------------------
# 💬  Programar reportes de habitos
# ------------------------------------------------------------
import io
import requests

def _wa_upload(file_bytes: bytes, mime: str) -> str:
    url = f"https://graph.facebook.com/v{current_app.config['GRAPH_API_VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/media"
    headers = {"Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"}
    files = {"file": ("report", io.BytesIO(file_bytes), mime)}
    data = {"messaging_product": "whatsapp"}
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
    resp.raise_for_status()
    return resp.json()["id"]


def send_report_document(wa_phone: str, filename: str, file_bytes: bytes, mime: str):
    media_id = _wa_upload(file_bytes, mime)
    payload = {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": wa_phone,
        "type": "document",
        "document": {
            "id": media_id,
            "filename": filename,
        },
    }
    return send_message(payload)

# Scheduling de envío automático (ejemplos):
# - Semanal: los lunes 08:05 para la semana anterior (hasta el domingo anterior o el día actual).
# - Mensual: el día 1 a las 08:10 para el mes anterior.
def _send_weekly_reports():
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        from app import create_app
        app = create_app()

    with app.app_context():
        ref = date.today()  # o el domingo anterior
        # Solo clientes con hábitos
        q = (Customer.query
            .join(Habito, Habito.customer_id == Customer.id)
            .filter(Habito.activo == True)
            .distinct())
        for c in q.all():
            fname, bts, mime = generate_weekly_report(c.id, ref_day=ref)
            send_report_document(c.phone, fname, bts, mime)

def _send_monthly_reports():
    today = date.today()
    # En el día 1, reportar mes anterior
    prev_month = (today.replace(day=1) - timedelta(days=1))
    y, m = prev_month.year, prev_month.month
    q = (Customer.query
         .join(Habito, Habito.customer_id == Customer.id)
         .filter(Habito.activo == True)
         .distinct())
    for c in q.all():
        fname, bts, mime = generate_monthly_report(c.id, y, m)
        send_report_document(c.phone, fname, bts, mime)

def register_habit_report_jobs():
    """
    Llamar UNA vez en el arranque, luego de scheduler.start().
    Usa replace_existing=True para que, si el proceso se reinicia,
    no se creen duplicados.
    """
    
    # Semanal: lunes 08:05
    scheduler.add_job(
        _send_weekly_reports,
        "cron",
        day_of_week="mon",
        hour=8,
        minute=5,
        timezone=LOCAL_TZ,
        id="habit_reports_weekly",
        replace_existing=True,
    )

    # Mensual: día 1 a las 08:10
    scheduler.add_job(
        _send_monthly_reports,
        "cron",
        day=1,
        hour=8,
        minute=10,
        timezone=LOCAL_TZ,
        id="habit_reports_monthly",
        replace_existing=True,
    )
