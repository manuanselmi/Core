# app/utils/datetime_normalizer.py
import os, re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

try:
    from dateparser.search import search_dates
    import dateparser  # noqa: F401
except Exception:
    search_dates = None

_NUM_WORDS = {
    "cero": 0, "un": 1, "uno": 1, "una": 1, "dos": 2, "tres": 3, "cuatro": 4, "cinco": 5,
    "seis": 6, "siete": 7, "ocho": 8, "nueve": 9, "diez": 10, "once": 11, "doce": 12,
    "trece": 13, "catorce": 14, "quince": 15, "veinte": 20, "treinta": 30, "cuarenta": 40,
    "cincuenta": 50, "sesenta": 60, "setenta": 70, "ochenta": 80, "noventa": 90
}

def _word_to_int(token: str) -> int | None:
    token = token.strip().lower()
    if token in _NUM_WORDS: return _NUM_WORDS[token]
    # “veinticinco”, “treintaycinco” (tolerante)
    m = re.match(r"(veinte|treinta|cuarenta|cincuenta|sesenta|setenta|ochenta|noventa)(y)?(uno|dos|tres|cuatro|cinco|seis|siete|ocho|nueve)", token)
    if m:
        tens = {"veinte":20,"treinta":30,"cuarenta":40,"cincuenta":50,"sesenta":60,"setenta":70,"ochenta":80,"noventa":90}[m.group(1)]
        ones = _NUM_WORDS[m.group(3)]
        return tens + ones
    return None

def _bump_future(dt: datetime, now: datetime, min_future_seconds: int = 30) -> datetime:
    # Nunca en el pasado/instante
    if dt <= now:
        dt = now + timedelta(seconds=max(min_future_seconds, 60))
    return dt

# Reglas rápidas para relativos
_RELATIVE_RULES = [
    # en/dentro de X min
    (re.compile(r"\b(?:en|dentro\s+de)\s+(\d+|[a-záéíóú]+)\s*(?:m|min|mins|minuto|minutos)\b", re.I),
     lambda n, now: now + timedelta(minutes=int(n))),
    # en/dentro de X h
    (re.compile(r"\b(?:en|dentro\s+de)\s+(\d+|[a-záéíóú]+)\s*(?:h|hs|hora|horas)\b", re.I),
     lambda n, now: now + timedelta(hours=int(n))),
    # en/dentro de X d(ías)
    (re.compile(r"\b(?:en|dentro\s+de)\s+(\d+|[a-záéíóú]+)\s*d(?:ía|ias)?\b", re.I),
     lambda n, now: now + timedelta(days=int(n))),
    # en media hora / en un rato
    (re.compile(r"\b(?:en|dentro\s+de)\s+media\s+hora\b", re.I),
     lambda _n, now: now + timedelta(minutes=30)),
    (re.compile(r"\b(?:en|dentro\s+de)\s+un\s+rato\b", re.I),
     lambda _n, now: now + timedelta(minutes=30)),
    # en 1 h y media / en 1 hora y media
    (re.compile(r"\b(?:en|dentro\s+de)\s+(\d+|[a-záéíóú]+)\s*(?:h|hs|hora|horas)\s*y\s+media\b", re.I),
     lambda n, now: now + timedelta(hours=int(n), minutes=30)),
    # sin unidad → asumimos minutos (útil para “en 5 de bañeme”)
    (re.compile(r"\b(?:en|dentro\s+de)\s+(\d+|[a-záéíóú]+)\b", re.I),
     lambda n, now: now + timedelta(minutes=int(n))),
]

def _coerce_number(s: str) -> int:
    if s.isdigit():
        return int(s)
    w = _word_to_int(s)
    if w is not None:
        return w
    # último recurso: ignorar si no se puede
    raise ValueError("not a number")

def normalize_to_future(text: str,
                        tz_str: str | None = None,
                        min_future_seconds: int = 30,
                        strict: bool = True) -> datetime | None:
    """
    Devuelve un datetime timezone-aware en el FUTURO a partir de texto en español.
    Estrategia:
      1) Reglas regex de relativos (en/dentro de ...), incluidos “media”, “y media”, números en palabras.
      2) dateparser.search_dates con settings en ES (mañana 10, viernes 10:30, 11/09 18:45...).
      3) Patrones simples: “a las 14”, “a las 7 pm”, “14:30”, “14hs”.
      4) Fallback: None (strict=True) o now+5min (strict=False).
    """
    tz = ZoneInfo(tz_str or os.getenv("TZ") or "America/Montevideo")
    now = datetime.now(tz)
    t = (text or "").strip().lower()

    # (1) Reglas relativas
    for rx, fn in _RELATIVE_RULES:
        m = rx.search(t)
        if m:
            token = m.group(1) if m.lastindex else "0"
            try:
                n = _coerce_number(token)
            except ValueError:
                continue
            dt = fn(n, now)
            return _bump_future(dt, now, min_future_seconds)

    # (2) dateparser (absolutos/mix)
    if search_dates:
        settings = {
            "PREFER_DATES_FROM": "future",
            "RELATIVE_BASE": now,
            "TIMEZONE": str(tz),
            "TO_TIMEZONE": str(tz),
            "RETURN_AS_TIMEZONE_AWARE": True,
            "DATE_ORDER": "DMY",
            "PREFER_DAY_OF_MONTH": "current",
            "SKIP_TOKENS": ["de", "del", "la", "el", "a", "las"],
            "LANGUAGE_DETECTION_CONFIDENCE_THRESHOLD": 0.0,
        }
        found = search_dates(t, languages=["es"], settings=settings)
        if found:
            # Tomamos el último match (suele ser el más específico)
            _, dt = found[-1]
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz)
            return _bump_future(dt, now, min_future_seconds)

    # (3) Patrones simples de horas
    # "a las 14", "a las 7 pm", "a las 7:30 pm"
    m = re.search(r"\ba\s+las\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\b", t, re.I)
    if m:
        hh = int(m.group(1))
        mm = int(m.group(2) or 0)
        ap = (m.group(3) or "").lower()
        if ap == "pm" and hh < 12: hh += 12
        if ap == "am" and hh == 12: hh = 0
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now: dt += timedelta(days=1)
        return _bump_future(dt, now, min_future_seconds)

    # "14:30" / "14.30" / "14h30"
    m = re.search(r"\b(\d{1,2})[:h\.](\d{2})\b", t)
    if m:
        hh, mm = int(m.group(1)), int(m.group(2))
        dt = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if dt <= now: dt += timedelta(days=1)
        return _bump_future(dt, now, min_future_seconds)

    # "14hs" / "14h"
    m = re.search(r"\b(\d{1,2})\s*(?:hs|h)\b", t, re.I)
    if m:
        hh = int(m.group(1))
        dt = now.replace(hour=hh, minute=0, second=0, microsecond=0)
        if dt <= now: dt += timedelta(days=1)
        return _bump_future(dt, now, min_future_seconds)

    # (4) Fallback
    if strict:
        return None
    return now + timedelta(minutes=5)

