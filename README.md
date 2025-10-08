# 🤖 Agente de IA para WhatsApp con Flask, OpenAI y SQLite

Este proyecto es un bot inteligente de WhatsApp construido con **Python**, **Flask**, la **API de WhatsApp Cloud de Meta**, y **OpenAI**, con persistencia de recordatorios mediante **SQLite**.

El agente puede:
- Recibir mensajes vía WhatsApp (webhooks).
- Responder automáticamente con IA usando OpenAI.
- Agendar y enviar recordatorios personalizados.

---

## 🚀 Tecnologías utilizadas

- Python 3.10+
- Flask
- API de WhatsApp Cloud (Meta)
- API de OpenAI
- SQLite3
- Ngrok (para desarrollo local)
- requests / threading

---

## 📁 Estructura del proyecto

```
Agente/
│
├── app/                     # Lógica principal del bot
│   ├── agents/             # Lógica del agente OpenAI
│   ├── core/               # Procesos como recordatorios, envío, etc.
│   ├── templates/          # HTML si se desea frontend
│   └── utils/              # Funciones auxiliares (envío de mensajes, logs)
│
├── data/                   # Base de datos SQLite
│   └── recordatorios.db
│
├── run.py                  # Script principal para correr la app
├── app.py                  # Archivo Flask con definición de rutas
├── init_db.py              # Script para inicializar la base SQLite
├── requirements.txt        # Dependencias
└── README.md               # Este archivo
```

---

## 🧠 Cómo funciona

1. Meta envía los mensajes recibidos a un **webhook Flask**.
2. Flask procesa el mensaje y puede:
   - Generar una respuesta con **OpenAI**.
   - Guardar un recordatorio en **SQLite**.
3. Un **hilo separado** monitorea los recordatorios y los envía cuando corresponde.

---

## 🛠️ Instalación local (modo desarrollo)

### 1. Clonar el repositorio

```bash
git clone https://github.com/maticoll/Agente.git
cd Agente
```

### 2. Crear entorno virtual e instalar dependencias

```bash
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 3. Inicializar la base de datos

```bash
python init_db.py
```

### 4. Correr el servidor Flask

```bash
python run.py
```

---

## 🌐 Webhook y conexión con WhatsApp

Para recibir mensajes de WhatsApp:

1. Usá [ngrok](https://ngrok.com/) para exponer tu puerto local:

```bash
ngrok http 5000
```

2. Copiá la URL generada (ej: `https://xyz.ngrok.io`) y configurala como **Webhook URL** en tu [Meta App Dashboard](https://developers.facebook.com/).

3. Asegurate de validar el webhook y suscribirte a los eventos `messages`.

---

## 🧪 Ejemplo de uso

- El usuario envía "recordáme tomar agua en 10 minutos".
- El bot interpreta el mensaje, guarda el recordatorio, y pasados los 10 minutos responde automáticamente.

---

## 📦 Deployment recomendado

Para producción 24/7, se recomienda:

- Hosting en **Render**, **Railway**, **Fly.io** o **AWS EC2**
- Migrar a una base de datos más robusta (PostgreSQL) si se escalan los usuarios
- Usar `apscheduler` o `Celery` para manejo profesional de recordatorios
- Certificado HTTPS (Ngrok solo es temporal)
- Sistema de logs + backups para la DB

---

## 📌 Roadmap futuro (sugerido)

- ✅ Modularización del bot
- ⏳ Migración a PostgreSQL
- ⏳ Panel web para ver/editar recordatorios
- ⏳ Dockerización
- ⏳ Hosting en la nube

---

## 📝 Licencia

Este proyecto usa la licencia MIT. Ver archivo `LICENCE.txt`.

---
