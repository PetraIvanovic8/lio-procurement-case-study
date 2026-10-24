"""Matching helpers for negotiated catalog article suggestions."""

import logging
import re
import uuid
from difflib import SequenceMatcher

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from app.models import Article
from app.schemas.articles import ArticleSuggestion

logger = logging.getLogger(__name__)

_TOKEN_PATTERN = re.compile(r"[a-z0-9]+", re.IGNORECASE)
_IGNORED_TOKENS = {
    "a", "an", "and", "for", "in", "of", "the", "to", "per",
    "sq", "ft", "mm", "cm", "inch", "inches", "kg", "lb", "lbs",
}
_MIN_SUGGESTION_SCORE = 0.20
_TOKEN_ALIASES = {
    "macbook": "laptop",
    "notebook": "laptop",
    "air": "ultralight",
    "pcs": "piece",
    "ea": "piece",
}


def _overlap_tokens(value: str) -> set[str]:
    """Return overlap tokens while keeping hyphenated compounds together.

    The scoring path intentionally keeps its broader tokenization for typo
    tolerance. Candidate filtering uses this stricter representation so
    ``heavy-duty`` cannot count as two independent shared words.
    """
    return {
        _normalise_token(token)
        for token in re.findall(r"[a-z0-9]+(?:-[a-z0-9]+)*", value.casefold())
        if len(token) > 1 and not token.isdigit() and token not in _IGNORED_TOKENS
    }


def _normalise_token(token: str) -> str:
    """Normalize common plural and procurement-specific token variants."""
    token = _TOKEN_ALIASES.get(token.lower(), token.lower())
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and not token.endswith(("ss", "us")) and len(token) > 3:
        return token[:-1]
    return token


def _tokens(value: str) -> set[str]:
    """Return normalized, meaningful search tokens from a description."""
    return {
        _normalise_token(token)
        for token in _TOKEN_PATTERN.findall(value)
        if token.lower() not in _IGNORED_TOKENS
        and len(token) > 1
        and not token.isdigit()
    }


def _token_similarity(query_token: str, article_token: str) -> float:
    """Return a conservative similarity score for two normalized tokens."""
    if query_token == article_token:
        return 1.0
    # PDF extraction often truncates words. Require three characters so short
    # tokens do not create broad false positives (for example, "it").
    if len(query_token) >= 3 and (
        query_token.startswith(article_token) or article_token.startswith(query_token)
    ):
        return 0.6
    if len(query_token) >= 4 and SequenceMatcher(None, query_token, article_token).ratio() >= 0.75:
        return 0.5
    return 0.0


def _has_minimum_token_overlap(query: str, article_description: str, minimum: int = 2) -> bool:
    """Return whether a candidate has enough exact or fuzzy meaningful tokens."""
    query_tokens = _overlap_tokens(query)
    article_tokens = _overlap_tokens(article_description)
    matched = sum(
        max(_token_similarity(query_token, article_token) for article_token in article_tokens) > 0
        for query_token in query_tokens
    )
    return matched >= minimum


def score_article_description(query: str, article_description: str) -> float | None:
    """Score how well a catalog description matches an order-line description.

    Args:
        query: Extracted or manually entered order-line description.
        article_description: Description from the negotiated catalog.

    Returns:
        A normalized score between 0 and 1, or ``None`` when the candidate is
        not sufficiently similar to be useful as a suggestion.
    """
    query_tokens = _tokens(query)
    if not query_tokens:
        return None

    normalized_query = " ".join(_TOKEN_PATTERN.findall(query.lower()))
    normalized_article = " ".join(_TOKEN_PATTERN.findall(article_description.lower()))
    article_tokens = _tokens(article_description)
    matched_tokens = sum(
        max(_token_similarity(query_token, article_token) for article_token in article_tokens)
        for query_token in query_tokens
    )
    lexical_score = matched_tokens / len(query_tokens)
    article_coverage = matched_tokens / len(article_tokens) if article_tokens else 0
    phrase_bonus = 0.05 if normalized_query and normalized_query in normalized_article else 0.0

    # Fuzzy similarity is a secondary signal so exact catalog words remain more
    # important than a superficially similar description.
    fuzzy_score = SequenceMatcher(None, normalized_query, normalized_article).ratio()
    raw_score = (
        lexical_score * 0.55
        + article_coverage * 0.25
        + fuzzy_score * 0.20
        + phrase_bonus
    )
    if lexical_score == 0 and fuzzy_score < 0.55:
        return None
    return min(raw_score, 1.0)


def suggest_articles(
    db: Session,
    organization_id: uuid.UUID,
    description: str,
    limit: int = 3,
) -> list[ArticleSuggestion]:
    """Find the closest negotiated catalog articles for an order-line description.

    Candidates are always restricted to the requester's organization. Matching is
    intentionally deterministic and explainable: articles containing more of the
    description's meaningful words rank first, with an exact phrase match as a
    small tie-breaker.

    Args:
        db: Database session used to read the organization catalog.
        organization_id: Organization whose negotiated catalog is searched.
        description: Free-text order-line description.
        limit: Maximum number of suggestions to return.

    Returns:
        A short list of catalog articles ordered by match quality. Candidates
        below the minimum confidence threshold are omitted.
    """
    if not _tokens(description):
        return []

    # The catalog is currently small enough to rank all articles for this
    # organization. Keeping the organization filter in SQL prevents cross-tenant
    # comparisons while allowing fuzzy matching to recover from typos.
    candidates = db.scalars(
        select(Article)
        .where(Article.organization_id == organization_id)
        .options(joinedload(Article.supplier))
    ).all()

    ranked = []
    for article in candidates:
        score = score_article_description(description, article.description)
        if (
            score is not None
            and score >= _MIN_SUGGESTION_SCORE
            and _has_minimum_token_overlap(description, article.description)
        ):
            ranked.append((score, article.unit_price, article.description.casefold(), article))

    ranked.sort(key=lambda item: (-item[0], item[1], item[2], item[3].article_number))
    distinct = []
    seen_descriptions = set()
    for item in ranked:
        description_key = item[2]
        if description_key in seen_descriptions:
            continue
        seen_descriptions.add(description_key)
        distinct.append(item)
    logger.info(
        "Catalog suggestions generated: organization_id=%s description_length=%d candidates=%d",
        organization_id,
        len(description),
        len(distinct),
    )
    return [
        ArticleSuggestion(
            id=article.id,
            article_number=article.article_number,
            supplier_id=article.supplier_id,
            supplier_name=article.supplier.name if article.supplier else None,
            description=article.description,
            unit_price=article.unit_price,
            currency=article.currency,
            unit=article.unit,
            quantity=article.quantity,
            created_at=article.created_at,
            match_score=round(score, 3),
        )
        for score, _, _, article in distinct[:limit]
    ]
