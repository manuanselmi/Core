from flask import Flask
from .config import load_configurations, configure_logging
from .services.scheduler_service import init_scheduler
from .routes import webhook_bp  # asumimos que es el nuevo nombre
from app.models import db 
# Si tenés más blueprints, agregalos acá

def create_app():
    # Logging + carga de .env
    configure_logging()
    app = Flask(__name__)

    # Carga de variables y configuración general (como DATABASE_URL)
    load_configurations(app)

    # Configurar la base de datos
    db.init_app(app)

    # Iniciar el scheduler (recordatorios)
    init_scheduler(app)

    # Registrar rutas (blueprints)
    app.register_blueprint(webhook_bp)
    # app.register_blueprint(debug_bp)  ← si lo vas a usar

    return app
