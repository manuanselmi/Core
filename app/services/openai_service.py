import os
from openai import OpenAI, NotFoundError

USE_DB_THREADS = os.getenv("USE_SUPABASE_THREADS", "1") == "1"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

def _thread_for(
    wa_id: str,
    customer_id: int | None = None,
    correlation_id: str | None = None,
    last_wa_msg_id: str | None = None,
) -> str:
    if USE_DB_THREADS:
        # Lazy imports → evita el import circular en cold start
        from app.services.threads_service import ensure_thread, set_thread
        from app.utils.phone_utils import normalize_phone_e164

        # 1) asegurar thread en DB (idempotente)
        tid = ensure_thread(
            wa_id,
            customer_id=customer_id,
            correlation_id=correlation_id,
            last_wa_msg_id=last_wa_msg_id,
        )
        # 2) si en OpenAI no existe (borrado manual), recrear y actualizar DB
        try:
            client.beta.threads.retrieve(tid)
            return tid
        except NotFoundError:
            th = client.beta.threads.create()
            set_thread(normalize_phone_e164(wa_id), th.id)
            return th.id
    else:
        # fallback (legacy shelve)
        import shelve
        shelf_path = os.getenv("THREADS_DB", "/tmp/threads_db")
        with shelve.open(shelf_path, writeback=True) as dbs:
            tid = dbs.get(wa_id)
            if tid:
                try:
                    client.beta.threads.retrieve(tid)
                    return tid
                except NotFoundError:
                    pass
            th = client.beta.threads.create()
            dbs[wa_id] = th.id
            return th.id

def get_or_create_thread(
    wa_id: str,
    customer_id: int | None = None,
    correlation_id: str | None = None,
    last_wa_msg_id: str | None = None,
) -> str:
    return _thread_for(
        wa_id,
        customer_id=customer_id,
        correlation_id=correlation_id,
        last_wa_msg_id=last_wa_msg_id,
    )
