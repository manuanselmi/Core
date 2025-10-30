"""
Repository Provider

Factory para crear y gestionar repositorios DynamoDB con configuración compartida.
Feature flag para migración progresiva desde PostgreSQL.
"""
import os
import logging
from typing import Callable

from app.db.dynamo_client import get_ddb_client, TABLE_NAME
from app.db.repos.customer_repo import CustomerRepo
from app.db.repos.assistant_conversation_repo import AssistantConversationRepo
from app.db.repos.conversation_repo import ConversationRepo
from app.db.repos.turn_repo import TurnRepo
from app.db.repos.appointment_repo import AppointmentRepo
from app.db.repos.reminder_repo import ReminderRepo
from app.db.repos.scheduled_message_repo import ScheduledMessageRepo


class RepositoryProvider:
    """
    Factory centralizado para repositorios DynamoDB.
    
    Provee:
    - Cliente DynamoDB compartido
    - Configuración de logging con correlation ID
    - Feature flag para migración progresiva
    - Instancias de repositorios específicos
    
    Example:
        >>> provider = RepositoryProvider(
        ...     correlation_id_provider=lambda: request_context.correlation_id
        ... )
        >>> customer = provider.customers.get_by_phone("+1234567890")
    """
    
    def __init__(
        self,
        correlation_id_provider: Callable[[], str] | None = None,
        logger: logging.Logger | None = None
    ):
        """
        Inicializa el provider de repositorios.
        
        Args:
            correlation_id_provider: Callable que retorna el correlation ID actual.
                                   Si no se provee, usa lambda: "no-correlation-id"
            logger: Logger personalizado. Si no se provee, usa logger por defecto
        
        Notes:
            - Lee feature flag USE_DYNAMO (env var, default "1")
            - No crea conexiones de red; solo inicializa estructuras
            - DYNAMO_TABLE_NAME debe estar configurado (validado al importar dynamo_client)
        """
        # Feature flag para migración progresiva
        self.use_dynamo = os.getenv("USE_DYNAMO", "1") == "1"
        
        # Correlation ID provider
        self.correlation_id_provider = correlation_id_provider or (lambda: "no-correlation-id")
        
        # Logger
        self.logger = logger or logging.getLogger(__name__)
        
        # Cliente DynamoDB compartido (lazy initialization en producción)
        self._client = None
        
        # Repositorios (se inicializan en propiedades)
        self._customers = None
        self._assistant_conversations = None
        self._conversations = None
        self._turns = None
        self._appointments = None
        self._reminders = None
        self._scheduled_messages = None
    
    @property
    def client(self):
        """Cliente DynamoDB (singleton, lazy initialization)."""
        if self._client is None:
            self._client = get_ddb_client()
        return self._client
    
    @property
    def customers(self) -> CustomerRepo:
        """Repositorio de Customer."""
        if self._customers is None:
            self._customers = CustomerRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._customers
    
    @property
    def assistant_conversations(self) -> AssistantConversationRepo:
        """Repositorio de AssistantConversation."""
        if self._assistant_conversations is None:
            self._assistant_conversations = AssistantConversationRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._assistant_conversations
    
    @property
    def conversations(self) -> ConversationRepo:
        """Repositorio de Conversation."""
        if self._conversations is None:
            self._conversations = ConversationRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._conversations
    
    @property
    def turns(self) -> TurnRepo:
        """Repositorio de Turn."""
        if self._turns is None:
            self._turns = TurnRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._turns
    
    @property
    def appointments(self) -> AppointmentRepo:
        """Repositorio de Appointment."""
        if self._appointments is None:
            self._appointments = AppointmentRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._appointments
    
    @property
    def reminders(self) -> ReminderRepo:
        """Repositorio de Reminder."""
        if self._reminders is None:
            self._reminders = ReminderRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._reminders
    
    @property
    def scheduled_messages(self) -> ScheduledMessageRepo:
        """Repositorio de ScheduledMessage."""
        if self._scheduled_messages is None:
            self._scheduled_messages = ScheduledMessageRepo(
                client=self.client,
                table_name=TABLE_NAME,
                logger=self.logger,
                correlation_id_provider=self.correlation_id_provider
            )
        return self._scheduled_messages
