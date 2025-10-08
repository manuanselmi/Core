import os, json, argparse
from copy import deepcopy
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")
CATALOG_PATH = os.getenv("FUNCTIONS_CATALOG_PATH", "functions_catalog.json")

def tool_to_dict(t):
    if hasattr(t, "model_dump"): return t.model_dump()
    if hasattr(t, "dict"):       return t.dict()
    if isinstance(t, dict):      return t
    if hasattr(t, "json"):       return json.loads(t.json())
    return {"type": getattr(t, "type", None)}

def canonical(d):
    return json.loads(json.dumps(d, sort_keys=True))

def main(dry_run: bool):
    with open(CATALOG_PATH, encoding="utf-8") as f:
        catalog = json.load(f)
    tools_from_catalog = catalog["tools"] if isinstance(catalog, dict) else catalog
    if not isinstance(tools_from_catalog, list) or not tools_from_catalog:
        raise SystemExit("❌ Catálogo vacío o inválido: se espera un objeto con clave 'tools' (lista).")

    # 1) Estado actual del assistant
    asst = client.beta.assistants.retrieve(ASSISTANT_ID)
    current_tools = [tool_to_dict(t) for t in (asst.tools or [])]

    # 2) Preservar built-ins en forma canónica
    preserved = []
    current_funcs = {}
    for t in current_tools:
        ty = t.get("type")
        if ty in ("file_search", "code_interpreter", "file_upload"):
            preserved.append({"type": ty})
        elif ty == "function":
            current_funcs[t["function"]["name"]] = t
        else:
            preserved.append(t)

    # 3) Indexar catálogo por nombre
    catalog_funcs = { t["function"]["name"]: t for t in tools_from_catalog if t.get("type") == "function" }

    merged = preserved[:]
    changes = {"added": [], "updated": [], "unchanged": [], "kept_extra": []}

    # a) agregar/actualizar lo del catálogo
    for name, defn in catalog_funcs.items():
        prev = current_funcs.get(name)
        candidate = deepcopy(defn)

        # preservar extras que hayas configurado en UI (p.ej. strict si no está en el catálogo)
        if prev:
            extras = {k:v for k,v in prev.get("function",{}).items()
                      if k not in ("name","description","parameters")}
            candidate["function"].update(extras)

        if prev and canonical(prev) == canonical(candidate):
            merged.append(prev)
            changes["unchanged"].append(name)
        else:
            merged.append(candidate)
            (changes["updated"] if prev else changes["added"]).append(name)

    # b) mantener functions actuales que NO estén en catálogo (por si cargaste algo manualmente)
    for name, prev in current_funcs.items():
        if name not in catalog_funcs:
            merged.append(prev)
            changes["kept_extra"].append(name)

    print("Plan de cambios:")
    print("  + added   :", changes["added"])
    print("  * updated :", changes["updated"])
    print("  = same    :", changes["unchanged"])
    print("  ~ kept    :", changes["kept_extra"])

    if dry_run:
        print("\n(🧪 Dry-run) No se actualizó el assistant.")
        return

    # 4) Update efectivo
    client.beta.assistants.update(assistant_id=ASSISTANT_ID, tools=merged)
    print("\n✅ Tools sincronizadas con el catálogo.")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Muestra cambios sin aplicar.")
    args = ap.parse_args()
    main(args.dry_run)
