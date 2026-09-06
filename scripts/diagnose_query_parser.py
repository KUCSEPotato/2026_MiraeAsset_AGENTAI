"""Bounded parser/planner diagnosis without opening any data store."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.domain.models import SemanticCoverageStatus
from app.entity.lookup import StaticEntityLookup
from app.entity.resolver import RegistryEntityResolver
from app.ontology.loader import OntologyLoader
from app.ontology.rdf_service import RDFOntologyService
from app.ontology.vocabulary import export_compact_semantic_vocabulary
from app.operations import OperationalSettings, configure_logging
from app.planning.coordinator import QueryPlanner
from app.planning.exceptions import UnsupportedQuerySemanticsError
from app.planning.metadata import RoutingMetadataRegistry
from app.planning.routing import FastRoutingChecker
from app.planning.rule_router import DeterministicRuleRouter
from app.planning.supervisor import DeterministicSupervisorPlanner
from app.planning.validator import StructuredQueryPlanValidator
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.config import HyperCLOVASemanticParserSettings
from app.query.exceptions import SemanticParseSafetyError
from app.query.llm_parser import HyperCLOVASemanticParserClient
from app.query.semantic_parser import SemanticParserCoordinator
from app.query.semantic_validation import LLMSemanticCandidateValidator
from app.retrieval.rdb_v2 import CanonicalV2FieldRegistry

QUESTIONS = [
    "채권 3개 보여줘.", "ETF 5개 보여줘.", "국내 ETF 5개 보여줘.",
    "만기가 2027년인 채권 5개 보여줘.",
    "채권을 만기일이 빠른 순서로 5개 보여줘.",
    "국내 ETF 중 순자산이 큰 순서로 5개 보여줘.",
]


async def diagnose(args):
    settings = HyperCLOVASemanticParserSettings.from_env()
    if args.live and not settings.configured:
        raise SystemExit("CLOVASTUDIO_API_KEY is required for --live; no request sent")
    ontology = RDFOntologyService(OntologyLoader(
        ROOT / "ontology", version="team-v1",
        known_canonical_fields=CanonicalV2FieldRegistry().canonical_fields,
    ).load())
    vocabulary = export_compact_semantic_vocabulary(ontology.index)
    metadata = RoutingMetadataRegistry()
    planner = QueryPlanner(routing_checker=FastRoutingChecker(metadata),
        rule_router=DeterministicRuleRouter(), supervisor_planner=DeterministicSupervisorPlanner(),
        plan_validator=StructuredQueryPlanValidator(metadata))
    class ProbeRule(RuleBasedQueryAnalyzer):
        async def analyze(self, question):
            parsed = await super().analyze(question)
            # Local diagnostic option only: retain all real rule constraints.
            return parsed.model_copy(update={"semantic_coverage": SemanticCoverageStatus.INCOMPLETE}) if args.force_llm else parsed
    llm = HyperCLOVASemanticParserClient(settings) if args.live else None
    coordinator = SemanticParserCoordinator(rule_parser=ProbeRule(), llm_parser=llm,
        candidate_validator=LLMSemanticCandidateValidator(vocabulary), compact_vocabulary=vocabulary)
    try:
        for question in args.questions:
            rule = await RuleBasedQueryAnalyzer().analyze(question)
            output = {"question": question, "store_execution": False,
                      "rule_coverage": rule.semantic_coverage.value,
                      "rule_residuals": [s.raw_text for s in rule.unparsed_material_spans]}
            try:
                parsed = await coordinator.analyze(question)
                output.update(parser=parsed.parser_source.value,
                    filters=[f.model_dump(mode="json") for f in parsed.filters],
                    sort=[s.model_dump(mode="json") for s in parsed.sort],
                    limit=parsed.result_limit.value if parsed.result_limit else None,
                    temporal=parsed.temporal_constraint.model_dump(mode="json") if parsed.temporal_constraint else None)
                resolved = await RegistryEntityResolver(StaticEntityLookup([])).resolve(parsed)
                plan = await planner.create_plan(await ontology.ground(resolved))
                output.update(status="planned", sources=[s.source.value for s in plan.steps])
            except SemanticParseSafetyError as exc:
                output.update(status="parser_failure", reason=exc.reason,
                              candidate_reasons=getattr(exc.__cause__, "reasons", []))
            except UnsupportedQuerySemanticsError as exc:
                output.update(status="unsupported", reasons=exc.reasons)
            print(json.dumps(output, ensure_ascii=False))
    finally:
        if llm is not None:
            await llm.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", dest="questions", action="append")
    parser.add_argument("--live", action="store_true", help="Allow one real HCX fallback per question; reads environment settings")
    parser.add_argument("--force-llm", action="store_true", help="With --live, probe HCX even for complete rule parses")
    args = parser.parse_args()
    args.questions = args.questions or QUESTIONS
    if len(args.questions) > 6:
        parser.error("at most six questions per run")
    if args.force_llm and not args.live:
        parser.error("--force-llm requires --live")
    configure_logging(OperationalSettings())
    asyncio.run(diagnose(args))
