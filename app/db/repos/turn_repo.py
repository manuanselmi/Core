"""
Turn Repository

Repositorio para gestión de mensajes individuales (turns) en conversaciones.
PK: CUST#<phone>
SK: TURN#<epoch_ms>#<wa_msg_id>
"""
from botocore.exceptions import ClientError
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_turn


class TurnRepo(DynamoRepoBase):
    """
    Repositorio para Turn (mensajes en conversación).
    
    Patron de acceso:
    - Append con idempotencia por wa_msg_id
    - Query reciente por phone + conversation_id
    - GetItem por wa_msg_id (GSI)
    """
    
    def append(
        self,
        phone: str,
        conversation_id: str,
        wa_msg_id: str,
        ts_ms: int,
        payload: dict
    ) -> dict:
        """
        Agrega un turn (mensaje) a la conversación de un cliente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            conversation_id: ID de la conversación (numérico o UUID)
            wa_msg_id: WhatsApp message ID (para idempotencia)
            ts_ms: Timestamp del mensaje en milisegundos (epoch UTC)
            payload: Datos del mensaje
                {
                    'role': str,  # 'user' | 'assistant'
                    'content': str,  # contenido del mensaje
                }
        
        Returns:
            Dict con el turn guardado
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'TURN#<ts_ms>#<wa_msg_id>',
                'conversation_id': str,
                'wa_msg_id': str,
                'role': str,
                'content': str,
                'created_at': int  # epoch_ms
            }
        
        Notes:
            - Usa condición "attribute_not_exists(pk)" para garantizar idempotencia
            - Si el wa_msg_id ya existe, lanza ConditionalCheckFailedException
            - El GSI ByWaMsgId permite lookup por wa_msg_id
        """
        # Pre-check por GSI ByWaMsgId para idempotencia
        existing = self.get_by_wa_msg_id(wa_msg_id)
        if existing:
            return existing
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_turn(ts_ms, wa_msg_id),
            'item_type': 'TURN',
            'conversation_id': str(conversation_id),
            'wa_msg_id': wa_msg_id,
            'ts_ms': ts_ms,
            'role': payload['role'],
            'content': payload['content'],
            'created_at': ts_ms,
        }
        
        # PutItem con condición de no existencia
        try:
            return self.put_strict(
                item,
                condition='attribute_not_exists(pk) AND attribute_not_exists(sk)'
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # Race condition: otro proceso insertó entre el check y el put
                # Re-fetch y devolver
                return self.get_item({'pk': item['pk'], 'sk': item['sk']}) or item
            raise
    
    def list_recent(
        self,
        phone: str,
        conversation_id: str,
        limit: int = 20
    ) -> list[dict]:
        """
        Lista los mensajes más recientes de una conversación.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            conversation_id: ID de la conversación
            limit: Cantidad máxima de mensajes a retornar
        
        Returns:
            Lista de turns ordenados por timestamp descendente (más reciente primero)
            
        Notes:
            - Query con PK=CUST#<phone> y SK begins_with "TURN#"
            - ScanIndexForward=False para orden descendente
            - Filtro adicional por conversation_id si es necesario
        """
        results = self.query(
            key_condition_expr='#pk = :pk AND begins_with(#sk, :sk_prefix)',
            expr_attr_names={
                '#pk': 'pk',
                '#sk': 'sk',
                '#cid': 'conversation_id'
            },
            expr_attr_values={
                ':pk': pk_customer(phone),
                ':sk_prefix': 'TURN#',
                ':cid': str(conversation_id)
            },
            filter_expr='#cid = :cid',
            scan_forward=False,
            limit=limit
        )
        
        return results
    
    def get_by_wa_msg_id(self, wa_msg_id: str) -> dict | None:
        """
        Busca un turn por su WhatsApp message ID.
        
        Args:
            wa_msg_id: WhatsApp message ID
        
        Returns:
            Dict con el turn o None si no existe
            
        Notes:
            - Usa GSI ByWaMsgId (PK: wa_msg_id)
            - Útil para actualizaciones post-creación (ej: transcripción de audio)
        """
        results = self.query(
            key_condition_expr='#wamid = :wamid',
            expr_attr_names={'#wamid': 'wa_msg_id'},
            expr_attr_values={':wamid': wa_msg_id},
            index_name='ByWaMsgId',
            limit=1
        )
        
        return results[0] if results else None
