# app/config/secrets_loader.py
import os, json, re
import boto3
from botocore.config import Config

_ARN_RE = re.compile(r"^arn:aws:secretsmanager:[a-z0-9-]+:\d{12}:secret:[A-Za-z0-9/_+=.@-]+$")

_SECRET_ENV_MAP = {
    "SM_ARN_OPENAI_API_KEY": "OPENAI_API_KEY",
    "SM_ARN_WHATSAPP_ACCESS_TOKEN": "WHATSAPP_ACCESS_TOKEN",
    "SM_ARN_WHATSAPP_VERIFY_TOKEN": "WHATSAPP_VERIFY_TOKEN",
    "SM_ARN_DB_URL": "DATABASE_URL",
    "SM_ARN_SUPABASE_SERVICE_ROLE": "SUPABASE_SERVICE_ROLE",
    "SM_ARN_GOOGLE_SA": "GOOGLE_SA_JSON",
}

def _normalize_db_url():
    db_url = os.getenv("DATABASE_URL") or os.getenv("SQLALCHEMY_DATABASE_URI")
    if not db_url:
        return
    fixed = db_url.strip()
    if fixed.startswith("postgres://"):
        fixed = fixed.replace("postgres://", "postgresql://", 1)
    if fixed.startswith("postgresql://") and "+psycopg" not in fixed:
        fixed = fixed.replace("postgresql://", "postgresql+psycopg://", 1)
    os.environ["DATABASE_URL"] = fixed
    os.environ["SQLALCHEMY_DATABASE_URI"] = fixed

def _is_valid_arn(val: str) -> bool:
    return isinstance(val, str) and bool(_ARN_RE.match(val))

def load_into_env() -> None:
    # Cliente con timeouts conservadores (evita cuelgues)
    sm = boto3.client("secretsmanager", config=Config(connect_timeout=2, read_timeout=3, retries={"max_attempts": 2}))
    for arn_env, final_env in _SECRET_ENV_MAP.items():
        if final_env in os.environ and os.environ[final_env]:
            continue  # no pisar valores ya inyectados (útil en local)
        arn = os.environ.get(arn_env, "")
        if not _is_valid_arn(arn):
            continue
        resp = sm.get_secret_value(SecretId=arn)
        if "SecretString" in resp:
            secret = resp["SecretString"]
        else:
            # rara vez binario; lo pasamos a str
            import base64
            secret = base64.b64decode(resp["SecretBinary"]).decode("utf-8")
        # Para GOOGLE_SA_JSON guardá el JSON completo como SecretString (ver sección 5)
        os.environ[final_env] = secret
    _normalize_db_url()