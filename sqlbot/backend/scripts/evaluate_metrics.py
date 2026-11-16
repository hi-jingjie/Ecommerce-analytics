"""Versioned metric Agent evaluation. --validate-only never calls a model."""
import argparse
import hashlib
import json
import math
import os
import statistics
import sys
import time
from collections import Counter
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

from apps.metrics.agent import planner_prompt
from apps.metrics.schemas import CATALOG, MetricIntent, resolve_period
from scripts.verify_metric_llm import CheckFailure, payload, require

DEFAULT_DATASET = Path(__file__).resolve().parents[1] / 'evals/metrics/questions.v1.json'
DEMO_NAME = '电商指标演示（模拟数据）'
TZ = ZoneInfo('Asia/Shanghai')


def normalized_rows(rows):
    """Compare exact decimals, null averages and unordered groups without rounding."""
    result = []
    for row in rows:
        require(isinstance(row, dict) and 'value' in row, 'Invalid result row')
        value = row['value']
        if value is not None:
            value = Decimal(str(value))
            require(value.is_finite(), 'Non-finite result')
        # A multiset preserves duplicate rows; Decimal equality ignores scale.
        result.append(tuple(sorted({**row, 'value': value}.items())))
    return Counter(result)


def load_dataset(path: Path, today: date):
    raw = path.read_bytes()
    data = json.loads(raw)
    if not data.get('version') or not isinstance(data.get('cases'), list) or not data['cases']:
        raise ValueError('Dataset needs a version and nonempty cases')
    ids, questions = set(), set()
    for case in data['cases']:
        for field in ('id', 'question', 'category'):
            if not isinstance(case.get(field), str) or not case[field].strip():
                raise ValueError(f'Invalid case {field}')
        if case['id'] in ids or case['question'] in questions:
            raise ValueError('Duplicate ID or question')
        ids.add(case['id'])
        questions.add(case['question'])
        expected = case['expected']
        if expected['status'] == 'ready':
            intent = expected['intent']
            if 'period' in intent:
                if 'start_date' in intent or 'end_date' in intent or 'rows' in expected:
                    raise ValueError('Relative cases cannot have fixed dates or result golds')
                start, end = resolve_period(intent.pop('period'), today)
                intent.update(start_date=start.isoformat(), end_date=end.isoformat())
            expected['intent'] = MetricIntent.model_validate(intent).model_dump(mode='json')
            if 'rows' in expected:
                if not isinstance(expected['rows'], list):
                    raise ValueError('Gold rows must be a list')
                normalized_rows(expected['rows'])
        elif expected['status'] == 'needs_clarification':
            if not expected.get('reason') or 'intent' in expected or 'rows' in expected:
                raise ValueError('Clarification needs a reason and no executable intent')
        else:
            raise ValueError('Invalid expected status')
    return data['cases'], hashlib.sha256(raw).hexdigest()


def empty_records(cases):
    return [dict(**case, status='NOT_RUN', evaluated=False, checks={}) for case in cases]


def object_payload(response):
    body = payload(response)
    require(isinstance(body, dict), 'Expected JSON object response')
    return body


def query(client, datasource_id, intent):
    plan = object_payload(client.post('/api/v1/metrics/preview', json={
        'datasource_id': datasource_id, 'intent': intent}))
    require(plan['intent'] == intent and plan['status'] == 'preview', 'Preview differs from intent')
    result = object_payload(client.post(f"/api/v1/metrics/queries/{plan['id']}/execute", json={'confirmed': True}))
    require(result['status'] == 'completed', 'Query did not complete')
    require(isinstance(result.get('rows'), list), 'Expected query result rows')
    return plan, result


def summarize(report):
    records = report['cases']
    evaluated = [r for r in records if r['evaluated']]
    report['coverage'] = dict(total=len(records), evaluated=len(evaluated),
                              fraction=len(evaluated) / len(records) if records else None)
    metrics = {}
    for name, eligible in [
        ('parameters', [r for r in records if r['expected']['status'] == 'ready']),
        ('clarification', [r for r in records if r['expected']['status'] == 'needs_clarification']),
        ('results', [r for r in records if 'rows' in r['expected']]),
    ]:
        tested = [r for r in eligible if r['evaluated']]
        correct = sum(r['checks'].get(name) is True for r in tested)
        metrics[name] = dict(eligible=len(eligible), evaluated=len(tested), correct=correct,
                             accuracy=correct / len(tested) if tested else None)
    report['metrics'] = metrics
    report['categories'] = {category: dict(total=len(group), evaluated=sum(r['evaluated'] for r in group),
                                          passed=sum(r['status'] == 'PASS' for r in group))
                            for category in sorted({r['category'] for r in records})
                            for group in [[r for r in records if r['category'] == category]]}
    durations = sorted(r['resolve_seconds'] for r in records if 'resolve_seconds' in r)
    report['latency'] = dict(resolve_calls=len(durations),
        resolve_p50_seconds=statistics.median(durations) if durations else None,
        resolve_p95_seconds=durations[math.ceil(len(durations) * .95) - 1] if durations else None,
        total_case_seconds=round(sum(r.get('duration_seconds', 0) for r in records), 3))
    usages = [r['usage'] for r in records if isinstance(r.get('usage'), dict)]
    report['usage'] = dict(reported_calls=len(usages), attempted_calls=len(durations))
    for key in ('input_tokens', 'output_tokens', 'total_tokens'):
        values = [u[key] for u in usages if type(u.get(key)) is int and u[key] >= 0]
        report['usage'][key] = sum(values) if values else None
        report['usage'][key + '_reported_calls'] = len(values)
    report['model_ids'] = sorted({str(r['model_id']) for r in records if r.get('model_id') is not None})
    return report


def evaluate(client, cases):
    report = dict(status='BLOCKED', cases=empty_records(cases))
    try:
        sources = payload(client.get('/api/v1/metrics/sources'))
        require(isinstance(sources, list) and all(isinstance(source, dict) for source in sources),
                'Expected datasource list')
        matches = [source for source in sources if source['name'] == DEMO_NAME]
        require(len(matches) == 1, 'Expected one synthetic demo datasource')
        datasource_id = matches[0]['id']
        report['datasource_id'] = datasource_id
    except (CheckFailure, ValueError, KeyError, TypeError, httpx.HTTPError) as exc:
        report['reason'] = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
        return summarize(report)
    # Verify the fixed demo data/definitions before attributing result differences
    # to a model. These are gold-parameter queries, never model observations.
    fixture = dict(status='PASS', checked=0, total=sum('rows' in c['expected'] for c in cases), cases=[])
    report['fixture_validation'] = fixture
    for case in cases:
        if 'rows' not in case['expected']:
            continue
        try:
            plan, result = query(client, datasource_id, case['expected']['intent'])
            require(normalized_rows(result['rows']) == normalized_rows(case['expected']['rows']),
                    'Demo data or definitions differ from dataset golds; check reference week and bindings')
            fixture['cases'].append(dict(id=case['id'], query_id=plan['id'], version=plan['version'], rows=result['rows']))
            fixture['checked'] += 1
        except (CheckFailure, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
            fixture.update(status='FAIL', failed_case=case['id'])
            report['reason'] = str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__
            return summarize(report)
    blocked = False
    for record in report['cases']:
        started = time.monotonic()
        try:
            call_started = time.monotonic()
            try:
                response = client.post('/api/v1/metrics/resolve', json={
                    'datasource_id': datasource_id, 'question': record['question']})
            finally:
                record['resolve_seconds'] = round(time.monotonic() - call_started, 6)
            if response.status_code in (401, 403, 503):
                record.update(status='BLOCKED', reason=f'Resolve HTTP {response.status_code}; check session/model configuration')
                blocked = True
                break
            record['evaluated'] = True
            resolved = object_payload(response)
            record['actual'] = {key: resolved.get(key) for key in ('status', 'intent', 'questions')}
            record['usage'] = resolved.get('usage')
            record['model_id'] = resolved.get('model_id')
            require(record['model_id'] is not None, 'Missing model identity')
            expected = record['expected']
            if expected['status'] == 'needs_clarification':
                questions = resolved.get('questions')
                correct = (resolved.get('status') == 'needs_clarification' and resolved.get('intent') is None
                           and isinstance(questions, list) and bool(questions)
                           and all(isinstance(q, str) and bool(q.strip()) for q in questions))
                record['checks']['clarification'] = correct
                require(correct, 'Expected clarification without executable intent')
            else:
                require(resolved.get('status') == 'ready', 'Expected ready intent')
                actual = MetricIntent.model_validate(resolved.get('intent')).model_dump(mode='json')
                record['checks']['parameters'] = actual == expected['intent']
                require(record['checks']['parameters'], 'Intent differs from gold; query not executed')
                if 'rows' in expected:
                    plan, result = query(client, datasource_id, actual)
                    record.update(query_id=plan['id'], version=plan['version'])
                    record['actual']['rows'] = result['rows']
                    record['checks']['results'] = normalized_rows(result['rows']) == normalized_rows(expected['rows'])
                    require(record['checks']['results'], 'Query result differs from gold')
            record['status'] = 'PASS'
        except (CheckFailure, ValueError, KeyError, TypeError, ArithmeticError, httpx.HTTPError) as exc:
            # Failed attempts count in accuracy; unavailable configuration above does not.
            record.update(status='FAIL', evaluated=True,
                          reason=str(exc) if isinstance(exc, CheckFailure) else type(exc).__name__)
        finally:
            record['duration_seconds'] = round(time.monotonic() - started, 6)
    report['status'] = 'BLOCKED' if blocked else ('PASS' if all(r['status'] == 'PASS' for r in report['cases']) else 'FAIL')
    return summarize(report)


def write_reports(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    lines = ['# 指标 Agent 评测', '', f"状态：**{report['status']}**；模式：{report['mode']}", '',
             f"完成比例：{report['coverage']['evaluated']} / {report['coverage']['total']}。", '',
             'VALIDATED 只表示标注校验通过；BLOCKED 不表示模型通过。正确率分母只含已评测题目，须结合完成比例阅读。', '',
             '| 指标 | 正确 / 已评测 / 应评测 | 正确率 |', '| --- | --- | --- |']
    for key, metric in report['metrics'].items():
        accuracy = '未评测' if metric['accuracy'] is None else f"{metric['accuracy']:.1%}"
        lines.append(f"| {key} | {metric['correct']} / {metric['evaluated']} / {metric['eligible']} | {accuracy} |")
    lines += ['', f"解析耗时 p50 / p95（秒）：{report['latency']['resolve_p50_seconds']} / {report['latency']['resolve_p95_seconds']}",
              f"已报告 token 合计：{report['usage']['total_tokens']}；用量返回次数：{report['usage']['reported_calls']} / {report['usage']['attempted_calls']}。None 表示未知。",
              '', f"数据集 SHA256：`{report['dataset_sha256']}`", f"运行时间：{report['checked_at']}", '',
              f"数据基准预检：{report.get('fixture_validation', {}).get('status', 'NOT_RUN')}；"
              f"{report.get('fixture_validation', {}).get('checked', 0)} 个金标准查询通过（不计入模型正确率）。", '',
              '## 逐题结果', '', '| ID | 分类 | 状态 | 原因 |', '| --- | --- | --- | --- |']
    for row in report['cases']:
        cells = [row['id'], row['category'], row['status'], row.get('reason', '')]
        lines.append('| ' + ' | '.join(str(c).replace('|', '\\|').replace('\n', ' ') for c in cells) + ' |')
    path.with_suffix('.md').write_text('\n'.join(lines) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dataset', type=Path, default=DEFAULT_DATASET)
    parser.add_argument('--output', type=Path, required=True, help='JSON report; a Markdown sibling is also written')
    parser.add_argument('--validate-only', action='store_true')
    parser.add_argument('--base-url', default='http://127.0.0.1:18000')
    args = parser.parse_args()
    if args.output.suffix != '.json':
        parser.error('--output must end in .json')
    today = datetime.now(TZ).date()
    try:
        cases, digest = load_dataset(args.dataset, today)
    except (OSError, ValueError, KeyError, TypeError, CheckFailure, ArithmeticError):
        parser.error('Invalid evaluation dataset; check annotations and contract')
    if args.validate_only:
        report = summarize(dict(status='VALIDATED', cases=empty_records(cases)))
    elif not os.environ.get('SQLBOT_TEST_TOKEN'):
        report = summarize(dict(status='BLOCKED', reason='Set SQLBOT_TEST_TOKEN to a logged-in session token', cases=empty_records(cases)))
    else:
        with httpx.Client(base_url=args.base_url, timeout=75, trust_env=False,
                          headers={'X-SQLBOT-TOKEN': 'Bearer ' + os.environ['SQLBOT_TEST_TOKEN']}) as client:
            report = evaluate(client, cases)
    report.update(mode='offline_validation' if args.validate_only else 'live_http', dataset_sha256=digest,
                  checked_at=datetime.now(TZ).isoformat(), reference_date=today.isoformat(),
                  planner_template_sha256=hashlib.sha256(planner_prompt(today, sorted(CATALOG)).encode()).hexdigest())
    write_reports(args.output, report)
    print(f"{report['status']}: {report['coverage']['evaluated']}/{len(cases)} evaluated; {args.output}")
    return {'PASS': 0, 'VALIDATED': 0, 'FAIL': 1, 'BLOCKED': 2}[report['status']]


if __name__ == '__main__':
    sys.exit(main())
