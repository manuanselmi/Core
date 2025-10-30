"""
DynamoDB Client Configuration

Cliente boto3 configurado para Lambda runtime con timeouts y retries apropiados.
Valida variables de entorno requeridas al importar.
"""
import os
import time
from botocore.config import Config

# Validar variable de entorno requerida al importar
TABLE_NAME = os.getenv("DYNAMO_TABLE_NAME")
if not TABLE_NAME:
    raise RuntimeError(
        "Variable de entorno 'DYNAMO_TABLE_NAME' no configurada. "
        "Esta variable es obligatoria para el acceso a DynamoDB."
    )


def get_ddb_client():
    """
    Crea y retorna un cliente DynamoDB configurado para Lambda.
    
    Configuración:
    - Connect timeout: 2 segundos
    - Read timeout: 15 segundos
    - Retries: max 3 intentos en modo standard
    
    Returns:
        boto3 DynamoDB client (low-level)
    
    Note:
        boto3 está incluido en el runtime de Lambda, no se requiere en requirements.txt
    """
    import boto3  # Import local para evitar carga innecesaria en tests
    
    config = Config(
        connect_timeout=2,
        read_timeout=15,
        retries={
            'max_attempts': 3,
            'mode': 'standard'
        }
    )
    
    return boto3.client('dynamodb', config=config)


def now_ms() -> int:
    """
    Retorna timestamp actual en milisegundos (epoch UTC).
    
    Returns:
        Epoch en milisegundos como entero
    
    Example:
        >>> now_ms()
        1698765432000
    """
    return int(time.time() * 1000)
