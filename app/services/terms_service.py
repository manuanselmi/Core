import logging
from datetime import datetime, timedelta

from app.utils.whatsapp_utils import (
    send_message,
    get_terminos_template_input,   # ← la helper que acabas de añadir
)
from app.models import db, Customer            # para persistir “solicitud enviada”

logger = logging.getLogger(__name__)

# Guardamos cuándo enviamos la última solicitud para no spamear
COOLDOWN = timedelta(hours=1)      # re-intenta tras 1 h si el user aún no respondió


def request_terms_acceptance(customer):
    """
    Envía la plantilla 'terminos_condiciones_v1' si el usuario aún NO aceptó
    los T&C y no se le ha solicitado en la última hora.

    * `customer` puede ser:
        • instancia SQLAlchemy (Customer)  –o–
        • dict devuelto por CustomerService.find_or_create
    Devuelve True si se envió, False en cualquier otro caso.
    """
    # ─────────────── Helpers para acceder indistintamente por atributo o clave
    def get(attr, default=None):
        return getattr(customer, attr, customer.get(attr, default))
    # -----------------------------------------------------------------------

    # 1) Ya aceptó → nada que hacer
    if get("accept_terms", False):
        logger.debug("✅ %s ya aceptó los T&C — no se envía plantilla", get("phone"))
        return False

    # 2) ¿Se la enviamos hace muy poco?
    last_sent_at = get("terms_requested_at")          # puede no existir aún
    if last_sent_at and datetime.utcnow() - last_sent_at < COOLDOWN:
        logger.debug("⏳ A %s se le pidió T&C hace <1 h — omitimos",
                     get("phone"))
        return False

    # 3) Componer y mandar el mensaje
    payload = get_terminos_template_input(
        recipient=get("phone"),
        nombre=get("name", "Amigo")
    )
    resp = send_message(payload)
    logger.info("📨 Plantilla T&C enviada a %s — resp=%s", get("phone"), resp)

    # 4) Anotar timestamp en DB (solo si recibimos instancia SQLAlchemy)
    if isinstance(customer, Customer):
        customer.terms_requested_at = datetime.utcnow()
        db.session.commit()

    return True

from app.models import db, Customer

def mark_accepted(phone: str) -> None:
    """
    Marca accept_terms = True para el número dado.
    """
    customer: Customer | None = Customer.query.filter_by(phone=phone).first()
    if not customer:
        return
    customer.accept_terms = True
    db.session.commit()
