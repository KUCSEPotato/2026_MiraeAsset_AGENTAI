from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.domain.models import SemanticStorageIdentity
from app.ontology.runtime_mapping import ONTOLOGY_NAMESPACE


@dataclass(frozen=True, slots=True)
class FundShareClassRuntimeMetadata:
    identity: SemanticStorageIdentity
    parent_fund_id: str
    share_class_id: str
    offering_type_uri: str
    offering_type: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_row_id": self.identity.storage_row_id,
            "fund_share_class_id": self.share_class_id,
            "parent_fund_id": self.parent_fund_id,
            "ontology_entity_type": self.identity.ontology_entity_type,
            "ontology_uri": self.identity.ontology_uri,
            "compatibility_product_type": (
                self.identity.compatibility_product_type
            ),
            "offering_type": self.offering_type,
            "offering_type_uri": self.offering_type_uri,
            "provenance_semantics": (
                "SourceRecord→describesEntity→FundShareClass; "
                "SourceRecord→supportsEntity→Fund"
            ),
            "ontology_gap": None,
        }


class FundShareClassStorageAdapter:
    """M10.7 bridge; it does not change the canonical PostgreSQL grain."""

    PUBLIC_COMPATIBILITY_TYPE = "FinancialProduct.PublicFund"

    def adapt(
        self,
        canonical_row: Mapping[str, Any],
        fund_class_row: Mapping[str, Any],
    ) -> FundShareClassRuntimeMetadata:
        storage_id = str(canonical_row["canonical_product_id"])
        parent_fund_id = str(fund_class_row["fund_id"])
        compatibility_type = str(canonical_row["product_type"])
        raw_offering = str(fund_class_row.get("public_private") or "").strip()
        if raw_offering == "공모":
            offering = "OfferingType.PUBLIC"
        elif raw_offering == "사모":
            offering = "OfferingType.PRIVATE"
        elif compatibility_type == self.PUBLIC_COMPATIBILITY_TYPE:
            offering = "OfferingType.PUBLIC"
        else:
            raise ValueError("fund share class offering type is unresolved")
        offering_resource = "PUBLIC" if offering.endswith("PUBLIC") else "PRIVATE"
        identity = SemanticStorageIdentity(
            storage_row_id=storage_id,
            ontology_entity_type="FundShareClass",
            ontology_uri=f"{ONTOLOGY_NAMESPACE}FundShareClass",
            parent_entity_id=parent_fund_id,
            compatibility_product_type=compatibility_type,
        )
        return FundShareClassRuntimeMetadata(
            identity=identity,
            parent_fund_id=parent_fund_id,
            share_class_id=storage_id,
            offering_type_uri=f"{ONTOLOGY_NAMESPACE}{offering_resource}",
            offering_type=offering,
        )
