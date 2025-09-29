from app import create_app
from app.services.scheduler_service import scheduler, reschedule_all_reminders
from app.services.habitos_services import reschedule_all_habit_prompts
from app.services.scheduled_message_service import reschedule_all_messages
from app.config import configure_logging
from app.services.scheduler_service import register_habit_report_jobs


configure_logging()
app = create_app()

from app.models import db
with app.app_context():
    db.create_all()

reschedule_all_reminders(scheduler)
reschedule_all_habit_prompts()
reschedule_all_messages(scheduler)
register_habit_report_jobs()


app.run(host="0.0.0.0", port=8000)