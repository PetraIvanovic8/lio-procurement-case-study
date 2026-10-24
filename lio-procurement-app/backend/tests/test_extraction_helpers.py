"""Unit tests for deterministic extraction post-processing."""

from app.schemas.extraction import ExtractedVendorData, OrderLineData
from app.schemas.requests import AdjustmentIn
from app.services.extraction import (
    _is_known_buyer,
    _normalise_line_types,
    _reconcile_totals,
)


def test_alternatives_are_excluded_and_adjustments_are_preserved():
    extracted = ExtractedVendorData.model_validate(
        {
            "title": "Moss panel",
            "orderLines": [
                {
                    "positionDescription": "Moss panel",
                    "unitPrice": 100,
                    "amount": 1,
                    "totalPrice": 100,
                    "lineType": "item",
                },
                {
                    "positionDescription": "Alternative to ball moss",
                    "unitPrice": 150,
                    "amount": 1,
                    "totalPrice": 150,
                    "lineType": "alternative",
                },
                {
                    "positionDescription": "Vertical logo (Alt.)",
                    "unitPrice": 40,
                    "amount": 1,
                    "totalPrice": 40,
                    "lineType": "item",
                },
                {
                    "positionDescription": "Shipping",
                    "unitPrice": 20,
                    "amount": 1,
                    "totalPrice": 20,
                    "lineType": "shipping",
                },
            ],
            "totalCost": 120,
        }
    )

    warnings = _normalise_line_types(extracted)

    assert len(extracted.order_lines) == 1
    assert [alternative.description for alternative in extracted.alternatives] == [
        "Alternative to ball moss",
        "Vertical logo (Alt.)",
    ]
    assert [adjustment.line_type for adjustment in extracted.adjustments] == ["shipping"]
    assert warnings == [
        "Excluded Alternative to ball moss from order lines.",
        "Excluded Vertical logo (Alt.) from order lines.",
    ]
    assert _reconcile_totals(extracted) == []


def test_product_descriptions_are_not_misclassified_as_alternatives():
    """Ordinary replacement, optional, and option wording remains purchasable."""
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [
                {
                    "positionDescription": description,
                    "unitPrice": 10,
                    "amount": 1,
                    "totalPrice": 10,
                    "lineType": "item",
                }
                for description in (
                    "Replacement toner cartridge black",
                    "Optional power adapter",
                    "Option 2 bundle: 5 monitors",
                    "Alt key caps set",
                )
            ],
            "totalCost": 40,
        }
    )

    warnings = _normalise_line_types(extracted)

    assert len(extracted.order_lines) == 4
    assert not extracted.alternatives
    assert not warnings


def test_missing_quantity_defaults_to_one_for_lump_sum_items():
    """A positive lump-sum total makes a missing quantity a single unit."""
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [
                {
                    "positionDescription": "Commissioning lump sum",
                    "unitPrice": 1500,
                    "amount": None,
                    "totalPrice": 1500,
                    "lineType": "item",
                }
            ],
            "totalCost": 1500,
        }
    )

    warnings = _normalise_line_types(extracted)

    assert extracted.order_lines[0].amount == 1
    assert extracted.order_lines[0].unit_price == 1500
    assert any("quantity to 1" in warning for warning in warnings)


def test_reconciliation_warns_when_items_and_adjustments_do_not_match_total():
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [
                {"positionDescription": "Item", "totalPrice": 100, "lineType": "item"}
            ],
            "totalCost": 125,
        }
    )

    assert _reconcile_totals(extracted)


def test_inline_discount_total_is_reconciled_without_a_second_discount():
    """A discounted extended line total is accepted as the quoted amount."""
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [
                {
                    "positionDescription": "Discounted item",
                    "unitPrice": 100,
                    "amount": 1,
                    "totalPrice": 80,
                    "lineType": "item",
                }
            ],
            "totalCost": 80,
        }
    )

    warnings = _normalise_line_types(extracted)

    assert extracted.order_lines[0].unit_price == 80
    assert any("inline discount" in warning for warning in warnings)
    assert _reconcile_totals(extracted) == []


def test_unknown_and_optional_line_types_are_classified_conservatively():
    """Unknown rows remain purchasable while explicit alternatives are excluded."""
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [
                {
                    "positionDescription": "Optional replacement item",
                    "unitPrice": 100,
                    "amount": 1,
                    "totalPrice": 100,
                    "lineType": "optional",
                },
                {
                    "positionDescription": "Tax",
                    "unitPrice": 0,
                    "amount": 1,
                    "totalPrice": 10,
                    "lineType": "tax",
                },
                {
                    "positionDescription": "Unknown row",
                    "unitPrice": 50,
                    "amount": 1,
                    "totalPrice": 50,
                    "lineType": "mystery",
                },
            ],
            "totalCost": 160,
        }
    )

    warnings = _normalise_line_types(extracted)

    assert [line.position_description for line in extracted.order_lines] == [
        "Unknown row"
    ]
    assert len(extracted.alternatives) == 1
    assert [adjustment.line_type for adjustment in extracted.adjustments] == ["fee"]
    assert any("Treated unknown line type" in warning for warning in warnings)


def test_adjustments_are_safe_for_request_submission():
    """Provider-specific adjustment labels become valid request adjustments."""
    extracted = ExtractedVendorData.model_validate(
        {
            "adjustments": [
                {
                    "description": "CA eWaste Recycling Fee",
                    "amount": 10,
                    "lineType": "recycling",
                },
                {"description": "Fuel surcharge", "amount": 5, "lineType": "surcharge"},
                {"description": None, "amount": 7, "lineType": "tax"},
            ]
        }
    )

    _normalise_line_types(extracted)

    submitted = [
        AdjustmentIn.model_validate(adjustment.model_dump(by_alias=True))
        for adjustment in extracted.adjustments
    ]
    assert [adjustment.line_type for adjustment in submitted] == ["fee", "fee", "tax"]
    assert submitted[-1].description == "Tax"


def test_nullable_line_and_adjustment_values_use_safe_defaults():
    """A provider null cannot invalidate an otherwise usable extraction."""
    extracted = ExtractedVendorData.model_validate(
        {
            "orderLines": [{
                "positionDescription": None,
                "unitPrice": None,
                "amount": None,
                "unit": None,
                "totalPrice": None,
            }],
            "adjustments": [{"description": None, "amount": None}],
            "totalCost": None,
        }
    )

    assert extracted.order_lines == [OrderLineData()]
    assert extracted.adjustments[0].amount == 0
    assert _reconcile_totals(extracted)


def test_empty_vendor_does_not_trigger_buyer_guard():
    """Missing vendor data should be reported as missing, not as the buyer."""
    assert not _is_known_buyer("", "Acme Corp", ["Lio Technologies"])


def test_buyer_guard_detects_known_buyer_names_only():
    assert _is_known_buyer("Lio Technologies, Inc.", "Acme Corp", ["Lio Technologies"])
    assert not _is_known_buyer("Hartwell Industrial Tools, Inc.", "Acme Corp", ["Lio Technologies"])
