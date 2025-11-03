import os
from openai import OpenAI
import httpx

# Cliente único y centralizado para toda la app.
# Si luego querés configurar retries u otros timeouts globales,
# este es el lugar.
# Timeout: connect=2s, read=25s para dar margen cuando la respuesta tarda en consolidarse
client = OpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    http_client=httpx.Client(
        timeout=httpx.Timeout(read=25.0, write=25.0, connect=2.0, pool=25.0)
    )
)
