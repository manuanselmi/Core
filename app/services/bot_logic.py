import os
import requests
import re
from datetime import datetime, date
import logging


logging.basicConfig(level=logging.INFO)
class BotLogic:
    """
    Lógica local para manejo de consultas de fecha:
     - validate_date_format: comprueba si el mensaje es una fecha válida DD/MM/YYYY.
     - get_day_of_date: devuelve el día de la semana para fecha YYYY-MM-DD.
     - calculate_days_until: calcula días hasta fecha YYYY-MM-DD.
    """
    def validate_date_format(self, message: str) -> bool:
        """
        Verifica si el texto coincide con DD/MM/YYYY.
        """
        try:
            datetime.strptime(message.strip(), "%d/%m/%Y")
            return True
        except ValueError:
            return False

    def get_day_of_date(self, message: str) -> str | None:
        """
        Si el mensaje incluye 'qué día cae' y una fecha YYYY-MM-DD, devuelve texto descriptivo.
        """
        texto = message.lower()
        if not re.search(r"\b(qué día cae|que día cae|día cae)\b", texto):
            return None
        match = re.search(r"(\d{4}-\d{2}-\d{2})", message)
        if not match:
            return None
        date_str = match.group(1)
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            return None
        traducciones = {
            'Monday': 'lunes', 'Tuesday': 'martes', 'Wednesday': 'miércoles',
            'Thursday': 'jueves', 'Friday': 'viernes',
            'Saturday': 'sábado', 'Sunday': 'domingo'
        }
        day_es = traducciones.get(dt.strftime('%A'), dt.strftime('%A').lower())
        return f"El día {date_str} cae en {day_es}."

    def calculate_days_until(self, message: str) -> str | None:
        """
        Si el mensaje lleva 'cuántos días faltan' o similar y una fecha YYYY-MM-DD, devuelve texto.
        """
        texto = message.lower()
        if not re.search(r"\b(cuántos días faltan|cuantos dias faltan|faltan)\b", texto):
            return None
        match = re.search(r"(\d{4}-\d{2}-\d{2})", message)
        if not match:
            return None
        date_str = match.group(1)
        try:
            target = datetime.strptime(date_str, "%Y-%m-%d").date()
        except ValueError:
            return None
        today = date.today()
        delta = (target - today).days
        if delta > 0:
            return f"Faltan {delta} días para {date_str}."
        elif delta == 0:
            return f"¡Es hoy, {date_str}!"
        else:
            return f"La fecha {date_str} ya pasó hace {-delta} días."
        
    def search_weather_serpapi(query: str) -> str:
        api_key = os.getenv("SERPAPI_API_KEY")
        params = {
            "engine": "google",
            "q": query,
            "location": "Montevideo, Uruguay",
            "hl": "es",
            "api_key": api_key
        }
        response = requests.get("https://serpapi.com/search", params=params)
        logging.info(f"SerpAPI HTTP {response.status_code} — {response.url}")
        
        if response.status_code != 200:
            logging.error(f"Error SerpAPI: {response.text}")
            return "No pude obtener información del clima (falló la llamada)."
        
        data = response.json()
        logging.debug(f"SerpAPI raw JSON: {data}")
        
        # 1️⃣ Intentar cajón de respuestas directas
        if "answer_box" in data and "answer" in data["answer_box"]:
            return data["answer_box"]["answer"]
        
        # 2️⃣ Intentar snippet orgánico
        if data.get("organic_results"):
            first = data["organic_results"][0]
            if "snippet" in first:
                return first["snippet"]
        
        # 3️⃣ Intentar sección de knowledge graph
        if "knowledge_graph" in data and "description" in data["knowledge_graph"]:
            return data["knowledge_graph"]["description"]
        
        # 4️⃣ Fallback informativo
        logging.warning("Estructura JSON inesperada, sin datos de clima.")
        return "No encontré datos claros sobre el clima justo ahora."
    def formatea_clima(texto_raw: str, lugar: str) -> str:
        if "°" in texto_raw:
            primera = texto_raw.split("·")[0].strip()
            temp_actual = primera.split()[-1]
            condicion = " ".join(primera.split()[2:-1])
            return (f"En {lugar.title()} ahora está {condicion.lower()} y hay unos {temp_actual}.")
        return f"Según lo que encontré, en {lugar.title()}: {texto_raw}"

    # en app/services/bot_logic.py (o donde manejes la intención de "crear cita")
    def handle_create_appointment(user_text: str) -> dict | str:
        """
        Si el usuario pide crear una cita, devuelve:
        { "title": "<texto de confirmación>",
            "date":  "YYYY-MM-DD HH:MM" }
        En cualquier otro caso, devuelve un string de respuesta normal.
        """
        if "crear cita" in user_text.lower():
            # parseas la fecha/hora del texto o la pides en pasos anteriores
            fecha = "2025-07-12 14:00"
            titulo = "Tu cita está confirmada"
            return {"title": titulo, "date": fecha}
        # si no, caes al fallback de ChatGPT u otro handler
        return "Lo siento, no entendí tu mensaje."
