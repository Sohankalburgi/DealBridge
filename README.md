# DealBridge

DealBridge helps buyers and sellers turn informal Telegram conversations into clearer, safer deals. Its AI drafts and updates deal terms, flags potential scam signals, and helps check delivery proof. PayPal authorization can hold funds while the agreed deal checks are completed.

> **Prototype notice:** DealBridge is a hackathon prototype, not a regulated escrow service. Use PayPal Sandbox for testing and do not rely on it to protect real transactions.

## Features

- Create and share product or service deal listings through Telegram.
- Use AI to draft deal terms from a seller's description and update them as the conversation changes.
- Flag potential scam signals and answer questions using the deal's known terms.
- Check delivery proof photos with AI.
- Authorize, capture, or void payments through PayPal, with a mock checkout path for local demos.
- Store deal records and history in Elasticsearch.

## Tech stack

- **Language:** Python
- **Bot:** Telegram Bot API with `python-telegram-bot`
- **Web service:** FastAPI and Uvicorn
- **AI:** Groq models through LangChain
- **Payments:** PayPal REST APIs (Sandbox by default)
- **Storage and search:** Elasticsearch

## Requirements

- Python 3.10 or later
- A Telegram bot token
- A Groq API key
- An Elasticsearch instance (defaults to `http://localhost:9200`)
- PayPal Sandbox credentials for real Sandbox payment flows; optional for the mock checkout demo

## Setup on Windows

From the project root, run:

```powershell
cd src
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `src/.env` and set at least:

```dotenv
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
GROQ_API_KEY=your_groq_api_key
```

The app also needs Elasticsearch running. Its default URL is `http://localhost:9200`; set `ELASTICSEARCH_URL` if yours differs. To use PayPal Sandbox, set `PAYPAL_CLIENT_ID` and `PAYPAL_CLIENT_SECRET`. The default PayPal API base is `https://api-m.sandbox.paypal.com`.

Start the bot and web service from the `src` directory:

```powershell
python main.py
```

The bot starts polling Telegram, and the FastAPI service listens on `http://localhost:8000`. The app creates its Elasticsearch index on startup if it does not already exist.

## Telegram commands

- `/start` — Start the bot or begin creating/joining a deal.
- `/search <query>` — Search your unjoined product listings.
- `/orders` — View your orders.
- `/status` — Check the current deal status.
- `/retry` — Retry an available payment step.

## Local PayPal demo

When PayPal credentials are not configured, the app provides a mock checkout page at:

```text
http://localhost:8000/paypal/mock-checkout?deal_id=demo
```

The app also exposes `/paypal/return` for the checkout redirect and `/paypal/webhook` for PayPal authorization notifications. The webhook endpoint is intended for integration with PayPal; the local prototype does not configure webhook verification or public hosting for you.

## Configuration

See [`src/.env.example`](src/.env.example) for the available settings. Keep credentials in your local `.env` file and never commit them.

## License

See [LICENSE](LICENSE).
