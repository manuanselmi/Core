"""
Servicio de seguimiento de hábitos.
Mantiene todo el ciclo de vida: planificar recordatorios diarios,
registrar respuestas, aplicar reglas de inactividad y reactivación.
"""
from datetime import datetime, date, time as dtime, timedelta

from app.services.scheduler_service import scheduler, LOCAL_TZ
from app.models import Habito, RegistroHabito, db, Customer
from app.utils.whatsapp_utils import get_text_message_input, send_message
from flask import current_app 

# ───────── Helpers ─────────────────────────────────────────────
_app = None   

def init_habitos(app):
    """Registra el job diario y guarda el app para los contextos."""
    global _app
    _app = app

    # se crea / sustituye el job de la 20:30pm
    scheduler.add_job(
        plan_daily_jobs,
        trigger="cron", 
        hour=20, minute=30,
        id="habits_midnight_planner",
        replace_existing=True,
    )
# ───────── Tareas programadas de alto nivel ────────────────────

def get_or_create_registro(habito: Habito, fecha: date):
    reg = RegistroHabito.query.filter_by(habito_id=habito.id, fecha=fecha).first()
    if reg:
        return reg, False
    reg = RegistroHabito(habito_id=habito.id, fecha=fecha, 
                         estado="pendiente", mensaje_id=None,
                         hora_envio=None, hora_respuesta=None)
    db.session.add(reg)
    db.session.commit()
    return reg, True

def plan_daily_jobs():
    """Se ejecuta cada día a las 00:00. Agenda los recordatorios del día."""
    today = date.today()
    with _app.app_context():
        for hb in Habito.query.filter_by(activo=True).all():
            reg, created = get_or_create_registro(
                habito=hb,
                fecha=today
            )
            send_time = dtime(hour=hb.recordatorio_horas, minute=0, second=0)
            run_dt = datetime.combine(today, send_time, tzinfo=LOCAL_TZ)

            job_id = f"habit_prompt_{hb.id}_{today.isoformat()}"
            scheduler.add_job(
                func=_send_habit_prompt,
                trigger="date",
                run_date=run_dt,
                args=[reg.id],
                id=job_id,
                replace_existing=True,
            )

            hb.fecha_ultimo_envio = today
        db.session.commit()

# ─── nuevo reschedule_all_habit_prompts() ───────────────────────
def reschedule_all_habit_prompts():
    today = date.today()
    now   = datetime.now(tz=LOCAL_TZ)

    with _app.app_context():
        q = (RegistroHabito.query.filter(RegistroHabito.estado == "pendiente",RegistroHabito.fecha >= today).all())
        for reg in q:
            hb = reg.habito
            run_dt = datetime.combine(
                reg.fecha,
                dtime(hour=hb.recordatorio_horas, minute=0, second=0),
                tzinfo=LOCAL_TZ
            )
            if run_dt <= now:
                # prompt “perdido” → agenda sólo el follow-up
                _follow_up_if_no_response(reg.id)
                continue

            job_id = f"habit_prompt_{hb.id}_{reg.fecha.isoformat()}"
            scheduler.add_job(
                _send_habit_prompt,
                "date",
                run_date=run_dt,
                args=[reg.id],
                id=job_id,
                replace_existing=True,
            )


def _send_habit_prompt(registro_id: str):
    """Envío inicial + programación follow-up."""
    with _app.app_context():
        reg: RegistroHabito = db.session.get(RegistroHabito, registro_id)
        if not reg:
            return

        # 1. Enviar plantilla interactiva ------------------------------------------------
        from app.utils.whatsapp_utils import get_habit_check_template_input, send_message

        payload = get_habit_check_template_input(
            recipient=reg.habito.customer.phone,
            nombre_habito=reg.habito.nombre,
            fecha=reg.fecha.strftime("%d/%m/%Y"),
        )
        resp = send_message(payload)               # ← devuelve {"messages":[{"id":…}]}
        msg_id = resp["messages"][0]["id"]
        reg.mensaje_id = msg_id
        reg.hora_envio = datetime.now(LOCAL_TZ)
        db.session.commit()
        
        # 2. Programar follow-up en 1 h --------------------------------------------------
        fu_id = f"habit_followup_{reg.id}"
        scheduler.add_job(
            func=_follow_up_if_no_response,
            trigger="date",
            run_date= datetime.now(LOCAL_TZ) + timedelta(hours=1),
            args=[reg.id],
            id=fu_id,
            replace_existing=True,
        )

def _follow_up_if_no_response(registro_id: str):
    """Si el registro sigue en 'pendiente' tras 1 h, marca no cumplido y chequea inactividad."""
    with _app.app_context():
        reg: RegistroHabito = db.session.get(RegistroHabito, registro_id)
        if not reg or reg.estado != "pendiente":
            return

        reg.estado = "no_cumplido"
        reg.hora_respuesta = None
        hb = reg.habito
        hb.dias_inactivos += 1
        db.session.commit()

        # Si acumula 3 días sin respuesta → desactivar y planificar reactivación
        if hb.dias_inactivos >= 3:
            hb.activo = False
            db.session.commit()

            react_dt = datetime.now(LOCAL_TZ) + timedelta(days=7)
            scheduler.add_job(
                func=_reactivation_prompt,
                trigger="date",
                run_date=react_dt,
                args=[hb.id],
                id=f"habit_reactivate_{hb.id}",
                replace_existing=True,
            )

def _reactivation_prompt(habito_id: str):
    """Envía el mensaje de “¿Querés retomarlo?”."""
    with _app.app_context():
        hb: Habito = db.session.get(Habito, habito_id)
        if not hb or hb.activo:
            return

        from app.utils.whatsapp_utils import get_habit_reactivation_template_input, send_message

        payload = get_habit_reactivation_template_input(
            recipient=hb.customer.phone, nombre_habito=hb.nombre
        )
        send_message(payload)
    

# ───────────── Helpers comunes a botones y texto ───────────────
def _finalizar_registro(reg: RegistroHabito, cumplio: bool):
    """Marca el registro como cumplido / no cumplido y cancela follow-up."""
    reg.estado = "cumplido" if cumplio else "no_cumplido"
    reg.hora_respuesta = datetime.now(LOCAL_TZ)
    hb = reg.habito
    hb.dias_inactivos = 0 
    db.session.commit()

    # elimina el job de follow-up si estaba pendiente
    fu_id = f"habit_followup_{reg.id}"
    if scheduler.get_job(fu_id):
        scheduler.remove_job(fu_id)

    # confirmación al usuario (opcional – ajusta el texto a tu gusto)
    txt = "¡Genial! ✅ Hábito registrado como cumplido." if cumplio \
        else "Entendido. ❌ Hoy no cumpliste el hábito."
    send_message(get_text_message_input(hb.customer.phone, txt))
    

# ───────────── Entrada por BOTONES (interactive) ───────────────
def handle_button(phone: str, payload: str, context_id: str | None) -> bool:
    """
    Procesa los payloads Cumpli / No cumpli.
    Devuelve True si el botón era de hábitos y se gestionó.
    """
    if payload not in {"cumpli", "no cumpli"}:
        return False

    customer: Customer | None = Customer.query.filter_by(phone=phone).first()
    if not customer or not context_id:
        return False

    # Localizar el registro pendiente vinculado a ese msg_id
    reg = RegistroHabito.query.filter_by(
        mensaje_id=context_id,
        estado="pendiente",
    ).first()
    if not reg:
        return False

    _finalizar_registro(reg, payload == "cumpli")
    return True

