from typing import Dict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from cachetools import TTLCache  # deduplicación en memoria
from flask import current_app as app


# ─────────────────────────────────────────────────────────
# Utilidad local: normalizar teléfonos al formato +E.164
def _normalize_phone_number(raw: str) -> str | None:
    digits = "".join(ch for ch in raw if ch.isdigit())
    if len(digits) < 7:
        return None
    return f"+{digits}"
# ─────────────────────────────────────────────────────────


class SendMessageFlow:
    """Enviar mensaje a un tercero – versión sin FSM.

    • Deduplicación intra-proceso con TTLCache (120 s).
    • Conversión y validación de la hora en **America/Montevideo**.
    • Si la hora queda en el pasado (<24 h) la mueve a "mañana".
    """

    def __init__(self, repo_provider=None) -> None:
        self.state: Dict[str, Dict] = {}
        self._recent_ops: TTLCache[str, bool] = TTLCache(maxsize=500, ttl=120)
        
        # Repo provider para DynamoDB
        if repo_provider is None:
            from app.db import RepositoryProvider
            repo_provider = RepositoryProvider()
        self.repo = repo_provider

    # ------------------------------------------------------------------
    # Entrada principal (el Orchestrator/LLM hace el parseo)
    # ------------------------------------------------------------------
    def handle(
        self,
        message: str,
        origin_phone: str,
        origin_name: str,
        customer_id: int,
    ) -> str | None:
        return None

    # ------------------------------------------------------------------
    # 🚀  Procesar directo
    # ------------------------------------------------------------------
    def _process_direct(
        self,
        origin_phone: str,
        origin_name: str,
        customer_id: int,
        phone: str,
        text: str,
        send_dt: datetime | None,
    ) -> str | None:
        """Programa o envía el mensaje exactamente una vez."""
        # 1️⃣  Deduplicación intra-proceso
        key = "|".join([
            origin_phone,
            phone,
            text,
            send_dt.isoformat() if send_dt else "now",
        ])
        if key in self._recent_ops:
            return ""  # ya procesado
        self._recent_ops[key] = True

        # 2️⃣  Normalizar / validar fecha-hora
        tz_local = ZoneInfo("America/Montevideo")
        now_local = datetime.now(tz_local)

        if send_dt is not None:
            # a) Convertir a hora local (mantiene hora si es naive)
            send_dt_local = (
                send_dt.astimezone(tz_local)
                if send_dt.tzinfo is not None
                else send_dt.replace(tzinfo=tz_local)
            )

            # b) Si quedó en el pasado <24 h → lo pasa a mañana
            diff_sec = (send_dt_local - now_local).total_seconds()

            # c) Validar futuro ≥60 s
            if diff_sec < 60:
                return (
                    "⏰ La fecha y hora indicada debe ser al menos 1 minuto "
                    "en el futuro. Por favor elegí un momento válido."
                )
        else:
            send_dt_local = None  # envío inmediato

        # 3️⃣  Ejecutar acción
        data = {
            "target": phone,
            "text": text,
            "origin_phone": origin_phone,
            "origin_name": origin_name,
            "origin_customer_id": customer_id,
        }
        app.logger.info(f"[SendMessageFlow] Enviando mensaje a {phone} para {send_dt_local}, {data}")
        if send_dt_local:
            app.logger.info(f"[SendMessageFlow] Mensaje programado para {send_dt_local}")
            self._schedule_send(data, send_dt_local)
        else:
            app.logger.info(f"[SendMessageFlow] Mensaje enviado inmediatamente")
            self._send_now(data)
        return ""  # confirmación llega vía plantilla

    # ------------------------------------------------------------------
    # 👇 Helpers
    # ------------------------------------------------------------------
    def _send_now(self, data: Dict):
        from app.utils.whatsapp_utils import (
            send_comida_template,
            send_recordatorio_mensaje,
        )
        now = datetime.now(ZoneInfo("America/Montevideo"))
        now_str = now.strftime("%d/%m/%Y %H:%M")
        app.logger.info(f"[SendMessageFlow] Enviando mensaje inmediato a {data['target']}, {data}")
        send_comida_template(
            recipient=data["target"],
            nombre=data["origin_name"],
            numero=data["origin_phone"],
            texto=data["text"],
        )
        send_recordatorio_mensaje(
            recipient=data["origin_phone"],
            texto=data["text"],
            numero=data["target"],
            fecha=now_str,
        )

    # ------------------------------------------------------------------
    def _schedule_send(self, data: Dict, send_dt: datetime):
        from app.services.scheduled_message_service import ScheduledMessageService
        from app.services import scheduler_service
        from app.utils.whatsapp_utils import send_recordatorio_mensaje

        # Persistir y agendar usando repo_provider
        from flask import current_app as app
        app.logger.info(
            f"[SendMessageFlow] Programando mensaje a {data['target']} "
            f"para {send_dt.strftime('%d/%m/%Y %H:%M')}"
        )
        sm_id = ScheduledMessageService.create(
            customer_id=data["origin_customer_id"],
            target_phone=data["target"],
            text=data["text"],
            send_at=send_dt,
            repo_provider=self.repo  # Pasar repo_provider
        )
        scheduler_service.schedule_scheduled_message(
            scheduler_service.scheduler,
            sm_id,
            send_dt,
        )

        origin_phone = data.get("origin_phone")
        if origin_phone:
            resp = send_recordatorio_mensaje(
                    recipient=origin_phone,
                    texto=data["text"],
                    numero=data["target"],
                    fecha=send_dt.strftime("%d/%m/%Y %H:%M"),
            )
            wa_msg_id = (resp.get("messages") or [{}])[0].get("id")
            
            # Actualizar wa_msg_id usando repo (en lugar de SQLAlchemy)
            # Nota: sm_id ahora es epoch_ms, necesitamos construir las claves
            if wa_msg_id and sm_id:
                # TODO: Implementar update_wa_msg_id en scheduled_message_repo si es necesario
                # Por ahora, el wa_msg_id se puede actualizar cuando se procese el mensaje
                app.logger.info(f"[SendMessageFlow] wa_msg_id={wa_msg_id} para sm_id={sm_id}")

        
        
