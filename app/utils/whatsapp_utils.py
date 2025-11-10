import logging
import json
import requests
import os
import io
from openai import OpenAI
import re
from flask import current_app
from flask import current_app as app
from app.services.bot_logic import BotLogic
import tempfile
from app.services.openai_client import client as openai_client

# Inicializar la lógica de fecha
g_logic = BotLogic()

# --- Lazy helper para evitar circular import ---
def _cancel_reminder_via_orchestrator(recipient: str, context_id: str | None) -> bool:
    """
    Realiza import diferido para no crear ciclos:
    orchestrator <- scheduler_service <- whatsapp_utils (este módulo).
    """
    if not context_id:
        return False
    from app.services.orchestrator import Orchestrator
    from app.db import RepositoryProvider
    
    # Crear repo_provider para pasar al Orchestrator
    repo_provider = RepositoryProvider()
    orch = Orchestrator(openai_client, repo_provider=repo_provider)
    return orch.cancel_reminder(phone=recipient, context_id=context_id)


def get_text_message_input(recipient: str, text: str) -> dict:
    """
    Construye el payload para enviar un mensaje de texto por WhatsApp.
    """
    body = (text or "").strip()
    if not body:
        raise ValueError(
            "[get_text_message_input] Intento de enviar texto vacío a WhatsApp"
        )
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "text",
        "text": {"preview_url": False, "body": text}
    }

# ───────────────────────────────────────────────────────────────
#                         RECORDATORIOS
# ───────────────────────────────────────────────────────────────

def get_recordatorio_template_input(
    recipient: str,
    mensaje: str,
    fecha: str
) -> dict:
    """
    Payload para la plantilla 'recordatorio' aprobada en Meta,
    con Header, Body (2 parámetros) y botón Quick Reply.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "confirmacion_recordatorio",
            "language": {"code": "es_AR"},
            "components": [
               {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": mensaje},  # {{1}}
                        {"type": "text", "text": fecha}      # {{2}}
                    ]               
                },
                {
                    "type": "button",
                    "sub_type": "QUICK_REPLY",
                    "index": 0
                }
            ]
        }
    }

def get_event_reminder_template_input(
    recipient: str,
    nombre_sesion: str,
    fecha: str,
    hora: str
) -> dict:
    """
    Payload para la plantilla 'service' aprobada en Meta.
    Body con 3 parámetros:
    - {{1}}: nombre de la sesión/título
    - {{2}}: fecha
    - {{3}}: hora
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "recordatorio",          
            "language": {"code": "es_AR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre_sesion},  
                        {"type": "text", "text": fecha},          
                        {"type": "text", "text": hora},           
                    ],
                }
            ],
        },
    }

# ───────────────────────────────────────────────────────────────
#                     TERMINOS Y CONDICIONES
# ───────────────────────────────────────────────────────────────
def get_terminos_template_input(recipient: str, nombre: str) -> dict:
    """
    Payload para la plantilla 'terminos_condiciones_v1' aprobada en Meta.
    Incluye Body con 1 parámetro (nombre) y 3 botones: Aceptar, Rechazar y URL.
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "terminos_condiciones_v1",
            "language": {"code": "es_AR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre}  
                    ]
                }
            ]
        }
    }

# ───────────────────────────────────────────────────────────────
#                           Habitos
# ───────────────────────────────────────────────────────────────

def get_habit_reactivation_template_input(recipient: str, nombre_habito: str) -> dict:
    """
    Payload para la plantilla 'reactivacion_habito' aprobada en Meta.
    Incluye Body con 1 parámetro (nombre del hábito).
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "reactivacion_habito", # Nombre de la plantilla
            "language": {"code": "es_AR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre_habito}  
                    ]
                }
            ]
        }
    }


def get_habit_check_template_input(recipient: str, nombre_habito: str, fecha: str) -> dict:
    """
    Payload para la plantilla 'check_habito' aprobada en Meta.
    Incluye Body con 2 parámetros (nombre del hábito y fecha) y 2 botones: "Cumpli" y "No cumpli".
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "servicio_habitos",
            "language": {"code": "es_AR"},
            "components": [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": nombre_habito},  
                        {"type": "text", "text": fecha}
                    ]
                },
                {
                    "type": "button",
                    "sub_type": "QUICK_REPLY",
                    "index": 0,
                    "parameters": [{"type": "payload", "payload": "Cumpli"}]
                },
                {
                    "type": "button",
                    "sub_type": "QUICK_REPLY",
                    "index": 1,
                    "parameters": [{"type": "payload", "payload": "No cumpli"}]
                }
            ]
        }
    }

# ───────────────────────────────────────────────────────────────
#                     MANDAR MENSAJE A X
# ───────────────────────────────────────────────────────────────

def get_comida_template_input(recipient: str, nombre: str, numero: str, texto: str) -> dict:
    """
    Payload para la plantilla *comida* (Spanish-ARG, 3 variables):
        {{1}} → nombre del remitente
        {{2}} → teléfono del remitente
        {{3}} → texto libre a reenviar
    """
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "comida",
            "language": {"code": "es_AR"},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": nombre},
                    {"type": "text", "text": numero},
                    {"type": "text", "text": texto},
                ],
            }],
        },
    }

def get_recordatorio_mensaje_template_input(
    recipient: str,
    texto: str,
    numero: str,
    fecha: str,
) -> dict:
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": recipient,
        "type": "template",
        "template": {
            "name": "recordatorio_mensaje",   # 👈 nombre exacto en Meta
            "language": {"code": "en"},
            "components": [{
                "type": "body",
                "parameters": [
                    {"type": "text", "text": texto},
                    {"type": "text", "text": numero},
                    {"type": "text", "text": fecha},
                ],
            }],
        },
    }

def send_message(payload: dict) -> dict:
    """
    Envía un mensaje a través de la Graph API de WhatsApp y registra la respuesta.
    """
    url = f"https://graph.facebook.com/v{current_app.config['GRAPH_API_VERSION']}/{current_app.config['PHONE_NUMBER_ID']}/messages"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"
    }

    logging.info(f"🚀 [send_message] Payload:\n{payload!r}")
    resp = requests.post(url, json=payload, headers=headers, timeout=(5, 20))
    if resp.status_code != 200:
        logging.error(f"❌ [send_message] Error {resp.status_code}:\n{resp.text}")
    else:
        try:
            body = resp.json()
        except ValueError:
            body = resp.text
        logging.info(f"✅ [send_message] Success, response:\n{body!r}")
    resp.raise_for_status()
    return resp.json()


def process_whatsapp_message(body: dict):
    try:
        msg = body["entry"][0]["changes"][0]["value"]["messages"][0]
        msg_type = msg.get("type")

        # Emisor y contacto
        sender = msg["from"]
        contact = body["entry"][0]["changes"][0]["value"]["contacts"][0]
        recipient = sender  # usamos sender siempre como recipient
        name = contact["profile"]["name"]

        # ────────────────────────────────────────────────────────────────
        # Manejo de botones / quick-replies (API v13-v15 y v16)
        # ────────────────────────────────────────────────────────────────

        if msg_type in ("button", "interactive", "list"):
            # ▸ Extraer payload según la estructura que envía Meta
            if msg_type == "button":  # Cloud API v16
                payload = msg.get("button", {}).get("payload", "").lower()
            elif msg_type == "interactive":  # v13-v15
                payload = (
                    msg.get("interactive", {}).get("button_reply", {}).get("id") or
                    msg.get("interactive", {}).get("list_reply", {}).get("id", "")
                ).lower()
            else:  # msg_type == "list" (v16)
                payload = (
                    msg.get("list", {})
                       .get("single_select", {})
                       .get("selection", {})
                       .get("id", "")
                ).lower()

            # ▸ Cancelar recordatorio
            if payload == "Cancelar":
                cancelled = _cancel_reminder_via_orchestrator(
                    recipient,
                    msg.get("context", {}).get("id", "")
                )
                text = (
                    "Listo, tu recordatorio ha sido cancelado ❌."
                    if cancelled
                    else "⚠️ No había ningún recordatorio activo."
                )
                send_message(get_text_message_input(recipient, text))
                return

        response = None

        # Manejo de Texto
        if msg_type == "text":
            text = msg["text"]["body"]
            if g_logic.validate_date_format(text):
                response = f"✅ La fecha {text.strip()} es válida."
            elif (day := g_logic.get_day_of_date(text)):
                response = day
            elif (until := g_logic.calculate_days_until(text)):
                response = until

            if not response:
                send_message(get_text_message_input(recipient, "⏳ Consultando API de ChatGPT..."))
                response = generate_response(text, sender, name)
                send_message(get_text_message_input(recipient, "✅ Respuesta de ChatGPT recibida"))

            # Normalizar respuesta
            if isinstance(response, dict) and "title" in response and "date" in response:
                # Mantener dict para plantilla
                response = {
                    "mensaje": response["title"],
                    "fecha": response["date"]
                }
            else:
                # Convertir a texto plano
                response = str(response)

        elif msg_type == "audio":
            media_id = msg["audio"]["id"]
            text = transcribe_audio(media_id)

            # Re-usar el MISMO flujo que para texto 👇
            # ✔️ Nuevo flujo según si es reenviado o no
            if msg.get("context", {}).get("forwarded", False):
                send_message(
                    get_text_message_input(
                        recipient,
                        text                      # enviamos solo la transcripción
                    )
                )
                return 
            else:
                 # 2️⃣ Audio grabado por el usuario → procesar normalmente
                 if g_logic.validate_date_format(text):
                     response = f"✅ La fecha {text.strip()} es válida."
                 elif (day := g_logic.get_day_of_date(text)):
                     response = day
                 elif (until := g_logic.calculate_days_until(text)):
                     response = until
                 else:
                     send_message(
                         get_text_message_input(
                             recipient,
                             "⏳ Transcribiendo y consultando ChatGPT..."
                         )
                     )
                     response = generate_response(text, sender, name)
        else:
            response = "⚠️ Solo entiendo texto y audio actualmente."

        # Montaje final del payload
        # ──────────────────────────────────────────────────────────────
        # Evitar enviar mensajes vacíos (previene error 400)
        # ──────────────────────────────────────────────────────────────
        if not response or (isinstance(response, str) and not response.strip()):
            return        
        if isinstance(response, dict) and "mensaje" in response and "fecha" in response:
            payload = get_recordatorio_template_input(
                recipient,
                mensaje=response["mensaje"],
                fecha=response["fecha"]
            )
        else:
            payload = get_text_message_input(recipient, response)
        send_message(payload)
    except Exception:
        logging.exception("Error in process_whatsapp_message")

# -----------------------------------------------------------
#  AUDIO HELPERS
# -----------------------------------------------------------

def _get_media_meta(media_id: str) -> tuple[str, int]:
    """Devuelve (url, tamaño_en_bytes) para un media-id de WhatsApp."""
    api_v  = current_app.config["GRAPH_API_VERSION"]
    token  = current_app.config["ACCESS_TOKEN"]
    r = requests.get(
        f"https://graph.facebook.com/v{api_v}/{media_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=10,
    )
    r.raise_for_status()
    meta = r.json()
    return meta["url"], int(meta.get("file_size", 0))


def download_media(media_id: str) -> bytes:
    """Descarga los bytes crudos del media-id."""
    url, _ = _get_media_meta(media_id)
    r = requests.get(
        url,
        headers={"Authorization": f"Bearer {current_app.config['ACCESS_TOKEN']}"},
        timeout=20,
    )
    r.raise_for_status()
    return r.content


def transcribe_audio(media_id: str) -> str:
    """
    Descarga un audio de WhatsApp y lo transcribe con Whisper.
    Límite OpenAI: 25 MB.
    """
    url, size = _get_media_meta(media_id)
    if size > 25 * 1024 * 1024:
        return "Lo siento, el audio es demasiado grande para procesarlo (límite 25 MB)."

    audio_bytes = download_media(media_id)
    with tempfile.NamedTemporaryFile(suffix=".ogg") as tmp:
        tmp.write(audio_bytes)
        tmp.flush()
        transcript = openai_client.audio.transcriptions.create(
            model="whisper-1",
            file=open(tmp.name, "rb"),
            response_format="text",
        )
    return transcript.strip()


def is_valid_whatsapp_message(body: dict) -> bool:
    """
    Devuelve **True** si el webhook corresponde al campo «messages».
    Traza internamente los valores para depuración.
    """
    try:
        change = body["entry"][0]["changes"][0]
        field  = change.get("field")
        value  = change.get("value", {})
        msgs   = value.get("messages", [])

        return field == "messages" and isinstance(msgs, list) and len(msgs) > 0
    except (KeyError, IndexError, TypeError):
        app.logger.exception("[VALIDACIÓN] Error analizando estructura del webhook")
        return False
    
def send_comida_template(recipient: str,
                         nombre: str,
                         numero: str,
                         texto: str) -> dict:
    """
    Wrapper que construye el payload de la plantilla «comida»
    y reutiliza la función `send_message`.
    """
    payload = get_comida_template_input(recipient, nombre, numero, texto)
    return send_message(payload)


# Utilidad ─ Normalizar teléfonos al formato E.164
# Ej.: "598 99 394 422" → "+59899394422"
def normalize_phone_number(raw: str) -> str:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if not digits:
        raise ValueError("Número de teléfono inválido.")
    return f"+{digits}"

def send_recordatorio_mensaje(
    recipient: str,
    texto: str,
    numero: str,
    fecha: str,
) -> dict:
    payload = get_recordatorio_mensaje_template_input(
        recipient, texto, numero, fecha
    )
    return send_message(payload)