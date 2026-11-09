# DealBridge

## Inspiration

Informal online deals often rely on trust and scattered messages. We wanted to make those transactions clearer and safer for both sides.

## What it does

DealBridge turns Telegram conversations into structured deal terms, flags potential scam signals, and helps verify delivery proof. PayPal authorization can hold funds while the agreed deal checks are completed.

When a buyer opens a listing link, they can ask the AI about the product or describe their needs. The assistant uses the seller's description, current deal terms, and recent buyer-seller chat to answer accurately, explain which listed details match those needs, and call out information that is not specified.

## How we built it

We built a Telegram bot and FastAPI service in Python, using Groq-powered AI for deal assistance, PayPal APIs for payment authorization, and Elasticsearch to store deal data.

## Challenges we ran into

Keeping deal terms consistent as conversations evolve, coordinating payment and deal states, and making the experience work smoothly across Telegram and PayPal.

## Accomplishments that we're proud of

We brought deal creation, AI assistance, delivery proof, and PayPal payment authorization together in one prototype.

## What we learned

AI can help people make better-informed decisions, but payment actions need clear rules and explicit human approval.

## What's next for DealBridge

Add dispute handling, live price checks, a judge-friendly dashboard, and a hosted demo with a smoother onboarding experience.
