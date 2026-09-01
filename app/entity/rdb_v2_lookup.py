"""Exact/ambiguous entity lookup against canonical_v2 only."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import Engine, or_, select, true

from app.data.cleaning import normalize_lookup_value
from app.data.v2_schema import (
    canonical_entities,
    entity_aliases,
    entity_identifiers,
    financial_products,
    identifier_collision_cases,
)
from app.domain.models import CanonicalEntity, EntityLookupMatch
from app.retrieval.rdb_v2 import CanonicalV2SnapshotSelector


class CanonicalV2EntityLookup:
    """Resolve IDs, validated identifiers, preferred names, and aliases.

    It returns every candidate.  RegistryEntityResolver owns exact-one versus
    ambiguous status and collection search never uses this lookup.
    """

    _ENTITY_KINDS = {
        "product": {"FINANCIAL_PRODUCT"},
        "fund": {"FINANCIAL_PRODUCT"},
        "fund_share_class": {"FUND_SHARE_CLASS"},
        "sale_lot": {"SALE_LOT"},
        "management_company": {"ORGANIZATION"},
        "organization": {"ORGANIZATION"},
        "security": {"SECURITY"},
        "index": {"INDEX"},
    }

    def __init__(self, engine: Engine, selector: CanonicalV2SnapshotSelector) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("canonical_v2 entity lookup requires PostgreSQL")
        self._engine = engine
        self._selector = selector

    async def lookup(self, raw_text: str, entity_type: str) -> list[EntityLookupMatch]:
        return await asyncio.to_thread(self._lookup_sync, raw_text, entity_type)

    def _lookup_sync(self, raw_text: str, entity_type: str) -> list[EntityLookupMatch]:
        kinds = self._ENTITY_KINDS.get(entity_type)
        if kinds is None:
            return []
        normalized = normalize_lookup_value(raw_text)
        qualified_ticker = (
            re.fullmatch(r"([A-Z]{4}):([A-Z0-9.\-]{1,16})", raw_text.strip().upper())
            if entity_type == "security"
            else None
        )
        implicit_krx_ticker = (
            re.fullmatch(r"\d{6}", raw_text.strip())
            if entity_type == "security"
            else None
        )
        if (
            entity_type == "security"
            and qualified_ticker is None
            and implicit_krx_ticker is None
            and re.fullmatch(r"[A-Z]{1,5}", raw_text.strip().upper())
        ):
            # A bare foreign ticker has no exchange identity and must not bind
            # to whichever listing happens to be present in this snapshot.
            return []
        identifier_keys = {
            qualified_ticker.group(2)
            if qualified_ticker is not None
            else normalized,
            qualified_ticker.group(2)
            if qualified_ticker is not None
            else raw_text.strip().upper(),
        }
        eligibility = (
            true()
            if entity_type in {"organization", "security"}
            else canonical_entities.c.query_eligible.is_(True)
        )
        with self._engine.connect() as connection:
            self._selector.select(connection)
            collision_candidates = self._collision_candidates(
                connection,
                identifier_keys,
                namespace=(
                    qualified_ticker.group(1) if qualified_ticker
                    else "KRX" if implicit_krx_ticker
                    else None
                ),
            )
            identifier_conditions = [
                entity_identifiers.c.normalized_value.in_(identifier_keys),
                entity_identifiers.c.validation_status == "VALIDATED",
                entity_identifiers.c.resolution_status == "RESOLVED",
            ]
            if qualified_ticker is not None:
                identifier_conditions.extend((
                    entity_identifiers.c.scheme_code == "TICKER",
                    entity_identifiers.c.namespace == qualified_ticker.group(1),
                ))
            elif implicit_krx_ticker is not None:
                identifier_conditions.extend((
                    entity_identifiers.c.scheme_code == "TICKER",
                    entity_identifiers.c.namespace == "KRX",
                ))
            identifiers = connection.execute(
                select(
                    entity_identifiers.c.entity_id,
                    entity_identifiers.c.scheme_code,
                    entity_identifiers.c.raw_value,
                    entity_identifiers.c.conflict_status,
                ).where(*identifier_conditions)
            ).mappings().all()
            names = [] if qualified_ticker is not None else connection.execute(
                select(
                    canonical_entities.c.entity_id,
                    canonical_entities.c.preferred_name,
                    canonical_entities.c.entity_kind,
                    canonical_entities.c.name_status,
                ).where(
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                    or_(
                        canonical_entities.c.entity_id == raw_text,
                        canonical_entities.c.normalized_preferred_name == normalized,
                    ),
                )
            ).mappings().all()
            aliases = [] if qualified_ticker is not None else connection.execute(
                select(
                    entity_aliases.c.entity_id,
                    entity_aliases.c.alias,
                    entity_aliases.c.alias_type,
                )
                .join(
                    canonical_entities,
                    canonical_entities.c.entity_id == entity_aliases.c.entity_id,
                )
                .where(
                    entity_aliases.c.normalized_alias == normalized,
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                )
            ).mappings().all()

            candidate_ids = {
                str(row["entity_id"]) for row in (*names, *aliases, *identifiers)
            } | collision_candidates
            if entity_type == "fund" and candidate_ids:
                fund_ids = set(
                    connection.scalars(
                        select(financial_products.c.product_id).where(
                            financial_products.c.product_id.in_(candidate_ids),
                            financial_products.c.product_type_code == "FUND",
                        )
                    )
                )
                candidate_ids &= {str(value) for value in fund_ids}
            entity_rows = {
                str(row["entity_id"]): row
                for row in connection.execute(
                    select(canonical_entities).where(
                        canonical_entities.c.entity_id.in_(candidate_ids),
                        canonical_entities.c.entity_kind.in_(kinds),
                        eligibility,
                    )
                ).mappings()
            }
            alias_by_id: dict[str, list[str]] = {}
            if entity_rows:
                for row in connection.execute(
                    select(entity_aliases.c.entity_id, entity_aliases.c.alias)
                    .where(entity_aliases.c.entity_id.in_(entity_rows))
                    .order_by(entity_aliases.c.entity_id, entity_aliases.c.alias)
                ):
                    alias_by_id.setdefault(str(row.entity_id), []).append(str(row.alias))

        identifier_by_id = {str(row["entity_id"]): row for row in identifiers}
        matched_alias_by_id = {str(row["entity_id"]): row for row in aliases}
        matches: list[EntityLookupMatch] = []
        for entity_id in sorted(entity_rows):
            row = entity_rows[entity_id]
            identifier = identifier_by_id.get(entity_id)
            alias = matched_alias_by_id.get(entity_id)
            if entity_id == raw_text:
                matched, identifier_type = raw_text, "canonical_id"
            elif row["normalized_preferred_name"] == normalized:
                matched = row["preferred_name"]
                identifier_type = "preferred_name"
            elif alias is not None:
                matched = alias["alias"]
                identifier_type = str(alias["alias_type"])
            elif identifier is not None:
                matched = identifier["raw_value"]
                identifier_type = str(identifier["scheme_code"])
            else:
                # Candidate came from an authoritative open collision case.
                matched = raw_text
                identifier_type = "ambiguous_identifier"
            identifiers_for_entity = (
                {str(identifier["scheme_code"]): str(identifier["raw_value"])}
                if identifier is not None else {}
            )
            matches.append(
                EntityLookupMatch(
                    entity=CanonicalEntity(
                        canonical_id=entity_id,
                        entity_type=entity_type,
                        official_name=row["preferred_name"],
                        aliases=alias_by_id.get(entity_id, []),
                        identifiers=identifiers_for_entity,
                    ),
                    matched_alias=str(matched),
                    identifier_type=identifier_type,
                )
            )
        return matches

    @staticmethod
    def _collision_candidates(
        connection, normalized: set[str], *, namespace: str | None = None,
    ) -> set[str]:
        conditions = [
            identifier_collision_cases.c.normalized_value.in_(normalized),
            identifier_collision_cases.c.status == "OPEN",
        ]
        if namespace is not None:
            conditions.extend((
                identifier_collision_cases.c.scheme_code == "TICKER",
                identifier_collision_cases.c.namespace == namespace,
            ))
        rows = connection.execute(
            select(identifier_collision_cases.c.candidate_entity_ids).where(*conditions)
        ).scalars()
        result: set[str] = set()
        for values in rows:
            if isinstance(values, list):
                result.update(str(value) for value in values)
        return result
