"""
Appointment Repository

Repositorio para gestión de citas/appointments.
PK: CUST#<phone>
SK: APPT#<starts_epoch>#<uuid>
"""
from botocore.exceptions import ClientError
from app.db.dynamo_repo_base import DynamoRepoBase
from app.db.dynamo_keys import pk_customer, sk_appt
from app.db.dynamo_client import now_ms


class AppointmentRepo(DynamoRepoBase):
    """
    Repositorio para Appointment (citas agendadas).
    
    Patron de acceso:
    - Create con validación de idempotencia por intent_wa_msg_id (GSI)
    - Query upcoming por customer
    - Query global por status+starts_at (GSI)
    - Query reminders pendientes (GSI)
    - GetItem por google_event_id (GSI)
    """
    
    def create_if_absent(
        self,
        phone: str,
        starts_at_ms: int,
        uuid: str,
        data: dict,
        intent_wa_msg_id: str | None = None
    ) -> dict:
        """
        Crea una cita si no existe una duplicada.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            starts_at_ms: Timestamp de inicio en milisegundos (epoch UTC)
            uuid: UUID único para la cita
            data: Datos de la cita
                {
                    'title': str,
                    'description': str,  # opcional
                    'mode': str,  # 'online' | 'presencial' | None
                    'location': str,  # opcional
                    'host': str,  # opcional
                    'service': str,  # opcional
                    'ends_at': int,  # epoch_ms, requerido
                    'timezone': str,  # default 'America/Montevideo'
                    'status': str,  # default 'scheduled'
                    'remind_before_min': int,  # default 60
                    'google_calendar_id': str,  # opcional
                    'google_event_id': str,  # opcional
                    'google_meet_link': str,  # opcional
                    'source': str,  # default 'wa'
                    'meta': dict  # opcional
                }
            intent_wa_msg_id: WhatsApp message ID del intent (para idempotencia)
        
        Returns:
            Dict con la cita guardada
            
        Schema del resultado:
            {
                'pk': 'CUST#<phone>',
                'sk': 'APPT#<starts_at_ms>#<uuid>',
                'uuid': str,
                'customer_phone': str,
                'title': str,
                'starts_at': int,
                'ends_at': int,
                'status': str,
                'intent_wa_msg_id': str,  # opcional
                'remind_at': int,  # calculado
                'reminder_status': str,  # default 'pending'
                'created_at': int,  # epoch_ms
                'updated_at': int,  # epoch_ms
                ... (otros campos de data)
            }
        
        Notes:
            - Si intent_wa_msg_id se provee, primero verifica duplicados vía GSI ByIntentMsgId
            - Calcula remind_at = starts_at - remind_before_min
            - Para GSI ApptStatusStartsAt: crea atributo compuesto status_starts
            - Para GSI ApptReminderQueue: crea atributo reminder_status_status si reminder_status='pending'
        
        Raises:
            ClientError: Si ya existe una cita con el mismo intent_wa_msg_id
        """
        # Idempotencia: verificar por intent_wa_msg_id si está presente
        if intent_wa_msg_id:
            existing = self._get_by_intent_wa_msg_id(intent_wa_msg_id)
            if existing:
                return existing
        
        ts = now_ms()
        remind_before_min = data.get('remind_before_min', 60)
        remind_at_ms = starts_at_ms - (remind_before_min * 60 * 1000)
        status = data.get('status', 'scheduled')
        reminder_status = data.get('reminder_status', 'pending')
        
        item = {
            'pk': pk_customer(phone),
            'sk': sk_appt(starts_at_ms, uuid),
            'item_type': 'APPT',
            'appointment_id': uuid,
            'customer_phone': phone,
            'title': data['title'],
            'starts_at_epoch': starts_at_ms,
            'ends_at_epoch': data['ends_at'],
            'timezone': data.get('timezone', 'America/Montevideo'),
            'status': status,
            'reminder_status': reminder_status,
            'remind_before_min': remind_before_min,
            'remind_at_epoch': remind_at_ms,
            'source': data.get('source', 'wa'),
            'created_at': ts,
            'updated_at': ts,
        }
        
        # Campos opcionales
        if 'description' in data:
            item['description'] = data['description']
        if 'mode' in data:
            item['mode'] = data['mode']
        if 'location' in data:
            item['location'] = data['location']
        if 'host' in data:
            item['host'] = data['host']
        if 'service' in data:
            item['service'] = data['service']
        if intent_wa_msg_id:
            item['intent_wa_msg_id'] = intent_wa_msg_id
        if 'google_calendar_id' in data:
            item['google_calendar_id'] = data['google_calendar_id']
        if 'google_event_id' in data:
            item['google_event_id'] = data['google_event_id']
        if 'google_meet_link' in data:
            item['google_meet_link'] = data['google_meet_link']
        if 'meta' in data:
            item['meta'] = data['meta']
        if 'ttl' in data:
            item['ttl'] = data['ttl']
        
        # Atributos compuestos para GSIs
        # GSI ApptReminderQueue: PK = reminder_status_status = "PENDING#SCHEDULED"
        if status == 'scheduled' and reminder_status == 'pending':
            item['reminder_status_status'] = f"{reminder_status}#{status}".upper()
        
        # PutItem con condición de no existencia
        try:
            return self.put_strict(
                item,
                condition='attribute_not_exists(pk) AND attribute_not_exists(sk)'
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # Ya existe, devolver el existente
                return self.get_item({'pk': item['pk'], 'sk': item['sk']}) or item
            raise
    
    def _get_by_intent_wa_msg_id(self, intent_wa_msg_id: str) -> dict | None:
        """Helper: busca por intent_wa_msg_id en GSI ByIntentMsgId."""
        results = self.query(
            key_condition_expr='#iid = :iid',
            expr_attr_names={'#iid': 'intent_wa_msg_id'},
            expr_attr_values={':iid': intent_wa_msg_id},
            index_name='ByIntentMsgId',
            limit=1
        )
        return results[0] if results else None
    
    def list_upcoming_by_customer(
        self,
        phone: str,
        now_ms: int,
        limit: int = 50
    ) -> list[dict]:
        """
        Lista las próximas citas de un cliente.
        
        Args:
            phone: Número de teléfono normalizado (E.164)
            now_ms: Timestamp actual en milisegundos (epoch UTC)
            limit: Cantidad máxima de citas a retornar
        
        Returns:
            Lista de citas ordenadas por starts_at ascendente
            
        Notes:
            - Query con PK=CUST#<phone> y SK begins_with "APPT#"
            - SK >= "APPT#<now_ms>" para filtrar futuras
            - Filtro adicional status='scheduled' (FilterExpression)
        """
        return self.query(
            key_condition_expr='#pk = :pk AND begins_with(#sk, :sk_prefix)',
            expr_attr_names={
                '#pk': 'pk',
                '#sk': 'sk',
                '#st': 'status',
                '#sat': 'starts_at_epoch'
            },
            expr_attr_values={
                ':pk': pk_customer(phone),
                ':sk_prefix': 'APPT#',
                ':status': 'scheduled',
                ':now': now_ms
            },
            filter_expr='#st = :status AND #sat >= :now',
            scan_forward=True,
            limit=limit
        )
    
    def query_global_by_status(
        self,
        status: str,
        from_ms: int,
        to_ms: int,
        limit: int = 200
    ) -> list[dict]:
        """
        Busca citas globalmente por status y rango de fechas.
        
        Args:
            status: Estado de la cita ('scheduled', 'canceled', 'completed', 'no_show')
            from_ms: Timestamp de inicio del rango (epoch_ms)
            to_ms: Timestamp de fin del rango (epoch_ms)
            limit: Cantidad máxima de citas a retornar
        
        Returns:
            Lista de citas ordenadas por starts_at
            
        Notes:
            - Usa GSI ApptStatusStartsAt
            - PK: status
            - SK: starts_at (range query BETWEEN from_ms AND to_ms)
        """
        return self.query(
            key_condition_expr='#st = :st AND #sat BETWEEN :from AND :to',
            expr_attr_names={
                '#st': 'status',
                '#sat': 'starts_at_epoch'
            },
            expr_attr_values={
                ':st': status,
                ':from': from_ms,
                ':to': to_ms
            },
            index_name='ApptStatusStartsAt',
            scan_forward=True,
            limit=limit
        )
    
    def query_reminders_due(
        self,
        now_ms: int,
        limit: int = 200
    ) -> list[dict]:
        """
        Busca citas cuyo recordatorio debe enviarse ahora.
        
        Args:
            now_ms: Timestamp actual en milisegundos (epoch UTC)
            limit: Cantidad máxima de citas a retornar
        
        Returns:
            Lista de citas con recordatorio pendiente ordenadas por remind_at
            
        Notes:
            - Usa GSI ApptReminderQueue para encontrar PKs
            - Luego hace GetItem por cada PK/SK para obtener atributos completos
            - Necesario porque el GSI puede no proyectar todos los atributos
        """
        from decimal import Decimal
        
        # Query GSI para obtener PKs (puede retornar solo keys si ProjectionType != ALL)
        sparse_items = self.query(
            key_condition_expr='#pk = :pk AND #sk <= :now',
            expr_attr_names={
                '#pk': 'reminder_status_status',
                '#sk': 'remind_at_epoch'
            },
            expr_attr_values={
                ':pk': 'PENDING#SCHEDULED',
                ':now': Decimal(now_ms)
            },
            index_name='ApptReminderQueue',
            scan_forward=True,
            limit=limit
        )
        
        # Fetch completo de cada item usando PK/SK real
        full_items = []
        for item in sparse_items:
            pk = item.get('pk')
            sk = item.get('sk')
            if pk and sk:
                full_item = self.get_item({'pk': pk, 'sk': sk})
                if full_item:
                    full_items.append(full_item)
        
        return full_items
    
    def get_by_google_event_id(self, google_event_id: str) -> dict | None:
        """
        Busca una cita por su Google Calendar event ID.
        
        Args:
            google_event_id: ID del evento en Google Calendar
        
        Returns:
            Dict con la cita o None si no existe
            
        Notes:
            - Usa GSI ByGoogleEvent
            - PK: google_event_id
            - Útil para sincronización bidireccional con Google Calendar
        """
        results = self.query(
            key_condition_expr='#geid = :geid',
            expr_attr_names={'#geid': 'google_event_id'},
            expr_attr_values={':geid': google_event_id},
            index_name='ByGoogleEvent',
            limit=1
        )
        return results[0] if results else None
    
    def update_reminder_status(
        self,
        pk: str,
        sk: str,
        new_status: str
    ) -> dict:
        """
        Actualiza el reminder_status de una cita.
        
        Args:
            pk: Partition key (CUST#<phone>)
            sk: Sort key (APPT#<starts_epoch>#<uuid>)
            new_status: Nuevo estado ('pending', 'sent', 'cancelled')
        
        Returns:
            Dict con la cita actualizada
            
        Notes:
            - Usado por scheduler para marcar reminders como enviados
            - También actualiza el índice ApptReminderQueue automáticamente
        """
        return self.update_conditional(
            key={'pk': pk, 'sk': sk},
            update_expr='SET #rs = :status',
            expr_attr_names={'#rs': 'reminder_status'},
            expr_attr_values={':status': new_status}
        )
    
    def claim_reminder(self, pk: str, sk: str, now_ms: int) -> dict | None:
        """
        Reclama un reminder de forma atómica cambiando status de 'pending' a 'sending'.
        
        Args:
            pk: Partition key
            sk: Sort key
            now_ms: Timestamp actual en milisegundos
        
        Returns:
            Dict con el item actualizado si claim exitoso, None si falla
            
        Notes:
            - Condición: reminder_status='pending' (o 'sending' stale > 2 min)
            - Setea claimed_at=now_ms
        """
        try:
            # Intentar claim desde 'pending'
            return self.update_conditional(
                key={'pk': pk, 'sk': sk},
                update_expr='SET #rs = :sending, claimed_at = :now',
                condition_expr='#rs = :pending',
                expr_attr_names={'#rs': 'reminder_status'},
                expr_attr_values={
                    ':sending': 'sending',
                    ':pending': 'pending',
                    ':now': now_ms
                }
            )
        except ClientError as e:
            if e.response['Error']['Code'] == 'ConditionalCheckFailedException':
                # Intentar reclaim de 'sending' stale (> 2 min)
                try:
                    stale_threshold = now_ms - (2 * 60 * 1000)
                    return self.update_conditional(
                        key={'pk': pk, 'sk': sk},
                        update_expr='SET claimed_at = :now',
                        condition_expr='#rs = :sending AND claimed_at < :stale',
                        expr_attr_names={'#rs': 'reminder_status'},
                        expr_attr_values={
                            ':sending': 'sending',
                            ':now': now_ms,
                            ':stale': stale_threshold
                        }
                    )
                except ClientError:
                    return None
            raise
    
    def mark_reminder_sent(self, pk: str, sk: str, wa_msg_id: str, now_ms: int) -> dict:
        """
        Marca un reminder como enviado.
        
        Args:
            pk: Partition key
            sk: Sort key
            wa_msg_id: WhatsApp message ID de la confirmación enviada
            now_ms: Timestamp actual en milisegundos
        
        Returns:
            Dict con el item actualizado
        """
        return self.update_conditional(
            key={'pk': pk, 'sk': sk},
            update_expr='SET #rs = :sent, reminder_sent_at = :now, last_reminder_wa_msg_id = :waid REMOVE claimed_at',
            expr_attr_names={'#rs': 'reminder_status'},
            expr_attr_values={
                ':sent': 'sent',
                ':now': now_ms,
                ':waid': wa_msg_id
            }
        )
    
    def release_claim(self, pk: str, sk: str, error_msg: str | None = None) -> dict:
        """
        Libera un claim fallido volviendo a 'pending'.
        
        Args:
            pk: Partition key
            sk: Sort key
            error_msg: Mensaje de error opcional
        
        Returns:
            Dict con el item actualizado
        """
        update_expr = 'SET #rs = :pending REMOVE claimed_at'
        expr_attr_values = {':pending': 'pending'}
        
        if error_msg:
            update_expr = 'SET #rs = :pending, last_error = :err REMOVE claimed_at'
            expr_attr_values[':err'] = error_msg
        
        return self.update_conditional(
            key={'pk': pk, 'sk': sk},
            update_expr=update_expr,
            expr_attr_names={'#rs': 'reminder_status'},
            expr_attr_values=expr_attr_values
        )
    
    def expire_reminder(self, pk: str, sk: str, now_ms: int) -> dict:
        """
        Marca un reminder como expirado (fuera de ventana).
        
        Args:
            pk: Partition key
            sk: Sort key
            now_ms: Timestamp actual en milisegundos
        
        Returns:
            Dict con el item actualizado
        """
        return self.update_conditional(
            key={'pk': pk, 'sk': sk},
            update_expr='SET #rs = :expired, expired_at = :now REMOVE claimed_at',
            expr_attr_names={'#rs': 'reminder_status'},
            expr_attr_values={
                ':expired': 'expired',
                ':now': now_ms
            }
        )
    
    def delete_reminder(self, pk: str, sk: str, now_ms: int) -> dict:
        """
        Elimina el recordatorio de un appointment (limpia campos de reminder).
        
        Args:
            pk: Partition key
            sk: Sort key
            now_ms: Timestamp actual en milisegundos
        
        Returns:
            Dict con el item actualizado
            
        Notes:
            - Elimina remind_at_epoch, reminder_status, claimed_at, reminder_status_status
            - Agrega reminder_deleted_at para registro informativo
            - El appointment NO se borra, solo se elimina la información del recordatorio
            - Esto previene que el scheduler vuelva a seleccionar este appointment
        """
        return self.update_conditional(
            key={'pk': pk, 'sk': sk},
            update_expr=(
                'SET reminder_deleted_at = :now '
                'REMOVE remind_at_epoch, reminder_status, claimed_at, reminder_status_status'
            ),
            expr_attr_values={':now': now_ms}
        )
