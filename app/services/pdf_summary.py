import io
import logging
import requests
from typing import List

from PyPDF2 import PdfReader

logger = logging.getLogger(__name__)

MAX_CHARS = 12_000  # tamaño máximo (en caracteres) de cada fragmento


def chunk_text(text: str, size: int = MAX_CHARS) -> List[str]:
    """
    Divide un texto largo en bloques de ≤ `size` caracteres sin cortar palabras.
    Se delega el resumen al Orchestrator; aquí sólo se secciona.
    """
    words, chunks, current, length = text.split(), [], [], 0
    for w in words:
        if length + len(w) + 1 > size:
            chunks.append(" ".join(current))
            current, length = [w], len(w) + 1
        else:
            current.append(w)
            length += len(w) + 1
    if current:
        chunks.append(" ".join(current))
    return chunks


def extract_pdf_text(pdf_url: str) -> str:
    """
    Descarga el PDF y devuelve TODO su texto concatenado.
    No hay llamadas a OpenAI aquí; sólo I/O + extracción.
    """
    logger.info(f"[pdf_summary_service] Descargando {pdf_url}")
    resp = requests.get(pdf_url, timeout=20)
    resp.raise_for_status()

    reader = PdfReader(io.BytesIO(resp.content))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)

    if not text.strip():
        raise ValueError("No se pudo extraer texto del PDF")

    return text
def extract_text_from_bytes(data: bytes) -> str:
    """
    Igual que extract_pdf_text pero partiendo de bytes en memoria.
    """
    reader = PdfReader(io.BytesIO(data))
    text = "\n".join(p.extract_text() or "" for p in reader.pages)
    if not text.strip():
        raise ValueError("No se pudo extraer texto del PDF")
    return text