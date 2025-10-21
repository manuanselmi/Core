from pathlib import Path
from functools import lru_cache

@lru_cache(maxsize=1)
def load_kairito() -> str:
    p = Path(__file__).with_name("kairito.md")
    return p.read_text(encoding="utf-8")
