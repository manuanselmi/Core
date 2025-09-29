import re
import json
import os
import logging
import time
from datetime import timedelta, datetime, date
from uuid import uuid4
from sqlalchemy.exc import IntegrityError
from zoneinfo import ZoneInfo

import dateparser 

from openai import OpenAI
from flask import current_app 

from requests import Session

from app.services.bot_logic import BotLogic
from app.services.customer_service import CustomerService
from app.services.calendar_service import CalendarService
import app.services.scheduler_service as scheduler_service
from app.utils.datetime_utils import parse_iso8601
from app.services.weather_service import get_forecast
from app.services.memory_service import Memory
from app.utils.datetime_normalizer import normalize_to_future
import datetime as dt
from app.models import db, Reminder, Customer, Habito, RegistroHabito, ScheduledMessage
import psycopg2
from psycopg2.extras import RealDictCursor
from app.services.google_calendar_service import CALENDAR_ID, GoogleCalendarService
from app.services.send_message_flow import SendMessageFlow
from app.services.pdf_summary import extract_pdf_text, chunk_text
from app.services.openai_service import client, _thread_for, ASSISTANT_ID
from zoneinfo import ZoneInfo
import os

LOCAL_TZ_STR = os.getenv("TZ", "America/Montevideo")

def log_http_response(response):
    logging.info(f"Status: {response.status_code}")
    logging.info(f"Content-type: {response.headers.get('content-type')}")
    logging.info(f"Body: {response.text}")

class Orchestrator:
    def __init__(self, client: OpenAI, db_session: Session, catalog_path: str = "functions_catalog.json"):
        """
        Orchestrator que centraliza lógica local (botlogic) y function-calling.
        """
        self.client = client
        self.logic = BotLogic()
        self.db_session = db_session
        self.calendar_api = GoogleCalendarService()
        # Carga catálogo de funciones para el modelo (legacy)
        with open(catalog_path, encoding="utf-8") as f:
            self.functions = json.load(f)
        # FSM para “enviar mensaje a otra persona”
        self.send_msg_flow = SendMessageFlow()

    def handle_message(self, message: str, phone: str, name: str, idms: str | None = None) -> str | dict:
        # 0) Registramos o buscamos usuario automático
        user = CustomerService.find_or_create(phone, name)
        self.current_customer_id = user.get("id")
        self.current_phone = phone
        self.current_name = name

        # PDF -> resumen (legacy)
        if re.search(r"\.pdf($|\\?)", message.lower()):
            try:
                pdf_text = extract_pdf_text(message.strip())
                chunks = chunk_text(pdf_text)
                partials = [
                    self.client.chat.completions.create(
                        model="gpt-4o-mini",
                        messages=[{
                            "role": "user",
                            "content": "Resume en un párrafo claro:\n\n" + c
                        }],
                        max_tokens=400,
                    ).choices[0].message.content.strip()
                    for c in chunks
                ]
                final = self.client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[{
                        "role": "user",
                        "content": "Fusiona estos resúmenes en máx. 6 frases:\n\n" + "\n\n".join(partials)
                    }],
                    max_tokens=300,
                ).choices[0].message.content.strip()
                return final
            except Exception as e:
                current_app.logger.error("[Orchestrator] PDF error: %s", e)
                return "Lo siento, no pude resumir el documento PDF."
        
        # ─── Gatekeeper de T&C ──────────────────────────────────────────
        if not user.get("accept_terms", False):
            from app.services.terms_service import request_terms_acceptance
            request_terms_acceptance(user)
            return            
        # ────────────────────────────────────────────────────────────────

        # ─── Flujo «enviar mensaje a tercero» ───────────────────────────
        flow_resp = self.send_msg_flow.handle(
            message, phone, name, customer_id=self.current_customer_id
        )
        if flow_resp is not None:
            return flow_resp
        # ────────────────────────────────────────────────────────────────

        # 1) Preprocesamiento local usando botlogic
        if self.logic.validate_date_format(message):
            if re.search(r"\b(día cae|qué día)\b", message, re.IGNORECASE):
                return self.logic.get_day_of_date(message)
            return self.logic.calculate_days_until(message)

        # 2) Delegar al Assistant (tools)
        assistant_answer = self._assistant_reply(phone, message, user_name=name)

        # 3) Persistir turno en memoria (legacy)
        Memory.save_turn(phone, "user", message, wa_msg_id=idms)
        if isinstance(assistant_answer, str):
            Memory.save_turn(phone, "assistant", assistant_answer)
            if Memory.should_summarize(phone):
                Memory.summarize(phone, self.client)

        return assistant_answer

    # ---------------- Dispatcher para tools ---------------------
    def _execute_function(self, tool_name: str, **kwargs):
        """
        Dispatcher para tool calls del Assistant.
        - Mapea cada tool del catálogo a un método del Orchestrator.
        - Inyecta defaults y sanea args mínimos cuando corresponde.
        - Mantiene compatibilidad con funciones ya existentes (fallback getattr).
        """

        # 🔧 Inyecciones/saneos por tool (no crean coupling con el LLM)
        if tool_name == "notify_creators":
            kwargs.setdefault(
                "sender_name",
                getattr(self, "current_user_name", None) or getattr(self, "current_phone", None)
            )

        elif tool_name == "schedule_meeting":
            kwargs.setdefault("duration_minutes", 60)

        elif tool_name == "enviar_mensaje":
            if not kwargs.get("fecha_hora"):
                kwargs["fecha_hora"] = None

        elif tool_name == "lookup_customer":
            cid = kwargs.get("customer_id")
            if cid is not None:
                try:
                    kwargs["customer_id"] = int(cid)
                except Exception:
                    pass

        elif tool_name == "create_habit":
            rh = kwargs.get("recordatorio_horas")
            if rh is not None:
                try:
                    kwargs["recordatorio_horas"] = int(rh)
                except Exception:
                    pass

        dispatch = {
            # Calendario / Agenda
            "create_reminder":       self.create_reminder,
            "check_availability": getattr(self, "check_availability", None),
            "schedule_meeting":   getattr(self, "schedule_meeting", None),

            # Clima
            "get_weather":        getattr(self, "get_weather", None),

            # Clientes
            "lookup_customer":    getattr(self, "lookup_customer", None),

            # Supermercado
            "create_grocery_list": getattr(self, "create_grocery_list", None),

            # Notificaciones a creadores
            "notify_creators":    getattr(self, "notify_creators", None),

            # Hábitos
            "create_habit":       getattr(self, "create_habit", None),
            "delete_habit":       getattr(self, "delete_habit", None),
            "list_habits":        getattr(self, "list_habits", None),
            "reactivate_habit":   getattr(self, "reactivate_habit", None),

            # Mensajería a terceros
            "enviar_mensaje":     getattr(self, "enviar_mensaje", None),

            # Documentos
            "summarize_pdf":      getattr(self, "summarize_pdf", None),
        }

        fn = dispatch.get(tool_name) or getattr(self, tool_name, None)
        if not callable(fn):
            raise ValueError(f"Tool desconocida o no callable: {tool_name}")

        return fn(**kwargs)



    # ---------------- Helper para extraer texto de mensajes ------------
    def _unwrap_message(self, m) -> str:
        try:
            for part in getattr(m, "content", []) or []:
                t = getattr(part, "type", None)
                if t == "text" and hasattr(part, "text"):
                    return part.text.value
                if t == "refusal" and hasattr(part, "refusal"):
                    # por si el assistant devuelve una negativa estructurada
                    return part.refusal
            # fallback mínimo
            if hasattr(m, "content") and isinstance(m.content, list) and m.content:
                # intentar value plano si existe
                c0 = m.content[0]
                if hasattr(c0, "text") and hasattr(c0.text, "value"):
                    return c0.text.value
        except Exception:
            pass
        return "Listo."

    # ---------------- Core: integrar con Assistants API ----------------
    def _assistant_reply(self, wa_id: str, user_msg: str, user_name: str | None = None) -> str | dict:
        """
        Envía el mensaje al Assistant y devuelve la respuesta
        (o dict si una tool-call ya maneja la salida final).-
        """
        thread_id = _thread_for(wa_id)
        
        # 0) Si hay un run activo, esperar a que finalice o requiera acción
        last_runs = client.beta.threads.runs.list(thread_id=thread_id, limit=1).data
        if last_runs and last_runs[0].status in ("queued", "in_progress", "requires_action"):
            run = last_runs[0]
            while run.status in ("queued", "in_progress"):
                time.sleep(0.7)
                run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)
            if run.status == "requires_action":
                # dejamos que el flujo de abajo maneje la acción
                pass
        
        LOCAL_TZ = ZoneInfo("America/Montevideo")   
        extra_instr = None
        if user_name:
            extra_instr = (
                f"Soy Kairo, agente virtual de Kairo Agency en WhatsApp. "
                f"Si preguntan '¿quién sos?' → respondé siempre 'Soy Kairo, el agente de Kairo Agency'. "
                f"Mis funcionalidades son: recordatorios puntuales, hábitos diarios, clima, disponibilidad y reuniones, listas de compras, enviar mensajes a terceros, resumir PDFs, transcribir audios y reportar algun error a los creadores. Los detalles están en tus system instructions. "
                f"Usá la tool correcta según corresponda, una sola por turno. "
                f"Perfil del usuario: se llama '{user_name}'. "
                f"Si el usuario pregunta '¿cómo me llamo?' o similar, respondé '{user_name}'. "
                f"Podés saludar o referirte a él por ese nombre cuando tenga sentido. "
                f"La fecha y hora actual es {datetime.now(LOCAL_TZ).strftime('%d/%m/%Y %H:%M')} "
                f"y el día de hoy es {datetime.now(LOCAL_TZ).strftime('%A')}."
            )

        # 1) Guardar el turno del usuario
        client.beta.threads.messages.create(
            thread_id=thread_id,
            role="user",
            content=user_msg
        )

        # 2) Lanzar el run con instrucciones efímeras (acá sí)
        # Check if there's an active run for the thread
        last_runs = client.beta.threads.runs.list(thread_id=thread_id, limit=1).data
        if last_runs and last_runs[0].status in ("queued", "in_progress", "requires_action"):
            current_app.logger.info("[Orchestrator] Active run detected: %s", last_runs[0].id)
            run = last_runs[0]
        else:
            # Create a new run if no active run exists
            run_args = { "thread_id": thread_id, "assistant_id": ASSISTANT_ID }

            if extra_instr:
                run_args["instructions"] = extra_instr
            run = client.beta.threads.runs.create(**run_args)
            current_app.logger.info("[Orchestrator] Run started: %s", run.id)

        # 3) Loop de polling con manejo de tools
        last_tool_result = None  # sólo lo usamos para create_reminder → routes manda plantilla
        while True:
            run = client.beta.threads.runs.retrieve(thread_id=thread_id, run_id=run.id)

            # a) Si requiere tools
            if run.status == "requires_action":
                tool_outputs = []
                tool_calls = run.required_action.submit_tool_outputs.tool_calls

                for call in tool_calls:
                    tool_name = call.function.name
                    try:
                        args = json.loads(call.function.arguments or "{}")
                    except Exception as e:
                        args = {}
                        current_app.logger.exception("[Run %s] JSON args inválidos para %s: %s", run.id, tool_name, e)

                    current_app.logger.debug("[Run %s] tool_call -> %s(%s)", run.id, tool_name, args)

                    # Normalización sólo para create_reminder (preferir fecha del Assistant)
                    if tool_name == "create_reminder":
                        local_tz = ZoneInfo(LOCAL_TZ_STR)
                        now_local = datetime.now(local_tz).replace(second=0, microsecond=0)

                        raw_date = args.get("date")
                        dt_assistant = None
                        dt_final = None

                        # 1) Intentar usar la fecha que propuso el Assistant
                        if raw_date:
                            try:
                                dt_assistant = parse_iso8601(raw_date)
                                if dt_assistant.tzinfo is None:
                                    dt_assistant = dt_assistant.replace(tzinfo=local_tz)
                                else:
                                    dt_assistant = dt_assistant.astimezone(local_tz)
                            except Exception:
                                dt_assistant = None

                        if dt_assistant and dt_assistant > now_local:
                            dt_final = dt_assistant
                        else:
                            # 2) Fallback: reparsear desde el texto (si el Assistant no trajo fecha válida o quedó pasada)
                            dt_from_text = normalize_to_future(user_msg, tz_str=LOCAL_TZ_STR, strict=True)
                            if dt_from_text:
                                if dt_from_text.tzinfo is None:
                                    dt_from_text = dt_from_text.replace(tzinfo=local_tz)
                                if dt_from_text > now_local:
                                    dt_final = dt_from_text

                        # 3) Si seguimos sin fecha válida, devolvemos error para que el assistant pida precisión
                        if dt_final is None:
                            tool_outputs.append({
                                "tool_call_id": call.id,
                                "output": json.dumps({
                                    "ok": False,
                                    "error": "TIME_NOT_UNDERSTOOD",
                                    "hint": "No pude inferir una fecha/hora válida (quedó vacía o en el pasado). Pedí hora exacta."
                                }, ensure_ascii=False)
                            })
                            continue
                        # 4) Formateo final (local)
                        args["date"] = dt_final.strftime("%Y-%m-%d %H:%M")
                        current_app.logger.debug(
                            "[create_reminder] assistant_date=%s → final_date=%s (now=%s)",
                            raw_date, args["date"], now_local.strftime("%Y-%m-%d %H:%M")
                        )

                    try:
                        result = self._execute_function(tool_name, **args)
                        if tool_name == "create_reminder":
                            last_tool_result = result
                        tool_outputs.append({
                            "tool_call_id": call.id,
                            "output": json.dumps(result, default=str),
                        })
                    except Exception as e:
                        current_app.logger.exception("[Run %s] error ejecutando %s", run.id, tool_name)
                        tool_outputs.append({
                            "tool_call_id": call.id,
                            "output": json.dumps({"error": str(e)}, default=str),
                        })

                # 👉 ENTREGAMOS outputs y seguimos poll-eando; NO devolvemos mensaje acá
                client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )

                # Si la tool fue create_reminder, devolvemos su dict para que routes mande la plantilla.
                if last_tool_result is not None:
                    return last_tool_result

                time.sleep(0.6)
                continue

            # b) Si sigue procesando, esperar
            if run.status in ("queued", "in_progress"):
                time.sleep(0.7)
                continue

            # c) Estado terminal → recién ahora leemos la respuesta final
            break

        # 4) Run terminado: tomar el último mensaje del assistant (texto final)
        # last = client.beta.threads.messages.list(thread_id=thread_id, limit=1).data[0]
        # return last.content[0].text.value
        return self._last_assistant_text(thread_id)
    
    def _last_assistant_text(self, thread_id: str) -> str | None:
        msgs = client.beta.threads.messages.list(thread_id=thread_id, limit=20).data
        for m in msgs:  # viene en orden desc por defecto
            if getattr(m, "role", None) == "assistant":
                for part in getattr(m, "content", []) or []:
                    if getattr(part, "type", "") == "text":
                        return part.text.value
        return None

    
    # ------------------------------------------------------------------
    #   Métodos/funciones/tools expuestos al LLM
    # ------------------------------------------------------------------    
    
    def get_weather(self, location: str, date: str | None = None) -> dict:
            """send_message
            Devuelve el pronóstico.  Si `date` viene None, asume la fecha de hoy
            (a las 12:00 hora local Montevideo) para evitar crashear.
            """
            try:
                if date:
                    target = datetime.strptime(date, "%Y-%m-%d").date()
                else:
                    target = datetime.now(ZoneInfo("America/Montevideo")).date()

                return get_forecast(location, target)   # tu wrapper a la API
            except Exception as exc:
                return {"error": str(exc)}
            
    # ------------------------------------------------------------------
    # 📅  Recordatorios / Reminder
    # ------------------------------------------------------------------
    
    def create_reminder(self, date: str, title: str, wa_msg_id: str | None = None) -> dict:
            print(f"[DEBUG] Orchestrator.reminder → customer_id={self.current_customer_id}, date={date}, title={title}, wa_msg_id={wa_msg_id}")
            # 1) Guarda en la BD, pasando primero el customer_id
            cs = CalendarService()
            reminder_id = cs.create(
                self.current_customer_id,  # <- customer_id
                date,                      # <- fecha como string "YYYY-MM-DD HH:MM:SS"
                title,
                wa_msg_id
            )

            # 2) Parsear la fecha a datetime
            reminder_dt = parse_iso8601(date)

            # 3) Programar los jobs: recordatorio y notificación al inicio
            advance = current_app.config.get("EVENT_ADVANCE", timedelta(minutes=1))

            scheduler_service.schedule_event_reminder(
                scheduler_service.scheduler,  # instancia de APScheduler
                reminder_id,
                advance=advance
            )

            # Devolvemos también title y date para templates
            date_str = reminder_dt.strftime("%Y-%m-%d %H:%M")
            return {"reminder_id": reminder_id, "date": date_str, "title": title, "wa_msg_id": wa_msg_id}

    def lookup_customer(self, customer_id: int) -> dict:
            customer = CustomerService.get(customer_id)
            return {"customer": customer}
        
        
    def cancel_reminder(self,
                        phone_id: str,
                        context_id: str | None = None,
                        wa_msg_id: str | None = None) -> bool:
        """
        Cancela el recordatorio asociado a la tarjeta de confirmación cuyo
        `wa_msg_id` coincide con el ID del mensaje al que responde el
        botón «Cancelar».  Usamos `context_id` (v16-) o, como *fallback*,
        `wa_msg_id`.
        """
        # 1) Buscar el cliente
        target_msg_id = context_id or wa_msg_id
        if not target_msg_id:
            return False        # no hay referencia válida
        customer = Customer.query.filter_by(phone=phone_id).first()
        if not customer:
            return False

        # 2) Buscar el recordatorio cuyo wa_msg_id coincide con el botón “Cancelar”
        reminder = (
            Reminder.query
            .filter_by(customer_id=customer.id, wa_msg_id=target_msg_id)
            .first()
        )
        if not reminder:
            return False

        reminder_id = reminder.id

        # 3) Eliminar de la base
        db.session.delete(reminder)
        db.session.commit()

        # 4) Cancelar tareas programadas
        for prefix in ("reminder_event_", "notify_event_"):
            job_id = f"{prefix}{reminder_id}"
            if scheduler_service.scheduler.get_job(job_id):
                scheduler_service.scheduler.remove_job(job_id)
        return True
        
    # ------------------------------------------------------------------
    # Listas del Super
    # ------------------------------------------------------------------
        
    def create_grocery_list(self, items: list[str]) -> dict:
            """
            Devuelve la lista tal cual, para que el LLM formatee la respuesta final.
            (Podrías guardar la lista en BD en el futuro si lo necesitas).
            """
            return {"items": items}
        
    # ------------------------------------------------------------------
    # Avisar a los creadores
    # ------------------------------------------------------------------

    def notify_creators(self, user_message: str,sender_name: str | None =None,) -> dict:
            """
            Envía un aviso por WhatsApp al número configurado en `ADMIN_PHONE`
            (con compatibilidad para `ADMIN_WAID`) cuando el usuario
            pide informar a los creadores/desarrolladores.

            """
            try:
                from app.utils.whatsapp_utils import send_message, get_text_message_input
                admin_phone = current_app.config.get("ADMIN_WAID")
                if not admin_phone:
                    return {"status": "no_admin_phone"}
                sender = sender_name or self.current_phone

                # 2️⃣  Nombre si viene; de lo contrario usa el teléfono del usuario
                sender = sender_name or self.current_phone
                alert = f"🚨 Reporte de {sender}: “{user_message}”"
                send_message(get_text_message_input(admin_phone, alert))
                return {"status": "sent"}
            except Exception as exc:
                logging.exception("[notify_creators] error al enviar alerta")
                return {"status": "error", "detail": str(exc)}
            
    # ------------------------------------------------------------------
    # 📅  Integración Google Calendar
    # ------------------------------------------------------------------

    def check_availability(
            self, date: str, start_time: str | None = None,
            end_time: str | None = None
        ) -> dict:
            """
            Devuelve una lista de bloques libres («slots») para la fecha dada.
            """
            try:
                slots = self.calendar_api.get_free_slots(date, start_time, end_time)
                return {"slots": slots}
            except Exception as exc:
                logging.exception("[check_availability] error")
                return {"error": str(exc)}

    def schedule_meeting(
        self,
        date: str,
        title: str,
        duration_minutes: int = 60,
        calendar_id: str | None = None,
    ) -> dict:
        """
        Crea la reunión y avisa por WhatsApp al número de Lucas.
        """
        try:
            dt_target = datetime.fromisoformat(date)
            wa_id_var = getattr(self, "current_phone", None)
            if wa_id_var:
                wa_id_var = self._to_e164_uy(wa_id_var)
            current_app.logger.info(f"[schedule_meeting] Número recibido: '{wa_id_var}' (debe coincidir exactamente con SPECIAL_WAID)")
            current_app.logger.info(f"[schedule_meeting] Request by wa_id={wa_id_var}, date={date}, duration={duration_minutes} min, calendar_override={calendar_id}")

            slots = self.calendar_api.get_free_slots(
                dt_target.date().isoformat(),
                dt_target.strftime("%H:%M"),
                (dt_target + timedelta(minutes=duration_minutes)).strftime("%H:%M"),
                slot_minutes=duration_minutes,
                wa_id=wa_id_var,
                calendar_id=calendar_id,
            )
            current_app.logger.info(f"[schedule_meeting] Slots disponibles para wa_id={wa_id_var}: {slots}")

            if not slots:
                return {
                    "error": "slot_unavailable",
                    "message": "Ese horario ya está ocupado. Prueba otra hora o pregúntame horarios libres.",
                }

            event_id = self.calendar_api.schedule_meeting(
                start_dt_str=date,
                title=title,
                duration_minutes=duration_minutes,
                wa_id=wa_id_var,
                calendar_id=calendar_id,
            )
            current_app.logger.info(f"[schedule_meeting] Evento creado en calendar_id para wa_id={wa_id_var}, event_id={event_id}")

            return {"event_id": event_id, "date": date, "title": title}

        except Exception as exc:
            logging.exception("[schedule_meeting] error")
            return {"error": str(exc)}
            
    #-------------------------------------------------------------------
    # 🗓️  Habitos
    #-------------------------------------------------------------------
        
    def create_habit(self, name: str, recordatorio_horas: int) -> dict:
        """Alta de hábito con validaciones mínimas."""
        try:
            if recordatorio_horas < 21 or recordatorio_horas > 23:
                current_app.logger.warning(f"[create_habit] Hora de recordatorio inválida: {recordatorio_horas}")
                recordatorio_horas = 23

            hb = Habito(
                customer_id=self.current_customer_id,  # Assuming `self.current_customer_id` is the correct customer ID
                nombre=name.title(),
                recordatorio_horas=recordatorio_horas,
                frecuencia="diaria",
            )
            db.session.add(hb)
            db.session.commit()
            current_app.logger.info(f"[create_habit] Hábito creado: {hb.nombre} ({hb.id})")
        except IntegrityError:
            db.session.rollback()
            return {
                "name": name,
                "message": f"Perfecto, tu hábito ha sido creado, te consultaré cada día a las {hb.recordatorio_horas:02d}:00 a ver si lo cumpliste.",
                "created": True,
            }
        except Exception as e:
            db.session.rollback()
            current_app.logger.exception("[create_habit] error inesperado")
            return {
                "name": name,
                "message": "❌ Ocurrió un error creando tu hábito. Intenta de nuevo.",
                "created": False,
            }
        current_app.logger.info(f"[create_habit] Hábito creado: {hb.nombre} ({hb.id}) {hb.recordatorio_horas}h")
        return {
            "name": hb.nombre,
            "hours": [recordatorio_horas],
            "message": f"✅ ¡Listo! Te preguntaré cada día a las {recordatorio_horas:02d}:00 si cumpliste o no.",
            "created": True,
        }


    def delete_habit(self, name: str) -> bool:
        """
        Desactiva (soft-delete) el hábito `name` del `customer` actual.
        Devuelve True si lo encontró y desactivó; False si no existía.
        """
        query = Habito.query.filter_by(
            nombre=name.strip().title(),
            customer_id=self.current_customer_id
        )

        hb = query.first()
        if not hb or not hb.activo:
            current_app.logger.info(f"[delete_habit] Hábito no encontrado o ya inactivo: {name}")
            return False

        hb.activo = False
        db.session.commit()
        current_app.logger.info(f"[delete_habit] Hábito desactivado: {hb.nombre} (ID: {hb.id})")

        # ⚠️  Cancela cualquier job futuro pendiente
        today = date.today().isoformat()
        job_id = f"habit_prompt_{hb.id}_{today}"
        if scheduler_service.scheduler.get_job(job_id):
            scheduler_service.scheduler.remove_job(job_id)
            current_app.logger.info(f"[delete_habit] Job cancelado: {job_id}")

        return True


    def list_habits(self) -> list[dict]:
            """
            Devuelve un array con todos los hábitos del usuario, ej.:
            [{ "name": "Beber Agua", "activo": True, "dias_inactivos": 1 }]
            """
            return [
                {
                    "name": hb.nombre,
                    "activo": hb.activo,
                    "dias_inactivos": hb.dias_inactivos,
                }
                for hb in Habito.query.filter_by(customer_id=self.current_customer_id).all()
            ]


    def reactivate_habit(self, name: str) -> bool:
            """
            Reactiva un hábito previamente desactivado y pone dias_inactivos=0.
            Programa de inmediato el job de medianoche (plan_daily_jobs se encarga).
            """
            hb = Habito.query.filter_by(customer_id=self.current_customer_id,
                                        nombre=name.strip().title()).first()
            if not hb or hb.activo:
                return False

            hb.activo = True
            hb.dias_inactivos = 0
            db.session.commit()
            return {"reactivated": True, "name": hb.nombre}
        
        
    # ------------------------------------------------------------------
    # 📅  Programar mensajes a terceros
    # ------------------------------------------------------------------
    import re 
    
    @staticmethod
    def _to_e164_uy(phone: str) -> str:
        """
        Normaliza a E.164 Uruguay:
        - '092308142'      -> '+59892308142'
        - '59892308142'    -> '+59892308142'
        - '+59892308142'   -> '+59892308142'
        - '0059892308142'  -> '+59892308142'
        """
        raw = (phone or "").strip()
        digits = re.sub(r"\D", "", raw)          # solo dígitos

        if not digits:
            return ""                           
        
        if digits.startswith("00"):             
            digits = digits[2:]

        if digits.startswith("0"):               
            digits = digits[1:]

        if not digits.startswith("598"):        
            digits = "598" + digits

        return f"+{digits}"


    def enviar_mensaje(self, telefono: str, mensaje: str, fecha_hora: str | None = None):
        """
        Tool handler para 'enviar_mensaje' reutilizando la lógica legacy.
        - telefono: número destino tal cual lo envía el Assistant.
        - mensaje: texto a reenviar.
        - fecha_hora: string (natural o ISO) en horario de America/Montevideo, o None para envío inmediato.
        Devuelve lo que retorna _process_direct (string de error o "" si ok).
        """
        from flask import current_app
        # 1) Normalizar fecha_hora -> datetime (o None)
        if fecha_hora:
            current_app.logger.info(f"[Orchestrator] Enviando mensaje a {telefono} para {fecha_hora}")
            dt = dateparser.parse(
                fecha_hora,
                settings={
                    "TIMEZONE": "America/Montevideo",
                    "RETURN_AS_TIMEZONE_AWARE": False,  # evita marca UTC
                },
            )
            send_dt = dt.replace(tzinfo=ZoneInfo("America/Montevideo")) if dt else None
            if send_dt:
                current_app.logger.info(f"[Orchestrator] Enviando mensaje a {telefono} para {send_dt}")
        else:
            send_dt = None

        # 1bis) Normalizar teléfono a E.164 UY
        to_e164 = self._to_e164_uy(telefono)
        current_app.logger.info(f"[Orchestrator] Teléfono normalizado: {telefono} -> {to_e164}")
        current_app.logger.info(f"[Orchestrator] Enviando mensaje a {to_e164} para {send_dt}")
        # 2) Reusar el flow existente para despachar
        return self.send_msg_flow._process_direct(
            origin_phone=getattr(self, "current_phone", None) or "",
            origin_name=getattr(self, "current_name", None) or "",
            customer_id=getattr(self, "current_customer_id", None),
            phone=to_e164,          #
            text=mensaje,
            send_dt=send_dt,
        )

        
    def cancel_scheduled_message(
                self,
                phone_id: str,
                context_id: str | None = None,
                wa_msg_id: str | None = None
        ) -> bool:
            target_msg_id = context_id or wa_msg_id
            if not target_msg_id:
                return False            # no hay referencia válida

            customer = Customer.query.filter_by(phone=phone_id).first()
            if not customer:
                return False

            sm = (ScheduledMessage.query
                    .filter_by(customer_id=customer.id,
                            wa_msg_id=target_msg_id,
                            status="pending")
                    .first())
            if not sm:
                return False
            
            # 1️⃣  borrar/invalidar el job
            job_id = f"scheduled_msg_{sm.id}"
            if scheduler_service.scheduler.get_job(job_id):
                scheduler_service.scheduler.remove_job(job_id)

            # 2️⃣  eliminar la fila (mantiene tu patrón de `Reminder`)
            db.session.delete(sm)
            db.session.commit()
            return True