from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid
from datetime import datetime, time, date
from sqlalchemy.dialects.postgresql import UUID          # ← sólo Postgres
from sqlalchemy import Enum, JSON, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB


db = SQLAlchemy()

class Customer(db.Model):
    __tablename__ = 'customers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String)
    phone = db.Column(db.String, nullable=False, unique=True)
    accept_terms = db.Column(db.Boolean, default=False)

    recordatorios = db.relationship('Reminder', backref='customer', cascade='all, delete')

class Reminder(db.Model):
    __tablename__ = 'reminders'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    titulo = db.Column(db.String, nullable=False)
    date = db.Column(db.DateTime, nullable=False)
    wa_msg_id = db.Column(db.String(200), nullable=True, unique=True, index=True)
    customer_id = db.Column(db.Integer, db.ForeignKey('customers.id'), nullable=False)
    appointment_id = db.Column(db.BigInteger, db.ForeignKey("appointments.id"), nullable=True, index=True)

class Conversation(db.Model):
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
    content         = db.Column(db.Text,  nullable=False, default="")  # Permitir vacío temporalmente
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    wa_msg_id       = db.Column(db.String(200), nullable=True, unique=True, index=True)

# ------------------------------------------------------------
# 💬  Mensajes programados

class ScheduledMessage(db.Model):
    __tablename__ = "scheduled_messages"

    id            = db.Column(db.Integer, primary_key=True)
    customer_id   = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False)
    target_phone  = db.Column(db.String(20), nullable=False)
    text          = db.Column(db.Text, nullable=False)
    send_at       = db.Column(db.DateTime(timezone=True), nullable=False)
    status        = db.Column(db.String(10), default="pending")  # pending/sent/error
    created_at    = db.Column(db.DateTime(timezone=True), default=db.func.now())
    wa_msg_id     = db.Column(db.String(200), unique=True, index=True)

    customer      = db.relationship("Customer", backref="scheduled_messages")

    def mark_sent(self):
        self.status = "sent"
        db.session.commit()

    def mark_error(self):
        self.status = "error"
        db.session.commit()


class AssistantThread(db.Model):
    __tablename__ = "assistant_threads"

    id             = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id    = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    wa_phone       = db.Column(db.String(32), nullable=False, index=True)  
    thread_id      = db.Column(db.String(128), nullable=False, unique=True)
    status         = db.Column(db.String(16), nullable=False, default="active")  
    meta           = db.Column("metadata", JSONB, nullable=True)
    last_wa_msg_id = db.Column(db.String(200), nullable=True)
    dedupe_key     = db.Column(db.String(200), nullable=True)  
    created_at     = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at     = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now(), nullable=False)
    last_used_at   = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)

    __table_args__ = (
        db.Index("ix_assistant_threads_customer_id", "customer_id"),
        db.Index("ix_assistant_threads_wa_phone", "wa_phone"),
    )
    
    
class AssistantConversation(db.Model):
    __tablename__ = "assistant_conversations"

    id               = db.Column(db.Integer, primary_key=True, autoincrement=True)
    customer_id      = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=True, index=True)
    wa_phone         = db.Column(db.String(32), nullable=False, unique=True, index=True)
    conversation_id  = db.Column(db.String(128), nullable=False, unique=True)
    status           = db.Column(db.String(16), nullable=False, default="active")
    meta         = db.Column(JSONB, nullable=True)
    last_wa_msg_id   = db.Column(db.String(200), nullable=True)
    last_response_id = db.Column(db.String(128), nullable=True)
    created_at       = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at       = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), onupdate=db.func.now(), nullable=False)

from sqlalchemy import CheckConstraint

class Appointment(db.Model):
    __tablename__ = "appointments"

    # PK
    id = db.Column(db.BigInteger, primary_key=True, autoincrement=True)

    # Relaciones
    customer_id = db.Column(db.Integer, db.ForeignKey("customers.id"), nullable=False, index=True)
    conversation_id = db.Column(db.Integer, db.ForeignKey("conversations.id"), nullable=True, index=True)
    assistant_conversation_id = db.Column(db.Integer, db.ForeignKey("assistant_conversations.id"), nullable=True, index=True)

    # Contenido
    title = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    mode = db.Column(db.String(16), nullable=True)  # 'online' | 'presencial'
    location = db.Column(db.Text, nullable=True)
    host = db.Column(db.String(80), nullable=True)    
    service = db.Column(db.String(80), nullable=True)  

    # Tiempo
    starts_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ends_at = db.Column(db.DateTime(timezone=True), nullable=False)
    timezone = db.Column(db.String(64), nullable=False, default="America/Montevideo")

    # Estado (usa ENUMs ya creados en Postgres)
    status = db.Column(db.Enum(
        "scheduled", "canceled", "completed", "no_show",
        name="appointment_status", create_type=False
    ), nullable=False, default="scheduled")

    # Idempotencia / WhatsApp
    intent_wa_msg_id         = db.Column(db.String(200), unique=True, index=True, nullable=True)
    confirm_wa_msg_id        = db.Column(db.String(200), unique=True, index=True, nullable=True)
    cancel_prompt_wa_msg_id  = db.Column(db.String(200), unique=True, index=True, nullable=True)
    cancel_confirm_wa_msg_id = db.Column(db.String(200), unique=True, index=True, nullable=True)

    # Recordatorio
    remind_before_min       = db.Column(db.Integer, nullable=False, default=60)
    remind_at               = db.Column(db.DateTime(timezone=True), nullable=True, index=True)
    last_reminder_sent_at   = db.Column(db.DateTime(timezone=True), nullable=True)
    reminder_status = db.Column(db.Enum(
        "pending", "sent", "skipped", "error",
        name="appointment_reminder_status", create_type=False
    ), nullable=False, default="pending")

    # Google Calendar
    google_calendar_id = db.Column(db.String(128), nullable=True)
    google_event_id    = db.Column(db.String(128), nullable=True)
    google_meet_link   = db.Column(db.Text, nullable=True)

    # Otros
    source        = db.Column(db.String(32), nullable=False, default="wa")
    cancel_reason = db.Column(db.Text, nullable=True)
    meta          = db.Column(JSONB, nullable=True)

    # Timestamps
    created_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(), nullable=False)
    updated_at = db.Column(db.DateTime(timezone=True), server_default=db.func.now(),
                           onupdate=db.func.now(), nullable=False)

    # Backrefs (coherente con tu estilo)
    customer = db.relationship("Customer", backref="appointments")

    __table_args__ = (
        # CHECKs
        CheckConstraint("ends_at > starts_at", name="chk_appointments_time"),
        CheckConstraint("(mode IS NULL) OR (mode IN ('online','presencial'))", name="chk_appointments_mode"),

        # Índices parciales y compuestos
        db.Index(
            "ux_appointments_active_customer_slot",
            "customer_id", "starts_at",
            unique=True,
            postgresql_where=db.text("status = 'scheduled'")
        ),
        db.Index("ix_appointments_status_starts_at", "status", "starts_at"),
        db.Index(
            "ix_appointments_remind_pending",
            "remind_at",
            postgresql_where=db.text("status = 'scheduled' AND reminder_status = 'pending'")
        ),
        db.Index(
            "ux_appointments_google_event",
            "google_calendar_id", "google_event_id",
            unique=True,
            postgresql_where=db.text("google_event_id IS NOT NULL")
        ),
        db.Index(
            "ix_appointments_customer_upcoming",
            "customer_id", "starts_at",
            postgresql_where=db.text("status = 'scheduled'")
        ),
    )

    def __repr__(self):
        return f"<Appointment id={self.id} customer_id={self.customer_id} {self.starts_at}→{self.ends_at} status={self.status}>"