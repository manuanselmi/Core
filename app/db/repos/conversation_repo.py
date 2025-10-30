"""
Conversation Repository

Repositorio para gestión de conversaciones (summary y metadata).
PK: CUST#<phone>
SK: CONVO
"""
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_convo
from app.db.dynamo_client import now_ms


class ConversationRepo(DynamoRepoBase):
    """
    Repositorio para Conversation (resumen de conversación).
    
    Patron de acceso:
    - GetItem por phone
    - Put/Update para actualizar summary
    """
    
    def get_by_phone(self, phone: str) -> dict | None:
        """
        Obtiene la conversación para un cliente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
        
        Returns:
            Dict con datos de la conversación o None si no existe
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'CONVO',
                'phone': str,
                'summary': str,  # resumen de la conversación
                'updated_at': int  # epoch_ms
            }
        """
        key = {
            'pk': pk_customer(phone),
            'sk': sk_convo()
        }
        return self.get_item(key)
    
    def set(self, phone: str, data: dict) -> dict:
        """
        Crea o actualiza la conversación.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            data: Datos de la conversación
                {
                    'summary': str,  # opcional, default ""
                }
        
        Returns:
            Dict con la conversación completa guardada
            
        Notes:
            - Sobrescribe el item completo (PUT)
            - Actualiza 'updated_at' automáticamente
        """
        ts = now_ms()
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_convo(),
            'item_type': 'CONVO',
            'phone': phone,
            'summary': data.get('summary', ''),
            'updated_at': ts,
        }
        
        # PutItem sin condición (overwrite)
        return self.put_strict(item, condition=None)
