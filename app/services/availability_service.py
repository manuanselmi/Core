from __future__ import annotations
from datetime import datetime, time, timedelta, date, timezone
from typing import Callable, Iterable, List, Optional, Tuple

from sqlalchemy import and_, or_

from app.models import (
    db,
    Tenant,
    Service,
    Staff,
    Resource,
    OpeningHours,
    ExceptionDate,
    Booking,
)
from .policy_service import effective_buffers_for_service, horizon_days

# Tipado de callback opcional para busy externo (Google, etc.)
BusyProvider = Callable[[int, Optional[int], Optional[int], datetime, datetime], List[Tuple[datetime, datetime]]]
# firma: (tenant_id, staff_id, resource_id, day_start_utc, day_end_utc) -> lista de (start_utc, end_utc)

ACTIVE_BOOKING_STATES = ("PENDING", "CONFIRMED", "RESCHEDULED")  # Excluimos CANCELED/NOSHOW/COMPLETED

def _ensure_aware_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _daterange_utc(day: date) -> Tuple[datetime, datetime]:
    start = datetime(day.year, day.month, day.day, 0, 0, tzinfo=timezone.utc)
    end   = start + timedelta(days=1)
    return start, end

def _opening_intervals_for_day(tenant_id: int, day: date) -> List[Tuple[time, time]]:
    """
    Devuelve los intervalos [start,end) en hora LOCAL DEL TENANT definidos en OpeningHours,
    pero nosotros operamos en UTC. La conversión de TZ se hará en servicios de presentación.
    En el CORE asumimos que la app nos llama ya con 'day' en la TZ del tenant y convertimos slots a UTC
    usando el supuesto de que las 00:00 del día corresponden al inicio del día en esa TZ.
    Para mantener el CORE simple y estable, devolvemos Time puro y el que llama decide la TZ.
    """
    weekday = day.weekday()  # 0=Mon ... 6=Sun
    rows = OpeningHours.query.filter_by(tenant_id=tenant_id, weekday=weekday, is_open=True).all()
    return [(r.start, r.end) for r in rows]

def _exception_for_day(tenant_id: int, day: date) -> Optional[ExceptionDate]:
    return ExceptionDate.query.filter_by(tenant_id=tenant_id, date=day).first()

def _clip_by_exceptions(base: List[Tuple[time, time]], exc: Optional[ExceptionDate]) -> List[Tuple[time, time]]:
    if not exc:
        return base
    if exc.is_closed:
        return []
    # Franja especial: ignoramos base y usamos [start,end] de la excepción
    if exc.start and exc.end:
        return [(exc.start, exc.end)]
    return base

def _to_dt_utc(day: date, tt: time, tenant_tz: str) -> datetime:
    """
    Convierte un time (en TZ del tenant) a datetime UTC del día dado.
    Para evitar dependencia de pytz/zoneinfo aquí, asumimos upstream la conversión.
    Si ya trabajás con TZ reales, reemplazaremos esto por util de TZ en la capa de presentación.
    Por ahora: tratamos la hora como si fuera UTC (conservador).
    """
    return datetime(day.year, day.month, day.day, tt.hour, tt.minute, tzinfo=timezone.utc)

def _collect_busy_utc(
    tenant_id: int,
    staff_id: Optional[int],
    resource_id: Optional[int],
    day_start_utc: datetime,
    day_end_utc: datetime,
    external_busy: Optional[BusyProvider],
) -> List[Tuple[datetime, datetime]]:
    busy: List[Tuple[datetime, datetime]] = []

    q = Booking.query.filter(
        Booking.tenant_id == tenant_id,
        Booking.starts_at < day_end_utc,
        Booking.ends_at   > day_start_utc,
        Booking.status.in_(ACTIVE_BOOKING_STATES),
        or_(
            staff_id is None,
            Booking.staff_id == staff_id
        ),
        or_(
            resource_id is None,
            Booking.resource_id == resource_id
        ),
    )
    for b in q.all():
        busy.append((b.starts_at, b.ends_at))

    if external_busy:
        busy += external_busy(tenant_id, staff_id, resource_id, day_start_utc, day_end_utc)

    # normalizar y ordenar
    busy = [(s.astimezone(timezone.utc), e.astimezone(timezone.utc)) for s, e in busy]
    busy.sort(key=lambda x: x[0])
    return busy

def _is_free(intervals: List[Tuple[datetime, datetime]], start: datetime, end: datetime) -> bool:
    for s, e in intervals:
        if start < e and end > s:
            return False
    return True

def compute_daily_slots(
    tenant_id: int,
    service_id: int,
    day: date,
    *,
    staff_id: Optional[int] = None,
    resource_id: Optional[int] = None,
    max_options: int = 5,
    step_minutes: int = 5,
    external_busy: Optional[BusyProvider] = None,
) -> List[dict]:
    """
    Calcula hasta max_options slots recomendados para 'day' (3–5 por defecto),
    respetando horarios de apertura/cierres, excepciones, buffers y ocupación
    por Booking y fuente externa (Google) si se provee 'external_busy'.
    Retorna slots con starts_at/ends_at en UTC.
    """
    service: Service = Service.query.get(service_id)
    if not service or not service.active:
        return []

    # Horizonte (no ofrecer mas allá)
    # Queda a criterio del caller rechazar días fuera del horizonte antes de llamar aquí.
    _ = horizon_days(tenant_id)

    tenant = Tenant.query.get(tenant_id)
    tenant_tz = tenant.timezone if tenant else "UTC"

    base = _opening_intervals_for_day(tenant_id, day)
    base = _clip_by_exceptions(base, _exception_for_day(tenant_id, day))
    if not base:
        return []

    before_buf, after_buf = effective_buffers_for_service(tenant_id, service)
    dur = timedelta(minutes=(service.duration_min or 0))
    total_span = timedelta(minutes=(service.duration_min or 0) + before_buf + after_buf)

    day_utc_start, day_utc_end = _daterange_utc(day)
    busy = _collect_busy_utc(
        tenant_id, staff_id, resource_id, day_utc_start, day_utc_end, external_busy
    )

    results: List[dict] = []
    step = timedelta(minutes=step_minutes)

    for start_t, end_t in base:
        # Convertimos a UTC (ver docstring: asumimos ya UTC conservador)
        w_start = _to_dt_utc(day, start_t, tenant_tz)
        w_end   = _to_dt_utc(day, end_t, tenant_tz)

        cursor = w_start
        while cursor + total_span <= w_end and len(results) < max_options:
            start = cursor + timedelta(minutes=before_buf)  # bloqueamos buffer antes
            end   = start + dur
            # La ocupación se chequea contra [cursor, cursor+total_span) para contemplar buffers
            if _is_free(busy, cursor, cursor + total_span):
                results.append({
                    "tenant_id": tenant_id,
                    "service_id": service_id,
                    "staff_id": staff_id,
                    "resource_id": resource_id,
                    "starts_at": start,
                    "ends_at": end,
                })
            cursor += step

        if len(results) >= max_options:
            break

    return results
