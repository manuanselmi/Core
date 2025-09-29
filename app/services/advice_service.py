# advice_service.py
from __future__ import annotations

from typing import Dict, List, Tuple, Optional
from flask import current_app
from openai import OpenAI
from datetime import date

from app.models import Customer

GPT_MODEL_DEFAULT = "gpt-4o-mini"


def _pct(x: float) -> str:
    try:
        return f"{float(x):.1f}%"
    except Exception:
        return "0.0%"


def _customer_name(customer_id: int) -> str:
    c = Customer.query.get(customer_id)
    return (c.name or getattr(c, "nombre", None) or "Usuario") if c else "Usuario"


def _habits_table(curr_stats: Dict, prev_stats: Optional[Dict]) -> List[Dict]:
    """Lista de hábitos con pct actual y delta vs semana anterior (si hubiera)."""
    prev_map = {}
    if prev_stats:
        prev_map = {h["id"]: float(h.get("pct", 0.0)) for h in prev_stats.get("per_habit", [])}

    rows = []
    for h in curr_stats.get("per_habit", []):
        hid = h["id"]
        pct = float(h.get("pct", 0.0))
        delta = round(pct - float(prev_map.get(hid, 0.0)), 1)
        rows.append({"nombre": h["nombre"], "pct": pct, "delta": delta})
    return rows


class AdviceService:
    """
    Genera:
      - coach_text: dos líneas con reacción + consejo (personalizado, rioplatense, estoico).
      - quote_text: una cita corta estilo estoico (entre “”), adaptada a semana buena/neutral/mala.
    """

    @staticmethod
    def _client() -> OpenAI:
        return OpenAI()

    @staticmethod
    def generate_weekly_advice(
        customer_id: int,
        curr_stats: Dict,
        prev_stats: Optional[Dict],
    ) -> Tuple[str, str]:
        """
        Retorna (coach_text, quote_text)
        coach_text: 1–2 líneas, menciona 1–2 hábitos por nombre si corresponde.
        quote_text: 1 línea entre comillas, tono estoico; si fue mala semana, más contenedor;
                    si fue buena, más reforzador.
        """
        try:
            client = AdviceService._client()
            model = current_app.config.get("OPENAI_GPT_ADVICE_MODEL", GPT_MODEL_DEFAULT)

            name = _customer_name(customer_id)
            curr_pct = float(curr_stats.get("porcentaje_cumplimiento", 0.0))
            prev_pct = float(prev_stats.get("porcentaje_cumplimiento", 0.0)) if prev_stats else 0.0
            delta = round(curr_pct - prev_pct, 1)
            since, until = curr_stats.get("since"), curr_stats.get("until")
            period = ""
            if isinstance(since, date) and isinstance(until, date):
                period = f"{since.strftime('%d/%m/%Y')}–{until.strftime('%d/%m/%Y')}"

            habits_rows = _habits_table(curr_stats, prev_stats)

            # Clasificación simple para matiz de la cita:
            if curr_pct >= 70 or delta >= 5:
                week_kind = "buena"
            elif curr_pct < 40 or delta <= -5:
                week_kind = "mala"
            else:
                week_kind = "neutral"

            system = (
                "Sos un coach estoico rioplatense: directo, cálido, sin humo. "
                "Usá español rioplatense (podés decir 'bo'), sin exagerar. "
                "Devolvés dos piezas:\n"
                "1) COACH: dos líneas. Línea 1 = reacción breve según desempeño. "
                "Línea 2 = consejo accionable, concreto (menciona 1–2 hábitos por nombre si aporta, pensa en como le fue al usuario con sus habitos, y pensando particularmente en que habito le fue mal o bien, dale el consejo o la felicitacion.). "
                "Máx. 220 caracteres en total.\n"
                "2) QUOTE: una línea entre comillas, estilo estoico (original o paráfrasis breve de ideas estoicas), "
                "adaptada si la semana fue buena, mala o neutral. Sin atribución, solo la frase."
            )

            # Compactamos los hábitos para que el modelo pueda personalizar:
            # form: "Habito | pct | delta"
            habits_brief = "; ".join(f"{r['nombre']}|{r['pct']:.1f}%|{r['delta']:+.1f}%"
                                     for r in habits_rows[:8])

            user = f"""
Nombre: {name}
Período: {period}
Cumplimiento semanal: {_pct(curr_pct)}
Δ vs semana anterior: {delta:+.1f}%
Racha actual (días): {curr_stats.get('racha_actual', 0)}
Hábitos (nombre|pct|Δ): {habits_brief}
Tipo de semana: {week_kind}

Formateá tu respuesta EXACTAMENTE así:
COACH: <dos líneas en un solo párrafo, sin encabezados, sin viñetas, menciona 1–2 hábitos si suma>
QUOTE: “<una sola línea entre comillas, estilo estoico motivador>”
"""

            rsp = client.chat.completions.create(
                model=model,
                messages=[{"role": "system", "content": system},
                          {"role": "user", "content": user}],
                temperature=0.6,
                max_tokens=220,
            )
            raw = (rsp.choices[0].message.content or "").strip()

            coach_text = ""
            quote_text = ""
            for line in raw.splitlines():
                line = line.strip()
                if line.upper().startswith("COACH:"):
                    coach_text = line[len("COACH:"):].strip()
                elif line.upper().startswith("QUOTE:"):
                    quote_text = line[len("QUOTE:"):].strip()

            # Fallbacks seguros
            if not coach_text:
                coach_text = (
                    "Bo, cada paso cuenta: bien ahí sosteniendo lo que pudiste. "
                    "Mañana arrancá simple: elegí 1 hábito clave (p. ej., Agradecer) y cumplilo a la misma hora."
                )
            if not quote_text:
                quote_text = "“El control está en tus actos, no en el resultado; hacé hoy lo que depende de vos.”"

            return coach_text, quote_text

        except Exception as e:
            current_app.logger.exception("[AdviceService] Error generando consejo: %s", e)
            return (
                "Bo, sin drama: aprendé de esta semana y volvé a lo básico. "
                "Elegí 1–2 hábitos fijos y cumplilos a la misma hora, todos los días.",
                "“La constancia vence donde la intensidad se rinde.”",
            )
