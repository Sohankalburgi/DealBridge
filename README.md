# DealBridge

DealBridge helps buyers and sellers turn informal Telegram conversations into clearer, safer deals. Its AI drafts and updates deal terms, flags potential scam signals, and helps check delivery proof. PayPal authorization can hold funds while the agreed deal checks are completed.

> **Prototype notice:** DealBridge is a hackathon prototype, not a regulated escrow service. Use PayPal Sandbox for testing and do not rely on it to protect real transactions.

## Features

- Create and share product or service deal listings through Telegram.
- Use AI to draft deal terms from a seller's description and update them as the conversation changes.
- Flag potential scam signals and answer questions using the deal's known terms.
- After opening a buyer link, ask the AI product questions or describe your needs; it answers from the seller's original description, current deal terms, and recent buyer-seller chat, and identifies details that are not specified.
- Confirm the joined deal in chat with phrases such as “I want to buy,” “deal done,” or “order confirmed”; both participants must still confirm terms, and the buyer must provide an address and authorize payment.
- After marketplace search, a clear “I want to buy this product” message uses the recent matching listing results to join the deal; if no specific listing is available, the bot keeps the purchase flow active and asks for the product name rather than searching for “yes.”
- Check buyer-submitted delivery photos against the agreed product and look for visible defects; unclear results or possible issues require buyer review.
- Authorize, capture, or void payments through PayPal, with a mock checkout path for local demos.
- Ask the buyer for a delivery address after both participants confirm, save it on the deal in Elasticsearch, and then create the PayPal checkout link.
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

After both participants confirm a deal, the bot privately asks the buyer for a full delivery address. It saves the address with the deal in Elasticsearch before creating the payment link; the address is not relayed to the seller.

## Configuration

See [`src/.env.example`](src/.env.example) for the available settings. Keep credentials in your local `.env` file and never commit them.

## License

See [LICENSE](LICENSE).
