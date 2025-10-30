from __future__ import annotations
from datetime import datetime, timedelta
from typing    import List, Dict
import threading
import logging
from collections import defaultdict

from openai import OpenAI

# --- Parámetros ---
K, M, GAP_H = 8, 12, 6
GPT_MODEL   = "gpt-4o-mini"

# Locks por teléfono
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)

logger = logging.getLogger("memory_service")


# ---------- API ----------
class Memory:
    @staticmethod
    def fetch_context(phone: str, repo_provider=None) -> List[Dict[str, str]]:
        """
        Obtiene contexto de conversación: resumen + últimos K turns.
        
        Args:
            phone: Teléfono del usuario
            repo_provider: RepositoryProvider (requerido)
        """
        if not repo_provider:
            raise ValueError("repo_provider es requerido")
        
        # Usar repos DynamoDB
        convo = repo_provider.conversations.get_by_phone(phone)
        if not convo:
            # Crear conversación vacía
            convo = {"phone": phone, "summary": ""}
            repo_provider.conversations.set(phone, convo)
        
        ctx: List[Dict[str, str]] = []
        
        if convo.get("summary"):
            ctx.append({
                "role": "system",
                "content": f"Resumen de la sesión: {convo['summary']}"
            })
        
        # Obtener últimos K turns
        turns = repo_provider.turns.list_recent(
            phone=phone,
            conversation_id=phone,  # En Dynamo, conversation_id = phone
            limit=K
        )
        
        for t in turns:
            ctx.append({
                "role": t.get("role", "user"),
                "content": t.get("content", "")
            })
        
        return ctx

    @staticmethod
    def save_turn(phone: str, role: str, content: str, wa_msg_id: str | None = None, repo_provider=None) -> bool:
        """
        Guarda un turn en la DB con idempotencia por wa_msg_id.
        
        Args:
            phone: Teléfono del usuario
            role: Rol del turn ("user" o "assistant")
            content: Contenido del mensaje
            wa_msg_id: ID de WhatsApp para idempotencia
            repo_provider: RepositoryProvider (requerido)
        
        Returns:
            True si se creó el turn (nuevo)
            False si wa_msg_id ya existía (duplicado)
        """
        if not repo_provider:
            logger.error("[Memory] repo_provider es requerido")
            return False
        
        # En DynamoDB, la idempotencia está en append() con pre-check
        # Si append() no lanza excepción, se creó exitosamente
        try:
            import time
            ts_ms = int(time.time() * 1000)
            repo_provider.turns.append(
                phone=phone,
                conversation_id=phone,  # En Dynamo, conversation_id = phone
                wa_msg_id=wa_msg_id,
                ts_ms=ts_ms,
                payload={"role": role, "content": content}
            )
            return True
        except Exception:
            return False

    @staticmethod
    def should_summarize(phone: str, repo_provider=None) -> bool:
        """
        Determina si se debe resumir la conversación basándose en:
        - Número de turns >= M
        - Gap de tiempo >= GAP_H horas desde última actualización
        
        Args:
            phone: Teléfono del usuario
            repo_provider: RepositoryProvider (requerido)
        """
        if not repo_provider:
            logger.error("[Memory] repo_provider es requerido")
            return False
        
        # Obtener conversación
        convo = repo_provider.conversations.get_by_phone(phone)
        if not convo:
            return False
        
        # Obtener turns recientes para contar
        turns = repo_provider.turns.list_recent(
            phone=phone,
            conversation_id=phone,
            limit=M + 1  # Un poco más para detectar si >= M
        )
        
        if len(turns) >= M:
            return True
        
        # Check time gap
        updated_at_ms = convo.get("updated_at_ms")
        if updated_at_ms:
            import time
            now_ms = int(time.time() * 1000)
            gap_ms = GAP_H * 60 * 60 * 1000
            if (now_ms - updated_at_ms) >= gap_ms:
                return True
        
        return False

    @staticmethod
    def summarize(phone: str, client: OpenAI, repo_provider=None) -> None:
        """
        Resume la conversación y limpia turns antiguos, dejando solo los últimos K.
        
        Args:
            phone: Teléfono del usuario
            client: Cliente de OpenAI para generar resumen
            repo_provider: RepositoryProvider (requerido)
        """
        with _locks[phone]:
            if not Memory.should_summarize(phone, repo_provider):
                return

            if not repo_provider:
                logger.error("[Memory] repo_provider es requerido")
                return
            
            # Obtener conversación y turns
            convo = repo_provider.conversations.get_by_phone(phone)
            if not convo:
                return
            
            turns = repo_provider.turns.list_recent(
                phone=phone,
                conversation_id=phone,
                limit=M
            )
            
            recent = "\n".join(
                f"{t.get('role', 'user').upper()}: {t.get('content', '')}"
                for t in turns
            )
            
            prompt = f"""
            Eres un asistente que mantiene una memoria concisa de la conversación.
            
            INSTRUCCIONES:
            • Fusiona el RESUMEN anterior con los MENSAJES RECIENTES.
            • NO repitas texto literal ni detalles irrelevantes (temperaturas, cuentos completos…).
            • Sí incluye: temas tratados, pedidos del usuario, acciones/recordatorios creados,
               y cualquier preferencia o dato que ayude a continuar la charla.
            • Escribe en tercera persona en español, 2 – 3 frases o viñetas, ≤ 120 palabras.
            • Este resumen **solo se usará como contexto en futuros llamados al mismo LLM**;
               el usuario nunca lo verá directamente.
            
            RESUMEN ANTERIOR:
            {convo.get('summary') or '—'}
            
            MENSAJES RECIENTES:
            {recent}
            """

            rsp = client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
            )
            
            new_summary = rsp.choices[0].message.content.strip()
            
            # Actualizar conversación
            import time
            now_ms = int(time.time() * 1000)
            convo["summary"] = new_summary
            convo["updated_at_ms"] = now_ms
            repo_provider.conversations.set(phone, convo)
