import re
import json
import os
import logging
import time
from datetime import timedelta, datetime, date
from uuid import uuid4
from sqlalchemy.exc import IntegrityError
from zoneinfo import ZoneInfo
from openai import OpenAI
import dateparser
from app.services.responses_service import (
    get_or_create_conversation_id,
    responses_create,
)
from pathlib import Path
from functools import lru_cache
from app.prompts import load_kairito
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
from zoneinfo import ZoneInfo
import os

LOCAL_TZ_STR = os.getenv("TZ", "America/Montevideo")

_WD_MAP = {
    "Monday": "lunes",
    "Tuesday": "martes",
    "Wednesday": "miércoles",
    "Thursday": "jueves",
    "Friday": "viernes",
    "Saturday": "sábado",
    "Sunday": "domingo"
}

def build_extra_instructions(user_name: str | None) -> str:
    """
    Construye instrucciones adicionales para el Assistant con fecha/hora actual.
    Mantiene formato y contenido exacto del legacy para compatibilidad.
    """
    local_tz = ZoneInfo(LOCAL_TZ_STR)
    now = datetime.now(local_tz)
    weekday_es = _WD_MAP[now.strftime('%A')]
    
    return (
        f"Usá la tool correcta según corresponda, una sola por turno. "
        f"Perfil del usuario: se llama '{user_name}'. "
        f"Si el usuario pregunta '¿cómo me llamo?' o similar, respondé '{user_name}'. "
        f"Podés saludar o referirte a él por ese nombre cuando tenga sentido. "
        f"La fecha y hora actual es {now.strftime('%d/%m/%Y %H:%M')} "
        f"y el día de hoy es {weekday_es}."
    )

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
        self.logger = logging.getLogger("Orchestrator")
        if not self.logger.handlers:
            handler = logging.StreamHandler()
            handler.setLevel(logging.DEBUG)
            formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.DEBUG)
        try:
            self.calendar_api = GoogleCalendarService()
            self.has_calendar = bool(
                getattr(self.calendar_api, "service", None)  # nuestro wrapper
                or getattr(self.calendar_api, "client", None)  # por si en el futuro cambiamos el nombre
            )
        except Exception as e:
            logging.warning("Calendar deshabilitado o no disponible: %s", e)
            self.calendar_api = None
            self.has_calendar = False

        logging.info(
            "Google Calendar API %s",
            "inicializada" if self.has_calendar else "no inicializada (modo sin Google)."
        )
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
        return reply
    
    # ---------- Tools catalog loader ----------
    @staticmethod
    @lru_cache(maxsize=1)
    def _load_tools_catalog_cached(root_path: str) -> list[dict]:
        """
        Carga y normaliza functions_catalog.json una sola vez por proceso.
        - Busca en <app root>/functions_catalog.json y en <repo root>/functions_catalog.json
        - Limpia campos no soportados (p. ej., 'strict' dentro de 'function')
        """
        candidates = [
            Path(root_path) / "functions_catalog.json",
            Path(root_path).parent / "functions_catalog.json",
        ]
        raw = None
        for p in candidates:
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    break
                except Exception:
                    raw = None
        # Acepta formato lista o dict con clave 'tools'
        if isinstance(raw, dict) and "tools" in raw:
            tools = raw.get("tools") or []
        elif isinstance(raw, list):
            tools = raw
        else:
            tools = []
        # Normaliza: quita 'strict' dentro de cada function (en Responses es por request)
        cleaned: list[dict] = []
        for t in tools:
            try:
                if isinstance(t, dict) and t.get("type") == "function":
                    fn = t.get("function")
                    if isinstance(fn, dict):
                        fn = dict(fn)
                        fn.pop("strict", None)
                        t = dict(t)
                        t["function"] = fn
                cleaned.append(t)
            except Exception:
                # Si algo viene mal formado, lo omitimos silenciosamente
                continue
        return cleaned

    def _load_tools_catalog(self) -> list[dict]:
        """
        Devuelve la lista de tools para pasar a Responses.create(...).
        Usa caché y hace fallback a lista vacía si no hay catálogo.
        """
        try:
            root = current_app.root_path  # normalmente apunta a /var/task/app
        except Exception:
            root = Path(__file__).resolve().parents[2].as_posix()
        tools = self._load_tools_catalog_cached(root)
        if not tools:
            try:
                current_app.logger.warning("functions_catalog.json no encontrado o vacío; sigo sin tools.")
            except Exception:
                pass
        return tools


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
        Envía el mensaje a OpenAI Responses y devuelve el texto final.
        Si hay tool-calls, las ejecuta y encadena hasta obtener respuesta final.
        """
        # --- Contexto base ---
        wa_phone = wa_id  # si tenes normalizador E.164, usalo acá
        correlation_id = getattr(current_app, "correlation_id", None)
        wa_msg_id = os.getenv("CURRENT_WAMID")  # si lo cargas en contexto/env

        # Conversación (estado persistente tipo threads)
        conv_id = get_or_create_conversation_id(
            wa_phone,
            customer_id=getattr(self, "current_customer_id", None),
            correlation_id=correlation_id,
            last_wa_msg_id=wa_msg_id
        )

        # Tools (de tu catálogo). Asegurate que esta función devuelva la lista (no el dict raíz)
        tools = self._load_tools_catalog()
        if isinstance(tools, dict) and "tools" in tools:
            tools = tools["tools"]
            
        base_instr = load_kairito()
        now = datetime.now(ZoneInfo(current_app.config.get("TZ", "America/Montevideo")))
        extra_instr = (
            f"\n\n[Instrucciones de runtime]\n"
            f"- Fecha/Hora actual: {now.strftime('%Y-%m-%d %H:%M')} ({now.tzinfo})\n"
            f"- Usá SIEMPRE esta zona horaria para interpretar/mostrar horarios.\n"
            f"- Stage: {current_app.config.get('STAGE','local')}\n"
        )
        instructions = base_instr + extra_instr

        # --- 1) Primera llamada: el modelo puede pedir function_call(s) ---
        resp = responses_create(
            model=current_app.config["OPENAI_MODEL"],
            instructions=instructions,
            input_items=[{
                "role": "user",
                "content": [{"type": "input_text", "text": user_msg}]
            }],
            tools=tools,
            conversation_id=conv_id,
            stream=False,              # Síncrono (no SSE). La memoria persiste igual por conversation+store.
            tool_choice="auto",
            store=True,
            #strict=True,
            metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id}
        )

        prev_id = getattr(resp, "id", None)

        # --- 2) Loop: ejecutar tools y encadenar con previous_response_id ---
        while True:
            output_items = getattr(resp, "output", []) or []
            f_calls = [it for it in output_items if getattr(it, "type", "") == "function_call"]

            if not f_calls:
                break  # no hay más tools → tenemos respuesta final

            for fc in f_calls:
                tool_name = fc.name
                tool_args = json.loads(fc.arguments or "{}")

                # Ejecutar servicio real (GCal/DB/etc.) con manejo de errores
                try:
                    result = self._dispatch_tool(tool_name, tool_args)
                except Exception as e:
                    current_app.logger.exception("[Tool] %s falló", tool_name)
                    result = {"ok": False, "error": str(e)}

                # Devolver resultado de tool correlacionado por call_id y encadenado al response previo
                resp = responses_create(
                    model=current_app.config["OPENAI_MODEL"],
                    instructions=instructions,  
                    input_items=[{
                        "role": "tool",
                        "call_id": fc.call_id,     # <-- correlación Responses
                        "content": [{
                            "type": "output_text",
                            "text": json.dumps(result, ensure_ascii=False)
                        }]
                    }],
                    tools=tools,                 # dejar habilitadas por si encadena otra tool
                    conversation_id=conv_id,
                    previous_response_id=prev_id,
                    stream=False,
                    store=True,
                    #strict=True
                )
                prev_id = getattr(resp, "id", prev_id)

        # --- 3) Texto final ---
        final_text = getattr(resp, "output_text", None) or self._render_text(getattr(resp, "output", []))
        return final_text


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
        current_app.logger.info("[APPOINTMENT] Attempting to schedule: date=%s, duration=%d, title=%s, user=%s",
                    date, duration_minutes, title, self.current_phone)
        
        if not self.has_calendar:
            current_app.logger.error("[APPOINTMENT] Calendar service not initialized")
            raise RuntimeError("Servicio de calendario no disponible")
        try:
            # 0) Customer actual
            wa_id_var = getattr(self, "current_phone", None)

            # 1) Check básico de disponibilidad
            slots = self.calendar_api.get_free_slots(
                date_str=date,
                slot_minutes=duration_minutes,
                calendar_id=calendar_id,
                wa_id=wa_id_var
            )
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
        
    def check_availability(
        self,
        date: str,
        start_time: str | None = None,
        end_time: str | None = None,
        slot_minutes: int = 60,
        calendar_id: str | None = None,
    ):
        """
        Tool: devuelve bloques libres (del tamaño 'slot_minutes') en la fecha indicada.
        Usa el WAID del usuario actual para resolver calendario especial si aplica.
        """
        current_app.logger.info("[APPOINTMENT] Checking availability: date=%s, start=%s, end=%s, duration=%d minutes, user=%s",
                        date, start_time, end_time, slot_minutes, self.current_phone)
        
        if not self.has_calendar or not getattr(self.calendar_api, "get_free_slots", None):
            current_app.logger.error("[APPOINTMENT] Calendar service unavailable")
            return {"error": "calendar_unavailable"}
        if not self.has_calendar or not getattr(self.calendar_api, "get_free_slots", None):
            return {
                "error": "calendar_unavailable",
                "message": "No tengo acceso a Google Calendar por ahora.",
            }
        try:
            wa_id = self.current_phone  # WhatsApp del usuario actual, si lo tenés en contexto
            slots = self.calendar_api.get_free_slots(
                date_str=date,
                start_time=start_time,
                end_time=end_time,
                slot_minutes=slot_minutes,
                wa_id=wa_id,
                calendar_id=calendar_id,
            )
            return {"slots": slots}
        except Exception as e:
            logging.exception("[check_availability] error")
            return {"error": "calendar_error", "message": str(e)}

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
