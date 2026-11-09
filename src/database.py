from copy import deepcopy
from datetime import datetime, timezone
from typing import Any

from elasticsearch import ApiError, ConflictError, Elasticsearch, NotFoundError

from config import (
    ELASTICSEARCH_API_KEY,
    ELASTICSEARCH_INDEX,
    ELASTICSEARCH_PASSWORD,
    ELASTICSEARCH_URL,
    ELASTICSEARCH_USERNAME,
    logger,
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DealDocument(dict[str, Any]):
    """Dictionary-compatible deal document that persists each field update."""

    def __init__(
        self,
        deal_id: str,
        source: dict[str, Any],
        client: Elasticsearch,
        seq_no: int,
        primary_term: int,
    ) -> None:
        super().__init__(source)
        self._deal_id = deal_id
        self._client = client
        self._seq_no = seq_no
        self._primary_term = primary_term

    def __setitem__(self, key: str, value: Any) -> None:
        self.update_fields({key: value})

    def update_fields(self, fields: dict[str, Any]) -> None:
        for attempt in range(3):
            candidate = dict(self)
            previous_state = candidate.get("state")
            previous_terms = candidate.get("terms")
            candidate.update(fields)
            changed_at = _utc_now()

            if "state" in fields and previous_state != candidate["state"]:
                candidate.setdefault("order_steps", []).append(
                    {
                        "from_state": previous_state,
                        "to_state": candidate["state"],
                        "changed_at": changed_at,
                    }
                )

            if "terms" in fields and previous_terms != candidate["terms"]:
                candidate["product_details"] = candidate["terms"]
                history = candidate.setdefault("terms_history", [])
                history.append(
                    {
                        "version": len(history) + 1,
                        "terms": candidate["terms"],
                        "updated_at": changed_at,
                    }
                )

            candidate["updated_at"] = changed_at
            try:
                response = self._client.index(
                    index=ELASTICSEARCH_INDEX,
                    id=self._deal_id,
                    document=candidate,
                    if_seq_no=self._seq_no,
                    if_primary_term=self._primary_term,
                )
            except ConflictError:
                if attempt == 2:
                    raise
                latest = self._client.get(index=ELASTICSEARCH_INDEX, id=self._deal_id)
                dict.clear(self)
                dict.update(self, latest["_source"])
                self._seq_no = latest["_seq_no"]
                self._primary_term = latest["_primary_term"]
                continue

            self._seq_no = response["_seq_no"]
            self._primary_term = response["_primary_term"]
            dict.clear(self)
            dict.update(self, candidate)
            return


class DealsDatabase:
    """Elasticsearch-backed storage for deal, conversation, and order records."""

    def __init__(self) -> None:
        self._client: Elasticsearch | None = None

    def connect(self) -> None:
        if self._client is not None:
            return

        if bool(ELASTICSEARCH_USERNAME) != bool(ELASTICSEARCH_PASSWORD):
            raise ValueError(
                "Set both ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD, or neither."
            )

        client_options: dict[str, Any] = {"request_timeout": 5}
        if ELASTICSEARCH_API_KEY:
            client_options["api_key"] = ELASTICSEARCH_API_KEY
        elif ELASTICSEARCH_USERNAME and ELASTICSEARCH_PASSWORD:
            client_options["basic_auth"] = (
                ELASTICSEARCH_USERNAME,
                ELASTICSEARCH_PASSWORD,
            )

        client = Elasticsearch(ELASTICSEARCH_URL, **client_options)
        if not client.ping():
            client.close()
            raise ConnectionError(
                f"Elasticsearch at {ELASTICSEARCH_URL} did not respond to ping."
            )

        mappings = {
            "properties": {
                "deal_id": {"type": "keyword"},
                "seller_id": {"type": "long"},
                "buyer_id": {"type": "long"},
                "buyer_name": {"type": "keyword"},
                "buyer_username": {"type": "keyword"},
                "buyer_shipping_address": {"type": "text"},
                "seller_name": {"type": "keyword"},
                "seller_username": {"type": "keyword"},
                "state": {"type": "keyword"},
                "raw_description": {"type": "text"},
                "product_details": {"type": "text"},
                "terms": {"type": "text"},
                "terms_history": {
                    "type": "nested",
                    "properties": {
                        "version": {"type": "integer"},
                        "terms": {"type": "text"},
                        "updated_at": {"type": "date"},
                    },
                },
                "terms_agreements": {
                    "type": "nested",
                    "properties": {
                        "user_id": {"type": "long"},
                        "terms_version": {"type": "integer"},
                        "terms": {"type": "text"},
                        "accepted_at": {"type": "date"},
                    },
                },
                "conversation": {
                    "type": "nested",
                    "properties": {
                        "sender_role": {"type": "keyword"},
                        "text": {"type": "text"},
                        "created_at": {"type": "date"},
                    },
                },
                "order_steps": {
                    "type": "nested",
                    "properties": {
                        "from_state": {"type": "keyword"},
                        "to_state": {"type": "keyword"},
                        "changed_at": {"type": "date"},
                    },
                },
                "created_at": {"type": "date"},
                "updated_at": {"type": "date"},
            }
        }

        try:
            if not client.indices.exists(index=ELASTICSEARCH_INDEX):
                client.indices.create(index=ELASTICSEARCH_INDEX, mappings=mappings)
        except ApiError:
            client.close()
            raise

        self._client = client
        logger.info("Connected to Elasticsearch index '%s'.", ELASTICSEARCH_INDEX)

    def _require_client(self) -> Elasticsearch:
        if self._client is None:
            raise RuntimeError("Elasticsearch has not been connected.")
        return self._client

    def __getitem__(self, deal_id: str) -> DealDocument:
        client = self._require_client()
        response = client.get(index=ELASTICSEARCH_INDEX, id=deal_id)
        return DealDocument(
            deal_id=deal_id,
            source=response["_source"],
            client=client,
            seq_no=response["_seq_no"],
            primary_term=response["_primary_term"],
        )

    def list_for_user(self, user_id: int, limit: int = 10) -> list[DealDocument]:
        """Return the most recent deals in which this Telegram user participates."""
        client = self._require_client()
        response = client.search(
            index=ELASTICSEARCH_INDEX,
            query={
                "bool": {
                    "should": [
                        {"term": {"seller_id": user_id}},
                        {"term": {"buyer_id": user_id}},
                    ],
                    "minimum_should_match": 1,
                }
            },
            sort=[{"created_at": {"order": "desc"}}],
            size=limit,
            seq_no_primary_term=True,
        )
        return [
            DealDocument(
                deal_id=hit["_id"],
                source=hit["_source"],
                client=client,
                seq_no=hit["_seq_no"],
                primary_term=hit["_primary_term"],
            )
            for hit in response["hits"]["hits"]
        ]

    def search_seller_products(
        self, seller_id: int, query_text: str, limit: int = 8
    ) -> list[DealDocument]:
        """Find this seller's unjoined listings in the Elasticsearch deals index."""
        client = self._require_client()
        response = client.search(
            index=ELASTICSEARCH_INDEX,
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["terms^3", "product_details^3", "raw_description"],
                                "type": "best_fields",
                                "operator": "and",
                            }
                        }
                    ],
                    "filter": [
                        {"term": {"seller_id": seller_id}},
                        {"term": {"state": "CREATED"}},
                    ],
                }
            },
            sort=[{"created_at": {"order": "desc"}}],
            size=limit,
            seq_no_primary_term=True,
        )
        return [
            DealDocument(
                deal_id=hit["_id"],
                source=hit["_source"],
                client=client,
                seq_no=hit["_seq_no"],
                primary_term=hit["_primary_term"],
            )
            for hit in response["hits"]["hits"]
        ]

    def search_available_products(
        self, query_text: str, limit: int = 8
    ) -> list[DealDocument]:
        """Search public, unjoined listings in the marketplace."""
        client = self._require_client()
        response = client.search(
            index=ELASTICSEARCH_INDEX,
            query={
                "bool": {
                    "must": [
                        {
                            "multi_match": {
                                "query": query_text,
                                "fields": ["terms^3", "product_details^3", "raw_description"],
                                "type": "best_fields",
                                "operator": "and",
                            }
                        }
                    ],
                    "filter": [{"term": {"state": "CREATED"}}],
                }
            },
            sort=[{"created_at": {"order": "desc"}}],
            size=limit,
            seq_no_primary_term=True,
        )
        return [
            DealDocument(
                deal_id=hit["_id"],
                source=hit["_source"],
                client=client,
                seq_no=hit["_seq_no"],
                primary_term=hit["_primary_term"],
            )
            for hit in response["hits"]["hits"]
        ]

    def __setitem__(self, deal_id: str, deal: dict[str, Any]) -> None:
        client = self._require_client()
        created_at = _utc_now()
        state = deal.get("state")
        terms = deal.get("terms", "")
        document = deepcopy(deal)
        document.update(
            {
                "deal_id": deal_id,
                "product_details": terms,
                "terms_history": [
                    {"version": 1, "terms": terms, "updated_at": created_at}
                ],
                "terms_agreements": [],
                "conversation": [],
                "order_steps": [
                    {
                        "from_state": None,
                        "to_state": state,
                        "changed_at": created_at,
                    }
                ],
                "created_at": created_at,
                "updated_at": created_at,
            }
        )
        client.index(
            index=ELASTICSEARCH_INDEX,
            id=deal_id,
            document=document,
            op_type="create",
        )

    def __contains__(self, deal_id: object) -> bool:
        return bool(
            self._require_client().exists(
                index=ELASTICSEARCH_INDEX, id=str(deal_id)
            )
        )

    def get(self, deal_id: str, default: Any = None) -> DealDocument | Any:
        try:
            return self[deal_id]
        except NotFoundError:
            return default

    def append_conversation(
        self, deal: DealDocument, sender_role: str, text: str
    ) -> None:
        conversation = list(deal.get("conversation", []))
        conversation.append(
            {
                "sender_role": sender_role,
                "text": text,
                "created_at": _utc_now(),
            }
        )
        deal["conversation"] = conversation

    def record_terms_agreement(
        self, deal: DealDocument, user_id: int, confirmation_field: str
    ) -> None:
        if confirmation_field not in {
            "seller_confirmed_terms",
            "buyer_confirmed_terms",
        }:
            raise ValueError(f"Unsupported terms confirmation field: {confirmation_field}")

        terms_history = deal.get("terms_history", [])
        terms_version = terms_history[-1]["version"] if terms_history else 1
        agreements = list(deal.get("terms_agreements", []))
        already_recorded = any(
            agreement["user_id"] == user_id
            and agreement["terms_version"] == terms_version
            for agreement in agreements
        )
        if already_recorded and deal.get(confirmation_field):
            return

        if not already_recorded:
            agreements.append(
                {
                    "user_id": user_id,
                    "terms_version": terms_version,
                    "terms": deal["terms"],
                    "accepted_at": _utc_now(),
                }
            )
        deal.update_fields(
            {
                confirmation_field: True,
                "terms_agreements": agreements,
            }
        )


deals_db = DealsDatabase()
