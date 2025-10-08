# app/Scripts/dump_threads.py
import os, shelve, sys
from pathlib import Path
SHELF = os.getenv("THREADS_DB", "/opt/render/project/src/threads_db")
if not any(Path(SHELF + e).exists() for e in ("", ".dat", ".dir", ".db")):
    sys.exit(f"[FATAL] No existe shelve en {SHELF}")
with shelve.open(SHELF) as db:
    print(f"[INFO] {len(db)} pares en {SHELF}")
    for i, (wa, th) in enumerate(db.items(), 1):
        print(f"{i:03d}. {wa} -> {th}")
        if i >= 50:  # evita spam
            print("... (truncado)")
            break
