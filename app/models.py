from flask_sqlalchemy import SQLAlchemy
from datetime import datetime
import uuid
from datetime import datetime, time, date
from sqlalchemy.dialects.postgresql import UUID          # ← sólo Postgres
from sqlalchemy import Enum, JSON, UniqueConstraint


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
    content         = db.Column(db.Text,  nullable=False)
    created_at      = db.Column(db.DateTime, default=datetime.utcnow)
    wa_msg_id       = db.Column(db.String(200), nullable=True, unique=True, index=True)
    
class Habito(db.Model):
    __tablename__ = "habitos"

    id                 = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    customer_id        = db.Column(db.Integer,
                                   db.ForeignKey("customers.id"),
                                   nullable=False)
    nombre             = db.Column(db.String,  nullable=False)

    # evita duplicados (un hábito por cliente-nombre)
    __table_args__ = (
        UniqueConstraint("customer_id", "nombre", name="uix_customer_nombre"),
    )
    frecuencia         = db.Column(db.String,  default="diaria")   
    activo             = db.Column(db.Boolean, default=True)
    recordatorio_horas = db.Column(db.Integer, nullable=False)     
    fecha_ultimo_envio = db.Column(db.Date)
    dias_inactivos     = db.Column(db.Integer, default=0)
    creado_en          = db.Column(db.DateTime, default=datetime.utcnow)

    registros = db.relationship(
        "RegistroHabito",
        backref="habito",
        cascade="all, delete",
        lazy="dynamic",
    )

EstadoHabitoEnum = Enum(
    "pendiente",
    "cumplido",
    "no_cumplido",
    name="estado_habito",
)

class RegistroHabito(db.Model):
    __tablename__ = "registro_habitos"

    id             = db.Column(db.BigInteger, primary_key=True, autoincrement=True)
    habito_id      = db.Column(db.BigInteger,
                               db.ForeignKey("habitos.id"))
    fecha          = db.Column(db.Date, nullable=False)    
    __table_args__ = (
        UniqueConstraint("habito_id", "fecha", name="uix_habito_fecha"),
    )      
    estado         = db.Column(EstadoHabitoEnum,
                               nullable=False,
                               default="pendiente")
    mensaje_id     = db.Column(db.String(200))     
    hora_envio     = db.Column(db.Time)
    hora_respuesta = db.Column(db.Time)
    registrado_en  = db.Column(db.DateTime, default=datetime.utcnow)

    # evita duplicados (un registro por hábito-día)
    __table_args__ = (
        UniqueConstraint("habito_id", "fecha", name="uix_habito_fecha"),
    )

Customer.habitos = db.relationship(
    "Habito",
    backref="customer",
    cascade="all, delete",
    lazy="dynamic",
)

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
# ------------------------------------------------------------