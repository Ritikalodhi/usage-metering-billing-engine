from fastapi import FastAPI

from app.api.errors import register_error_handlers
from app.api.routes.billing import router as billing_router
from app.api.routes.generate import router as generate_router
from app.api.routes.health import router as health_router
from app.api.routes.usage import router as usage_router
from app.api.routes.webhooks import router as webhooks_router

app = FastAPI(title="Usage Metering & Billing Engine")

register_error_handlers(app)

app.include_router(health_router)
app.include_router(generate_router)
app.include_router(usage_router)
app.include_router(billing_router)
app.include_router(webhooks_router)

