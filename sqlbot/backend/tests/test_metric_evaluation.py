"""Evaluator correctness tests; simulated HTTP is not a model benchmark."""
import importlib.util
import json
from datetime import date

import httpx
import pytest


def module():
    assert importlib.util.find_spec('scripts.evaluate_metrics') is not None, 'evaluation runner missing'
    from scripts import evaluate_metrics
    return evaluate_metrics


def test_dataset_has_40_unique_annotated_questions():
    m = module()
    cases, digest = m.load_dataset(m.DEFAULT_DATASET, date(2026, 10, 29))
    assert len(cases) == 40 and len(digest) == 64
    assert len({case['id'] for case in cases}) == 40
    assert sum(case['expected']['status'] == 'ready' for case in cases) == 26
    assert sum('rows' in case['expected'] for case in cases) == 20
    relative = next(case for case in cases if case['id'] == 'relative_last_week')
    assert relative['expected']['intent']['start_date'] == '2026-10-19'
    assert relative['expected']['intent']['end_date'] == '2026-10-25'


def simulate(fault=None):
    m = module()
    cases, _ = m.load_dataset(m.DEFAULT_DATASET, date(2026, 10, 29))
    # One result case and one clarification case; faults must affect metrics honestly.
    cases = [cases[0], next(case for case in cases if case['expected']['status'] == 'needs_clarification')]
    plans, executions = {}, []
    resolving = False
    def handle(request):
        nonlocal resolving
        body = json.loads(request.content) if request.content else {}
        path = request.url.path
        if path.endswith('/sources'):
            return httpx.Response(200, json=[{'id': 1, 'name': m.DEMO_NAME}])
        if path.endswith('/resolve'):
            resolving = True
            case = next(case for case in cases if case['question'] == body['question'])
            if fault == 'blocked' or (fault == 'partial' and case == cases[1]):
                return httpx.Response(503)
            if fault == 'malformed':
                return httpx.Response(200, json=['unexpected'])
            data = dict(status=case['expected']['status'], intent=case['expected'].get('intent'),
                        questions=['请明确时间'] if case == cases[1] else [], model_id=42,
                        usage={'input_tokens': 10, 'output_tokens': 5, 'total_tokens': 15})
            if fault == 'missing_usage':
                data['usage'] = None
            if fault == 'wrong_intent' and case == cases[0]:
                data['intent'] = {**data['intent'], 'filters': {'region': '北京'}}
            if fault == 'empty_clarification' and case == cases[1]:
                data['questions'] = []
            return httpx.Response(200, json={'data': data})
        if path.endswith('/preview'):
            plans['p'] = body['intent']
            return httpx.Response(200, json=dict(id='p', status='preview', version=1, intent=body['intent']))
        executions.append({'phase': 'model' if resolving else 'fixture', **body})
        return httpx.Response(200, json=dict(status='completed', rows=[{'value': '999'}] if fault == 'bad_fixture' or (fault == 'wrong_result' and resolving)
                                            else cases[0]['expected']['rows']))
    with httpx.Client(base_url='http://test', transport=httpx.MockTransport(handle)) as client:
        report = m.evaluate(client, cases)
    return report, executions


def test_success_scores_tokens_and_results():
    report, executions = simulate()
    assert report['status'] == 'PASS' and len([e for e in executions if e['phase'] == 'model']) == 1
    assert report['metrics']['parameters']['accuracy'] == 1
    assert report['metrics']['clarification']['accuracy'] == 1
    assert report['metrics']['results']['accuracy'] == 1
    assert report['usage']['total_tokens'] == 30
    assert report['latency']['resolve_p95_seconds'] is not None


@pytest.mark.parametrize('fault,metric', [('wrong_intent', 'parameters'), ('wrong_result', 'results'),
                                        ('empty_clarification', 'clarification')])
def test_errors_reduce_correct_metric(fault, metric):
    report, executions = simulate(fault)
    assert report['status'] == 'FAIL'
    assert report['metrics'][metric]['accuracy'] == 0
    if fault == 'wrong_intent':
        assert not [e for e in executions if e['phase'] == 'model']
        assert report['metrics']['results']['accuracy'] == 0


def test_missing_model_never_reports_zero_or_perfect_accuracy():
    report, executions = simulate('blocked')
    assert report['status'] == 'BLOCKED' and not [e for e in executions if e['phase'] == 'model']
    assert report['metrics']['parameters']['accuracy'] is None
    assert report['coverage']['evaluated'] == 0
    assert report['coverage']['total'] == 2


def test_partial_run_keeps_coverage_and_pending_denominator():
    report, _ = simulate('partial')
    assert report['status'] == 'BLOCKED'
    assert report['coverage']['evaluated'] == 1
    assert report['metrics']['clarification']['accuracy'] is None
    assert report['metrics']['clarification']['eligible'] == 1


def test_missing_usage_is_unknown_not_free():
    report, _ = simulate('missing_usage')
    assert report['status'] == 'PASS'
    assert report['usage']['total_tokens'] is None
    assert report['usage']['reported_calls'] == 0


def test_invalid_dataset_is_rejected(tmp_path):
    m = module()
    path = tmp_path / 'bad.json'
    path.write_text(json.dumps({'version': '1', 'cases': []}))
    with pytest.raises(ValueError):
        m.load_dataset(path, date(2026, 10, 29))


def test_wrong_fixture_blocks_before_model_scoring():
    report, executions = simulate('bad_fixture')
    assert report['status'] == 'BLOCKED'
    assert report['coverage']['evaluated'] == 0
    assert report['fixture_validation']['status'] == 'FAIL'
    assert not [e for e in executions if e['phase'] == 'model']


def test_malformed_model_response_is_reported_and_remaining_cases_run():
    report, executions = simulate('malformed')
    assert report['status'] == 'FAIL'
    assert report['coverage']['evaluated'] == 2
    assert all(row['reason'] == 'Expected JSON object response' for row in report['cases'])
    assert not [e for e in executions if e['phase'] == 'model']
