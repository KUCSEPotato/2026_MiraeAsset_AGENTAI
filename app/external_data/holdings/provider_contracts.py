"""Reviewed provider contracts for trusted external holdings.

Provider-specific identifiers and source semantics stop at this boundary.  The
canonical relation remains FinancialProduct --HOLDS--> Security for every
provider.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class HoldingsProviderContract:
    provider: str
    product_identifier_namespace: str
    security_identifier_namespace: str
    dataset_id: str
    dataset_code: str
    display_name: str
    schema_contract_version: str
    coverage_scope: str
    canonical_snapshot_id: str
    semantic_mapping_version: str
    transformer_version: str


KODEX_CONTRACT = HoldingsProviderContract(
    provider="Samsung Asset Management KODEX",
    product_identifier_namespace="KODEX",
    security_identifier_namespace="KODEX_SECURITY",
    dataset_id="dataset:kodex-holdings",
    dataset_code="KODEX_HOLDINGS",
    display_name="KODEX long-only compatible holdings",
    schema_contract_version="external-kodex-holdings-scope-v1",
    coverage_scope="KODEX_LONG_ONLY_COMPATIBLE",
    canonical_snapshot_id="snapshot:kodex-long-only:20260824:v1",
    semantic_mapping_version="c2.5-step3-ready-scope-v1",
    transformer_version="m10.9-c2-kodex-holdings-1",
)

TIGER_CONTRACT = HoldingsProviderContract(
    provider="Mirae Asset Management TIGER",
    product_identifier_namespace="TIGER",
    security_identifier_namespace="TIGER_SECURITY",
    dataset_id="dataset:tiger-holdings",
    dataset_code="TIGER_HOLDINGS",
    display_name="TIGER long-only compatible holdings",
    schema_contract_version="external-tiger-holdings-scope-v1",
    coverage_scope="TIGER_LONG_ONLY_COMPATIBLE",
    canonical_snapshot_id="snapshot:tiger-long-only:20260824:v1",
    semantic_mapping_version="c2.7-tiger-ready-scope-v1",
    transformer_version="m10.9-c2.7-multi-provider-holdings-1",
)

ISHARES_CONTRACT = HoldingsProviderContract(
    provider="BlackRock iShares",
    product_identifier_namespace="ISHARES_US",
    security_identifier_namespace="ISHARES_HOLDING",
    dataset_id="dataset:ishares-us-holdings",
    dataset_code="ISHARES_US_HOLDINGS",
    display_name="iShares US foreign-ETF reviewed security holdings",
    schema_contract_version="external-ishares-us-holdings-scope-v1",
    coverage_scope="ISHARES_US_FOREIGN_ETF_SECURITY_HOLDINGS",
    canonical_snapshot_id="snapshot:ishares-us-foreign-etf:20260824:v1",
    semantic_mapping_version="c2.8-ishares-us-ready-scope-v1",
    transformer_version="m10.9-c2.8-foreign-etf-holdings-1",
)

PROVIDER_CONTRACTS = {
    item.provider: item for item in (KODEX_CONTRACT, TIGER_CONTRACT, ISHARES_CONTRACT)
}


def provider_contract(provider: str) -> HoldingsProviderContract:
    try:
        return PROVIDER_CONTRACTS[provider]
    except KeyError as exc:
        raise ValueError(f"unreviewed holdings provider: {provider}") from exc
