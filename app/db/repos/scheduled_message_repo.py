"""
ScheduledMessage Repository

Repositorio para gestión de mensajes programados.
PK: CUST#<phone>
SK: SM#<send_epoch>#<uuid>
"""
from botocore.exceptions import ClientError
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_sm
from app.db.dynamo_client import now_ms


class ScheduledMessageRepo(DynamoRepoBase):
    """
    Repositorio para ScheduledMessage (mensajes programados).
    
    Patron de acceso:
    - Enqueue (crear mensaje)
    - Claim batch con lock distribuido (GSI)
    - Mark sent/error
    """
    
    def enqueue(
        self,
        phone: str,
        send_at_ms: int,
        uuid: str,
        data: dict
    ) -> dict:
        """
        Encola un mensaje para envío programado.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            send_at_ms: Timestamp de envío en milisegundos (epoch UTC)
            uuid: UUID único para el mensaje
            data: Datos del mensaje
                {
                    'target_phone': str,
                    'text': str,
                }
        
        Returns:
            Dict con el mensaje encolado
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'SM#<send_at_ms>#<uuid>',
                'uuid': str,
                'customer_phone': str,
                'target_phone': str,
                'text': str,
                'send_at': int,  # epoch_ms
                'status': str,  # 'pending'
                'created_at': int,  # epoch_ms
                'wa_msg_id': str  # se actualiza tras envío
            }
        
        Notes:
            - Status inicial: 'pending'
            - Para GSI SmStatusSendAt: crea atributo status_send_at
        """
        ts = now_ms()
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_sm(send_at_ms, uuid),
            'item_type': 'SM',
            'sm_id': uuid,
            'customer_phone': phone,
            'target_phone': data['target_phone'],
            'text': data['text'],
            'send_at_epoch': send_at_ms,
            'status': 'pending',
            'created_at': ts,
        }
        
        # Atributo compuesto para GSI SmStatusSendAt
        # PK del GSI = status
        # SK del GSI = send_at_epoch
        # (DynamoDB automáticamente usa estos atributos si están definidos en el GSI)
        
        if 'ttl' in data:
            item['ttl'] = data['ttl']
        
        # PutItem con condición de no existencia
        return self.put_strict(
            item,
            condition='attribute_not_exists(pk) AND attribute_not_exists(sk)'
        )
    
    def claim_pending_batch(
        self,
        now_ms: int,
        n: int = 25,
        stale_after_ms: int = 60000
    ) -> list[dict]:
        """
        Reclama un batch de mensajes pendientes para procesamiento (con lock distribuido).
        
        Args:
            now_ms: Timestamp actual en milisegundos (epoch UTC)
            n: Cantidad máxima de mensajes a reclamar
            stale_after_ms: Milisegundos después de los cuales un claim se considera stale
        
        Returns:
            Lista de mensajes reclamados con status actualizado a 'sending'
            
        Notes:
            - Usa GSI SmStatusSendAt para query global de mensajes pending
            - PK: 'pending'
            - SK: send_at (<= now_ms)
            - Implementa lock distribuido con UpdateItem condicional:
              * Condición: status='pending' OR (status='sending' AND claimed_at < now - stale_after_ms)
              * Update: SET status='sending', claimed_at=now_ms
            - Retorna solo los mensajes que se pudieron reclamar exitosamente
        """
        # Query por mensajes pending vencidos
        candidates = self.query(
            key_condition_expr='#st = :st AND #sat <= :now',
            expr_attr_names={
                '#st': 'status',
                '#sat': 'send_at_epoch'
            },
            expr_attr_values={
                ':st': 'pending',
                ':now': now_ms
            },
            index_name='SmStatusSendAt',
            scan_forward=True,
            limit=n
        )
        
        claimed = []
        stale_threshold = now_ms - stale_after_ms
        
        for msg in candidates:
            try:
                # Intentar reclamar con UpdateItem condicional
                updated = self.update_conditional(
                    key={'pk': msg['pk'], 'sk': msg['sk']},
                    update_expr='SET #st = :sending, claimed_at = :now',
                    expr_attr_names={'#st': 'status'},
                    expr_attr_values={
                        ':sending': 'sending',
                        ':now': now_ms,
                        ':pending': 'pending',
                        ':stale': stale_threshold
                    },
                    condition=(
                        '#st = :pending OR '
                        '(#st = :sending AND claimed_at < :stale)'
                    ).replace('#st', 'status')  # Reemplazar alias en condición
                )
                claimed.append(updated)
            except ClientError as e:
                if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                    # Otro proceso ya lo reclamó, continuar
                    continue
                # Otro error, propagar
                raise
        
        return claimed
    
    def mark_sent(self, key: dict, wa_msg_id: str, at_ms: int) -> dict:
        """
        Marca un mensaje como enviado exitosamente.
        
        Args:
            key: Clave del mensaje (pk, sk)
            wa_msg_id: WhatsApp message ID del mensaje enviado
            at_ms: Timestamp de envío en milisegundos (epoch UTC)
        
        Returns:
            Dict con el mensaje actualizado
            
        Notes:
            - Actualiza status='sent', sent_at=at_ms, wa_msg_id
            - Elimina atributo status_send_at para excluir de GSI
            - El mensaje puede ser eliminado posteriormente (cleanup job)
        """
        return self.update_conditional(
            key=key,
            update_expr='SET #st = :sent, sent_at = :at, wa_msg_id = :wamid REMOVE claimed_at',
            expr_attr_names={'#st': 'status'},
            expr_attr_values={
                ':sent': 'sent',
                ':at': at_ms,
                ':wamid': wa_msg_id
            }
        )
