"""
AssistantConversation Repository

Repositorio para gestión de conversaciones con el asistente de OpenAI.
PK: CUST#<phone>
SK: ASSTCONV
"""
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_asstconv
from app.db.dynamo_client import now_ms


class AssistantConversationRepo(DynamoRepoBase):
    """
    Repositorio para AssistantConversation.
    
    Patron de acceso:
    - GetItem por phone
    - Put/Update para actualizar estado
    """
    
    def get_by_phone(self, phone: str) -> dict | None:
        """
        Obtiene la conversación del asistente para un cliente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
        
        Returns:
            Dict con datos de la conversación o None si no existe
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'ASSTCONV',
                'wa_phone': str,
                'conversation_id': str,  # UUID de OpenAI
                'status': str,  # 'active' | 'inactive'
                'meta': dict,  # JSONB opcional
                'last_wa_msg_id': str,  # opcional
                'last_response_id': str,  # opcional
                'created_at': int,  # epoch_ms
                'updated_at': int  # epoch_ms
            }
        """
        key = {
            'pk': pk_customer(phone),
            'sk': sk_asstconv()
        }
        return self.get_item(key)
    
    def set(self, phone: str, data: dict) -> dict:
        """
        Crea o actualiza completamente la conversación del asistente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            data: Datos completos de la conversación
                {
                    'conversation_id': str,  # requerido
                    'status': str,  # opcional, default 'active'
                    'meta': dict,  # opcional
                    'last_wa_msg_id': str,  # opcional
                    'last_response_id': str  # opcional
                }
        
        Returns:
            Dict con la conversación completa guardada
            
        Notes:
            - Sobrescribe el item completo (PUT)
            - Actualiza 'updated_at' automáticamente
            - Si no existe 'created_at', lo inicializa
        """
        ts = now_ms()
        
        # Verificar si existe para preservar created_at
        existing = self.get_by_phone(phone)
        created_at = existing['created_at'] if existing else ts
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_asstconv(),
            'item_type': 'ASSTCONV',
            'wa_phone': phone,
            'conversation_id': data['conversation_id'],
            'status': data.get('status', 'active'),
            'created_at': created_at,
            'updated_at': ts,
        }
        
        # Campos opcionales
        if 'meta' in data:
            item['meta'] = data['meta']
        if 'last_wa_msg_id' in data:
            item['last_wa_msg_id'] = data['last_wa_msg_id']
        if 'last_response_id' in data:
            item['last_response_id'] = data['last_response_id']
        
        # PutItem sin condición (overwrite)
        return self.put_strict(item, condition=None)
