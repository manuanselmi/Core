"""
Customer Repository

Repositorio para gestión de perfiles de clientes en DynamoDB.
PK: CUST#<phone>
SK: PROFILE
"""
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_profile
from app.db.dynamo_client import now_ms


class CustomerRepo(DynamoRepoBase):
    """
    Repositorio para Customer (perfil del cliente).
    
    Patron de acceso:
    - GetItem por phone
    - Upsert de perfil
    """
    
    def get_by_phone(self, phone: str) -> dict | None:
        """
        Obtiene el perfil de un cliente por su número de teléfono.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
        
        Returns:
            Dict con datos del perfil o None si no existe
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'PROFILE',
                'name': str,
                'phone': str,
                'accept_terms': bool,
                'created_at': int  # epoch_ms
            }
        """
        key = {
            'pk': pk_customer(phone),
            'sk': sk_profile()
        }
        return self.get_item(key)
    
    def upsert_profile(self, phone: str, data: dict) -> dict:
        """
        Crea o actualiza el perfil de un cliente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            data: Datos del perfil a guardar
                {
                    'name': str (opcional),
                    'accept_terms': bool (opcional),
                }
        
        Returns:
            Dict con el perfil completo guardado
            
        Notes:
            - Si el perfil no existe, lo crea con timestamp actual
            - Si existe, actualiza solo los campos provistos en data
            - El campo 'phone' siempre se preserva de la PK
        """
        ts = now_ms()
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_profile(),
            'item_type': 'CUST',
            'customer_phone': phone,
            'phone': phone,
            'created_at': ts,
            'updated_at': ts,
        }
        
        # Merge data opcional
        if 'name' in data:
            item['name'] = data['name']
        if 'accept_terms' in data:
            item['accept_terms'] = data['accept_terms']
        
        # PutItem sin condición (overwrite idempotente)
        return self.put_strict(item, condition=None)
