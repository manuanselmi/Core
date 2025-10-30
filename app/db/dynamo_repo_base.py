"""
DynamoDB Repository Base Class

Clase base para todos los repositorios DynamoDB, provee operaciones
comunes con logging de consumo de capacidad y manejo de errores.
"""
import logging
from typing import Callable, Any
from botocore.exceptions import ClientError

from .dynamo_marshalling import to_ddb, from_ddb


class DynamoRepoBase:
    """
    Clase base para repositorios DynamoDB.
    
    Provee operaciones CRUD con:
    - Serialización/deserialización automática
    - Logging de ConsumedCapacity con correlation ID
    - Manejo consistente de errores
    """
    
    def __init__(
        self,
        client: Any,
        table_name: str,
        logger: logging.Logger,
        correlation_id_provider: Callable[[], str]
    ):
        """
        Inicializa el repositorio base.
        
        Args:
            client: Cliente boto3 DynamoDB (low-level)
            table_name: Nombre de la tabla DynamoDB
            logger: Logger para operaciones del repositorio
            correlation_id_provider: Callable que retorna correlation ID actual
        """
        self.client = client
        self.table_name = table_name
        self.logger = logger
        self.correlation_id_provider = correlation_id_provider
    
    def _log_capacity(self, operation: str, consumed_capacity: dict | None):
        """
        Loguea el consumo de capacidad de una operación.
        
        Args:
            operation: Nombre de la operación (PutItem, Query, etc.)
            consumed_capacity: Dict con información de ConsumedCapacity
        """
        # Removed verbose capacity logging - user requested reducing CloudWatch noise
        pass
    
    def put_strict(self, item: dict, condition: str | None = None) -> dict:
        """
        Inserta un item con condición opcional (típicamente para idempotencia).
        
        Args:
            item: Item a insertar (formato Python, será serializado)
            condition: Expresión de condición opcional (ej: "attribute_not_exists(pk)")
        
        Returns:
            Item insertado (deserializado)
        
        Raises:
            ClientError: Si la condición falla o hay error de DynamoDB
        """
        params = {
            'TableName': self.table_name,
            'Item': to_ddb(item),
            'ReturnConsumedCapacity': 'TOTAL'
        }
        
        if condition:
            params['ConditionExpression'] = condition
        
        try:
            response = self.client.put_item(**params)
            self._log_capacity('PutItem', response.get('ConsumedCapacity'))
            return item
        except ClientError as e:
            self.logger.error("[DDB:PutItem] Error: %s", e.response['Error']['Code'])
            raise
    
    def update_conditional(
        self,
        key: dict,
        update_expr: str,
        expr_attr_names: dict | None = None,
        expr_attr_values: dict | None = None,
        condition: str | None = None
    ) -> dict:
        """
        Actualiza un item con expresión de actualización y condición opcional.
        
        Args:
            key: Clave del item (pk, sk) - será serializada
            update_expr: Expresión de actualización (ej: "SET #status = :val")
            expr_attr_names: Mapeo de nombres de atributos (ej: {"#status": "status"})
            expr_attr_values: Mapeo de valores - será serializado
            condition: Expresión de condición opcional
        
        Returns:
            Item actualizado (deserializado)
        
        Raises:
            ClientError: Si la condición falla o hay error de DynamoDB
        """
        params = {
            'TableName': self.table_name,
            'Key': to_ddb(key),
            'UpdateExpression': update_expr,
            'ReturnValues': 'ALL_NEW',
            'ReturnConsumedCapacity': 'TOTAL'
        }
        
        if expr_attr_names:
            params['ExpressionAttributeNames'] = expr_attr_names
        
        if expr_attr_values:
            params['ExpressionAttributeValues'] = to_ddb(expr_attr_values)
        
        if condition:
            params['ConditionExpression'] = condition
        
        try:
            response = self.client.update_item(**params)
            self._log_capacity('UpdateItem', response.get('ConsumedCapacity'))
            
            if 'Attributes' in response:
                return from_ddb(response['Attributes'])
            return {}
        except ClientError as e:
            self.logger.error("[DDB:UpdateItem] Error: %s", e.response['Error']['Code'])
            raise
    
    def get_item(self, key: dict) -> dict | None:
        """
        Obtiene un item por su clave.
        
        Args:
            key: Clave del item (pk, sk) - será serializada
        
        Returns:
            Item deserializado o None si no existe
        """
        params = {
            'TableName': self.table_name,
            'Key': to_ddb(key),
            'ReturnConsumedCapacity': 'TOTAL'
        }
        
        try:
            response = self.client.get_item(**params)
            self._log_capacity('GetItem', response.get('ConsumedCapacity'))
            
            if 'Item' in response:
                return from_ddb(response['Item'])
            return None
        except ClientError as e:
            correlation_id = self.correlation_id_provider()
            self.logger.error(
                f"[DDB:GetItem] Error: {e.response['Error']['Code']} "
                f"correlation_id={correlation_id}"
            )
            raise
    
    def query(
        self,
        key_condition_expr: str,
        expr_attr_names: dict | None = None,
        expr_attr_values: dict | None = None,
        filter_expr: str | None = None,
        index_name: str | None = None,
        scan_forward: bool = True,
        limit: int | None = None,
        **kwargs
    ) -> list[dict]:
        """
        Ejecuta una Query en DynamoDB.
        
        Args:
            key_condition_expr: Expresión de condición de clave
            expr_attr_names: Mapeo de nombres de atributos
            expr_attr_values: Mapeo de valores - será serializado
            filter_expr: Expresión de filtro opcional
            index_name: Nombre del GSI opcional
            scan_forward: True para orden ascendente, False para descendente
            limit: Límite de items a retornar
            **kwargs: Parámetros adicionales para query
        
        Returns:
            Lista de items deserializados
        """
        params = {
            'TableName': self.table_name,
            'KeyConditionExpression': key_condition_expr,
            'ScanIndexForward': scan_forward,
            'ReturnConsumedCapacity': 'TOTAL'
        }
        
        if expr_attr_names:
            params['ExpressionAttributeNames'] = expr_attr_names
        
        if expr_attr_values:
            params['ExpressionAttributeValues'] = to_ddb(expr_attr_values)
        
        if filter_expr:
            params['FilterExpression'] = filter_expr
        
        if index_name:
            params['IndexName'] = index_name
        
        if limit:
            params['Limit'] = limit
        
        # Permitir parámetros adicionales (ProjectionExpression, etc.)
        params.update(kwargs)
        
        # Debug logging antes de ejecutar query (sanitizado)
        debug_params = {
            'IndexName': params.get('IndexName'),
            'KeyConditionExpression': key_condition_expr,
            'ExpressionAttributeNames': expr_attr_names,
            'ExpressionAttributeValues_types': {
                k: type(v).__name__ for k, v in (expr_attr_values or {}).items()
            } if expr_attr_values else None
        }
        self.logger.debug(
            "[DDB:Query] Executing query with params: %s",
            debug_params
        )
        
        try:
            response = self.client.query(**params)
            self._log_capacity('Query', response.get('ConsumedCapacity'))
            
            items = response.get('Items', [])
            return [from_ddb(item) for item in items]
        except ClientError as e:
            correlation_id = self.correlation_id_provider()
            self.logger.error(
                f"[DDB:Query] Error: {e.response['Error']['Code']} "
                f"correlation_id={correlation_id} params={debug_params}"
            )
            raise
    
    def delete_conditional(self, key: dict, condition: str | None = None) -> None:
        """
        Elimina un item con condición opcional.
        
        Args:
            key: Clave del item (pk, sk) - será serializada
            condition: Expresión de condición opcional
        
        Raises:
            ClientError: Si la condición falla o hay error de DynamoDB
        """
        params = {
            'TableName': self.table_name,
            'Key': to_ddb(key),
            'ReturnConsumedCapacity': 'TOTAL'
        }
        
        if condition:
            params['ConditionExpression'] = condition
        
        try:
            response = self.client.delete_item(**params)
            self._log_capacity('DeleteItem', response.get('ConsumedCapacity'))
        except ClientError as e:
            self.logger.error("[DDB:DeleteItem] Error: %s", e.response['Error']['Code'])
            raise
