from importlib import metadata
from typing import Optional, Dict, Any, List
from sqlalchemy.exc import IntegrityError

from app.models import db, AssistantConversation
from app.utils.phone_utils import normalize_phone_e164
from app.services.openai_client import client as openai_client

def create_conversation() -> str:
    conv = openai_client.conversations.create()
    return conv.id

def get_or_create_conversation_id(
    wa_phone: str,
    customer_id: Optional[int] = None,
    correlation_id: Optional[str] = None,
    last_wa_msg_id: Optional[str] = None
) -> str:
    wa_norm = normalize_phone_e164(wa_phone) or wa_phone
    ac = (AssistantConversation.query
          .filter_by(wa_phone=wa_norm, status="active")
          .order_by(AssistantConversation.created_at.desc())
          .first())
    if ac and ac.conversation_id:
        if last_wa_msg_id and ac.last_wa_msg_id != last_wa_msg_id:
            ac.last_wa_msg_id = last_wa_msg_id
            db.session.commit()
        return ac.conversation_id

    conv_id = create_conversation()
    ac = AssistantConversation(
        wa_phone=wa_norm,
        customer_id=customer_id,
        conversation_id=conv_id,
        status="active",
        last_wa_msg_id=last_wa_msg_id,
        metadata={"correlation_id": correlation_id} if correlation_id else None
    )
    db.session.add(ac)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Carrera: otra invocación creó la fila; devolvemos la existente
        ac = (AssistantConversation.query
              .filter_by(wa_phone=wa_norm, status="active")
              .order_by(AssistantConversation.created_at.desc())
              .first())
        return ac.conversation_id if ac else conv_id
    return conv_id

def responses_create(
    *,
    model: str,
    instructions: str,
    input_items: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    previous_response_id: Optional[str] = None,
    stream: bool = False,
    tool_choice: str = "auto",
    store: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    # strict: bool = True,
    timeout_s: float = 17.0,
):
    kwargs = dict(
        model=model,
        instructions=instructions,
        input=input_items,
        tools=tools or [],
        tool_choice=tool_choice,
        stream=stream,
        store=store,
        metadata=metadata or {},
        #strict=strict,
    )
    if conversation_id:
        kwargs["conversation"] = conversation_id
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    client_with_timeout = openai_client.with_options(timeout=timeout_s)
    return client_with_timeout.responses.create(**kwargs)
