# app/Scripts/check_threads.py
import os, shelve
from dotenv import load_dotenv
from openai import OpenAI
from openai._exceptions import APIStatusError

load_dotenv()
api_key = os.getenv("OPENAI_API_KEY")
if not api_key:
    raise SystemExit("[FATAL] Falta OPENAI_API_KEY (en Render va en Env Vars).")

client = OpenAI(api_key=api_key)
shelf = os.getenv("THREADS_DB", "threads_db")

missing = []
ok = []
db_len = 0

# Abrir y contar ANTES de cerrar el shelve
with shelve.open(shelf) as db:
    db_len = len(db)
    for wa, th in db.items():
        try:
            client.beta.threads.retrieve(th)
            ok.append((wa, th))
        except APIStatusError as e:
            if e.status_code == 404:
                missing.append((wa, th))
            else:
                print(f"[WARN] retrieve {th} → {e.status_code}: {e}")
        except Exception as e:
            print(f"[WARN] retrieve {th} → {e}")

print(f"[INFO] Pairs totales: {db_len} — inexistentes en OpenAI: {len(missing)} — OK: {len(ok)}")

# Muestra hasta 20 inexistentes para inspección rápida
for wa, th in missing[:20]:
    print(f" - missing: wa={wa} thread={th}")

# Tip: si querés ver TODOS, corré con `| cat` para que no pagine la consola de Render.
