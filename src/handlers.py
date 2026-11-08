import base64
import os
import re
import uuid
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import ContextTypes
from config import logger
from database import deals_db
from agent import (
    analyze_product_with_ai, 
    check_message_for_scams, 
    answer_buyer_question, 
    update_terms_from_chat,
    detect_confirmation_intent,
    verify_proof_photo  # NEW
)
from paypal import (
    create_authorize_order, 
    capture_authorization, 
    void_authorization, 
    verify_and_authorize_deal
)

from decimal import Decimal, ROUND_HALF_UP


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


async def trigger_user_confirmation(context: ContextTypes.DEFAULT_TYPE, deal_id: str, user_id: int) -> None:
    """Shared logic to confirm terms via button OR chat text and trigger PayPal."""
    deal = deals_db.get(deal_id)
    if not deal or deal['state'] in (
        'WAITING_FOR_PAYMENT',
        'FUNDS_HELD',
        'PAYMENT_CREATING'
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

    # If both parties are confirmed, trigger the PayPal Hold Link
    if deal['seller_confirmed_terms'] and deal['buyer_confirmed_terms']:
        deal['state'] = 'PAYMENT_CREATING'
        try:
            original_amount, original_currency = extract_price_and_currency(deal['terms'])
            paypal_amount, paypal_currency = prepare_paypal_amount(
                original_amount,
                original_currency
            )
            if paypal_amount <= 0:
                raise ValueError("The deal price must be greater than zero.")

            amount = f"{paypal_amount:.2f}"
            currency = paypal_currency
            await context.bot.send_message(
                chat_id=deal['seller_id'],
                text=f"🎉 **Deal Confirmed ({amount} {currency})!**\nGenerating the secure PayPal hold link for the buyer...",
                parse_mode='Markdown'
            )

            buyer_msg = await context.bot.send_message(
                chat_id=deal['buyer_id'],
                text=f"🎉 **Deal Confirmed ({amount} {currency})!**\nGenerating your secure PayPal hold link...",
                parse_mode='Markdown'
            )

            approval_url = await create_authorize_order(deal_id=deal_id, amount=amount, currency=currency)
            if not approval_url:
                raise ValueError("PayPal did not return an approval URL.")
            
            pay_keyboard = [
                [InlineKeyboardButton(f"💳 1. Approve {amount} {currency} on PayPal", url=approval_url)],
                [InlineKeyboardButton("✅ 2. I've Approved — Verify Now", callback_data=f"verifypay_{deal_id}")]
            ]
            pay_markup = InlineKeyboardMarkup(pay_keyboard)
            
            await buyer_msg.edit_text(
                f"🔒 **Action Required:**\nPlease authorize the **{amount} {currency}** payment hold on PayPal. No money will be captured until delivery is proven.",
                reply_markup=pay_markup,
                parse_mode='Markdown'
            )
            
            deal['state'] = 'WAITING_FOR_PAYMENT'
            
        except Exception as e:
            logger.error(f"PayPal Error: {e}")
            deal['state'] = 'JOINED'
            error_text = (
                "❌ Could not generate the PayPal payment link. "
                "Please check the deal price/currency and PayPal configuration, then confirm the terms again."
            )
            await context.bot.send_message(chat_id=deal['seller_id'], text=error_text)
            await context.bot.send_message(chat_id=deal['buyer_id'], text=error_text)

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

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handles standard onboarding and deep-linked deal joining."""
    user = update.effective_user
    args = context.args 

    if args and args[0].startswith("deal_"):
        deal_id = args[0].replace("deal_", "")
        
        if deal_id in deals_db:
            deals_db[deal_id]['buyer_id'] = user.id
            deals_db[deal_id]['state'] = 'JOINED'
            context.user_data['active_deal'] = deal_id
            
            await update.message.reply_text(
                f"Welcome {user.first_name}! You've securely joined deal `{deal_id}` as the buyer.\n\n"
                "Connecting you to the seller's chat... You can now type messages here to talk to them."
            )
            
            seller_id = deals_db[deal_id]['seller_id']
            await context.bot.send_message(
                chat_id=seller_id,
                text="🔔 **Your buyer has joined the chat!**\nAny messages you type here will now be securely forwarded to them.",
                parse_mode='Markdown'
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
        "Send /newdeal to draft a new secure transaction.",
        parse_mode='HTML'
    )

async def newdeal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Triggered by /newdeal command from the seller."""
    context.user_data['state'] = 'WAITING_FOR_DEAL_TERMS'
    context.user_data['draft_terms'] = "None yet."
    context.user_data['draft_inputs'] = []
    await update.message.reply_text(
        "Let's set up a new deal. Please describe the item, price, currency, and expected delivery time."
    )

async def generate_and_send_draft(update: Update, context: ContextTypes.DEFAULT_TYPE, new_input: str) -> None:
    """Runs the AI extraction, merging cumulative seller inputs with the existing draft."""
    message = update.message if update.message else update.callback_query.message
    processing_msg = await message.reply_text("🤖 AI is structuring your deal terms...")
    
    if 'draft_inputs' not in context.user_data:
        context.user_data['draft_inputs'] = []
    context.user_data['draft_inputs'].append(new_input)
    
    full_seller_notes = "\n".join(
        [f"- Message {i+1}: {msg}" for i, msg in enumerate(context.user_data['draft_inputs'])]
    )
    existing_terms = context.user_data.get('draft_terms', "None yet.")
    
    ai_structured_terms = await analyze_product_with_ai(full_seller_notes, existing_terms)
    
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

async def process_new_deal(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw_terms = update.message.text
    context.user_data['draft_terms'] = "None yet."
    context.user_data['draft_inputs'] = []
    await generate_and_send_draft(update, context, raw_terms)

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
        join_link = f"https://t.me/{bot_username}?start=deal_{short_id}"
        
        deals_db[short_id] = {
            'seller_id': update.effective_user.id,
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

    # 3. Handle Terms Confirmation (Buyer & Seller)
    if data.startswith("confirm_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if not deal:
            await query.edit_message_text("Deal not found or expired.")
            return
            
        await query.edit_message_text(f"✅ You confirmed the terms.\n\n{deal['terms']}")
        await trigger_user_confirmation(context, deal_id, update.effective_user.id)
        return

    # 4. Handle Buyer Manual Proof Approval (When AI returns ASK_BUYER)
    if data.startswith("release_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if not deal or update.effective_user.id != deal['buyer_id']:
            return

        try:
            capture_id = await capture_authorization(deal.get('authorization_id', 'MOCK_AUTH'))
            deal['capture_id'] = capture_id
            deal['state'] = 'COMPLETED'

            await query.edit_message_caption(
                caption=f"✅ **You approved the proof!** Payment released (Capture ID: `{capture_id}`).",
                parse_mode='Markdown'
            )
            await context.bot.send_message(
                chat_id=deal['seller_id'],
                text=f"🎉 **Buyer approved your proof!** Funds have been captured (`{capture_id}`). Deal completed!",
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Manual release capture failed: {e}")
            await context.bot.send_message(
                chat_id=deal['buyer_id'],
                text="❌ Error capturing payment with PayPal. Please try again."
            )
        return

    # 5. Handle Buyer Proof Dispute (When AI returns ASK_BUYER)
    if data.startswith("dispute_"):
        deal_id = data.split("_")[1]
        deal = deals_db.get(deal_id)
        if not deal or update.effective_user.id != deal['buyer_id']:
            return

        deal['state'] = 'DISPUTED'
        await query.edit_message_caption(
            caption="🚨 **Dispute Opened.** Your funds remain safely held and have NOT been captured.",
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
    current_state = context.user_data.get('state')

    if current_state == 'WAITING_FOR_DEAL_TERMS':
        await process_new_deal(update, context)
        return
        
    if current_state == 'WAITING_FOR_DEAL_UPDATES':
        await process_deal_update(update, context)
        return

    active_deal_id = context.user_data.get('active_deal')
    
    if active_deal_id and active_deal_id in deals_db:
        deal = deals_db[active_deal_id]
        user_id = update.effective_user.id
        message = update.message
        if message is None:
            logger.warning("Ignoring a text update without a message.")
            return
        if message.text is None:
            await message.reply_text("Please send a text message.")
            return
        text = message.text
        
        if user_id == deal['seller_id']:
            sender_role, recipient_id = "Seller", deal['buyer_id']
        elif user_id == deal['buyer_id']:
            sender_role, recipient_id = "Buyer", deal['seller_id']
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

        # Deterministic confirmation detection
        normalized_text = re.sub(
            r"[.!?,]+$",
            "",
            text.lower().strip()
        )

        confirmation_phrases = {
            "i want to buy",
            "confirm",
            "i confirm",
            "i agree",
            "i agree to the terms",
            "let's do it",
            "deal",
            "ready to pay",
            "proceed to payment"
        }

        is_confirming = normalized_text in confirmation_phrases

        # Use AI only for other possible confirmation messages
        if not is_confirming:
            is_confirming = await detect_confirmation_intent(text)

        # 2. Intercept Buyer Factual Questions
        if sender_role == "Buyer" and not is_confirming:
            ai_answer = await answer_buyer_question(text, deal['terms'])
            if ai_answer.strip() != "FORWARD_TO_SELLER":
                terms_changed = await update_deal_terms_from_message(
                    context,
                    active_deal_id,
                    deal,
                    sender_role,
                    text
                )
                if terms_changed:
                    return
                await update.message.reply_text(
                    f"🤖 **AI Agent:**\n{ai_answer}", 
                    parse_mode='Markdown'
                )
                return 

        # 3. AI Scam Check & Relay
        scam_analysis = await check_message_for_scams(text)
        warning_label = f"\n\n🚨 **AI Safety Warning:** {scam_analysis}" if "HIGH" in scam_analysis.upper() or "MEDIUM" in scam_analysis.upper() else ""
        relayed_message = f"💬 **{sender_role}:**\n{text}{warning_label}"

        try:
            await context.bot.send_message(chat_id=recipient_id, text=relayed_message, parse_mode='Markdown')
        except Exception as e:
            logger.error(f"Failed to relay message: {e}")
            await update.message.reply_text("❌ Failed to send message. The other user might have blocked the bot.")
            return

        # 4. Auto-refresh terms before acting on a confirmation.
        terms_changed = await update_deal_terms_from_message(
            context,
            active_deal_id,
            deal,
            sender_role,
            text
        )
        if terms_changed:
            return

        if is_confirming:
            logger.info(
                f"Buyer/seller confirmation detected: "
                f"user={user_id}, deal={active_deal_id}"
            )
            await trigger_user_confirmation(context, active_deal_id, user_id)

    else:
        await update.message.reply_text("I am currently waiting for commands. Use /newdeal to start a transaction.")


# --- NEW: PHOTO PROOF HANDLER (Steps 6, 7 & 8) ---
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Processes shipping proof photos sent by the seller."""
    active_deal_id = context.user_data.get('active_deal')
    if not active_deal_id or active_deal_id not in deals_db:
        await update.message.reply_text("Please join or create an active deal first.")
        return

    deal = deals_db[active_deal_id]
    user_id = update.effective_user.id

    if user_id != deal['seller_id']:
        await update.message.reply_text("Only the seller can upload shipping proof photos.")
        return

    if deal['state'] not in ('FUNDS_HELD', 'HOLD_RESUBMIT'):
        await update.message.reply_text(
            f"⏳ Cannot submit proof right now (Current Deal State: `{deal['state']}`). "
            "Wait until the buyer authorizes the payment hold.",
            parse_mode='Markdown'
        )
        return

    status_msg = await update.message.reply_text("🔍 **AI Vision Agent is inspecting your proof photo and verification code...**", parse_mode='Markdown')

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
            f"Please write `{deal.get('proof_code')}` clearly on paper next to the item and send a new photo.",
            parse_mode='Markdown'
        )

    # --- OUTCOME 2: ASK_BUYER (Ambiguous item match or fail-closed) ---
    elif decision == "ASK_BUYER":
        deal['state'] = 'NEEDS_BUYER_REVIEW'
        await status_msg.edit_text(
            f"⚠️ **Proof Submitted for Buyer Review**\n\n"
            f"**AI Notes:** {verdict['reason']}\n"
            f"We've forwarded the photo to the buyer for final confirmation.",
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
                f"📦 **Seller Uploaded Shipping Proof**\n\n"
                f"**AI Verification:** {verdict['reason']}\n"
                f"**Tracking:** `{verdict['tracking']}`\n\n"
                f"Do you approve releasing the held funds to the seller?"
            ),
            reply_markup=InlineKeyboardMarkup(review_keyboard),
            parse_mode='Markdown'
        )

    # --- OUTCOME 3: RELEASE (All checks passed -> Capture PayPal Hold) ---
    elif decision == "RELEASE":
        try:
            capture_id = await capture_authorization(deal.get('authorization_id', 'MOCK_AUTH'))
            deal['capture_id'] = capture_id
            deal['state'] = 'COMPLETED'

            await status_msg.edit_text(
                f"✅ **Proof Verified & Payment Released!**\n\n"
                f"**AI Verdict:** {verdict['reason']}\n"
                f"**PayPal Capture ID:** `{capture_id}`\n\n"
                f"The funds have been transferred to your account!",
                parse_mode='Markdown'
            )

            await context.bot.send_photo(
                chat_id=deal['buyer_id'],
                photo=update.message.photo[-1].file_id,
                caption=(
                    f"✅ **Shipping Proof Verified — Deal Completed!**\n\n"
                    f"**AI Verdict:** {verdict['reason']}\n"
                    f"**Tracking:** `{verdict['tracking']}`\n"
                    f"**PayPal Capture ID:** `{capture_id}`\n\n"
                    f"Your payment hold has been released to the seller."
                ),
                parse_mode='Markdown'
            )
        except Exception as e:
            logger.error(f"Capture failed: {e}")
            await status_msg.edit_text("❌ AI verified the photo, but PayPal capture failed. Moving to manual review.")

async def complete_payment_hold_notifications(context: ContextTypes.DEFAULT_TYPE, deal_id: str, auth_id: str) -> None:
    """Marks the deal as FUNDS_HELD and sends the proof code to the seller."""
    deal = deals_db.get(deal_id)
    if not deal or deal.get('state') == 'FUNDS_HELD':
        return

    deal['state'] = 'FUNDS_HELD'
    deal['authorization_id'] = auth_id
    deal['proof_code'] = str(uuid.uuid4())[:6].upper()

    await context.bot.send_message(
        chat_id=deal['buyer_id'],
        text=f"✅ **Payment Hold Secured!** (Auth ID: `{auth_id}`)\n"
             f"Your funds are safely held in escrow. The seller has been instructed to ship the item and upload photo proof.",
        parse_mode='Markdown'
    )

    await context.bot.send_message(
        chat_id=deal['seller_id'],
        text=f"✅ **Funds Secured in Escrow!** (Auth ID: `{auth_id}`)\n"
             f"The buyer has authorized the payment hold. It is now safe to ship the item.\n\n"
             f"📸 **Action Required — Submit Proof:**\n"
             f"Write this one-time verification code on a piece of paper next to the item:\n\n"
             f"👉 **`{deal['proof_code']}`** 👈\n\n"
             f"Send the photo right here in the chat along with the tracking number in the caption!",
        parse_mode='Markdown'
    )