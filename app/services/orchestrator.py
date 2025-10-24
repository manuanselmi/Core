import re
import json
import os
import logging
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from openai import OpenAI, BadRequestError
import dateparser
from app.services.responses_service import (
    get_or_create_conversation_id,
    responses_create,
    set_last_response_id,
    reset_conversation_for_phone,
    build_function_call_output,
    continue_with_function_outputs,
)
from pathlib import Path
from functools import lru_cache
from app.prompts import load_kairito
from flask import current_app 

from requests import Session
from app.services.customer_service import CustomerService
from app.services.calendar_service import CalendarService
import app.services.scheduler_service as scheduler_service
from app.utils.datetime_utils import parse_iso8601
from app.services.weather_service import get_forecast
from app.models import db, Reminder, Customer, ScheduledMessage
from app.services.google_calendar_service import GoogleCalendarService
from app.services.send_message_flow import SendMessageFlow
from zoneinfo import ZoneInfo

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

 


# ---------------- Helpers Responses/tool-calls ---------------------
def extract_tool_calls(resp) -> list:
    """
    Detect tool/function calls in a Responses result.
    - Prefer required_action.submit_tool_outputs.tool_calls in the caller,
      but this helper scans resp.output for top-level items and nested parts.
    Returns a list of call-like objects (SDK-specific structures).
    """
    import logging
    calls: list = []
    out_items = getattr(resp, "output", []) or []
    try:
        logging.getLogger("Orchestrator").debug(
            "[RESP] out types=%s",
            [getattr(i, "type", None) for i in out_items]
        )
    except Exception:
        pass

    for it in out_items:
        t = getattr(it, "type", None)
        # Top-level tool/function call
        if t in ("function_call", "tool_call"):
            calls.append(it)
        # Nested under message.content[*]
        if t == "message":
            for part in getattr(it, "content", []) or []:
                pt = getattr(part, "type", None)
                if pt in ("tool_use", "function_call", "tool_call"):
                    calls.append(part)
    return calls
    
class Orchestrator:
    def __init__(self, client: OpenAI, db_session: Session, catalog_path: str = "functions_catalog.json"):
        self.client = client
        self.db = db_session
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
        reply = self._assistant_reply(user.get("phone"), message or "", name, wa_msg_id)
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

    # ---------------- Helper para extraer texto de mensajes ------------
    # _unwrap_message deprecated (not used). Using _render_text instead.

    # ---------------- Core: integrar con Assistants API ----------------
    def _assistant_reply(self, wa_id: str, user_msg: str, user_name: str | None = None, wa_msg_id: str | None = None) -> str | dict:
        """
        Envía el mensaje a OpenAI Responses y devuelve el texto final.
        Si hay tool-calls, las ejecuta y encadena hasta obtener respuesta final.
        """
        # --- Contexto base ---
        wa_phone = wa_id  # si tenes normalizador E.164, usalo acá
        correlation_id = getattr(current_app, "correlation_id", None)
        # Propagate WhatsApp message id for idempotency/metadata
        wa_msg_id = wa_msg_id or os.getenv("CURRENT_WAMID") 

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
            f"- El usuario se llama: {user_name or 'Usuario'}.\n"
            f"- Fecha/Hora actual: {now.strftime('%Y-%m-%d %H:%M')} ({now.tzinfo})\n"
            f"- Usá SIEMPRE esta zona horaria para interpretar/mostrar horarios.\n"
            f"- Stage: {current_app.config.get('STAGE','local')}\n"
        )
        instructions = base_instr + extra_instr

        # --- 1) Primera llamada: el modelo puede pedir function_call(s) ---
        # Idempotency key for first turn (deterministic per conv + wamid)
        first_idem = f"resp:{conv_id}:{wa_msg_id or 'none'}:turn0"
        # Feature flag to disable parallel tool calls for compatibility
        disable_parallel = os.getenv("DISABLE_PARALLEL_TOOL_CALLS", "true").lower() in ("1", "true", "yes")
        if disable_parallel:
            current_app.logger.info("[RESP] parallel_tool_calls disabled")
        try:
            resp = responses_create(
                model=current_app.config["OPENAI_MODEL"],
                instructions=instructions,
                input_items=[{
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_msg}]
                }],
                tools=tools,
                conversation_id=conv_id,
                stream=False,
                tool_choice="auto",
                store=True,
                metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id},
                idempotency_key=first_idem,
                parallel_tool_calls=False if disable_parallel else None,
            )
        except BadRequestError as e:
            msg = str(e) or ""
            if "No tool output found" in msg:
                # Soft reset: create a fresh conversation without deleting the old one, then retry once
                current_app.logger.warning(
                    "[RESP] dirty conversation for %s (conv=%s). Soft-rotating and retrying once.",
                    wa_phone, conv_id
                )
                conv_id = reset_conversation_for_phone(wa_phone, delete_remote=False)
                first_idem = f"resp:{conv_id}:{wa_msg_id or 'none'}:turn0"
                resp = responses_create(
                    model=current_app.config["OPENAI_MODEL"],
                    instructions=instructions,
                    input_items=[{
                        "role": "user",
                        "content": [{"type": "input_text", "text": user_msg}]
                    }],
                    tools=tools,
                    conversation_id=conv_id,
                    stream=False,
                    tool_choice="auto",
                    store=True,
                    metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id},
                    idempotency_key=first_idem,
                    parallel_tool_calls=False if disable_parallel else None,
                )
            else:
                raise

        prev_id = getattr(resp, "id", None)
        if prev_id:
            set_last_response_id(wa_phone, prev_id)

        # --- 2) Loop: ejecutar tools y encadenar con previous_response_id ---
        while True:
            # 1) Preferir la ruta oficial: required_action.submit_tool_outputs.tool_calls
            req_action = getattr(resp, "required_action", None)
            tool_calls = []
            if req_action and getattr(req_action, "type", None) == "submit_tool_outputs":
                sto = getattr(req_action, "submit_tool_outputs", None)
                if sto:
                    tool_calls = getattr(sto, "tool_calls", []) or []

            # 2) Fallback robusto: buscar en resp.output incluyendo partes anidadas
            if not tool_calls:
                tool_calls = extract_tool_calls(resp)

            # Observabilidad: IDs y cantidad detectada
            ids_detectadas = [
                getattr(c, "call_id", None) or getattr(c, "id", None) or getattr(c, "tool_call_id", None)
                for c in (tool_calls or [])
            ]
            current_app.logger.debug(
                "[TOOL] detectadas=%d ids=%s",
                len(tool_calls or []), ids_detectadas
            )

            if not tool_calls:
                # Observabilidad adicional para depurar ausencias de tool-calls
                try:
                    out_items = getattr(resp, "output", []) or []
                    msg_types = []
                    for it in out_items:
                        t = getattr(it, "type", None)
                        if t == "message":
                            msg_types.extend([getattr(p, "type", None) for p in (getattr(it, "content", []) or [])])
                    current_app.logger.warning(
                        "[RESP] no tool calls found. resp.id=%s out_types=%s message.part.types=%s",
                        getattr(resp, "id", None),
                        [getattr(i, "type", None) for i in out_items],
                        msg_types,
                    )
                except Exception:
                    pass
                break  # no hay más tools → tenemos respuesta final

            call_ids: list[str] = []
            batched_results: list[dict] = []
            for fc in tool_calls:
                # Compatibilidad: estructura distinta según SDK
                tool_name = getattr(fc, "name", None)

                # Leer argumentos desde arguments (str/dict) o input (dict)
                tool_args = {}
                raw_args = getattr(fc, "arguments", None)
                if isinstance(raw_args, str):
                    try:
                        tool_args = json.loads(raw_args)
                    except Exception:
                        tool_args = {}
                elif isinstance(raw_args, dict):
                    tool_args = raw_args
                if not tool_args:
                    alt_input = getattr(fc, "input", None)
                    if isinstance(alt_input, dict):
                        tool_args = alt_input
                    else:
                        tool_args = {}

                # Id del llamado a la tool (depende de la representación)
                call_id = (
                    getattr(fc, "call_id", None)
                    or getattr(fc, "id", None)
                    or getattr(fc, "tool_call_id", None)
                )
                if not call_id:
                    current_app.logger.warning("[TOOL] %s sin call_id; se omite para evitar inconsistencias", tool_name)
                    continue

                # Ejecutar servicio real (GCal/DB/etc.) con manejo de errores
                try:
                    result = self._dispatch_tool(tool_name, tool_args)
                    current_app.logger.info("[TOOL] %s ejecutada → result type=%s", tool_name, type(result).__name__)
                    if result is None:
                        current_app.logger.warning("[TOOL] %s devolvió None, usando dict con error", tool_name)
                        result = {"error": "tool_returned_none"}
                except Exception as e:
                    current_app.logger.exception("[TOOL] %s falló", tool_name)
                    result = {"ok": False, "error": str(e)}

                call_ids.append(call_id)
                # Serialize output as JSON string per Responses API contract
                out_str = json.dumps(result if result is not None else {}, ensure_ascii=False)
                current_app.logger.info("[TOOL] %s → call_id=%s output_len=%d", tool_name, call_id, len(out_str))
                batched_results.append({
                    "tool_call_id": call_id,
                    "output": out_str,
                })

            if not batched_results:
                current_app.logger.warning("[CONT] no hubo outputs válidos para enviar; corto el loop para evitar bucles.")
                break

            # Construir function_call_output items (uno por cada tool-call)
            fc_outputs: list[dict] = []
            for br in batched_results:
                fc_outputs.append(
                    build_function_call_output(
                        call_id=br.get("tool_call_id"),
                        output_json_string=br.get("output", "{}"),
                    )
                )

            # Validación: no enviar parcial
            if len(fc_outputs) != len(tool_calls):
                current_app.logger.error(
                    "[CONT] outputs=%d pero tool_calls=%d → no envío parcial; corto para evitar 400",
                    len(fc_outputs), len(tool_calls)
                )
                break

            # Enviar una sola continuación con todas las devoluciones de esta ronda
            current_app.logger.info(
                "[CONT] sending prev_id=%s call_ids=%s count=%d",
                prev_id, call_ids, len(fc_outputs)
            )
            # Log detallado de lo que vamos a enviar
            for idx, br in enumerate(fc_outputs):
                current_app.logger.debug(
                    "[CONT] output %d: call_id=%s preview=%s",
                    idx, br.get("call_id"), br.get("output", "")[:200]
                )
            # Deterministic idempotency key for this follow-up
            follow_idem = f"resp:{conv_id}:{wa_msg_id or 'none'}:follow:{prev_id}"
            current_app.logger.info(
                "[CONT] returning %d outputs prev_id=%s ids=%s",
                len(fc_outputs), prev_id, [o.get("call_id") for o in fc_outputs]
            )
            resp = continue_with_function_outputs(
                model=current_app.config["OPENAI_MODEL"],
                previous_response_id=prev_id,
                outputs=fc_outputs,
                idempotency_key=follow_idem,
                tools=tools,
                disable_parallel=disable_parallel,
                timeout_s=17.0,
            )
            current_app.logger.debug(
                "[CONT] response.id=%s status=%s",
                getattr(resp, "id", None), getattr(resp, "status", None),
            )
            if not resp:
                current_app.logger.error(
                    "[CONT] continuation aborted due to invalid outputs; breaking to avoid loop."
                )
                break
            prev_id = getattr(resp, "id", prev_id)
            if prev_id:
                set_last_response_id(wa_phone, prev_id)

        # --- 3) Texto final ---
        final_text = getattr(resp, "output_text", None) or self._render_text(getattr(resp, "output", []))
        return final_text

    def _dispatch_tool(self, tool_name: str, tool_args: dict) -> dict | None:
        """
        Despacha la ejecución de una tool function a su método correspondiente.
        Retorna el resultado en formato dict o None.
        """
        # Mapeo de nombres de tools a métodos
        tool_map = {
            "create_reminder": self.create_reminder,
            "cancel_reminder": self.cancel_reminder,
            "lookup_customer": self.lookup_customer,
            "get_weather": self.get_weather,
            "schedule_meeting": self.schedule_meeting,
            "check_availability": self.check_availability,
            "enviar_mensaje": self.enviar_mensaje,
            "cancel_scheduled_message": self.cancel_scheduled_message,
        }
        
        handler = tool_map.get(tool_name)
        if not handler:
            current_app.logger.warning("[_dispatch_tool] Unknown tool: %s", tool_name)
            return {"error": f"Unknown tool: {tool_name}"}
        # Sanitizar args para tools conocidas (evita TypeError por campos legacy)
        if tool_name == "schedule_meeting":
            allowed = {"date", "duration_minutes", "title", "calendar_id"}
            tool_args = {k: v for k, v in (tool_args or {}).items() if k in allowed}
        
        return handler(**(tool_args or {}))

    def _render_text(self, output_items: list) -> str:
        """
        Extrae texto de output items de Responses API.
        Fallback si output_text no está disponible.
        """
        for item in output_items or []:
            item_type = getattr(item, "type", None)
            if item_type == "message":
                content = getattr(item, "content", []) or []
                for part in content:
                    if getattr(part, "type", None) == "text":
                        return getattr(part, "text", "Listo.")
        return "Listo."

    # ------------------------------------------------------------------
    # Recordatorios
    # ------------------------------------------------------------------

    def create_reminder(self, date: str, title: str, wa_msg_id: str | None = None) -> dict:
            print(f"[DEBUG] Orchestrator.reminder → customer_id={self.current_customer_id}, date={date}, title={title}, wa_msg_id={wa_msg_id}")
            # 1) Guarda en la BD, pasando primero el customer_id
            cs = CalendarService()
            reminder_id = cs.create(
                self.current_customer_id,  
                date,                     
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
            return False        
        customer = Customer.query.filter_by(phone=phone_id).first()
        if not customer:
            return False

        reminder = (
            Reminder.query
            .filter_by(customer_id=customer.id, wa_msg_id=target_msg_id)
            .first()
        )
        if not reminder:
            return False

        db.session.delete(reminder)
        db.session.commit()
        return True
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
        current_app.logger.info("[TOOL] schedule_meeting: date=%s, duration=%d, user=%s",
                    date, duration_minutes, self.current_phone)

        # Estandarizar título con nombre desde DB (ignorar el 'title' libre)
        db_name = None
        try:
            if self.current_customer_id:
                cust = CustomerService.get(self.current_customer_id) or {}
                db_name = cust.get("name")
            if not db_name and self.current_phone:
                c = Customer.query.filter_by(phone=self.current_phone).first()
                db_name = getattr(c, "name", None)
        except Exception:
            db_name = None
        customer_name = db_name or (self.current_name or "Usuario")
        event_title = f"Reunión con {customer_name}"
        
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
                title=event_title,
                duration_minutes=duration_minutes,
                wa_id=wa_id_var,
                calendar_id=calendar_id,
            )
            current_app.logger.info(f"[TOOL] schedule_meeting ok wa_id={wa_id_var} event_id={event_id}")

            return {"event_id": event_id, "date": date, "title": event_title}

        except Exception as exc:
            logging.exception("[TOOL] schedule_meeting error")
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
            out = {"slots": slots}
            try:
                current_app.logger.info(
                    "[TOOL] check_availability OUTPUT=%s",
                    json.dumps(out, ensure_ascii=False)
                )
            except Exception:
                pass
            return out
        except Exception as e:
            logging.exception("[check_availability] error")
            return {"error": "calendar_error", "message": str(e)}

    # ------------------------------------------------------------------
    # 📅  Programar mensajes a terceros (sin APScheduler)
    # ------------------------------------------------------------------
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
