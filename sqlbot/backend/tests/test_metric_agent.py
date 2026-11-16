import importlib.util
from datetime import date
import pytest


def test_agent_module_exists():
    assert importlib.util.find_spec('apps.metrics.agent') is not None, 'metric agent is missing'


def parse(payload):
    from apps.metrics.agent import parse_resolution
    return parse_resolution(payload, date(2026, 10, 29))


def test_model_relative_dates_resolved_by_code():
    result = parse('{"metric_code":"payment_amount","period":"last_week","dimension":"channel"}')
    assert result['intent']['start_date'] == '2026-10-19'
    assert result['intent']['end_date'] == '2026-10-25'
    assert result['status'] == 'ready'


@pytest.mark.parametrize('payload', ['{}', '{"metric_code":"payment_amount"}',
    '{"period":"last_week"}', '{"clarification":"销售额是否指支付金额？"}'])
def test_missing_arguments_are_clarified_without_execution(payload):
    assert parse(payload)['status'] == 'needs_clarification'


@pytest.mark.parametrize('payload', ['{"metric_code":"DROP TABLE"}',
    '{"metric_code":"payment_amount","period":"last_week","sql":"SELECT 1"}',
    '{"metric_code":"payment_amount","period":"last_week","filters":{"product":"A"}}',
    '{"metric_code":"payment_amount","period":"last_week","start_date":"2026-10-01"}',
    'not json'])
def test_invalid_model_output_is_rejected(payload):
    with pytest.raises(ValueError):
        parse(payload)


def test_unknown_conditions_are_not_silently_dropped():
    assert parse('{"metric_code":"payment_amount","period":"last_week","unsupported":["净利润"]}')['status'] == 'needs_clarification'


def test_explicit_dates_and_filter_survive():
    result = parse('```json\n{"metric_code":"refund_amount","start_date":"2026-10-01","end_date":"2026-10-28","filters":{"region":"上海"}}\n```')
    assert result['intent']['filters'] == {'region': '上海'}
    assert result['intent']['metric_code'] == 'refund_amount'
