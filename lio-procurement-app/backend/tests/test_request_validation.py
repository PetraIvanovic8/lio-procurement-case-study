"""Unit tests for request payload and total validation."""

import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.routers.requests import _approval_for_total, _authoritative_total
from app.core.config import settings
from app.schemas.requests import RequestCreate


def _request_payload(**overrides) -> dict:
    """Return a minimal valid request payload for schema tests."""
    payload = {
        "requestorName": "Petra",
        "titleShortDescription": "Office equipment",
        "vendorName": "Example Supplier",
        "vatId": None,
        "commodityGroupId": 29,
        "totalCost": 100,
        "orderLines": [
            {
                "positionDescription": "Docking station",
                "unitPrice": 100,
                "amount": 1,
                "unit": "piece",
                "totalPrice": 100,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_zero_quantity_is_rejected():
    """A request cannot contain a line with no requested quantity."""
    with pytest.raises(ValidationError):
        RequestCreate.model_validate(
            _request_payload(
                orderLines=[
                    {
                        "positionDescription": "Docking station",
                        "unitPrice": 100,
                        "amount": 0,
                        "unit": "piece",
                        "totalPrice": 0,
                    }
                ]
            )
        )


def test_adjustments_are_typed_and_finite():
    """Only supported adjustment types and finite numeric amounts are accepted."""
    payload = RequestCreate.model_validate(
        _request_payload(
            totalCost=110,
            adjustments=[
                {"description": "Shipping", "amount": 10, "lineType": "shipping"}
            ],
        )
    )
    assert payload.adjustments[0].amount == 10

    with pytest.raises(ValidationError):
        RequestCreate.model_validate(
            _request_payload(
                adjustments=[
                    {"description": "Unknown", "amount": 10, "lineType": "other"}
                ]
            )
        )


def test_authoritative_total_rejects_understated_line_total():
    """A client cannot lower a line total below unit price times quantity."""
    payload = RequestCreate.model_validate(
        _request_payload(
            totalCost=0,
            orderLines=[
                {
                    "positionDescription": "Docking station",
                    "unitPrice": 100,
                    "amount": 1,
                    "unit": "piece",
                    "totalPrice": 0,
                }
            ],
        )
    )

    with pytest.raises(HTTPException, match="total must equal"):
        _authoritative_total(payload)


def test_auto_approval_applies_only_below_threshold():
    """Totals below the threshold bypass approval; the boundary does not."""
    approved_status, approved_at = _approval_for_total(settings.auto_approval_threshold - 0.01)
    pending_status, pending_at = _approval_for_total(settings.auto_approval_threshold)

    assert approved_status == "approved"
    assert approved_at is not None
    assert pending_status == "pending"
    assert pending_at is None
