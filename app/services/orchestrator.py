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
from app.models import db, Reminder, Customer, ScheduledMessage
from app.services.google_calendar_service import CALENDAR_ID, GoogleCalendarService
from app.services.send_message_flow import SendMessageFlow
from app.services.openai_service import client, get_or_create_thread, ASSISTANT_ID
from zoneinfo import ZoneInfo
import os

LOCAL_TZ_STR = os.getenv("TZ", "America/Montevideo")

def log_http_response(response):
    logging.info(f"Status: {response.status_code}")
    logging.info(f"Content-type: {response.headers.get('content-type')}")
    logging.info(f"Body: {response.text}")

class Orchestrator:
    def __init__(self, client: OpenAI, db_session: Session, catalog_path: str = "functions_catalog.json"):
        self.client = client
        self.db = db_session
        self.bot_logic = BotLogic()
        self.catalog_path = catalog_path
        self.current_customer_id = None
        self.current_phone = None
        self.current_name = None
        self.calendar_api = None
        self.has_calendar = False
        try:
            from app.services.google_calendar_service import GoogleCalendarService
            self.calendar_api = GoogleCalendarService() 
            self.has_calendar = bool(getattr(self.calendar_api, "client", None))
        except Exception as e:
            logging.warning("Calendar deshabilitado o no disponible: %s", e)
            self.calendar_api = None
            self.has_calendar = False

        if not self.has_calendar:
            logging.info("Google Calendar API no inicializada (modo sin Google).")
        self.send_msg_flow = SendMessageFlow()

    def handle_message(self, message: str, phone: str, name: str | None, wa_msg_id: str | None):
        """
        Entrada principal. Mantengo el flujo original y sólo adapto el scheduling.
        """
        user = CustomerService.find_or_create(phone, name)
        self.current_customer_id = user.get("id")
        self.current_phone = phone
        self.current_name = name

        # Assistants (igual)
        reply = self._assistant_reply(user.get("phone"), message or "", name)
        
        # Persistir turno en memoria (legacy)
        Memory.save_turn(phone, "user", message, wa_msg_id=wa_msg_id)
        if isinstance(reply, str):
            Memory.save_turn(phone, "assistant", reply)
            if Memory.should_summarize(phone):
                Memory.summarize(phone, self.client)

        return reply

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

        dispatch = {
            # Calendario / Agenda
            "create_reminder":       self.create_reminder,
            "check_availability": getattr(self, "check_availability", None),
            "schedule_meeting":   getattr(self, "schedule_meeting", None),

            # Clima
            "get_weather":        getattr(self, "get_weather", None),

            # Clientes
            "lookup_customer":    getattr(self, "lookup_customer", None),

            # Notificaciones a creadores
            "notify_creators":    getattr(self, "notify_creators", None),

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
        thread_id = get_or_create_thread(
            wa_id,
            customer_id=getattr(self, "current_customer_id", None),
            correlation_id=getattr(current_app, "correlation_id", None),
            last_wa_msg_id=os.getenv("CURRENT_WAMID")  # opcional si lo guardás en contexto
        )
        
        extra_instr = None
        try:
            extra_instr = Memory.build_ephemeral_instructions(wa_id=wa_id, user_name=user_name)
        except Exception:
            current_app.logger.warning("[Memory] no se pudieron construir instrucciones efímeras")

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
                for call in run.required_action.submit_tool_outputs.tool_calls:
                    name = call.function.name
                    args = json.loads(call.function.arguments or "{}")

                    # saneo de fechas de create_reminder (mantengo tu lógica de normalización)
                    if name == "create_reminder":
                        # 1) Preferencia a argumentos bien formateados del assistant
                        dt_assistant = None
                        local_tz = ZoneInfo(LOCAL_TZ_STR)
                        now_local = datetime.now(local_tz)
                        try:
                            candidate = args.get("date")
                            if candidate:
                                dt_assistant = dateparser.parse(
                                    candidate,
                                    settings={
                                        "TIMEZONE": LOCAL_TZ_STR,
                                        "RETURN_AS_TIMEZONE_AWARE": True,
                                        "PREFER_DATES_FROM": "future",
                                        "STRICT_PARSING": False,
                                    },
                                )
                                if dt_assistant and dt_assistant.tzinfo is None:
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

                    try:
                        result = self._execute_function(name, **args)
                        if isinstance(result, dict):
                            tool_outputs.append({
                                "tool_call_id": call.id,
                                "output": json.dumps(result, ensure_ascii=False)
                            })
                        else:
                            tool_outputs.append({
                                "tool_call_id": call.id,
                                "output": json.dumps({"message": str(result or "")}, ensure_ascii=False)
                            })
                    except Exception as e:
                        current_app.logger.exception("[Tool] error en %s", name)
                        tool_outputs.append({
                            "tool_call_id": call.id,
                            "output": json.dumps({"error": str(e)}, ensure_ascii=False)
                        })

                client.beta.threads.runs.submit_tool_outputs(
                    thread_id=thread_id,
                    run_id=run.id,
                    tool_outputs=tool_outputs
                )
                continue

            # b) Si completó, devolver texto
            if run.status == "completed":
                thread_msgs = client.beta.threads.messages.list(thread_id=thread_id, limit=1)
                if not thread_msgs.data:
                    return "Listo."
                return self._unwrap_message(thread_msgs.data[0])

            if run.status in ("failed", "expired", "cancelled"):
                return "Se interrumpió el proceso, probemos de nuevo."

            time.sleep(0.35)  # backoff leve

    # ------------------------------------------------------------------
    # Recordatorios
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

            # 3) Programar los jobs: recordatorio y notificación al inicio (EventBridge)
            advance = current_app.config.get("EVENT_ADVANCE", timedelta(minutes=1))

            scheduler_service.schedule_event_reminder(
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

        # 3) Eliminar de la base
        db.session.delete(reminder)
        db.session.commit()
        return True
        
    # ------------------------------------------------------------------
    # Clima
    # ------------------------------------------------------------------
    def get_weather(self, city: str = "Montevideo", units: str = "metric") -> dict:
            try:
                return get_forecast(city, units=units)
            except Exception:
                current_app.logger.exception("[get_weather] error")
                return {"error": "weather_unavailable"}

    # ------------------------------------------------------------------
    # Calendario: disponibilidad + agendado en GCal
    # ------------------------------------------------------------------
    def schedule_meeting(self,
                         date: str,
                         duration_minutes: int = 60,
                         title: str = "Reunión",
                         calendar_id: str | None = None) -> dict:
        """
        Reserva slot en el Calendar si está libre.
        """
        try:
            # 0) Customer actual
            wa_id_var = getattr(self, "current_phone", None)

            # 1) Check básico de disponibilidad
            slots = self.calendar_api.list_available_slots(date, duration_minutes, calendar_id)
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

    # ------------------------------------------------------------------
    # 📅  Programar mensajes a terceros (sin APScheduler)
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
                    "RETURN_AS_TIMEZONE_AWARE": True,
                    "PREFER_DATES_FROM": "future",
                    "STRICT_PARSING": False,
                },
            )
            if not dt:
                return "No entendí la fecha/hora para programar el mensaje."

            # asegurar aware en tz local
            tz_local = ZoneInfo("America/Montevideo")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz_local)
        else:
            dt = None  # envío inmediato

        # 2) Normalizar teléfono y despachar
        to_e164 = self._to_e164_uy(telefono)
        current_app.logger.info(f"[Orchestrator] Teléfono normalizado: {telefono} -> {to_e164}")
        current_app.logger.info(f"[Orchestrator] Enviando mensaje a {to_e164} para {dt}")
        # 3) Reusar el flow existente para despachar
        return self.send_msg_flow._process_direct(
            origin_phone=getattr(self, "current_phone", None) or "",
            origin_name=getattr(self, "current_name", None) or "",
            customer_id=getattr(self, "current_customer_id", None),
            phone=to_e164,          #
            text=mensaje,
            send_dt=dt,
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

            # 2️⃣  eliminar la fila (mantiene tu patrón de `Reminder`)
            db.session.delete(sm)
            db.session.commit()
            return True
