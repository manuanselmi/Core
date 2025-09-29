import json
import os

from flask import Blueprint, request, make_response, current_app, jsonify

app = current_app
import logging
import requests
from app.models import db, Reminder, Turn
from app.services.openai_service import client as openai_client
from app.utils.whatsapp_media import get_media_url, download_media
from app.services.pdf_summary import extract_text_from_bytes, chunk_text
from app.services.orchestrator import Orchestrator

from app.utils.whatsapp_utils import (
    get_text_message_input,
    get_recordatorio_template_input,
    send_message,
    transcribe_audio
)
router = Blueprint("webhook", __name__)
orchestrator = Orchestrator(openai_client, db_session=db.session)
webhook_bp = router

def generate_response(message_body, wa_id, name, idms=None):
    return orchestrator.handle_message(message_body, wa_id, name, idms)

def _msg_already_processed(idms: str) -> bool:
    """
    Devuelve True si el idms ya está registrado en la tabla Turn.
    Usa EXISTS() → consulta muy rápida.
    """
    return db.session.query(
        db.exists().where(Turn.wa_msg_id == idms)
    ).scalar()
    
import os, requests

_raw_ver = os.getenv("GRAPH_API_VERSION", "v23.0")
GRAPH_VER = _raw_ver if _raw_ver.startswith("v") else f"v{_raw_ver}"

ACCESS_TOKEN = os.getenv("ACCESS_TOKEN")         
PHONE_NUMBER_ID = os.getenv("PHONE_NUMBER_ID")    

def indicate_typing(incoming_wamid: str, phone_number_id: str | None = None) -> None:
    phone_number_id = phone_number_id or PHONE_NUMBER_ID
    if not phone_number_id:
        raise RuntimeError("phone_number_id vacío (del webhook o ENV).")
    if not ACCESS_TOKEN:
        raise RuntimeError("ACCESS_TOKEN (de WhatsApp) no configurado.")

    url = f"https://graph.facebook.com/{GRAPH_VER}/{phone_number_id}/messages"
    payload = {
        "messaging_product": "whatsapp",
        "status": "read",
        "message_id": incoming_wamid,
        "typing_indicator": {"type": "text"}
    }
    headers = {"Authorization": f"Bearer {ACCESS_TOKEN}"}
    r = requests.post(url, json=payload, headers=headers, timeout=10)

    if r.status_code >= 400:
        current_app.logger.warning(
            "[typing] %s :: url=%s :: body=%s",
            r.status_code, url, r.text
        )
    r.raise_for_status()

@router.route("/webhook", methods=["GET", "POST"])
def webhook():
    if request.method == "GET":
        # verificación de Meta
        if request.args.get("hub.verify_token") == os.getenv("VERIFY_TOKEN"):
            return request.args.get("hub.challenge"), 200
        return "Invalid verify token", 403

    # --- POST (mensaje entrante) ---
    data = request.get_json()
    
    app.logger.info("[WEBHOOK] Datos recibidos: %s", json.dumps(data, indent=2, ensure_ascii=False))

    try:
        value    = data["entry"][0]["changes"][0]["value"]
        # si NO viene "contacts", es otro callback (statuses, etc.)
        if "contacts" not in value:
            app.logger.info("[WEBHOOK] Sin contacts; callback ignorado")
            return "ok", 200

        contact  = value["contacts"][0]
        phone_id = contact["wa_id"]                        # ← número destino (to)
        msg_obj  = value["messages"][0]
        msg_type = msg_obj.get("type")
        idms     = msg_obj.get("id")                
        
        # 𐄂  Traza del mensaje bruto  ─────────────────────────────────────
        app.logger.debug("📦 msg_obj:\n%s",
                         json.dumps(msg_obj, indent=2, ensure_ascii=False))
        
        from datetime import datetime, timezone, timedelta
        STALE_MINUTES = 3

        ts_utc = datetime.fromtimestamp(
            int(msg_obj.get("timestamp", "0")),
            tz=timezone.utc
        )
        if datetime.now(tz=timezone.utc) - ts_utc > timedelta(minutes=STALE_MINUTES):
            app.logger.info(
                "[WEBHOOK] Mensaje %s ignorado por antigüedad (%s UTC)",
                msg_obj.get("id"), ts_utc.isoformat(timespec='seconds')
            )
            return "stale", 200
        if _msg_already_processed(idms):
            app.logger.info("[WEBHOOK] Mensaje %s ya procesado previamente", idms)
            return "ok", 200

        phone_number_id = value["metadata"]["phone_number_id"] 
        wamid = value["messages"][0]["id"]

        try:
            indicate_typing(wamid, phone_number_id)          
        except Exception as e:
            current_app.logger.warning(f"[typing] fallo: {e}")

        # ──────────────────────────────────────────────────────────────
        # 1️⃣  BOTONES / QUICK-REPLIES (v13-15 y v16)
        # ──────────────────────────────────────────────────────────────
        if msg_type in ("button", "interactive", "list"):

            # a) Cloud API v16 --------------  button.payload
            if msg_type == "button":
                payload = msg_obj.get("button", {}).get("payload", "")

            # b) Cloud API v13-15 ------------  interactive.button_reply.id / list_reply.id
            elif msg_type == "interactive":
                payload = (
                    msg_obj.get("interactive", {})
                          .get("button_reply", {})
                          .get("id")
                          
                    or
                    msg_obj.get("interactive", {})
                          .get("list_reply", {})
                          .get("id")
                    or ""
                )
              
            # c) Cloud API v16 --------------  list.single_select.selection.id
            else:  # msg_type == "list"
                payload = (
                    msg_obj.get("list", {})
                          .get("single_select", {})
                          .get("selection", {})
                          .get("id", "")
                )

            payload = payload.lower()
            context_id = msg_obj.get("context", {}).get("id", "")
            app.logger.info("🔘 Button payload = %r", payload)

            if payload == "cancelar":
                app.logger.info("🔕 Solicitud de cancelar recordatorio(ctx=%s)", context_id)
                try:
                    cancelled = orchestrator.cancel_reminder(
                        phone_id=phone_id,
                        context_id=context_id
                    )
                except Exception:
                    app.logger.error("❌ cancel_event() lanzó excepción:",
                                    exc_info=True)
                    send_message(
                        get_text_message_input(phone_id,
                                            "❌ Error interno al cancelar"))
                    return "ok", 200   
                text = ("Listo, tu recordatorio ha sido cancelado ❌."
                    if cancelled
                    else "⚠️ No había ningún recordatorio activo.")
                send_message(get_text_message_input(phone_id, text))
                return "ok", 200
            
            # 2) Aceptar T&C
            if payload == "aceptar":
                from app.services.terms_service import mark_accepted
                mark_accepted(phone_id)
                send_message(get_text_message_input(
                    phone_id,
                    "¡Gracias! Ya aceptaste los Términos y Condiciones. "
                    "Podés usar el asistente con normalidad."
                ))
                # ------ Enviar mensaje con lista de funcionalidades ------
                funcionalidades = (
                    "Aquí tienes una lista completa de mis funcionalidades:\n"
                    "1. *Recordatorios*: Puedo crear recordatorios puntuales para que no olvides tareas o eventos importantes.\n"
                    "2. *Hábitos diarios*: Puedo ayudarte a establecer y gestionar hábitos diarios.\n"
                    "3. *Consultar el clima*: Puedo darte el pronóstico del clima para diferentes ubicaciones.\n"
                    "4. *Reuniones con Kairo Agency*: Puedo ayudarte a gestionar tus reuniones con Kairo Agency.\n"
                    "5. *Listas de compras*: Puedo generar listas de compras según tus necesidades.\n"
                    "6. *Enviar mensajes a terceros*: Puedo enviar mensajes a través de WhatsApp a los contactos que necesites.\n"
                    "7. *Resumir PDFs*: Si tienes documentos en PDF, puedo ayudarte a resumirlos.\n"
                    "8. *Transcribir audios*: Puedo convertir audio en texto.\n"
                    "9. *Reportar errores*: Si encuentras un problema, puedo informar a los creadores sobre ello.\n"
                    "Si necesitas ayuda con alguna de estas funciones, no dudes en decírmelo. ¡Estoy aquí para ayudarte!"
                )
                send_message(get_text_message_input(phone_id, funcionalidades))
                return "ok", 200

            # 3) Rechazar T&C (opcional)
            if payload == "rechazar":
                send_message(get_text_message_input(
                    phone_id,
                    "Entendido. No podrás usar el asistente hasta aceptar los T&C."
                ))
                return "ok", 200
                
            # 4) Botones de hábitos
            if payload in {"cumpli", "no cumpli"}:
                from app.services.habitos_services import handle_button
                app.logger.info(
                    "[WEBHOOK] Botón de hábito recibido entro al if: %s", payload)
                if handle_button(phone_id, payload, context_id):
                    return "ok", 200
                else:
                    app.logger.warning(
                        "[WEBHOOK] Botón de hábito no gestionado: %s", payload)
                    return "ok", 200
                
            # 5) Botones de cancelar msg programado
            if payload == "no enviar":
                app.logger.info("🛑 Solicitud de cancelar mensaje programado (ctx=%s)", context_id)
                try:
                    cancelled = orchestrator.cancel_scheduled_message(
                                    phone_id=phone_id,
                                    context_id=context_id)
                except Exception:
                    app.logger.exception("❌ cancel_scheduled_message() error")
                    send_message(get_text_message_input(
                        phone_id, "❌ Error interno al cancelar"))
                    return "ok", 200

                txt = ("Listo, tu mensaje programado no se enviará ❌."
                    if cancelled else "⚠️ No había ningún mensaje pendiente.")
                send_message(get_text_message_input(phone_id, txt))
                return "ok", 200

            # Otros botones se ignoran (o añádelos aquí…)
            return "ok", 200          
        if msg_type == "document" and msg_obj["document"]["mime_type"] == "application/pdf":
            try:
                media_id = msg_obj["document"]["id"]
                media_url = get_media_url(media_id)
                pdf_bytes = download_media(media_url)

                # 1️⃣  Extraer texto
                pdf_text = extract_text_from_bytes(pdf_bytes)
                chunks = chunk_text(pdf_text)

                # 2️⃣  Resumir con tu cliente OpenAI ya configurado
                partials = [
                    orchestrator.client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{
                            "role": "user",
                            "content": "Resume en un párrafo claro:\n\n" + c
                        }],
                    ).choices[0].message.content.strip()
                    for c in chunks
                ]
                final_summary = orchestrator.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": (
                            "Fusiona estos resúmenes en un único texto "
                            "de máximo 6 frases, en español:\n\n" + "\n\n".join(partials)
                        )
                    }],
                ).choices[0].message.content.strip()
                
                current_app.logger.debug("[WEBHOOK] Resumen final derecho en routes:\n%s", final_summary)
                # 3️⃣  Envía el resumen al usuario (usa tu helper send_message)
                send_message(get_text_message_input(phone_id, final_summary))
                return make_response("PDF resumido", 200)
            except Exception as exc:
                app.logger.exception("[WEBHOOK] error al resumir PDF", exc_info=True)
                send_message(
                    get_text_message_input(phone_id, "Lo siento, no pude resumir tu PDF."))
                return make_response("error", 200)

        #-------------------------------------------
        # -----------mensaje de audio---------------
        # ------------------------------------------        

        if msg_type == "audio":
            media_id = msg_obj["audio"]["id"]
            text = transcribe_audio(media_id)
            # 🎯 1️⃣ — Audio reenviado: solo transcripción y salida temprana
            if msg_obj.get("context", {}).get("forwarded", False):
                app.logger.debug("[ROUTE] Audio reenviado → solo transcripción")
                send_message(get_text_message_input(phone_id, text))
                return "ok", 200
            user_msg = text
            # 2️⃣  Audio grabado por el usuario → flujo normal
            user_msg = text
            
            
        else:
            user_msg = msg_obj.get("text", {}).get("body", "")
        name    = contact.get("profile", {}).get("name", "Desconocido")

    except Exception:
        app.logger.error("[WEBHOOK] Error inesperado:",
                            exc_info=True)
        return "unsupported format", 400

    # -------------------------------------------------
    # NUEVA LÓGICA DE RESPUESTA SEGÚN el SUGERIDO
    # -------------------------------------------------

    bot_reply = orchestrator.handle_message(user_msg, phone_id, name, idms)

    payload = None

    # Caso create_reminder
    if isinstance(bot_reply, dict) and "reminder_id" in bot_reply:
        title = bot_reply.get("title") or bot_reply.get("mensaje") or ""
        date_str = bot_reply.get("date") or bot_reply.get("fecha") or ""
        payload = get_recordatorio_template_input(
            phone_id,
            mensaje=title,
            fecha=date_str
        )

    # Caso evento agendado
    elif isinstance(bot_reply, dict) and "event_id" in bot_reply:
        # Respuesta rápida de confirmación HTTP 200 OK vacío sin enviar mensaje adicional a WhatsApp
        return jsonify({}), 200
    # Caso dict con mensaje plano
    elif isinstance(bot_reply, dict) and "message" in bot_reply:
        payload = get_text_message_input(phone_id, str(bot_reply["message"]))

    # Caso texto normal
    elif isinstance(bot_reply, str):
        if not bot_reply.strip():
            app.logger.warning("[WEBHOOK] bot_reply está vacío, no se enviará mensaje")
            return "ok", 200
        payload = get_text_message_input(phone_id, bot_reply.strip())

    # Tipo inesperado
    else:
        app.logger.warning("[WEBHOOK] bot_reply tipo inesperado: %r", type(bot_reply))
        return "ok", 200

    # Envía mensaje si corresponde
    try:
        resp = send_message(payload)
        app.logger.debug("[send_message] Raw resp ⇢ %s", json.dumps(resp, indent=2, ensure_ascii=False))
    except requests.HTTPError as e:
        app.logger.error(f"[send_message] {e.response.status_code} {e.response.text}")
        # Responder 200 para que WhatsApp no reintente
        return "ok", 200

    # Si era recordatorio, persistir wa_msg_id del botón
    if isinstance(bot_reply, dict) and "reminder_id" in bot_reply:
        try:
            card_id = resp["messages"][0]["id"]
            app.logger.debug("[WEBHOOK] card_id extraído ⇢ %s", card_id)
            evento = db.session.get(Reminder, bot_reply["reminder_id"])
            if evento:
                evento.wa_msg_id = card_id
                db.session.commit()
                app.logger.debug("[WEBHOOK] Evento %s ➜ wa_msg_id actualizado", evento.id)
            else:
                app.logger.warning("[WEBHOOK] ¡Evento %s no encontrado en DB!", bot_reply["reminder_id"])
        except Exception as e:
            app.logger.exception("[WEBHOOK] No se pudo persistir wa_msg_id del card: %s", e)

    return "ok", 200

