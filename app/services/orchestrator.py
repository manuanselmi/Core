import re
import json
import os
import logging
from datetime import timedelta, datetime
from zoneinfo import ZoneInfo
from openai import OpenAI, BadRequestError
import dateparser
from app.services.responses_service import (
    create_first_response,
    continue_with_tool_output,
    build_function_call_output,
)
from pathlib import Path
from functools import lru_cache
from app.prompts import load_kairito
from flask import current_app 

from app.services.customer_service import CustomerService
from app.services.assistant_conversation_service import AssistantConversationService
import app.services.scheduler_service as scheduler_service
from app.utils.datetime_utils import parse_iso8601
from app.services.weather_service import get_forecast
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
    def __init__(self, client: OpenAI, catalog_path: str = "functions_catalog.json", repo_provider=None):
        self.client = client
        self.catalog_path = catalog_path
        self.current_customer_id = None
        self.current_phone = None
        self.current_name = None
        
        # Dynamo repositories (fallback si no se inyecta)
        if repo_provider is None:
            from app.db import RepositoryProvider
            repo_provider = RepositoryProvider()
        self.repo = repo_provider
        self.correlation_id_provider = getattr(repo_provider, 'correlation_id_provider', lambda: 'no-cid')
        
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

    def _safe_current_cid(self):
        """
        Accede de forma segura a current_app.correlation_id sin romper cuando no hay contexto.
        
        Returns:
            str | None: correlation_id desde Flask app context, o None si no está disponible
        """
        try:
            from flask import current_app
            return getattr(current_app, "correlation_id", None)
        except Exception:
            return None

    def handle_message(self, message: str, phone: str, name: str | None, wa_msg_id: str | None):
        """
        Entrada principal. Mantengo el flujo original y sólo adapto el scheduling.
        """
        cid = self.correlation_id_provider()
        self.logger.info(f"[CID={cid}] handle_message → phone={phone}, wa_msg_id={wa_msg_id}")
        
        user = CustomerService.find_or_create(phone, name, repo_provider=self.repo)
        # En Dynamo no hay customer_id numérico, guardamos None para compatibilidad
        self.current_customer_id = None
        self.current_phone = phone
        self.current_name = name
        self.current_wa_msg_id = wa_msg_id  # Para idempotencia en schedule_meeting

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
        
        **SOLO RESPONSES API** - sin Conversations API.
        Primera llamada con tools, continuaciones con previous_response_id.
        """
        # --- Contexto base ---
        wa_phone = wa_id
        correlation_id = self._safe_current_cid() or self.correlation_id_provider()
        wa_msg_id = wa_msg_id or os.getenv("CURRENT_WAMID")

        # --- Obtener/crear AssistantConversation y recuperar last_response_id ---
        assistant_conv = AssistantConversationService.find_or_create(
            wa_phone=wa_phone,
            customer_id=None,  # Dynamo no usa customer_id
            repo_provider=self.repo
        )
        previous_response_id = assistant_conv.get('last_response_id')
        
        # Tools (de tu catálogo). Asegurate que esta función devuelva la lista (no el dict raíz)
        tools = self._load_tools_catalog()
        if isinstance(tools, dict) and "tools" in tools:
            tools = tools["tools"]
            
        base_instr = load_kairito()
        now = datetime.now(ZoneInfo(current_app.config.get("TZ", "America/Montevideo")))
        extra_instr = (
            f"\n\n[Instrucciones de runtime]\n"
            f"- Usuario: {user_name or 'Usuario'}.\n"
            f"- Fecha/Hora: {now.strftime('%Y-%m-%d %H:%M')} ({now.tzinfo}). Usá SIEMPRE esta zona horaria.\n"
            f"- Sé directo: resolvé sin rodeos ni repreguntas.\n"
            f"- Por defecto: turno presencial, 60 min, con Karina y para quien escribe.\n"
            f"- Si falta un dato, asumilo; no hagas listas de preguntas.\n"
            f"- Estilo WhatsApp: breve, natural y orientado a la acción.\n"
            f"- Disponibilidad → mostrar horarios. Agendar → reservar. Cancelar → eliminar. Reprogramar → ofrecer nuevos.\n"
            f"- No preguntes por modalidad, nombre o duración.\n"
            f"- Stage: {current_app.config.get('STAGE','local')}\n"
        )
        instructions = base_instr + extra_instr

        # --- 1) Primera llamada: usar previous_response_id si existe, o create_first_response ---
        turn_index = 0
        first_idem = f"resp::{wa_msg_id or 'none'}::turn{turn_index}"
        
        # Feature flag to disable parallel tool calls for compatibility
        disable_parallel = os.getenv("DISABLE_PARALLEL_TOOL_CALLS", "true").lower() in ("1", "true", "yes")
        
        # Initial input messages
        input_messages = [{
            "role": "user",
            "content": [{"type": "input_text", "text": user_msg}]
        }]
        
        # Si existe previous_response_id, usar continuación; sino, crear primera respuesta
        if previous_response_id:
            resp = continue_with_tool_output(
                model=current_app.config["OPENAI_MODEL"],
                previous_response_id=previous_response_id,
                input_items=input_messages,
                instructions=instructions,  # ADDED: Always pass instructions
                tools=tools,  # ADDED: Always pass tools
                tool_choice="auto",  # ADDED: Always pass tool_choice
                store=True,
                metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id, "correlation_id": correlation_id},
                idempotency_key=first_idem,
                parallel_tool_calls=False if disable_parallel else None,
            )
        else:
            resp = create_first_response(
                model=current_app.config["OPENAI_MODEL"],
                instructions=instructions,
                input_items=input_messages,
                tools=tools,
                tool_choice="auto",
                store=True,
                metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id, "correlation_id": correlation_id},
                idempotency_key=first_idem,
                parallel_tool_calls=False if disable_parallel else None,
            )

        first_response_id = getattr(resp, "id", None)
        
        # --- Persistir response_id en la BD ---
        if first_response_id:
            AssistantConversationService.update_last_response_id(
                wa_phone, 
                first_response_id, 
                repo_provider=self.repo
            )

        # --- 2) Loop: ejecutar tools y encadenar SOLO con previous_response_id ---
        max_iterations = 10  # Safety limit
        iteration = 0
        
        while iteration < max_iterations:
            iteration += 1
            
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

            if not tool_calls:
                # No hay más tools → tenemos respuesta final
                break

            # 3) Ejecutar cada tool-call y construir outputs
            tool_outputs = []
            
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
                    current_app.logger.warning(
                        "[TOOL] %s sin call_id; se omite para evitar inconsistencias", 
                        tool_name
                    )
                    continue

                # Ejecutar servicio real (GCal/DB/etc.) con manejo de errores
                try:
                    result = self._dispatch_tool(tool_name, tool_args)
                    if result is None:
                        result = {}
                except Exception as e:
                    current_app.logger.exception("[TOOL] %s failed", tool_name)
                    result = {"ok": False, "error": str(e)}

                # CRÍTICO: Serialize output as JSON STRING per Responses API contract
                out_str = json.dumps(result if result is not None else {}, ensure_ascii=False)
                
                # Agregar function_call_output con call_id y JSON string
                tool_outputs.append(
                    build_function_call_output(call_id, out_str)
                )

            # Validación: asegurar que agregamos outputs
            if not tool_outputs:
                current_app.logger.warning("[CONT] no outputs; breaking to avoid loop")
                break

            # 4) Enviar continuation con previous_response_id (SOLO previous_response_id)
            turn_index += 1
            follow_idem = f"resp::{wa_msg_id or 'none'}::follow::{first_response_id}::n{turn_index}"
            
            resp = continue_with_tool_output(
                model=current_app.config["OPENAI_MODEL"],  # REQUIRED en continuación
                previous_response_id=first_response_id,  # SIEMPRE el primer response.id del turno
                input_items=tool_outputs,
                instructions=instructions,  # ADDED: Always pass instructions
                tools=tools,  # ADDED: Always pass tools
                tool_choice="auto",  # ADDED: Always pass tool_choice
                store=True,
                metadata={"wa_id": wa_phone, "wa_msg_id": wa_msg_id, "correlation_id": correlation_id},
                idempotency_key=follow_idem,
                parallel_tool_calls=False if disable_parallel else None,  # ADDED: Always pass parallel setting
            )
            
            if not resp:
                current_app.logger.error("[CONT] continuation failed; breaking")
                break
            
            # --- Persistir response_id actualizado en la BD ---
            continuation_response_id = getattr(resp, "id", None)
            if continuation_response_id:
                AssistantConversationService.update_last_response_id(
                    wa_phone, 
                    continuation_response_id, 
                    repo_provider=self.repo
                )
                # Actualizar first_response_id para siguientes continuaciones
                first_response_id = continuation_response_id

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
            "cancelar": self.cancel_meeting,
            "list_upcoming_appointments": self.list_upcoming_appointments,
        }
        
        handler = tool_map.get(tool_name)
        if not handler:
            current_app.logger.warning("[_dispatch_tool] Unknown tool: %s", tool_name)
            return {"error": f"Unknown tool: {tool_name}"}
        # Sanitizar args para tools conocidas (evita TypeError por campos legacy)
        if tool_name == "schedule_meeting":
            allowed = {"date", "duration_minutes", "title", "calendar_id", "intent_wa_msg_id"}
            tool_args = {k: v for k, v in (tool_args or {}).items() if k in allowed}
            # Inyectar intent_wa_msg_id del mensaje original para idempotencia
            if not tool_args.get("intent_wa_msg_id") and hasattr(self, "current_wa_msg_id"):
                tool_args["intent_wa_msg_id"] = self.current_wa_msg_id
        elif tool_name == "cancelar":
            allowed = {"appointment_id", "google_event_id", "cancel_reason"}
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
            import uuid
            from app.db.dynamo_client import now_ms
            
            # 1) Parsear la fecha a datetime y convertir a epoch_ms
            reminder_dt = parse_iso8601(date)
            date_ms = int(reminder_dt.timestamp() * 1000)
            
            # 2) Crear reminder en Dynamo
            reminder_uuid = str(uuid.uuid4())
            data = {
                'titulo': title,
                'wa_msg_id': wa_msg_id
            }
            
            reminder = self.repo.reminders.create(
                phone=self.current_phone,
                date_ms=date_ms,
                uuid=reminder_uuid,
                data=data
            )
            
            # 3) Programar el job de recordatorio (EventBridge)
            advance = current_app.config.get("EVENT_ADVANCE", timedelta(minutes=1))
            scheduler_service.schedule_event_reminder(
                reminder.get('reminder_id'),
                advance=advance
            )

            # Devolvemos también title y date para templates
            date_str = reminder_dt.strftime("%Y-%m-%d %H:%M")
            return {
                "reminder_id": reminder.get('reminder_id'),
                "date": date_str,
                "title": title,
                "wa_msg_id": wa_msg_id
            }

    def lookup_customer(self, customer_id: int) -> dict:
            """
            DEPRECADO en Dynamo: customer_id no existe.
            Retorna info del customer actual por phone.
            """
            if not self.current_phone:
                return {"error": "no_phone", "customer": None}
            
            customer = CustomerService.get_by_phone(self.current_phone, repo_provider=self.repo)
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
        from app.db.dynamo_keys import pk_customer
        
        target_msg_id = context_id or wa_msg_id
        if not target_msg_id:
            return False

        # Buscar reminder por wa_msg_id
        reminder = self.repo.reminders.get_by_wa_msg_id(target_msg_id)
        if not reminder:
            return False

        # Verificar que pertenece al customer correcto
        if reminder.get('customer_phone') != phone_id:
            return False

        # Eliminar reminder
        key = {'pk': reminder['pk'], 'sk': reminder['sk']}
        self.repo.reminders.delete_conditional(key)
        
        return True
    
    def cancel_meeting(self,
                      appointment_id: int | None = None,
                      google_event_id: str | None = None,
                      cancel_reason: str | None = None) -> dict:
        """
        Cancela una cita: marca appointment.status='canceled', cancela en Google Calendar,
        y envía confirmación por WhatsApp.
        """
        from datetime import datetime, timezone
        from app.db.dynamo_client import now_ms
        
        # Buscar appointment
        appt = None
        if google_event_id:
            appt = self.repo.appointments.get_by_google_event_id(google_event_id)
        elif appointment_id:
            # appointment_id en Dynamo es el UUID, buscar por phone + uuid
            # Como no tenemos acceso directo, usamos google_event_id principalmente
            return {"error": "use_google_event_id", "message": "Usar google_event_id para cancelar."}
        
        if not appt:
            return {"error": "appointment_not_found", "message": "No encontré esa cita."}
        
        if appt.get('status') == 'canceled':
            return {"message": "Esta cita ya estaba cancelada."}
        
        try:
            # 1) Marcar como cancelado en BD
            key = {'pk': appt['pk'], 'sk': appt['sk']}
            update_expr = 'SET #st = :st, #rs = :rs, #ua = :ua'
            expr_attr_names = {
                '#st': 'status',
                '#rs': 'reminder_status',
                '#ua': 'updated_at'
            }
            expr_attr_values = {
                ':st': 'canceled',
                ':rs': 'skipped',
                ':ua': now_ms()
            }
            
            if cancel_reason:
                update_expr += ', #cr = :cr'
                expr_attr_names['#cr'] = 'cancel_reason'
                expr_attr_values[':cr'] = cancel_reason
            
            updated_appt = self.repo.appointments.update_conditional(
                key=key,
                update_expr=update_expr,
                expr_attr_names=expr_attr_names,
                expr_attr_values=expr_attr_values
            )
            
            # 2) Cancelar en Google Calendar
            if appt.get('google_event_id'):
                try:
                    self.calendar_api.cancel_event(
                        event_id=appt['google_event_id'],
                        calendar_id=appt.get('google_calendar_id')
                    )
                except Exception:
                    self.logger.exception("Error canceling in Google Calendar")
            
            # 3) Enviar confirmación por WhatsApp
            customer_phone = appt.get('customer_phone')
            if customer_phone:
                try:
                    from app.utils.whatsapp_utils import send_message, get_text_message_input
                    
                    # Convertir epoch_ms a datetime para formateo
                    starts_at_ms = appt.get('starts_at_epoch')
                    starts_dt = datetime.fromtimestamp(starts_at_ms / 1000, tz=timezone.utc)
                    
                    cancel_text = (
                        f"❌ Tu cita ha sido cancelada:\n\n"
                        f"📅 {starts_dt.strftime('%d/%m/%Y %H:%M')}\n"
                        f"📝 {appt.get('title', 'Cita')}"
                    )
                    if cancel_reason:
                        cancel_text += f"\n\nMotivo: {cancel_reason}"
                    
                    wa_response = send_message(get_text_message_input(customer_phone, cancel_text))
                    if wa_response and "messages" in wa_response:
                        # Actualizar cancel_confirm_wa_msg_id
                        confirm_msg_id = wa_response["messages"][0]["id"]
                        self.repo.appointments.update_conditional(
                            key=key,
                            update_expr='SET #ccid = :ccid',
                            expr_attr_names={'#ccid': 'cancel_confirm_wa_msg_id'},
                            expr_attr_values={':ccid': confirm_msg_id}
                        )
                except Exception:
                    self.logger.exception("Error sending WhatsApp confirmation")
            
            return {
                "appointment_id": appt.get('appointment_id'),
                "message": "Cita cancelada exitosamente."
            }
            
        except Exception as exc:
            self.logger.exception("cancel_meeting error")
            return {"error": str(exc)}
    
    def list_upcoming_appointments(self, customer_id: int | None = None) -> list:
        """
        Lista las próximas citas de un cliente (status='scheduled' AND starts_at >= now).
        """
        from datetime import datetime, timezone
        from app.db.dynamo_client import now_ms
        
        # En Dynamo no usamos customer_id numérico, usamos phone
        phone = self.current_phone
        if not phone:
            return []
        
        now_epoch = now_ms()
        appointments = self.repo.appointments.list_upcoming_by_customer(
            phone=phone,
            now_ms=now_epoch,
            limit=50
        )
        
        result = []
        for appt in appointments:
            # Adaptar formato Dynamo a formato esperado
            starts_ms = appt.get('starts_at_epoch')
            ends_ms = appt.get('ends_at_epoch')
            
            result.append({
                "appointment_id": appt.get('appointment_id'),
                "title": appt.get('title'),
                "starts_at": datetime.fromtimestamp(starts_ms / 1000, tz=timezone.utc).isoformat(),
                "ends_at": datetime.fromtimestamp(ends_ms / 1000, tz=timezone.utc).isoformat(),
                "description": appt.get('description', ''),
                "google_event_id": appt.get('google_event_id'),
                "google_meet_link": appt.get('google_meet_link')
            })
        
        return result
    
    # ------------------------------------------------------------------
    def get_weather(self, city: str = "Montevideo", units: str = "metric") -> dict:
        try:
            return get_forecast(city, units=units)
        except Exception:
            current_app.logger.exception("[get_weather] error")
            return {"error": "weather_unavailable"}

    # ------------------------------------------------------------------
    # Calendario: disponibilidad + agendado en GCal
    def schedule_meeting(self,
                         date: str,
                         duration_minutes: int = 60,
                         title: str = "Reunión",
                         calendar_id: str | None = None,
                         intent_wa_msg_id: str | None = None) -> dict:
        """
        Reserva slot en el Calendar si está libre, crea Appointment, y envía confirmación por WhatsApp.
        """
        import uuid
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo
        from app.config.settings import SETTINGS
        from app.db.dynamo_client import now_ms

        # Estandarizar título con nombre desde DB (ignorar el 'title' libre)
        db_name = None
        try:
            if self.current_phone:
                cust = CustomerService.get_by_phone(self.current_phone, repo_provider=self.repo)
                db_name = cust.get("name") if not cust.get("error") else None
        except Exception:
            db_name = None
        customer_name = db_name or (self.current_name or "Usuario")
        event_title = f"Reunión con {customer_name}"
        
        if not self.has_calendar:
            self.logger.error("Calendar service not initialized")
            raise RuntimeError("Servicio de calendario no disponible")
        
        try:
            # 0) Customer actual
            wa_id_var = self.current_phone
            if not wa_id_var:
                return {"error": "phone_required", "message": "No pude identificar tu número."}
            
            # Idempotencia: verificar si ya existe appointment con este intent_wa_msg_id
            if intent_wa_msg_id:
                existing = self.repo.appointments._get_by_intent_wa_msg_id(intent_wa_msg_id)
                if existing and existing.get('status') == 'scheduled':
                    starts_ms = existing.get('starts_at_epoch')
                    return {
                        "appointment_id": existing.get('appointment_id'),
                        "event_id": existing.get('google_event_id'),
                        "date": datetime.fromtimestamp(starts_ms / 1000, tz=timezone.utc).isoformat(),
                        "title": existing.get('title'),
                        "message": "Tu cita ya estaba agendada."
                    }

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

            # 2) Crear evento en Google Calendar
            cal_result = self.calendar_api.schedule_meeting(
                start_dt_str=date,
                title=event_title,
                duration_minutes=duration_minutes,
                wa_id=wa_id_var,
                calendar_id=calendar_id,
            )

            # 3) Crear Appointment en Dynamo
            local_tz = ZoneInfo(SETTINGS.TZ)
            starts_at = datetime.fromisoformat(cal_result["start_dt"]).replace(tzinfo=local_tz)
            ends_at = datetime.fromisoformat(cal_result["end_dt"]).replace(tzinfo=local_tz)
            
            starts_at_utc = starts_at.astimezone(timezone.utc)
            ends_at_utc = ends_at.astimezone(timezone.utc)
            starts_at_ms = int(starts_at_utc.timestamp() * 1000)
            ends_at_ms = int(ends_at_utc.timestamp() * 1000)
            
            # Crear UUID para el appointment
            appt_uuid = str(uuid.uuid4())
            
            # Data para appointment
            appt_data = {
                'title': event_title,
                'description': f"Reunión agendada vía WhatsApp con {customer_name}",
                'ends_at': ends_at_ms,
                'timezone': str(local_tz),
                'status': 'scheduled',
                'google_calendar_id': cal_result.get("calendar_id"),
                'google_event_id': cal_result["event_id"],
                'google_meet_link': cal_result.get("meet_link"),
                'remind_before_min': SETTINGS.EVENT_ADVANCE_MINUTES,
                'reminder_status': 'pending',
                'source': 'wa'
            }
            
            appt = self.repo.appointments.create_if_absent(
                phone=wa_id_var,
                starts_at_ms=starts_at_ms,
                uuid=appt_uuid,
                data=appt_data,
                intent_wa_msg_id=intent_wa_msg_id
            )
            
            # 4) Enviar confirmación por WhatsApp
            from app.utils.whatsapp_utils import send_message, get_text_message_input
            confirm_text = (
                f"✅ Listo! Tu cita está agendada:\n\n"
                f"📅 Fecha: {starts_at.strftime('%d/%m/%Y %H:%M')}\n"
                f"⏱️ Duración: {duration_minutes} minutos\n"
                f"📝 {event_title}\n\n"
                f"Te recordaré {SETTINGS.EVENT_ADVANCE_MINUTES} minutos antes."
            )
            if cal_result.get("meet_link"):
                confirm_text += f"\n\n🔗 Link de Meet: {cal_result['meet_link']}"
            
            try:
                wa_response = send_message(get_text_message_input(wa_id_var, confirm_text))
                if wa_response and "messages" in wa_response:
                    confirm_msg_id = wa_response["messages"][0]["id"]
                    # Actualizar confirm_wa_msg_id
                    key = {'pk': appt['pk'], 'sk': appt['sk']}
                    self.repo.appointments.update_conditional(
                        key=key,
                        update_expr='SET #cid = :cid',
                        expr_attr_names={'#cid': 'confirm_wa_msg_id'},
                        expr_attr_values={':cid': confirm_msg_id}
                    )
            except Exception:
                self.logger.exception("Error sending WhatsApp confirmation")

            return {
                "appointment_id": appt.get('appointment_id'),
                "event_id": cal_result["event_id"],
                "date": date,
                "title": event_title,
                "message": "Cita agendada exitosamente"
            }

        except Exception as exc:
            self.logger.exception("schedule_meeting error")
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
        # 1) Normalizar fecha_hora -> datetime (o None)
        if fecha_hora:
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
            """
            Cancela un mensaje programado pendiente.
            En Dynamo, buscamos por customer y filtramos por wa_msg_id de confirmación.
            """
            from app.db.dynamo_keys import pk_customer
            from app.db.dynamo_client import now_ms
            
            target_msg_id = context_id or wa_msg_id
            if not target_msg_id:
                return False

            # Buscar mensajes programados del customer (status=pending)
            # Como no hay GSI por wa_msg_id, necesitamos query por customer
            try:
                pending_messages = self.repo.scheduled_messages.query(
                    key_condition_expr='#pk = :pk AND begins_with(#sk, :sk_prefix)',
                    expr_attr_names={
                        '#pk': 'pk',
                        '#sk': 'sk',
                        '#st': 'status'
                    },
                    expr_attr_values={
                        ':pk': pk_customer(phone_id),
                        ':sk_prefix': 'SM#',
                        ':st': 'pending'
                    },
                    filter_expr='#st = :st',
                    limit=100
                )
                
                # Filtrar por wa_msg_id (mensaje de confirmación)
                sm = None
                for msg in pending_messages:
                    if msg.get('wa_msg_id') == target_msg_id:
                        sm = msg
                        break
                
                if not sm:
                    return False

                # Eliminar el mensaje
                key = {'pk': sm['pk'], 'sk': sm['sk']}
                self.repo.scheduled_messages.delete_conditional(key)
                
                return True
                
            except Exception as e:
                self.logger.exception("cancel_scheduled_message error")
                return False
