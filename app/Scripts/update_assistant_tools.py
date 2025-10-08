# app/Scripts/update_assistant_tools.py
import os, json
from copy import deepcopy
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
ASSISTANT_ID = os.getenv("OPENAI_ASSISTANT_ID")

CREATE_EVENT_TOOL = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": "Crea un evento en el calendario interno y agenda recordatorio por WhatsApp.",
        "parameters": {
            "type": "object",
            "properties": {
                "date":  {"type": "string", "description": "YYYY-MM-DD HH:MM, ISO8601 o HH:MM (hoy)"},
                "title": {"type": "string", "description": "Título del evento"},
                "wa_msg_id": {"type": "string", "description": "ID de mensaje de WhatsApp (opcional)"}
            },
            "required": ["date", "title"]
        }
    }
}

def tool_to_dict(t):
    if hasattr(t, "model_dump"):
        return t.model_dump()
    if hasattr(t, "dict"):     # pydantic v1
        return t.dict()
    if isinstance(t, dict):
        return t
    if hasattr(t, "json"):
        return json.loads(t.json())
    # último recurso
    return {"type": getattr(t, "type", None)}

asst = client.beta.assistants.retrieve(ASSISTANT_ID)

# 1) Normalizar todas las tools a dict
current_tools = [tool_to_dict(t) for t in (asst.tools or [])]

# 2) Preservar 'strict' si ya existía en create_event de la UI
existing_ce = next(
    (t for t in current_tools
     if t.get("type") == "function" and t.get("function", {}).get("name") == "create_event"),
    None
)
strict = False
if existing_ce:
    strict = existing_ce.get("function", {}).get("strict", False)

# 3) Filtrar la vieja create_event
kept = [
    t for t in current_tools
    if not (t.get("type") == "function" and t.get("function", {}).get("name") == "create_event")
]

# 4) Minimizar built-ins a su forma canónica
normalized = []
for t in kept:
    ty = t.get("type")
    if ty in ("file_search", "code_interpreter", "file_upload"):
        normalized.append({"type": ty})
    else:
        normalized.append(t)

# 5) Agregar la nueva create_event (preservando strict si lo usás)
new_tool = deepcopy(CREATE_EVENT_TOOL)
if strict:
    new_tool["function"]["strict"] = True
normalized.append(new_tool)

# 6) Actualizar el assistant
client.beta.assistants.update(
    assistant_id=ASSISTANT_ID,
    tools=normalized
)

print("✅ Assistant actualizado: create_event agregada/actualizada y resto de tools preservadas.")
