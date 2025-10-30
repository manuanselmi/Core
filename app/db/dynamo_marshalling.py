"""
DynamoDB Marshalling Utilities

Serialización/deserialización entre objetos Python y formato DynamoDB,
evitando problemas con tipo Decimal y simplificando el manejo de datos.
"""
from decimal import Decimal


def to_ddb(item: dict) -> dict:
    """
    Serializa un diccionario Python a formato DynamoDB.
    
    Args:
        item: Diccionario con valores Python nativos
    
    Returns:
        Diccionario en formato DynamoDB (con type descriptors: {'S': ...}, {'N': ...}, etc.)
    
    Example:
        >>> to_ddb({'name': 'John', 'age': 30})
        {'name': {'S': 'John'}, 'age': {'N': '30'}}
    """
    from boto3.dynamodb.types import TypeSerializer
    
    serializer = TypeSerializer()
    return {k: serializer.serialize(v) for k, v in item.items()}


def from_ddb(item: dict) -> dict:
    """
    Deserializa un item DynamoDB a diccionario Python.
    
    Convierte automáticamente:
    - Decimal sin parte decimal → int
    - Decimal con parte decimal → float
    
    Args:
        item: Item en formato DynamoDB
    
    Returns:
        Diccionario con valores Python nativos (int, float, str, list, dict)
    
    Example:
        >>> from_ddb({'name': {'S': 'John'}, 'age': {'N': '30'}})
        {'name': 'John', 'age': 30}
    """
    from boto3.dynamodb.types import TypeDeserializer
    
    deserializer = TypeDeserializer()
    result = {k: deserializer.deserialize(v) for k, v in item.items()}
    
    # Convertir Decimals a int/float para evitar problemas downstream
    return _convert_decimals(result)


def _convert_decimals(obj):
    """
    Convierte recursivamente objetos Decimal a int/float.
    
    Args:
        obj: Objeto Python (puede ser dict, list, Decimal, u otro tipo)
    
    Returns:
        Objeto con todos los Decimals convertidos a int/float
    """
    if isinstance(obj, list):
        return [_convert_decimals(item) for item in obj]
    elif isinstance(obj, dict):
        return {k: _convert_decimals(v) for k, v in obj.items()}
    elif isinstance(obj, Decimal):
        # Si es entero, devolver int; si tiene decimales, devolver float
        if obj % 1 == 0:
            return int(obj)
        else:
            return float(obj)
    else:
        return obj
