# tests/core/test_webhook_delivery_shape.py
"""The delivery ledger must be writable no matter what shape arrived.

Replaying the two deliveries the production ledger recorded as
``TransactionError`` surfaced a second defect with nothing to do with
transactions: a payload that parses but is not an object (or whose nested
``repository`` / ``workflow_job`` is not one) raised ``AttributeError`` inside
``record_delivery``, so the row that should have explained the failure was never
written. Two failures stacked, and the second hid the first.

GitHub itself always sends objects; this is not about trusting GitHub. It is
that the ledger is the only answer to "what arrived while I was broken", and a
recorder that can itself fail answers nothing.
"""

from __future__ import annotations

import json

from conftest import make_webhook_service

from rhosocial.github_ci_tracker.models import AsyncWebhookDelivery

REPO = "acme/demo"


def _payload(**overrides: object) -> dict:
    body = {
        "action": "completed",
        "repository": {"id": 3, "full_name": REPO},
        "workflow_job": {
            "id": 4243,
            "run_id": 4242,
            "name": "tests",
            "status": "completed",
            "conclusion": "success",
            "steps": [],
        },
    }
    body.update(overrides)  # type: ignore[arg-type]
    return body


class TestRecordDeliveryShape:
    async def test_a_nested_non_object_is_recorded_not_raised(self, bound) -> None:
        """`repository: "oops"` must produce a row, not an AttributeError."""
        service = make_webhook_service()

        result = await service.record_delivery(
            "workflow_job",
            delivery_id="d-shape-1",
            payload={"repository": "oops", "workflow_job": "also not an object"},
            handled=False,
            reason="whatever_the_handler_said",
        )

        assert result["handled"] is False
        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-shape-1"
        )
        assert delivery is not None
        assert delivery.repository_full_name == ""
        assert delivery.github_run_id is None

    async def test_a_non_mapping_payload_is_recorded(self, bound) -> None:
        """A replay harness sending the body twice produces a string root."""
        service = make_webhook_service()

        await service.record_delivery(
            "workflow_job",
            delivery_id="d-shape-2",
            payload=json.dumps(_payload()),  # a str, not a dict
            handled=False,
            reason="ignored",
        )

        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-shape-2"
        )
        assert delivery is not None

    async def test_a_well_formed_payload_still_records_the_ids(self, bound) -> None:
        service = make_webhook_service()

        await service.record_delivery(
            "workflow_job",
            delivery_id="d-shape-3",
            payload=_payload(),
            handled=True,
            reason="processed",
        )

        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-shape-3"
        )
        assert delivery is not None
        assert delivery.handled is True
        assert delivery.repository_full_name == REPO
        assert delivery.github_run_id == 4242
        assert delivery.github_job_id == 4243


class TestProcessWebhookShape:
    async def test_a_json_root_that_is_not_an_object_is_rejected(self, bound) -> None:
        service = make_webhook_service()

        result = await service.process_webhook("workflow_job", "[1, 2, 3]", delivery_id="d-root-1")

        assert result["handled"] is False
        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-root-1"
        )
        assert delivery is not None
        assert "not an object" in (delivery.reason or "")

    async def test_a_double_encoded_body_is_rejected_not_crashed(self, bound) -> None:
        service = make_webhook_service()

        result = await service.process_webhook(
            "workflow_job",
            json.dumps(json.dumps(_payload())),  # twice-encoded
            delivery_id="d-root-2",
        )

        assert result["handled"] is False
        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-root-2"
        )
        assert delivery is not None


class TestRepositoryFieldShape:
    """``repository`` must survive being something other than an object.

    The replay container reproduced the production shape exactly: a
    ``repository`` that is a string (GitHub sends an object; the tracker's own
    ingest did not) left ``AttributeError`` in the ledger, because
    ``payload.get("repository") or {}`` does not catch a truthy non-mapping.
    The delivery is recorded and handled as ignored, which is the whole answer.
    """

    async def test_a_string_repository_is_ignored_not_raised(self, bound) -> None:
        service = make_webhook_service()

        result = await service.process_webhook(
            "workflow_job",
            {
                "repository": "rhosocial/python-activerecord-mariadb",
                "workflow_job": {"id": 1, "run_id": 1, "status": "queued", "steps": []},
            },
            delivery_id="d-repo-str",
        )

        assert result["handled"] is False
        assert result["reason"] == "ignored"
        delivery = await AsyncWebhookDelivery.find_one(
            AsyncWebhookDelivery.c.delivery_id == "d-repo-str"
        )
        assert delivery is not None
        assert delivery.reason == "ignored"
