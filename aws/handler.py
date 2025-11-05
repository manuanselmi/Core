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

from app.utils.phone_utils import normalize_phone_e164
from uuid import uuid4
from app.db import RepositoryProvider
from app.services.openai_client import client as openai_client
from app.services.orchestrator import Orchestrator
from app.services import scheduler_service
from app.services.memory_service import Memory
from app.utils.whatsapp_utils import (
    get_text_message_input,
    get_recordatorio_template_input,
    send_message,
    transcribe_audio,
)
from app.config.settings import SETTINGS

# ── Bootstrapping ───────────────────────────────────────────────
APP = Flask(__name__)

# Load settings into Flask config
from app.config.settings import SETTINGS
for key, value in SETTINGS.__dict__.items():
    APP.config[key] = value

# Config WhatsApp / Tokens
APP.config["GRAPH_API_VERSION"] = os.getenv("GRAPH_API_VERSION", "v23.0").lstrip("v")
APP.config["PHONE_NUMBER_ID"]   = os.getenv("PHONE_NUMBER_ID")
APP.config["ACCESS_TOKEN"]      = os.getenv("WHATSAPP_ACCESS_TOKEN") or os.getenv("ACCESS_TOKEN")
VERIFY_TOKEN                    = os.getenv("WHATSAPP_VERIFY_TOKEN") or os.getenv("VERIFY_TOKEN")
LOCAL_TZ                        = os.getenv("TZ", "America/Montevideo")

if not APP.config["PHONE_NUMBER_ID"] or not APP.config["ACCESS_TOKEN"]:
    raise RuntimeError("Faltan PHONE_NUMBER_ID y/o WHATSAPP_ACCESS_TOKEN.")

# Inicializar scheduler (no requiere DB SQL)
scheduler_service.init_scheduler(APP)

# Configuración de logging: solo INFO para app, ERROR para librerías
logging.getLogger().setLevel(logging.INFO)

# Silenciar logs verbosos de librerías externas
logging.getLogger('botocore').setLevel(logging.ERROR)
logging.getLogger('boto3').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)
logging.getLogger('httpx').setLevel(logging.ERROR)
logging.getLogger('httpcore').setLevel(logging.ERROR)
logging.getLogger('openai').setLevel(logging.WARNING)
logging.getLogger('googleapiclient').setLevel(logging.ERROR)
logging.getLogger('google').setLevel(logging.ERROR)

# Configurar loggers de la app en INFO
for logger_name in ['GoogleCalendarService', 'Orchestrator', '__main__']:
    logger = logging.getLogger(logger_name)
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.INFO)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)

logger = logging.getLogger(__name__)

# Orchestrator global (será reinicializado con repo_provider en cada request)
# Por ahora creamos una instancia por defecto
default_repo_provider = RepositoryProvider()
orchestrator = Orchestrator(openai_client, repo_provider=default_repo_provider)

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

def _handle_scheduler_jobs(repo_provider: RepositoryProvider, correlation_id: str) -> dict:
    """
    Procesa jobs programados: mensajes pendientes y recordatorios de citas.
    Branch exclusivo para EventBridge cuando USE_DYNAMO=1.
    """
    from app.utils.whatsapp_utils import get_text_message_input, get_event_reminder_template_input
    
    logger.info("[CID=%s] [SCHEDULER] Processing jobs", correlation_id)
    now_ms = int(time.time() * 1000)
    
    sent_messages = 0
    sent_reminders = 0
    
    # 1) Procesar mensajes programados
    try:
        candidates = repo_provider.scheduled_messages.claim_pending_batch(now_ms, n=25)
        
        for msg in candidates:
            try:
                target_phone = msg.get("target_phone")
                text = msg.get("text")
                pk = msg.get("pk")
                sk = msg.get("sk")
                
                payload = get_text_message_input(target_phone, text)
                resp = send_message(payload)
                wa_msg_id = resp.get("messages", [{}])[0].get("id")
                
                # BORRAR el mensaje inmediatamente después del envío exitoso
                repo_provider.scheduled_messages.delete_item(pk, sk)
                sent_messages += 1
                
                logger.info(
                    "[CID=%s] [SCHEDULER] ScheduledMessage enviado y borrado: pk=%s sk=%s wa_msg_id=%s",
                    correlation_id, pk, sk, wa_msg_id
                )
            except Exception:
                logger.exception("[CID=%s] [SCHEDULER] Error sending scheduled message", correlation_id)
    except Exception:
        logger.exception("[CID=%s] [SCHEDULER] Error claiming scheduled messages", correlation_id)
    
    # 2) Procesar recordatorios de citas
    try:
        reminders = repo_provider.appointments.query_reminders_due(now_ms)
        
        for appt in reminders:
            try:
                # Validación defensiva: skipear items sin campos requeridos
                phone = appt.get("customer_phone")
                title = appt.get("title")
                starts_at_ms = appt.get("starts_at_epoch")
                timezone_name = appt.get("timezone", LOCAL_TZ)
                pk = appt.get("pk")
                sk = appt.get("sk")
                
                if not all([phone, title, starts_at_ms, pk, sk]):
                    logger.warning(
                        "[CID=%s] [SCHEDULER] Skipping invalid reminder: phone=%s title=%s starts_at=%s pk=%s sk=%s",
                        correlation_id, phone, bool(title), starts_at_ms, pk, sk
                    )
                    continue
                
                # Formatear fecha y hora
                from zoneinfo import ZoneInfo
                tz = ZoneInfo(timezone_name)
                dt = datetime.fromtimestamp(starts_at_ms / 1000, tz=tz)
                fecha = dt.strftime("%d/%m/%Y")  # DD/MM/YYYY
                hora = dt.strftime("%H:%M")      # HH:MM
                
                payload = get_event_reminder_template_input(
                    recipient=phone,
                    nombre_sesion=title,
                    fecha=fecha,
                    hora=hora
                )
                send_message(payload)
                sent_reminders += 1
                
                # BORRAR el recordatorio inmediatamente después del envío exitoso
                repo_provider.appointments.delete_reminder(pk, sk, now_ms)
                
                logger.info(
                    "[CID=%s] [SCHEDULER] Recordatorio enviado y borrado: pk=%s sk=%s customer=%s",
                    correlation_id, pk, sk, phone
                )
            except Exception:
                logger.exception("[CID=%s] [SCHEDULER] Error sending appointment reminder", correlation_id)
    except Exception:
        logger.exception("[CID=%s] [SCHEDULER] Error querying appointment reminders", correlation_id)
    
    result = {
        "scheduled_messages_sent": sent_messages,
        "appointment_reminders_sent": sent_reminders,
        "timestamp": now_ms,
    }
    
    logger.info("[CID=%s] [SCHEDULER] Completed: %d messages, %d reminders", correlation_id, sent_messages, sent_reminders)
    return {
        "statusCode": 200,
        "headers": {"content-type": "application/json"},
        "body": json.dumps(result, ensure_ascii=False)
    }

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
        try:
            cancelled = orchestrator.cancel_scheduled_message(phone_id=wa_id, context_id=context_id)
        except Exception:
            logging.exception("cancel_scheduled_message() error")
            send_message(get_text_message_input(wa_id, "❌ Error interno al cancelar."))
            return
        txt = "Listo, tu mensaje programado no se enviará ❌." if cancelled else "⚠️ No había ningún mensaje pendiente."
        send_message(get_text_message_input(wa_id, txt))
        return

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
    
    # Inicializar app context con correlation_id
    with APP.app_context():
        APP.correlation_id = correlation_id
        
        try:
            # Crear repo_provider con correlation_id
            repo_provider = RepositoryProvider(correlation_id_provider=lambda: correlation_id)

            # 0) Invocación de EventBridge → correr jobs pendientes
            if _is_eventbridge(event):
                return _handle_scheduler_jobs(repo_provider, correlation_id)

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
            
            # Obtener timeEpoch para staleness guard
            request_ctx = event.get("requestContext", {})
            time_epoch_ms = request_ctx.get("timeEpoch")
            if not time_epoch_ms:
                # Fallback: usar timestamp del mensaje
                try:
                    time_epoch_ms = int(msg_obj.get("timestamp", "0")) * 1000
                except Exception:
                    time_epoch_ms = int(time.time() * 1000)

            # 2.1) STALENESS GUARD: Rechazar eventos antiguos
            now_ms = int(time.time() * 1000)
            stale_threshold_ms = STALE_MINUTES * 60 * 1000
            
            if time_epoch_ms < (now_ms - stale_threshold_ms):
                return {"statusCode": 200, "body": "stale"}

            # 2.2) IDEMPOTENCIA TEMPRANA: Verificar si ya procesamos este wa_msg_id
            existing_turn = repo_provider.turns.get_by_wa_msg_id(wamid)
            if existing_turn:
                logger.info("[CID=%s] [IDEMPOTENT] wa_msg_id=%s", correlation_id, wamid)
                return {"statusCode": 200, "body": "ok"}

            # 2.3) UX: marcar leído + typing (opcional delay leve)
            try:
                indicate_typing(wamid, phone_number_id)
            except Exception:
                pass
            _simulate_typing_delay()

            # 2.4) Gestión de botones
            if msg_type in ("button", "interactive", "list"):
                payload_lower, context_id = _extract_button_payload(msg_type, msg_obj)
                
                # Persistir turn con contenido de botón
                button_content = f"[button:{payload_lower}]"
                repo_provider.turns.append(
                    phone=wa_id,
                    conversation_id=wa_id,  # En Dynamo, conversation_id = phone
                    wa_msg_id=wamid,
                    ts_ms=time_epoch_ms,
                    payload={"role": "user", "content": button_content}
                )
                
                _handle_button_action(payload_lower, wa_id, context_id)
                return {"statusCode": 200, "body": "ok"}

            # 2.5) Audio → transcribir (con tu helper) y usar como user_msg
            if msg_type == "audio":
                try:
                    media_id = msg_obj["audio"]["id"]
                    text = transcribe_audio(media_id)
                    # Si es audio reenviado, sólo devolvemos la transcripción al usuario
                    if msg_obj.get("context", {}).get("forwarded", False):
                        audio_content = f"[audio_forwarded]: {text}"
                        # Persistir turn
                        repo_provider.turns.append(
                            phone=wa_id,
                            conversation_id=wa_id,
                            wa_msg_id=wamid,
                            ts_ms=time_epoch_ms,
                            payload={"role": "user", "content": audio_content}
                        )
                        
                        send_message(get_text_message_input(wa_id, text))
                        return {"statusCode": 200, "body": "ok"}
                    user_msg = text
                except Exception:
                    logging.exception(
                        "[CID=%s] [audio] fallo transcripción; se sigue con cadena vacía",
                        correlation_id
                    )
                    user_msg = ""
            else:
                user_msg = msg_obj.get("text", {}).get("body", "").strip()

            # 2.5b) Persistir turn con contenido final
            if user_msg:
                # Guardar turn en DynamoDB
                repo_provider.turns.append(
                    phone=wa_id,
                    conversation_id=wa_id,  # En Dynamo, conversation_id = phone
                    wa_msg_id=wamid,
                    ts_ms=time_epoch_ms,
                    payload={"role": "user", "content": user_msg}
                )

            # 2.6) Orchestrator → respuesta
            # La idempotencia está garantizada por el guardado temprano del turn
            # Pasar repo_provider al orchestrator
            # TODO: Modificar orchestrator para recibir repo_provider (próxima iteración)
            bot_reply = orchestrator.handle_message(user_msg or "", wa_id, name, wamid)

            # 2.7) Construcción de payload de salida
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

            # 2.8) Enviar mensaje (y persistir wa_msg_id si era recordatorio)
            if payload:
                try:
                    resp = send_message(payload)
                    if isinstance(bot_reply, dict) and "reminder_id" in bot_reply:
                        try:
                            card_id = resp["messages"][0]["id"]
                            # Actualizar wa_msg_id en DynamoDB
                            repo_provider.reminders.update_wa_msg_id_by_reminder_id(
                                phone=wa_id,
                                reminder_id=bot_reply["reminder_id"],
                                wa_msg_id=card_id
                            )
                        except Exception:
                            logging.exception("[CID=%s] [WEBHOOK] no se pudo persistir card_id", correlation_id)
                except requests.HTTPError:
                    logging.exception("[send_message] HTTP error; se responde 200 igual")

            return {"statusCode": 200, "body": "ok"}
        
        finally:
            # Limpiar correlation_id del contexto
            if hasattr(APP, 'correlation_id'):
                delattr(APP, 'correlation_id')
