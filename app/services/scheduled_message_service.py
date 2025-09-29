from datetime import datetime
from app.models import db, ScheduledMessage
from app.utils.whatsapp_utils import send_comida_template
from flask import current_app


class ScheduledMessageService:
    @staticmethod
    def create(customer_id: int, target_phone: str, text: str, send_at: datetime) -> int:
        sm = ScheduledMessage(
            customer_id=customer_id,
            target_phone=target_phone,
            text=text,
            send_at=send_at,
        )
        db.session.add(sm)
        db.session.commit()
        return sm.id

    @staticmethod
    def run(sm_id: int):
        """
        Job lanzado por APScheduler.  Se asegura de abrir un `app_context`
        antes de tocar la BD o usar utilidades que dependen de Flask.
        """
        # ─── Garantizar contexto ────────────────────────────────────
        from flask import current_app
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            # No hay contexto activo → creamos uno nuevo
            from app import create_app
            app = create_app()
        app.logger.info(f"[ScheduledMessageService] Ejecutando mensaje programado {sm_id}")
        
        with app.app_context():
            sm = ScheduledMessage.query.get(sm_id)
            if not sm or sm.status != "pending":
                return
            try:
                app.logger.info(f"[ScheduledMessageService] Enviando mensaje a {sm.target_phone}")
                send_comida_template(  
                    sm.target_phone,
                    sm.customer.name,  # Access the related customer object
                    sm.customer.phone,  # Access the related customer object
                    sm.text,
                )
                sm.mark_sent()
                db.session.delete(sm)
                db.session.commit()
            except Exception:
                sm.mark_error()
                raise
            
    @staticmethod
    def reschedule_all_messages(scheduler):
        """
        Reprograma todos los mensajes pendientes al arrancar la aplicación.
        """
        from flask import current_app
        try:
            app = current_app._get_current_object()
        except RuntimeError:
            # No hay contexto activo → creamos uno nuevo
            from app import create_app
            app = create_app()
            
        with app.app_context():
            messages = ScheduledMessage.query.filter_by(status="pending").all()
            app.logger.info(
                f"[ScheduledMessageService] Reprogramando {len(messages)} mensajes pendientes."
            )
            for sm in messages:
                scheduler.add_job(
                    ScheduledMessageService.run,
                    trigger="date",
                    run_date=sm.send_at,
                    args=[sm.id],
                    id=f"scheduled_message_{sm.id}",
                    replace_existing=True,
                )
                
def reschedule_all_messages(scheduler):
    """Alias para mantener imports existentes."""
    return ScheduledMessageService.reschedule_all_messages(scheduler)