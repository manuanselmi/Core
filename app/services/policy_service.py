# app/services/policy_service.py
from datetime import timedelta
from typing import Optional, Tuple

from app.models import db, TenantPolicy, Service

class PolicyDefaults:
    CANCEL_MIN_HOURS = 24
    MAX_DAYS_AHEAD = 60
    BUFFER_BEFORE = 0
    BUFFER_AFTER = 0
    NO_SHOW_GRACE = 10

def get_active_policy(tenant_id: int) -> TenantPolicy:
    """
    Devuelve la política del tenant o una 'virtual' con defaults si no existe aún.
    No hace commit. No crea filas por defecto.
    """
    pol = TenantPolicy.query.filter_by(tenant_id=tenant_id).first()
    if pol:
        return pol
    # build "virtual" object con defaults (no persistido)
    return TenantPolicy(
        tenant_id=tenant_id,
        cancel_min_hours=PolicyDefaults.CANCEL_MIN_HOURS,
        max_days_ahead=PolicyDefaults.MAX_DAYS_AHEAD,
        buffer_before=PolicyDefaults.BUFFER_BEFORE,
        buffer_after=PolicyDefaults.BUFFER_AFTER,
        no_show_grace=PolicyDefaults.NO_SHOW_GRACE,
    )

def effective_buffers_for_service(tenant_id: int, service: Service) -> Tuple[int, int]:
    """
    Combina buffers del tenant con los del servicio.
    Política: el buffer efectivo es la suma (tenant + service) para mantener conservador.
    """
    pol = get_active_policy(tenant_id)
    before = (pol.buffer_before or 0) + (service.buffer_before_min or 0)
    after  = (pol.buffer_after  or 0) + (service.buffer_after_min  or 0)
    return before, after

def horizon_days(tenant_id: int) -> int:
    return (get_active_policy(tenant_id).max_days_ahead or PolicyDefaults.MAX_DAYS_AHEAD)

def cancel_min_td(tenant_id: int):
    return timedelta(hours=get_active_policy(tenant_id).cancel_min_hours or PolicyDefaults.CANCEL_MIN_HOURS)
