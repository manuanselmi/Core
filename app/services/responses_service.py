from typing import Optional, Dict, Any, List
import json
import logging
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
    try:
        ac = (AssistantConversation.query
              .filter_by(wa_phone=wa_norm, status="active")
              .order_by(AssistantConversation.created_at.desc())
              .first())
    except Exception:
        db.session.rollback()
        ac = None
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
        meta={"correlation_id": correlation_id} if correlation_id else None
    )
    db.session.add(ac)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Carrera: otra invocación creó la fila; devolvemos la existente
        try:
            ac = (AssistantConversation.query
                  .filter_by(wa_phone=wa_norm, status="active")
                  .order_by(AssistantConversation.created_at.desc())
                  .first())
        except Exception:
            db.session.rollback()
            ac = None
        return ac.conversation_id if ac else conv_id
    return conv_id

def set_last_response_id(wa_phone: str, response_id: str | None) -> None:
    """Persist last_response_id for a given WhatsApp phone."""
    wa_norm = normalize_phone_e164(wa_phone) or wa_phone
    try:
        ac = (AssistantConversation.query
              .filter_by(wa_phone=wa_norm, status="active")
              .order_by(AssistantConversation.created_at.desc())
              .first())
    except Exception:
        db.session.rollback()
        return
    if not ac:
        return
    ac.last_response_id = response_id
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()

def reset_conversation_for_phone(wa_phone: str, delete_remote: bool = True) -> str:
    """
    Create a fresh OpenAI conversation for the given phone, optionally deleting
    the previous conversation remotely, and update the local row.
    Returns the new conversation_id.
    """
    wa_norm = normalize_phone_e164(wa_phone) or wa_phone
    try:
        ac = (AssistantConversation.query
              .filter_by(wa_phone=wa_norm, status="active")
              .order_by(AssistantConversation.created_at.desc())
              .first())
    except Exception:
        db.session.rollback()
        ac = None
    old_id = getattr(ac, "conversation_id", None) if ac else None

    # Create new remote conversation first
    new_id = create_conversation()

    # Optionally delete old remote conversation (best-effort)
    if delete_remote and old_id:
        try:
            openai_client.conversations.delete(old_id)
        except Exception:
            # non-fatal
            pass

    # Upsert local row
    if not ac:
        ac = AssistantConversation(
            wa_phone=wa_norm,
            customer_id=None,
            conversation_id=new_id,
            status="active",
            last_wa_msg_id=None,
            meta=None,
            last_response_id=None,
        )
        db.session.add(ac)
    else:
        ac.conversation_id = new_id
        ac.last_response_id = None
    try:
        db.session.commit()
    except Exception:
        db.session.rollback()
    return new_id

def responses_create(
    *,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
    input_items: Optional[List[Dict[str, Any]]] = None,
    previous_response_id: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    conversation_id: Optional[str] = None,
    stream: bool = False,
    tool_choice: str = "auto",
    store: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    timeout_s: float = 17.0,
    parallel_tool_calls: Optional[bool] = None,
):
    """
    Wrapper for OpenAI Responses API (initial and continuation requests).
    
    - Initial turn: set model/instructions/input/tools/conversation
    - Continuation: set previous_response_id and input to function_call_output items
    """
    logger = logging.getLogger("responses_service")
    
    kwargs: Dict[str, Any] = {}
    if model:
        kwargs["model"] = model
    if instructions:
        kwargs["instructions"] = instructions
    if tools is not None:
        kwargs["tools"] = tools
    if tool_choice:
        kwargs["tool_choice"] = tool_choice
    if stream is not None:
        kwargs["stream"] = stream
    if store is not None:
        kwargs["store"] = store
    if metadata is not None:
        kwargs["metadata"] = metadata
    if input_items is not None:
        kwargs["input"] = input_items
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id
    if conversation_id:
        kwargs["conversation"] = conversation_id
    if parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = parallel_tool_calls

    # Prefer passing idempotency via header for wider SDK compatibility
    if idempotency_key:
        headers = dict(kwargs.get("extra_headers") or {})
        headers["Idempotency-Key"] = idempotency_key
        kwargs["extra_headers"] = headers

    # Log request details (truncated for security)
    log_kwargs = dict(kwargs)
    if "input" in log_kwargs:
        log_kwargs["input"] = f"[{len(log_kwargs['input'])} items]"
    logger.info(
        "[RESP] create: conv=%s prev_id=%s model=%s parallel=%s idem=%s",
        kwargs.get("conversation"),
        kwargs.get("previous_response_id"),
        kwargs.get("model"),
        kwargs.get("parallel_tool_calls"),
        idempotency_key,
    )

    client_with_timeout = openai_client.with_options(timeout=timeout_s)
    
    try:
        response = client_with_timeout.responses.create(**kwargs)
        # Log response metadata
        logger.info("[RESP] ok: id=%s status=%s", 
                    getattr(response, "id", None), 
                    getattr(response, "status", None))
        return response
    except Exception as e:
        # Enhanced error logging
        logger.error("[RESP] error: %s", str(e))
        if hasattr(e, 'response'):
            try:
                logger.error("[RESP] status: %s", e.response.status_code)
                logger.error("[RESP] x-request-id: %s", 
                           dict(e.response.headers).get("x-request-id", "N/A"))
                logger.error("[RESP] body: %s", e.response.text[:1000])
            except Exception:
                pass
        raise
    
def build_function_call_output(call_id: str, output_json_string: str) -> Dict[str, Any]:
    """Construct a function_call_output item for Responses API continuation."""
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output_json_string,
    }


def continue_with_function_outputs(
    *,
    model: str,
    previous_response_id: Optional[str],
    outputs: List[Dict[str, Any]],
    idempotency_key: Optional[str] = None,
    tools: Optional[List[Dict[str, Any]]] = None,
    disable_parallel: bool = True,
    timeout_s: float = 17.0,
    metadata: Optional[Dict[str, Any]] = None,
):
    """
    Chain a Responses conversation sending one or more function_call_output items.
    """
    logger = logging.getLogger("responses_service")

    # Log outputs preview
    previews = [
        {"call_id": o.get("call_id"), "output_preview": (o.get("output") or "")[:500]}
        for o in (outputs or [])
    ]
    logger.info(
        "[CONT] start: prev_id=%s model=%s outputs=%d parallel=%s idem=%s",
        previous_response_id, model, len(outputs or []), disable_parallel, idempotency_key,
    )
    logger.debug("[CONT] outputs_preview=%s", json.dumps(previews, ensure_ascii=False))

    client_with_timeout = openai_client.with_options(timeout=timeout_s)

    # Pre-call validation: outputs must be non-empty and each must have call_id and non-empty string output
    call_ids = []
    for o in outputs or []:
        cid = o.get("call_id")
        out = o.get("output")
        if not cid or not isinstance(out, str) or out == "":
            logger.error("[CONT] invalid function_call_output: call_id=%s has_valid_output=%s", cid, isinstance(out, str) and out != "")
            return None
        call_ids.append(cid)

    # Build kwargs for create call
    kwargs: Dict[str, Any] = {
        "model": model,
        "input": outputs,
    }
    # Continuations must use previous_response_id only; never include conversation
    if previous_response_id:
        kwargs["previous_response_id"] = previous_response_id

    if tools is not None:
        kwargs["tools"] = tools
    if disable_parallel:
        kwargs["parallel_tool_calls"] = False
    if metadata is not None:
        kwargs["metadata"] = metadata

    if idempotency_key:
        headers = dict(kwargs.get("extra_headers") or {})
        headers["Idempotency-Key"] = idempotency_key
        kwargs["extra_headers"] = headers

    try:
        resp = client_with_timeout.responses.create(**kwargs)
        logger.info(
            "[CONT] ok: prev_id=%s next_id=%s status=%s",
            previous_response_id,
            getattr(resp, "id", None), getattr(resp, "status", None),
        )
        return resp
    except Exception as e:
        status = None
        body = None
        req_id = None
        resp_obj = getattr(e, "response", None)
        if resp_obj is not None:
            status = getattr(resp_obj, "status_code", None)
            try:
                body = getattr(resp_obj, "text", None)
                hdrs = getattr(resp_obj, "headers", None)
                if isinstance(hdrs, dict):
                    req_id = hdrs.get("x-request-id")
            except Exception:
                pass
        try:
            outputs_preview = [
                {"call_id": o.get("call_id"), "output_preview": (o.get("output") or "")[:300]}
                for o in (outputs or [])
            ]
        except Exception:
            outputs_preview = []
        logger.error(
            "[CONT] error: %s status=%s x-request-id=%s prev_id=%s call_ids=%s outputs_preview=%s body=%s",
            f"{type(e).__name__}: {str(e)}", status, req_id, previous_response_id, call_ids, outputs_preview, (body or "")[:1000],
        )
        raise
    