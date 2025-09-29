from __future__ import annotations
from datetime import datetime, timedelta
from typing    import List, Dict
import threading
from sqlalchemy.exc import IntegrityError
from flask import current_app
from collections import defaultdict

from openai import OpenAI
from app.models import db, Conversation, Turn

# --- Parámetros ---
K, M, GAP_H = 8, 12, 6
GPT_MODEL   = "gpt-4o-mini"

# Locks por teléfono
_locks: defaultdict[str, threading.Lock] = defaultdict(threading.Lock)


# ---------- helpers ----------
def _get_convo(phone: str) -> Conversation:
    convo = Conversation.query.filter_by(phone=phone).first()
    if not convo:
        convo = Conversation(phone=phone)
        db.session.add(convo)
        db.session.commit()
    return convo


def _recent_turns(convo: Conversation, limit: int) -> List[Turn]:
    return (Turn.query
            .filter_by(conversation_id=convo.id)
            .order_by(Turn.created_at.desc())
            .limit(limit)
            .all()[::-1])


# ---------- API ----------
class Memory:
    @staticmethod
    def fetch_context(phone: str) -> List[Dict[str, str]]:
        convo = _get_convo(phone)
        ctx: List[Dict[str, str]] = []

        if convo.summary:
            ctx.append({"role": "system",
                        "content": f"Resumen de la sesión: {convo.summary}"})

        for t in _recent_turns(convo, K):
            ctx.append({"role": t.role, "content": t.content})
        return ctx

    @staticmethod
    def save_turn(phone: str, role: str, content: str, wa_msg_id: str | None = None) -> None:
        convo = _get_convo(phone)
        try:
            db.session.add(Turn(conversation_id=convo.id,
                                role=role,
                                content=content,
                                wa_msg_id=wa_msg_id))
            convo.updated_at = datetime.utcnow()
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            current_app.logger.info("[Memory] wa_msg_id duplicado %s — ignorado", wa_msg_id)

    @staticmethod
    def should_summarize(phone: str) -> bool:
        convo = _get_convo(phone)
        n_turns = Turn.query.filter_by(conversation_id=convo.id).count()
        if n_turns >= M:
            return True
        if convo.updated_at and \
           (datetime.utcnow() - convo.updated_at) >= timedelta(hours=GAP_H):
            return True
        return False

    @staticmethod
    def summarize(phone: str, client: OpenAI) -> None:
        with _locks[phone]:
            if not Memory.should_summarize(phone):
                return

            convo  = _get_convo(phone)
            turns  = _recent_turns(convo, M)
            recent = "\n".join(f"{t.role.upper()}: {t.content}" for t in turns)
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
            • Fusiona el RESUMEN anterior con los MENSAJES RECIENTES.
            • NO repitas texto literal ni datos efímeros (temperaturas exactas, texto de cuentos…).
            • SÍ incluye: temas tratados, solicitudes del usuario, acciones realizadas (recordatorios),
              preferencias reveladas y cualquier información útil para continuar la conversación.
            • Formato: 2 – 3 frases concisas en tercera persona, español, ≤ 120 palabras.
            
            RESUMEN ANTERIOR:
            {convo.summary or '—'}
            
            MENSAJES RECIENTES:
            {recent}
            """

            rsp = client.chat.completions.create(
                model=GPT_MODEL,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.2,
                max_tokens=400,
            )
            convo.summary   = rsp.choices[0].message.content.strip()
            convo.updated_at = datetime.utcnow()

            # dejar solo la ventana K
            keep_ids = [t.id for t in _recent_turns(convo, K)]
            (Turn.query
                 .filter(Turn.conversation_id == convo.id,
                         ~Turn.id.in_(keep_ids))
                 .delete(synchronize_session=False))
            db.session.commit()
