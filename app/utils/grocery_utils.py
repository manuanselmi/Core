from typing import List
from app.services.openai_service import client as openai_client

SYS_PROMPT = (
    "Eres un asistente que recibe la transcripción literal de un audio. "
    "Si el usuario ha indicado que quiere 'lista del super', devuelve "
    "EXCLUSIVAMENTE la lista de productos, un ítem por línea, sin numerar, "
    "sin comentarios adicionales. Los productos deben ir en singular y "
    "capitalizados (p. ej. 'Manzana', 'Leche entera'). "
    "No incluyas emojis, saltos en blanco extra, ni texto antes o después."
)

def extract_grocery_list(text: str) -> str | None:
    """
    Devuelve un string listo para enviar al usuario con la lista formateada,
    o None si el modelo no pudo entender la lista.
    """
    user_prompt = (
        "Transcripción completa (entre triple comillas):\n"
        f'"""\n{text}\n"""\n\n'
        "Si el audio NO es una lista del super, responde literalmente 'NULL'."
    )

    chat = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": SYS_PROMPT},
            {"role": "user",   "content": user_prompt},
        ],
        temperature=0
    )
    answer = chat.choices[0].message.content.strip()
    return None if answer.upper() == "NULL" else answer
