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
telegram_bot_username: str | None = None


@app.get("/", response_class=HTMLResponse)
async def homepage():
    if not telegram_bot_username:
        return HTMLResponse("Telegram bot is not ready yet.", status_code=503)

    return f"""<!doctype html>
    <html lang="en">
      <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1">
        <meta name="theme-color" content="#101827">
        <title>DealBridge — Make your next deal clearer</title>
        <style>
          * {{ box-sizing: border-box; }}
          body {{
            margin: 0;
            min-height: 100vh;
            display: grid;
            place-items: center;
            padding: 24px;
            color: #f4f7fb;
            background: radial-gradient(ellipse at 50% 0, #233b62, #101827 68%);
            font-family: Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
          }}
          main {{ width: min(100%, 640px); text-align: center; }}
          .mark {{ color: #7dd3fc; font-size: 15px; font-weight: 700; letter-spacing: .12em; }}
          h1 {{ margin: 22px 0 16px; font-size: clamp(40px, 8vw, 68px); line-height: 1.05; }}
          p {{ margin: 0 auto 30px; max-width: 500px; color: #bdc9d9; font-size: 18px; line-height: 1.65; }}
          a {{
            display: inline-block;
            padding: 15px 24px;
            border-radius: 12px;
            color: #082032;
            background: #7dd3fc;
            font-weight: 750;
            text-decoration: none;
            transition: background .15s ease, transform .15s ease;
          }}
          a:hover {{ background: #a5e3fc; transform: translateY(-2px); }}
          .note {{ margin-top: 20px; font-size: 14px; }}
        </style>
      </head>
      <body>
        <main>
          <div class="mark">DEALBRIDGE</div>
          <h1>Make your next deal clearer.</h1>
          <p>Turn your product or service details into a clear listing, and keep buyers and sellers on the same page—all through Telegram.</p>
          <a href="https://t.me/{telegram_bot_username}?start=seller">Start selling with the Telegram bot</a>
          <p class="note">Opens Telegram. Send a message to the bot to create your first listing.</p>
        </main>
      </body>
    </html>"""

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
    global telegram_bot_username
    bot_info = await telegram_app.bot.get_me()
    if not bot_info.username:
        raise RuntimeError("The configured Telegram bot does not have a username.")
    telegram_bot_username = bot_info.username
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