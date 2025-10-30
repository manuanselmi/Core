"""
DynamoDB Single-Table Key Generation Helpers

Generación consistente de PK/SK para el modelo single-table.
Todos los epochs son enteros (epoch_ms) para mantener orden lexicográfico.
"""


def pk_customer(phone: str) -> str:
    """
    Genera PK para Customer y todas sus entidades relacionadas.
    
    Args:
        phone: Número de teléfono en formato normalizado (E.164)
    
    Returns:
        PK en formato "CUST#<phone>"
    """
    return f"CUST#{phone}"


def sk_profile() -> str:
    """
    Genera SK para el perfil/profile del Customer.
    
    Returns:
        SK fijo "PROFILE"
    """
    return "PROFILE"


def sk_asstconv() -> str:
    """
    Genera SK para AssistantConversation del Customer.
    
    Returns:
        SK fijo "ASSTCONV"
    """
    return "ASSTCONV"


def sk_convo() -> str:
    """
    Genera SK para Conversation del Customer.
    
    Returns:
        SK fijo "CONVO"
    """
    return "CONVO"


def sk_turn(epoch_ms: int, wa_msg_id: str) -> str:
    """
    Genera SK para Turn (mensaje en conversación).
    
    Args:
        epoch_ms: Timestamp en milisegundos (epoch UTC)
        wa_msg_id: WhatsApp message ID para garantizar unicidad
    
    Returns:
        SK en formato "TURN#<epoch_ms>#<wa_msg_id>"
    
    Ejemplo:
        sk_turn(1698765432000, "wamid.123") -> "TURN#1698765432000#wamid.123"
    """
    return f"TURN#{epoch_ms}#{wa_msg_id}"


def sk_appt(starts_epoch: int, uuid: str) -> str:
    """
    Genera SK para Appointment.
    
    Args:
        starts_epoch: Timestamp de inicio en milisegundos (epoch UTC)
        uuid: UUID para garantizar unicidad
    
    Returns:
        SK en formato "APPT#<starts_epoch>#<uuid>"
    
    Ejemplo:
        sk_appt(1698765432000, "550e8400-e29b-41d4-a716-446655440000")
        -> "APPT#1698765432000#550e8400-e29b-41d4-a716-446655440000"
    """
    return f"APPT#{starts_epoch}#{uuid}"


def sk_rem(date_epoch: int, uuid: str) -> str:
    """
    Genera SK para Reminder.
    
    Args:
        date_epoch: Timestamp de ejecución en milisegundos (epoch UTC)
        uuid: UUID para garantizar unicidad
    
    Returns:
        SK en formato "REM#<date_epoch>#<uuid>"
    
    Ejemplo:
        sk_rem(1698765432000, "550e8400-e29b-41d4-a716-446655440000")
        -> "REM#1698765432000#550e8400-e29b-41d4-a716-446655440000"
    """
    return f"REM#{date_epoch}#{uuid}"


def sk_sm(send_epoch: int, uuid: str) -> str:
    """
    Genera SK para ScheduledMessage.
    
    Args:
        send_epoch: Timestamp de envío en milisegundos (epoch UTC)
        uuid: UUID para garantizar unicidad
    
    Returns:
        SK en formato "SM#<send_epoch>#<uuid>"
    
    Ejemplo:
        sk_sm(1698765432000, "550e8400-e29b-41d4-a716-446655440000")
        -> "SM#1698765432000#550e8400-e29b-41d4-a716-446655440000"
    """
    return f"SM#{send_epoch}#{uuid}"
