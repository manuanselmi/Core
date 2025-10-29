# Database Usage Map - DynamoDB Migration Reference

**Fecha de generación**: 2025-10-29  
**Propósito**: Inventario de operaciones CRUD y queries por modelo para facilitar la migración de PostgreSQL a DynamoDB.

---

## Índice por Modelo

1. [Customer](#1-customer)
2. [Conversation](#2-conversation)
3. [Turn](#3-turn)
4. [Reminder](#4-reminder)
5. [ScheduledMessage](#5-scheduledmessage)
6. [AssistantConversation](#6-assistantconversation)
7. [Appointment](#7-appointment)

---

## 1. Customer

### Esquema actual (PostgreSQL)
```python
id: Integer (PK, autoincrement)
name: String
phone: String (UNIQUE, NOT NULL)
accept_terms: Boolean (default=False)
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/customer_service.py`
- **Función**: `CustomerService.find_or_create()`
  ```python
  new_customer = Customer(phone=phone, name=alias, accept_terms=accept_terms)
  db.session.add(new_customer)
  db.session.commit()
  ```

#### **Read**
- **Archivo**: `app/services/customer_service.py`
- **Función**: `CustomerService.get(customer_id)`
  - **Query**: `Customer.query.filter(Customer.id == customer_id).first()`
  - **Índice requerido**: PK lookup

- **Función**: `CustomerService.find_or_create(phone)`
  - **Query**: `Customer.query.filter(Customer.phone == phone).first()`
  - **Índice requerido**: GSI por `phone` (UNIQUE)

- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.schedule_meeting()`
  - **Query**: `Customer.query.filter_by(phone=wa_id_var).first()`
  - **Índice requerido**: GSI por `phone`

- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.cancel_reminder()`
  - **Query**: `Customer.query.filter_by(phone=phone_id).first()`
  - **Índice requerido**: GSI por `phone`

#### **Update**
- No hay updates explícitos detectados; el modelo es mayormente read-only tras creación.

#### **Delete**
- No hay deletes directos detectados.
- **Cascadas**: `reminders` (all, delete), `appointments`, `scheduled_messages` se borran en cascada.

### Filtros y Orden
- **Por `phone`** (UNIQUE constraint): Filtro principal para búsquedas
- **Por `id`** (PK): Lookup directo

### Llaves de Idempotencia
- **`phone`**: UNIQUE constraint garantiza un solo customer por número de teléfono

### Dependencias Temporales
- Ninguna

### Notas para DynamoDB
- **PK**: `phone` (String) - ya es único y es el acceso principal
- **SK**: No necesario (entidad simple)
- **GSI**: `customer_id-index` si se requiere lookup inverso por ID numérico (poco frecuente)
- **Atributos**: `name`, `accept_terms`, `created_at`

---

## 2. Conversation

### Esquema actual (PostgreSQL)
```python
id: Integer (PK, autoincrement)
phone: String (UNIQUE, NOT NULL, indexed)
summary: Text (default="")
updated_at: DateTime (default=utcnow)
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory._get_convo(phone)`
  ```python
  convo = Conversation(phone=phone)
  db.session.add(convo)
  db.session.commit()
  ```

#### **Read**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory._get_convo(phone)`
  - **Query**: `Conversation.query.filter_by(phone=phone).first()`
  - **Índice requerido**: GSI por `phone` (UNIQUE)

- **Función**: `Memory.fetch_context(phone)`
  - **Query**: `Conversation.query.filter_by(phone=phone).first()`
  - Accede a `convo.summary` y relación `turns`

#### **Update**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory.save_turn()`
  ```python
  convo.updated_at = datetime.utcnow()
  db.session.commit()
  ```

- **Función**: `Memory.summarize()`
  ```python
  convo.summary = rsp.choices[0].message.content.strip()
  convo.updated_at = datetime.utcnow()
  db.session.commit()
  ```

#### **Delete**
- No hay deletes directos.
- **Cascadas**: `turns` se borran en cascada (all, delete)

### Filtros y Orden
- **Por `phone`** (UNIQUE): Filtro principal

### Llaves de Idempotencia
- **`phone`**: UNIQUE constraint

### Dependencias Temporales
- **`updated_at`**: Usado para determinar si debe resumirse (gap de 6 horas)
  ```python
  if (datetime.utcnow() - convo.updated_at) >= timedelta(hours=GAP_H):
  ```

### Notas para DynamoDB
- **PK**: `phone` (String)
- **SK**: No necesario
- **Atributos**: `summary`, `updated_at` (timestamp)
- **Relación con Turns**: Manejar vía query a tabla Turn con `conversation_id` en SK

---

## 3. Turn

### Esquema actual (PostgreSQL)
```python
id: Integer (PK, autoincrement)
conversation_id: Integer (FK to conversations.id, NOT NULL)
role: String (NOT NULL) # "user" | "assistant"
content: Text (NOT NULL, default="")
created_at: DateTime (default=utcnow)
wa_msg_id: String(200) (UNIQUE, indexed, nullable)
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory.save_turn()`
  ```python
  db.session.add(Turn(
      conversation_id=convo.id,
      role=role,
      content=content,
      wa_msg_id=wa_msg_id
  ))
  db.session.commit()
  ```
  - **Idempotencia**: `IntegrityError` en `wa_msg_id` duplicado (UNIQUE constraint)

#### **Read**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory._recent_turns(convo, limit)`
  - **Query**: 
    ```python
    Turn.query
        .filter_by(conversation_id=convo.id)
        .order_by(Turn.created_at.desc())
        .limit(limit)
    ```
  - **Índice requerido**: GSI por `conversation_id` + sort por `created_at` DESC

- **Archivo**: `aws/handler.py`
- **Función**: `lambda_handler()` - actualización post-creación
  - **Query**: `Turn.query.filter_by(wa_msg_id=wamid).first()`
  - **Índice requerido**: GSI por `wa_msg_id` (UNIQUE)

#### **Update**
- **Archivo**: `aws/handler.py`
- **Función**: `lambda_handler()` - actualizar contenido de turn tras transcripción/extracción
  ```python
  turn = Turn.query.filter_by(wa_msg_id=wamid).first()
  if turn:
      turn.content = user_msg  # o "[button:payload]" o "[audio_forwarded]: text"
      db.session.commit()
  ```

#### **Delete**
- **Archivo**: `app/services/memory_service.py`
- **Función**: `Memory.summarize()` - limpieza de turns antiguos
  ```python
  keep_ids = [t.id for t in _recent_turns(convo, K)]
  Turn.query
      .filter(Turn.conversation_id == convo.id, ~Turn.id.in_(keep_ids))
      .delete(synchronize_session=False)
  db.session.commit()
  ```
  - **Patrón**: Mantener solo los últimos K=8 turns tras resumir

### Filtros y Orden
- **Por `conversation_id`**: Filtro principal
- **Por `wa_msg_id`**: Para idempotencia y actualizaciones
- **Orden por `created_at DESC`**: Para obtener mensajes recientes

### Llaves de Idempotencia
- **`wa_msg_id`**: UNIQUE constraint, permite detectar duplicados en webhook delivery

### Dependencias Temporales
- **`created_at`**: Usado para ordenar mensajes cronológicamente

### Notas para DynamoDB
- **PK**: `conversation_id` (o `phone` si se desnormaliza)
- **SK**: `created_at#wa_msg_id` (permite ordenar por tiempo y garantiza unicidad)
- **GSI**: `wa_msg_id-index` para lookups por idempotencia
- **Atributos**: `role`, `content`
- **TTL**: Considerar TTL automático después de N días si la limpieza manual se vuelve costosa

---

## 4. Reminder

### Esquema actual (PostgreSQL)
```python
id: Integer (PK, autoincrement)
titulo: String (NOT NULL)
date: DateTime (NOT NULL)
wa_msg_id: String(200) (UNIQUE, indexed, nullable)
customer_id: Integer (FK to customers.id, NOT NULL)
appointment_id: BigInteger (FK to appointments.id, indexed, nullable)
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.create_reminder()`
  ```python
  reminder = Reminder(
      customer_id=self.current_customer_id,
      titulo=title,
      date=reminder_dt,
      wa_msg_id=wa_msg_id
  )
  db.session.add(reminder)
  db.session.commit()
  ```

- **Función**: `Orchestrator.schedule_meeting()` - reminder vinculado a appointment
  ```python
  reminder = Reminder(
      customer_id=customer_id,
      titulo=f"Recordatorio: {event_title}",
      date=appt.remind_at,
      appointment_id=appt.id
  )
  db.session.add(reminder)
  db.session.commit()
  ```

#### **Read**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.cancel_reminder()`
  - **Query**: 
    ```python
    Reminder.query
        .filter_by(customer_id=customer.id, wa_msg_id=target_msg_id)
        .first()
    ```
  - **Índice requerido**: GSI por `customer_id` + `wa_msg_id`

- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.cancel_meeting()` - buscar reminder asociado
  - **Query**: `Reminder.query.filter_by(appointment_id=appt.id).first()`
  - **Índice requerido**: GSI por `appointment_id`

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `schedule_event_reminder()` - JOIN con Customer
  - **Query**: 
    ```python
    db.session.query(Reminder)
        .join(Customer)
        .filter(Reminder.id == reminder_id)
        .first()
    ```

- **Función**: `_claim_due_reminders()` - recordatorios vencidos standalone
  - **Query**: 
    ```python
    select(Reminder)
        .join(Customer)
        .filter(Reminder.appointment_id.is_(None))
        .order_by(Reminder.date.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ```
  - **Índice requerido**: GSI por `appointment_id` (IS NULL) + sort por `date`
  - **Filtro temporal**: `date <= now_local` (evaluado en código)

#### **Update**
- No hay updates explícitos detectados.

#### **Delete**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.cancel_reminder()`
  ```python
  db.session.delete(reminder)
  db.session.commit()
  ```

- **Función**: `Orchestrator.cancel_meeting()`
  ```python
  reminder = Reminder.query.filter_by(appointment_id=appt.id).first()
  if reminder:
      db.session.delete(reminder)
  ```

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `_process_reminders()` - tras enviar
  ```python
  sess.delete(r)  # idempotencia: no vuelve a aparecer
  ```

### Filtros y Orden
- **Por `customer_id` + `wa_msg_id`**: Para cancelación por botón
- **Por `appointment_id`**: Para vincular/desvincular con citas
- **Por `appointment_id IS NULL` + `date ASC`**: Para procesar standalone reminders
- **Por `id`**: Lookup directo

### Llaves de Idempotencia
- **`wa_msg_id`**: UNIQUE constraint, usado para identificar mensaje de confirmación

### Dependencias Temporales
- **`date`**: Timestamp de cuándo debe enviarse
- **Timezone**: Se convierte a `LOCAL_TZ` (America/Montevideo) para comparación

### Notas para DynamoDB
- **PK**: `customer_id` (String)
- **SK**: `date#reminder_id` (permite ordenar por fecha de ejecución)
- **GSI-1**: `wa_msg_id-index` para cancelaciones
- **GSI-2**: `appointment_id-index` para lookups por cita
- **GSI-3**: `date-index` para queries globales de reminders vencidos (si no se filtra por customer)
- **Atributos**: `titulo`, `appointment_id` (puede ser NULL)
- **TTL**: Considerar TTL basado en `date + X días` para auto-limpieza

---

## 5. ScheduledMessage

### Esquema actual (PostgreSQL)
```python
id: Integer (PK)
customer_id: Integer (FK to customers.id, NOT NULL)
target_phone: String(20) (NOT NULL)
text: Text (NOT NULL)
send_at: DateTime(timezone=True) (NOT NULL)
status: String(10) (default="pending") # pending/sent/error/sending
created_at: DateTime(timezone=True) (default=now)
wa_msg_id: String(200) (UNIQUE, indexed, nullable)
claimed_at: DateTime (para lock distribuido)
sent_at: DateTime
error_at: DateTime
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/scheduled_message_service.py`
- **Función**: `ScheduledMessageService.create()`
  ```python
  sm = ScheduledMessage(
      customer_id=customer_id,
      target_phone=target_phone,
      text=text,
      send_at=send_at
  )
  db.session.add(sm)
  db.session.commit()
  ```

#### **Read**
- **Archivo**: `app/services/scheduled_message_service.py`
- **Función**: `ScheduledMessageService.run(sm_id)`
  - **Query**: `ScheduledMessage.query.get(sm_id)`

- **Función**: `ScheduledMessageService.reschedule_all_messages()`
  - **Query**: `ScheduledMessage.query.filter_by(status="pending").all()`

- **Archivo**: `app/services/send_message_flow.py`
- **Función**: `_finalize_edit_message()`
  - **Query**: `ScheduledMessage.query.get(sm_id)`

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `schedule_scheduled_message()`
  - **Query**: `db.session.get(ScheduledMessage, sm_id)`

- **Función**: `_claim_pending_scheduled_messages()` - batch processing
  - **Query**: 
    ```python
    select(ScheduledMessage)
        .where(
            ScheduledMessage.status == "pending",
            ScheduledMessage.send_at <= now_utc
        )
        .order_by(ScheduledMessage.send_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ```
  - **Índice requerido**: GSI por `status` + `send_at` (compound)
  - **Lock distribuido**: `with_for_update(skip_locked=True)` evita doble procesamiento

#### **Update**
- **Archivo**: `app/services/scheduled_message_service.py`
- **Función**: `ScheduledMessage.mark_sent()`
  ```python
  self.status = "sent"
  db.session.commit()
  ```

- **Función**: `ScheduledMessage.mark_error()`
  ```python
  self.status = "error"
  db.session.commit()
  ```

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `schedule_scheduled_message()` - ajustar fecha
  ```python
  if sm.send_at != send_at:
      sm.send_at = send_at
      db.session.commit()
  ```

- **Función**: `_claim_pending_scheduled_messages()` - marcar como "sending"
  ```python
  for r in rows:
      r.status = "sending"
      r.claimed_at = now_utc
  ```

- **Función**: `_process_scheduled_messages()` - marcar resultado
  ```python
  sm.status = "sent"
  sm.sent_at = now_utc
  # o
  sm.status = "error"
  sm.error_at = now_utc
  ```

#### **Delete**
- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `cancel_scheduled_message(sm_id)`
  ```python
  sm = db.session.get(ScheduledMessage, sm_id)
  if sm:
      db.session.delete(sm)
      db.session.commit()
  ```

- **Archivo**: `app/services/scheduled_message_service.py`
- **Función**: `ScheduledMessageService.run()` - tras envío exitoso
  ```python
  sm.mark_sent()
  db.session.delete(sm)  # limpiar tras envío
  db.session.commit()
  ```

### Filtros y Orden
- **Por `status = "pending"` + `send_at <= now`**: Query principal para procesamiento
- **Por `id`**: Lookup directo
- **Orden por `send_at ASC`**: Para procesar mensajes más antiguos primero

### Llaves de Idempotencia
- **`wa_msg_id`**: UNIQUE constraint, usado para identificar mensaje de confirmación

### Dependencias Temporales
- **`send_at`**: Timestamp UTC de cuándo debe enviarse
- **`created_at`**: Timestamp de creación
- **`claimed_at`**, `sent_at`, `error_at`: Timestamps para tracking de procesamiento

### Máquina de Estados
```
pending → sending → sent (delete)
                 → error
```

### Notas para DynamoDB
- **PK**: `customer_id` (String)
- **SK**: `send_at#scheduled_message_id` (permite ordenar por fecha de envío)
- **GSI-1**: `status-send_at-index` para queries de mensajes pendientes globales
  - PK: `status`
  - SK: `send_at`
  - Esto permite `Query` eficiente de todos los mensajes "pending" ordenados por fecha
- **GSI-2**: `wa_msg_id-index` para cancelaciones
- **Atributos**: `target_phone`, `text`, `created_at`, `claimed_at`, `sent_at`, `error_at`
- **TTL**: Considerar TTL basado en `sent_at + 7 días` o `error_at + 30 días`
- **Lock distribuido**: 
  - Implementar con conditional writes: `attribute_not_exists(claimed_at) OR claimed_at < :stale_threshold`
  - O usar DynamoDB Streams para procesamiento FIFO

---

## 6. AssistantConversation

### Esquema actual (PostgreSQL)
```python
id: Integer (PK, autoincrement)
customer_id: Integer (FK to customers.id, indexed, nullable)
wa_phone: String(32) (UNIQUE, indexed, NOT NULL)
conversation_id: String(128) (UNIQUE, NOT NULL)
status: String(16) (NOT NULL, default="active")
meta: JSONB (nullable)
last_wa_msg_id: String(200) (nullable)
last_response_id: String(128) (nullable)
created_at: DateTime(timezone=True) (server_default=now, NOT NULL)
updated_at: DateTime(timezone=True) (server_default=now, onupdate=now, NOT NULL)
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/assistant_conversation_service.py`
- **Función**: `AssistantConversationService.find_or_create()`
  ```python
  conversation_id = str(uuid.uuid4())
  new_conversation = AssistantConversation(
      customer_id=customer_id,
      wa_phone=wa_phone,
      conversation_id=conversation_id,
      status="active"
  )
  db.session.add(new_conversation)
  db.session.commit()
  ```

#### **Read**
- **Archivo**: `app/services/assistant_conversation_service.py`
- **Función**: `AssistantConversationService.find_or_create()`
  - **Query**: `AssistantConversation.query.filter_by(wa_phone=wa_phone).first()`
  - **Índice requerido**: GSI por `wa_phone` (UNIQUE)

- **Función**: `AssistantConversationService.get_last_response_id()`
  - **Query**: `AssistantConversation.query.filter_by(wa_phone=wa_phone).first()`

- **Función**: `AssistantConversationService.update_last_response_id()`
  - **Query**: `AssistantConversation.query.filter_by(wa_phone=wa_phone).first()`

- **Función**: `AssistantConversationService.update_last_wa_msg_id()`
  - **Query**: `AssistantConversation.query.filter_by(wa_phone=wa_phone).first()`

#### **Update**
- **Archivo**: `app/services/assistant_conversation_service.py`
- **Función**: `AssistantConversationService.find_or_create()` - actualizar customer_id
  ```python
  conversation.customer_id = conversation.customer_id or customer_id
  db.session.commit()
  ```

- **Función**: `AssistantConversationService.update_last_response_id()`
  ```python
  conversation.last_response_id = response_id
  db.session.commit()
  ```

- **Función**: `AssistantConversationService.update_last_wa_msg_id()`
  ```python
  conversation.last_wa_msg_id = wa_msg_id
  db.session.commit()
  ```

#### **Delete**
- No hay deletes detectados.

### Filtros y Orden
- **Por `wa_phone`** (UNIQUE): Filtro principal y único acceso detectado

### Llaves de Idempotencia
- **`wa_phone`**: UNIQUE constraint, un registro por número de WhatsApp
- **`conversation_id`**: UNIQUE, UUID generado

### Dependencias Temporales
- **`created_at`**, **`updated_at`**: Timestamps automáticos (no se usan en queries detectados)

### Notas para DynamoDB
- **PK**: `wa_phone` (String)
- **SK**: No necesario (entidad simple, acceso directo por PK)
- **Atributos**: `customer_id`, `conversation_id`, `status`, `meta` (JSON), `last_wa_msg_id`, `last_response_id`, `created_at`, `updated_at`
- **GSI**: `conversation_id-index` si se requiere lookup inverso por conversation_id (no detectado en código actual)
- **Patrón de acceso dominante**: GetItem por `wa_phone`

---

## 7. Appointment

### Esquema actual (PostgreSQL)
```python
id: BigInteger (PK, autoincrement)
customer_id: Integer (FK to customers.id, indexed, NOT NULL)
conversation_id: Integer (FK to conversations.id, indexed, nullable)
assistant_conversation_id: Integer (FK to assistant_conversations.id, indexed, nullable)

title: String(200) (NOT NULL)
description: Text (nullable)
mode: String(16) (nullable) # 'online' | 'presencial'
location: Text (nullable)
host: String(80) (nullable)
service: String(80) (nullable)

starts_at: DateTime(timezone=True) (NOT NULL)
ends_at: DateTime(timezone=True) (NOT NULL)
timezone: String(64) (NOT NULL, default="America/Montevideo")

status: Enum (NOT NULL, default="scheduled") # scheduled/canceled/completed/no_show
intent_wa_msg_id: String(200) (UNIQUE, indexed, nullable)
confirm_wa_msg_id: String(200) (UNIQUE, indexed, nullable)
cancel_prompt_wa_msg_id: String(200) (UNIQUE, indexed, nullable)
cancel_confirm_wa_msg_id: String(200) (UNIQUE, indexed, nullable)

remind_before_min: Integer (NOT NULL, default=60)
remind_at: DateTime(timezone=True) (indexed, nullable)
last_reminder_sent_at: DateTime(timezone=True) (nullable)
reminder_status: Enum (NOT NULL, default="pending") # pending/sent/skipped/error

google_calendar_id: String(128) (nullable)
google_event_id: String(128) (nullable)
google_meet_link: Text (nullable)

source: String(32) (NOT NULL, default="wa")
cancel_reason: Text (nullable)
meta: JSONB (nullable)

created_at: DateTime(timezone=True) (server_default=now, NOT NULL)
updated_at: DateTime(timezone=True) (server_default=now, onupdate=now, NOT NULL)
```

### Índices Especiales (PostgreSQL)
```python
# Prevenir doble booking
ux_appointments_active_customer_slot (customer_id, starts_at) UNIQUE WHERE status='scheduled'

# Queries de procesamiento
ix_appointments_status_starts_at (status, starts_at)
ix_appointments_remind_pending (remind_at) WHERE status='scheduled' AND reminder_status='pending'
ix_appointments_customer_upcoming (customer_id, starts_at) WHERE status='scheduled'

# Sincronización con Google Calendar
ux_appointments_google_event (google_calendar_id, google_event_id) UNIQUE WHERE google_event_id IS NOT NULL
```

### Operaciones CRUD

#### **Create**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.schedule_meeting()`
  ```python
  appt = Appointment(
      customer_id=customer_id,
      title=event_title,
      description=f"Reunión agendada vía WhatsApp con {customer_name}",
      starts_at=starts_at.astimezone(timezone.utc),
      ends_at=ends_at.astimezone(timezone.utc),
      timezone=str(local_tz),
      status='scheduled',
      google_calendar_id=cal_result.get("calendar_id"),
      google_event_id=cal_result["event_id"],
      google_meet_link=cal_result.get("meet_link"),
      remind_before_min=SETTINGS.EVENT_ADVANCE_MINUTES,
      reminder_status='pending',
      source='wa',
      intent_wa_msg_id=intent_wa_msg_id
  )
  appt.remind_at = appt.starts_at - timedelta(minutes=appt.remind_before_min)
  db.session.add(appt)
  db.session.flush()  # Para obtener appt.id antes de commit
  ```

#### **Read**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.schedule_meeting()` - verificar idempotencia
  - **Query**: 
    ```python
    Appointment.query.filter_by(
        intent_wa_msg_id=intent_wa_msg_id,
        status='scheduled'
    ).first()
    ```
  - **Índice requerido**: GSI por `intent_wa_msg_id` (UNIQUE) + filtro por `status`

- **Función**: `Orchestrator.cancel_meeting()` - buscar por ID o event_id
  - **Query por ID**: `Appointment.query.get(appointment_id)`
  - **Query por event_id**: `Appointment.query.filter_by(google_event_id=google_event_id).first()`
  - **Índice requerido**: GSI por `google_event_id`

- **Función**: `Orchestrator.list_upcoming_appointments()` - próximas citas
  - **Query**: 
    ```python
    Appointment.query
        .filter_by(customer_id=cust_id, status='scheduled')
        .filter(Appointment.starts_at >= now_utc)
        .order_by(Appointment.starts_at.asc())
    ```
  - **Índice requerido**: GSI por `customer_id` + `status` + sort por `starts_at`

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `_claim_due_appointments()` - recordatorios pendientes
  - **Query**: 
    ```python
    select(Appointment)
        .filter(
            Appointment.status == 'scheduled',
            Appointment.reminder_status == 'pending',
            Appointment.remind_at <= now_utc
        )
        .order_by(Appointment.remind_at.asc())
        .limit(limit)
        .with_for_update(skip_locked=True)
    ```
  - **Índice requerido**: GSI por `status` + `reminder_status` + `remind_at` (compound)
  - **Lock distribuido**: `with_for_update(skip_locked=True)`

- **Función**: `_process_appointments()` - obtener customer
  - **Query**: `sess.get(Customer, appt.customer_id)`

#### **Update**
- **Archivo**: `app/services/orchestrator.py`
- **Función**: `Orchestrator.schedule_meeting()` - guardar wa_msg_id de confirmación
  ```python
  appt.confirm_wa_msg_id = wa_response["messages"][0]["id"]
  db.session.commit()
  ```

- **Función**: `Orchestrator.cancel_meeting()` - cancelar cita
  ```python
  appt.status = 'canceled'
  if cancel_reason:
      appt.cancel_reason = cancel_reason
  if appt.reminder_status == 'pending':
      appt.reminder_status = 'skipped'
  # ...
  appt.cancel_confirm_wa_msg_id = wa_response["messages"][0]["id"]
  db.session.commit()
  ```

- **Archivo**: `app/services/scheduler_service.py`
- **Función**: `_process_appointments()` - marcar recordatorio enviado
  ```python
  appt.reminder_status = 'sent'
  appt.last_reminder_sent_at = now_utc
  # o
  appt.reminder_status = 'error'
  # o (si detecta cancelación externa)
  appt.status = 'canceled'
  appt.reminder_status = 'skipped'
  appt.cancel_reason = "Cancelado externamente (detectado al enviar recordatorio)"
  ```

#### **Delete**
- No hay deletes directos detectados.
- Los appointments cancelados mantienen `status='canceled'` para historial.

### Filtros y Orden
- **Por `customer_id` + `status='scheduled'` + `starts_at >= now`**: Citas futuras de un cliente
- **Por `intent_wa_msg_id` + `status='scheduled'`**: Idempotencia de creación
- **Por `google_event_id`**: Sincronización con Google Calendar
- **Por `status='scheduled'` + `reminder_status='pending'` + `remind_at <= now`**: Procesamiento de recordatorios
- **Orden por `starts_at ASC`**: Citas en orden cronológico
- **Orden por `remind_at ASC`**: Recordatorios en orden de envío

### Llaves de Idempotencia
- **`intent_wa_msg_id`**: UNIQUE, previene doble agendado desde mismo mensaje
- **`confirm_wa_msg_id`**: UNIQUE, identifica mensaje de confirmación
- **`cancel_prompt_wa_msg_id`**: UNIQUE, identifica prompt de cancelación
- **`cancel_confirm_wa_msg_id`**: UNIQUE, identifica confirmación de cancelación
- **Constraint UNIQUE `(customer_id, starts_at)` WHERE `status='scheduled'`**: Previene doble booking de slots

### Dependencias Temporales
- **`starts_at`**, **`ends_at`**: Timestamps UTC del evento
- **`remind_at`**: Calculado como `starts_at - remind_before_min`
- **`timezone`**: String de timezone (e.g., "America/Montevideo") para conversiones
- **Validación**: `ends_at > starts_at` (CHECK constraint)

### Máquina de Estados
```
scheduled → canceled
         → completed
         → no_show

reminder_status:
pending → sent
       → skipped (si se cancela la cita)
       → error
```

### Notas para DynamoDB
- **PK**: `customer_id` (String)
- **SK**: `starts_at#appointment_id` (permite ordenar citas por fecha)

- **GSI-1**: `intent_wa_msg_id-index` para idempotencia
  - PK: `intent_wa_msg_id`
  - Permite verificar duplicados rápidamente

- **GSI-2**: `google_event_id-index` para sincronización
  - PK: `google_event_id`
  - Permite lookup por evento de Google Calendar

- **GSI-3**: `status-starts_at-index` para queries globales de citas activas
  - PK: `status`
  - SK: `starts_at`
  - Permite queries tipo "todas las citas scheduled ordenadas por fecha"

- **GSI-4**: `reminder_status-remind_at-index` para procesamiento de recordatorios
  - PK: `reminder_status#status` (e.g., "pending#scheduled")
  - SK: `remind_at`
  - Permite query eficiente de recordatorios pendientes vencidos

- **Atributos complejos**:
  - `meta`: JSON document
  - `timezone`: String, importante para conversiones de hora

- **Prevención de doble booking**:
  - Implementar con conditional write: `attribute_not_exists(PK) AND attribute_not_exists(SK)`
  - GSI adicional `customer_id-starts_at-index` con projection de `status` para validar antes de escribir

- **TTL**: Considerar TTL basado en `ends_at + 90 días` para archivado automático de citas antiguas

---

## Resumen de Patrones de Acceso Críticos

### 1. Lookups por Idempotencia (WhatsApp Message IDs)
- `Turn.wa_msg_id` (UNIQUE)
- `Reminder.wa_msg_id` (UNIQUE)
- `ScheduledMessage.wa_msg_id` (UNIQUE)
- `Appointment.intent_wa_msg_id` (UNIQUE)
- `Appointment.confirm_wa_msg_id` (UNIQUE)

**Estrategia DynamoDB**: GSI con `wa_msg_id` como PK para cada tabla

### 2. Queries Temporales con Lock Distribuido
- `ScheduledMessage` WHERE `status='pending'` AND `send_at <= now` ORDER BY `send_at`
- `Appointment` WHERE `status='scheduled'` AND `reminder_status='pending'` AND `remind_at <= now` ORDER BY `remind_at`
- `Reminder` WHERE `appointment_id IS NULL` AND `date <= now` ORDER BY `date`

**Estrategia DynamoDB**: 
- GSI con compound key (e.g., `status-send_at-index`)
- Lock distribuido con conditional updates (`claimed_at`)
- Considerar DynamoDB Streams para FIFO processing

### 3. Relaciones Uno-a-Muchos
- `Customer` → `Reminders`
- `Customer` → `Appointments`
- `Customer` → `ScheduledMessages`
- `Conversation` → `Turns`

**Estrategia DynamoDB**:
- PK = parent ID (e.g., `customer_id`, `phone`)
- SK = timestamp/composite (e.g., `starts_at#appointment_id`, `created_at#turn_id`)

### 4. Constraints UNIQUE Multi-columna
- `Appointment`: `(customer_id, starts_at)` WHERE `status='scheduled'` (prevenir doble booking)
- `Appointment`: `(google_calendar_id, google_event_id)` WHERE `google_event_id IS NOT NULL`

**Estrategia DynamoDB**:
- Pre-check con Query en GSI antes de PutItem
- Usar conditional expressions para atomicidad
- Considerar tabla auxiliar para locks/uniqueness checks

### 5. Range Queries con Filtros Complejos
- Citas futuras: `customer_id` + `status='scheduled'` + `starts_at >= now`
- Turns recientes: `conversation_id` + ORDER BY `created_at DESC` LIMIT K

**Estrategia DynamoDB**:
- GSI con PK = entity ID + SK = timestamp
- FilterExpression para `status` (si no es parte del key)
- Considerar composite keys (e.g., `customer_id#status` como PK)

---

## Consideraciones de Migración

### Timezone Management
- Todos los timestamps en PostgreSQL usan `timezone=True` (aware)
- Local timezone: `America/Montevideo` (GMT-3)
- Conversiones frecuentes entre UTC y local para comparaciones
- **DynamoDB**: Almacenar timestamps como epoch (UTC) + timezone string separado

### Transacciones
- Uso frecuente de `db.session.begin()` para atomicidad
- **DynamoDB**: TransactWriteItems para operaciones multi-item atómicas (máx 100 items)

### Locks Distribuidos
- `with_for_update(skip_locked=True)` en queries de scheduler
- **DynamoDB**: 
  - Conditional updates con `claimed_at` timestamp
  - DynamoDB Streams para procesamiento ordenado
  - Step Functions para workflows complejos

### Cascadas (CASCADE DELETE)
- `Customer` → `Reminders`, `Appointments`, `ScheduledMessages`
- `Conversation` → `Turns`
- **DynamoDB**: Implementar con DynamoDB Streams + Lambda triggers o aplicación manual

### Índices Parciales (PostgreSQL)
- Varios índices con `WHERE` clause (e.g., `WHERE status='scheduled'`)
- **DynamoDB**: 
  - Sparse indexes (GSI automáticamente omite items sin el atributo)
  - Composite keys para incluir filtros comunes

### Auto-incremento
- PostgreSQL usa `SERIAL` / `BIGSERIAL` para PKs
- **DynamoDB**: 
  - Usar UUIDs para PKs
  - O mantener contador en tabla separada con atomic increment
  - O usar timestamp + random suffix

---

## Métricas de Uso Estimadas (para capacity planning)

### Escrituras (Write Capacity Units)
- **Alta frecuencia**: `Turn`, `AssistantConversation` (cada mensaje)
- **Media frecuencia**: `Appointment`, `Reminder`, `ScheduledMessage` (decenas por día)
- **Baja frecuencia**: `Customer`, `Conversation` (esporádicas)

### Lecturas (Read Capacity Units)
- **Alta frecuencia**: `Customer`, `AssistantConversation` (cada webhook)
- **Media frecuencia**: `Turn`, `Appointment` (queries de contexto/listado)
- **Baja frecuencia**: `ScheduledMessage`, `Reminder` (solo en scheduler batch)

### Patrones de Consistencia
- **Strongly Consistent Reads**: 
  - Idempotency checks (wa_msg_id lookups)
  - Double-booking prevention (appointment slot checks)
- **Eventually Consistent Reads**: 
  - Listados (upcoming appointments)
  - Contexto de conversación (turns)

---

**Fin del documento**
