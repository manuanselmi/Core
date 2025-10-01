from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, time, date
from sqlalchemy import Enum, JSON, UniqueConstraint, Index

db = SQLAlchemy()

# ============================================================
# ================  LEGACY ÚTIL (SE CONSERVA)  ===============
# ============================================================

class Customer(db.Model):
    __tablename__ = 'customers'

    id           = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name         = db.Column(db.String)
    phone        = db.Column(db.String, nullable=False, unique=True)   # compat: único global
    accept_terms = db.Column(db.Boolean, default=False)

    # Rel: recordatorios genéricos (no ligados a bookings)
    recordatorios = db.relationship('Reminder', backref='customer', cascade='all, delete')

class Reminder(db.Model):
    """
    Recordatorio genérico: 'recordame X el día Y'. No está atado a una Booking.
    Lo usamos para features generales (no de agenda de turnos).
    """
    __tablename__ = 'reminders'

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    titulo      = db.Column(db.String, nullable=False)
    date        = db.Column(db.DateTime, nullable=False)  # UTC
    wa_msg_id   = db.Column(db.String(200), nullable=True, unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)

class Conversation(db.Model):
    """
    Memoria por teléfono (legacy). Mantenemos para MemoryService.
    Opcionalmente, en el futuro agregamos tenant_id si lo necesitás.
    """
    __tablename__ = "conversations"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    phone      = db.Column(db.String, nullable=False, unique=True, index=True)
    summary    = db.Column(db.Text, default="")
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

    turns = db.relationship(
        "Turn",
        backref="conversation",
        cascade="all, delete",
        lazy="dynamic",
    )

class Turn(db.Model):
    __tablename__ = "turns"

    id              = db.Column(db.Integer, primary_key=True, autoincrement=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=False)
    role            = db.Column(db.String, nullable=False)  # "user" | "assistant"
    content         = db.Column(db.Text,  nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    wa_msg_id       = db.Column(db.String(200), nullable=True, unique=True, index=True)

# 💬  Mensajes programados a terceros (útil para tu assistant)
class ScheduledMessage(db.Model):
    __tablename__ = "scheduled_messages"

    id            = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    target_phone  = db.Column(db.String(20), nullable=False)
    text          = db.Column(db.Text, nullable=False)
    send_at       = db.Column(db.DateTime(timezone=True), nullable=False)  # UTC
    status        = db.Column(db.String(10), default="pending")            # pending/sent/error
    created_at    = db.Column(db.DateTime(timezone=True), default=db.func.now())
    wa_msg_id     = db.Column(db.String(200), unique=True, index=True)

    customer      = db.relationship("Customer", backref="scheduled_messages")

    def mark_sent(self):
        self.status = "sent"
        db.session.commit()

    def mark_error(self):
        self.status = "error"
        db.session.commit()


# ============================================================
# ========================  CORE  ============================
# ============================================================

# -------- Enums --------
BookingStatus = Enum(
    "PENDING",
    "CONFIRMED",
    "RESCHEDULED",
    "CANCELED",
    "NOSHOW",
    "COMPLETED",
    name="booking_status",
)

# -------- Multitenancy --------
class Tenant(db.Model):
    """
    Reúne la configuración de alto nivel para cada marca/negocio.
    """
    __tablename__ = "tenants"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    slug       = db.Column(db.String(64), nullable=False, unique=True, index=True)  # ej. "barberia_centro"
    name       = db.Column(db.String(120), nullable=False)
    timezone   = db.Column(db.String(64),  nullable=False, default="UTC")
    locale     = db.Column(db.String(8),   nullable=False, default="es")
    brand_meta = db.Column(JSON,           nullable=True)    # colores, logo_url, etc.
    is_active  = db.Column(db.Boolean,     nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), default=db.func.now())
    updated_at = db.Column(db.DateTime(timezone=True), default=db.func.now(), onupdate=db.func.now())

    policies   = db.relationship("TenantPolicy", backref="tenant", cascade="all, delete", lazy="joined")
    templates  = db.relationship("TenantTemplate", backref="tenant", cascade="all, delete", lazy="dynamic")

class TenantPolicy(db.Model):
    """
    Reglas operativas del tenant (cancelaciones, buffers, horizonte de reserva, etc.).
    """
    __tablename__ = "tenant_policies"

    id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id        = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    cancel_min_hours = db.Column(db.Integer, nullable=False, default=24)  # cancelar/reprogramar hasta X horas antes
    max_days_ahead   = db.Column(db.Integer, nullable=False, default=60)  # reservar hasta X días
    buffer_before    = db.Column(db.Integer, nullable=False, default=0)   # min
    buffer_after     = db.Column(db.Integer, nullable=False, default=0)   # min
    no_show_grace    = db.Column(db.Integer, nullable=False, default=10)  # ventana de gracia min

    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_policies_tenant"),
    )

class TenantTemplate(db.Model):
    """
    Catálogo de plantillas por tenant y lenguaje. Soporta canal 'whatsapp' (default) y extensible.
    """
    __tablename__ = "tenant_templates"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id  = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name       = db.Column(db.String(80),  nullable=False)              # ej. "booking_confirmed"
    language   = db.Column(db.String(8),   nullable=False, default="es")
    channel    = db.Column(db.String(32),  nullable=False, default="whatsapp")
    body       = db.Column(JSON,           nullable=False)              # variables + estructura (plantilla)
    is_active  = db.Column(db.Boolean,     nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", "language", name="uq_template_per_tenant_lang"),
    )

# -------- Catálogo de servicios / capacidad --------
class Service(db.Model):
    __tablename__ = "services"

    id                = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id         = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name              = db.Column(db.String(120), nullable=False)
    duration_min      = db.Column(db.Integer, nullable=False)          # duración base
    price_cents       = db.Column(db.Integer, nullable=True)           # opcional
    currency          = db.Column(db.String(3), nullable=True, default="USD")
    buffer_before_min = db.Column(db.Integer, nullable=False, default=0)
    buffer_after_min  = db.Column(db.Integer, nullable=False, default=0)
    requires_resource = db.Column(db.Boolean, nullable=False, default=False)
    active            = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_service_name_per_tenant"),
    )

class Staff(db.Model):
    __tablename__ = "staff"

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name        = db.Column(db.String(120), nullable=False)
    phone       = db.Column(db.String(32), nullable=True)
    calendar_id = db.Column(db.String(120), nullable=True)  # ej. Google calendarId
    active      = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_staff_name_per_tenant"),
    )

class Resource(db.Model):
    __tablename__ = "resources"

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    name      = db.Column(db.String(120), nullable=False)     # ej. "Sillón 1"
    capacity  = db.Column(db.Integer, nullable=False, default=1)
    active    = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_resource_name_per_tenant"),
    )

class StaffService(db.Model):
    __tablename__ = "staff_services"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id  = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    staff_id   = db.Column(db.Integer, db.ForeignKey("staff.id"),   nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"),nullable=False, index=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "staff_id", "service_id", name="uq_staff_service_per_tenant"),
    )

# -------- Disponibilidad --------
class OpeningHours(db.Model):
    """
    Horarios base de atención por tenant (sin distinguir staff).
    Si necesitás horarios por staff, lo resolvemos en servicios (availability) más adelante.
    """
    __tablename__ = "opening_hours"

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    weekday   = db.Column(db.Integer, nullable=False)  # 0=Mon ... 6=Sun
    start     = db.Column(db.Time,    nullable=False)
    end       = db.Column(db.Time,    nullable=False)
    is_open   = db.Column(db.Boolean, nullable=False, default=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "weekday", "start", "end", name="uq_opening_hours_span"),
    )

class ExceptionDate(db.Model):
    """
    Excepciones/feriados: día completo cerrado (is_closed=True) o franja especial ese día.
    """
    __tablename__ = "exception_dates"

    id        = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenants.id"), nullable=False, index=True)
    date      = db.Column(db.Date,     nullable=False)
    is_closed = db.Column(db.Boolean,  nullable=False, default=True)
    start     = db.Column(db.Time,     nullable=True)
    end       = db.Column(db.Time,     nullable=True)
    note      = db.Column(db.String(200), nullable=True)

    __table_args__ = (
        UniqueConstraint("tenant_id", "date", name="uq_exception_per_day"),
    )

# -------- Reservas --------
class Booking(db.Model):
    __tablename__ = "bookings"

    id          = db.Column(db.Integer, primary_key=True, autoincrement=True)
    tenant_id   = db.Column(db.Integer, db.ForeignKey("tenants.id"),   nullable=False, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    service_id  = db.Column(db.Integer, db.ForeignKey("services.id"),  nullable=False, index=True)
    staff_id    = db.Column(db.Integer, db.ForeignKey("staff.id"),     nullable=True,  index=True)
    resource_id = db.Column(db.Integer, db.ForeignKey("resources.id"), nullable=True,  index=True)

    starts_at   = db.Column(db.DateTime(timezone=True), nullable=False)  # UTC
    ends_at     = db.Column(db.DateTime(timezone=True), nullable=False)  # UTC
    status      = db.Column(BookingStatus, nullable=False, default="PENDING")
    source      = db.Column(db.String(32), nullable=False, default="whatsapp")  # whatsapp|api|manual
    note        = db.Column(db.Text,      nullable=True)

    gcal_event_id      = db.Column(db.String(128), nullable=True)  # si hay sync con Google
    rescheduled_from_id= db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=True)

    created_at  = db.Column(db.DateTime(timezone=True), default=db.func.now())
    updated_at  = db.Column(db.DateTime(timezone=True), default=db.func.now(), onupdate=db.func.now())

    __table_args__ = (
        # Anti-solape por staff (cuando staff_id no es NULL)
        UniqueConstraint("tenant_id", "staff_id", "starts_at", name="uq_booking_staff_slot"),
        # Anti-solape por recurso (cuando resource_id no es NULL)
        UniqueConstraint("tenant_id", "resource_id", "starts_at", name="uq_booking_resource_slot"),
        Index("ix_booking_tenant_starts_at", "tenant_id", "starts_at"),
    )

    customer   = db.relationship("Customer", backref="bookings")
    service    = db.relationship("Service")
    staff      = db.relationship("Staff")
    resource   = db.relationship("Resource")
    rescheduled_from = db.relationship("Booking", remote_side=[id])

# Recordatorios operativos (T-24, T-3) ligados a Booking
class BookingReminder(db.Model):
    __tablename__ = "booking_reminders"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    booking_id = db.Column(db.Integer, db.ForeignKey("bookings.id"), nullable=False, index=True)
    kind       = db.Column(db.String(8),  nullable=False)  # "T24"|"T3"
    send_at    = db.Column(db.DateTime(timezone=True), nullable=False)  # UTC
    sent_at    = db.Column(db.DateTime(timezone=True), nullable=True)
    wa_msg_id  = db.Column(db.String(200), unique=True, index=True)
    status     = db.Column(db.String(10), default="pending")  # pending|sent|error

    __table_args__ = (
        UniqueConstraint("booking_id", "kind", name="uq_reminder_per_kind"),
    )

    booking    = db.relationship("Booking", backref="reminders")

# -------- Idempotencia (webhooks y jobs) --------
class IdempotencyKey(db.Model):
    __tablename__ = "idempotency_keys"

    id         = db.Column(db.Integer, primary_key=True, autoincrement=True)
    scope      = db.Column(db.String(64),  nullable=False)  # "whatsapp:messages", "scheduler:reminder", etc.
    key        = db.Column(db.String(200), nullable=False, unique=True, index=True)  # wamid, job_id, etc.
    created_at = db.Column(db.DateTime(timezone=True), default=db.func.now())
