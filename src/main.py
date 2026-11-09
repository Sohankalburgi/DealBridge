import asyncio
import os
from contextlib import asynccontextmanager
import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, CallbackQueryHandler, filters

from config import TELEGRAM_BOT_TOKEN, logger
from database import deals_db
from handlers import (
    button_handler,
    complete_payment_hold_notifications,
    handle_message,
    handle_photo,
    orders,
    retry,
    search_products,
    start,
    status,
)
from paypal import authorize_order

@asynccontextmanager
async def lifespan(_: FastAPI):
    await asyncio.to_thread(deals_db.connect)
    yield


app = FastAPI(lifespan=lifespan)
telegram_app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

@app.get("/health")
async def health():
    return {"status": "ok"}

async def mark_funds_held(deal_id: str, auth_id: str):
    """Updates deal state to FUNDS_HELD and notifies buyer and seller on Telegram."""
    await complete_payment_hold_notifications(
        telegram_app, deal_id, auth_id
    )

@app.get("/paypal/mock-checkout", response_class=HTMLResponse)
async def mock_paypal_checkout(deal_id: str, amount: str = "50.00", currency: str = "USD"):
    """Interactive Simulator when real PayPal keys aren't in .env yet."""
    return f"""
    <html>
        <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 60px; background: #f5f7fa;">
            <div style="background: white; max-width: 400px; margin: auto; padding: 30px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.1);">
                <h2 style="color: #003087;">PayPal Sandbox Simulator</h2>
                <p>Escrow Hold for Deal: <b>#{deal_id}</b></p>
                <h1 style="color: #0070ba;">{amount} {currency}</h1>
                <p style="font-size: 14px; color: #555;">Intent: <b>AUTHORIZE (Hold Only)</b></p>
                <br>
                <a href="/paypal/return?deal_id={deal_id}&token=MOCK_ORDER_{deal_id}" 
                   style="background: #0070ba; color: white; padding: 14px 28px; text-decoration: none; border-radius: 24px; font-weight: bold; display: inline-block;">
                   Approve Payment Hold
                </a>
            </div>
        </body>
    </html>
    """

@app.get("/paypal/return", response_class=HTMLResponse)
async def paypal_return(deal_id: str, token: str = ""):
    """Called when the buyer approves the payment on PayPal and is redirected back."""
    try:
        auth_id = await authorize_order(token)
        await mark_funds_held(deal_id, auth_id)
        return """
        <html>
            <body style="font-family: Arial, sans-serif; text-align: center; padding-top: 60px;">
                <h2 style="color: #28a745;">✅ Payment Hold Authorized!</h2>
                <p>You can close this window and return to the Telegram bot.</p>
            </body>
        </html>
        """
    except Exception as e:
        logger.error(f"Error authorizing on return: {e}")
        return f"<html><body><h2>❌ Authorization failed: {e}</h2></body></html>"

@app.post("/paypal/webhook")
async def paypal_webhook(request: Request):
    """Backup listener for PayPal's PAYMENT.AUTHORIZATION.CREATED webhook."""
    data = await request.json()
    event_type = data.get("event_type")
    logger.info(f"Received PayPal Webhook: {event_type}")

    if event_type == "PAYMENT.AUTHORIZATION.CREATED":
        resource = data.get("resource", {})
        deal_id = resource.get("custom_id")
        auth_id = resource.get("id", "WEBHOOK_AUTH")
        if deal_id and deal_id in deals_db:
            await mark_funds_held(deal_id, auth_id)

    return {"status": "success"}

async def main():
    await asyncio.to_thread(deals_db.connect)

    telegram_app.add_handler(CommandHandler("start", start))
    telegram_app.add_handler(CommandHandler("search", search_products))
    telegram_app.add_handler(CommandHandler("orders", orders))
    telegram_app.add_handler(CommandHandler("status", status))
    telegram_app.add_handler(CommandHandler("retry", retry))
    telegram_app.add_handler(CallbackQueryHandler(button_handler))
    telegram_app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    telegram_app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    await telegram_app.initialize()
    await telegram_app.start()
    await telegram_app.updater.start_polling()
    logger.info("Telegram Bot is polling...")

    port = int(os.environ.get("PORT", "8000"))
    config = uvicorn.Config(app=app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    logger.info("FastAPI Server listening on port %s", port)
    await server.serve()

    await telegram_app.updater.stop()
    await telegram_app.stop()
    await telegram_app.shutdown()

if __name__ == "__main__":
    asyncio.run(main())