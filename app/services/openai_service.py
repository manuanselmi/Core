import warnings
from app.services.openai_client import client

def get_or_create_conversation(
    wa_id: str,
    customer_id: int | None = None,
    correlation_id: str | None = None,
    last_wa_msg_id: str | None = None,
) -> str:
    # Import perezoso para evitar ciclos en import-time
    from app.services.responses_service import get_or_create_conversation_id as _get
    return _get(
        wa_id,
        customer_id=customer_id,
        correlation_id=correlation_id,
        last_wa_msg_id=last_wa_msg_id,
    )

# --- Shim de compatibilidad (deprecado) ------------------------
def get_or_create_thread(*args, **kwargs):
    warnings.warn(
        "get_or_create_thread() está deprecado. Usá get_or_create_conversation().",
        DeprecationWarning,
        stacklevel=2,
    )
    return get_or_create_conversation(*args, **kwargs)
