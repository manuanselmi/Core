# app/utils/datetime_utils.py
from datetime import datetime,date

def parse_iso8601(date_str: str) -> datetime:
    """
    Intenta parsear cadenas ISO-8601 con o sin zona horaria,
    o bien formatos 'YYYY-MM-DD HH:MM:SS' y 'YYYY-MM-DD'.
    """
    try:
        # Python 3.7+: soporta 'YYYY-MM-DD' o 'YYYY-MM-DDTHH:MM:SS'
        return datetime.fromisoformat(date_str)
    except ValueError:
        # Intenta con espacios y segundos
        try:
            return datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            # Solo fecha
            try:
                return datetime.strptime(date_str, "%Y-%m-%d")
            except ValueError:
                # Solo hora HH:MM → asumimos fecha de hoy
                try:
                    t = datetime.strptime(date_str, "%H:%M").time()
                    today = date.today()
                    return datetime.combine(today, t)
                except ValueError:
                    # Ningún formato válido
                    raise ValueError(f"Formato de fecha inválido: {date_str}")
