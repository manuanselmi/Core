import os, json, functools
import boto3

_cache = {}
_sm = boto3.client("secretsmanager")

@functools.lru_cache(maxsize=None)
def _get(arn: str) -> dict | str:
    if not arn: return ""
    if arn in _cache: return _cache[arn]
    val = _sm.get_secret_value(SecretId=arn)
    s = val.get("SecretString") or ""
    _cache[arn] = s
    return s

def load_into_env():
    mapping = {
       "WHATSAPP_ACCESS_TOKEN": os.getenv("SM_ARN_WHATSAPP_ACCESS_TOKEN"),
       "VERIFY_TOKEN":          os.getenv("SM_ARN_WHATSAPP_VERIFY_TOKEN"),
       "OPENAI_API_KEY":        os.getenv("SM_ARN_OPENAI_API_KEY"),
       "GOOGLE_SA_JSON":        os.getenv("SM_ARN_GOOGLE_SA"),
       "SUPABASE_SERVICE_ROLE": os.getenv("SM_ARN_SUPABASE_SERVICE_ROLE"),
    }
    for env_key, arn in mapping.items():
        if arn and not os.getenv(env_key):
            os.environ[env_key] = _get(arn)
