import logging
import requests
from flask import current_app

logger = logging.getLogger(__name__)

GRAPH_API = "https://graph.facebook.com/v19.0"

def get_media_url(media_id: str) -> str:
    """
    Devuelve la URL temporal para descargar el adjunto
    usando el token de WhatsApp Cloud API.
    """
    token = current_app.config["ACCESS_TOKEN"]
    resp = requests.get(f"{GRAPH_API}/{media_id}",
                        params={"access_token": token},
                        timeout=10)
    resp.raise_for_status()
    return resp.json()["url"]

def download_media(media_url: str) -> bytes:
    """
    Descarga el binario del adjunto (PDF en este caso).
    """
    token = current_app.config["ACCESS_TOKEN"]
    resp = requests.get(media_url,
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=30)
    resp.raise_for_status()
    return resp.content
