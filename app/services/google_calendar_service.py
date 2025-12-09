from __future__ import annotations

import os
import re
import logging
from datetime import datetime, timedelta, timezone
from typing import List, Dict
import app
from google.oauth2 import service_account
from googleapiclient.discovery import build
from flask import current_app
import json
 
def _logger():
    """Devuelve el logger de Flask si hay contexto; si no, un logger estándar."""
    logger = logging.getLogger("GoogleCalendarService")
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setLevel(logging.DEBUG)
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger

# ---------------- Config ----------------
SCOPES = ["https://www.googleapis.com/auth/calendar"]
SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")   # Ruta al JSON de credenciales
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")          # ID del calendario por defecto (fallback a 'primary')
LUCAS_WAID = os.getenv("LUCAS_WAID")                              # Teléfono de Lucas (598… sin “+”)
SPECIAL_WAID = os.getenv("SPECIAL_WAID")                          # Teléfono del usuario especial (598… sin “+”)
GOOGLE_CALENDAR_ID_SPECIAL = os.getenv("GOOGLE_CALENDAR_ID_SPECIAL")  # ID del calendario especial

LOCAL_TZ = timezone(timedelta(hours=-3))  # America/Montevideo

# ---------------- Helpers ----------------
def _norm_waid(wa_id: str | None) -> str | None:
    """Normaliza a solo dígitos (maneja '+598...', espacios, etc.)."""
    if not wa_id:
        return None
    return re.sub(r"\D", "", str(wa_id))

def _resolve_calendar_id(wa_id: str | None, explicit_calendar_id: str | None = None) -> str:
    """
    Selecciona el calendarId:
    1) Si viene explícito, usarlo.
    2) Si el wa_id normalizado == SPECIAL_WAID normalizado -> usar GOOGLE_CALENDAR_ID_SPECIAL.
    3) Si no, usar CALENDAR_ID (o 'primary').
    """
    if explicit_calendar_id:
        return explicit_calendar_id
    wa = _norm_waid(wa_id)
    special = _norm_waid(SPECIAL_WAID)
    if wa and special and GOOGLE_CALENDAR_ID_SPECIAL and wa == special:
        return GOOGLE_CALENDAR_ID_SPECIAL
    return CALENDAR_ID

class GoogleCalendarService:
    """Wrapper mínimo para *free/busy* y creación de os con ruteo por WAID."""

    def _init_client(self, service_file: str | None, sa_json: str | None):
        SCOPES = ["https://www.googleapis.com/auth/calendar"]
        creds = None
        if service_file and os.path.exists(service_file):
            creds = service_account.Credentials.from_service_account_file(
                service_file, scopes=SCOPES
            )
        elif sa_json:
            info = json.loads(sa_json)
            creds = service_account.Credentials.from_service_account_info(
                info, scopes=SCOPES
            )
        else:
            # Feature gate: Google opcional si no hay credenciales
            raise RuntimeError("Falta GOOGLE_SERVICE_ACCOUNT_FILE o GOOGLE_SA_JSON")

        delegated = os.getenv("GOOGLE_DELEGATED_USER")
        if delegated:
            creds = creds.with_subject(delegated)

        self.service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        self.client = self.service  # alias para compatibilidad

    def __init__(self):
        service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        sa_json = os.getenv("GOOGLE_SA_JSON")
        try:
            self._init_client(service_file, sa_json)
        except Exception as e:
            logging.error(f"Error inicializando GoogleCalendarService: {e}")
            _logger().error(f"Error inicializando GoogleCalendarService: {e}")
            self.service = None
            
    # ---------- Disponibilidad ----------
    def get_free_slots(
        self,
        date_str: str,
        start_time: str | None = None,
        end_time: str | None = None,
        slot_minutes: int = 60,
        wa_id: str | None = None,
        calendar_id: str | None = None,
    ) -> List[Dict]:
        """Devuelve bloques libres de `slot_minutes` min en la fecha dada."""
        
        # 1️⃣  Normalizamos la ventana de búsqueda
        try:
            day = datetime.fromisoformat(date_str).replace(tzinfo=LOCAL_TZ)
            
            # Aseguramos que la fecha no sea en el pasado
            now = datetime.now(LOCAL_TZ)
            if day.date() < now.date():
                day = now            # Normalizamos horarios de inicio y fin
            base_date = day.date()
            if end_time and datetime.strptime(end_time, "%H:%M").time() < datetime.strptime(start_time or "08:00", "%H:%M").time():
                # Si el fin es antes que el inicio, asumimos que es para el día siguiente
                base_date = base_date + timedelta(days=1)
            # Normalizamos horarios de inicio y fin
            t_min = (
                datetime.combine(day.date(), datetime.strptime(start_time, "%H:%M").time(), tzinfo=LOCAL_TZ)
                if start_time else day.replace(hour=8, minute=0)
            )
            
            # Si end_time es igual a start_time, ajustamos para que sea slot_minutes después
            if end_time and end_time == start_time:
                t_max = t_min + timedelta(minutes=slot_minutes)
            else:
                t_max = (
                    datetime.combine(day.date(), datetime.strptime(end_time, "%H:%M").time(), tzinfo=LOCAL_TZ)
                    if end_time else day.replace(hour=20, minute=0)
                )
            
            # Validamos que t_min sea menor que t_max
            if t_min >= t_max:
                _logger().error("[GoogleCalendarService] Invalid time window: t_min >= t_max")
                return []
                
            # Si t_min está en el pasado, lo ajustamos al presente
            if t_min < now:
                t_min = now
                          
        except ValueError as e:
            _logger().error("[GoogleCalendarService] Error parsing dates: %s", str(e))
            return []
        except Exception as e:
            _logger().error("[GoogleCalendarService] Unexpected error: %s", str(e))
            return []

        # 2️⃣  Elegimos el calendario (explícito > especial por WAID > default)
        cal_id = _resolve_calendar_id(wa_id, calendar_id)

        # 3️⃣  Pedimos a Calendar los eventos ocupados
        if not self.service:
            _logger().error("[GoogleCalendarService] Calendar service not initialized")
            return []
            
        try:
            fb = self.service.freebusy().query(
                body={
                    "timeMin": t_min.isoformat(),
                    "timeMax": t_max.isoformat(),
                    "items": [{"id": cal_id}],
                }
            ).execute()
            busy = fb["calendars"][cal_id]["busy"]
        except Exception as e:
            current_app.logger.error("[APPOINTMENT] Error checking freebusy: %s", str(e))
            return []

        # 4️⃣  Construimos la lista de huecos libres
        pointer = t_min
        free: List[Dict] = []
        for b in busy:
            start_b = datetime.fromisoformat(b["start"]).astimezone(LOCAL_TZ)
            if pointer + timedelta(minutes=slot_minutes) <= start_b:
                free.append({
                    "start": pointer.isoformat(timespec="minutes"),
                    "end": (pointer + timedelta(minutes=slot_minutes)).isoformat(timespec="minutes"),
                })
            pointer = max(pointer, datetime.fromisoformat(b["end"]).astimezone(LOCAL_TZ))
        # Add remaining slots after last busy period
        while pointer + timedelta(minutes=slot_minutes) <= t_max:
            free.append({
                "start": pointer.isoformat(timespec="minutes"),
                "end": (pointer + timedelta(minutes=slot_minutes)).isoformat(timespec="minutes"),
            })
            pointer += timedelta(minutes=slot_minutes)
        return free

    # ---------- Creación de evento ----------
    def is_slot_free(
        self,
        start_dt: datetime,
        end_dt: datetime,
        wa_id: str | None = None,
        calendar_id: str | None = None,
    ) -> bool:
        """Verifica si un slot específico está libre usando freebusy. Retorna True si está completamente libre."""
        if not self.service:
            _logger().error("[GoogleCalendarService] Calendar service not initialized")
            return False
        
        # Resolver calendar_id usando la misma lógica que schedule_meeting
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        
        try:
            fb = self.service.freebusy().query(
                body={
                    "timeMin": start_dt.isoformat(),
                    "timeMax": end_dt.isoformat(),
                    "items": [{"id": cal_id}],
                }
            ).execute()
            
            busy_periods = fb["calendars"][cal_id]["busy"]
            
            # Si hay cualquier periodo busy que se solape, el slot NO está libre
            if busy_periods:
                _logger().info(
                    "[GoogleCalendarService] Slot ocupado: %s - %s (calendar: %s, busy: %d)",
                    start_dt.isoformat(),
                    end_dt.isoformat(),
                    cal_id,
                    len(busy_periods)
                )
                return False
            
            _logger().info(
                "[GoogleCalendarService] Slot libre: %s - %s (calendar: %s)",
                start_dt.isoformat(),
                end_dt.isoformat(),
                cal_id
            )
            return True
            
        except Exception as e:
            _logger().exception(
                "[GoogleCalendarService] Error verificando disponibilidad: %s",
                str(e)
            )
            # En caso de error, asumimos ocupado por seguridad
            return False
    
    def schedule_meeting(
        self,
        start_dt_str: str,
        title: str,
        duration_minutes: int = 60,
        description: str = "",
        wa_id: str | None = None,
        calendar_id: str | None = None,
    ) -> dict:
        """Crea un evento y notifica por WhatsApp a Lucas. Devuelve dict con event_id, calendar_id, start_dt, end_dt, meet_link."""
        
        if not self.service:
            current_app.logger.error("[APPOINTMENT] Calendar service not initialized")
            raise RuntimeError("Calendar service not initialized")
            
        start_dt = datetime.fromisoformat(start_dt_str).replace(tzinfo=LOCAL_TZ)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        
        # CRITICAL: Verificar que el slot esté libre ANTES de crear el evento
        if not self.is_slot_free(start_dt, end_dt, wa_id, calendar_id):
            raise RuntimeError(
                f"Slot no disponible: {start_dt.isoformat()} - {end_dt.isoformat()}. "
                "Ya existe otro evento en ese horario."
            )

        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
            "end":   {"dateTime": end_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
        }

        # Select calendar (explicit > special by WAID > default) - consistent with freebusy
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        try:
            event = self.service.events().insert(calendarId=cal_id, body=body).execute()
        except Exception as e:
            current_app.logger.error("[APPOINTMENT] Error creating event: %s", str(e))
            raise

        # WhatsApp a Lucas (si está configurado)
        if LUCAS_WAID and _norm_waid(wa_id) != "59893944122":
            from app.utils.whatsapp_utils import send_message, get_text_message_input
            text = f"📅 Nueva reunión: \"{title}\" — {start_dt.strftime('%d/%m/%Y %H:%M')} - si queres contactarte con la persona, aquí está su número: {wa_id}"
            try:
                send_message(get_text_message_input(LUCAS_WAID, text))
            except Exception:
                current_app.logger.exception("[APPOINTMENT] Failed to send WhatsApp notification to admin")

        return {
            "event_id": event["id"],
            "calendar_id": cal_id,
            "start_dt": start_dt.isoformat(),
            "end_dt": end_dt.isoformat(),
            "meet_link": event.get("hangoutLink") or event.get("conferenceData", {}).get("entryPoints", [{}])[0].get("uri")
        }
    
    # ---------- Cancelación de evento ----------
    def cancel_event(self, event_id: str, calendar_id: str | None = None) -> bool:
        """
        Cancela un evento en Google Calendar. Retorna True si se canceló, False si no existe.
        
        Args:
            event_id: ID del evento en Google Calendar (requerido)
            calendar_id: ID del calendario. Si es None, usa CALENDAR_ID por defecto.
        
        Returns:
            True si se canceló exitosamente, False si el evento no existe (404) o hay error
            
        Notes:
            - CRÍTICO: Siempre pasar calendar_id correcto desde el appointment
            - El calendar_id debe coincidir con el usado al crear el evento
            - NUNCA lanza excepciones; retorna False en caso de error
            - Log exhaustivo para diagnóstico
        """
        if not self.service:
            _logger().error("[GoogleCalendarService] ✗ No hay servicio de calendario inicializado")
            return False
        
        # Validar que tenemos event_id
        if not event_id:
            _logger().error("[GoogleCalendarService] ✗ cancel_event llamado sin event_id")
            return False
        
        # Usar calendar_id pasado o fallback a default
        cal_id = calendar_id or CALENDAR_ID
        
        # Log DEBUG con IDs completos antes de la operación
        _logger().debug(
            "[GoogleCalendarService] cancel_event → event_id=%s, calendar_id=%s (explicit=%s, default=%s)",
            event_id,
            cal_id,
            calendar_id,
            CALENDAR_ID
        )
        
        try:
            self.service.events().delete(calendarId=cal_id, eventId=event_id).execute()
            _logger().info(
                "[GoogleCalendarService] ✓ Evento cancelado exitosamente: event_id=%s, calendar_id=%s",
                event_id,
                cal_id
            )
            return True
            
        except Exception as e:
            error_str = str(e)
            error_str_lower = error_str.lower()
            error_type = type(e).__name__
            
            # Caso esperado: evento no existe (404 o notFound)
            if "404" in error_str_lower or "not found" in error_str_lower or "notfound" in error_str_lower:
                _logger().info(
                    "[GoogleCalendarService] Evento no encontrado (404): event_id=%s, calendar_id=%s - probablemente ya fue borrado. error_type=%s",
                    event_id,
                    cal_id,
                    error_type
                )
                return False
            
            # Otros errores (auth, network, permission, etc.)
            _logger().error(
                "[GoogleCalendarService] ✗ Error cancelando evento: type=%s event_id=%s calendar_id=%s error=%s",
                error_type,
                event_id,
                cal_id,
                error_str
            )
            # CRÍTICO: No re-raise para no romper UX (Dynamo ya tiene estado correcto)
            return False
    
    # ---------- Obtención de evento ----------
    def get_event(self, event_id: str, calendar_id: str | None = None) -> dict | None:
        """Obtiene un evento de Google Calendar. Retorna dict o None si no existe."""
        if not self.service:
            _logger().error("[GoogleCalendarService] No hay servicio de calendario inicializado")
            return None
        
        cal_id = calendar_id or CALENDAR_ID
        try:
            event = self.service.events().get(calendarId=cal_id, eventId=event_id).execute()
            return event
        except Exception as e:
            if "404" in str(e) or "not found" in str(e).lower():
                return None
            _logger().exception("[GoogleCalendarService] Error obteniendo evento: event_id=%s", event_id)
            return None
