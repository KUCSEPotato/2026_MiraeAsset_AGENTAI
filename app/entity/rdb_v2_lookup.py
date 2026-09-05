"""Exact/ambiguous entity lookup against canonical_v2 only."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from sqlalchemy import Engine, or_, select, true
from sqlalchemy.exc import SQLAlchemyError

from app.data.cleaning import normalize_lookup_value
from app.data.v2_schema import (
    canonical_entities,
    canonical_facts,
    entity_aliases,
    entity_identifiers,
    financial_products,
    identifier_collision_cases,
    organization_relations,
)
from app.domain.models import (
    CanonicalEntity,
    EntityLookupMatch,
    EntityLookupOutcome,
    EntityResolutionCandidate,
)
from app.entity.normalization import (
    organization_identity_compatible,
    FUZZY_ACCEPTANCE_THRESHOLD,
    FUZZY_AMBIGUITY_MARGIN,
    FUZZY_CANDIDATE_LIMIT,
    FUZZY_CANDIDATE_THRESHOLD,
    entity_lookup_keys,
    entity_name_similarity,
    normalized_entity_form,
)
from app.entity.exceptions import EntityResolutionDependencyError
from app.retrieval.rdb_v2 import CanonicalV2SnapshotSelector


class CanonicalV2EntityLookup:
    """Resolve IDs, validated identifiers, preferred names, and aliases.

    It returns every candidate.  RegistryEntityResolver owns exact-one versus
    ambiguous status and collection search never uses this lookup.
    """

    _ENTITY_KINDS = {
        "product": {"FINANCIAL_PRODUCT"},
        "financial_product": {"FINANCIAL_PRODUCT"},
        "fund": {"FINANCIAL_PRODUCT"},
        "fund_share_class": {"FUND_SHARE_CLASS"},
        "sale_lot": {"SALE_LOT"},
        "management_company": {"ORGANIZATION"},
        "asset_manager": {"ORGANIZATION"},
        "company": {"ORGANIZATION"},
        "holding_company": {"ORGANIZATION"},
        "institution": {"ORGANIZATION"},
        "issuer": {"ORGANIZATION"},
        "organization": {"ORGANIZATION"},
        "portfolio_company": {"ORGANIZATION"},
        "subsidiary": {"ORGANIZATION"},
        "security": {"SECURITY"},
        "holding": {"SECURITY"},
        "index": {"INDEX"},
    }

    def __init__(self, engine: Engine, selector: CanonicalV2SnapshotSelector) -> None:
        if engine.dialect.name != "postgresql":
            raise ValueError("canonical_v2 entity lookup requires PostgreSQL")
        self._engine = engine
        self._selector = selector

    async def lookup(self, raw_text: str, entity_type: str) -> list[EntityLookupMatch]:
        outcome = await self.lookup_with_diagnostics(raw_text, entity_type)
        return outcome.matches

    async def lookup_with_diagnostics(
        self, raw_text: str, entity_type: str,
    ) -> EntityLookupOutcome:
        try:
            return await asyncio.to_thread(
                self._lookup_with_diagnostics_sync, raw_text, entity_type
            )
        except SQLAlchemyError as exc:
            raise EntityResolutionDependencyError(
                "canonical entity lookup failed"
            ) from exc

    def _lookup_with_diagnostics_sync(
        self, raw_text: str, entity_type: str,
    ) -> EntityLookupOutcome:
        exact = self._lookup_sync(raw_text, entity_type)
        if exact:
            ambiguous = len(exact) > 1
            return EntityLookupOutcome(
                matches=exact,
                candidates=[
                    self._diagnostic(
                        item,
                        rejection_reason="AMBIGUOUS" if ambiguous else None,
                    )
                    for item in exact
                ],
            )
        return self._fuzzy_lookup_sync(raw_text, entity_type)

    def _lookup_sync(self, raw_text: str, entity_type: str) -> list[EntityLookupMatch]:
        kinds = self._ENTITY_KINDS.get(entity_type)
        if kinds is None:
            return []
        normalized = normalize_lookup_value(raw_text)
        lookup_keys = set(entity_lookup_keys(raw_text, entity_type))
        normalized_form = normalized_entity_form(raw_text, entity_type)
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
            if kinds in ({"ORGANIZATION"}, {"SECURITY"})
            else canonical_entities.c.query_eligible.is_(True)
        )
        with self._engine.connect() as connection:
            snapshot = self._selector.select(connection)
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
                    canonical_entities.c.normalized_preferred_name,
                    canonical_entities.c.entity_kind,
                    canonical_entities.c.name_status,
                ).where(
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                    or_(
                        canonical_entities.c.entity_id == raw_text,
                        canonical_entities.c.normalized_preferred_name.in_(lookup_keys),
                    ),
                )
            ).mappings().all()
            aliases = [] if qualified_ticker is not None else connection.execute(
                select(
                    entity_aliases.c.entity_id,
                    entity_aliases.c.alias,
                    entity_aliases.c.normalized_alias,
                    entity_aliases.c.alias_type,
                )
                .join(
                    canonical_entities,
                    canonical_entities.c.entity_id == entity_aliases.c.entity_id,
                )
                .where(
                    entity_aliases.c.normalized_alias.in_(lookup_keys),
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                )
            ).mappings().all()

            canonical_name_ids = {
                str(row["entity_id"])
                for row in names
                if row["entity_id"] == raw_text
                or row["normalized_preferred_name"] == normalized
            }
            exact_alias_ids = {
                str(row["entity_id"])
                for row in aliases
                if row["normalized_alias"] == normalized
            }
            normalized_ids = {
                str(row["entity_id"]) for row in (*names, *aliases)
            }
            identifier_ids = {
                str(row["entity_id"]) for row in identifiers
            } | collision_candidates
            candidate_ids = next(
                (
                    values
                    for values in (
                        canonical_name_ids,
                        exact_alias_ids,
                        normalized_ids,
                        identifier_ids,
                    )
                    if values
                ),
                set(),
            )
            names = [
                row for row in names if str(row["entity_id"]) in candidate_ids
            ]
            aliases = [
                row for row in aliases if str(row["entity_id"]) in candidate_ids
            ]
            identifiers = [
                row for row in identifiers if str(row["entity_id"]) in candidate_ids
            ]
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
            candidate_ids = self._role_scoped_ids(
                connection, candidate_ids, entity_type, snapshot.snapshot_ids
            )
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
            if not organization_identity_compatible(raw_text, row["preferred_name"], entity_type):
                continue
            identifier = identifier_by_id.get(entity_id)
            alias = matched_alias_by_id.get(entity_id)
            if entity_id == raw_text:
                matched, identifier_type = raw_text, "canonical_id"
                match_method = "EXACT_CANONICAL"
            elif row["normalized_preferred_name"] in lookup_keys:
                matched = row["preferred_name"]
                identifier_type = "preferred_name"
                match_method = (
                    "EXACT_CANONICAL"
                    if row["normalized_preferred_name"] == normalized
                    else "NORMALIZED_EXACT"
                )
            elif alias is not None:
                matched = alias["alias"]
                identifier_type = str(alias["alias_type"])
                match_method = (
                    "EXACT_ALIAS"
                    if alias["normalized_alias"] == normalized
                    else "NORMALIZED_EXACT"
                )
            elif identifier is not None:
                matched = identifier["raw_value"]
                identifier_type = str(identifier["scheme_code"])
                match_method = "IDENTIFIER_MATCH"
            else:
                # Candidate came from an authoritative open collision case.
                matched = raw_text
                identifier_type = "ambiguous_identifier"
                match_method = "IDENTIFIER_MATCH"
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
                    match_method=match_method,
                    normalized_form=normalized_form,
                )
            )
        return matches

    def _fuzzy_lookup_sync(
        self, raw_text: str, entity_type: str,
    ) -> EntityLookupOutcome:
        """Generate typed lexical candidates, then accept only a clear winner."""

        kinds = self._ENTITY_KINDS.get(entity_type)
        query_form = normalized_entity_form(raw_text, entity_type)
        minimum_length = 3 if kinds == {"ORGANIZATION"} else 5
        if kinds is None or len(query_form) < minimum_length:
            return EntityLookupOutcome()
        # This is candidate generation only.  Short deterministic boundary
        # fragments bound the portable SQL query; identity is decided below
        # from the full normalized label and a conservative score/margin.
        storage_form = normalize_lookup_value(raw_text)
        fragments = {
            value
            for value in (storage_form[:3], storage_form[-3:])
            if len(value) == 3
        }
        if not fragments:
            return EntityLookupOutcome()
        name_fragment_filter = or_(*(
            canonical_entities.c.normalized_preferred_name.contains(
                fragment, autoescape=True
            )
            for fragment in sorted(fragments)
        ))
        alias_fragment_filter = or_(*(
            entity_aliases.c.normalized_alias.contains(
                fragment, autoescape=True
            )
            for fragment in sorted(fragments)
        ))
        eligibility = (
            true()
            if kinds in ({"ORGANIZATION"}, {"SECURITY"})
            else canonical_entities.c.query_eligible.is_(True)
        )
        with self._engine.connect() as connection:
            snapshot = self._selector.select(connection)
            name_ids = list(connection.scalars(
                select(canonical_entities.c.entity_id)
                .where(
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                    name_fragment_filter,
                )
                .order_by(canonical_entities.c.entity_id)
                .limit(1000)
            ))
            alias_ids = list(connection.scalars(
                select(entity_aliases.c.entity_id)
                .join(
                    canonical_entities,
                    canonical_entities.c.entity_id == entity_aliases.c.entity_id,
                )
                .where(
                    canonical_entities.c.entity_kind.in_(kinds),
                    eligibility,
                    alias_fragment_filter,
                )
                .order_by(entity_aliases.c.entity_id)
                .limit(1000)
            ))
            candidate_ids = {str(value) for value in (*name_ids, *alias_ids)}
            if entity_type == "fund" and candidate_ids:
                candidate_ids &= {
                    str(value) for value in connection.scalars(
                        select(financial_products.c.product_id).where(
                            financial_products.c.product_id.in_(candidate_ids),
                            financial_products.c.product_type_code == "FUND",
                        )
                    )
                }
            candidate_ids = self._role_scoped_ids(
                connection, candidate_ids, entity_type, snapshot.snapshot_ids
            )
            rows = {
                str(row["entity_id"]): row
                for row in connection.execute(
                    select(canonical_entities).where(
                        canonical_entities.c.entity_id.in_(candidate_ids),
                        canonical_entities.c.entity_kind.in_(kinds),
                        eligibility,
                    )
                ).mappings()
            }
            aliases_by_id: dict[str, list[str]] = {}
            if rows:
                for row in connection.execute(
                    select(entity_aliases.c.entity_id, entity_aliases.c.alias)
                    .where(entity_aliases.c.entity_id.in_(rows))
                    .order_by(entity_aliases.c.entity_id, entity_aliases.c.alias)
                ):
                    aliases_by_id.setdefault(str(row.entity_id), []).append(
                        str(row.alias)
                    )

        ranked: list[EntityLookupMatch] = []
        for entity_id, row in rows.items():
            if not organization_identity_compatible(raw_text, row["preferred_name"], entity_type):
                continue
            aliases = aliases_by_id.get(entity_id, [])
            labels = [value for value in (row["preferred_name"], *aliases) if value]
            if not labels:
                continue
            score, label = max(
                (entity_name_similarity(raw_text, str(value), entity_type), str(value))
                for value in labels
            )
            if score < FUZZY_CANDIDATE_THRESHOLD:
                continue
            ranked.append(EntityLookupMatch(
                entity=CanonicalEntity(
                    canonical_id=entity_id,
                    entity_type=entity_type,
                    official_name=row["preferred_name"],
                    aliases=aliases,
                ),
                matched_alias=label,
                identifier_type=(
                    "preferred_name" if label == row["preferred_name"] else "alias"
                ),
                match_method=(
                    "NORMALIZED_EXACT" if score == 1.0 else "FUZZY_MATCH"
                ),
                normalized_form=query_form,
                match_score=score,
            ))
        ranked.sort(key=lambda item: (-item.match_score, item.entity.canonical_id))
        ranked = ranked[:FUZZY_CANDIDATE_LIMIT]
        accepted: list[EntityLookupMatch] = []
        if ranked and ranked[0].match_score >= FUZZY_ACCEPTANCE_THRESHOLD:
            accepted = [
                item for item in ranked
                if ranked[0].match_score - item.match_score < FUZZY_AMBIGUITY_MARGIN
            ]
        accepted_ids = {item.entity.canonical_id for item in accepted}
        ambiguous = len(accepted) > 1
        return EntityLookupOutcome(
            matches=accepted,
            candidates=[
                self._diagnostic(
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

    @staticmethod
    def _role_scoped_ids(
        connection,
        candidate_ids: set[str],
        entity_type: str,
        snapshot_ids: tuple[str, ...],
    ) -> set[str]:
        """Apply an ontology-compatible organization role before scoring.

        A short provider label can normalize to several organizations.  A
        management-company mention may bind only to an organization that is
        actually the target of a current MANAGED_BY canonical fact.  This is
        a generic relation-role constraint, not a provider-name preference.
        """

        if entity_type not in {"management_company", "asset_manager"}:
            return candidate_ids
        if not candidate_ids:
            return set()
        return {
            str(value)
            for value in connection.scalars(
                select(organization_relations.c.organization_id)
                .join(
                    canonical_facts,
                    canonical_facts.c.fact_id == organization_relations.c.fact_id,
                )
                .where(
                    organization_relations.c.organization_id.in_(candidate_ids),
                    organization_relations.c.relation_type == "MANAGED_BY",
                    canonical_facts.c.snapshot_id.in_(snapshot_ids),
                    canonical_facts.c.resolution_status == "RESOLVED",
                )
                .distinct()
            )
        }

    @staticmethod
    def _diagnostic(
        match: EntityLookupMatch, *, rejection_reason: str | None,
    ) -> EntityResolutionCandidate:
        return EntityResolutionCandidate(
            entity_id=match.entity.canonical_id,
            entity_type=match.entity.entity_type,
            canonical_name=match.entity.official_name,
            aliases=match.entity.aliases,
            normalized_form=match.normalized_form or "",
            match_method=match.match_method,
            match_score=match.match_score,
            rejection_reason=rejection_reason,
        )

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
