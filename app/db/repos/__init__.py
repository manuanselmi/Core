"""
DynamoDB Repositories

Repositorios específicos para cada entidad del dominio.

Available repositories:
- CustomerRepo: Perfiles de clientes
- AssistantConversationRepo: Conversaciones con asistente OpenAI
- ConversationRepo: Resúmenes de conversación
- TurnRepo: Mensajes individuales
- AppointmentRepo: Citas agendadas
- ReminderRepo: Recordatorios standalone
- ScheduledMessageRepo: Mensajes programados

Uso:
    from app.db.repository_provider import RepositoryProvider
    
    provider = RepositoryProvider()
    customers_repo = provider.customers
    appointments_repo = provider.appointments
"""

__all__ = [
    'CustomerRepo',
    'AssistantConversationRepo',
    'ConversationRepo',
    'TurnRepo',
    'AppointmentRepo',
    'ReminderRepo',
    'ScheduledMessageRepo',
]

from .customer_repo import CustomerRepo
from .assistant_conversation_repo import AssistantConversationRepo
from .conversation_repo import ConversationRepo
from .turn_repo import TurnRepo
from .appointment_repo import AppointmentRepo
from .reminder_repo import ReminderRepo
from .scheduled_message_repo import ScheduledMessageRepo
