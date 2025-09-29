import os
import shelve
from dotenv import load_dotenv
from openai import OpenAI, NotFoundError


# 1) Cargo .env para API keys
load_dotenv()

# 2) Inicializo el cliente y la constante de Assistant
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

def _thread_for(wa_id: str) -> str:
    """
    Devuelve el thread_id para ese usuario, creándolo si no existe.
    Usa shelve en local; en producción cambialo por tu BD/Redis.
    """
    shelf_path = os.getenv("THREADS_DB", "/opt/render/project/src/threads_db")
    with shelve.open(shelf_path, writeback=True) as db:
        tid = db.get(wa_id)
        if tid:
            # Verificar que el thread todavía exista
            try:
                client.beta.threads.retrieve(tid)
                return tid
            except NotFoundError:
                pass  # caemos a recrear
        # Crear nuevo y actualizar mapping
        th = client.beta.threads.create()
        db[wa_id] = th.id
        return th.id