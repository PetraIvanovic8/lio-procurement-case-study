"""Unit tests for negotiated catalog article matching."""

from app.services.article_suggestions import _has_minimum_token_overlap, score_article_description


def test_score_normalizes_case_punctuation_and_common_words():
    score = score_article_description(
        "The DOCKING-STATION USB-C",
        "Docking station USB-C, dual 4K",
    )

    assert score is not None
    assert 0.0 <= score <= 1.0
    assert score > 0.65


def test_score_rewards_exact_phrase_matches():
    exact = score_article_description(
        "software license collaboration suite",
        "Software license collaboration suite, annual",
    )
    partial = score_article_description(
        "software license",
        "Software license collaboration suite, annual",
    )

    assert exact is not None and partial is not None
    assert exact > partial


def test_score_handles_small_pdf_typos():
    score = score_article_description(
        "dokin staton USB-C",
        "Docking station USB-C, dual 4K",
    )

    assert score is not None


def test_score_handles_truncated_pdf_words():
    score = score_article_description(
        "Docking stat",
        "Docking station USB-C, dual 4K",
    )

    assert score is not None
    assert 0.65 <= score < 0.85


def test_score_handles_catalog_aliases_and_partial_descriptions():
    """Common procurement aliases still find a useful catalog description."""
    score = score_article_description(
        "13 MacBook Air Apple M2 Chip",
        "Business laptop 13 ultralight 16 GB 1 TB",
    )

    assert score is not None
    assert score >= 0.20


def test_score_uses_real_extracted_macbook_line():
    """The catalog alias also works with the full extracted quote wording."""
    score = score_article_description(
        '13" MacBook Air: Apple M2 Chip - Space Gray',
        "Business laptop 13 ultralight 16 GB 1 TB",
    )

    assert score is not None
    assert score >= 0.20


def test_generic_single_token_matches_are_not_confident_suggestions():
    """A single shared generic word must not pass candidate filtering."""
    assert not _has_minimum_token_overlap(
        "Delivery and installation",
        "Last-mile delivery standard",
    )


def test_fuzzy_typo_tokens_count_toward_candidate_filtering():
    """Two misspelled product tokens can still recover a catalog candidate."""
    assert _has_minimum_token_overlap(
        "dokin staton USB-C",
        "Docking station USB-C, dual 4K",
    )


def test_score_rejects_unrelated_descriptions():
    assert (
        score_article_description(
            "industrial cleaning chemicals",
            "Docking station USB-C, dual 4K",
        )
        is None
    )


def test_score_returns_none_for_empty_description():
    assert score_article_description("", "Docking station USB-C") is None
