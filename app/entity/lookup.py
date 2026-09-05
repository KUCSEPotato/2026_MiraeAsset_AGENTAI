from typing import Protocol

from app.domain.models import (
    CanonicalEntity,
    EntityLookupMatch,
    EntityLookupOutcome,
    EntityResolutionCandidate,
)
from app.entity.normalization import (
    FUZZY_ACCEPTANCE_THRESHOLD,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_CANDIDATE_LIMIT,
    FUZZY_CANDIDATE_THRESHOLD,
    entity_name_similarity,
    normalized_entity_form,
)


class EntityLookup(Protocol):
    async def lookup(
        self,
        raw_text: str,
        entity_type: str,
    ) -> list[EntityLookupMatch]: ...


class StaticEntityLookup:
    """Tiny M3 fixture registry, not the evaluation entity dictionary."""

    def __init__(self, entities: list[CanonicalEntity] | None = None) -> None:
        self._entities = entities if entities is not None else _default_entities()

    async def lookup(
        self,
        raw_text: str,
        entity_type: str,
    ) -> list[EntityLookupMatch]:
        return (await self.lookup_with_diagnostics(raw_text, entity_type)).matches

    async def lookup_with_diagnostics(
        self,
        raw_text: str,
        entity_type: str,
    ) -> EntityLookupOutcome:
        normalized_query = normalized_entity_form(raw_text, entity_type)
        tiered_matches: list[tuple[int, EntityLookupMatch]] = []
        for entity in self._entities:
            if entity.entity_type != entity_type:
                continue
            candidates = [
                (entity.official_name, "official_name"),
                *((alias, "alias") for alias in entity.aliases),
                *((value, key) for key, value in entity.identifiers.items()),
            ]
            for value, identifier_type in candidates:
                normalized_candidate = normalized_entity_form(value, entity_type)
                if normalized_candidate == normalized_query:
                    exact = _normalize(value) == _normalize(raw_text)
                    method = (
                        "IDENTIFIER_MATCH"
                        if identifier_type not in {"official_name", "alias"}
                        else "EXACT_CANONICAL"
                        if exact and identifier_type == "official_name"
                        else "EXACT_ALIAS"
                        if exact and identifier_type == "alias"
                        else "NORMALIZED_EXACT"
                    )
                    tier = {
                        "EXACT_CANONICAL": 0,
                        "EXACT_ALIAS": 1,
                        "NORMALIZED_EXACT": 2,
                        "IDENTIFIER_MATCH": 3,
                    }[method]
                    tiered_matches.append((
                        tier,
                        EntityLookupMatch(
                            entity=entity,
                            matched_alias=value,
                            identifier_type=identifier_type,
                            match_method=method,
                            normalized_form=normalized_query,
                        ),
                    ))
                    break
        matches = []
        if tiered_matches:
            best_tier = min(item[0] for item in tiered_matches)
            matches = [item[1] for item in tiered_matches if item[0] == best_tier]
        if matches:
            ambiguous = len(matches) > 1
            return EntityLookupOutcome(
                matches=matches,
                candidates=[
                    _candidate(
                        match,
                        rejection_reason="AMBIGUOUS" if ambiguous else None,
                    )
                    for match in matches
                ],
            )

        ranked: list[EntityLookupMatch] = []
        for entity in self._entities:
            if entity.entity_type != entity_type:
                continue
            names = [
                (entity.official_name, "official_name"),
                *((alias, "alias") for alias in entity.aliases),
            ]
            scored = [
                (entity_name_similarity(raw_text, value, entity_type), value, kind)
                for value, kind in names
                if value
            ]
            if not scored:
                continue
            score, matched, identifier_type = max(scored)
            if score < FUZZY_CANDIDATE_THRESHOLD:
                continue
            ranked.append(EntityLookupMatch(
                entity=entity,
                matched_alias=matched,
                identifier_type=identifier_type,
                match_method="FUZZY_MATCH",
                normalized_form=normalized_query,
                match_score=score,
            ))
        ranked.sort(key=lambda item: (-item.match_score, item.entity.canonical_id))
        ranked = ranked[:FUZZY_CANDIDATE_LIMIT]
        accepted: list[EntityLookupMatch] = []
        ambiguous = False
        if ranked and ranked[0].match_score >= FUZZY_ACCEPTANCE_THRESHOLD:
            accepted = [
                item for item in ranked
                if ranked[0].match_score - item.match_score < FUZZY_AMBIGUITY_MARGIN
            ]
            ambiguous = len(accepted) > 1
        accepted_ids = {item.entity.canonical_id for item in accepted}
        return EntityLookupOutcome(
            matches=accepted,
            candidates=[
                _candidate(
                    item,
                    rejection_reason=(
                        "AMBIGUOUS"
                        if ambiguous and item.entity.canonical_id in accepted_ids
                        else None
                        if item.entity.canonical_id in accepted_ids
                        else "BELOW_THRESHOLD"
                    ),
                )
                for item in ranked
            ],
        )


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())


def _candidate(
    match: EntityLookupMatch,
    *,
    rejection_reason: str | None,
) -> EntityResolutionCandidate:
    return EntityResolutionCandidate(
        entity_id=match.entity.canonical_id,
        entity_type=match.entity.entity_type,
        canonical_name=match.entity.official_name,
        aliases=match.entity.aliases,
        normalized_form=(
            match.normalized_form
            or normalized_entity_form(match.matched_alias, match.entity.entity_type)
        ),
        match_method=match.match_method,
        match_score=match.match_score,
        rejection_reason=rejection_reason,
    )


def _default_entities() -> list[CanonicalEntity]:
    return [
        CanonicalEntity(
            canonical_id="TEST_PRODUCT_ALPHA",
            entity_type="product",
            official_name="테스트 ETF 알파",
            aliases=["알파ETF", "공통ETF"],
            identifiers={"ticker": "T0001", "isin": "KRTEST000001"},
        ),
        CanonicalEntity(
            canonical_id="TEST_PRODUCT_BETA",
            entity_type="product",
            official_name="테스트 ETF 베타",
            aliases=["베타ETF", "공통ETF"],
            identifiers={"ticker": "T0002", "isin": "KRTEST000002"},
        ),
        CanonicalEntity(
            canonical_id="TEST_INDEX_SP500",
            entity_type="index",
            official_name="S&P 500",
            aliases=["S&P500"],
        ),
    ]
