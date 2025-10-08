import os
from openai import OpenAI, NotFoundError
USE_DB_THREADS = os.getenv("USE_SUPABASE_THREADS", "1") == "1"

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

if USE_DB_THREADS:
    from app.services.threads_service import get_thread, set_thread

def _thread_for(wa_id: str) -> str:
    if USE_DB_THREADS:
        tid = get_thread(wa_id)
        if tid:
            try:
                client.beta.threads.retrieve(tid)
                return tid
            except NotFoundError:
                pass
        th = client.beta.threads.create()
        set_thread(wa_id, th.id)
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
