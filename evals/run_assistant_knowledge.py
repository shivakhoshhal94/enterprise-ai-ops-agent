"""Explicit 9B live checks. Never approve workflows; API proposal creates a pending checkpoint.

Run: python evals/run_assistant_knowledge.py --live
Reports are written to ignored .venv/; no provider objects or credentials are saved.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re
from decimal import Decimal
from unittest.mock import patch


def check_case(case, result, executions):
    """Explicit routing, source/section and bounded concept checks; not an LLM judge."""
    names = [item['name'] for item in executions]
    expected = case['tools']
    routing = sorted(names) == sorted(expected)
    if case.get('optional_tools'):
        routing = all(name in case['optional_tools'] for name in names)
    hits = [hit for item in executions if item['name'] == 'search_company_knowledge'
            for hit in item['output']['evidence']]
    retrieval = None
    if 'document' in case:
        retrieval = any(hit['document'] == case['document'] and hit['section'] == case['section'] for hit in hits)
    received = {(hit['document'], hit['title'], hit['section']) for hit in hits}
    reported = {(s['document'], s['title'], s['section']) for s in result['knowledge_sources']}
    attribution = reported == received
    body = result['answer'].split('\n\nKnowledge evidence is')[0]
    concepts = all(re.search(pattern, body, re.I) for pattern in case['concepts'])
    forbidden = any(re.search(pattern, result['answer'], re.I) for pattern in case['forbidden'])
    grounding = None
    if 'search_company_knowledge' in expected:
        grounding = bool(concepts and not forbidden and attribution
                         and 'not official Olist policy' in result['answer']
                         and (retrieval is not False))
    return {'routing': routing, 'retrieval': retrieval, 'grounding': grounding,
            'concept_checks': bool(concepts and not forbidden), 'attribution': attribution}


def fingerprint(connection):
    from sqlalchemy import text
    keys = {'public.customers':'customer_id', 'public.category_translations':'product_category_name',
            'public.sellers':'seller_id', 'public.products':'product_id', 'public.orders':'order_id',
            'public.order_items':'order_id,order_item_id', 'public.order_payments':'order_id,payment_sequential',
            'operations.order_review_cases':'case_id', 'operations.case_events':'event_id',
            'knowledge.document_chunks':'chunk_id'}
    result = {}
    for table, order in keys.items():
        row = connection.execute(text(f"SELECT count(*),md5(string_agg(r::text,'' ORDER BY {order})) FROM {table} r")).one()
        result[table] = {'count': row[0], 'fingerprint': row[1]}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true', required=True)
    parser.add_argument('--case', choices=['installments','priority','refund','canceled','categories','finance','mixed','proposal','malicious'],
                        help='Rerun one case only; preserves the other saved case reports.')
    args = parser.parse_args(argv)
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    from enterprise_ai_ops_agent.ai import assistant, knowledge_tool
    from enterprise_ai_ops_agent.config import Settings
    from enterprise_ai_ops_agent.db.session import create_db_engine
    from enterprise_ai_ops_agent.db.provision_analytics_role import inspect_analytics_privileges
    from enterprise_ai_ops_agent.business.finance import get_order_financial_summary
    from enterprise_ai_ops_agent.main import create_app

    settings = Settings(_env_file=('.env', 'local.env'))
    configured = {name: bool(getattr(settings, name)) for name in
                  ('database_url','analytics_database_url','knowledge_database_url','openai_api_key','openai_model')}
    print(json.dumps({'configuration_present': configured}), flush=True)
    if not all(configured.values()): return 1
    root = Path('knowledge/source')
    corpus = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob('*.md')}
    engine = create_db_engine(settings)
    try:
        with engine.connect() as connection:
            before = fingerprint(connection)
            privileges = inspect_analytics_privileges(connection).model_dump()
            finance = get_order_financial_summary(connection, '03ecec245220b63fd7f68c1737ba99ba').model_dump(mode='json')
            top_categories = connection.execute(text('''SELECT p.product_category_name, t.product_category_name_english,
                SUM(i.price) AS total FROM public.order_items i JOIN public.products p USING(product_id)
                LEFT JOIN public.category_translations t USING(product_category_name)
                GROUP BY 1,2 ORDER BY total DESC LIMIT 5''')).all()
        baseline = {'tables': before, 'analytics_privileges': privileges, 'corpus_hashes': corpus}
        Path('.venv/milestone9b_baseline.json').write_text(json.dumps(baseline))
        cases = json.loads(Path('evals/assistant_knowledge.json').read_text())[:9]
        if args.case:
            cases = [case for case in cases if case['id']==args.case]
        report_path = Path('.venv/milestone9b_live.json')
        reports = json.loads(report_path.read_text(encoding='utf-8')) if args.case and report_path.exists() else []
        actual_execute = assistant.execute_tool
        actual_engine = knowledge_tool.create_knowledge_engine
        users = []
        def observed_engine(config):
            value = actual_engine(config)
            users.append(value.url.username)
            return value
        with TestClient(create_app(settings)) as client:
            for case in cases:
                executed = []
                def observed_execution(*args, **kwargs):
                    item = actual_execute(*args, **kwargs)
                    executed.append({'name': item.trace.tool_name, 'arguments': item.trace.arguments,
                                     'output': json.loads(item.output_json)})
                    return item
                with patch.object(assistant, 'execute_tool', observed_execution), patch.object(knowledge_tool, 'create_knowledge_engine', observed_engine):
                    response = client.post('/ask', json={'question': case['question']})
                if response.status_code != 200:
                    report = {'id': case['id'], 'question': case['question'], 'status': response.status_code,
                              'safe_error': response.json(), 'checks': {'routing':False,'retrieval':False if 'document' in case else None,'grounding':False if 'document' in case else None}}
                else:
                    result = response.json()
                    checks = check_case(case, result, executed)
                    facts = None
                    if case['id'] == 'canceled':
                        payload = next((item['output'] for item in executed if item['name']=='get_order_status_summary'), [])
                        facts = any(row['order_status']=='canceled' and row['order_count']==625 for row in payload) and '625' in result['answer']
                    elif case['id'] in ('finance','mixed'):
                        payload = next((item['output'] for item in executed if item['name']=='get_order_financial_summary'), None)
                        body = result['answer']
                        facts = payload == finance and all(str(finance[key]) in body for key in
                            ('items_price_total','freight_total','items_and_freight_total','payment_total','difference'))
                    elif case['id']=='categories':
                        payload = next((item['output'] for item in executed if item['name']=='run_analytical_query'), {'rows':[]})
                        rows = payload['rows']
                        facts = len(rows)==5
                        for row, expected in zip(rows,top_categories):
                            facts = facts and any(value in row for value in expected[:2] if value is not None)
                            facts = facts and any(str(value)==str(expected[2]) for value in row)
                            # Final answer must contain the category and exact monetary total as well.
                            facts = facts and any(str(value) in result['answer'] for value in expected[:2] if value is not None)
                            digits = re.sub(r'[^0-9]','',str(expected[2]))
                            facts = facts and digits in re.sub(r'[^0-9]','',result['answer'])
                    checks['structured_facts'] = facts
                    if case['id']=='proposal':
                        checks['proposal_pending_only'] = bool(result['workflow'] and result['workflow']['status']=='awaiting_approval')
                    if case['id'] in ('refund','malicious'):
                        checks['no_proposal'] = result['action_proposal'] is None and result['workflow'] is None
                    report = {'id':case['id'],'question':case['question'],'status':200,'result':result,
                              'executions':executed,'checks':checks}
                previous = next((item for item in reports if item['id']==case['id']),None)
                if previous is not None:
                    reports[reports.index(previous)] = report
                else:
                    reports.append(report)
                report_path.write_text(json.dumps(reports, ensure_ascii=False, indent=2),encoding='utf-8')
                print(json.dumps({'case':case['id'],'checks':report['checks']}), flush=True)
        with engine.connect() as connection:
            after = fingerprint(connection)
            unchanged_privileges = inspect_analytics_privileges(connection).model_dump()==privileges
        unchanged_corpus = {str(path):hashlib.sha256(path.read_bytes()).hexdigest() for path in root.glob('*.md')} == corpus
        integrity = {'tables_unchanged':after==before,'analytics_privileges_unchanged':unchanged_privileges,
                     'corpus_unchanged':unchanged_corpus,'knowledge_reader_users':users}
        Path('.venv/milestone9b_integrity.json').write_text(json.dumps(integrity))
        print(json.dumps(integrity),flush=True)
        return 0 if after==before and unchanged_privileges and unchanged_corpus and all(
            value is not False for report in reports for value in report['checks'].values()) else 1
    finally:
        engine.dispose()


if __name__ == '__main__':
    raise SystemExit(main())
