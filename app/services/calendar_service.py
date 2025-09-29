from app.models import db, Reminder
from datetime import datetime

class CalendarService:
    def create(self, customer_id: int, date: str, title: str, wa_msg_id: str | None) -> int:
        try:
            dt = datetime.strptime(date, "%Y-%m-%d %H:%M")
        except ValueError:
            try:
                dt = datetime.fromisoformat(date)
            except ValueError:
                try:
                    dt = datetime.strptime(date, "%Y-%m-%d")
                except ValueError:
                    raise ValueError(f"Formato de fecha inválido: {date}")
        final_date = dt.replace(second=0)

        evento = Reminder(date=final_date, titulo=title, customer_id=customer_id, wa_msg_id=wa_msg_id)
        db.session.add(evento)
        db.session.commit()
        return evento.id
