"""Unit tests for organization-scoped commodity-group rules."""

from app.schemas.extraction import ExtractedVendorData, OrderLineData
from app.schemas.organizations import CommodityGroupOverride
from app.seed import ACME_COMMODITY_GROUP_OVERRIDES
from app.services.commodity_overrides import apply_commodity_override
from pydantic import ValidationError


RULES = [CommodityGroupOverride.model_validate(rule) for rule in ACME_COMMODITY_GROUP_OVERRIDES]


def _extracted(title: str, total_cost: float = 100.0, *descriptions: str):
    """Build a minimal extraction result for override tests."""
    return ExtractedVendorData(
        title=title,
        total_cost=total_cost,
        order_lines=[OrderLineData(position_description=value) for value in descriptions],
        commodity_group_id=31,
        commodity_group_name="Software",
    )


def test_cable_ties_override_to_electrical_engineering() -> None:
    """Cable ties always use Acme's Electrical Engineering group."""
    extracted = _extracted("Warehouse supplies", 100, "Nylon cable-ties")

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id == 11
    assert extracted.commodity_group_id == 11


def test_printer_toner_override_supports_german() -> None:
    """Printer toner matching includes the documented German synonym."""
    extracted = _extracted("Druckertoner replenishment")

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id == 29


def test_common_plural_and_zip_tie_phrasings_match() -> None:
    """Rules match common plural and synonym phrasings from vendor quotes."""
    toner = apply_commodity_override(_extracted("Toner cartridges HP"), RULES)
    cable = apply_commodity_override(_extracted("Zip ties 100pk"), RULES)

    assert toner.commodity_group_id == 29
    assert cable.commodity_group_id == 11


def test_software_subscription_has_cost_limit() -> None:
    """Software subscriptions above the configured small-order limit remain AI-classified."""
    extracted = _extracted("Software subscription renewal", 2000.01)

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id is None
    assert extracted.commodity_group_id == 31


def test_subscription_style_software_phrasings_match() -> None:
    """Generic software signals match while brand-only names fall back to AI."""
    for title in (
        "SaaS subscription",
        "Software license office suite, annual",
    ):
        extracted = _extracted(title)
        result = apply_commodity_override(extracted, RULES)
        assert result.commodity_group_id == 2001

    for title in (
        "Antivirus software license, 3 seats",
        "Adobe Creative Cloud annual subscription",
        "Microsoft 365 annual licenses",
    ):
        extracted = _extracted(title)
        result = apply_commodity_override(extracted, RULES)
        assert result.commodity_group_id is None


def test_perpetual_license_is_not_a_small_subscription() -> None:
    """A perpetual software license is not treated as a subscription override."""
    extracted = _extracted("CAD software license perpetual")

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id is None
    assert extracted.commodity_group_id == 31


def test_generic_subscriptions_do_not_match_software_override() -> None:
    """A subscription must carry a software signal before it is overridden."""
    for title in (
        "Wall Street Journal digital annual subscription",
        "Water cooler service monthly subscription",
        "Gym membership annual subscription",
        "Annual license fee elevator inspection",
    ):
        extracted = _extracted(title, 500)
        result = apply_commodity_override(extracted, RULES)
        assert result.commodity_group_id is None


def test_unknown_cost_does_not_qualify_for_cost_limited_rule() -> None:
    """A cost threshold is not applied when the total cost is unknown."""
    extracted = _extracted("Software subscription renewal", None)

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id is None
    assert extracted.commodity_group_id == 31


def test_empty_override_rule_is_rejected() -> None:
    """Rules must contain a signal and cannot match every request."""
    try:
        CommodityGroupOverride(commodity_group_id=11)
    except ValidationError as exc:
        assert "At least one override keyword" in str(exc)
    else:
        raise AssertionError("Expected empty override rule to be rejected")


def test_multiple_matches_are_reported_without_silent_priority() -> None:
    """Conflicting rules do not silently select one commodity group."""
    conflicting_rules = RULES + [
        CommodityGroupOverride(
            alternative_keywords=["software"], commodity_group_id=29
        )
    ]
    extracted = _extracted("Software subscription renewal")

    result = apply_commodity_override(extracted, conflicting_rules)

    assert result.conflict is True
    assert extracted.commodity_group_id == 31


def test_unmatched_text_preserves_ai_classification() -> None:
    """Unmatched text keeps the model's original classification."""
    extracted = _extracted("Office chairs")

    result = apply_commodity_override(extracted, RULES)

    assert result.commodity_group_id is None
    assert extracted.commodity_group_id == 31


def test_empty_rules_leave_other_organizations_untouched() -> None:
    """A tenant with no configured rules keeps the AI classification."""
    extracted = _extracted("Printer toner")

    result = apply_commodity_override(extracted, [])

    assert result.commodity_group_id is None
    assert extracted.commodity_group_id == 31
