# habit_reports_service.py
"""
PNG-only Habit Reports — estilo Kairo 2025

Cambios:
- Elimina por completo “Sugerencias: …”.
- Wordmark fallback "KAIRO." ahora oblicuo + negrita; “I” y “.” en celeste.
- Más aire entre KPIs y la tabla (no se pisa con “1 días consecutivos”).
- Consejo del coach sin encabezado, mismo estilo de párrafo discreto que la sugerencia anterior.
- Cita estoica (quote) centrada, un poco más grande, oblicua y entre “”.
- Wrap estricto del texto del consejo (corta antes, sin irse al margen derecho).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Tuple, Optional
from zoneinfo import ZoneInfo

from flask import current_app
from PIL import Image, ImageDraw, ImageFont, Image

from app.models import db, Habito, RegistroHabito, Customer

try:
    from .advice_service import AdviceService
except Exception:
    from advice_service import AdviceService  # type: ignore

# ─────────── Fechas

LOCAL_TZ = ZoneInfo("America/Montevideo")
_DOW_LABELS = ["L", "M", "X", "J", "V", "S", "D"]
WORDMARK_Y_OFFSET = 14 
HEADER_TEXT_OFFSET = 74


def _daterange(start: date, end: date) -> List[date]:
    days, cur = [], start
    while cur <= end:
        days.append(cur)
        cur += timedelta(days=1)
    return days

def _end_of_month(year: int, month: int) -> date:
    return date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)

def _last_completed_week(ref_day: date) -> tuple[date, date]:
    current_monday = ref_day - timedelta(days=ref_day.weekday())
    since = current_monday - timedelta(days=7)
    until = current_monday - timedelta(days=1)
    return since, until

# ─────────── Métricas

def compute_stats(customer_id: int, since: date, until: date) -> dict:
    habits: List[Habito] = (
        Habito.query.filter_by(customer_id=customer_id)
        .order_by(Habito.nombre.asc())
        .all()
    )
    habit_ids = [h.id for h in habits]

    regs: List[RegistroHabito] = (
        RegistroHabito.query
        .filter(RegistroHabito.habito_id.in_(habit_ids))
        .filter(RegistroHabito.fecha >= since, RegistroHabito.fecha <= until)
        .all()
    )
    reg_map: Dict[tuple[int, date], str] = {(r.habito_id, r.fecha): (r.estado or "pendiente") for r in regs}
    days = _daterange(since, until)

    heatmap: List[List[int]] = []
    per_habit: List[Dict[str, Any]] = []
    total_c = total_nc = total_p = 0

    for h in habits:
        row: List[int] = []
        c = nc = p = 0
        for d in days:
            st = reg_map.get((h.id, d))
            if st == "cumplido":
                row.append(1); c += 1; total_c += 1
            elif st == "no_cumplido":
                row.append(0); nc += 1; total_nc += 1
            else:
                row.append(-1); p += 1; total_p += 1
        total = c + nc
        pct = round(100.0 * c / total, 1) if total > 0 else 0.0
        per_habit.append({
            "id": h.id,
            "nombre": h.nombre,
            "cumplidos": c,
            "no_cumplidos": nc,
            "pendientes": p,
            "pct": pct,
            "total_dias": total,
        })
        heatmap.append(row)

    denom = total_c + total_nc
    pct_global = round(100.0 * total_c / denom, 1) if denom > 0 else 0.0

    threshold = float(current_app.config.get("HABIT_REPORT_STREAK_RATIO", 0.7))

    def _day_ratio(idx: int) -> float | None:
        ones = zeros = 0
        for r in range(len(habits)):
            v = heatmap[r][idx]
            if v == 1: ones += 1
            elif v == 0: zeros += 1
        if ones + zeros == 0:
            return None
        return ones / float(ones + zeros)

    racha = 0
    for i in range(len(days)-1, -1, -1):
        ratio = _day_ratio(i)
        if ratio is None:
            continue
        if ratio >= threshold:
            racha += 1
        else:
            break

    return {
        "customer_id": customer_id,
        "since": since,
        "until": until,
        "days": days,
        "habits": [{"id": h.id, "nombre": h.nombre} for h in habits],
        "heatmap": heatmap,
        "per_habit": per_habit,
        "totales": {"cumplidos": total_c, "no_cumplidos": total_nc, "pendientes": total_p},
        "porcentaje_cumplimiento": pct_global,
        "racha_actual": racha,
        "dow_labels": _DOW_LABELS,
    }

# ─────────── Render PNG

PNG_W, PNG_H = 1240, 1754
MARGIN_X, MARGIN_Y = 80, 80

COL_BG       = (255, 255, 255, 255)
COL_TEXT     = (17, 24, 39, 255)
COL_MUTED    = (107, 114, 128, 255)
COL_RULE     = (229, 231, 235, 255)
COL_PILL_BG  = (238, 242, 255, 255)
COL_OK       = (52, 195, 143, 255)
COL_FAIL     = (239, 68, 68, 255)
COL_NONE     = (243, 244, 246, 255)
COL_KAIRO_BLUE = (154, 187, 215, 255)

DAY_CELL     = 18
DAY_GAP      = 6
DAY_RADIUS   = 4
ROW_H        = 28
HEADER_H     = 120
KPIS_H       = 96  # +aire

COL_HABIT_W  = 320
COL_PCT_W    = 140
COL_DELTA_W  = 160

# Texto envuelto: acorta ancho para no pegarse al margen derecho
ADVICE_MAX_WIDTH_RATIO = 0.78  # 78% del ancho útil

def _load_font(size: int):
    path = (current_app.config.get("INTER_TTF_PATH") or "").strip()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size=size)
    except Exception:
        return ImageFont.load_default()

def _load_font_bolditalic(size: int):
    path = (current_app.config.get("INTER_TTF_BOLDITALIC_PATH") or "").strip()
    if path:
        try:
            return ImageFont.truetype(path, size=size)
        except Exception:
            pass
    # Fallback: usa regular y simulamos bold+italic con transform (abajo)
    return _load_font(size)

def _rounded_rect(draw: ImageDraw.ImageDraw, box, radius, fill, outline=None):
    try:
        draw.rounded_rectangle(box, radius=radius, fill=fill, outline=outline)
    except Exception:
        draw.rectangle(box, fill=fill, outline=outline)

def _pill(draw: ImageDraw.ImageDraw, xy, text, font, pad_x=10, pad_y=4, text_fill=COL_TEXT):
    l, t, r, b = draw.textbbox((0, 0), str(text), font=font)
    tx_w, tx_h = (r - l), (b - t)
    w = tx_w + pad_x * 2
    h = tx_h + pad_y * 2
    x, y = xy
    _rounded_rect(draw, (x, y, x + w, y + h), radius=h // 2, fill=COL_PILL_BG, outline=COL_RULE)
    draw.text((x + pad_x, y + pad_y), str(text), fill=text_fill, font=font)
    return w, h

def _load_logo():
    path = (current_app.config.get("KAIRO_LOGO_PATH") or "").strip()
    if not path:
        return None
    try:
        return Image.open(path).convert("RGBA")
    except Exception:
        return None

def _format_period_label(since: date, until: date) -> str:
    if since == until:
        return since.strftime("%d/%m/%Y")
    return f"{since.strftime('%d/%m/%Y')} – {until.strftime('%d/%m/%Y')}"

def _map_delta_by_habit(curr: dict, prev: dict | None) -> dict[int, float]:
    if not prev:
        return {h["id"]: 0.0 for h in curr["per_habit"]}
    prev_pct = {h["id"]: float(h.get("pct", 0.0)) for h in prev["per_habit"]}
    return {
        h["id"]: round(float(h.get("pct", 0.0)) - float(prev_pct.get(h["id"], 0.0)), 1)
        for h in curr["per_habit"]
    }

def _draw_text_oblique_bold(
    img: Image.Image,
    base: Image.Image,
    shear: float = 0.18,
) -> Image.Image:
    """
    Aplica una sola transformación oblicua (shear) a la capa base que ya contiene
    todo el texto con padding generoso. Evita recortes y mantiene kerning.
    """
    new_w = int(base.width + abs(shear) * base.height) + 8
    try:
        oblique = base.transform(
            (new_w, base.height),
            Image.AFFINE,
            (1, shear, 0, 0, 1, 0),
            resample=Image.BICUBIC,
            fillcolor=(0, 0, 0, 0),
        )
    except TypeError:
        oblique = base.transform(
            (new_w, base.height),
            Image.AFFINE,
            (1, shear, 0, 0, 1, 0),
            resample=Image.BICUBIC,
        )
    return oblique


def _draw_kairo_wordmark(
    dr: ImageDraw.ImageDraw,
    base_img: Image.Image,
    x: int,
    y: int,
    font_big: ImageFont.FreeTypeFont,
):
    """
    Wordmark fallback como UNA sola capa:
    1) Dibuja "KAIRO." completo en negro con padding amplio.
    2) Encima colorea 'I' y '.' en celeste, usando offsets precisos por ancho de substrings.
    3) Si NO tenés fuente italic real (INTER_TTF_BOLDITALIC_PATH), aplica shear una sola vez.
    """
    text = "KAIRO."
    pad = 32  # padding generoso para evitar cualquier corte

    # Capa base con el texto completo
    dummy = Image.new("RGBA", (2000, 500), (0, 0, 0, 0))
    dtmp = ImageDraw.Draw(dummy)
    l, t, r, b = dtmp.textbbox((0, 0), text, font=font_big)
    w, h = (r - l), (b - t)

    base = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)

    # Texto negro completo (esto da el kerning correcto)
    d.text((pad, pad), text, font=font_big, fill=COL_TEXT)

    # Recolorear 'I' y '.' superponiendo solo esos glifos en celeste
    # Calculamos offsets por ancho de substrings en la MISMA fuente.
    def _w(s: str) -> int:
        l2, t2, r2, b2 = dtmp.textbbox((0, 0), s, font=font_big)
        return r2 - l2

    x_I = pad + _w("KA")
    x_dot = pad + _w("KAIRO")
    d.text((x_I, pad), "I", font=font_big, fill=COL_KAIRO_BLUE)
    d.text((x_dot, pad), ".", font=font_big, fill=COL_KAIRO_BLUE)

    # Si tenés fuente italic real, no inclinamos; si no, shear suave
    italic_path = (current_app.config.get("INTER_TTF_BOLDITALIC_PATH") or "").strip()
    layer = base if italic_path else _draw_text_oblique_bold(base_img, base, shear=0.18)

    # Pegar en el lienzo principal
    base_img.paste(layer, (x, y), layer)


def _wrap_text(dr: ImageDraw.ImageDraw, text: str, font, max_width: int) -> List[str]:
    """Corta el texto por palabras para que no exceda max_width."""
    words = text.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        test = (cur + " " + w).strip()
        l, t, r, b = dr.textbbox((0, 0), test, font=font)
        if (r - l) <= max_width:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def _build_pages_for_png(
    stats: dict,
    customer_name: str,
    prev_stats: Optional[dict],
    advice_text: Optional[str],
    quote_text: Optional[str],
    pct_delta_global: float,
) -> List[Dict[str, Any]]:
    deltas = _map_delta_by_habit(stats, prev_stats)
    header_labels = [_DOW_LABELS[d.weekday()] for d in stats["days"]]

    rows: List[Dict[str, Any]] = []
    for idx, hb in enumerate(stats["habits"]):
        pct = stats["per_habit"][idx]["pct"]
        delta = deltas.get(hb["id"], 0.0)
        rows.append({
            "nombre": hb["nombre"],
            "pct": pct,
            "delta": delta,
            "cells": stats["heatmap"][idx],
        })

    usable_h = PNG_H - HEADER_H - KPIS_H - 320
    rows_per_page = max(10, min(28, usable_h // ROW_H))

    pages: List[Dict[str, Any]] = []
    if not rows:
        pages.append({
            "customer_name": customer_name,
            "pct": stats["porcentaje_cumplimiento"],
            "pct_delta_global": pct_delta_global,
            "racha": stats["racha_actual"],
            "header_labels": header_labels,
            "rows": [],
            "advice": advice_text or "",
            "quote": quote_text or "",
            "since": stats["since"],
            "until": stats["until"],
        })
        return pages

    for start in range(0, len(rows), rows_per_page):
        chunk = rows[start:start+rows_per_page]
        pages.append({
            "customer_name": customer_name,
            "pct": stats["porcentaje_cumplimiento"],
            "pct_delta_global": pct_delta_global,
            "racha": stats["racha_actual"],
            "header_labels": header_labels,
            "rows": chunk,
            "advice": advice_text or "",
            "quote": quote_text or "",
            "since": stats["since"],
            "until": stats["until"],
        })
    return pages

def _render_png(pages: List[dict]) -> bytes:
    f_brand_sm = _load_font(30)
    f_brand_big = _load_font_bolditalic(46)  # mayor tamaño base para wordmark
    f_title    = _load_font(20)
    f_name     = _load_font(18)
    f_kpi      = _load_font(44)
    f_kpi_sub  = _load_font(16)
    f_cell     = _load_font(12)
    f_row      = _load_font(14)
    f_small    = _load_font(12)
    f_par      = _load_font(13)  # párrafo consejo
    f_quote    = _load_font_bolditalic(16)  # cita oblicua un poco mayor

    logo = _load_logo()

    canvases = []
    now_str = datetime.now(LOCAL_TZ).strftime("%d/%m/%Y %H:%M")

    for page in pages:
        img = Image.new("RGBA", (PNG_W, PNG_H), COL_BG)
        dr  = ImageDraw.Draw(img)
        x0, y0 = MARGIN_X, MARGIN_Y

        # Header
        if logo:
            scale = 36 / max(1, logo.height)
            w = int(logo.width * scale); h = int(logo.height * scale)
            logo_resized = logo.resize((w, h))
            img.paste(logo_resized, (x0, y0), logo_resized)
            brand_x = x0 + w + 10
            dr.text((brand_x, y0), "KAIRO", font=f_brand_sm, fill=COL_TEXT)
        else:
            _draw_kairo_wordmark(dr, img, x0, y0 + WORDMARK_Y_OFFSET, f_brand_big)

        dr.text((x0, y0 + 40 + HEADER_TEXT_OFFSET), "Informe de Hábitos", font=f_title, fill=COL_MUTED)
        dr.text((x0, y0 + 66 + HEADER_TEXT_OFFSET), str(page.get("customer_name", "Usuario")), font=f_name, fill=COL_KAIRO_BLUE)

        period = _format_period_label(page["since"], page["until"])
        l, t, r, b = dr.textbbox((0, 0), period, font=f_small)
        dr.text((PNG_W - MARGIN_X - (r - l), y0), period, font=f_small, fill=COL_MUTED)

        # KPIs
        y = y0 + HEADER_H + HEADER_TEXT_OFFSET
        dr.text((x0, y), f"{page.get('pct', 0):.1f}% de cumplimiento", font=f_kpi, fill=COL_TEXT)
        y += 44
        dr.text((x0, y), f"vs semana anterior: {page.get('pct_delta_global', 0.0):+.1f}%", font=f_kpi_sub, fill=COL_MUTED)

        # Racha (con pluralización sutil)
        y += 22
        racha = int(page.get("racha", 0))
        racha_txt = "día" if racha == 1 else "días"
        dr.text((x0, y), f"{racha} {racha_txt} consecutivos", font=f_kpi_sub, fill=COL_MUTED)
        y += 22  # más aire que antes

        # Regla
        dr.line([(x0, y), (PNG_W - MARGIN_X, y)], fill=COL_RULE, width=1)
        y += 16  # empuja la tabla hacia abajo para que no se apriete

        # Encabezado tabla
        dr.text((x0, y), "Hábito", font=f_cell, fill=COL_TEXT)
        dr.text((x0 + COL_HABIT_W, y), "% Cumpl.", font=f_cell, fill=COL_TEXT)
        dr.text((x0 + COL_HABIT_W + COL_PCT_W, y), "Δ % (sem. ant.)", font=f_cell, fill=COL_TEXT)

        hx = x0 + COL_HABIT_W + COL_PCT_W + COL_DELTA_W
        for lab in page.get("header_labels", []):
            l, t, r, b = dr.textbbox((0, 0), str(lab), font=f_cell)
            dr.text((hx + (DAY_CELL - (r - l)) // 2, y), str(lab), font=f_cell, fill=COL_MUTED)
            hx += DAY_CELL + DAY_GAP
        y += 26

        # Filas
        for rr in page.get("rows", []):
            dr.text((x0, y + 4), str(rr["nombre"]), font=f_row, fill=COL_TEXT)
            _pill(dr, (x0 + COL_HABIT_W + 6, y - 2), f"{float(rr.get('pct', 0.0)):.1f}%", font=f_small)
            delta = float(rr.get("delta", 0.0))
            _pill(
                dr,
                (x0 + COL_HABIT_W + COL_PCT_W + 10, y - 2),
                f"{delta:+.1f}%",
                font=f_small,
                text_fill=(COL_OK if delta > 0 else (COL_FAIL if delta < 0 else COL_TEXT)),
            )
            hx = x0 + COL_HABIT_W + COL_PCT_W + COL_DELTA_W
            for cell in rr.get("cells", []):
                col = COL_NONE if cell == -1 else (COL_OK if cell == 1 else COL_FAIL)
                _rounded_rect(dr, (hx, y, hx + DAY_CELL, y + DAY_CELL), radius=DAY_RADIUS, fill=col, outline=None)
                hx += DAY_CELL + DAY_GAP
            y += ROW_H

        # Separador
        y += 8
        dr.line([(x0, y), (PNG_W - MARGIN_X, y)], fill=COL_RULE, width=1)
        y += 12

        # Consejo (solo el texto, sin título)
        advice = (page.get("advice") or "").strip()
        if advice:
            max_w = int((PNG_W - 2 * MARGIN_X) * ADVICE_MAX_WIDTH_RATIO)
            lines = _wrap_text(dr, advice, f_par, max_w)
            for ln in lines:
                dr.text((x0, y), ln, font=f_par, fill=COL_TEXT)
                y += 18
            y += 6

        # Cita estoica — centrada, oblicua
        quote = (page.get("quote") or "").strip()
        if quote:
            max_w = int((PNG_W - 2 * MARGIN_X) * 0.86)
            # si es muy larga, hacemos wrap también
            q_lines = _wrap_text(dr, quote, f_quote, max_w)
            for ln in q_lines:
                l, t, r, b = dr.textbbox((0, 0), ln, font=f_quote)
                w = r - l
                cx = (PNG_W - w) // 2
                dr.text((cx, y), ln, font=f_quote, fill=COL_TEXT)
                y += 20
            y += 4

        # Footer
        footer = f"Generado por Kairo • {now_str}"
        dr.text((x0, PNG_H - MARGIN_Y + 6), footer, font=f_small, fill=COL_MUTED)

        canvases.append(img)

    if len(canvases) == 1:
        out = canvases[0]
    else:
        total_h = sum(c.size[1] for c in canvases) + (len(canvases) - 1) * 20
        out = Image.new("RGBA", (PNG_W, total_h), COL_BG)
        y_cursor = 0
        for c in canvases:
            out.paste(c, (0, y_cursor))
            y_cursor += c.size[1] + 20

    from io import BytesIO
    buff = BytesIO()
    out.convert("RGB").save(buff, format="PNG", optimize=True)
    return buff.getvalue()

# ─────────── Público

def _get_customer_name(customer_id: int) -> str:
    c = Customer.query.get(customer_id)
    return (c.name or getattr(c, "nombre", None) or "Usuario") if c else "Usuario"

def generate_report_png(stats: dict, customer_name: str) -> bytes:
    pages = _build_pages_for_png(
        stats, customer_name=customer_name, prev_stats=None,
        advice_text=None, quote_text=None, pct_delta_global=0.0
    )
    return _render_png(pages)

def generate_weekly_report(customer_id: int, ref_day: date) -> Tuple[str, bytes, str]:
    since, until = _last_completed_week(ref_day)
    stats = compute_stats(customer_id=customer_id, since=since, until=until)

    prev_since = since - timedelta(days=7)
    prev_until = until - timedelta(days=7)
    prev_stats = compute_stats(customer_id=customer_id, since=prev_since, until=prev_until)

    curr_pct = float(stats.get("porcentaje_cumplimiento", 0.0))
    prev_pct = float(prev_stats.get("porcentaje_cumplimiento", 0.0)) if prev_stats else 0.0
    pct_delta_global = round(curr_pct - prev_pct, 1)

    customer_name = _get_customer_name(customer_id)

    # Consejo + Cita
    coach_text, quote_text = AdviceService.generate_weekly_advice(
        customer_id=customer_id,
        curr_stats=stats,
        prev_stats=prev_stats,
    )

    pages = _build_pages_for_png(
        stats,
        customer_name=customer_name,
        prev_stats=prev_stats,
        advice_text=coach_text,
        quote_text=quote_text,
        pct_delta_global=pct_delta_global,
    )
    png_bytes = _render_png(pages)

    fname_base = f"kairo_informe_semana_{since.strftime('%Y%m%d')}_{until.strftime('%Y%m%d')}"
    return f"{fname_base}.png", png_bytes, "image/png"

def generate_monthly_report(customer_id: int, year: int, month: int) -> Tuple[str, bytes, str]:
    since = date(year, month, 1)
    until = _end_of_month(year, month)
    stats = compute_stats(customer_id=customer_id, since=since, until=until)
    customer_name = _get_customer_name(customer_id)

    pages = _build_pages_for_png(
        stats,
        customer_name=customer_name,
        prev_stats=None,
        advice_text=None,
        quote_text=None,
        pct_delta_global=0.0,
    )
    png_bytes = _render_png(pages)

    fname_base = f"kairo_informe_mes_{year}{month:02d}"
    return f"{fname_base}.png", png_bytes, "image/png"
