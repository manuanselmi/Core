from typing import Optional, Dict, Any, List
import json
import logging
import time

from app.services.openai_client import client as openai_client

logger = logging.getLogger("responses_service")


def create_first_response(
    *,
    model: str,
    instructions: str,
    input_items: List[Dict[str, Any]],
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: str = "auto",
    store: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    timeout_s: float = 17.0,
    parallel_tool_calls: Optional[bool] = None,
):
    kwargs: Dict[str, Any] = {
        "model": model,
        "instructions": instructions,
        "input": input_items,
        "tool_choice": tool_choice,
        "store": store,
    }
    
    if tools is not None:
        kwargs["tools"] = tools
    if metadata is not None:
        kwargs["metadata"] = metadata
    if parallel_tool_calls is not None:
        kwargs["parallel_tool_calls"] = parallel_tool_calls
    
    if idempotency_key:
        headers = {}
        headers["Idempotency-Key"] = idempotency_key
        kwargs["extra_headers"] = headers

    log_kwargs = dict(kwargs)
    if "input" in log_kwargs:
        log_kwargs["input"] = f"[{len(log_kwargs['input'])} items]"
    
    logger.info(
        "[RESP] create_first_response: model=%s parallel=%s idem=%s",
        model,
        parallel_tool_calls,
        idempotency_key,
    )

    client_with_timeout = openai_client.with_options(timeout=timeout_s)
    
    start_time = time.time()
    try:
        response = client_with_timeout.responses.create(**kwargs)
        elapsed = time.time() - start_time
        
        logger.info(
            "[RESP] ok: id=%s status=%s elapsed=%.2fs", 
            getattr(response, "id", None), 
            getattr(response, "status", None),
            elapsed
        )
        return response
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("[RESP] error after %.2fs: %s", elapsed, str(e))
        if hasattr(e, 'response'):
            try:
                logger.error("[RESP] status: %s", e.response.status_code)
                logger.error("[RESP] x-request-id: %s", 
                           dict(e.response.headers).get("x-request-id", "N/A"))
                logger.error("[RESP] body: %s", e.response.text[:1000])
            except Exception:
                pass
        raise


def continue_with_tool_output(
    *,
    model: str,
    previous_response_id: str,
    input_items: List[Dict[str, Any]],
    store: bool = True,
    metadata: Optional[Dict[str, Any]] = None,
    idempotency_key: Optional[str] = None,
    timeout_s: float = 17.0,
):
    """
    Continue a turn by providing function_call_output using previous_response_id.
    
    CRITICAL: Continuations MUST include 'model' parameter.
    CRITICAL: Do NOT include 'tools', 'tool_choice', 'instructions', 'parallel_tool_calls' in continuations.
    
    Args:
        model: OpenAI model name (REQUIRED even in continuations)
        previous_response_id: The response.id from the first response in this turn
        input_items: List containing function_call_output items
        store: Whether to store the response
        metadata: Additional metadata
        idempotency_key: Key for idempotent requests
        timeout_s: Request timeout in seconds
        
    Returns:
        Response object from OpenAI Responses API
    """
    kwargs: Dict[str, Any] = {
        "model": model,  # REQUIRED
        "previous_response_id": previous_response_id,
        "input": input_items,
        "store": store,
    }
    
    if metadata is not None:
        kwargs["metadata"] = metadata
    
    if idempotency_key:
        headers = {}
        headers["Idempotency-Key"] = idempotency_key
        kwargs["extra_headers"] = headers

    log_kwargs = dict(kwargs)
    if "input" in log_kwargs:
        log_kwargs["input"] = f"[{len(log_kwargs['input'])} items]"
    
    logger.info(
        "[RESP] continue_with_tool_output: model=%s prev_id=%s items=%d idem=%s",
        model,
        previous_response_id,
        len(input_items),
        idempotency_key,
    )

    client_with_timeout = openai_client.with_options(timeout=timeout_s)
    
    start_time = time.time()
    try:
        response = client_with_timeout.responses.create(**kwargs)
        elapsed = time.time() - start_time
        
        logger.info(
            "[RESP] ok: id=%s status=%s elapsed=%.2fs", 
            getattr(response, "id", None), 
            getattr(response, "status", None),
            elapsed
        )
        return response
    except Exception as e:
        elapsed = time.time() - start_time
        logger.error("[RESP] continuation error after %.2fs: %s", elapsed, str(e))
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
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output_json_string,
        "status": "completed"
    }
