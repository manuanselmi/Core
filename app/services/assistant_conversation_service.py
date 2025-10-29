from app.models import db, AssistantConversation, Customer
import logging
import uuid

logger = logging.getLogger("assistant_conversation_service")


class AssistantConversationService:
    @staticmethod
    def find_or_create(wa_phone: str, customer_id: int | None = None) -> AssistantConversation:
        """
        Busca o crea un registro de AssistantConversation para el teléfono dado.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            customer_id: ID del customer (opcional)
            
        Returns:
            Registro de AssistantConversation
        """
        conversation = AssistantConversation.query.filter_by(wa_phone=wa_phone).first()
        
        if conversation:
            # Actualizar last_used_at si es necesario
            conversation.customer_id = conversation.customer_id or customer_id
            db.session.commit()
            logger.info(
                "[ASST_CONV] Found existing conversation: id=%s wa_phone=%s last_response_id=%s",
                conversation.id, wa_phone, conversation.last_response_id
            )
            return conversation
        
        # Crear nuevo registro
        conversation_id = str(uuid.uuid4())
        new_conversation = AssistantConversation(
            customer_id=customer_id,
            wa_phone=wa_phone,
            conversation_id=conversation_id,
            status="active"
        )
        
        db.session.add(new_conversation)
        db.session.commit()
        
        logger.info(
            "[ASST_CONV] Created new conversation: id=%s wa_phone=%s conversation_id=%s",
            new_conversation.id, wa_phone, conversation_id
        )
        return new_conversation
    
    @staticmethod
    def update_last_response_id(wa_phone: str, response_id: str) -> None:
        """
        Actualiza el last_response_id para una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            response_id: ID de la última respuesta de OpenAI
        """
        conversation = AssistantConversation.query.filter_by(wa_phone=wa_phone).first()
        
        if not conversation:
            logger.warning(
                "[ASST_CONV] No conversation found for wa_phone=%s, cannot update last_response_id",
                wa_phone
            )
            return
        
        conversation.last_response_id = response_id
        db.session.commit()
        
        logger.info(
            "[ASST_CONV] Updated last_response_id: wa_phone=%s response_id=%s",
            wa_phone, response_id
        )
    
    @staticmethod
    def get_last_response_id(wa_phone: str) -> str | None:
        """
        Obtiene el last_response_id de una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            
        Returns:
            last_response_id si existe, None en caso contrario
        """
        conversation = AssistantConversation.query.filter_by(wa_phone=wa_phone).first()
        
        if not conversation:
            logger.debug("[ASST_CONV] No conversation found for wa_phone=%s", wa_phone)
            return None
        
        logger.debug(
            "[ASST_CONV] Retrieved last_response_id=%s for wa_phone=%s",
            conversation.last_response_id, wa_phone
        )
        return conversation.last_response_id
    
    @staticmethod
    def update_last_wa_msg_id(wa_phone: str, wa_msg_id: str) -> None:
        """
        Actualiza el last_wa_msg_id para una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            wa_msg_id: ID del último mensaje de WhatsApp procesado
        """
        conversation = AssistantConversation.query.filter_by(wa_phone=wa_phone).first()
        
        if not conversation:
            logger.warning(
                "[ASST_CONV] No conversation found for wa_phone=%s, cannot update last_wa_msg_id",
                wa_phone
            )
            return
        
        conversation.last_wa_msg_id = wa_msg_id
        db.session.commit()
        
        logger.debug(
            "[ASST_CONV] Updated last_wa_msg_id: wa_phone=%s wa_msg_id=%s",
            wa_phone, wa_msg_id
        )
