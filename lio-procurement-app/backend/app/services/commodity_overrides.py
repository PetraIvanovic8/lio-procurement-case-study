"""Deterministic, organization-scoped commodity classification rules."""

import re
from dataclasses import dataclass

from app.schemas.extraction import ExtractedVendorData
from app.schemas.organizations import CommodityGroupOverride


@dataclass(frozen=True)
class OverrideMatch:
    """The result of evaluating configured commodity rules."""

    commodity_group_id: int | None = None
    reason: str | None = None
    conflict: bool = False
    group_ids: tuple[int, ...] = ()


def _normalize_token(value: str) -> str:
    """Normalize a token, including common English plural forms."""
    value = value.casefold()
    if value.endswith("ies") and len(value) > 4:
        return value[:-3] + "y"
    if value.endswith("s") and not value.endswith(("ss", "us")) and len(value) > 3:
        return value[:-1]
    return value


def _tokens(value: str) -> list[str]:
    """Normalize punctuation, hyphens, and common plural forms."""
    return [_normalize_token(token) for token in re.findall(r"[\w]+", value, re.UNICODE)]


def _contains_phrase(text: str, phrase: str) -> bool:
    """Return whether ``phrase`` occurs as a complete normalized phrase."""
    text_tokens = _tokens(text)
    phrase_tokens = _tokens(phrase)
    if not phrase_tokens or len(phrase_tokens) > len(text_tokens):
        return False
    return any(
        text_tokens[index : index + len(phrase_tokens)] == phrase_tokens
        for index in range(len(text_tokens) - len(phrase_tokens) + 1)
    )


def matching_overrides(
    text: str, total_cost: float | None, overrides: list[CommodityGroupOverride]
) -> list[CommodityGroupOverride]:
    """Return all configured rules matching the procurement description."""
    matches = []
    for override in overrides:
        excluded = any(_contains_phrase(text, keyword) for keyword in override.excluded_keywords)
        if excluded:
            continue
        required = all(
            _contains_phrase(text, keyword) for keyword in override.required_keywords
        )
        alternatives_match = not override.alternative_keywords or any(
            _contains_phrase(text, keyword)
            for keyword in override.alternative_keywords
        )
        below_limit = (
            override.max_total_cost is None
            or (total_cost is not None and total_cost <= override.max_total_cost)
        )
        if required and alternatives_match and below_limit:
            matches.append(override)
    return matches


def apply_commodity_override(
    extracted: ExtractedVendorData,
    overrides: list[CommodityGroupOverride],
) -> OverrideMatch:
    """Apply one deterministic override to an extracted result.

    Args:
        extracted: AI-extracted procurement data to update in place.
        overrides: Rules loaded from the authenticated organization's settings.

    Returns:
        Match metadata. Multiple matches are reported as a conflict and do not
        silently change the AI classification.
    """
    searchable_text = " ".join(
        [
            value
            for value in [
                extracted.title or "",
                *(line.position_description or "" for line in extracted.order_lines),
            ]
            if value
        ]
    )
    matches = matching_overrides(searchable_text, extracted.total_cost, overrides)
    matched_group_ids = {match.commodity_group_id for match in matches}
    if len(matched_group_ids) != 1:
        return OverrideMatch(
            conflict=len(matched_group_ids) > 1,
            group_ids=tuple(sorted(matched_group_ids)),
        )

    match = matches[0]
    extracted.commodity_group_id = match.commodity_group_id
    return OverrideMatch(
        commodity_group_id=match.commodity_group_id,
        reason="Configured organization commodity rule matched",
        group_ids=(match.commodity_group_id,),
    )
