# 🤖 AgenteIAWpp — WhatsApp AI Agent (Serverless on AWS)

Sistema de agente conversacional para WhatsApp con integración de IA (OpenAI) y persistencia en AWS completamente serverless.

---

## 🏗️ Arquitectura General

**Región primaria:** `sa-east-1`  
**Secundaria (réplica de datos):** `us-east-1`

### Componentes principales
| Servicio AWS | Función | Detalles |
|---------------|----------|-----------|
| **API Gateway (HTTP API v2)** | Endpoint público del webhook | <ul><li>`GET /webhook` → Verificación de Meta</li><li>`POST /webhook` → Recepción de eventos WhatsApp</li><li>`GET /health` → Health-check</li></ul> |
| **Lambda (Python 3.12 arm64)** | Capa de cómputo principal | Ejecuta `aws.handler.lambda_handler`. Contiene Flask app embebida. |
| **EventBridge Scheduler** | Jobs programados | Invoca la Lambda con `{ "source": "aws.events", "detail-type": "scheduled-job", "agentMode": "job" }` |
| **DynamoDB (Global Table)** | Almacenamiento de eventos, usuarios y recordatorios | Tabla única (`single-table design`). Con TTL, PITR y GSIs. Réplica global `us-east-1`. |
| **Secrets Manager** | Manejo seguro de credenciales | Cada secreto opcional cargado vía `SM_ARN_*` envs. Solo lectura (`secretsmanager:GetSecretValue`). |
| **CloudWatch Logs + Alarms** | Observabilidad y alertas | Logs dedicados. Alarmas por errores, invocaciones y API Gateway 5xx. |
| **SNS + Lambda Notifier + SES** | Notificación por email de errores | Canal interno de alertas a equipo de Kairo Agency. |
| **S3** | Almacenamiento de artefactos | <ul><li>ZIP de código (`app.zip`)</li><li>ZIP de dependencias (layer)</li></ul> |

---

## ⚙️ Flujo E2E

1. **Webhook de Meta**
   - `GET /webhook`: Verifica `hub.verify_token` contra Secrets. Si coincide, responde `hub.challenge` en `text/plain`.
   - `POST /webhook`: Procesa mensajes entrantes de WhatsApp.  
     ➜ Verifica frescura (`WEBHOOK_STALE_MINUTES`).  
     ➜ Aplica idempotencia (`wa_event_id` único en DB).  
     ➜ Inyecta `x-correlation-id` y delega al Orchestrator.

2. **Orchestrator + OpenAI**
   - Determina intención (ej. *agendar*, *cancelar*, *consultar*).  
   - Interactúa con OpenAI API para generar respuestas.  
   - Persiste eventos y estados en DynamoDB.

3. **Respuestas salientes**
   - Utilidades de WhatsApp construyen payloads JSON y llaman a la **Graph API (Meta)**.  
   - Incluye timeouts (`connect=2s`, `read=15s`) y manejo de errores tolerante.

4. **Tareas programadas**
   - EventBridge Scheduler dispara la Lambda en modo `job`.  
   - Se ejecutan recordatorios y tareas recurrentes del agente.

---

## 🔐 Variables de Entorno

### No secretas
| Nombre | Descripción |
|---------|-------------|
| `GRAPH_API_VERSION` | Ej. `v20.0` |
| `PHONE_NUMBER_ID` | ID del número de WhatsApp Business |
| `TZ` | Zona horaria del agente |
| `WEBHOOK_STALE_MINUTES` | Minutos máximos de validez de un evento (default: 3) |
| `SIMULATE_TYPING_MS` | Delay simulado de escritura (default: 0) |
| `AGENT_NAME` | Nombre lógico del agente |
| `STAGE` | Entorno de despliegue (`prod`, `dev`, etc.) |
| `SUPABASE_URL` | (opcional) URL de Supabase si aplica |

### Secretos (ARNs en `SM_ARN_*`)
| Secreto | Uso |
|----------|-----|
| `OPENAI_API_KEY` | Token de OpenAI |
| `WHATSAPP_ACCESS_TOKEN` | Token de Graph API |
| `WHATSAPP_VERIFY_TOKEN` | Token de validación del webhook |
| `DATABASE_URL` | Conexión PostgreSQL (Render) |
| `SUPABASE_SERVICE_ROLE` | (opcional) Supabase backend |
| `GOOGLE_SA_JSON` | (opcional) Google Calendar Service Account |

> El loader ignora secretos faltantes (feature-gate seguro).  
> Solo se ejecuta `GetSecretValue` si el valor es un ARN válido.

---

## 🧩 Empaquetado y Despliegue

### Estructura
├── aws/
│ └── handler.py
├── app/
│ ├── services/
│ ├── db/
│ ├── orchestrator.py
│ ├── prompts/
│ └── ...
└── agente-stack.yaml
