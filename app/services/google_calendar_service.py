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
    current_app.logger.info(f"[GoogleCalendarService] _resolve_calendar_id: wa_id='{wa}', special_waid_normalizado='{special}'")
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
        current_app.logger.info("[APPOINTMENT] Searching free slots: date=%s, start=%s, end=%s, duration=%d minutes, user=%s",
                  date_str, start_time, end_time, slot_minutes, wa_id)
        
        # 1️⃣  Normalizamos la ventana de búsqueda
        try:
            day = datetime.fromisoformat(date_str).replace(tzinfo=LOCAL_TZ)
            
            # Aseguramos que la fecha no sea en el pasado
            now = datetime.now(LOCAL_TZ)
            if day.date() < now.date():
                current_app.logger.warning("[APPOINTMENT] Date in past, adjusting to today: %s -> %s", 
                                day.date(), now.date())
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
                _logger().info("[GoogleCalendarService] Ajustando t_max para crear ventana válida: %s -> %s",
                            end_time, t_max.strftime("%H:%M"))
            else:
                t_max = (
                    datetime.combine(day.date(), datetime.strptime(end_time, "%H:%M").time(), tzinfo=LOCAL_TZ)
                    if end_time else day.replace(hour=20, minute=0)
                )
            
            # Validamos que t_min sea menor que t_max
            if t_min >= t_max:
                _logger().error("[GoogleCalendarService] Ventana de tiempo inválida: t_min >= t_max (%s >= %s)",
                              t_min.isoformat(), t_max.isoformat())
                return []
                
            # Si t_min está en el pasado, lo ajustamos al presente
            if t_min < now:
                _logger().warning("[GoogleCalendarService] Hora de inicio en el pasado, ajustando a ahora: %s -> %s",
                                t_min.isoformat(), now.isoformat())
                t_min = now
                
            _logger().debug("[GoogleCalendarService] Ventana de búsqueda normalizada: t_min=%s, t_max=%s",
                          t_min.isoformat(), t_max.isoformat())
                          
        except ValueError as e:
            _logger().error("[GoogleCalendarService] Error parseando fechas: %s", str(e))
            return []
        except Exception as e:
            _logger().error("[GoogleCalendarService] Error inesperado normalizando ventana de tiempo: %s", str(e))
            return []

        # 2️⃣  Elegimos el calendario (explícito > especial por WAID > default)
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        _logger().info("[GoogleCalendarService] freebusy wa_id=%s -> calendar_id=%s", _norm_waid(wa_id), cal_id)

        # 3️⃣  Pedimos a Calendar los eventos ocupados
        if not self.service:
            _logger().error("[GoogleCalendarService] No hay servicio de calendario inicializado")
            return []
            
        try:
            _logger().debug("[GoogleCalendarService] Consultando freebusy API: timeMin=%s, timeMax=%s, calendar=%s",
                          t_min.isoformat(), t_max.isoformat(), cal_id)
            fb = self.service.freebusy().query(
                body={
                    "timeMin": t_min.isoformat(),
                    "timeMax": t_max.isoformat(),
                    "items": [{"id": cal_id}],
                }
            ).execute()
            busy = fb["calendars"][cal_id]["busy"]
            current_app.logger.info("[APPOINTMENT] Found %d busy slots", len(busy))
            current_app.logger.debug("[APPOINTMENT] Busy slots detail: %s", busy)
        except Exception as e:
            current_app.logger.error("[APPOINTMENT] Error checking freebusy: %s", str(e))
            return []

        # 4️⃣  Construimos la lista de huecos libres
        pointer = t_min
        free: List[Dict] = []
        current_app.logger.debug("[APPOINTMENT] Analyzing time slots between busy periods")
        for b in busy:
            start_b = datetime.fromisoformat(b["start"]).astimezone(LOCAL_TZ)
            if pointer + timedelta(minutes=slot_minutes) <= start_b:
                free.append({
                    "start": pointer.isoformat(timespec="minutes"),
                    "end": (pointer + timedelta(minutes=slot_minutes)).isoformat(timespec="minutes"),
                })
            pointer = max(pointer, datetime.fromisoformat(b["end"]).astimezone(LOCAL_TZ))
        # Add remaining slots after last busy period
        current_app.logger.debug("[APPOINTMENT] Adding slots after last busy period")
        while pointer + timedelta(minutes=slot_minutes) <= t_max:
            free.append({
                "start": pointer.isoformat(timespec="minutes"),
                "end": (pointer + timedelta(minutes=slot_minutes)).isoformat(timespec="minutes"),
            })
            pointer += timedelta(minutes=slot_minutes)
        return free

    # ---------- Creación de evento ----------
    def schedule_meeting(
        self,
        start_dt_str: str,
        title: str,
        duration_minutes: int = 60,
        description: str = "",
        wa_id: str | None = None,
        calendar_id: str | None = None,
    ) -> str:
        """Crea un evento y notifica por WhatsApp a Lucas. Devuelve el `eventId`."""
        current_app.logger.info("[APPOINTMENT] Scheduling meeting: start=%s, title=%s, duration=%d minutes, user=%s",
                      start_dt_str, title, duration_minutes, wa_id)
        
        if not self.service:
            current_app.logger.error("[APPOINTMENT] Calendar service not initialized")
            raise RuntimeError("Calendar service not initialized")
            
        start_dt = datetime.fromisoformat(start_dt_str).replace(tzinfo=LOCAL_TZ)
        end_dt = start_dt + timedelta(minutes=duration_minutes)
        current_app.logger.debug("[APPOINTMENT] Normalized time: start=%s, end=%s",
                      start_dt.isoformat(), end_dt.isoformat())

        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
            "end":   {"dateTime": end_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
        }
        current_app.logger.debug("[APPOINTMENT] Event payload: %s", json.dumps(body))

        # Select calendar (explicit > special by WAID > default) - consistent with freebusy
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        try:
            event = self.service.events().insert(calendarId=cal_id, body=body).execute()
            current_app.logger.info("[APPOINTMENT] Event created successfully: id=%s, user=%s, calendar=%s",
                          event.get("id"), _norm_waid(wa_id), cal_id)
        except Exception as e:
            current_app.logger.error("[APPOINTMENT] Error creating event: %s", str(e))
            raise

        # WhatsApp a Lucas (si está configurado)
        if LUCAS_WAID and _norm_waid(wa_id) != "59893944122":
            from app.utils.whatsapp_utils import send_message, get_text_message_input
            text = f"📅 Nueva reunión: “{title}” — {start_dt.strftime('%d/%m/%Y %H:%M')} - si queres contactarte con la persona, aquí está su número: {wa_id}"
            try:
                send_message(get_text_message_input(LUCAS_WAID, text))
                current_app.logger.info("[APPOINTMENT] WhatsApp notification sent to admin: %s", text)
            except Exception:
                current_app.logger.exception("[APPOINTMENT] Failed to send WhatsApp notification to admin")

        return event["id"]
