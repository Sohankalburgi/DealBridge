# DealBridge

## Inspiration

Informal online deals often rely on trust and scattered messages. We wanted to make those transactions clearer and safer for both sides.

## What it does

DealBridge turns informal Telegram negotiations into structured, safer deals. It uses AI to draft and update deal terms, highlights scam risk signals, and helps confirm delivery proof before a transaction is considered complete.

Key features include:

- AI deal copilot: turns chat into a clear deal summary with pricing, quantity, timeline, delivery expectations, and buyer/seller responsibilities.
- Scam signal detection: flags risky language, urgency pressure, suspicious payment instructions, and mismatches between the listing and the chat.
- Needs-based product guidance: when a buyer opens a listing, they can ask the AI to match the product against their requirements and point out missing or unclear details.
- Delivery proof review: helps both parties compare uploaded proof, shipping details, and agreed conditions before releasing funds or marking the deal complete.
- PayPal Sandbox authorization: enables a held-funds flow so the transaction can pause while the deal checks finish, without treating the prototype as a regulated escrow service.
- Human-in-the-loop safety checks: every sensitive action, including payment authorization and dispute progression, is designed to require clear confirmation from both sides.

## How we built it

We built a Telegram-first experience with a Python FastAPI backend, Groq-powered AI for deal understanding and drafting, PayPal APIs for sandbox payment authorization, and Elasticsearch for structured deal data and retrieval. The system coordinates chat context, deal status, risk checks, and proof verification into one workflow.

## Challenges we ran into

Keeping deal terms consistent as conversations evolve, coordinating payment and deal states, and making the experience work smoothly across Telegram and PayPal.

## Accomplishments that we're proud of

We brought together AI-assisted deal drafting, scam-risk detection, delivery-proof review, and PayPal Sandbox authorization in one end-to-end prototype. We also made the workflow feel natural inside Telegram, where buyers and sellers already negotiate.

## What we learned

AI can make informal deals easier to understand and safer to evaluate, but trust-sensitive actions like payment holds and dispute resolution need explicit review, transparent rules, and human approval.

## What's next for DealBridge

Add stronger dispute workflows, live product-price validation, a more detailed verification dashboard for deal evidence, and a smoother hosted demo onboarding flow for real-world testing.
