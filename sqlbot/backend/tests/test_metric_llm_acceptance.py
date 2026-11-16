"""Tests of the acceptance runner, NOT evidence of real model quality."""
import json
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
import pytest

from scripts.verify_metric_llm import cases, run


def simulate(fault=None):
    suite = cases('2026-10-19', '2026-10-25', datetime.now(ZoneInfo('Asia/Shanghai')).date())
    questions = {case['question']: case for case in suite}
    plans, completed, executed = {}, set(), []

    def respond(request):
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        status = 200
        if path.endswith('/sources'):
            data = [{'id': 1, 'name': '电商指标演示（模拟数据）'}]
        elif path.endswith('/resolve'):
            if fault == 'unavailable':
                return httpx.Response(503, json={'detail': 'unavailable'})
            case = questions[body['question']]
            data = dict(model_id=42, usage=None, questions=[], intent=case.get('intent'), status='ready')
            if case.get('clarification'):
                data.update(status='needs_clarification', intent=None, questions=['请补充条件'])
                if fault == 'empty_clarification':
                    data['questions'] = []
            if fault == 'dropped_filter' and case['name'] == 'channel_filter':
                data['intent'] = {**case['intent'], 'filters': {}}
        elif path.endswith('/preview'):
            case = next(case for case in suite if case.get('intent') == body['intent'] and 'rows' in case)
            plan_id = str(len(plans))
            plans[plan_id] = case
            data = dict(id=plan_id, status='preview', version=1, intent=body['intent'])
        else:
            plan_id = path.split('/')[-2]
            if not body['confirmed']:
                status, data = 422, {}
            elif plan_id in completed:
                status, data = 409, {}
            else:
                completed.add(plan_id)
                executed.append(plans[plan_id]['name'])
                data = dict(status='completed', rows=plans[plan_id]['rows'])
                if fault == 'wrong_amount':
                    data['rows'] = [{'value': '999'}]
        return httpx.Response(status, json={'data': data})

    report = {'cases': []}
    with httpx.Client(base_url='http://test', transport=httpx.MockTransport(respond)) as client:
        run(client, report, '2026-10-19', '2026-10-25')
    return report, executed


def test_harness_checks_complete_flow_and_only_executes_six_explicit_queries():
    report, executed = simulate()
    assert report['status'] == 'PASS'
    assert len(report['cases']) == 13
    assert len(executed) == 6


@pytest.mark.parametrize('fault', ['wrong_amount', 'dropped_filter', 'empty_clarification'])
def test_harness_does_not_accept_wrong_results_or_dropped_conditions(fault):
    report, executed = simulate(fault)
    assert report['status'] == 'FAIL'
    if fault == 'dropped_filter':
        assert 'channel_filter' not in executed


def test_missing_model_blocks_without_query_execution():
    report, executed = simulate('unavailable')
    assert report['status'] == 'BLOCKED'
    assert len(report['cases']) == 1
    assert not executed
