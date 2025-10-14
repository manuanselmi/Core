
def normalize_phone_e164(raw: str) -> str | None:
    """
    Normaliza un número arbitrario al formato +E.164 sin librerías pesadas.
    - Conserva solo dígitos
    - Requiere al menos 7 dígitos
    - Prefija '+'
    """
    if not raw:
        return None
    digits = "".join(ch for ch in str(raw) if ch.isdigit())
    if len(digits) < 7:
        return None
    return f"+{digits}"