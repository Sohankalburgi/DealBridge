import unittest
from unittest.mock import AsyncMock, patch

import agent
from database import deals_db
from handlers import (
    handle_message,
    handle_marketplace_purchase_intent,
    _is_terms_confirmation_message,
    _marketplace_purchase_intent,
    _purchase_product_query,
    _recent_deal_chat_context,
    _restore_active_deal_context,
)


class BuyerQuestionContextTests(unittest.IsolatedAsyncioTestCase):
    def test_buyer_qa_prompt_explains_cod_and_paypal_authorization_accurately(self):
        prompt = agent.buyer_qa_prompt.format(
            message="Is COD available?",
            terms="Item: headphones",
            product_description="Wireless headphones.",
            chat_context="",
            role="Buyer",
        )

        self.assertIn("do not claim it is available unless the seller explicitly confirmed it", prompt)
        self.assertIn("DealBridge's checkout uses PayPal authorization, not COD", prompt)
        self.assertIn("funds are captured for the seller only after delivery proof is verified", prompt)
        self.assertIn("A chat message confirming the deal does not authorize or charge the buyer", prompt)

    def test_explicit_purchase_phrases_confirm_joined_deal_terms(self):
        for message in (
            "I wanted to buy",
            "I want to buy this!",
            "Deal done.",
            "Order confirmed",
            "I confirm the order",
        ):
            with self.subTest(message=message):
                self.assertTrue(_is_terms_confirmation_message(message))

    def test_non_confirmation_does_not_confirm_terms(self):
        self.assertFalse(_is_terms_confirmation_message("Is the order confirmed?"))
        self.assertFalse(_is_terms_confirmation_message("Order confirmed?"))
        self.assertFalse(_is_terms_confirmation_message("I have a question about the deal"))

    def test_purchase_intent_uses_previous_listing_context_or_product_name(self):
        contextual_intent = _marketplace_purchase_intent(
            "Ok I want to buy this product"
        )
        self.assertIsNotNone(contextual_intent)
        self.assertEqual(_purchase_product_query(contextual_intent), "")

        named_intent = _marketplace_purchase_intent("I wanted to buy a keychain")
        self.assertIsNotNone(named_intent)
        self.assertEqual(_purchase_product_query(named_intent), "keychain")

        self.assertIsNone(_marketplace_purchase_intent("Is this available?"))

    def test_purchase_confirmation_phrases_accept_conversational_wrappers(self):
        self.assertTrue(_is_terms_confirmation_message("Ok I want to buy this product"))
        self.assertTrue(_is_terms_confirmation_message("Yes, I wanted to buy this item."))

    def test_active_joined_deal_is_restored_after_user_context_is_lost(self):
        context = type("Context", (), {"user_data": {}})()
        joined_deal = {
            "deal_id": "keychain-1",
            "buyer_id": 42,
            "seller_id": 84,
            "state": "JOINED",
            "terms": "Item: keychain",
        }

        with patch("handlers.deals_db.list_for_user", return_value=[joined_deal]):
            deal_id, deal = _restore_active_deal_context(42, context)

        self.assertEqual(deal_id, "keychain-1")
        self.assertIs(deal, joined_deal)
        self.assertEqual(context.user_data["active_deal"], "keychain-1")
        self.assertIsNone(context.user_data["state"])

    def test_active_deal_is_not_guessed_when_multiple_orders_are_open(self):
        context = type("Context", (), {"user_data": {}})()
        deals = [
            {"deal_id": deal_id, "buyer_id": 42, "seller_id": 84, "state": "JOINED"}
            for deal_id in ("keychain-1", "phone-2")
        ]

        with patch("handlers.deals_db.list_for_user", return_value=deals):
            deal_id, deal = _restore_active_deal_context(42, context)

        self.assertIsNone(deal_id)
        self.assertIsNone(deal)
        self.assertNotIn("active_deal", context.user_data)

    async def test_purchase_message_restores_joined_deal_in_message_handler(self):
        user = type("User", (), {"id": 42, "full_name": "Buyer", "username": "buyer"})()
        message = type(
            "Message",
            (),
            {"text": "Ok I want to buy this product", "reply_text": AsyncMock()},
        )()
        update = type(
            "Update",
            (),
            {
                "message": message,
                "effective_user": user,
            },
        )()
        context = type(
            "Context",
            (),
            {
                "user_data": {},
                "bot": type("Bot", (), {"send_message": AsyncMock()})(),
            },
        )()
        deal = {
            "deal_id": "keychain-1",
            "buyer_id": user.id,
            "seller_id": 84,
            "buyer_name": "Buyer",
            "seller_name": "Seller",
            "state": "JOINED",
            "terms": "Item: keychain",
            "seller_confirmed_terms": True,
            "buyer_confirmed_terms": False,
        }

        with (
            patch("handlers.handle_buyer_shipping_address", new=AsyncMock(return_value=False)),
            patch("handlers.deals_db.list_for_user", return_value=[deal]),
            patch("handlers.deals_db.append_conversation"),
            patch.object(type(deals_db), "__contains__", return_value=True),
            patch.object(type(deals_db), "__getitem__", return_value=deal),
            patch("handlers.classify_product_search_intent", new=AsyncMock()) as search_intent,
            patch("handlers.classify_chat_intent", new=AsyncMock()) as chat_intent,
            patch("handlers.check_message_for_scams", new=AsyncMock(return_value="Risk Level: LOW\nReason: Looks safe")),
            patch("handlers.trigger_user_confirmation", new=AsyncMock()) as confirm_terms,
        ):
            await handle_message(update, context)

        self.assertEqual(context.user_data["active_deal"], "keychain-1")
        confirm_terms.assert_awaited_once_with(context, "keychain-1", user.id)
        search_intent.assert_not_awaited()
        chat_intent.assert_not_awaited()

    async def test_purchase_intent_joins_the_recently_searched_listing(self):
        context = type(
            "Context",
            (),
            {"user_data": {"marketplace_search_query": "keychain", "marketplace_search_results": ["deal-1"]}},
        )()
        update = type("Update", (), {})()
        purchase_intent = _marketplace_purchase_intent("I want to buy this product")

        with patch("handlers.join_marketplace_listing", new_callable=AsyncMock) as join_listing:
            handled = await handle_marketplace_purchase_intent(
                update, context, purchase_intent
            )

        self.assertTrue(handled)
        join_listing.assert_awaited_once_with(update, context, "deal-1")

    async def test_named_purchase_search_does_not_join_a_stale_search_result(self):
        context = type(
            "Context",
            (),
            {"user_data": {"marketplace_search_query": "keychain", "marketplace_search_results": ["keychain-deal"]}},
        )()
        update = type("Update", (), {})()
        purchase_intent = _marketplace_purchase_intent("I want to buy a phone")

        async def search_for_phone(_update, search_context, query, show_results):
            self.assertEqual(query, "phone")
            self.assertFalse(show_results)
            search_context.user_data["marketplace_search_query"] = "phone"
            search_context.user_data["marketplace_search_results"] = ["phone-deal"]

        with (
            patch("handlers.search_marketplace_products", new=search_for_phone),
            patch("handlers.join_marketplace_listing", new_callable=AsyncMock) as join_listing,
        ):
            handled = await handle_marketplace_purchase_intent(
                update, context, purchase_intent
            )

        self.assertTrue(handled)
        join_listing.assert_awaited_once_with(update, context, "phone-deal")

    async def test_answer_uses_listing_and_recent_chat_context(self):
        chain = AsyncMock()
        chain.ainvoke.return_value = "The seller confirmed it supports Bluetooth."

        with patch("agent.buyer_qa_chain", chain):
            answer = await agent.answer_participant_question(
                "Does it support Bluetooth?",
                "Item: headphones",
                "Buyer",
                "Wireless headphones, model X.",
                "Seller: Yes, it supports Bluetooth.",
            )

        self.assertIn("Bluetooth", answer)
        chain.ainvoke.assert_awaited_once_with(
            {
                "message": "Does it support Bluetooth?",
                "terms": "Item: headphones",
                "role": "Buyer",
                "product_description": "Wireless headphones, model X.",
                "chat_context": "Seller: Yes, it supports Bluetooth.",
            }
        )

    async def test_answer_reports_ai_unavailable_instead_of_silently_forwarding(self):
        chain = AsyncMock()
        chain.ainvoke.side_effect = RuntimeError("AI unavailable")

        with patch("agent.buyer_qa_chain", chain):
            answer = await agent.answer_participant_question(
                "What is the battery life?",
                "Item: headphones",
                "Buyer",
            )

        self.assertIn("couldn't generate an AI answer", answer)

    def test_chat_context_uses_recent_messages_and_limits_each_message(self):
        conversation = [
            {"sender_role": "Buyer", "text": f"Message {index}"}
            for index in range(13)
        ]
        conversation.append({"sender_role": "Seller", "text": "x" * 1200})

        context = _recent_deal_chat_context({"conversation": conversation})

        self.assertNotIn("Buyer: Message 1\n", context)
        self.assertIn("Message 2", context)
        self.assertIn("Message 12", context)
        self.assertEqual(len(context.splitlines()[-1].split(": ", 1)[1]), 1000)


if __name__ == "__main__":
    unittest.main()
