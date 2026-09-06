"""Defaults may fill parameters, never replace selection semantics or evidence."""
import asyncio
from copy import deepcopy
import json

import httpx
import pytest

from app.domain.models import SemanticCoverageStatus
from app.evidence.safe_response import ReasonAwareSafeResponseGenerator
from app.ontology.vocabulary import export_compact_semantic_vocabulary
from app.query.analyzer import RuleBasedQueryAnalyzer
from app.query.candidate_normalization import normalize_candidate_payload
from app.query.config import HyperCLOVASemanticParserSettings
from app.query.exceptions import SemanticCandidateValidationError, SemanticParseSafetyError
from app.query.llm_parser import HyperCLOVASemanticParserClient
from app.query.semantic_models import LLMSemanticParseCandidate
from app.query.semantic_parser import SemanticParserCoordinator
from app.query.semantic_validation import LLMSemanticCandidateValidator
from tests.test_clause_answerability import _answer, _record
from tests.test_m10_9_c1_structured_operations import _ontology


def _parse(q):
    return asyncio.run(RuleBasedQueryAnalyzer().analyze(q))


def _span(q, raw):
    start = q.index(raw)
    return dict(start=start, end=start + len(raw), raw_text=raw)


def _candidate(q, sort='수익률 좋은'):
    return dict(intent='search_product', product_types=[dict(value='ETF', source_span=_span(q, 'ETF'))],
                sorts=[dict(field='수익률', direction='desc', source_span=_span(q, sort))])


def _validate(q, data):
    return LLMSemanticCandidateValidator(export_compact_semantic_vocabulary(_ontology().index)).validate(
        q, _parse(q), LLMSemanticParseCandidate.model_validate(data), model='test',
        rule_latency_ms=0, llm_latency_ms=0, prompt_version='test', schema_version='test')


@pytest.mark.parametrize('question', [
    '수익률 좋은 ETF 알려줘', '국내 ETF 중 수익률 좋은 상품 알려줘',
    '안전하고 수익률 좋은 ETF 알려줘',
])
def test_return_defaults_are_recorded_without_fake_entities_or_domestic_scope(question):
    parsed = _parse(question)
    assert not parsed.entities
    assert parsed.metrics[0].temporal.period == '1Y'
    assert parsed.result_limit.value == 5
    assert parsed.sort[0].direction == 'desc'
    policies = {p.policy_id: p.inferred_value for p in parsed.parse_provenance.default_policies}
    assert policies == {'RETURN.default_period': '1Y', 'RETURN.positive_direction': 'DESC', 'TOPK.default_k': 5}
    assert all(p.source == 'DEFAULT_POLICY' for p in parsed.parse_provenance.default_policies)
    assert bool(parsed.product_universe) == ('국내' in question)
    if '안전' in question:
        assert any(c.required and c.unsupported_reason == 'subjective_execution_unsupported'
                   for c in parsed.semantic_constraints)


def test_explicit_period_count_and_direction_override_defaults():
    parsed = _parse('국내 ETF 중 최근 3개월 수익률이 낮은 상위 7개 알려줘')
    assert parsed.result_limit.value == 7 and parsed.sort[0].direction == 'asc'
    assert parsed.metrics[0].temporal.period == '3M'
    assert parsed.parse_provenance.default_policies == []
    negative = _parse('국내 ETF 중 수익률 나쁜 상품 알려줘')
    assert negative.sort[0].direction == 'asc'
    assert 'RETURN.negative_direction' in {p.policy_id for p in negative.parse_provenance.default_policies}


def test_date_output_and_ranked_recommendation_reach_planning_without_llm():
    result, trace, calls, llm = _answer('국내 ETF 중 순자산이 큰 상품 5개를 기준일과 함께 알려줘.',
                                      [_record('product.aum', '1000')])
    assert trace['status'] == 'partial' and len(calls) == 1 and llm.calls == 0
    assert '1000' in result.answer and '기준일' in result.answer
    assert any(c['status'] == 'UNSUPPORTED' and c['label'] == '기준일'
               for c in trace['validation_summary']['clauses'])
    parsed = _parse('순자산 큰 순서로 ETF 추천해줘')
    assert parsed.intent == 'search_product' and parsed.semantic_coverage == 'complete'
    assert parsed.product_universe is None and parsed.result_limit.value == 5


def test_partial_lookup_discloses_default_and_missing_risk():
    result, trace, calls, llm = _answer('TIGER 미국S&P500의 AUM과 수익률, 위험정보 알려줘',
        [_record('product.aum', '1000'), _record('product.one_year_return', '12.3')])
    assert trace['status'] == 'partial' and len(calls) == 1 and llm.calls == 0
    assert '1000' in result.answer and '12.3%' in result.answer and '위험등급: 현재 데이터에서 확인할 수 없음' in result.answer
    assert '기간을 지정하지 않아' in result.answer
    assert trace['default_policies'][0]['policy_id'] == 'RETURN.default_period'
    assert trace['llm_call_summary'] == {'semantic_parser_calls': 0, 'answer_generation_calls': 0, 'retries': 0}


@pytest.mark.parametrize('question', [
    '안전하고 수익률 좋은 ETF 알려줘', '안전한 국내 ETF 중 수익률 좋은 상품 알려줘',
    '미래에셋 ETF 중 운용보수가 낮고 수익률 좋은 상품 알려줘',
    '수익률 좋은 ETF 알려줘', '미국 시장에 상장된 ETF 중 수익률 높은 것 알려줘.',
    '순자산 큰 순서로 ETF 추천해줘',
])
def test_unverified_selection_is_capability_unsupported_and_never_executed(question):
    result, trace, calls, llm = _answer(question, [_record('product.one_year_return', '12.3')])
    assert trace['status'] == 'unsupported' and not calls and llm.calls == 0
    assert '12.3' not in result.answer
    assert trace['default_policies']


def test_domestic_return_ranking_and_explicit_projection_keep_plan_contracts():
    _, trace, calls, llm = _answer('국내 ETF 중 수익률 좋은 상품 알려줘', [_record('product.one_year_return', '12.3')])
    assert trace['status'] == 'success' and len(calls) == 1 and llm.calls == 0
    _, trace, calls, llm = _answer('국내 ETF 중 3개월 수익률 상위 5개와 AUM 알려줘', [])
    assert len(calls) == 1 and llm.calls == 0
    inputs = calls[0].steps[0].inputs
    assert inputs['sort'][0]['canonical_field'] == 'product.three_month_return'
    assert 'product.aum' in inputs['requested_fields']
    assert trace['default_policies'] == []


def test_canonical_alias_default_proposal_and_unique_offset_are_repaired_losslessly():
    q = '국내 ETF 중 수익률 좋은 상품 알려줘'
    data = _candidate(q)
    data['sorts'][0].update(field='product.one_year_return', direction='high')
    data['sorts'][0]['source_span']['start'] += 1
    data['default_policies'] = [dict(policy_id='TOPK.default_k', inferred_value=5, source='DEFAULT_POLICY')]
    data['result_limit'] = dict(source_span=_span(q, '수익률 좋은'), value=5)
    parsed = _validate(q, normalize_candidate_payload(q, data))
    assert parsed.sort[0].field == '수익률' and parsed.sort[0].direction == 'desc'
    assert parsed.product_universe == _parse(q).product_universe.model_copy(update={'constraint_id': parsed.product_universe.constraint_id})
    assert parsed.result_limit.value == 5
    assert all(q[c.source_span.start:c.source_span.end] == c.raw_text for c in parsed.semantic_constraints)


@pytest.mark.parametrize('fault', ['unknown_field', 'wrong_direction', 'invented_limit', 'invalid_policy', 'wrong_period'])
def test_recovery_never_changes_explicit_meaning(fault):
    q = '국내 ETF 중 3개월 수익률 높은 상품 알려줘' if fault == 'wrong_period' else '국내 ETF 중 수익률 좋은 상품 알려줘'
    data = _candidate(q, '3개월 수익률 높은' if fault == 'wrong_period' else '수익률 좋은')
    if fault == 'unknown_field':
        data['sorts'][0]['field'] = 'product.secret_score'
    elif fault == 'wrong_direction':
        data['sorts'][0]['direction'] = 'asc'
    elif fault == 'invented_limit':
        data['result_limit'] = dict(source_span=_span(q, '수익률 좋은'), value=5)
    elif fault == 'invalid_policy':
        data['default_policies'] = [dict(policy_id='TOPK.default_k', inferred_value=100)]
    else:
        data['sorts'][0]['field'] = 'product.one_year_return'
    with pytest.raises(SemanticCandidateValidationError):
        _validate(q, data)


def test_ambiguous_or_nonexistent_span_is_not_relocated():
    for q, raw in [('ETF ETF', 'ETF'), ('ETF', 'ETN')]:
        data = dict(source_span=dict(start=1, end=2, raw_text=raw))
        assert normalize_candidate_payload(q, data) == data


def test_llm_cannot_downgrade_known_subjective_selection_to_search_text():
    q = '안전한 국내 ETF 중 수익률 좋은 상품 알려줘'
    data = _candidate(q)
    data['semantic_texts'] = [dict(source_span=_span(q, '안전한'), value='안전한')]
    parsed = _validate(q, data)
    assert parsed.unsupported_constraint_ids
    assert any(c.required and c.unsupported_reason == 'subjective_execution_unsupported' for c in parsed.semantic_constraints)


@pytest.mark.parametrize('fault', ['candidate', 'json', 'schema', 'timeout', 'http'])
@pytest.mark.parametrize('repair_succeeds', [True, False])
def test_real_client_repair_is_bounded_and_transport_failures_are_not_retried(fault, repair_succeeds):
    q = '국내 ETF 중 수익률 좋은 상품 알려줘'
    good = _candidate(q)
    bad = deepcopy(good)
    bad['sorts'][0]['field'] = 'unverified_field'
    requests = []
    def handler(request):
        requests.append(json.loads(request.content))
        if fault == 'timeout':
            raise httpx.ReadTimeout('private transport detail', request=request)
        if fault == 'http':
            return httpx.Response(400, json={'error': 'private provider detail'})
        value = good if len(requests) == 2 and repair_succeeds else bad
        content = json.dumps(value)
        if len(requests) == 1 or not repair_succeeds:
            if fault == 'json': content = 'invalid json'
            if fault == 'schema': content = json.dumps({'intent': 'search_product', 'unknown': 'secret detail'})
        return httpx.Response(200, json={'result': {'message': {'content': content}}})
    class IncompleteRule(RuleBasedQueryAnalyzer):
        async def analyze(self, question):
            return (await super().analyze(question)).model_copy(update={'semantic_coverage': SemanticCoverageStatus.INCOMPLETE})
    async def run():
        vocabulary = export_compact_semantic_vocabulary(_ontology().index)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = HyperCLOVASemanticParserClient(HyperCLOVASemanticParserSettings(api_key='test-only'), http_client=http)
            return await SemanticParserCoordinator(rule_parser=IncompleteRule(), llm_parser=client,
                candidate_validator=LLMSemanticCandidateValidator(vocabulary), compact_vocabulary=vocabulary).analyze(q)
    transport = fault in {'timeout', 'http'}
    if transport or not repair_succeeds:
        with pytest.raises(SemanticParseSafetyError) as exc:
            asyncio.run(run())
        assert exc.value.llm_calls == (1 if transport else 2)
        assert exc.value.repair_attempts == (0 if transport else 1)
    else:
        parsed = asyncio.run(run())
        assert parsed.parse_provenance.llm_calls == 2 and parsed.parse_provenance.repair_attempts == 1
    assert len(requests) == (1 if transport else 2)
    if not transport:
        repair = json.loads(requests[1]['messages'][1]['content'])
        assert repair['original_question'] == q and repair['allowed_default_policies']
        context = repair['repair_context']
        assert context['validation_errors'] and 'rejected_candidate' in context
        assert all('secret detail' not in error for error in context['validation_errors'])


@pytest.mark.parametrize('conflicting', [False, True])
def test_partial_ranking_labels_use_only_unambiguous_name_facts(conflicting):
    records = [_record('product.name', '검증된 상품명'), _record('product.aum', '1000')]
    if conflicting:
        records.append(_record('product.name', '충돌하는 다른 상품명'))
    result, trace, calls, _ = _answer('국내 ETF 중 순자산이 큰 상품 5개를 기준일과 함께 알려줘.', records)
    assert trace['status'] == 'partial' and len(calls) == 1
    if conflicting:
        assert '검증된 상품명' not in result.answer and '충돌하는 다른 상품명' not in result.answer
    else:
        assert '검증된 상품명 — 순자산: 1000' in result.answer
        assert '검증된 상품명 — 기준일:' in result.answer


@pytest.mark.parametrize('reason,phrase', [
    ('unsupported_comparison:return_1Y_product_scope_not_verified', '수익률 근거가 없습니다'),
    ('unsupported_comparison:expense_ratio_scale_unverified', '보수의 단위와 비교 기준'),
    ('subjective_execution_unsupported', '선정 기준을 현재 근거로 검증할 수 없습니다'),
    ('unsupported_comparison:aum_scope_spans_or_cannot_exclude_incompatible_sources', '순자산 단위·통화'),
])
def test_known_capability_failures_explain_the_missing_contract(reason, phrase):
    from app.domain.models import ValidationResult
    result = asyncio.run(ReasonAwareSafeResponseGenerator().generate(ValidationResult(
        answerable=False, reason_codes=['UNSUPPORTED_CONSTRAINT'], reasons=[reason])))
    assert phrase in result and '잠시 후' not in result


@pytest.mark.parametrize('question', ['미국 ETF 알려줘', '미국에 투자하는 ETF 알려줘'])
def test_merging_exclusion_correction_does_not_allow_invented_negation(question):
    data = dict(intent='search_product', product_types=[dict(value='ETF', source_span=_span(question, 'ETF'))],
                filters=[dict(field='region', operator='ne', value='미국', source_span=_span(question, question))])
    with pytest.raises(SemanticCandidateValidationError) as exc:
        _validate(question, data)
    assert 'candidate_changes_rule_filter' in exc.value.reasons


def test_relation_type_normalization_uses_the_merged_schema_allowlist():
    from app.query.llm_parser import hyperclova_candidate_schema
    normalized = normalize_candidate_payload('', {'subject_type': 'salelot', 'target_type': 'asset class'})
    properties = hyperclova_candidate_schema()['properties']['relations']['items']['properties']
    assert normalized == {'subject_type': 'SaleLot', 'target_type': 'AssetClass'}
    assert normalized['subject_type'] in properties['subject_type']['enum']
    assert normalized['target_type'] in properties['target_type']['enum']


def test_merged_rule_retyping_cannot_downgrade_a_hard_numeric_filter():
    question = '국내 ETF 중 순자산 1조 이상인 상품 알려줘'
    rule = _parse(question)
    condition = next(c for c in rule.semantic_constraints if c.semantic_type.value == 'filter')
    assert condition.unsupported_reason == 'dataset_unit_mapping_unverified'
    data = dict(intent='search_product', product_types=[dict(value='ETF', source_span=_span(question, 'ETF'))],
                semantic_texts=[dict(value=condition.raw_text, source_span=_span(question, condition.raw_text))])
    with pytest.raises(SemanticCandidateValidationError) as exc:
        _validate(question, data)
    assert 'candidate_omits_rule_material' in exc.value.reasons
