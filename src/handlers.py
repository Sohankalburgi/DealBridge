import base64
import json
import os
import re
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import logger
from database import deals_db
from paypal import (
    create_authorize_order,
    capture_authorization,
    verify_and_authorize_deal,
)
from agent import (
    analyze_product_with_ai, 
    check_message_for_scams, 
    answer_participant_question,
    answer_order_history_question,
    update_terms_from_chat,
    classify_chat_intent,
    classify_listing_intent,
    classify_product_search_intent,
    extract_product_search_query,
    verify_proof_photo,
)
from decimal import Decimal, ROUND_HALF_UP

PRODUCT_SEARCH_QUERY = re.compile(
    r"^(?:search|find)\s+(?:my\s+)?products?\s+(.+)$", re.IGNORECASE
)
PRODUCT_SEARCH_REQUEST = re.compile(
    r"\b(?:search|find|look(?:ing)?\s+for|browse)\b.{0,60}\b(?:product|products|item|items|thing|something)\b",
    re.IGNORECASE,
)
STRUCTURED_LISTING_ITEM = re.compile(
    r"(?m)^\s*-?\s*Item\s*/\s*Service\s*:",
    re.IGNORECASE,
)
STRUCTURED_LISTING_PRICE = re.compile(
    r"(?m)^\s*-?\s*Price\s*&\s*Currency\s*:",
    re.IGNORECASE,
)
NEW_LISTING_REQUEST = re.compile(
    r"\b(?:add|create|start|make)\b.{0,40}\b(?:product|listing|item|deal)\b"
    r"|\b(?:new|another)\s+(?:product|listing|item|deal)\b",
    re.IGNORECASE,
)


def _is_structured_listing(text: str) -> bool:
    return bool(
        STRUCTURED_LISTING_ITEM.search(text)
        and STRUCTURED_LISTING_PRICE.search(text)
    )

def _participant_name(user) -> str:
    name = user.full_name.strip() or str(user.id)
    if user.username:
        return f"{name} (@{user.username})"
    return name


def _deal_join_link(bot_username: str, deal_id: str) -> str:
    return f"https://t.me/{bot_username}?start=deal_{deal_id}"


def _recent_deal_chat_context(deal: dict) -> str:
    recent_messages = deal.get("conversation", [])[-12:]
    return "\n".join(
        f"{entry.get('sender_role', 'Participant')}: {str(entry.get('text', ''))[:1000]}"
        for entry in recent_messages
        if isinstance(entry, dict) and entry.get("text")
    )


def _is_terms_confirmation_message(text: str) -> bool:
    if text.strip().endswith("?"):
        return False

    normalized_text = re.sub(r"[^\w\s']", " ", text.casefold())
    normalized_text = " ".join(normalized_text.split())
    normalized_text = re.sub(r"^(?:(?:ok|okay|yes|sure)\s+)+", "", normalized_text)
    if normalized_text in {
        "confirm",
        "i confirm",
        "i confirm the terms",
        "i agree",
        "i agree to the terms",
        "i want to buy",
        "i wanted to buy",
        "i want to buy this",
        "i want to buy it",
        "i would like to buy",
        "i would like to buy this",
        "i'd like to buy",
        "deal",
        "deal done",
        "it's a deal",
        "lets do it",
        "ready to pay",
        "proceed to payment",
        "order confirmed",
        "the order is confirmed",
        "confirm order",
        "confirm the order",
        "i confirm the order",
        "i want to buy this product",
        "i wanted to buy this product",
        "i want to buy that product",
        "i wanted to buy that product",
        "i want to buy this item",
        "i wanted to buy this item",
    }:
        return True

    return bool(re.fullmatch(
        r"i\s+(?:want(?:ed)?|would like|would love)\s+to buy\s+(?:this|that|it|this product|that product|this item|that item)",
        normalized_text,
    ))


ORDER_HISTORY_QUERY = re.compile(
    r"\b(?:previous|prior|past|earlier|last)\b.{0,40}\b(?:orders?|deals?|purchases?)\b"
    r"|\b(?:orders?|deals?|purchases?)\b.{0,40}\b(?:previous|prior|past|details?|status|history)\b",
    re.IGNORECASE,
)
RETRY_QUERY = re.compile(
    r"\b(?:retry|try again|restart payment|resend payment|continue payment)\b",
    re.IGNORECASE,
)
MARKETPLACE_PURCHASE_INTENT = re.compile(
    r"^\s*(?:(?:ok(?:ay)?|yes|sure)[,\s]+)*"
    r"(?:i\s+(?:want(?:ed)?|would like|would love)\s+to buy|i'd like to buy|i want to order)"
    r"(?:\s+(?P<product>.+))?\s*[.!]*\s*$",
    re.IGNORECASE,
)


def _marketplace_purchase_intent(text: str) -> re.Match | None:
    if text.strip().endswith("?"):
        return None
    return MARKETPLACE_PURCHASE_INTENT.fullmatch(text)


def _purchase_product_query(match: re.Match) -> str:
    product = _normalize_search_followup(match.group("product") or "")
    product = re.sub(r"^(?:this|that|the)\s+", "", product, flags=re.IGNORECASE)
    if product.casefold() in {"", "it", "this", "that", "product", "item", "this product", "that product", "this item", "that item", "one"}:
        return ""
    return product


def _marketplace_join_keyboard(deal_ids: list[str]) -> InlineKeyboardMarkup:
    buttons = []
    for deal_id in deal_ids:
        deal = deals_db.get(deal_id)
        if deal:
            item = str(deal.get("terms", "Product listing")).splitlines()[0][:40]
            buttons.append(
                [InlineKeyboardButton(f"Join {item}", callback_data=f"joinlisting_{deal_id}")]
            )
    return InlineKeyboardMarkup(buttons)


def _restore_active_deal_context(user_id: int, context: ContextTypes.DEFAULT_TYPE):
    active_deal_id = context.user_data.get("active_deal")
    active_deal = deals_db.get(active_deal_id) if active_deal_id else None
    if active_deal and user_id in (
        active_deal.get("seller_id"),
        active_deal.get("buyer_id"),
    ):
        return active_deal_id, active_deal

    resumable_states = {
        "JOINED",
        "WAITING_FOR_SHIPPING_ADDRESS",
        "PAYMENT_CREATING",
        "WAITING_FOR_PAYMENT",
        "FUNDS_HELD",
        "PROOF_SUBMITTED",
        "NEEDS_BUYER_REVIEW",
        "HOLD_RESUBMIT",
    }
    try:
        orders = deals_db.list_for_user(user_id, limit=20)
    except Exception:
        logger.exception("Could not restore active deal context for user %s", user_id)
        return None, None

    resumable_deals = [
        deal for deal in orders
        if deal.get("state") in resumable_states
        and user_id in (deal.get("seller_id"), deal.get("buyer_id"))
    ]
    if len(resumable_deals) != 1:
        return None, None

    deal = resumable_deals[0]
    deal_id = deal.get("deal_id", getattr(deal, "_deal_id", None))
    if not deal_id:
        logger.warning("Cannot restore active deal for user %s: deal ID is missing", user_id)
        return None, None

    context.user_data["active_deal"] = deal_id
    context.user_data["state"] = None
    logger.info("Restored active deal %s for user %s", deal_id, user_id)
    return deal_id, deal


async def search_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Search the seller's unjoined product/deal records and offer share links."""
    context.user_data["state"] = None
    await search_products_by_text(update, " ".join(context.args).strip())


async def search_products_by_text(update: Update, query_text: str) -> None:
    seller = update.effective_user
    if not query_text:
        await update.message.reply_text("Use /search followed by a product name or detail.")
        return

    try:
        results = deals_db.search_seller_products(seller.id, query_text)
    except Exception:
        logger.exception("Elasticsearch product search failed for seller %s", seller.id)
        await update.message.reply_text("Product search is unavailable right now. Please try again.")
        return

    if not results:
        await update.message.reply_text(
            "No unjoined listings matched that search. Describe a product or service you want to sell and the AI will draft a listing."
        )
        return

    keyboard = []
    for product in results:
        deal_id = product.get("deal_id", product.get("_id", ""))
        item = str(product.get("terms", "Product listing")).splitlines()[0][:45]
        keyboard.append(
            [InlineKeyboardButton(f"{item} · #{deal_id}", callback_data=f"share_{deal_id}")]
        )
    await update.message.reply_text(
        f"Found {len(results)} of your available listings. Select one to generate its buyer link:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def search_marketplace_products(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    query_text: str,
    show_results: bool = True,
) -> None:
    user_id = update.effective_user.id
    try:
        results = deals_db.search_available_products(query_text)
    except Exception:
        logger.exception("Marketplace product search failed for user %s", user_id)
        await update.message.reply_text("Product search is unavailable right now. Please try again.")
        return

    context.user_data["marketplace_search_query"] = query_text
    context.user_data["marketplace_search_results"] = [
        str(product.get("deal_id", ""))
        for product in results
        if product.get("deal_id") and product.get("seller_id") != user_id
    ]
    if not results:
        await update.message.reply_text(
            f"I couldn't find an available listing for “{query_text}”. Try another product name or detail."
        )
        return

    if show_results:
        keyboard = []
        for product in results:
            deal_id = product.get("deal_id", "")
            item = str(product.get("terms", "Product listing")).splitlines()[0][:40]
            if product.get("seller_id") == user_id:
                action = f"share_{deal_id}"
                action_text = "Your listing"
            else:
                action = f"product_{deal_id}"
                action_text = "View"
            keyboard.append(
                [InlineKeyboardButton(f"{item} · {action_text}", callback_data=action)]
            )
        await update.message.reply_text(
            f"Here are available listings matching “{query_text}”:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )


async def join_marketplace_listing(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    deal_id: str,
    query=None,
) -> bool:
    deal = deals_db.get(deal_id)
    user = update.effective_user
    if (
        not deal
        or deal.get("state") != "CREATED"
        or deal.get("buyer_id") not in (None, user.id)
        or deal.get("seller_id") == user.id
    ):
        error = "This listing is no longer available to join."
        if query:
            await query.edit_message_text(error)
        else:
            await update.effective_message.reply_text(error)
        context.user_data["marketplace_search_results"] = []
        return False

    deal.update_fields({
        "buyer_id": user.id,
        "buyer_name": _participant_name(user),
        "buyer_username": user.username or "",
        "state": "JOINED",
    })
    context.user_data["state"] = None
    context.user_data["active_deal"] = deal_id
    context.user_data["marketplace_search_results"] = []
    joined_text = (
        f"You joined deal #{deal_id}.\n\n"
        f"{deal.get('terms', 'No deal terms recorded.')}\n\n"
        "Ask the AI about the product or describe what you need to see how the listed details fit. "
        "It answers from the seller's description and deal terms, and will point out details that aren't specified. "
        "You can also chat with the seller here. Review and confirm the terms when ready."
    )
    confirm_markup = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirm Terms", callback_data=f"confirm_{deal_id}")
    ]])
    if query:
        await query.edit_message_text(
            joined_text,
            reply_markup=confirm_markup,
        )
    else:
        await update.effective_message.reply_text(
            joined_text,
            reply_markup=confirm_markup,
        )

    await context.bot.send_message(
        chat_id=deal["seller_id"],
        text=f"Buyer joined: {_participant_name(user)}\nDeal #{deal_id} is now connected.",
    )
    await context.bot.send_message(
        chat_id=deal["seller_id"],
        text=f"Proposed deal terms for #{deal_id}:\n\n{deal.get('terms', '')}",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Confirm Terms", callback_data=f"confirm_{deal_id}")
        ]]),
    )
    return True


async def handle_marketplace_purchase_intent(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    purchase_intent: re.Match,
) -> bool:
    """Resolve a buyer's conversational purchase intent against their recent search."""
    deal_ids = context.user_data.get("marketplace_search_results", [])
    product_query = _purchase_product_query(purchase_intent)
    last_query = str(context.user_data.get("marketplace_search_query", ""))
    if product_query and product_query.casefold() != last_query.casefold():
        await search_marketplace_products(
            update,
            context,
            product_query,
            show_results=False,
        )
        deal_ids = context.user_data.get("marketplace_search_results", [])

    if len(deal_ids) == 1:
        await join_marketplace_listing(update, context, deal_ids[0])
        return True
    if len(deal_ids) > 1:
        await update.effective_message.reply_text(
            "I found several listings. Choose which one you want to buy:",
            reply_markup=_marketplace_join_keyboard(deal_ids),
        )
        return True

    if not product_query:
        context.user_data["state"] = "WAITING_FOR_MARKETPLACE_PURCHASE_PRODUCT"
        await update.effective_message.reply_text(
            "I couldn't identify a listing to buy yet. Search for the product first, "
            "or tell me its name (for example, “I want to buy a keychain”)."
        )
        return True
    return True


def _normalize_search_followup(text: str) -> str:
    query = re.sub(r"^[\W_]*(?:a|an|the)\s+", "", text.strip(), flags=re.IGNORECASE)
    query = re.sub(r"^(?:product|item|thing|something)\s*$", "", query, flags=re.IGNORECASE)
    return query.strip(" \t\r\n.,!?")


def prepare_paypal_amount(amount, currency):
    if currency == "INR":
        rate_value = os.getenv("INR_TO_USD_RATE")
        if not rate_value:
            raise ValueError("INR_TO_USD_RATE is required to convert INR prices to USD.")
        rate = Decimal(rate_value)
        if rate <= 0:
            raise ValueError("INR_TO_USD_RATE must be greater than zero.")

        usd_amount = (amount * rate).quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP
        )

        return usd_amount, "USD"

    return amount, currency

def extract_price_and_currency(terms_text: str):
    match = re.search(
        r"Price\s*&\s*Currency\s*:\s*\(?\s*"
        r"(\d+(?:\.\d{1,2})?)\s*"
        r"(₹|RUPEES?|INR|RUP|RS\.?|USD)(?=\s|\)|$)",
        terms_text,
        re.IGNORECASE
    )

    if not match:
        raise ValueError(
            f"Could not extract price and currency from: {terms_text}"
        )

    amount = Decimal(match.group(1))
    currency_text = match.group(2).upper()

    currency_map = {
        "₹": "INR",
        "RUPEE": "INR",
        "RUPEES": "INR",
        "RUP": "INR",
        "RS": "INR",
        "RS.": "INR",
        "INR": "INR",
        "USD": "USD"
    }

    currency = currency_map[currency_text]

    return amount, currency


async def _create_and_send_payment_link(
    context: ContextTypes.DEFAULT_TYPE, deal_id: str
) -> None:
    """Create the buyer's checkout after their shipping address has been saved."""
    deal = deals_db.get(deal_id)
    if not deal or not deal.get("buyer_shipping_address"):
        return

    deal["state"] = "PAYMENT_CREATING"
    try:
        original_amount, original_currency = extract_price_and_currency(deal["terms"])
        paypal_amount, paypal_currency = prepare_paypal_amount(
            original_amount,
            original_currency
        )
        if paypal_amount <= 0:
            raise ValueError("The deal price must be greater than zero.")

        amount = f"{paypal_amount:.2f}"
        currency = paypal_currency
        approval_url = await create_authorize_order(
            deal_id=deal_id, amount=amount, currency=currency
        )
        if not approval_url:
            raise ValueError("PayPal did not return an approval URL.")
    except Exception:
        logger.exception("Could not generate the PayPal payment link for deal %s", deal_id)
        deal["state"] = "PAYMENT_SETUP_FAILED"
        error_text = (
            "❌ Could not generate the PayPal payment link. Your saved shipping address "
            "is still on the deal; use /retry to try again."
        )
        await context.bot.send_message(chat_id=deal["seller_id"], text=error_text)
        await context.bot.send_message(chat_id=deal["buyer_id"], text=error_text)
        return

    deal.update_fields(
        {
            "paypal_approval_url": approval_url,
            "state": "WAITING_FOR_PAYMENT",
        }
    )
    pay_keyboard = [
        [InlineKeyboardButton(f"💳 1. Approve {amount} {currency} on PayPal", url=approval_url)],
        [InlineKeyboardButton("✅ 2. I've Approved — Verify Now", callback_data=f"verifypay_{deal_id}")],
    ]
    await context.bot.send_message(
        chat_id=deal["seller_id"],
        text=f"🎉 **Deal Confirmed ({amount} {currency})!**\nThe buyer's secure PayPal hold link has been created.",
        parse_mode="Markdown",
    )
    await context.bot.send_message(
        chat_id=deal["buyer_id"],
        text=f"🔒 **Action Required:**\nPlease authorize the **{amount} {currency}** payment hold on PayPal. No money will be captured until delivery is proven.",
        reply_markup=InlineKeyboardMarkup(pay_keyboard),
        parse_mode="Markdown",
    )


async def trigger_user_confirmation(context: ContextTypes.DEFAULT_TYPE, deal_id: str, user_id: int) -> None:
    """Confirm terms and request a shipping address before creating PayPal checkout."""
    deal = deals_db.get(deal_id)
    if not deal or deal['state'] in (
        'WAITING_FOR_SHIPPING_ADDRESS',
        'WAITING_FOR_PAYMENT',
        'FUNDS_HELD',
        'PAYMENT_CREATING',
        'PAYMENT_SETUP_FAILED',
    ):
        return

    if user_id == deal['seller_id']:
        deals_db.record_terms_agreement(
            deal, user_id, 'seller_confirmed_terms'
        )
        await context.bot.send_message(
            chat_id=deal['seller_id'],
            text="✅ **You confirmed the terms!** Waiting for the buyer to confirm...",
            parse_mode='Markdown'
        )
        if deal['buyer_id']:
            await context.bot.send_message(
                chat_id=deal['buyer_id'],
                text="🔔 **The seller has confirmed the terms!** Type **'Confirm'** or **'I want to buy'**, or click **Confirm Terms** to proceed to payment.",
                parse_mode='Markdown'
            )

    elif user_id == deal['buyer_id']:
        deals_db.record_terms_agreement(
            deal, user_id, 'buyer_confirmed_terms'
        )

    # Ask for the address once both parties agree, before creating checkout.
    if deal['seller_confirmed_terms'] and deal['buyer_confirmed_terms']:
        if deal.get("buyer_shipping_address"):
            await _create_and_send_payment_link(context, deal_id)
            return

        deal["state"] = "WAITING_FOR_SHIPPING_ADDRESS"
        await context.bot.send_message(
            chat_id=deal["buyer_id"],
            text=(
                "✅ The deal is confirmed. Before I create your payment link, please send "
                "the full delivery address, including recipient name, street, city, postal "
                "code, and country. Your address will be saved with this deal."
            ),
        )
        await context.bot.send_message(
            chat_id=deal["seller_id"],
            text="✅ The deal is confirmed. The buyer is being asked for a delivery address before checkout is created.",
        )


async def handle_buyer_shipping_address(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    text: str,
) -> bool:
    """Save a buyer's address privately and start payment setup for the active deal."""
    deal_id = context.user_data.get("active_deal")
    deal = deals_db.get(deal_id) if deal_id else None
    if (
        not deal
        or deal.get("state") != "WAITING_FOR_SHIPPING_ADDRESS"
        or update.effective_user.id != deal.get("buyer_id")
    ):
        return False

    address = text.strip()
    if _is_terms_confirmation_message(address):
        await update.message.reply_text(
            "The deal terms are already confirmed. Please send your full delivery address, "
            "including recipient name, street, city, postal code, and country."
        )
        return True

    if not address:
        await update.message.reply_text(
            "Please send your full delivery address, including recipient name, street, city, postal code, and country."
        )
        return True

    deal.update_fields(
        {
            "buyer_shipping_address": address,
            "state": "PAYMENT_CREATING",
        }
    )
    await update.message.reply_text(
        "Your delivery address has been saved with this deal. I'm creating your payment link now."
    )
    await _create_and_send_payment_link(context, deal_id)
    return True

async def update_deal_terms_from_message(
    context: ContextTypes.DEFAULT_TYPE,
    deal_id: str,
    deal: dict,
    sender_role: str,
    text: str
) -> bool:
    if deal.get('state') not in ('CREATED', 'JOINED'):
        return False

    chat_history = list(deal.get('chat_history', []))
    chat_history.append(f"{sender_role}: {text}")
    deal['chat_history'] = chat_history
    updated_terms = await update_terms_from_chat(deal['terms'], chat_history)
    if updated_terms.strip() == "NO_CHANGE":
        return False

    deal.update_fields(
        {
            'terms': updated_terms,
            'chat_history': [],
            'seller_confirmed_terms': False,
            'buyer_confirmed_terms': False,
        }
    )

    keyboard = [[InlineKeyboardButton("✅ Confirm Terms", callback_data=f"confirm_{deal_id}")]]
    reply_markup = InlineKeyboardMarkup(keyboard)
    new_msg = (
        f"🔄 **Terms Auto-Updated from Chat** 📜\n\n{updated_terms}\n\n"
        "Please review and confirm the updated terms."
    )

    await context.bot.send_message(
        chat_id=deal['seller_id'],
        text=new_msg,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    await context.bot.send_message(
        chat_id=deal['buyer_id'],
        text=new_msg,
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )
    return True


def _order_history_for_ai(user_id: int) -> tuple[list, str]:
    orders = deals_db.list_for_user(user_id)
    records = [
        {
            "deal_id": order.get("deal_id"),
            "role": "seller" if order.get("seller_id") == user_id else "buyer",
            "status": order.get("state", "UNKNOWN"),
            "created_at": order.get("created_at", "unknown"),
            "terms": order.get("terms", "No terms recorded"),
        }
        for order in orders
    ]
    return orders, json.dumps(records, ensure_ascii=True)


async def orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the user's recent deals and allow them to inspect or resume one."""
    user_id = update.effective_user.id
    recent_orders, _ = _order_history_for_ai(user_id)
    if not recent_orders:
        await update.message.reply_text(
            "I couldn't find any deals linked to your account yet. Open a deal link or describe something you want to sell and the AI will draft a listing."
        )
        return

    keyboard = []
    for order in recent_orders:
        deal_id = order.get("deal_id", "unknown")
        status = order.get("state", "UNKNOWN")
        if order.get("seller_id") == user_id:
            partner = order.get("buyer_name") or "No buyer yet"
        else:
            partner = order.get("seller_name") or "Seller"
        keyboard.append(
            [
                InlineKeyboardButton(
                    f"#{deal_id} · {status} · {partner}"[:64],
                    callback_data=f"order_{deal_id}",
                )
            ]
        )
    await update.message.reply_text(
        "Your recent orders and deals (newest first). Choose one for its details:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Show the current order status in this chat."""
    deal_id = context.user_data.get("active_deal")
    deal = deals_db.get(deal_id) if deal_id else None
    user_id = update.effective_user.id
    if not deal or user_id not in (deal.get("seller_id"), deal.get("buyer_id")):
        await update.message.reply_text(
            "There isn't an active deal selected. Use /orders to find a previous deal."
        )
        return

    await update.message.reply_text(
        f"Deal #{deal_id}\nStatus: {deal.get('state', 'UNKNOWN')}\n\n"
        f"{deal.get('terms', 'No deal terms recorded.')}"
    )


async def retry(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Retry a failed payment-link step or resend the existing buyer checkout."""
    deal_id = context.user_data.get("active_deal")
    deal = deals_db.get(deal_id) if deal_id else None
    user_id = update.effective_user.id
    if not deal or user_id not in (deal.get("seller_id"), deal.get("buyer_id")):
        await update.message.reply_text(
            "Select a deal with /orders before retrying its payment process."
        )
        return

    if deal.get("state") == "WAITING_FOR_SHIPPING_ADDRESS":
        if user_id == deal.get("buyer_id"):
            await update.message.reply_text(
                "Please send your full delivery address here before checkout can be created."
            )
        else:
            await update.message.reply_text("The buyer still needs to provide a delivery address.")
        return

    if deal.get("state") == "PAYMENT_SETUP_FAILED":
        if user_id != deal.get("buyer_id"):
            await update.message.reply_text("The buyer must retry payment setup for this deal.")
            return
        await update.message.reply_text("Retrying payment setup with your saved delivery address...")
        await _create_and_send_payment_link(context, deal_id)
        return

    if (
        deal.get("state") in ("CREATED", "JOINED")
        and deal.get("seller_confirmed_terms")
        and deal.get("buyer_confirmed_terms")
        and deal.get("paypal_approval_url")
    ):
        deal["state"] = "WAITING_FOR_PAYMENT"
        buyer_keyboard = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("💳 Continue PayPal Checkout", url=deal["paypal_approval_url"])],
                [InlineKeyboardButton("🔍 Verify Payment", callback_data=f"verifypay_{deal_id}")],
            ]
        )
        await context.bot.send_message(
            chat_id=deal["buyer_id"],
            text="You can continue the existing payment checkout. No new PayPal order was created.",
            reply_markup=buyer_keyboard,
        )
        await update.message.reply_text("The existing payment checkout was sent to the buyer.")
        return

    if deal.get("state") == "WAITING_FOR_PAYMENT":
        if user_id != deal.get("buyer_id"):
            await update.message.reply_text("The buyer must complete or retry the payment approval.")
            return
        approval_url = deal.get("paypal_approval_url")
        keyboard = []
        if approval_url:
            keyboard.append([InlineKeyboardButton("💳 Retry PayPal Approval", url=approval_url)])
        keyboard.append(
            [InlineKeyboardButton("🔍 Verify Payment", callback_data=f"verifypay_{deal_id}")]
        )
        await update.message.reply_text(
            "Your deal is waiting for payment approval. Continue checkout or verify if you already approved it.",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if (
        deal.get("state") in ("CREATED", "JOINED")
        and deal.get("seller_confirmed_terms")
        and deal.get("buyer_confirmed_terms")
    ):
        await update.message.reply_text("Retrying the secure payment setup...")
        await trigger_user_confirmation(context, deal_id, user_id)
        return

    if deal.get("state") in ("CREATED", "JOINED"):
        await update.message.reply_text(
            "This deal is not ready for payment yet. Both participants need to confirm the current terms first."
        )
        return

    await update.message.reply_text(
        f"This deal cannot restart payment from its current status ({deal.get('state', 'UNKNOWN')}). "
        "Use /orders to check its details or describe another product or service for the AI to draft a new listing."
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles standard onboarding and deep-linked deal joining."""
    user = update.effective_user
    args = context.args 

    if args and args[0].startswith("deal_"):
        deal_id = args[0].replace("deal_", "")
        
        if deal_id in deals_db:
            deal = deals_db[deal_id]
            if user.id == deal.get('seller_id'):
                await update.message.reply_text("You created this deal. Use /orders or /search to manage your listings.")
                return
            if deal.get('buyer_id') not in (None, user.id):
                await update.message.reply_text("This deal has already been joined by another buyer.")
                return
            deal.update_fields({
                'buyer_id': user.id,
                'buyer_name': _participant_name(user),
                'buyer_username': user.username or '',
                'state': 'JOINED',
            })
            context.user_data['state'] = None
            context.user_data['active_deal'] = deal_id
            
            await update.message.reply_text(
                f"Welcome {user.first_name}! You've securely joined deal `{deal_id}` as the buyer.\n\n"
                "You can ask the AI about the product or describe what you need to see how the listed details fit. "
                "It answers from the seller's description and deal terms, and will point out details that aren't specified. "
                "You can also message the seller directly in this chat."
            )
            
            seller_id = deals_db[deal_id]['seller_id']
            await context.bot.send_message(
                chat_id=seller_id,
                text=f"🔔 Buyer joined: {_participant_name(user)}\n"
                     f"Deal #{deal_id} is now connected. Messages from the buyer will be identified by their name and role."
            )

            keyboard = [[InlineKeyboardButton("✅ Confirm Terms", callback_data=f"confirm_{deal_id}")]]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            terms_msg = (
                f"📜 **Proposed Deal Terms**\n\n{deals_db[deal_id]['terms']}\n\n"
                "Please review and click **Confirm Terms** (or simply type *'I want to buy'*) to proceed to the payment hold."
            )
            
            await context.bot.send_message(chat_id=user.id, text=terms_msg, reply_markup=reply_markup, parse_mode='Markdown')
            await context.bot.send_message(chat_id=seller_id, text=terms_msg, reply_markup=reply_markup, parse_mode='Markdown')

        else:
            await update.message.reply_text("Sorry, this deal link is invalid or has expired.")
        return

    context.user_data.clear()
    await update.message.reply_text(
        f"Hi {user.mention_html()}! Welcome to the AI Deal Agent.\n\n"
        "Describe a product or service you want to sell and the AI will draft a deal. "
        "Use /search to find your unjoined product listings, "
        "/orders to review past deals, "
        "/status for the selected deal, or /retry to continue a payment step.",
        parse_mode='HTML'
    )

async def generate_and_send_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, new_input: str) -> None:
    """Runs the AI extraction, merging cumulative seller inputs with the existing draft."""
    message = update.message if update.message else update.callback_query.message
    processing_msg = await message.reply_text("🤖 AI is structuring your deal terms...")
    
    if _is_structured_listing(new_input):
        context.user_data['draft_inputs'] = [new_input]
        ai_structured_terms = new_input.strip()
    else:
        if 'draft_inputs' not in context.user_data:
            context.user_data['draft_inputs'] = []
        context.user_data['draft_inputs'].append(new_input)

        full_seller_notes = "\n".join(
            [f"- Message {i+1}: {msg}" for i, msg in enumerate(context.user_data['draft_inputs'])]
        )
        existing_terms = context.user_data.get('draft_terms', "None yet.")

        ai_structured_terms = await analyze_product_with_ai(full_seller_notes, existing_terms)

    if ai_structured_terms == "AI Analysis unavailable at the moment.":
        context.user_data['state'] = 'WAITING_FOR_DEAL_UPDATES'
        await processing_msg.edit_text(
            "I couldn't draft the listing right now. Your details are still here; please try again in a moment or send the product details again."
        )
        return

    context.user_data['draft_terms'] = ai_structured_terms
    context.user_data['state'] = 'WAITING_FOR_DEAL_UPDATES'
    
    keyboard = [
        [InlineKeyboardButton("✅ Looks Good (Create Deal)", callback_data="draft_approve")],
        [InlineKeyboardButton("✏️ Add Missing Details", callback_data="draft_update")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    
    await processing_msg.edit_text(
        f"**Draft Deal Terms:**\n\n{ai_structured_terms}\n\n"
        "Review the **Open Questions** above. You can **just type the missing details directly below**, or click Finalize when ready!",
        reply_markup=reply_markup,
        parse_mode='Markdown'
    )

async def process_deal_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    new_info = update.message.text
    await generate_and_send_draft(update, context, new_info)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer() 
    data = query.data
    
    # 1. Handle Draft Updates
    if data == "draft_update":
        context.user_data['state'] = 'WAITING_FOR_DEAL_UPDATES'
        await query.edit_message_text(
            f"**Draft Terms:**\n\n{context.user_data.get('draft_terms')}\n\n"
            "👇 **Please type in the missing details (like model, brand, condition, or deadline) below:**",
            parse_mode='Markdown'
        )
        return
    
    # 2. Handle Draft Approval
    elif data == "draft_approve":
        short_id = str(uuid.uuid4())[:8]
        bot_username = (await context.bot.get_me()).username
        join_link = _deal_join_link(bot_username, short_id)
        
        deals_db[short_id] = {
            'seller_id': update.effective_user.id,
            'seller_name': _participant_name(update.effective_user),
            'seller_username': update.effective_user.username or '',
            'buyer_id': None,
            'state': 'CREATED',
            'raw_description': "\n".join(context.user_data.get('draft_inputs', [])),
            'terms': context.user_data.get('draft_terms'),
            'seller_confirmed_terms': True,  # Seller approved at creation
            'buyer_confirmed_terms': False,
            'chat_history': []
        }
        
        context.user_data['state'] = None
        context.user_data['active_deal'] = short_id

        await query.edit_message_text(
            f"✅ **Deal drafted successfully!**\n\n"
            f"**Final Terms:**\n{context.user_data.get('draft_terms')}\n\n"
            f"Share this link with your buyer so they can securely join the deal:\n"
            f"`{join_link}`\n\n"
            f"Once they join, this chat becomes your secure communication channel.",
            parse_mode='Markdown'
        )
        return

    if data.startswith("order_"):
        deal_id = data.removeprefix("order_")
        deal = deals_db.get(deal_id)
        user_id = update.effective_user.id
        if not deal or user_id not in (deal.get("seller_id"), deal.get("buyer_id")):
            await query.edit_message_text("That order could not be found for your account.")
            return

        keyboard = [[InlineKeyboardButton("Open this deal in chat", callback_data=f"resume_{deal_id}")]]
        await query.edit_message_text(
            f"Deal #{deal_id}\n"
            f"Role: {'seller' if user_id == deal.get('seller_id') else 'buyer'}\n"
            f"Buyer: {deal.get('buyer_name') or 'Not joined yet'}\n"
            f"Seller: {deal.get('seller_name') or 'Seller'}\n"
            f"Status: {deal.get('state', 'UNKNOWN')}\n"
            f"Created: {deal.get('created_at', 'unknown')}\n\n"
            f"{deal.get('terms', 'No deal terms recorded.')}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("share_"):
        deal_id = data.removeprefix("share_")
        deal = deals_db.get(deal_id)
        if (
            not deal
            or update.effective_user.id != deal.get("seller_id")
            or deal.get("buyer_id") is not None
            or deal.get("state") != "CREATED"
        ):
            await query.edit_message_text("This listing is no longer available to share.")
            return

        bot_username = (await context.bot.get_me()).username
        join_link = _deal_join_link(bot_username, deal_id)
        context.user_data["state"] = None
        context.user_data["active_deal"] = deal_id
        await query.edit_message_text(
            f"Buyer link for listing #{deal_id}:\n{join_link}\n\n"
            "Send this link to your buyer. Their Telegram name will appear here when they join."
        )
        return

    if data.startswith("product_"):
        deal_id = data.removeprefix("product_")
        deal = deals_db.get(deal_id)
        if not deal or deal.get("state") != "CREATED":
            await query.edit_message_text("This listing is no longer available.")
            return
        if update.effective_user.id == deal.get("seller_id"):
            await query.edit_message_text(
                f"Your listing #{deal_id} is available to share:\n"
                f"{_deal_join_link((await context.bot.get_me()).username, deal_id)}"
            )
            return
        keyboard = [[
            InlineKeyboardButton("Join this deal", callback_data=f"joinlisting_{deal_id}")
        ]]
        await query.edit_message_text(
            f"Available listing #{deal_id}\n\n{deal.get('terms', 'No listing details available.')}",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )
        return

    if data.startswith("joinlisting_"):
        deal_id = data.removeprefix("joinlisting_")
        await join_marketplace_listing(update, context, deal_id, query)
        return

    if data.startswith("resume_"):
        deal_id = data.removeprefix("resume_")
        deal = deals_db.get(deal_id)
        user_id = update.effective_user.id
        if not deal or user_id not in (deal.get("seller_id"), deal.get("buyer_id")):
            await query.edit_message_text("That order could not be opened for your account.")
            return

        context.user_data["state"] = None
        context.user_data["active_deal"] = deal_id
        await query.edit_message_text(
            f"Deal #{deal_id} is now selected in this chat.\n"
            f"Status: {deal.get('state', 'UNKNOWN')}\n\n"
            "You can continue messaging the other participant here, or use /status and /retry."
        )
        return

    # 3. Handle Terms Confirmation (Buyer & Seller)
    if data.startswith("confirm_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if not deal or update.effective_user.id not in (
            deal.get("seller_id"),
            deal.get("buyer_id"),
        ):
            await query.edit_message_text("This deal confirmation is unavailable for your account.")
            return
            
        await query.edit_message_text(f"✅ You confirmed the terms.\n\n{deal['terms']}")
        await trigger_user_confirmation(context, deal_id, update.effective_user.id)
        return

    # 4. Handle Buyer Manual Proof Approval (When AI returns ASK_BUYER)
    if data.startswith("release_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if (
            not deal
            or update.effective_user.id != deal['buyer_id']
            or deal.get('state') != 'NEEDS_BUYER_REVIEW'
        ):
            return

        try:
            capture_id = await capture_authorization(deal.get('authorization_id', 'MOCK_AUTH'))
        except Exception:
            logger.exception("Manual release capture failed for deal %s", deal_id)
            await context.bot.send_message(
                chat_id=deal['buyer_id'],
                text="❌ PayPal could not release the payment. The funds remain held; please try again later."
            )
            return

        deal['capture_id'] = capture_id
        deal['state'] = 'COMPLETED'
        try:
            if query.message.caption is not None:
                await query.edit_message_caption(
                    caption=f"✅ **You approved the received-order proof!** Payment released (Capture ID: `{capture_id}`).",
                    parse_mode='Markdown'
                )
            else:
                await query.edit_message_text(
                    f"✅ **You approved the received-order proof!** Payment released (Capture ID: `{capture_id}`).",
                    parse_mode='Markdown'
                )
        except Exception:
            logger.exception("Could not update buyer release message for deal %s", deal_id)
        try:
            await context.bot.send_message(
                chat_id=deal['seller_id'],
                text=f"🎉 **Buyer approved the received-order proof!** Funds have been captured (`{capture_id}`). Deal completed!",
                parse_mode='Markdown'
            )
        except Exception:
            logger.exception("Could not notify seller of manual release for deal %s", deal_id)
        return

    # 5. Handle Buyer Proof Dispute (When AI returns ASK_BUYER)
    if data.startswith("dispute_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if (
            not deal
            or update.effective_user.id != deal['buyer_id']
            or deal.get('state') != 'NEEDS_BUYER_REVIEW'
        ):
            return

        deal['state'] = 'DISPUTED'
        if query.message.caption is not None:
            await query.edit_message_caption(
                caption="🚨 **Dispute Opened.** Your funds remain safely held and have NOT been captured.",
                parse_mode='Markdown'
            )
        else:
            await query.edit_message_text(
                "🚨 **Dispute Opened.** Your funds remain safely held and have NOT been captured.",
                parse_mode='Markdown'
            )
        await context.bot.send_message(
            chat_id=deal['seller_id'],
            text="🚨 **The buyer reported a problem with the proof.** Deal moved to `DISPUTED`handle_message for admin review; funds remain held.",
            parse_mode='Markdown'
        )
        return

    if data.startswith("verifypay_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if (
            not deal
            or update.effective_user.id != deal.get("buyer_id")
            or deal.get("state") != "WAITING_FOR_PAYMENT"
        ):
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text="Payment verification isn't available for your account or this deal state.",
            )
            return
        success, info = await verify_and_authorize_deal(deal_id)
        if success:
            await complete_payment_hold_notifications(context, deal_id, info)
        else:
            await context.bot.send_message(
                chat_id=update.effective_user.id,
                text=f"⏳ **Payment Not Yet Authorized:**\n{info}"
            )
        return
    
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.message
    if message is None or message.text is None:
        logger.warning("Ignoring a text update without a message.")
        return
    text = message.text

    if await handle_buyer_shipping_address(update, context, text):
        return

    if ORDER_HISTORY_QUERY.search(text):
        _, history = _order_history_for_ai(update.effective_user.id)
        answer = await answer_order_history_question(text, history)
        if answer == "ORDER_HISTORY_UNAVAILABLE":
            await message.reply_text(
                "I couldn't retrieve your previous-order details right now. Please try /orders again shortly."
            )
        else:
            await message.reply_text(answer)
        return

    product_search = PRODUCT_SEARCH_QUERY.match(text)
    if product_search:
        await search_products_by_text(update, product_search.group(1).strip())
        return

    if RETRY_QUERY.search(text):
        await retry(update, context)
        return

    if NEW_LISTING_REQUEST.search(text) and not _is_structured_listing(text):
        context.user_data["active_deal"] = None
        context.user_data["draft_terms"] = "None yet."
        context.user_data["draft_inputs"] = []
        context.user_data["state"] = "WAITING_FOR_DEAL_UPDATES"
        await message.reply_text(
            "Sure — send the product or service details, including its price and currency, and I'll draft a new listing for you."
        )
        return

    if _is_structured_listing(text):
        context.user_data["active_deal"] = None
        context.user_data["draft_terms"] = "None yet."
        context.user_data["draft_inputs"] = []
        await generate_and_send_draft(update, context, text)
        return

    current_state = context.user_data.get("state")
    purchase_intent = _marketplace_purchase_intent(text)
    active_deal_id = context.user_data.get("active_deal")
    active_deal = deals_db.get(active_deal_id) if active_deal_id else None
    if not active_deal and (
        purchase_intent or _is_terms_confirmation_message(text)
    ):
        active_deal_id, active_deal = _restore_active_deal_context(
            update.effective_user.id,
            context,
        )
    active_participant = bool(
        active_deal
        and update.effective_user.id
        in (active_deal.get("seller_id"), active_deal.get("buyer_id"))
    )
    if active_participant and _is_terms_confirmation_message(text):
        context.user_data["state"] = None
        current_state = None

    if current_state == "WAITING_FOR_MARKETPLACE_PURCHASE_PRODUCT" and not active_participant:
        product_query = _normalize_search_followup(text)
        if product_query.casefold() in {"yes", "yes please", "ok", "okay", "sure", "this product", "that product", "it"}:
            await message.reply_text(
                "Which product would you like to buy? Please send its name, such as “keychain”."
            )
            return
        await search_marketplace_products(
            update,
            context,
            product_query,
            show_results=False,
        )
        deal_ids = context.user_data.get("marketplace_search_results", [])
        if len(deal_ids) == 1:
            await join_marketplace_listing(update, context, deal_ids[0])
        elif len(deal_ids) > 1:
            await message.reply_text(
                "I found several listings. Choose which one you want to buy:",
                reply_markup=_marketplace_join_keyboard(deal_ids),
            )
        elif not deal_ids:
            context.user_data["state"] = "WAITING_FOR_MARKETPLACE_PURCHASE_PRODUCT"
        return

    if purchase_intent and not active_participant:
        await handle_marketplace_purchase_intent(update, context, purchase_intent)
        return

    if current_state == "WAITING_FOR_PRODUCT_SEARCH":
        query_text = _normalize_search_followup(text)
        if not query_text:
            await message.reply_text("What product or service are you looking for? For example: running shoes or iPhone 15.")
            return
        context.user_data["state"] = None
        await search_marketplace_products(update, context, query_text)
        return

    if current_state == 'WAITING_FOR_DEAL_UPDATES':
        await process_deal_update(update, context)
        return

    if PRODUCT_SEARCH_REQUEST.search(text) or (
        not (active_participant and _is_terms_confirmation_message(text))
        and await classify_product_search_intent(text)
    ):
        query_text = _normalize_search_followup(
            await extract_product_search_query(text)
        )
        if not query_text:
            context.user_data["state"] = "WAITING_FOR_PRODUCT_SEARCH"
            await message.reply_text(
                "Sure, I can search available listings. What product or service are you looking for?"
            )
            return
        await search_marketplace_products(update, context, query_text)
        return

    if active_deal_id and active_deal_id in deals_db:
        deal = deals_db[active_deal_id]
        user_id = update.effective_user.id
        
        if user_id == deal['seller_id']:
            sender_role, recipient_id = "Seller", deal['buyer_id']
            sender_name = deal.get("seller_name", "Seller")
        elif user_id == deal['buyer_id']:
            sender_role, recipient_id = "Buyer", deal['seller_id']
            sender_name = deal.get("buyer_name", "Buyer")
        else:
            return 

        if not recipient_id:
            await update.message.reply_text("⏳ Your buyer hasn't joined yet. Share the link with them first!")
            return

        deals_db.append_conversation(deal, sender_role, text)

        # Auto-verify PayPal order if the buyer says "I have paid" / "paid" / "done"
        payment_keywords = ("paid", "i have paid", "payment done", "approved", "sent money")
        if sender_role == "Buyer" and deal['state'] == 'WAITING_FOR_PAYMENT' and any(k in text.lower() for k in payment_keywords):
            check_msg = await update.message.reply_text("🔍 Checking your payment status with PayPal...")
            success, info = await verify_and_authorize_deal(active_deal_id)
            if success:
                await check_msg.delete()
                await complete_payment_hold_notifications(context, active_deal_id, info)
                return
            else:
                await check_msg.edit_text(f"⏳ {info}")
                return

        # Classify both participants' messages and dispatch only deal-relevant intents.
        message_confirms_terms = _is_terms_confirmation_message(text)
        if message_confirms_terms:
            intent = "CONFIRM_TERMS"
        else:
            intent = await classify_chat_intent(text, sender_role, deal.get("terms", ""))
        logger.info(
            "Deal chat intent: deal=%s role=%s user=%s intent=%s",
            active_deal_id,
            sender_role,
            user_id,
            intent,
        )
        is_confirming = message_confirms_terms or intent == "CONFIRM_TERMS"

        # 3. AI Scam Check & Relay
        scam_analysis = await check_message_for_scams(text)
        warning_label = f"\n\n🚨 **AI Safety Warning:** {scam_analysis}" if "HIGH" in scam_analysis.upper() or "MEDIUM" in scam_analysis.upper() else ""
        relayed_message = f"💬 {sender_role} ({sender_name}):\n{text}{warning_label}"

        try:
            await context.bot.send_message(chat_id=recipient_id, text=relayed_message)
        except Exception as e:
            logger.error(f"Failed to relay message: {e}")
            await update.message.reply_text("❌ Failed to send message. The other user might have blocked the bot.")
            return

        if intent == "ASK_DEAL_QUESTION" and not is_confirming:
            ai_answer = await answer_participant_question(
                text,
                deal.get("terms", ""),
                sender_role,
                deal.get("raw_description", ""),
                _recent_deal_chat_context(deal),
            )
            await update.message.reply_text(f"🤖 AI Deal Agent:\n{ai_answer}")
            try:
                await context.bot.send_message(
                    chat_id=recipient_id,
                    text=f"🤖 AI Deal Agent answered {sender_role.lower()} ({sender_name}):\n{ai_answer}",
                )
            except Exception as e:
                logger.error("Failed to relay deal question answer: %s", e)
            return

        if intent == "UPDATE_TERMS":
            await update_deal_terms_from_message(
                context,
                active_deal_id,
                deal,
                sender_role,
                text,
            )
            return

        if is_confirming and deal.get("state") in ("CREATED", "JOINED"):
            logger.info(
                f"Buyer/seller confirmation detected: "
                f"user={user_id}, deal={active_deal_id}"
            )
            await trigger_user_confirmation(context, active_deal_id, user_id)
            if sender_role == "Buyer" and message_confirms_terms:
                if deal.get("state") in ("CREATED", "JOINED"):
                    await update.message.reply_text(
                        "✅ You confirmed the current terms for this listing. "
                        "The seller must also confirm before checkout can be prepared. "
                        "You will still review and authorize the PayPal payment yourself."
                    )
                    await context.bot.send_message(
                        chat_id=deal["seller_id"],
                        text=f"✅ The buyer confirmed the current terms for deal #{active_deal_id}. "
                        "Please review and confirm the terms to continue.",
                    )

    elif await classify_listing_intent(text):
        context.user_data["draft_terms"] = "None yet."
        context.user_data["draft_inputs"] = []
        await generate_and_send_draft(update, context, text)
    else:
        await update.message.reply_text(
            "I can help with your deal, find your listings with /search, or draft a new listing. "
            "To start one, describe what you want to sell, including its price and delivery or fulfillment details."
        )


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes received-order evidence photos sent by the buyer."""
    active_deal_id = context.user_data.get('active_deal')
    if not active_deal_id or active_deal_id not in deals_db:
        await update.message.reply_text("Please join or create an active deal first.")
        return

    deal = deals_db[active_deal_id]
    user_id = update.effective_user.id

    if user_id != deal['buyer_id']:
        await update.message.reply_text("Only the buyer can upload a received-order photo.")
        return

    if deal['state'] not in ('FUNDS_HELD', 'HOLD_RESUBMIT'):
        await update.message.reply_text(
            f"⏳ Cannot submit proof right now (Current Deal State: `{deal['state']}`). "
            "The payment hold must be secured before you submit a received-order photo.",
            parse_mode='Markdown'
        )
        return

    status_msg = await update.message.reply_text(
        "🔍 **AI is checking the seller's code, product match, and visible condition in your delivery photo. Send a clear photo of the actual received item, not a listing image.**",
        parse_mode='Markdown'
    )

    # Download the highest-resolution photo into memory and encode to Base64
    photo_file = await update.message.photo[-1].get_file()
    photo_bytes = await photo_file.download_as_bytearray()
    image_b64 = base64.b64encode(photo_bytes).decode('utf-8')
    caption = update.message.caption or ""

    deal['state'] = 'PROOF_SUBMITTED'
    verdict = await verify_proof_photo(
        image_base64=image_b64,
        expected_code=deal.get('proof_code', 'UNKNOWN'),
        terms=deal['terms'],
        caption=caption
    )
    deal['last_verdict'] = verdict
    decision = verdict['decision']

    # --- OUTCOME 1: HOLD (Code missing or wrong) ---
    if decision == "HOLD":
        deal['state'] = 'HOLD_RESUBMIT'
        await status_msg.edit_text(
            f"❌ **Proof Rejected — Funds Remain Held**\n\n"
            f"**Reason:** {verdict['reason']}\n\n"
            "Ask the seller to send the code in the deal chat if you don't have it, then send a clear photo of the actual received item with the code visible.",
            parse_mode='Markdown'
        )

    # --- OUTCOME 2: ASK_BUYER (Ambiguous item match or fail-closed) ---
    elif decision == "ASK_BUYER":
        deal['state'] = 'NEEDS_BUYER_REVIEW'
        await status_msg.edit_text(
            f"⚠️ **Proof Submitted for Buyer Review**\n\n"
            f"**AI Notes:** {verdict['reason']}\n"
            f"The agent checks the product against the agreed terms and looks for visible damage or other issues. It could not verify everything automatically. Confirm receipt to release payment, or report a problem.",
            parse_mode='Markdown'
        )

        review_keyboard = [
            [InlineKeyboardButton("✅ Approve & Release Payment", callback_data=f"release_{active_deal_id}")],
            [InlineKeyboardButton("🚨 Report Problem (Dispute)", callback_data=f"dispute_{active_deal_id}")]
        ]
        await context.bot.send_photo(
            chat_id=deal['buyer_id'],
            photo=update.message.photo[-1].file_id,
            caption=(
                f"📦 **Your Received-Order Photo Needs Review**\n\n"
                f"**AI Verification:** {verdict['reason']}\n"
                f"**Product condition:** {verdict['product_condition']}\n"
                f"**Tracking:** `{verdict['tracking']}`\n\n"
                f"If you received the item as agreed, approve releasing the held funds to the seller. Otherwise, report a problem."
            ),
            reply_markup=InlineKeyboardMarkup(review_keyboard),
            parse_mode='Markdown'
        )

    # --- OUTCOME 3: RELEASE (All checks passed -> Capture PayPal Hold) ---
    elif decision == "RELEASE":
        try:
            capture_id = await capture_authorization(deal.get('authorization_id', 'MOCK_AUTH'))
        except Exception:
            logger.exception("PayPal capture failed for deal %s", active_deal_id)
            deal['state'] = 'NEEDS_BUYER_REVIEW'
            await status_msg.edit_text(
                "❌ The photo was verified, but PayPal could not release the funds. "
                "The hold remains in place; you can retry approval or report a problem."
            )
            review_keyboard = [
                [InlineKeyboardButton("✅ Retry Release", callback_data=f"release_{active_deal_id}")],
                [InlineKeyboardButton("🚨 Report Problem (Dispute)", callback_data=f"dispute_{active_deal_id}")]
            ]
            await context.bot.send_message(
                chat_id=deal['buyer_id'],
                text="Your received-order photo passed verification, but payment release failed. The funds remain held.",
                reply_markup=InlineKeyboardMarkup(review_keyboard),
            )
            return

        deal['capture_id'] = capture_id
        deal['state'] = 'COMPLETED'
        try:
            await status_msg.edit_text(
                f"✅ **Received-Order Photo Verified & Payment Released!**\n\n"
                f"**AI Verdict:** {verdict['reason']}\n"
                f"**Product condition:** {verdict['product_condition']}\n"
                f"**PayPal Capture ID:** `{capture_id}`\n\n"
                f"The held funds have been released to the seller.",
                parse_mode='Markdown'
            )
        except Exception:
            logger.exception("Could not notify buyer of completed deal %s", active_deal_id)

        try:
            await context.bot.send_message(
                chat_id=deal['seller_id'],
                text=(
                    f"🎉 **Buyer receipt verified — payment released!**\n"
                    f"**AI Verdict:** {verdict['reason']}\n"
                    f"**PayPal Capture ID:** `{capture_id}`\n"
                    f"Deal #{active_deal_id} is complete."
                ),
                parse_mode='Markdown'
            )
        except Exception:
            logger.exception("Could not notify seller of completed deal %s", active_deal_id)

        try:
            await context.bot.send_photo(
                chat_id=deal['buyer_id'],
                photo=update.message.photo[-1].file_id,
                caption=(
                    f"✅ **Received-Order Photo Verified — Deal Completed!**\n\n"
                    f"**AI Verdict:** {verdict['reason']}\n"
                    f"**Product condition:** {verdict['product_condition']}\n"
                    f"**Tracking:** `{verdict['tracking']}`\n"
                    f"**PayPal Capture ID:** `{capture_id}`\n\n"
                    f"The payment hold has been released to the seller."
                ),
                parse_mode='Markdown'
            )
        except Exception:
            logger.exception("Could not send verified photo to buyer for deal %s", active_deal_id)

async def complete_payment_hold_notifications(context: ContextTypes.DEFAULT_TYPE, deal_id: str, auth_id: str) -> None:
    """Marks the deal as FUNDS_HELD and sends the proof code to the seller."""
    deal = deals_db.get(deal_id)
    if not deal or deal.get('state') != 'WAITING_FOR_PAYMENT':
        logger.info(
            "Ignoring payment-hold notification for deal %s in state %s",
            deal_id,
            deal.get('state') if deal else "MISSING",
        )
        return

    deal['state'] = 'FUNDS_HELD'
    deal['authorization_id'] = auth_id
    deal['proof_code'] = str(uuid.uuid4())[:6].upper()

    await context.bot.send_message(
        chat_id=deal['buyer_id'],
        text=f"✅ **Payment Hold Secured!** (Auth ID: `{auth_id}`)\n"
             f"Your funds are safely held in escrow. After the order arrives, send a clear photo of the actual item you received (not a listing or stock image) with the code from the seller visible. The agent checks that it matches the deal and looks for visible damage or other issues before releasing payment. Unclear photos or possible issues go to buyer review.",
        parse_mode='Markdown'
    )

    await context.bot.send_message(
        chat_id=deal['seller_id'],
        text=f"✅ **Funds Secured in Escrow!** (Auth ID: `{auth_id}`)\n"
             f"The buyer has authorized the payment hold. It is now safe to fulfill the agreed terms.\n\n"
             f"📨 **Action Required — Send this one-time code to the buyer in the deal chat.** After the order arrives, the buyer must send a clear photo of the actual received item with the code visible. The agent checks the product against the deal terms and looks for visible defects before releasing payment; possible issues require buyer review.\n\n"
             f"👉 **`{deal['proof_code']}`** 👈\n\n"
             f"Do not upload the proof photo yourself; the buyer submits it after receiving the order.",
        parse_mode='Markdown'
    )