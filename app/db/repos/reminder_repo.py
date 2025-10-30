"""
Reminder Repository

Repositorio para gestión de recordatorios standalone (no vinculados a appointments).
PK: CUST#<phone>
SK: REM#<date_epoch>#<uuid>
"""
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_rem
from app.db.dynamo_client import now_ms


class ReminderRepo(DynamoRepoBase):
    """
    Repositorio para Reminder (recordatorios standalone).
    
    Patron de acceso:
    - Create simple
    - GetItem por wa_msg_id (GSI)
    
    Notes:
        Los reminders vinculados a appointments se gestionan dentro del appointment.
        Este repositorio es para recordatorios independientes creados por el usuario.
    """
    
    def create(
        self,
        phone: str,
        date_ms: int,
        uuid: str,
        data: dict
    ) -> dict:
        """
        Crea un recordatorio standalone.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            date_ms: Timestamp de ejecución en milisegundos (epoch UTC)
            uuid: UUID único para el recordatorio
            data: Datos del recordatorio
                {
                    'titulo': str,
                    'appointment_id': str,  # opcional, si está vinculado a cita
                }
        
        Returns:
            Dict con el recordatorio guardado
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'REM#<date_ms>#<uuid>',
                'uuid': str,
                'customer_phone': str,
                'titulo': str,
                'date': int,  # epoch_ms
                'appointment_id': str,  # opcional
                'wa_msg_id': str,  # se actualiza después del envío
                'created_at': int  # epoch_ms
            }
        
        Notes:
            - El wa_msg_id se actualiza después de enviar la confirmación
            - Para recordatorios standalone, appointment_id es None
        """
        ts = now_ms()
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_rem(date_ms, uuid),
            'item_type': 'REM',
            'reminder_id': uuid,
            'customer_phone': phone,
            'titulo': data['titulo'],
            'date_epoch': date_ms,
            'created_at': ts,
        }
        
        # Campos opcionales
        if 'appointment_id' in data:
            item['appointment_id'] = data['appointment_id']
        if 'wa_msg_id' in data:
            item['wa_msg_id'] = data['wa_msg_id']
        if 'ttl' in data:
            item['ttl'] = data['ttl']
        
        # PutItem con condición de no existencia
        return self.put_strict(
            item,
            condition='attribute_not_exists(pk) AND attribute_not_exists(sk)'
        )
    
    def get_by_wa_msg_id(self, wa_msg_id: str) -> dict | None:
        """
        Busca un recordatorio por su WhatsApp message ID.
        
        Args:
            wa_msg_id: WhatsApp message ID de la confirmación
        
        Returns:
            Dict con el recordatorio o None si no existe
            
        Notes:
            - Usa GSI ByWaMsgId
            - Útil para cancelaciones via botón
        """
        results = self.query(
            key_condition_expr='#wamid = :wamid',
            expr_attr_names={'#wamid': 'wa_msg_id'},
            expr_attr_values={':wamid': wa_msg_id},
            index_name='ByWaMsgId',
            limit=1
        )
        return results[0] if results else None
    
    def update_wa_msg_id_by_reminder_id(
        self,
        phone: str,
        reminder_id: str,
        wa_msg_id: str
    ) -> dict | None:
        """
        Actualiza el wa_msg_id de un recordatorio buscándolo por reminder_id.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            reminder_id: UUID del recordatorio
            wa_msg_id: WhatsApp message ID a asignar
        
        Returns:
            Dict con el recordatorio actualizado o None si no existe
            
        Notes:
            - Hace Query con pk=CUST#<phone> y begins_with(sk, 'REM#')
            - Filtra por reminder_id en memoria (DynamoDB no soporta filter en key)
            - Luego hace UpdateItem con la clave encontrada
        """
        # Query todos los reminders del customer
        results = self.query(
            key_condition_expr='#pk = :pk AND begins_with(#sk, :sk_prefix)',
            expr_attr_names={'#pk': 'pk', '#sk': 'sk'},
            expr_attr_values={
                ':pk': pk_customer(phone),
                ':sk_prefix': 'REM#'
            }
        )
        
        # Filtrar por reminder_id
        target = None
        for item in results:
            if item.get('reminder_id') == reminder_id:
                target = item
                break
        
        if not target:
            cid = self.correlation_id_provider()
            self.logger.warning(
                f"[CID={cid}] Reminder not found: phone={phone}, reminder_id={reminder_id}"
            )
            return None
        
        # Actualizar wa_msg_id
        key = {'pk': target['pk'], 'sk': target['sk']}
        return self.update_conditional(
            key=key,
            update_expr='SET #wamid = :wamid',
            expr_attr_names={'#wamid': 'wa_msg_id'},
            expr_attr_values={':wamid': wa_msg_id}
        )
