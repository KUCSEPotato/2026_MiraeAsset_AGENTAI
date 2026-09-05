"""Generic, entity-type-aware normalization for deterministic lookup."""

from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

from app.data.cleaning import normalize_lookup_value


_PRODUCT_TYPES = {
    "product",
    "financial_product",
    "fund",
    "fund_share_class",
    "sale_lot",
}
_ORGANIZATION_TYPES = {
    "asset_manager",
    "company",
    "holding_company",
    "institution",
    "issuer",
    "management_company",
    "organization",
    "portfolio_company",
    "subsidiary",
}
_SEPARATOR_PATTERN = re.compile(r"[\s\-‐‑‒–—_.,·ㆍ/()\[\]{}]+")
_LEGAL_AFFIX_PATTERN = re.compile(
    r"^(?:주식회사|㈜|\(주\))\s*|"
    r"(?:주식회사|㈜|\(주\)|\bco\.?\s*,?\s*ltd\.?|\bltd\.?)$",
    re.IGNORECASE,
)
_PRODUCT_SUFFIX_PATTERN = re.compile(
    r"(?:\s+|(?<=[가-힣]))(?:etf|etn|펀드|채권|상품)$",
    re.IGNORECASE,
)

FUZZY_ACCEPTANCE_THRESHOLD = 0.9
FUZZY_AMBIGUITY_MARGIN = 0.03
FUZZY_CANDIDATE_THRESHOLD = 0.55
FUZZY_CANDIDATE_LIMIT = 10


def entity_lookup_keys(value: str, entity_type: str) -> tuple[str, ...]:
    """Return exact storage keys, including safe context-derived variants."""

    variants = _text_variants(value, entity_type)
    keys = [normalize_lookup_value(item) for item in variants]
    return tuple(dict.fromkeys(key for key in keys if key))


def normalized_entity_form(value: str, entity_type: str) -> str:
    """Return the canonical comparison form used for normalized exact match."""

    variants = _text_variants(value, entity_type)
    selected = variants[-1] if variants else value
    normalized = unicodedata.normalize("NFKC", selected).casefold()
    return _SEPARATOR_PATTERN.sub("", normalized)


def entity_name_similarity(left: str, right: str, entity_type: str) -> float:
    """Deterministic lexical score for candidate generation, never identity."""

    if entity_type in _ORGANIZATION_TYPES and _identity_suffix(left) != _identity_suffix(right):
        return 0.0
    query = normalized_entity_form(left, entity_type)
    candidate = normalized_entity_form(right, entity_type)
    if not query or not candidate:
        return 0.0
    if query == candidate:
        return 1.0
    if min(len(query), len(candidate)) < 5:
        return 0.0
    return round(SequenceMatcher(None, query, candidate).ratio(), 6)


def _text_variants(value: str, entity_type: str) -> tuple[str, ...]:
    normalized = " ".join(unicodedata.normalize("NFKC", value).strip().split())
    variants = [normalized]
    key = entity_type.casefold()
    if key in _PRODUCT_TYPES:
        stripped = _PRODUCT_SUFFIX_PATTERN.sub("", normalized).strip()
        if stripped and stripped != normalized:
            variants.append(stripped)
    if key in _ORGANIZATION_TYPES:
        legal = _LEGAL_AFFIX_PATTERN.sub("", normalized).strip()
        if legal and legal != normalized:
            variants.append(legal)
        organization = variants[-1]
        replacements = (
            (r"자산운용사$", "자산운용"),
            (r"증권사$", "증권"),
        )
        for pattern, replacement in replacements:
            replaced = re.sub(pattern, replacement, organization).strip()
            if replaced and replaced != organization:
                variants.append(replaced)
                organization = replaced
    return tuple(dict.fromkeys(variants))


def _identity_suffix(value: str) -> str | None:
    legal = _LEGAL_AFFIX_PATTERN.sub("", value.strip()).strip()
    match = re.search(r"(자산운용사?|운용사|증권사?|은행|보험)$", legal)
    return match.group(1).removesuffix("사") if match else None


def organization_identity_compatible(raw: str, canonical: str | None, entity_type: str) -> bool:
    """A qualified organization name cannot alias a different business identity."""
    if entity_type not in _ORGANIZATION_TYPES or not _identity_suffix(raw):
        return True
    return bool(canonical) and _identity_suffix(raw) == _identity_suffix(canonical)
