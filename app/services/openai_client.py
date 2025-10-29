import os
from openai import OpenAI

# Cliente único y centralizado para toda la app.
# Si luego querés configurar retries u otros timeouts globales,
# este es el lugar.
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
