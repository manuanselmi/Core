import os, json
from aws import handler

def test_get_verify_ok(monkeypatch):
    monkeypatch.setenv("VERIFY_TOKEN", "abc")
    event = {
        "requestContext": {"http": {"method": "GET", "path": "/webhook"}},
        "queryStringParameters": {"hub.verify_token":"abc","hub.challenge":"12345"}
    }
    r = handler.lambda_handler(event, None)
    assert r["statusCode"] == 200 and r["body"] == "12345"

def test_post_basic(monkeypatch):
    # evita pegarle a WhatsApp
    from app.utils import whatsapp_utils
    monkeypatch.setattr(whatsapp_utils, "send_message", lambda payload: {"messages":[{"id":"X"}]})

    body = {
      "entry":[{"changes":[{"value":{
        "metadata":{"phone_number_id":"111"},
        "contacts":[{"wa_id":"59891111111","profile":{"name":"Manu"}}],
        "messages":[{"id":"wamid.1","timestamp":"1730569200","type":"text","text":{"body":"hola"}}]
      }}]}]
    }
    event = {
        "requestContext": {"http": {"method": "POST", "path": "/webhook"}},
        "body": json.dumps(body)
    }
    r = handler.lambda_handler(event, None)
    assert r["statusCode"] == 200
