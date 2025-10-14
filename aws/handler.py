import os, json, logging, base64, time, sys, io
from datetime import datetime, timezone, timedelta

# ── Secrets primero (robusto a opcionales) ─────────────────────
from app.config.secrets_loader import load_into_env
load_into_env()

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

# ── Deps app ───────────────────────────────────────────────────
import requests
from flask import Flask
from sqlalchemy.pool import NullPool

from app.models import db, Turn, Reminder, ScheduledMessage, Customer
from app.services.threads_service import ensure_thread
from app.utils.phone_utils import normalize_phone_e164
from uuid import uuid4
from app.services.openai_service import client as openai_client 
from app.services.orchestrator import Orchestrator
from app.services import scheduler_service
from app.utils.whatsapp_utils import (
    get_text_message_input,
    get_recordatorio_template_input,
    send_message,
    transcribe_audio,
)
from app.config.settings import SETTINGS

# ── Bootstrapping ───────────────────────────────────────────────
APP = Flask(__name__)

# DB_URL: corrige el "or" mal puesto (Render / Supabase)
DB_URL = (
    os.getenv("DATABASE_URL")
    or os.getenv("SUPABASE_DB_URL")
    or os.getenv("SUPABASE_URL")
)
if not DB_URL:
    raise RuntimeError("Falta DATABASE_URL / SUPABASE_DB_URL en variables/env/secrets.")

# Normaliza el dialecto para SQLAlchemy + psycopg (v3) eliminar a futuro, duplicado con el secrets loader
if DB_URL.startswith("postgres://"):
    DB_URL = "postgresql+psycopg://" + DB_URL[len("postgres://"):]
else:
    DB_URL = DB_URL.replace("postgresql://", "postgresql+psycopg://", 1)

APP.config["SQLALCHEMY_DATABASE_URI"] = DB_URL
APP.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
# Lambda + Render: evitar pools persistentes
APP.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"poolclass": NullPool}

# Config WhatsApp / Tokens
APP.config["GRAPH_API_VERSION"] = os.getenv("GRAPH_API_VERSION", "v23.0").lstrip("v")
APP.config["PHONE_NUMBER_ID"]   = os.getenv("PHONE_NUMBER_ID")
APP.config["ACCESS_TOKEN"]      = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN                    = os.getenv("WHATSAPP_VERIFY_TOKEN") or os.getenv("VERIFY_TOKEN")
LOCAL_TZ                        = os.getenv("TZ", "America/Montevideo")

if not APP.config["PHONE_NUMBER_ID"] or not APP.config["ACCESS_TOKEN"]:
    raise RuntimeError("Faltan PHONE_NUMBER_ID y/o WHATSAPP_ACCESS_TOKEN.")

db.init_app(APP)
APP.app_context().push()

scheduler_service.init_scheduler(APP)

logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger(__name__)

# Orchestrator intacto
orchestrator = Orchestrator(openai_client, db_session=db.session)

# Constantes/params
STALE_MINUTES = int(os.getenv("WEBHOOK_STALE_MINUTES", "3"))
SIMULATE_TYPING_MS = int(os.getenv("SIMULATE_TYPING_MS", "0"))  # 0 = off
GRAPH_VER = f"v{APP.config['GRAPH_API_VERSION']}"
ACCESS_TOKEN = APP.config["ACCESS_TOKEN"]

# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _is_eventbridge(evt: dict) -> bool:
    """Verdadero si la invocación viene de EventBridge Scheduler."""
    return (
        evt.get("source") == "aws.events"
        or evt.get("detail-type") == "scheduled-job"
        or evt.get("agentMode") == "job"
   )

def _msg_already_processed(wamid: str) -> bool:
    if not wamid:
        return False
    return db.session.query(db.exists().where(Turn.wa_msg_id == wamid)).scalar()

def _parse_http_meta(event):
    """Normaliza evento de API GW v2 / v1."""
    ctx = event.get("requestContext", {})
    http = ctx.get("http", {})
    method = http.get("method") or event.get("httpMethod", "POST")
    path = http.get("path") or event.get("rawPath", "")
    qs = event.get("queryStringParameters") or {}
    body = event.get("body")
    if event.get("isBase64Encoded"):
        body = base64.b64decode(body or b"").decode("utf-8") if body else ""
    return method, path, qs, body

def indicate_typing(incoming_wamid: str, phone_number_id: str | None = None) -> None:
    """
    Marca como leído y (si la API lo admite) insinúa 'typing'.
    Cloud API oficialmente no soporta typing real; mantenemos este stub por compatibilidad.
    """
    phone_number_id = phone_number_id or APP.config["PHONE_NUMBER_ID"]
    if not phone_number_id or not ACCESS_TOKEN:
        return
    url = f"https://graph.facebook.com/{GRAPH_VER}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": incoming_wamid,
        # 'typing_indicator' no está documentado; lo dejamos si tu cuenta lo tolera.
        "typing_indicator": {"type": "text"}
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    try:
        r = requests.post(url, json=payload, headers=headers, timeout=10)
        if r.status_code >= 400:
            logging.warning("[typing] %s :: url=%s :: body=%s", r.status_code, url, r.text)
    except Exception:
        logging.exception("[typing] fallo")

def _simulate_typing_delay():
    if SIMULATE_TYPING_MS > 0:
        time.sleep(min(SIMULATE_TYPING_MS, 600) / 1000.0)

def _extract_button_payload(msg_type: str, msg_obj: dict) -> tuple[str, str]:
    """
    Devuelve (payload_lower, context_id) para distintos formatos:
    - v16: type='button' → button.payload
    - v13-15: type='interactive' → interactive.button_reply.id / list_reply.id
    - v16: type='list' → list.single_select.selection.id
    """
    context_id = (msg_obj.get("context") or {}).get("id", "")
    payload = ""

    if msg_type == "button":
        payload = msg_obj.get("button", {}).get("payload", "")

    elif msg_type == "interactive":
        payload = (
            msg_obj.get("interactive", {}).get("button_reply", {}).get("id")
            or msg_obj.get("interactive", {}).get("list_reply", {}).get("id")
            or ""
        )

    elif msg_type == "list":
        payload = (
            msg_obj.get("list", {})
                 .get("single_select", {})
                 .get("selection", {})
                 .get("id", "")
        )

    return (payload.lower(), context_id)

def _handle_button_action(payload_lower: str, wa_id: str, context_id: str):
    """
    Ejecuta acciones de botones conocidas: cancelar recordatorio, T&C, cancelar mensaje.
    (Hábitos eliminados.)
    """
    # 1) Cancelar recordatorio
    if payload_lower == "cancelar":
        logging.info("🔕 Solicitud de cancelar recordatorio (ctx=%s)", context_id)
        try:
            cancelled = orchestrator.cancel_reminder(phone_id=wa_id, context_id=context_id)
        except Exception:
            logging.exception("cancel_reminder() error")
            send_message(get_text_message_input(wa_id, "❌ Error interno al cancelar."))
            return
        txt = "Listo, tu recordatorio ha sido cancelado ❌." if cancelled else "⚠️ No había ningún recordatorio activo."
        send_message(get_text_message_input(wa_id, txt))
        return

    # 2) Cancelar mensaje programado
    if payload_lower == "no enviar":
        logging.info("🛑 Solicitud de cancelar mensaje programado (ctx=%s)", context_id)
        try:
            cancelled = orchestrator.cancel_scheduled_message(phone_id=wa_id, context_id=context_id)
        except Exception:
            logging.exception("cancel_scheduled_message() error")
            send_message(get_text_message_input(wa_id, "❌ Error interno al cancelar."))
            return
        txt = "Listo, tu mensaje programado no se enviará ❌." if cancelled else "⚠️ No había ningún mensaje pendiente."
        send_message(get_text_message_input(wa_id, txt))
        return

    # Otros botones se ignoran
    logging.info("[buttons] payload no mapeado: %r", payload_lower)

def _extract_value(data: dict) -> tuple[dict, dict, dict] | None:
    """
    Devuelve (value, contact, msg_obj) o None si no hay 'contacts'/'messages'.
    """
    try:
        value = data["entry"][0]["changes"][0]["value"]
        if "contacts" not in value or "messages" not in value:
            return None
        contact = value["contacts"][0]
        msg_obj  = value["messages"][0]
        return value, contact, msg_obj
    except Exception:
        return None

# ─────────────────────────────────────────────────────────────────────────────
# Lambda handler
# ─────────────────────────────────────────────────────────────────────────────

def lambda_handler(event, context):
    # Correlation-ID (propagar a logs y llamadas salientes)
    headers = (event.get("headers") or {})
    correlation_id = (
        headers.get("x-correlation-id")
        or headers.get("x-amzn-trace-id")
        or str(uuid4())
    )

    # 0) Invocación de EventBridge → correr jobs pendientes
    if _is_eventbridge(event):
        out = scheduler_service.run_due_jobs()
        return {
            "statusCode": 200,
            "headers": {"content-type": "application/json"},
            "body": json.dumps(out, ensure_ascii=False)
        }

    method, path, qs, raw_body = _parse_http_meta(event)

    # 1) GET /webhook → verificación (Meta)
    if method == "GET":
        if (qs.get("hub.verify_token") == VERIFY_TOKEN) and qs.get("hub.challenge"):
            return {"statusCode": 200, "body": qs["hub.challenge"]}
        return {"statusCode": 403, "body": "Invalid verify token"}

    # 2) POST /webhook
    try:
        body = json.loads(raw_body or "{}")
    except Exception:
        logging.exception("[WEBHOOK] body inválido", extra={"correlation_id": correlation_id})
        return {"statusCode": 200, "body": "ok"}

    result = _extract_value(body)
    if not result:
        logging.info(
            "[WEBHOOK] Callback sin contacts/messages (statuses u otros)",
            extra={"correlation_id": correlation_id}
        )
        return {"statusCode": 200, "body": "ok"}

    value, contact, msg_obj = result
    msg_type = msg_obj.get("type", "")
    wa_id    = contact.get("wa_id")
    name     = contact.get("profile", {}).get("name", "Desconocido")
    phone_number_id = value["metadata"]["phone_number_id"]
    wamid    = msg_obj.get("id")

    # 2.1) Freshness + idempotencia
    try:
        ts_utc = datetime.fromtimestamp(int(msg_obj.get("timestamp", "0")), tz=timezone.utc)
    except Exception:
        ts_utc = datetime.now(timezone.utc)

    if datetime.now(timezone.utc) - ts_utc > timedelta(minutes=STALE_MINUTES):
        logging.info(
            "[WEBHOOK] Mensaje %s ignorado por antigüedad (%s UTC)",
            wamid, ts_utc.isoformat(timespec='seconds'),
            extra={"correlation_id": correlation_id, "wa_id": wa_id}
        )
        return {"statusCode": 200, "body": "stale"}

    if _msg_already_processed(wamid):
        logging.info(
            "[WEBHOOK] Mensaje ya procesado %s",
            wamid,
            extra={"correlation_id": correlation_id, "wa_id": wa_id}
        )
        return {"statusCode": 200, "body": "ok"}

    # 2.2) UX: marcar leído + typing (opcional delay leve)
    try:
        indicate_typing(wamid, phone_number_id)
    except Exception:
        pass
    _simulate_typing_delay()

    # 2.3) Gestión de botones
    if msg_type in ("button", "interactive", "list"):
        payload_lower, context_id = _extract_button_payload(msg_type, msg_obj)
        logging.info(
            "🔘 Button payload = %r (ctx=%s)",
            payload_lower, context_id,
            extra={"correlation_id": correlation_id, "wa_id": wa_id, "wamid": wamid}
        )
        _handle_button_action(payload_lower, wa_id, context_id)
        return {"statusCode": 200, "body": "ok"}

    # 2.4) Audio → transcribir (con tu helper) y usar como user_msg
    if msg_type == "audio":
        try:
            media_id = msg_obj["audio"]["id"]
            text = transcribe_audio(media_id)
            # Si es audio reenviado, sólo devolvemos la transcripción al usuario
            if msg_obj.get("context", {}).get("forwarded", False):
                send_message(get_text_message_input(wa_id, text))
                return {"statusCode": 200, "body": "ok"}
            user_msg = text
        except Exception:
            logging.exception(
                "[audio] fallo transcripción; se sigue con cadena vacía",
                extra={"correlation_id": correlation_id, "wa_id": wa_id, "wamid": wamid}
            )
            user_msg = ""
    else:
        user_msg = msg_obj.get("text", {}).get("body", "").strip()

    # 2.5) Asegurar thread activo (idempotente/seguro ante carreras)
    try:
        _ = ensure_thread(
            wa_phone_raw=wa_id,
            customer_id=None,  # si lo tenés resuelto antes, pasalo aquí
            correlation_id=correlation_id,
            last_wa_msg_id=wamid
        )
        logging.info(
            "[WEBHOOK] ensure_thread OK",
            extra={"correlation_id": correlation_id, "wa_id": wa_id, "wamid": wamid}
        )
    except Exception:
        # Hardening: no tiramos 5xx al webhook; log y seguimos (el orchestrator también reintenta)
        logging.exception(
            "[WEBHOOK] ensure_thread falló (se continúa para no interrumpir el flujo)",
            extra={"correlation_id": correlation_id, "wa_id": wa_id, "wamid": wamid}
        )

    # 2.6) Orchestrator → respuesta
    bot_reply = orchestrator.handle_message(user_msg or "", wa_id, name, wamid)

    # 2.6) Construcción de payload de salida
    payload = None
    if isinstance(bot_reply, dict) and "reminder_id" in bot_reply:
        title = bot_reply.get("title") or bot_reply.get("mensaje") or ""
        date_str = bot_reply.get("date") or bot_reply.get("fecha") or ""
        payload = get_recordatorio_template_input(wa_id, mensaje=title, fecha=date_str)

    elif isinstance(bot_reply, dict) and "event_id" in bot_reply:
        # No se envía un WhatsApp extra; sólo confirmamos HTTP 200
        return {"statusCode": 200, "body": json.dumps({})}

    elif isinstance(bot_reply, dict) and "message" in bot_reply:
        payload = get_text_message_input(wa_id, str(bot_reply["message"]))

    elif isinstance(bot_reply, str):
        if bot_reply.strip():
            payload = get_text_message_input(wa_id, bot_reply.strip())
        else:
            logging.warning("[WEBHOOK] bot_reply vacío; no se envía mensaje")
            return {"statusCode": 200, "body": "ok"}

    else:
        logging.warning("[WEBHOOK] bot_reply tipo inesperado: %r", type(bot_reply))
        return {"statusCode": 200, "body": "ok"}

    # 2.7) Enviar mensaje (y persistir wa_msg_id si era recordatorio)
    if payload:
        try:
            resp = send_message(payload)
            if isinstance(bot_reply, dict) and "reminder_id" in bot_reply:
                try:
                    card_id = resp["messages"][0]["id"]
                    evento = db.session.get(Reminder, bot_reply["reminder_id"])
                    if evento:
                        evento.wa_msg_id = card_id
                        db.session.commit()
                except Exception:
                    logging.exception("[WEBHOOK] no se pudo persistir card_id")
        except requests.HTTPError:
            logging.exception("[send_message] HTTP error; se responde 200 igual")

    return {"statusCode": 200, "body": "ok"}
