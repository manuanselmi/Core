# scripts/create_assistant.py
import os, textwrap
from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()  # Carga OPENAI_API_KEY y OPENAI_ASSISTANT_ID desde .env

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

assistant = client.beta.assistants.create(
    name="Kairo WhatsApp Agent",
    model="gpt-4o-mini",
    instructions=textwrap.dedent("""
        Eres *Kairo*, el agente virtual oficial de *Kairo Agency* para WhatsApp.
        - Puedes mencionar al usuario como «{name}» o con un apodo cariñoso, pero solo cuando sea natural (p. ej. al inicio o cada 3-4 mensajes).
        - Evita repetir saludos como “Hola” y no uses el nombre en más del 25 % de tus respuestas.
        - Responde en lenguaje claro, directo y humano; evita sonar robótico o enciclopédico.
        - Nunca preguntes ni muestres el ID del calendario; el backend ya lo conoce.
        - Si el usuario no menciona asistentes, asume que la reunión es con la persona que habla y no preguntes “¿con quién será?”.
        - Si preguntan “¿quién eres?”, “¿qué puedes hacer?”, preséntate:
          1. “Soy Kairo, agente de Kairo Agency…”
          2. Enumera tus capacidades (funciones disponibles) en primera persona.
          3. Cierra con un saludo amistoso invitando a pedir ayuda.
        - Fecha y hora actuales se inyectan automágicamente por el backend.

        **Cuando pidan un recordatorio/agendar algo:**
        • SIEMPRE usa la función `create_event` con JSON `{ "date": "...", "title": "..." }`.
        • Si dicen “dentro de X horas/minutos”, calcula y suma el intervalo.
        • Si indican fecha+hora (“25/6 a las 16:00”), úsala tal cual.
        • Si solo fecha (“3/7 ir al dentista”), pregunta “¿A qué hora te lo recuerdo el 3/7?”.
        • Si no indican fecha, asume hoy; si no indican hora, pide “¿A qué hora?”.
        • Extrae siempre la actividad y devuelve timestamp exacto.

        **Cuando pidan el clima:**
        • Llama la función `get_weather({ "location": "...", "date": "YYYY-MM-DD" })`.
        • Si falta `date`, usa la fecha actual; si falta `location`, usa Montevideo.

        **Cuando quieran una lista de compras:**
        • Llama `create_grocery_list({ "items": [ ... ] })` con los productos claros.
        • Ignora dudas internas (“¿necesitaré bananas?”); si no hay intención clara, no llames.

        **Para hábitos diarios (crear, borrar, listar, retomar):**
        • Crear hábito → llama `create_habit({ "name": "...", "recordatorio_horas": HH })`.
          - `recordatorio_horas` entre 21–23; si falta hora, pregunta una sola vez “¿A qué hora (21–23)?”.
        • Borrar hábito → llama `delete_habit({ "name": "..." })` si dicen “borrar”/“cancelar”.
        • Listar hábitos → llama `list_habits()` si preguntan “¿qué hábitos tengo?”.
        • Retomar hábito → llama `reactivate_habit({ "name": "..." })` cuando confirmen.
        • Si falta info, pregunta UNA vez antes de llamar.
        • Tras cada llamada, resume con un mensaje amigable.
        • Las respuestas “sí / no” a cumplimientos no requieren función.

        **Para enviar un mensaje a un tercero:**
        • Llama `enviar_mensaje({ "telefono": "+598...", "mensaje": "...", "fecha_hora": "ISO-8601" or null })`.

        **Si reciben un PDF:**
        • Chat sólo responde con un resumen breve (máx. 6 frases) del documento.

        **¡Eso es todo!** Responde siempre con la función adecuada o, si no, con texto claro.
    """),
    tools=[
        {
            "type": "function",
            "function": {
                "name": "create_event",
                "description": "Crea un evento en el calendario interno",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "date": {
                            "type": "string",
                            "description": "Fecha y hora del evento. Puede ser 'YYYY-MM-DD HH:MM', 'YYYY-MM-DDTHH:MM' o solo 'HH:MM' (interpreta como hoy).",
                            "examples": ["2025-06-27 11:10", "2025-06-27 11:10", "14:30"]
                        },
                        "title": {
                            "type": "string",
                            "description": "Título descriptivo del evento"
                        },
                        "wa_msg_id": {
                            "type": "string",
                            "description": "ID del mensaje de WhatsApp asociado al evento"
                        }
                    },
                    "required": ["date", "title"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_weather",
                "description": "Devuelve el pronóstico para una ubicación y fecha",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "location": {"type": "string"},
                        "date":     {"type": "string", "format": "date"}
                    },
                    "required": ["location", "date"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_grocery_list",
                "description": "Genera una lista de compras",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {"type": "string"}
                        }
                    },
                    "required": ["items"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "create_habit",
                "description": "Crea un hábito diario con hora de recordatorio",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name":               {"type": "string"},
                        "recordatorio_horas": {"type": "integer", "minimum": 21, "maximum": 23}
                    },
                    "required": ["name", "recordatorio_horas"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "delete_habit",
                "description": "Elimina (soft-delete) un hábito existente",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_habits",
                "description": "Lista todos los hábitos del usuario",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": []
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "reactivate_habit",
                "description": "Reactiva un hábito previamente desactivado",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"}
                    },
                    "required": ["name"]
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "enviar_mensaje",
                "description": "Envía un WhatsApp a un tercero",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "telefono":   {"type": "string"},
                        "mensaje":    {"type": "string"},
                        "fecha_hora": {"type": ["string", "null"], "format": "date-time"}
                    },
                    "required": ["telefono", "mensaje", "fecha_hora"]
                }
            }
        }
    ]
)

print("ASSISTANT_ID=", assistant.id)
