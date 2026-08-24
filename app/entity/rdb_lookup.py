import asyncio

from sqlalchemy import Engine, distinct, or_, select
from sqlalchemy.exc import SQLAlchemyError

from app.data.cleaning import normalize_lookup_value
from app.data.schema import canonical_products, funds, product_identifiers
from app.domain.models import CanonicalEntity, EntityLookupMatch
from app.graph.identity import explicit_source_id, source_scoped_name_id


class RDBEntityLookup:
    """Resolve exact names and identifiers from the canonical product master."""

    def __init__(self, engine: Engine, *, snapshot_date: str) -> None:
        self._engine = engine
        self._snapshot_date = snapshot_date

    async def lookup(
        self,
        raw_text: str,
        entity_type: str,
    ) -> list[EntityLookupMatch]:
        if entity_type == "product":
            return await asyncio.to_thread(self._lookup_product_sync, raw_text)
        if entity_type == "fund":
            return await asyncio.to_thread(self._lookup_fund_sync, raw_text)
        if entity_type == "management_company":
            return await asyncio.to_thread(self._lookup_manager_sync, raw_text)
        return []

    def _lookup_product_sync(self, raw_text: str) -> list[EntityLookupMatch]:
        normalized = normalize_lookup_value(raw_text)
        try:
            with self._engine.connect() as connection:
                name_rows = connection.execute(
                    select(canonical_products).where(
                        canonical_products.c.dataset_snapshot
                        == self._snapshot_date,
                        or_(
                            canonical_products.c.normalized_product_name
                            == normalized,
                            canonical_products.c.normalized_short_name
                            == normalized,
                        ),
                    )
                ).mappings().all()
                identifier_rows = connection.execute(
                    select(
                        product_identifiers.c.canonical_product_id,
                        product_identifiers.c.identifier_type,
                        product_identifiers.c.identifier_value,
                    ).where(
                        product_identifiers.c.dataset_snapshot
                        == self._snapshot_date,
                        product_identifiers.c.normalized_value == normalized,
                    )
                ).mappings().all()
                identifier_by_id = {
                    row["canonical_product_id"]: row for row in identifier_rows
                }
                name_by_id = {
                    row["canonical_product_id"]: row for row in name_rows
                }
                missing_ids = set(identifier_by_id) - set(name_by_id)
                if missing_ids:
                    extra_rows = connection.execute(
                        select(canonical_products).where(
                            canonical_products.c.dataset_snapshot
                            == self._snapshot_date,
                            canonical_products.c.canonical_product_id.in_(
                                missing_ids
                            ),
                        )
                    ).mappings()
                    name_by_id.update(
                        {row["canonical_product_id"]: row for row in extra_rows}
                    )
        except SQLAlchemyError:
            raise

        matches: list[EntityLookupMatch] = []
        for canonical_id in sorted(name_by_id):
            row = name_by_id[canonical_id]
            identifier = identifier_by_id.get(canonical_id)
            official_match = row["normalized_product_name"] == normalized
            short_match = row["normalized_short_name"] == normalized
            if official_match:
                matched_alias = row["product_name"]
                identifier_type = "official_name"
            elif short_match:
                matched_alias = row["short_name"]
                identifier_type = "short_name"
            elif identifier is not None:
                matched_alias = identifier["identifier_value"]
                identifier_type = identifier["identifier_type"]
            else:
                continue
            matches.append(
                EntityLookupMatch(
                    entity=CanonicalEntity(
                        canonical_id=canonical_id,
                        entity_type="product",
                        official_name=row["product_name"],
                        aliases=(
                            [row["short_name"]] if row["short_name"] else []
                        ),
                        identifiers=(
                            {
                                identifier["identifier_type"]: identifier[
                                    "identifier_value"
                                ]
                            }
                            if identifier is not None
                            else {}
                        ),
                    ),
                    matched_alias=matched_alias,
                    identifier_type=identifier_type,
                )
            )
        return matches

    def _lookup_fund_sync(self, raw_text: str) -> list[EntityLookupMatch]:
        normalized = normalize_lookup_value(raw_text)
        with self._engine.connect() as connection:
            rows = connection.execute(
                select(
                    funds.c.fund_id,
                    funds.c.fund_name,
                    funds.c.source_fund_id,
                ).where(funds.c.dataset_snapshot == self._snapshot_date)
            ).mappings()
            matches = [
                row
                for row in rows
                if normalize_lookup_value(str(row["fund_name"])) == normalized
                or normalize_lookup_value(str(row["source_fund_id"]))
                == normalized
            ]
        return [
            EntityLookupMatch(
                entity=CanonicalEntity(
                    canonical_id=row["fund_id"],
                    entity_type="fund",
                    official_name=row["fund_name"],
                    identifiers={"source_fund_id": row["source_fund_id"]},
                ),
                matched_alias=(
                    row["fund_name"]
                    if normalize_lookup_value(str(row["fund_name"])) == normalized
                    else row["source_fund_id"]
                ),
                identifier_type=(
                    "official_name"
                    if normalize_lookup_value(str(row["fund_name"])) == normalized
                    else "source_fund_id"
                ),
            )
            for row in sorted(matches, key=lambda item: item["fund_id"])
        ]

    def _lookup_manager_sync(self, raw_text: str) -> list[EntityLookupMatch]:
        normalized = normalize_lookup_value(raw_text)
        with self._engine.connect() as connection:
            exchange_rows = connection.execute(
                select(
                    distinct(canonical_products.c.asset_manager).label("label"),
                    canonical_products.c.source_dataset,
                ).where(
                    canonical_products.c.dataset_snapshot == self._snapshot_date,
                    canonical_products.c.asset_manager.is_not(None),
                )
            ).mappings()
            fund_rows = connection.execute(
                select(distinct(canonical_products.c.issuer).label("code")).where(
                    canonical_products.c.dataset_snapshot == self._snapshot_date,
                    canonical_products.c.source_dataset == "public_fund",
                    canonical_products.c.issuer.is_not(None),
                )
            ).mappings()
            candidates: dict[str, EntityLookupMatch] = {}
            for row in exchange_rows:
                label = str(row["label"])
                if normalize_lookup_value(label) != normalized:
                    continue
                source = str(row["source_dataset"])
                canonical_id = source_scoped_name_id(
                    "asset_manager", source, label
                )
                candidates[canonical_id] = EntityLookupMatch(
                    entity=CanonicalEntity(
                        canonical_id=canonical_id,
                        entity_type="management_company",
                        official_name=label,
                    ),
                    matched_alias=label,
                    identifier_type="source_scoped_exact_name",
                )
            for row in fund_rows:
                code = str(row["code"])
                if normalize_lookup_value(code) != normalized:
                    continue
                canonical_id = explicit_source_id(
                    "asset_manager", "public_fund", code
                )
                candidates[canonical_id] = EntityLookupMatch(
                    entity=CanonicalEntity(
                        canonical_id=canonical_id,
                        entity_type="management_company",
                        official_name=code,
                        identifiers={"external_institution_code": code},
                    ),
                    matched_alias=code,
                    identifier_type="external_institution_code",
                )
        return [candidates[key] for key in sorted(candidates)]
