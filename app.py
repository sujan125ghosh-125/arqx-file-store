import hmac
import json
import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse
from telegram import Update, BotCommand, BotCommandScopeDefault

from bot import (
    BOT_TOKEN, OWNER_IDS, STORAGE_CHAT_ID, DATABASE_URL, USER_COMMANDS,
    build_application, init_db, logger,
)

WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
_DEFAULT_MENU_SET = False
_DB_INITIALIZED = False

@asynccontextmanager
async def lifespan(app: FastAPI):
    if not BOT_TOKEN or not OWNER_IDS or not STORAGE_CHAT_ID or not DATABASE_URL or not WEBHOOK_SECRET:
        logger.warning("Required environment variables are missing; requests will fail until configured.")
    yield

app = FastAPI(title="ARQX File Store Webhook", lifespan=lifespan)

@app.get("/")
async def health():
    return {"ok": True, "service": "ARQX File Store webhook"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    if not WEBHOOK_SECRET:
        raise HTTPException(status_code=503, detail="Webhook secret is not configured")
    supplied = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
    if not hmac.compare_digest(supplied, WEBHOOK_SECRET):
        raise HTTPException(status_code=403, detail="Invalid webhook secret")
    try:
        payload = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    global _DEFAULT_MENU_SET, _DB_INITIALIZED
    application = build_application()
    await application.initialize()
    try:
        if not _DB_INITIALIZED:
            init_db()
            _DB_INITIALIZED = True
        update = Update.de_json(payload, application.bot)
        # Set the public command menu once per warm function instance.
        if not _DEFAULT_MENU_SET:
            try:
                await application.bot.set_my_commands(USER_COMMANDS, scope=BotCommandScopeDefault())
                _DEFAULT_MENU_SET = True
            except Exception:
                logger.warning("Could not set default command menu", exc_info=True)
        await application.process_update(update)
    except Exception:
        logger.exception("Webhook update processing failed")
        raise HTTPException(status_code=500, detail="Update processing failed")
    finally:
        await application.shutdown()
    return JSONResponse({"ok": True})
