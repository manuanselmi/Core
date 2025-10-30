import logging
import time
import uuid as uuid_lib
from datetime import datetime, timezone
from app.utils.whatsapp_utils import send_comida_template

logger = logging.getLogger("scheduled_message_service")


class ScheduledMessageService:
    @staticmethod
    def create(customer_id: int, target_phone: str, text: str, send_at: datetime, repo_provider=None) -> int:
        """
        Crea un mensaje programado.
        
        Args:
            customer_id: DEPRECADO (no usado en DynamoDB)
            target_phone: Teléfono destino
            text: Contenido del mensaje
            send_at: Datetime UTC de cuándo enviar
            repo_provider: RepositoryProvider (requerido)
            
        Returns:
            Timestamp epoch_ms como "ID" del mensaje
        """
        if not repo_provider:
            logger.error("[ScheduledMessageService] repo_provider es requerido")
            return -1
        
        # Convertir send_at a epoch_ms
        if send_at.tzinfo is None:
            send_at = send_at.replace(tzinfo=timezone.utc)
        send_at_ms = int(send_at.timestamp() * 1000)
        
        # Generar UUID único
        msg_uuid = str(uuid_lib.uuid4())
        
        # Preparar payload
        data = {
            "target_phone": target_phone,
            "text": text,
        }
        
        # Enqueue en DynamoDB
        repo_provider.scheduled_messages.enqueue(
            phone=target_phone,
            send_at_ms=send_at_ms,
            uuid=msg_uuid,
            data=data
        )
        
        logger.info(
            "[ScheduledMessageService] Mensaje programado: phone=%s send_at=%s uuid=%s",
            target_phone, send_at.isoformat(), msg_uuid
        )
        
        # Retornar timestamp como "ID"
        return send_at_ms

    @staticmethod
    def send(target_phone: str, customer_name: str, text: str) -> None:
        """
        Envía un mensaje inmediatamente (no programado).
        
        Args:
            target_phone: Teléfono destino
            customer_name: Nombre del destinatario
            text: Contenido del mensaje
        """
        try:
            send_comida_template(target_phone, customer_name, target_phone, text)
            logger.info("[ScheduledMessageService] Mensaje enviado: phone=%s", target_phone)
        except Exception:
            logger.exception("[ScheduledMessageService] Error enviando mensaje a %s", target_phone)
            raise