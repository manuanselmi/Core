import logging
import uuid

logger = logging.getLogger("assistant_conversation_service")


class AssistantConversationService:
    @staticmethod
    def find_or_create(wa_phone: str, customer_id: int | None = None, repo_provider=None) -> dict:
        """
        Busca o crea un registro de AssistantConversation para el teléfono dado.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            customer_id: ID del customer (opcional)
            repo_provider: RepositoryProvider (requerido)
            
        Returns:
            Dict con campos del AssistantConversation
        """
        if not repo_provider:
            raise ValueError("repo_provider es requerido")
        
        # Usar repo DynamoDB
        asstconv = repo_provider.assistant_conversations.get_by_phone(wa_phone)
        
        if asstconv:
            # Actualizar customer_id si viene y no estaba
            if customer_id and not asstconv.get("customer_id"):
                asstconv["customer_id"] = customer_id
                repo_provider.assistant_conversations.set(wa_phone, asstconv)
            
            return asstconv
        
        # Crear nuevo
        conversation_id = str(uuid.uuid4())
        new_conv = {
            "wa_phone": wa_phone,
            "customer_id": customer_id,
            "conversation_id": conversation_id,
            "status": "active",
            "last_response_id": None,
            "last_wa_msg_id": None,
        }
        repo_provider.assistant_conversations.set(wa_phone, new_conv)
        
        return new_conv
    
    @staticmethod
    def update_last_response_id(wa_phone: str, response_id: str, repo_provider=None) -> None:
        """
        Actualiza el last_response_id para una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            response_id: ID de la última respuesta de OpenAI
            repo_provider: RepositoryProvider (requerido)
        """
        if not repo_provider:
            logger.error("[ASST_CONV] repo_provider es requerido")
            return
        
        asstconv = repo_provider.assistant_conversations.get_by_phone(wa_phone)
        if not asstconv:
            logger.error("[ASST_CONV] No conversation found for wa_phone=%s", wa_phone)
            return
        
        asstconv["last_response_id"] = response_id
        repo_provider.assistant_conversations.set(wa_phone, asstconv)
    
    @staticmethod
    def get_last_response_id(wa_phone: str, repo_provider=None) -> str | None:
        """
        Obtiene el last_response_id de una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            repo_provider: RepositoryProvider (requerido)
            
        Returns:
            last_response_id si existe, None en caso contrario
        """
        if not repo_provider:
            logger.error("[ASST_CONV] repo_provider es requerido")
            return None
        
        asstconv = repo_provider.assistant_conversations.get_by_phone(wa_phone)
        if not asstconv:
            return None
        
        return asstconv.get("last_response_id")
    
    @staticmethod
    def update_last_wa_msg_id(wa_phone: str, wa_msg_id: str, repo_provider=None) -> None:
        """
        Actualiza el last_wa_msg_id para una conversación.
        
        Args:
            wa_phone: Número de WhatsApp del usuario
            wa_msg_id: ID del último mensaje de WhatsApp procesado
            repo_provider: RepositoryProvider (requerido)
        """
        if not repo_provider:
            logger.error("[ASST_CONV] repo_provider es requerido")
            return
        
        asstconv = repo_provider.assistant_conversations.get_by_phone(wa_phone)
        if not asstconv:
            logger.error("[ASST_CONV] No conversation found for wa_phone=%s", wa_phone)
            return
        
        asstconv["last_wa_msg_id"] = wa_msg_id
        repo_provider.assistant_conversations.set(wa_phone, asstconv)
