from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional, Tuple

from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app.models import (
    db,
    Booking,
    Customer,
    Service,
    IdempotencyKey,
)
from .policy_service import get_active_policy, effective_buffers_for_service, cancel_min_td

ACTIVE_BOOKING_STATES = ("PENDING", "CONFIRMED", "RESCHEDULED")

class BookingError(Exception): ...
class PolicyViolation(BookingError): ...
class SlotTaken(BookingError): ...
class AlreadyProcessed(BookingError): ...

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _register_idempotency(scope: str, key: Optional[str]) -> None:
    if not key:
        return
    existing = IdempotencyKey.query.filter_by(key=key).first()
    if existing:
        raise AlreadyProcessed(f"idempotency key {key} already processed")
    db.session.add(IdempotencyKey(scope=scope, key=key))

def _overlaps(a_start, a_end, b_start, b_end) -> bool:
    return a_start < b_end and a_end > b_start

def _check_conflicts(
    tenant_id: int,
    staff_id: Optional[int],
    resource_id: Optional[int],
    starts_at: datetime,
    ends_at: datetime,
) -> None:
    """
    Verifica solapes sobre staff o recurso en estados activos.
    """
    q = Booking.query.filter(
        Booking.tenant_id == tenant_id,
        Booking.status.in_(ACTIVE_BOOKING_STATES),
        Booking.starts_at < ends_at,
        Booking.ends_at > starts_at,
        or_(staff_id is None, Booking.staff_id == staff_id),
        or_(resource_id is None, Booking.resource_id == resource_id),
    )
    # Nota: en una próxima iteración podemos añadir .with_for_update() condicionado a Postgres
    if q.first():
        raise SlotTaken("El horario ya está ocupado.")

def _compute_span(tenant_id: int, service: Service) -> Tuple[int, int, datetime]:
    before, after = effective_buffers_for_service(tenant_id, service)
    return before, after, None

def _apply_policy_horizon(tenant_id: int, starts_at: datetime, now_utc: datetime) -> None:
    pol = get_active_policy(tenant_id)
    max_days = pol.max_days_ahead or 60
    if starts_at > (now_utc + timedelta(days=max_days)):
        raise PolicyViolation(f"Fuera de horizonte: {max_days} días.")

def create_booking(
    *,
    tenant_id: int,
    customer_id: int,
    service_id: int,
    starts_at_utc: datetime,
    staff_id: Optional[int] = None,
    resource_id: Optional[int] = None,
    source: str = "whatsapp",
    note: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Booking:
    """
    Crea una reserva respetando políticas y anti-solape.
    No agenda recordatorios aquí (eso va en reminder_service).
    """
    now_utc = _ensure_utc(datetime.utcnow())
    starts_at = _ensure_utc(starts_at_utc)

    service = Service.query.get(service_id)
    if not service or not service.active:
        raise BookingError("Servicio no disponible.")

    _apply_policy_horizon(tenant_id, starts_at, now_utc)

    before, after, _ = _compute_span(tenant_id, service)
    duration = timedelta(minutes=service.duration_min or 0)
    total_before = timedelta(minutes=before)
    total_after  = timedelta(minutes=after)

    # La franja real ocupada por la reserva incluye buffers
    booked_start = starts_at - total_before
    booked_end   = starts_at + duration + total_after

    _register_idempotency("booking:create", idempotency_key)

    _check_conflicts(tenant_id, staff_id, resource_id, booked_start, booked_end)

    b = Booking(
        tenant_id=tenant_id,
        customer_id=customer_id,
        service_id=service_id,
        staff_id=staff_id,
        resource_id=resource_id,
        starts_at=starts_at,
        ends_at=starts_at + duration,
        status="CONFIRMED",
        source=source,
        note=note,
    )
    db.session.add(b)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        # Podría ser la unique por staff/resource slot
        raise SlotTaken("Conflicto de horario (constraint).")

    return b

def reschedule_booking(
    *,
    booking_id: int,
    new_starts_at_utc: datetime,
    idempotency_key: Optional[str] = None,
) -> Booking:
    now_utc = _ensure_utc(datetime.utcnow())
    new_starts = _ensure_utc(new_starts_at_utc)

    b: Booking = Booking.query.get(booking_id)
    if not b or b.status in ("CANCELED",):
        raise BookingError("Reserva inexistente o cancelada.")

    pol_cancel_td = cancel_min_td(b.tenant_id)
    # no permitimos reprogramar si falta menos que la ventana de cancelación mínima
    if (b.starts_at - now_utc) <= pol_cancel_td:
        raise PolicyViolation("Fuera de ventana para reprogramar.")

    service = Service.query.get(b.service_id)
    before, after, _ = _compute_span(b.tenant_id, service)
    duration = timedelta(minutes=service.duration_min or 0)
    total_before = timedelta(minutes=before)
    total_after  = timedelta(minutes=after)

    new_booked_start = new_starts - total_before
    new_booked_end   = new_starts + duration + total_after

    _register_idempotency("booking:reschedule", idempotency_key)

    _check_conflicts(b.tenant_id, b.staff_id, b.resource_id, new_booked_start, new_booked_end)

    b.starts_at = new_starts
    b.ends_at   = new_starts + duration
    b.status    = "RESCHEDULED"

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise SlotTaken("Conflicto de horario al reprogramar.")
    return b

def cancel_booking(
    *,
    booking_id: int,
    reason: Optional[str] = None,
    idempotency_key: Optional[str] = None,
) -> Booking:
    now_utc = _ensure_utc(datetime.utcnow())

    b: Booking = Booking.query.get(booking_id)
    if not b or b.status in ("CANCELED",):
        raise BookingError("Reserva inexistente o ya cancelada.")

    pol_cancel_td = cancel_min_td(b.tenant_id)
    if (b.starts_at - now_utc) <= pol_cancel_td:
        raise PolicyViolation("Fuera de ventana para cancelar.")

    _register_idempotency("booking:cancel", idempotency_key)

    b.status = "CANCELED"
    if reason:
        b.note = (b.note + "\n" if b.note else "") + f"[CANCEL] {reason}"

    db.session.commit()
    return b
