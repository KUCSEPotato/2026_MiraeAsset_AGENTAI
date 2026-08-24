from typing import Protocol

from app.domain.models import CanonicalEntity, EntityLookupMatch


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
        normalized_query = _normalize(raw_text)
        matches: list[EntityLookupMatch] = []
        for entity in self._entities:
            if entity.entity_type != entity_type:
                continue
            candidates = [
                (entity.official_name, "official_name"),
                *((alias, "alias") for alias in entity.aliases),
                *((value, key) for key, value in entity.identifiers.items()),
            ]
            for value, identifier_type in candidates:
                if _normalize(value) == normalized_query:
                    matches.append(
                        EntityLookupMatch(
                            entity=entity,
                            matched_alias=value,
                            identifier_type=identifier_type,
                        )
                    )
                    break
        return matches


def _normalize(value: str) -> str:
    return "".join(value.casefold().split())


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
    ]

