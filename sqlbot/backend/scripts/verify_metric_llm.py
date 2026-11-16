"""Opt-in real-model HTTP acceptance checks against the synthetic metric demo.

Uses the application's configured default model; never substitutes a mock. Supply
a logged-in session in SQLBOT_TEST_TOKEN (raw token, without the Bearer prefix).
The run makes up to 13 model requests and creates query history for the demo.
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from apps.metrics.schemas import MetricIntent, resolve_period


class CheckFailure(Exception):
    pass


def require(condition, message):
    if not condition:
        raise CheckFailure(message)


def payload(response):
    require(response.status_code == 200, f'HTTP {response.status_code}')
    body = response.json()
    return body.get('data', body) if isinstance(body, dict) else body


def cases(start, end, today):
    def intent(code):
        return dict(metric_code=code, start_date=start, end_date=end,
                    dimension=None, filters={})

    period = f'{start} 至 {end}'
    result = [dict(name=code, question=f'{period}的{name}是多少？',
                   intent=intent(code), rows=[{'value': value}])
              for code, name, value in [
                  ('payment_amount', '支付金额', '351'),
                  ('paid_orders', '支付订单数', '3'),
                  ('average_order_value', '客单价', '117'),
                  ('refund_amount', '退款金额', '40')]]
    grouped = intent('payment_amount')
    grouped['dimension'] = 'channel'
    filtered = intent('payment_amount')
    filtered['filters'] = {'channel': 'App'}
    result.extend([
        dict(name='channel_group', question=f'{period}各渠道支付金额是多少？', intent=grouped,
             rows=[{'dimension': 'App', 'value': '201'}, {'dimension': 'Web', 'value': '150'}]),
        dict(name='channel_filter', question=f'{period}渠道为 App 的支付金额是多少？',
             intent=filtered, rows=[{'value': '201'}]),
    ])
    relative = intent('payment_amount')
    relative['start_date'], relative['end_date'] = [day.isoformat() for day in resolve_period('last_week', today)]
    # Relative dates are checked against the live clock, not the fixed demo week.
    result.append(dict(name='last_week', question='上周支付金额是多少？', intent=relative))
    for name, question in [
        ('ambiguous_date', '最近支付金额是多少？'),
        ('ambiguous_metric', f'{period}销售额是多少？'),
        ('missing_date', '支付订单数是多少？'),
        ('unsupported_metric', f'{period}净利润是多少？'),
        ('unsupported_dimension', f'{period}按商品统计支付金额'),
        ('unsupported_comparison', f'{period}支付金额同比增长多少？'),
    ]:
        result.append(dict(name=name, question=question, clarification=True))
    return result


def normalized_rows(rows):
    return sorted((tuple(sorted((key, str(Decimal(str(value)).normalize()) if key == 'value' else str(value))
                               for key, value in row.items())) for row in rows))


def run(client, report, start, end):
    sources = payload(client.get('/api/v1/metrics/sources'))
    matches = [row for row in sources if row['name'] == '电商指标演示（模拟数据）']
    require(len(matches) == 1, 'Expected exactly one synthetic demo datasource')
    datasource_id = matches[0]['id']
    report['datasource_id'] = datasource_id
    today = datetime.now(ZoneInfo('Asia/Shanghai')).date()
    for case in cases(start, end, today):
        record = dict(name=case['name'], question=case['question'], status='FAIL')
        report['cases'].append(record)
        started = time.monotonic()
        try:
            response = client.post('/api/v1/metrics/resolve', json={
                'datasource_id': datasource_id, 'question': case['question']})
            if response.status_code == 503:
                record['status'] = 'BLOCKED'
                record['reason'] = 'Default model unavailable; configure it and verify provider connectivity.'
                report['status'] = 'BLOCKED'
                return
            resolved = payload(response)
            # Only selected API fields are persisted; no tokens, provider URLs or raw errors.
            record['resolution'] = {key: resolved.get(key) for key in (
                'status', 'intent', 'questions', 'model_id', 'usage')}
            require(resolved.get('model_id') is not None, 'Missing model identity')
            if case.get('clarification'):
                require(resolved.get('status') == 'needs_clarification'
                        and resolved.get('intent') is None and resolved.get('questions'),
                        'Expected clarification with no executable intent')
            else:
                require(resolved.get('status') == 'ready', 'Expected ready resolution')
                actual = MetricIntent.model_validate(resolved['intent']).model_dump(mode='json')
                require(actual == case['intent'], 'Resolved parameters differ from expected intent')
                if 'rows' in case:
                    plan = payload(client.post('/api/v1/metrics/preview', json={
                        'datasource_id': datasource_id, 'intent': actual}))
                    require(plan['intent'] == actual and plan['status'] == 'preview', 'Preview differs from intent')
                    path = f"/api/v1/metrics/queries/{plan['id']}/execute"
                    require(client.post(path, json={'confirmed': False}).status_code == 422,
                            'Execution must require confirmation')
                    result = payload(client.post(path, json={'confirmed': True}))
                    require(result['status'] == 'completed', 'Query did not complete')
                    require(normalized_rows(result['rows']) == normalized_rows(case['rows']), 'Incorrect query result')
                    require(client.post(path, json={'confirmed': True}).status_code == 409,
                            'Repeated execution must be rejected')
                    record.update(query_id=plan['id'], version=plan['version'], rows=result['rows'])
            record['status'] = 'PASS'
        except (CheckFailure, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
            # Provider/transport exception strings may contain credentials: record type only.
            record['reason'] = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        finally:
            record['duration_seconds'] = round(time.monotonic() - started, 3)
    report['status'] = 'PASS' if all(row['status'] == 'PASS' for row in report['cases']) else 'FAIL'


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    parser.add_argument('--start-date', default='2026-10-19', help='Existing synthetic demo week start')
    parser.add_argument('--end-date', default='2026-10-25', help='Existing synthetic demo week end')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = dict(status='BLOCKED', checked_at=datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(),
                  kind='real_model_http_acceptance', planned_cases=13, cases=[])
    try:
        token = os.environ.get('SQLBOT_TEST_TOKEN')
        require(bool(token), 'Set SQLBOT_TEST_TOKEN to a logged-in session token')
        with httpx.Client(base_url=args.base_url, timeout=75, trust_env=False,
                          headers={'X-SQLBOT-TOKEN': 'Bearer ' + token}) as client:
            run(client, report, args.start_date, args.end_date)
    except (CheckFailure, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        report['reason'] = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
    report['passed'] = sum(case['status'] == 'PASS' for case in report['cases'])
    report['failed'] = sum(case['status'] == 'FAIL' for case in report['cases'])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(f"{report['status']}: {report['passed']}/{report['planned_cases']} passed; report: {args.output}")
    return {'PASS': 0, 'FAIL': 1, 'BLOCKED': 2}[report['status']]


if __name__ == '__main__':
    sys.exit(main())
