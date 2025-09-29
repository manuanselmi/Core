#!/usr/bin/env python3
from __future__ import annotations
import os, shelve, sys, time, argparse
from collections import defaultdict
from dotenv import load_dotenv
from openai import OpenAI, NotFoundError
from openai._exceptions import APIStatusError

# --- util de ruta canónica ---
HERE = os.path.abspath(os.path.dirname(__file__))
ROOT = os.path.abspath(os.path.join(HERE, "..", ".."))

def shelf_exists(path: str | None) -> bool:
    if not path: return False
    for ext in ("", ".db", ".dat", ".dir"):
        if os.path.exists(path + ext):
            return True
    return False

def resolve_shelf(cli_shelf: str | None) -> str:
    if cli_shelf:
        if not shelf_exists(cli_shelf):
            raise SystemExit(f"[FATAL] Shelve no existe: {cli_shelf}")
        return cli_shelf
    env_shelf = os.getenv("THREADS_DB")
    cands = [env_shelf] if env_shelf else []
    cands += [
        os.path.join(ROOT, "threads_db"),
        os.path.join(ROOT, "app", "threads_db"),
        "threads_db",
    ]
    for c in cands:
        if shelf_exists(c):
            return c
    print("[FATAL] No encontré el shelve. Probé:", *cands, sep="\n  - ")
    sys.exit(2)

def load_mapping(shelf_path: str) -> dict[str, str]:
    with shelve.open(shelf_path) as db:
        return dict(db)

def save_mapping(mapping: dict[str, str], shelf_path: str) -> None:
    with shelve.open(shelf_path, writeback=True) as db:
        db.clear()
        for k, v in mapping.items():
            db[k] = v

def cancel_active_runs(client: OpenAI, thread_id: str) -> int:
    """Intenta cancelar runs activos. Si el thread no existe, lo toma como 0 cancelados."""
    cancelled = 0
    cursor = None
    try:
        while True:
            runs = client.beta.threads.runs.list(thread_id=thread_id, limit=50, after=cursor)
            for run in runs.data:
                if getattr(run, "status", None) in ("queued", "in_progress", "requires_action"):
                    try:
                        client.beta.threads.runs.cancel(thread_id=thread_id, run_id=run.id)
                        cancelled += 1
                        time.sleep(0.05)
                    except Exception as e:
                        print(f"  [WARN] No pude cancelar run {run.id}: {e}")
            if not getattr(runs, "has_more", False):
                break
            cursor = runs.data[-1].id
    except NotFoundError:
        # thread ya no existe → no hay nada que cancelar
        return 0
    return cancelled

def delete_thread(client: OpenAI, thread_id: str) -> bool:
    """Borra el thread. Si ya no existe (404), lo consideramos borrado."""
    try:
        res = client.beta.threads.delete(thread_id)
        return bool(getattr(res, "deleted", False))
    except NotFoundError:
        return True
    except Exception as e:
        print(f"  [ERR] Falló delete {thread_id}: {e}")
        return False

def verify_deleted(client: OpenAI, thread_id: str) -> bool:
    try:
        client.beta.threads.retrieve(thread_id)
        return False
    except NotFoundError:
        return True
    except APIStatusError as api_err:
        return api_err.status_code == 404
    except Exception:
        return False

def main():
    parser = argparse.ArgumentParser(description="Borra todos los Threads mapeados y limpia el shelve.")
    g = parser.add_mutually_exclusive_group(required=True)
    g.add_argument("--dry-run", action="store_true")
    g.add_argument("--delete", action="store_true")
    parser.add_argument("--confirm", help='Escribe exactamente "BORRAR" para confirmar.')
    parser.add_argument("--shelf", help="Ruta del shelve (opcional, se resuelve sola si no se pasa)")
    parser.add_argument("--keep", nargs="*", default=[], help="wa_ids a conservar")
    args = parser.parse_args()

    load_dotenv()
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        print("[FATAL] Falta OPENAI_API_KEY.")
        sys.exit(2)
    client = OpenAI(api_key=api_key)

    shelf_path = resolve_shelf(args.shelf)
    mapping = load_mapping(shelf_path)
    if not mapping:
        print("[INFO] Mapping vacío. Nada para hacer.")
        print(f"[INFO] Shelve: {shelf_path}")
        return 0

    keep = set(args.keep or [])
    filtered = {wa: th for wa, th in mapping.items() if wa not in keep}
    by_thread = defaultdict(list)
    for wa, th in filtered.items():
        by_thread[th].append(wa)

    print(f"[INFO] Pairs totales: {len(mapping)} — candidatos: {len(filtered)} — threads únicos: {len(by_thread)}")
    print(f"[INFO] Shelve: {shelf_path}")
    for th, was in by_thread.items():
        sample = ", ".join(was[:5]) + ("…" if len(was) > 5 else "")
        print(f"  - {th} ⇢ {len(was)} wa_ids: {sample}")

    if args.dry_run:
        print("\n[DRY-RUN] No se eliminó nada.")
        return 0

    if args.delete and args.confirm != "BORRAR":
        print('[ABORT] Falta --confirm "BORRAR"')
        sys.exit(3)

    deleted, cancelled_total, removed_pairs, ghosts = 0, 0, 0, 0

    for th, was in by_thread.items():
        print(f"\n[WORK] Thread {th}")
        # cancelar runs si el thread existe; si no existe, no falla
        cancelled_total += cancel_active_runs(client, th)

        # borrar thread; si ya no existe, cuenta como ghost
        ok = delete_thread(client, th)
        if ok:
            if verify_deleted(client, th):
                deleted += 1
            else:
                print("  [WARN] No pude verificar el delete, continúo.")
        else:
            print("  [ERR] No pude borrar este thread por API (lo quito del mapping igual).")

        # quitar del mapping siempre, exista o no
        for wa in was:
            if mapping.get(wa) == th:
                mapping.pop(wa, None)
                removed_pairs += 1

    save_mapping(mapping, shelf_path)

    print("\n[SUMMARY]")
    print(f"  Threads borrados (o ya inexistentes): {deleted}")
    print(f"  Runs cancelados: {cancelled_total}")
    print(f"  Pairs wa_id→thread_id removidos: {removed_pairs}")
    print(f"  Mapping remanente: {len(mapping)}")
    print(f"[OK] Reset listo.")

if __name__ == "__main__":
    raise SystemExit(main())
