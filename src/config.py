import os
import logging
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Enable logging (Fixed single-line format string)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("ai_deal_agent")

# --- CORE KEYS ---
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

# --- PAYPAL & SERVER CONFIG ---
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID", "")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8000").rstrip("/")
PAYPAL_API_BASE = os.getenv("PAYPAL_API_BASE", "https://api-m.sandbox.paypal.com")

# --- ELASTICSEARCH CONFIG ---
ELASTICSEARCH_URL = os.getenv("ELASTICSEARCH_URL", "http://localhost:9200")
ELASTICSEARCH_INDEX = os.getenv("ELASTICSEARCH_INDEX", "deals")
ELASTICSEARCH_USERNAME = os.getenv("ELASTICSEARCH_USERNAME", "")
ELASTICSEARCH_PASSWORD = os.getenv("ELASTICSEARCH_PASSWORD", "")
ELASTICSEARCH_API_KEY = os.getenv("ELASTICSEARCH_API_KEY", "")

if not TELEGRAM_BOT_TOKEN or not GROQ_API_KEY:
    raise ValueError("Missing TELEGRAM_BOT_TOKEN or GROQ_API_KEY in .env file")