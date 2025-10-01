from __future__ import annotations
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from sqlalchemy.exc import IntegrityError

from app.models import db, Booking, BookingReminder

def _ensure_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)

def _mk(booking: Booking, kind: str, send_at: datetime) -> BookingReminder:
    return BookingReminder(booking_id=booking.id, kind=kind, send_at=send_at)

def plan_standard_reminders(booking: Booking, *, t24: bool = True, t3: bool = True) -> List[BookingReminder]:
    """
    Crea (si no existen) los recordatorios T-24 y T-3 ligados a la Booking.
    No envía mensajes ni encola jobs aquí.
    """
    created: List[BookingReminder] = []
    starts = _ensure_utc(booking.starts_at)

    if t24:
        send_at = starts - timedelta(hours=24)
        r = BookingReminder.query.filter_by(booking_id=booking.id, kind="T24").first()
        if not r:
            r = _mk(booking, "T24", send_at)
            db.session.add(r)
            created.append(r)

    if t3:
        send_at = starts - timedelta(hours=3)
        r = BookingReminder.query.filter_by(booking_id=booking.id, kind="T3").first()
        if not r:
            r = _mk(booking, "T3", send_at)
            db.session.add(r)
            created.append(r)

    if created:
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            # otra transacción pudo crear alguno: salir silencioso
    return created

def resync_after_reschedule(booking: Booking) -> None:
    """
    Si la reserva cambia de horario, recalculamos send_at de T24/T3 (si siguen pendientes).
    """
    starts = _ensure_utc(booking.starts_at)
    t24 = BookingReminder.query.filter_by(booking_id=booking.id, kind="T24").first()
    if t24 and not t24.sent_at:
        t24.send_at = starts - timedelta(hours=24)

    t3 = BookingReminder.query.filter_by(booking_id=booking.id, kind="T3").first()
    if t3 and not t3.sent_at:
        t3.send_at = starts - timedelta(hours=3)

    db.session.commit()

def cancel_pending_for_booking(booking_id: int) -> int:
    """
    Elimina recordatorios pendientes (no enviados) de una booking cancelada.
    Devuelve la cantidad de filas afectadas.
    """
    q = BookingReminder.query.filter_by(booking_id=booking_id, status="pending")
    count = q.count()
    q.delete()
    db.session.commit()
    return count
