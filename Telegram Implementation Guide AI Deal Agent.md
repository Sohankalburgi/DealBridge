# Telegram Implementation Guide: AI Deal Agent

Complete build workflow for the PayPal AI Hackathon submission

## 1. What gets built

A Telegram bot that sits between a buyer and a seller in an informal deal. The bot relays their chat, turns it into confirmed terms, holds the buyer's money with a PayPal authorization, checks the seller's delivery proof with AI, and then captures or voids the payment.

Workflow in one line: seller creates a deal, buyer joins by link, agent drafts terms, buyer approves a PayPal hold, seller ships with proof, agent verifies, and the payment is released or stays held.

How this meets the hackathon requirements:

- Meaningful PayPal integration, built on the free sandbox: authorize, capture, void, refund, webhooks.
- Meaningful AI: terms extraction, scam-signal detection, price sanity check, and a vision-based proof check.
- A working prototype with a hosted demo URL or run instructions, a public open-source repo with a visible license, a text description, a tools list, and a demo video under 3 minutes.

## 2. Tool stack and where each hackathon tool fits

| Tool | Role in the build | Status |
| --- | --- | --- |
| Telegram Bot API (grammY for TypeScript, or python-telegram-bot) | The user interface: onboarding, chat relay, buttons, photo upload | Core |
| PayPal REST APIs (sandbox) | Orders with intent AUTHORIZE, authorize, capture, void, refund, tracking, webhooks | Core |
| PayPal Agent Toolkit or MCP server (@paypal/mcp, sandbox environment) | Gives the agent PayPal tools such as get\_order and create\_order, which supports the Agentic Commerce angle. Money-moving steps stay in your own PayPal service | Recommended |
| A vision-capable LLM (Claude API or any model you prefer) | Terms extraction, scam signals, proof photo verification. Any AI tool is allowed | Core |
| Channel3 | Product search with live prices, used as a market price check that flags suspiciously cheap listings | Recommended (sponsor prize category) |
| Render | Hosts the web service, background worker and Elasticsearch, and gives you the hosted demo URL | Recommended (sponsor prize category) |
| AG Grid | Admin and judge dashboard: deals, states, verdicts, event log | Recommended (sponsor prize category) |
| APIMatic Context Plugin | Gives your coding assistant accurate PayPal API context while building | Recommended (sponsor prize category) |
| Postman | API collection for every endpoint, plus PayPal sandbox request testing and docs | Optional |
| Zapier | No-code hook, for example log completed deals to a sheet or send an email when a dispute opens | Optional |
| Elasticsearch | Persists deal details, conversations, terms/acceptances, and order progress | Core |
| KERNEL, Astropods, Bryntum | Not verified for this build. Read each sponsor page and use one only if it fits naturally | Skip unless useful |

Only the sponsor tools with a named prize are listed as prize categories above. Confirm the current list on the Devpost page before committing time to any of them.

## 3. Architecture

```
Telegram users (buyer, seller)
        |  Bot API webhook
        v
+-----------------------------+
| Bot service                 |
+-----------------------------+
   |             |            |
   v             v            v
Deal service   Agent service  PayPal service
(state         (LLM, vision,  (Orders, authorize,
 machine)       Channel3)      capture, void, refund)
   |             |            |
   +-------------+------------+
                 |
           Elasticsearch <---- Worker (timers, expiry)
                 ^
                 |
          Web dashboard (AG Grid)

PayPal webhooks ---> /webhooks/paypal
```

Design principles:

- Every order-state transition is persisted with its previous state and timestamp.
- The agent suggests, and deterministic code decides money movement.
- Fail closed: if anything times out or errors, the hold is kept or voided, never captured.

## 4. Telegram design

### 4.1 Setup

1. Create the bot with BotFather and keep the token in an environment variable.
2. Register a webhook URL with a secret token, and reject any request that does not carry that secret in the Telegram secret-token header.
3. Use grammY (TypeScript) or python-telegram-bot as the framework.

Two platform limits shape the design:

- A bot cannot message someone who has not started it, so both buyer and seller must open the bot first.
- A bot cannot create a group chat, so a shared group is not automatic.

### 4.2 Relay model (recommended)

Each deal has two private chats: buyer with bot, and seller with bot. The bot forwards messages between them, labeled with the sender's role, and passes every message through the agent for terms and scam checks. This avoids group privacy-mode problems and gives the agent a view of the whole conversation.

A group mode, where the bot is added to a shared chat the users create, is a possible later extension.

### 4.3 Commands and buttons

| Input | Who | What it does |
| --- | --- | --- |
| /start | Both | Onboarding. With a deal payload, joins that deal |
| /newdeal | Seller | Asks for item, price, currency and expected delivery time, then returns a join link |
| /status | Both | Shows state, terms, and next action |
| /proof | Seller | Starts proof upload (or just send a photo while the deal is in the shipping state) |
| /cancel | Both | Cancels the deal where the state allows it |
| Inline buttons | Both | Confirm terms, Edit terms, Approve payment (URL button to PayPal), Confirm received, Report a problem |

Join link format: `https://t.me/<bot_username>?start=deal_<short_id>`. The start payload allows only letters, digits, underscore and dash, up to 64 characters, so use a short random ID.

### 4.4 Security

- Check the webhook secret token on every request.
- Map each Telegram user ID to a role in a deal, and ignore anyone who is not a participant.
- Use random, unguessable deal IDs, and never put secrets in deep links.
- Verify button callbacks on the server: check the user, the deal, and that the state still allows that action.
- Rate-limit by user.

## 5. End-to-end workflow

### Step 1: Seller creates the deal

- Telegram: the seller sends /newdeal and answers the bot's questions: item, price, currency, expected delivery time.
- Backend: create the deal (state CREATED), generate the short ID, return the join link.
- Agent: if the seller describes the item in free text, the LLM turns it into structured fields.

### Step 2: Buyer joins

- Telegram: the seller shares the link in their Instagram or WhatsApp chat. The buyer taps it, opens the bot, and sees the draft deal.
- Backend: attach the buyer's Telegram ID to the deal (state JOINED).

### Step 3: Terms and checks

- Telegram: buyer and seller talk through the bot. The agent posts a terms card that both must confirm with a button.
- Agent: extracts terms from the chat (item, attributes, price, currency, delivery deadline). Runs the Channel3 market price check and the scam-signal check. If risk is high, the buyer sees a warning before any money is held.
- Backend: when both confirm, freeze the terms (state TERMS\_CONFIRMED) and generate a one-time proof code, a short random string the seller will have to show in the delivery photo.

### Step 4: Buyer approves the hold

- Backend: create a PayPal order with intent AUTHORIZE, the seller's sandbox business account as payee, and the deal ID in the custom ID field.
- Telegram: send the buyer an Approve payment button that opens the PayPal approval link.
- Buyer: logs in with a sandbox personal account and approves.

### Step 5: Funds secured

- Backend: on return from PayPal, authorize the order. Store the authorization ID and set state FUNDS\_HELD. A PayPal webhook confirms the same event.
- Telegram: tell the seller that funds are secured and it is safe to ship, and remind them of the proof code and deadline. Tell the buyer the hold is in place and nothing has been charged.

### Step 6: Seller ships and submits proof

- Telegram: the seller sends a photo of the item with the one-time code written on paper beside it, plus a tracking number.
- Backend: store the photo, record the tracking number, and attach tracking to the PayPal order (check the current docs for the exact endpoint). State becomes PROOF\_SUBMITTED.

### Step 7: AI verification

- Agent: checks the code is visible and correct, checks the item matches the confirmed terms, and checks tracking status. See section 9 for the checks and the combiner.
- Backend: stores a verdict with reasons.

### Step 8: Outcome

| Verdict | What happens |
| --- | --- |
| RELEASE | Capture the authorization. The seller is paid. Both users are notified. State COMPLETED |
| ASK\_BUYER | The buyer sees the evidence and the agent's reasons, and chooses Confirm received or Report a problem |
| HOLD | The code is missing or wrong, or tracking looks bad. The seller is asked to resubmit. The money stays held |

### Step 9: Buyer confirmation window

After delivery, the buyer has a fixed window to confirm or report a problem. Confirm leads to capture. Report a problem keeps the hold and moves the deal to DISPUTED for manual review in the dashboard. If the window passes in silence on a clean verdict, the worker captures automatically.

### Step 10: Timeouts and cancellations

The worker handles timers: no buyer approval, no shipment, no proof, or a hold nearing expiry. Each case voids the authorization and tells both users. If a capture has already happened and a problem is confirmed, refund the capture.

## 6. State machine

```
CREATED -> JOINED -> TERMS_CONFIRMED -> FUNDS_HELD -> PROOF_SUBMITTED

PROOF_SUBMITTED -> RELEASED -> COMPLETED
PROOF_SUBMITTED -> NEEDS_BUYER_REVIEW -> RELEASED | DISPUTED
PROOF_SUBMITTED -> HOLD_RESUBMIT -> PROOF_SUBMITTED

DISPUTED -> RELEASED | VOIDED | REFUNDED   (manual decision in dashboard)

(any pre-capture state) -> CANCELLED | EXPIRED -> VOIDED
COMPLETED -> REFUNDED   (if a problem is confirmed after capture)
```

Every transition is validated: a button press or webhook that does not match the current state is ignored and logged.

## 7. Data model

| Elasticsearch document | Key fields |
| --- | --- |
| deals | deal\_id, seller\_id, buyer\_id, state, raw\_description, product\_details, terms, terms\_history, terms\_agreements, conversation, order\_steps, PayPal IDs, proof code, and latest verdict |

Set `ELASTICSEARCH_URL` and, when authentication is enabled, either
`ELASTICSEARCH_API_KEY` or both `ELASTICSEARCH_USERNAME` and
`ELASTICSEARCH_PASSWORD`. `ELASTICSEARCH_INDEX` defaults to `deals`. Each deal
document keeps its product details, complete text conversation, versioned terms
and recorded participant acceptances, plus a timestamped history of order states.
The local Elasticsearch and Kibana containers use username/password
authentication over localhost-only HTTP; keep them bound to localhost. Use TLS
before exposing either service to another machine or network.

## 8. PayPal integration

### 8.1 Sandbox setup

1. Create a PayPal Developer account.
2. In the sandbox, create one business account (the seller stand-in) and two or more personal accounts (buyers).
3. Create a REST app and copy the client ID and secret.
4. Register your Render URL as the webhook endpoint for the authorization, capture and refund events.

### 8.2 Calls used

| Step | PayPal operation |
| --- | --- |
| Create the hold | Create order with intent AUTHORIZE, a single purchase unit with amount, payee set to the seller business account, custom ID set to the deal ID, return and cancel URLs |
| Buyer approval | Redirect the buyer to the approval link returned in the order response |
| Place the hold | Authorize the order after the buyer returns |
| Release funds | Capture the authorization |
| Cancel the hold | Void the authorization |
| Post-capture fix | Refund the capture |
| Delivery evidence | Add tracking information to the order (confirm the endpoint in the docs) |
| Event confirmation | Webhooks: authorization created and voided, capture completed, refund completed |

Implementation rules:

- Send a unique request ID header on every create, capture, void and refund call, so a retry can never double-process.
- Verify the signature of every incoming PayPal webhook using PayPal's verification endpoint, and handle each event idempotently by event ID.
- Re-read the order or authorization from PayPal before any capture, so decisions are made on current state.

### 8.3 Agent Toolkit and MCP

PayPal's Agent Toolkit and MCP server expose tools such as create\_order, get\_order and capture\_order, and run against the sandbox with an access token. Use them for the agent's read and create steps so the agentic-commerce story is visible. The toolkit's tool list does not clearly include authorize or void, so call the REST API directly for those, and keep every capture, void and refund behind your own policy code rather than letting the LLM call them freely. Check whether the toolkit's create\_order supports the AUTHORIZE intent; if not, create the order through REST.

### 8.4 Known limits to design around

- An authorization guarantees funds for only a limited window, typically around 3 days for most payment sources, so confirm the current honor period and set shipping and proof deadlines well inside it.
- Real multi-seller payouts need PayPal's marketplace or platform products. For the hackathon, use one sandbox business account as the payee and say so in the submission.
- The buyer must approve each order. A vaulted payment method may remove that step; check sandbox support before relying on it.

## 9. AI components

The rule across all four: the AI produces structured, explainable output, and deterministic code makes the final call.

### 9.1 Terms extraction

Input: the relayed chat and the seller's item description. Output fields:

| Field | Notes |
| --- | --- |
| item | Short name and category |
| attributes | Model, size, color, condition, accessories included |
| price and currency | Must match what both sides confirm |
| delivery deadline | Absolute date |
| open questions | Anything ambiguous the agent should ask about |

The agent proposes. Both people confirm with a button. Nothing is frozen until both confirm.

### 9.2 Market price check (Channel3)

Search the item through the Channel3 API or MCP and compare the listed price to typical offers. Flag a price far below the typical range as a scam signal. Use it only as a warning, never as a block, because used and one-off items vary widely.

### 9.3 Scam signals

Run on each relayed message, with a combination of rules and the LLM:

- A request to pay outside the protected flow, or by an unusual method
- Pressure and urgency
- A price far below market
- Refusal to provide proof or a tracking number
- A very new or empty seller profile

Output is a risk level and short reasons. A high level triggers a warning to the buyer before the hold step.

### 9.4 Proof verification

Inputs: confirmed terms, the one-time code, the proof photo and the tracking number. Checks:

1. Code check: the vision model reads the code in the photo, and the code compares it to the stored value in plain code.
2. Item check: does the photo match the confirmed item and attributes, such as model, color and visible condition?
3. Tracking check: is a tracking number present, and what is its status? In the demo, use a mock carrier status endpoint.

Deterministic combiner:

| Condition | Decision |
| --- | --- |
| Code missing or wrong | HOLD, ask the seller to resubmit |
| Item mismatch or low confidence | ASK\_BUYER |
| All checks pass and tracking shows delivered | RELEASE |
| Model error or timeout | ASK\_BUYER (fail closed) |

The photo code is the main defense against a stock photo. Do not claim the AI can detect reused images on its own.

## 10. Repository and configuration

```
/apps/bot            Telegram webhook, commands, buttons, relay
/apps/api            deal service, PayPal service, webhooks, worker
/apps/dashboard      AG Grid admin view
/packages/agent      terms, scam signals, price check, proof verifier
/packages/paypal     REST client, webhook verification
/packages/mock       mock carrier and tracking endpoint
/packages/eval       scripted scenarios and results
/postman             collection and environment
/docs                architecture, setup guide
LICENSE              open-source license at the repo root
render.yaml          services and database definition
```

Environment variables:

- TELEGRAM\_BOT\_TOKEN, TELEGRAM\_WEBHOOK\_SECRET, BOT\_USERNAME
- PAYPAL\_CLIENT\_ID, PAYPAL\_CLIENT\_SECRET, PAYPAL\_ENV=sandbox, PAYPAL\_WEBHOOK\_ID, SELLER\_SANDBOX\_ACCOUNT
- LLM\_API\_KEY and the model name
- CHANNEL3\_API\_KEY
- ELASTICSEARCH\_URL, ELASTICSEARCH\_INDEX, and Elasticsearch credentials
- PUBLIC\_BASE\_URL (your Render URL, used for return URLs and webhooks)

## 11. Deployment and tooling

- Render: one web service (bot, API and dashboard), one background worker for timers, and an Elasticsearch deployment. The public URL doubles as the hosted demo and the webhook target for both Telegram and PayPal.
- Postman: a collection covering deal creation, order creation, authorize, capture, void and refund against the sandbox, plus saved examples for the README and for judges.
- APIMatic: add the context plugin to your coding assistant so generated PayPal calls use the current API shapes.
- AG Grid: dashboard columns for deal ID, state, amount, risk level, verdict, timestamps, with a row detail view showing the event timeline and proof photo.
- Zapier (optional): trigger on a DISPUTED event to send an email or add a row to a sheet.
- Elastic: stores deal documents, conversations, terms/acceptances, and order-state history.

## 12. Test and demo scenarios

| Scenario | What happens | Expected result |
| --- | --- | --- |
| Honest deal | Hold approved, correct photo with code, tracking delivered | RELEASE, seller paid |
| Wrong item | Photo shows a different item | ASK\_BUYER, evidence shown, money held |
| Missing code (stock photo) | Photo without the code | HOLD, seller asked to resubmit |
| Seller vanishes | No shipment before the deadline | Auto-void, buyer protected |
| Suspiciously cheap listing | Price far below Channel3 range | Warning before the hold |
| Off-platform payment request | Seller says to pay directly | Scam warning to buyer |
| Buyer reports a problem | Problem reported after delivery | DISPUTED, hold kept |
| Duplicate button press or webhook | Same event sent twice | Ignored, no double capture |

The eval package runs these scripted cases against the real verifier and reports results, which gives you a measured accuracy claim for the video and README.

## 13. Submission checklist

- Public repo on GitHub, GitLab or Bitbucket with a visible open-source license.
- Clear setup and run instructions, plus a hosted demo URL.
- Text description of features and functionality.
- List of tools used (model, PayPal, Telegram, sponsor tools) and how each is used.
- Demo video under 3 minutes, public on YouTube, showing the project running end to end on the device it was built for.
- No third-party trademarks or copyrighted music in the video without permission. Check the official rules on whether screen recordings of the Telegram and PayPal interfaces are acceptable, and avoid adding logos or music of your own.

## 14. Open items to verify in the docs

- Exact PayPal endpoint and fields for adding tracking to an order.
- Whether the Agent Toolkit create\_order supports the AUTHORIZE intent.
- Whether a vaulted payment method works in the sandbox for pre-approval.
- Current authorization honor period.
- Which sponsor tools carry a named prize this time.
