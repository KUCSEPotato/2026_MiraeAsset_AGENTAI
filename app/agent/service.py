import json
import asyncio
from datetime import date
from time import perf_counter
from functools import lru_cache
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy import Engine, distinct, func, select

from app.agent.fakes import (
    FakeAnswerGenerator,
    FakeEntityResolver,
    FakeEvidenceValidator,
    FakeExecutor,
    FakeOntologyService,
    FakePlanner,
    FakeQueryAnalyzer,
    FakeSafeResponseGenerator,
)
from app.agent.interfaces import (
    AnswerGenerator,
    EntityResolver,
    EvidenceBuilder,
    EvidenceValidator,
    Executor,
    ExecutionResultExecutor,
    OntologyService,
    Planner,
    QueryAnalyzer,
    SafeResponseGenerator,
)
from app.domain.models import (
    AnswerabilityReasonCode,
    ParseProvenance,
    ResolutionStatus,
    RetrievalSource,
    ValidationResult,
)
from app.data.database import DatabaseSettings, create_database_engine
from app.data.holdings_coverage import KODEX_READY_SCOPE, TIGER_READY_SCOPE
from app.data.v2_schema import CANONICAL_V2_SCHEMA_VERSION
from app.data.v2_schema import (
    canonical_facts,
    dataset_snapshots,
    entity_relations,
    external_snapshot_manifests,
    fact_evidence_links,
    source_datasets,
)
from app.data.schema import metadata as database_metadata
from app.evidence.answer import DeterministicEvidenceAnswerGenerator
from app.evidence.llm_answer import (
    HyperCLOVAAnswerSettings,
    HyperCLOVAEvidenceAnswerGenerator,
)
from app.evidence.builder import GenericEvidenceBuilder
from app.evidence.quality import (
    CanonicalV2FieldQualityProvider,
    DatabaseFieldQualityProvider,
    FieldQualityProvider,
)
from app.evidence.safe_response import ReasonAwareSafeResponseGenerator
from app.evidence.serializer import serialize_evidence_bundle
from app.evidence.validator import QualityAwareEvidenceValidator
from app.entity.rdb_lookup import RDBEntityLookup
from app.entity.rdb_v2_lookup import CanonicalV2EntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.execution.config import ExecutionSettings
from app.execution.executor import QueryExecutor
from app.execution.transforms import InternalTransformExecutor
from app.graph.backend import Neo4jGraphBackend
from app.graph.compiler import GraphQueryCompiler
from app.graph.config import GraphSettings
from app.graph.mapping import GraphMappingRegistry
from app.graph.v2 import CanonicalV2GraphBackend, V2_GRAPH_NODE_LABEL
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.ontology.vocabulary import export_compact_semantic_vocabulary
from app.planning.coordinator import QueryPlanner
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.config import HyperCLOVASemanticParserSettings
from app.query.exceptions import SemanticParseSafetyError
from app.query.llm_parser import (
    HyperCLOVASemanticParserClient,
    SemanticParserLLM,
)
from app.query.semantic_parser import SemanticParserCoordinator
from app.query.semantic_validation import LLMSemanticCandidateValidator
from app.retrieval.fakes import FakeGraphRetriever
from app.retrieval.graph import RealGraphRetriever
from app.retrieval.rdb import RDBFieldRegistry, RDBQueryCompiler, RealRDBRetriever
from app.retrieval.rdb_v2 import (
    CanonicalV2FieldRegistry,
    CanonicalV2QueryCompiler,
    CanonicalV2RDBRetriever,
    CanonicalV2SnapshotSelector,
)
from app.retrieval.registry import RetrieverRegistry
from app.retrieval.semantic import RealBM25Retriever, RealVectorRetriever
from app.schemas.agent import AgentResult
from app.search.config import SearchSettings
from app.search.embedding import (
    DeterministicMultilingualEmbeddingProvider,
    EmbeddingProvider,
)
from app.search.store import SemanticIndexStore


class AnswerService(Protocol):
    async def answer(self, question: str) -> AgentResult:
        """Return an evidence-grounded answer result for a question."""
        ...


class PipelineAnswerService:
    """Orchestrate the agent pipeline without depending on concrete components."""

    def __init__(
        self,
        *,
        query_analyzer: QueryAnalyzer,
        entity_resolver: EntityResolver,
        ontology_service: OntologyService,
        planner: Planner,
        executor: Executor,
        evidence_builder: EvidenceBuilder,
        evidence_validator: EvidenceValidator,
        answer_generator: AnswerGenerator,
        safe_response_generator: SafeResponseGenerator,
        close_callbacks: list[Callable[[], Awaitable[None]]] | None = None,
        readiness_checks: list[tuple[str, Callable[[], Awaitable[None]]]] | None = None,
        runtime_metadata: dict[str, str] | None = None,
    ) -> None:
        self._query_analyzer = query_analyzer
        self._entity_resolver = entity_resolver
        self._ontology_service = ontology_service
        self._planner = planner
        self._executor = executor
        self._evidence_builder = evidence_builder
        self._evidence_validator = evidence_validator
        self._answer_generator = answer_generator
        self._safe_response_generator = safe_response_generator
        self._close_callbacks = close_callbacks or []
        self._readiness_checks = readiness_checks or []
        self._runtime_metadata = runtime_metadata or {
            "active_runtime_bundle": "canonical_v1",
            "compatibility_status": "READY",
        }

    async def validate_derived_stores(self) -> None:
        """Fail closed if the selected multi-store bundle is not coherent."""
        for store_name, check in self._readiness_checks:
            try:
                await check()
            except Exception:
                self._runtime_metadata[f"{store_name}_readiness"] = "FAILED"
                self._runtime_metadata["compatibility_status"] = "NOT_READY"
                raise
            self._runtime_metadata[f"{store_name}_readiness"] = "READY"
        self._runtime_metadata["compatibility_status"] = "READY"

    def runtime_health(self) -> dict[str, str]:
        """Safe, operational state only; credentials are never included."""
        return dict(self._runtime_metadata)

    async def close(self) -> None:
        for callback in reversed(self._close_callbacks):
            await callback()

    async def answer(self, question: str) -> AgentResult:
        request_started = perf_counter()
        trace: list[str] = []
        query_started = perf_counter()

        try:
            parsed_query = await self._query_analyzer.analyze(question)
        except SemanticParseSafetyError as exc:
            trace.extend(["query_understanding", "semantic_safety"])
            return await self._semantic_safety_result(
                question,
                trace,
                parser_summary={
                    "parser": exc.parser,
                    "status": "rejected",
                    "reason": exc.reason,
                    "llm_calls": 0 if exc.reason == "llm_fallback_not_configured" else 1,
                },
                total_started=request_started,
            )
        query_latency_ms = _elapsed_ms(query_started)
        trace.append("query_understanding")

        resolution_started = perf_counter()
        resolved_query = await self._entity_resolver.resolve(parsed_query)
        resolution_latency_ms = _elapsed_ms(resolution_started)
        trace.append("entity_resolution")

        ontology_started = perf_counter()
        grounded_query = await self._ontology_service.ground(resolved_query)
        ontology_latency_ms = _elapsed_ms(ontology_started)
        trace.append("ontology_grounding")

        planning_started = perf_counter()
        try:
            plan = await self._planner.create_plan(grounded_query)
        except UnsupportedQuerySemanticsError as exc:
            trace.append("semantic_safety")
            entity_reason = (
                AnswerabilityReasonCode.ENTITY_AMBIGUOUS
                if any(
                    item.resolution_status is ResolutionStatus.AMBIGUOUS
                    for item in resolved_query.resolved_entities
                )
                else AnswerabilityReasonCode.ENTITY_NOT_FOUND
                if any(
                    item.resolution_status is ResolutionStatus.UNRESOLVED
                    for item in resolved_query.resolved_entities
                )
                else AnswerabilityReasonCode.UNSUPPORTED_CONSTRAINT
            )
            return await self._semantic_safety_result(
                question,
                trace,
                parser_summary=_parser_summary(parsed_query.parse_provenance),
                total_started=request_started,
                ontology_latency_ms=ontology_latency_ms,
                planning_latency_ms=_elapsed_ms(planning_started),
                unsupported_details=[
                    reason
                    for reason in exc.reasons
                    if reason.startswith("unsupported_comparison:")
                ],
                reason_code=entity_reason,
            )
        planning_latency_ms = _elapsed_ms(planning_started)
        trace.append("planning")

        execution_started = perf_counter()
        execution_result = None
        if isinstance(self._executor, ExecutionResultExecutor):
            execution_result = await self._executor.execute_with_result(plan)
            records = execution_result.records
        else:
            records = await self._executor.execute(plan)
        execution_latency_ms = _elapsed_ms(execution_started)
        trace.append("execution")

        evidence_started = perf_counter()
        evidence = await self._evidence_builder.build(
            grounded_query,
            records,
            execution_result,
        )
        evidence_latency_ms = _elapsed_ms(evidence_started)
        trace.append("evidence_building")

        validation_started = perf_counter()
        validation = await self._evidence_validator.validate(
            grounded_query,
            evidence,
        )
        validation_latency_ms = _elapsed_ms(validation_started)
        trace.append("validation")

        answer_started = perf_counter()
        if validation.answerable:
            final_answer = await self._answer_generator.generate(
                question,
                evidence,
                validation,
            )
            trace.append("answer_generation")
            status = "success"
        else:
            final_answer = await self._safe_response_generator.generate(validation)
            trace.append("safe_response")
            status = "unanswerable"
        answer_latency_ms = _elapsed_ms(answer_started)

        return AgentResult(
            retrieved_context=serialize_evidence_bundle(evidence, validation),
            think_trace=json.dumps(
                {
                    "steps": trace,
                    "status": status,
                    "planner": plan.planner.value,
                    "planning_summary": {
                        "routing_reasons": [
                            reason.value for reason in plan.routing_reasons
                        ],
                        "sources": list(
                            dict.fromkeys(
                                step.source.value
                                for step in plan.steps
                                if step.source.value != "internal"
                            )
                        ),
                        "step_count": len(plan.steps),
                    },
                    "evidence_count": len(evidence.evidence),
                    "execution_cardinality": {
                        step_id: result.retrieval_metadata
                        for step_id, result in execution_result.step_results.items()
                        if result.retrieval_metadata
                    } if execution_result is not None else {},
                    "validation_reasons": validation.reasons,
                    "validation_summary": {
                        "answerable": validation.answerable,
                        "reason_codes": [
                            code.value for code in validation.reason_codes
                        ],
                    },
                    "llm_call_summary": {
                        "semantic_parser_calls": (
                            1 if parsed_query.parser_source.value == "llm_fallback" else 0
                        ),
                        "answer_generation_calls": (
                            int(getattr(self._answer_generator, "model_calls_per_answer", 0))
                            if validation.answerable
                            else 0
                        ),
                        "retries": 0,
                    },
                    "performance_ms": {
                        "query_understanding": query_latency_ms,
                        "entity_resolution": resolution_latency_ms,
                        "ontology_grounding": ontology_latency_ms,
                        "planning": planning_latency_ms,
                        "execution": execution_latency_ms,
                        "evidence_building": evidence_latency_ms,
                        "validation": validation_latency_ms,
                        "answer_generation": answer_latency_ms,
                        "total": _elapsed_ms(request_started),
                    },
                    "semantic_summary": {
                        "parser": parsed_query.parser_source.value,
                        "parse": _parser_summary(parsed_query.parse_provenance),
                        "intent": parsed_query.intent.value,
                        "entity_resolution_statuses": [
                            entity.resolution_status.value
                            for entity in resolved_query.resolved_entities
                        ],
                        "canonical_concepts": [
                            concept.value
                            for concept in grounded_query.canonical_concepts
                        ],
                        "canonical_fields": grounded_query.canonical_fields,
                        "unresolved_concepts": grounded_query.unresolved_concepts,
                        "grounded_relations": [
                            {
                                "raw_text": relation.raw_text,
                                "canonical_relation": relation.canonical_relation,
                                "direction": relation.direction.value,
                                "status": relation.status.value,
                            }
                            for relation in grounded_query.grounded_relations
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            answer=final_answer,
        )

    async def _semantic_safety_result(
        self,
        question: str,
        trace: list[str],
        *,
        parser_summary: dict[str, object],
        total_started: float,
        ontology_latency_ms: float = 0.0,
        planning_latency_ms: float = 0.0,
        unsupported_details: list[str] | None = None,
        reason_code: AnswerabilityReasonCode = (
            AnswerabilityReasonCode.UNSUPPORTED_CONSTRAINT
        ),
    ) -> AgentResult:
        details = list(dict.fromkeys(unsupported_details or []))
        validation = ValidationResult(
            answerable=False,
            reason_codes=[reason_code],
            reasons=[reason_code.value, *details],
        )
        final_answer = await self._safe_response_generator.generate(validation)
        return AgentResult(
            retrieved_context=json.dumps(
                {
                    "question": question,
                    "validation": {
                        "answerable": False,
                        "reason_codes": [
                            reason_code.value
                        ],
                        "reasons": details,
                    },
                },
                ensure_ascii=False,
            ),
            think_trace=json.dumps(
                {
                    "steps": trace,
                    "status": "unsupported",
                    "reason": "unsupported_constraint",
                    "validation_summary": {
                        "answerable": False,
                        "reason_codes": [
                            reason_code.value
                        ],
                        "reasons": details,
                    },
                    "query_understanding": parser_summary,
                    "llm_call_summary": {
                        "semantic_parser_calls": (
                            int(parser_summary.get("llm_calls", 0))
                        ),
                        "answer_generation_calls": 0,
                        "retries": 0,
                    },
                    "performance_ms": {
                        "ontology_grounding": ontology_latency_ms,
                        "planning": planning_latency_ms,
                        "total": _elapsed_ms(total_started),
                    },
                },
                ensure_ascii=False,
            ),
            answer=final_answer,
        )


def create_pipeline_answer_service(
    *,
    executor: Executor | None = None,
    answer_generator: AnswerGenerator | None = None,
) -> PipelineAnswerService:
    """Compose the isolated M2 fake pipeline for tests and simulations."""
    return PipelineAnswerService(
        query_analyzer=FakeQueryAnalyzer(),
        entity_resolver=FakeEntityResolver(),
        ontology_service=FakeOntologyService(),
        planner=FakePlanner(),
        executor=executor or FakeExecutor(),
        evidence_builder=GenericEvidenceBuilder(),
        evidence_validator=FakeEvidenceValidator(),
        answer_generator=answer_generator or FakeAnswerGenerator(),
        safe_response_generator=FakeSafeResponseGenerator(),
    )


def create_production_answer_service(
    *,
    executor: Executor | None = None,
    quality_provider: FieldQualityProvider | None = None,
    answer_generator: AnswerGenerator | None = None,
    safe_response_generator: SafeResponseGenerator | None = None,
    database_engine: Engine | None = None,
    database_settings: DatabaseSettings | None = None,
    ontology_service: OntologyService | None = None,
    ontology_loader: OntologyLoader | None = None,
    search_settings: SearchSettings | None = None,
    embedding_provider: EmbeddingProvider | None = None,
    semantic_index_store: SemanticIndexStore | None = None,
    graph_settings: GraphSettings | None = None,
    graph_backend: Neo4jGraphBackend | None = None,
    semantic_parser_llm: SemanticParserLLM | None = None,
    semantic_parser_settings: HyperCLOVASemanticParserSettings | None = None,
    hyperclova_answer_settings: HyperCLOVAAnswerSettings | None = None,
) -> PipelineAnswerService:
    """Compose the production semantic, retrieval, and evidence pipeline."""
    if database_settings is not None:
        settings = database_settings
    elif database_engine is not None:
        settings = DatabaseSettings(
            database_url=database_engine.url.render_as_string(
                hide_password=False
            )
        )
    else:
        settings = DatabaseSettings.from_env()
    semantic_settings = search_settings or SearchSettings.from_env()
    resolved_graph_settings = graph_settings or GraphSettings.from_env()
    _assert_runtime_bundle_configuration(
        settings, semantic_settings, resolved_graph_settings
    )
    routing_metadata = RoutingMetadataRegistry()
    planner = QueryPlanner(
        routing_checker=FastRoutingChecker(routing_metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(
            candidate_limit=semantic_settings.candidate_limit
        ),
        plan_validator=StructuredQueryPlanValidator(routing_metadata),
    )
    engine = database_engine or create_database_engine(settings)
    if settings.rdb_repository_version == "v1":
        database_metadata.create_all(engine)

    v2_snapshot_selector = CanonicalV2SnapshotSelector(
        snapshot_date=settings.snapshot_date,
        generation=settings.v2_generation,
        ontology_version=settings.v2_ontology_version,
        transformer_version=settings.v2_transformer_version,
        include_trusted_holdings=settings.trusted_holdings_runtime_enabled,
        trusted_holdings_scopes=settings.trusted_holdings_scopes,
        include_trusted_issuers=settings.trusted_issuer_runtime_enabled,
        trusted_issuer_scope=settings.trusted_issuer_scope,
    )

    if ontology_service is None:
        loader = ontology_loader or OntologyLoader(
            Path(__file__).resolve().parents[2] / "ontology",
            known_canonical_fields=(
                CanonicalV2FieldRegistry().canonical_fields
                if settings.rdb_repository_version == "v2"
                else RDBFieldRegistry().canonical_fields
            ),
            version=resolved_graph_settings.ontology_version,
        )
        ontology_service = RDFOntologyService(loader.load())

    close_callbacks: list[Callable[[], Awaitable[None]]] = []
    readiness_checks: list[tuple[str, Callable[[], Awaitable[None]]]] = []
    runtime_metadata = {
        "active_runtime_bundle": settings.runtime_bundle.name,
        "generation": settings.v2_generation if settings.runtime_bundle.uses_canonical_v2 else "legacy",
        "snapshot": settings.snapshot_date,
        "ontology_version": settings.v2_ontology_version if settings.runtime_bundle.uses_canonical_v2 else resolved_graph_settings.ontology_version,
        "canonical_schema_version": CANONICAL_V2_SCHEMA_VERSION if settings.runtime_bundle.uses_canonical_v2 else "m10.7-canonical-v1",
        "transformer_version": settings.v2_transformer_version if settings.runtime_bundle.uses_canonical_v2 else "legacy",
        "rdb_readiness": "PENDING" if settings.runtime_bundle.uses_canonical_v2 else "READY",
        "graph_readiness": "PENDING" if settings.runtime_bundle.uses_canonical_v2 else "READY",
        "semantic_index_readiness": "PENDING" if settings.runtime_bundle.uses_canonical_v2 else "READY",
        "holdings_readiness": (
            "PENDING"
            if settings.runtime_bundle.uses_canonical_v2
            and settings.trusted_holdings_runtime_enabled
            else "DISABLED"
        ),
        "issuer_source_readiness": (
            "PENDING"
            if settings.runtime_bundle.uses_canonical_v2
            and settings.trusted_issuer_runtime_enabled
            else "DISABLED"
        ),
        "canonical_issuer_readiness": (
            "PENDING"
            if settings.runtime_bundle.uses_canonical_v2
            and settings.trusted_issuer_runtime_enabled
            else "DISABLED"
        ),
        "graph_issuer_readiness": (
            "PENDING"
            if settings.runtime_bundle.uses_canonical_v2
            and settings.trusted_issuer_runtime_enabled
            else "DISABLED"
        ),
        "company_query_readiness": (
            "PENDING"
            if settings.runtime_bundle.uses_canonical_v2
            and settings.trusted_issuer_runtime_enabled
            else "DISABLED"
        ),
        "graph_projection_version": resolved_graph_settings.v2_graph_projection_version if settings.runtime_bundle.uses_canonical_v2 else resolved_graph_settings.graph_version,
        "semantic_projection_version": semantic_settings.v2_index_version if settings.runtime_bundle.uses_canonical_v2 else semantic_settings.index_version,
        "compatibility_status": "PENDING" if settings.runtime_bundle.uses_canonical_v2 else "READY",
    }

    parser_settings = (
        semantic_parser_settings or HyperCLOVASemanticParserSettings.from_env()
    )
    llm_parser = semantic_parser_llm
    if llm_parser is None and parser_settings.configured:
        hyperclova_client = HyperCLOVASemanticParserClient(parser_settings)
        llm_parser = hyperclova_client
        close_callbacks.append(hyperclova_client.close)
    ontology_index = (
        ontology_service.index
        if isinstance(ontology_service, RDFOntologyService)
        else None
    )
    compact_vocabulary = export_compact_semantic_vocabulary(ontology_index)
    query_analyzer = SemanticParserCoordinator(
        rule_parser=RuleBasedQueryAnalyzer(),
        llm_parser=llm_parser,
        candidate_validator=LLMSemanticCandidateValidator(compact_vocabulary),
        compact_vocabulary=compact_vocabulary,
    )

    resolved_answer_generator = answer_generator
    if resolved_answer_generator is None:
        answer_settings = (
            hyperclova_answer_settings or HyperCLOVAAnswerSettings.from_env()
        )
        answer_settings.validate()
        if answer_settings.enabled:
            hyperclova_answer = HyperCLOVAEvidenceAnswerGenerator(answer_settings)
            resolved_answer_generator = hyperclova_answer
            close_callbacks.append(hyperclova_answer.close)
        else:
            resolved_answer_generator = DeterministicEvidenceAnswerGenerator()

    if executor is None:
        retriever_registry = RetrieverRegistry()
        if settings.rdb_repository_version == "v2":
            v2_compiler = CanonicalV2QueryCompiler(
                CanonicalV2FieldRegistry(),
                default_limit=settings.rdb_default_limit,
                max_limit=settings.rdb_max_limit,
            )
            retriever_registry.register(
                RetrievalSource.RDB,
                CanonicalV2RDBRetriever(
                    engine, v2_compiler, v2_snapshot_selector
                ),
            )
        else:
            compiler = RDBQueryCompiler(
                RDBFieldRegistry(),
                default_limit=settings.rdb_default_limit,
                snapshot_date=settings.snapshot_date,
                max_limit=settings.rdb_max_limit,
            )
            retriever_registry.register(
                RetrievalSource.RDB,
                RealRDBRetriever(engine, compiler),
            )
        if settings.rdb_repository_version == "v2" and settings.v2_multi_store_enabled:
            # v2 is only a complete set: canonical_v2 RDB + canonical_v2
            # graph + canonical_v2 semantic corpus.  No v1 fallback exists.
            if not resolved_graph_settings.configured:
                raise ValueError(
                    "canonical_v2 multi-store mode requires an isolated Neo4j configuration"
                )
            backend = graph_backend or CanonicalV2GraphBackend.connect(
                resolved_graph_settings
            )
            graph_compiler = GraphQueryCompiler(
                GraphMappingRegistry(version="canonical-v2"),
                snapshot=settings.snapshot_date,
                max_depth=resolved_graph_settings.max_depth,
                limit=resolved_graph_settings.query_limit,
                node_label=V2_GRAPH_NODE_LABEL,
            )
            retriever_registry.register(
                RetrievalSource.GRAPH,
                RealGraphRetriever(backend, graph_compiler, snapshot=settings.snapshot_date),
            )
            close_callbacks.append(backend.close)
            provider = embedding_provider or DeterministicMultilingualEmbeddingProvider(
                model_name=semantic_settings.embedding_model,
                dimension=semantic_settings.embedding_dimension,
            )
            store = semantic_index_store or SemanticIndexStore(
                semantic_settings.v2_index_path
            )
            retriever_registry.register(
                RetrievalSource.VECTOR,
                RealVectorRetriever(
                    store, provider, semantic_settings,
                    snapshot_date=settings.snapshot_date, canonical_v2=True,
                ),
            )
            retriever_registry.register(
                RetrievalSource.BM25,
                RealBM25Retriever(
                    store, semantic_settings,
                    snapshot_date=settings.snapshot_date, canonical_v2=True,
                ),
            )
            async def assert_v2_graph_ready() -> None:
                manifest = await backend.assert_ready(expected_snapshot=settings.snapshot_date)
                if settings.trusted_holdings_runtime_enabled:
                    rdb_holds = await asyncio.to_thread(
                        _v2_holds_count, engine, v2_snapshot_selector
                    )
                    graph_holds = int(manifest.relation_counts.get("HOLDS", 0))
                    if rdb_holds <= 0 or graph_holds != rdb_holds:
                        raise RuntimeError(
                            "C2 HOLDS PostgreSQL/Neo4j reconciliation failed"
                        )

            async def assert_v2_rdb_ready() -> None:
                await asyncio.to_thread(_assert_v2_rdb_ready, engine, v2_snapshot_selector)

            async def assert_v2_holdings_ready() -> None:
                await asyncio.to_thread(
                    _assert_v2_holdings_ready, engine, settings.trusted_holdings_scopes
                )

            async def assert_v2_issuer_source_ready() -> None:
                await asyncio.to_thread(
                    _assert_v2_issuer_source_ready, engine, settings.trusted_issuer_scope
                )

            async def assert_v2_canonical_issuer_ready() -> None:
                await asyncio.to_thread(
                    _assert_v2_canonical_issuer_ready, engine, v2_snapshot_selector
                )

            async def assert_v2_graph_issuer_ready() -> None:
                manifest = await backend.assert_ready(
                    expected_snapshot=settings.snapshot_date
                )
                rdb_issuers = await asyncio.to_thread(
                    _v2_issuer_count, engine, v2_snapshot_selector
                )
                graph_issuers = int(
                    manifest.relation_counts.get("SECURITY_ISSUED_BY", 0)
                )
                if rdb_issuers <= 0 or graph_issuers != rdb_issuers:
                    raise RuntimeError(
                        "C2.6 SECURITY_ISSUED_BY PostgreSQL/Neo4j reconciliation failed"
                    )

            async def assert_v2_company_query_ready() -> None:
                await assert_v2_graph_issuer_ready()

            async def assert_v2_semantic_ready() -> None:
                await asyncio.to_thread(
                    store.validate_derived_manifest,
                    generation=semantic_settings.v2_generation,
                    snapshot=settings.snapshot_date,
                    ontology_version=semantic_settings.v2_ontology_version,
                    canonical_schema_version=CANONICAL_V2_SCHEMA_VERSION,
                    transformer_version=semantic_settings.v2_transformer_version,
                    projection_version=semantic_settings.v2_index_version,
                )

            readiness_checks.extend([
                ("rdb", assert_v2_rdb_ready),
                *(
                    [("holdings", assert_v2_holdings_ready)]
                    if settings.trusted_holdings_runtime_enabled
                    else []
                ),
                *(
                    [
                        ("issuer_source", assert_v2_issuer_source_ready),
                        ("canonical_issuer", assert_v2_canonical_issuer_ready),
                    ]
                    if settings.trusted_issuer_runtime_enabled
                    else []
                ),
                ("graph", assert_v2_graph_ready),
                *(
                    [
                        ("graph_issuer", assert_v2_graph_issuer_ready),
                        ("company_query", assert_v2_company_query_ready),
                    ]
                    if settings.trusted_issuer_runtime_enabled
                    else []
                ),
                ("semantic_index", assert_v2_semantic_ready),
            ])
        elif settings.rdb_repository_version == "v1" and resolved_graph_settings.configured:
            backend = graph_backend or Neo4jGraphBackend.connect(
                resolved_graph_settings
            )
            ontology_index = (
                ontology_service.index
                if isinstance(ontology_service, RDFOntologyService)
                else None
            )
            graph_mapping = GraphMappingRegistry(
                ontology_index,
                version=resolved_graph_settings.ontology_version,
            )
            graph_compiler = GraphQueryCompiler(
                graph_mapping,
                snapshot=settings.snapshot_date,
                max_depth=resolved_graph_settings.max_depth,
                limit=resolved_graph_settings.query_limit,
            )
            retriever_registry.register(
                RetrievalSource.GRAPH,
                RealGraphRetriever(
                    backend,
                    graph_compiler,
                    snapshot=settings.snapshot_date,
                ),
            )
            close_callbacks.append(backend.close)
        elif settings.rdb_repository_version == "v1":
            retriever_registry.register(
                RetrievalSource.GRAPH, FakeGraphRetriever()
            )
        if settings.rdb_repository_version == "v1":
            provider = embedding_provider or DeterministicMultilingualEmbeddingProvider(
                model_name=semantic_settings.embedding_model,
                dimension=semantic_settings.embedding_dimension,
            )
            store = semantic_index_store or SemanticIndexStore(
                semantic_settings.index_path
            )
            retriever_registry.register(
                RetrievalSource.VECTOR,
                RealVectorRetriever(
                    store,
                    provider,
                    semantic_settings,
                    snapshot_date=settings.snapshot_date,
                ),
            )
            retriever_registry.register(
                RetrievalSource.BM25,
                RealBM25Retriever(
                    store,
                    semantic_settings,
                    snapshot_date=settings.snapshot_date,
                ),
            )
        executor = QueryExecutor(
            registry=retriever_registry,
            transform_executor=InternalTransformExecutor(),
            settings=ExecutionSettings.from_env(),
        )
    field_quality = quality_provider or (
        CanonicalV2FieldQualityProvider(engine)
        if settings.rdb_repository_version == "v2"
        else DatabaseFieldQualityProvider(
            engine, snapshot_date=settings.snapshot_date
        )
    )
    entity_lookup = (
        CanonicalV2EntityLookup(engine, v2_snapshot_selector)
        if settings.rdb_repository_version == "v2"
        else RDBEntityLookup(engine, snapshot_date=settings.snapshot_date)
    )
    return PipelineAnswerService(
        query_analyzer=query_analyzer,
        entity_resolver=RegistryEntityResolver(entity_lookup),
        ontology_service=ontology_service,
        planner=planner,
        executor=executor,
        evidence_builder=GenericEvidenceBuilder(),
        evidence_validator=QualityAwareEvidenceValidator(field_quality),
        answer_generator=resolved_answer_generator,
        safe_response_generator=(
            safe_response_generator or ReasonAwareSafeResponseGenerator()
        ),
        close_callbacks=close_callbacks,
        readiness_checks=readiness_checks,
        runtime_metadata=runtime_metadata,
    )


@lru_cache(maxsize=1)
def get_answer_service() -> AnswerService:
    return create_production_answer_service()


def _elapsed_ms(started: float) -> float:
    return round((perf_counter() - started) * 1000.0, 3)


def _parser_summary(provenance: ParseProvenance) -> dict[str, object]:
    return {
        "parser": provenance.parser_source.value,
        "status": provenance.validation_status,
        "constraints_schema": provenance.semantic_schema_version,
        "model": provenance.model,
        "llm_calls": 1 if provenance.parser_source.value == "llm_fallback" else 0,
    }


def _assert_v2_rdb_ready(
    engine: Engine, selector: CanonicalV2SnapshotSelector
) -> None:
    """Validate the four READY/PASSED source snapshots before serving v2."""
    with engine.connect() as connection:
        selector.select(connection)


def _v2_holds_count(
    engine: Engine, selector: CanonicalV2SnapshotSelector | None = None,
) -> int:
    with engine.connect() as connection:
        snapshot_ids = selector.select(connection).snapshot_ids if selector else None
        return int(connection.scalar(
            select(func.count())
            .select_from(entity_relations.join(
                canonical_facts,
                canonical_facts.c.fact_id == entity_relations.c.fact_id,
            ))
            .where(
                entity_relations.c.relation_type == "HOLDS",
                canonical_facts.c.resolution_status == "RESOLVED",
                *(
                    (canonical_facts.c.snapshot_id.in_(snapshot_ids),)
                    if snapshot_ids is not None else ()
                ),
            )
        ) or 0)


def _v2_issuer_count(
    engine: Engine, selector: CanonicalV2SnapshotSelector | None = None,
) -> int:
    with engine.connect() as connection:
        snapshot_ids = selector.select(connection).snapshot_ids if selector else None
        return int(connection.scalar(
            select(func.count())
            .select_from(entity_relations.join(
                canonical_facts,
                canonical_facts.c.fact_id == entity_relations.c.fact_id,
            ))
            .where(
                entity_relations.c.relation_type == "SECURITY_ISSUED_BY",
                canonical_facts.c.resolution_status == "RESOLVED",
                *(
                    (canonical_facts.c.snapshot_id.in_(snapshot_ids),)
                    if snapshot_ids is not None else ()
                ),
            )
        ) or 0)


def _assert_v2_holdings_ready(
    engine: Engine,
    expected_scopes: tuple[str, ...] = (KODEX_READY_SCOPE,),
) -> None:
    """Fail closed unless trusted source, canonical facts and evidence agree."""

    with engine.connect() as connection:
        manifest_scopes = set(connection.execute(
            select(external_snapshot_manifests.c.manifest_json["scope"].as_string())
            .where(
                external_snapshot_manifests.c.status == "READY",
                external_snapshot_manifests.c.data_cutoff_date
                == date.fromisoformat("2026-08-24"),
                external_snapshot_manifests.c.manifest_json["scope"].as_string()
                .in_(expected_scopes),
            )
        ).scalars().all())
        facts = int(connection.scalar(
            select(func.count()).select_from(entity_relations).where(
                entity_relations.c.relation_type == "HOLDS"
            )
        ) or 0)
        evidenced = int(connection.scalar(
            select(func.count(distinct(entity_relations.c.fact_id)))
            .select_from(
                entity_relations.join(
                    fact_evidence_links,
                    fact_evidence_links.c.fact_id == entity_relations.c.fact_id,
                ).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_relations.c.fact_id,
                )
            )
            .where(entity_relations.c.relation_type == "HOLDS")
        ) or 0)
    if manifest_scopes != set(expected_scopes) or facts <= 0 or evidenced != facts:
        raise RuntimeError(
            "trusted holdings runtime is not canonical-data READY"
        )


def _assert_v2_issuer_source_ready(
    engine: Engine, expected_scope: str = KODEX_READY_SCOPE,
) -> None:
    with engine.connect() as connection:
        count = int(connection.scalar(
            select(func.count())
            .select_from(
                external_snapshot_manifests
                .join(
                    dataset_snapshots,
                    dataset_snapshots.c.snapshot_id
                    == external_snapshot_manifests.c.canonical_snapshot_id,
                )
                .join(
                    source_datasets,
                    source_datasets.c.dataset_id == dataset_snapshots.c.dataset_id,
                )
            )
            .where(
                source_datasets.c.dataset_code == "KRX_SECURITY_ISSUER",
                external_snapshot_manifests.c.status == "READY",
                external_snapshot_manifests.c.data_cutoff_date
                == date.fromisoformat("2026-08-24"),
                dataset_snapshots.c.metadata_json["scope"].as_string()
                == expected_scope,
            )
        ) or 0)
    if count != 1:
        raise RuntimeError("authoritative issuer source is not READY")


def _assert_v2_canonical_issuer_ready(
    engine: Engine, selector: CanonicalV2SnapshotSelector | None = None,
) -> None:
    with engine.connect() as connection:
        snapshot_ids = selector.select(connection).snapshot_ids if selector else None
        facts = int(connection.scalar(
            select(func.count())
            .select_from(entity_relations.join(
                canonical_facts,
                canonical_facts.c.fact_id == entity_relations.c.fact_id,
            ))
            .where(
                entity_relations.c.relation_type == "SECURITY_ISSUED_BY",
                canonical_facts.c.resolution_status == "RESOLVED",
                *(
                    (canonical_facts.c.snapshot_id.in_(snapshot_ids),)
                    if snapshot_ids is not None else ()
                ),
            )
        ) or 0)
        evidenced = int(connection.scalar(
            select(func.count(distinct(entity_relations.c.fact_id)))
            .select_from(
                entity_relations.join(
                    fact_evidence_links,
                    fact_evidence_links.c.fact_id == entity_relations.c.fact_id,
                ).join(
                    canonical_facts,
                    canonical_facts.c.fact_id == entity_relations.c.fact_id,
                )
            )
            .where(
                entity_relations.c.relation_type == "SECURITY_ISSUED_BY",
                *(
                    (canonical_facts.c.snapshot_id.in_(snapshot_ids),)
                    if snapshot_ids is not None else ()
                ),
            )
        ) or 0)
    if facts <= 0 or evidenced != facts:
        raise RuntimeError("canonical issuer relations are not evidence READY")


def _assert_runtime_bundle_configuration(
    database: DatabaseSettings,
    semantic: SearchSettings,
    graph: GraphSettings,
) -> None:
    """Reject version drift before any v2 store can be selected."""
    if not database.runtime_bundle.uses_canonical_v2:
        return
    expected = {
        "generation": database.v2_generation,
        "ontology": database.v2_ontology_version,
        "transformer": database.v2_transformer_version,
    }
    actual = {
        "graph.generation": graph.v2_generation,
        "graph.ontology": graph.v2_ontology_version,
        "graph.transformer": graph.v2_transformer_version,
        "semantic.generation": semantic.v2_generation,
        "semantic.ontology": semantic.v2_ontology_version,
        "semantic.transformer": semantic.v2_transformer_version,
    }
    mismatches = [
        f"{name}={value!r} expected {expected[key]!r}"
        for name, value, key in (
            ("graph.generation", actual["graph.generation"], "generation"),
            ("graph.ontology", actual["graph.ontology"], "ontology"),
            ("graph.transformer", actual["graph.transformer"], "transformer"),
            ("semantic.generation", actual["semantic.generation"], "generation"),
            ("semantic.ontology", actual["semantic.ontology"], "ontology"),
            ("semantic.transformer", actual["semantic.transformer"], "transformer"),
        )
        if value != expected[key]
    ]
    if mismatches:
        raise ValueError(
            "canonical_v2 runtime bundle configuration is incompatible: "
            + "; ".join(mismatches)
        )
