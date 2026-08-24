import json
import os
from time import perf_counter
from functools import lru_cache
from pathlib import Path
from collections.abc import Awaitable, Callable
from typing import Protocol

from sqlalchemy import Engine

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
    RetrievalSource,
    ValidationResult,
)
from app.data.database import DatabaseSettings, create_database_engine
from app.data.schema import metadata as database_metadata
from app.evidence.answer import DeterministicEvidenceAnswerGenerator
from app.evidence.builder import GenericEvidenceBuilder
from app.evidence.quality import (
    DatabaseFieldQualityProvider,
    FieldQualityProvider,
    StaticFieldQualityProvider,
)
from app.evidence.safe_response import ReasonAwareSafeResponseGenerator
from app.evidence.serializer import serialize_evidence_bundle
from app.evidence.validator import QualityAwareEvidenceValidator
from app.entity.lookup import StaticEntityLookup
from app.entity.rdb_lookup import RDBEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.execution.config import ExecutionSettings
from app.execution.executor import QueryExecutor
from app.execution.transforms import InternalTransformExecutor
from app.graph.backend import Neo4jGraphBackend
from app.graph.compiler import GraphQueryCompiler
from app.graph.config import GraphSettings
from app.graph.mapping import GraphMappingRegistry
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
from app.retrieval.fakes import (
    FakeBM25Retriever,
    FakeGraphRetriever,
    FakeRDBRetriever,
    FakeVectorRetriever,
)
from app.retrieval.graph import RealGraphRetriever
from app.retrieval.rdb import RDBFieldRegistry, RDBQueryCompiler, RealRDBRetriever
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

    async def close(self) -> None:
        for callback in reversed(self._close_callbacks):
            await callback()

    async def answer(self, question: str) -> AgentResult:
        request_started = perf_counter()
        trace: list[str] = []

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
                },
                total_started=request_started,
            )
        trace.append("query_understanding")

        resolved_query = await self._entity_resolver.resolve(parsed_query)
        trace.append("entity_resolution")

        ontology_started = perf_counter()
        grounded_query = await self._ontology_service.ground(resolved_query)
        ontology_latency_ms = _elapsed_ms(ontology_started)
        trace.append("ontology_grounding")

        planning_started = perf_counter()
        try:
            plan = await self._planner.create_plan(grounded_query)
        except UnsupportedQuerySemanticsError:
            trace.append("semantic_safety")
            return await self._semantic_safety_result(
                question,
                trace,
                parser_summary=_parser_summary(parsed_query.parse_provenance),
                total_started=request_started,
                ontology_latency_ms=ontology_latency_ms,
                planning_latency_ms=_elapsed_ms(planning_started),
            )
        planning_latency_ms = _elapsed_ms(planning_started)
        trace.append("planning")

        execution_result = None
        if isinstance(self._executor, ExecutionResultExecutor):
            execution_result = await self._executor.execute_with_result(plan)
            records = execution_result.records
        else:
            records = await self._executor.execute(plan)
        trace.append("execution")

        evidence = await self._evidence_builder.build(
            grounded_query,
            records,
            execution_result,
        )
        trace.append("evidence_building")

        validation = await self._evidence_validator.validate(
            grounded_query,
            evidence,
        )
        trace.append("validation")

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
                    "validation_reasons": validation.reasons,
                    "validation_summary": {
                        "answerable": validation.answerable,
                        "reason_codes": [
                            code.value for code in validation.reason_codes
                        ],
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
    ) -> AgentResult:
        validation = ValidationResult(
            answerable=False,
            reason_codes=[AnswerabilityReasonCode.UNSUPPORTED_QUERY_SEMANTICS],
            reasons=[AnswerabilityReasonCode.UNSUPPORTED_QUERY_SEMANTICS.value],
        )
        final_answer = await self._safe_response_generator.generate(validation)
        return AgentResult(
            retrieved_context=json.dumps(
                {
                    "question": question,
                    "validation": {
                        "answerable": False,
                        "reason_codes": [
                            AnswerabilityReasonCode.UNSUPPORTED_QUERY_SEMANTICS.value
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            think_trace=json.dumps(
                {
                    "steps": trace,
                    "status": "unsupported",
                    "reason": "semantic_constraints_incomplete",
                    "query_understanding": parser_summary,
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
) -> PipelineAnswerService:
    """Compose the production semantic, retrieval, and evidence pipeline."""
    settings = database_settings or DatabaseSettings.from_env()
    semantic_settings = search_settings or SearchSettings.from_env()
    resolved_graph_settings = graph_settings or GraphSettings.from_env()
    routing_metadata = RoutingMetadataRegistry()
    planner = QueryPlanner(
        routing_checker=FastRoutingChecker(routing_metadata),
        rule_router=DeterministicRuleRouter(),
        supervisor_planner=DeterministicSupervisorPlanner(
            candidate_limit=semantic_settings.candidate_limit
        ),
        plan_validator=StructuredQueryPlanValidator(routing_metadata),
    )
    use_real_rdb = database_engine is not None or bool(os.getenv("DATABASE_URL"))
    engine = database_engine
    if use_real_rdb and engine is None:
        engine = create_database_engine(settings)
    if engine is not None:
        database_metadata.create_all(engine)

    if ontology_service is None:
        loader = ontology_loader or OntologyLoader(
            Path(__file__).resolve().parents[2] / "ontology",
            known_canonical_fields=RDBFieldRegistry().canonical_fields,
        )
        ontology_service = RDFOntologyService(loader.load())

    close_callbacks: list[Callable[[], Awaitable[None]]] = []

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

    if executor is None:
        retriever_registry = RetrieverRegistry()
        if use_real_rdb and engine is not None:
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
        else:
            retriever_registry.register(RetrievalSource.RDB, FakeRDBRetriever())
        if resolved_graph_settings.configured:
            backend = graph_backend or Neo4jGraphBackend.connect(
                resolved_graph_settings
            )
            ontology_index = (
                ontology_service.index
                if isinstance(ontology_service, RDFOntologyService)
                else None
            )
            graph_mapping = GraphMappingRegistry(ontology_index)
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
        else:
            retriever_registry.register(
                RetrievalSource.GRAPH, FakeGraphRetriever()
            )
        if use_real_rdb:
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
        else:
            retriever_registry.register(
                RetrievalSource.VECTOR, FakeVectorRetriever()
            )
            retriever_registry.register(RetrievalSource.BM25, FakeBM25Retriever())
        executor = QueryExecutor(
            registry=retriever_registry,
            transform_executor=InternalTransformExecutor(),
            settings=ExecutionSettings.from_env(),
        )
    field_quality = quality_provider or (
        DatabaseFieldQualityProvider(engine, snapshot_date=settings.snapshot_date)
        if use_real_rdb and engine is not None
        else StaticFieldQualityProvider()
    )
    entity_lookup = (
        RDBEntityLookup(engine, snapshot_date=settings.snapshot_date)
        if use_real_rdb and engine is not None
        else StaticEntityLookup()
    )
    return PipelineAnswerService(
        query_analyzer=query_analyzer,
        entity_resolver=RegistryEntityResolver(entity_lookup),
        ontology_service=ontology_service,
        planner=planner,
        executor=executor,
        evidence_builder=GenericEvidenceBuilder(),
        evidence_validator=QualityAwareEvidenceValidator(field_quality),
        answer_generator=answer_generator
        or (
            DeterministicEvidenceAnswerGenerator()
            if use_real_rdb
            else FakeAnswerGenerator()
        ),
        safe_response_generator=(
            safe_response_generator or ReasonAwareSafeResponseGenerator()
        ),
        close_callbacks=close_callbacks,
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
    }
