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
