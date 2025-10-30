"""
DynamoDB Data Access Layer

Capa de acceso a datos para DynamoDB con arquitectura single-table.

Módulos principales:
- dynamo_keys: Generación de PK/SK
- dynamo_client: Cliente configurado para Lambda
- dynamo_marshalling: Serialización/deserialización
- dynamo_repo_base: Clase base para repositorios
- repository_provider: Factory de repositorios

Uso:
    from app.db.repository_provider import RepositoryProvider
    
    provider = RepositoryProvider(
        correlation_id_provider=lambda: request_context.correlation_id
    )
    
    customer = provider.customers.get_by_phone("+1234567890")
"""

__all__ = [
    'RepositoryProvider',
]

from .repository_provider import RepositoryProvider
