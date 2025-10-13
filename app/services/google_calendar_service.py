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
 
def _logger():
    """Devuelve el logger de Flask si hay contexto; si no, un logger estándar."""
    try:
        return current_app.logger  # type: ignore[attr-defined]
    except Exception:
        return logging.getLogger("GoogleCalendarService")

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

    def __init__(self):
        service_file = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
        if not service_file:
            # Feature gate: Google opcional
            logging.warning("Google Calendar desactivado: falta GOOGLE_SERVICE_ACCOUNT_FILE.")
            self.client = None
            return
        try:
            self._init_client(service_file)
        except Exception as e:
            logging.error(f"Error inicializando GoogleCalendarService: {e}")
            _logger().error(f"Error inicializando GoogleCalendarService: {e}")
            self.client = None
            
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
        day = datetime.fromisoformat(date_str).replace(tzinfo=LOCAL_TZ)
        t_min = (
            datetime.combine(day.date(), datetime.strptime(start_time, "%H:%M").time(), tzinfo=LOCAL_TZ)
            if start_time else day.replace(hour=8, minute=0)
        )
        t_max = (
            datetime.combine(day.date(), datetime.strptime(end_time, "%H:%M").time(), tzinfo=LOCAL_TZ)
            if end_time else day.replace(hour=20, minute=0)
        )

        # 2️⃣  Elegimos el calendario (explícito > especial por WAID > default)
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        _logger().info("[GoogleCalendarService] freebusy wa_id=%s -> calendar_id=%s", _norm_waid(wa_id), cal_id)

        # 3️⃣  Pedimos a Calendar los eventos ocupados
        fb = self.service.freebusy().query(
            body={
                "timeMin": t_min.isoformat(),
                "timeMax": t_max.isoformat(),
                "items": [{"id": cal_id}],
            }
        ).execute()
        busy = fb["calendars"][cal_id]["busy"]

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
        # Después del último ocupado
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
        start_dt = datetime.fromisoformat(start_dt_str).replace(tzinfo=LOCAL_TZ)
        end_dt = start_dt + timedelta(minutes=duration_minutes)

        body = {
            "summary": title,
            "description": description,
            "start": {"dateTime": start_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
            "end":   {"dateTime": end_dt.isoformat(), "timeZone": str(LOCAL_TZ)},
        }

        # Selección de calendario (explícito > especial por WAID > default) — consistente con freebusy
        cal_id = _resolve_calendar_id(wa_id, calendar_id)
        event = self.service.events().insert(calendarId=cal_id, body=body).execute()
        _logger().info("[GoogleCalendarService] insert wa_id=%s -> calendar_id=%s", _norm_waid(wa_id), cal_id)

        # WhatsApp a Lucas (si está configurado)
        if LUCAS_WAID and _norm_waid(wa_id) != "59893944122":
            from app.utils.whatsapp_utils import send_message, get_text_message_input
            text = f"📅 Nueva reunión: “{title}” — {start_dt.strftime('%d/%m/%Y %H:%M')} - si queres contactarte con la persona, aquí está su número: {wa_id}"
            try:
                send_message(get_text_message_input(LUCAS_WAID, text))
                current_app.logger.info("[GoogleCalendarService] WhatsApp enviado a Lucas: %s", text)
                logging.info("[GoogleCalendarService] WhatsApp enviado a Lucas")
            except Exception:
                logging.exception("[GoogleCalendarService] No se pudo enviar WhatsApp a Lucas")

        return event["id"]
    
