"""Explicit 10B live /ask verification. Real OpenAI, MCP subprocesses, and PostgreSQL.

Run: python evals/run_assistant_support.py --live
Only the explicit proposal case creates a pending workflow; never approves it.
Credential-free evidence is saved under ignored .venv/.
"""

import argparse
import hashlib
import json
from pathlib import Path
import runpy
from unittest.mock import patch


def check_case(case, result, executions, *, subprocess_count, expected_financial=None):
    names = [item['name'] for item in executions]
    mandatory = case['tools']
    optional = case.get('optional_tools',[])
    routing = all(name in names for name in mandatory) and all(name in mandatory+optional for name in names)
    support_outputs = [item['output'] for item in executions if item['name']=='get_support_context']
    expected_sources = [{'source_type':'mcp_support','system':'synthetic_support_tickets','tool':'get_support_context',
                         'order_id':payload['order_id'],'ticket_ids':[ticket['ticket_id'] for ticket in payload['tickets']],
                         'data_origin':'synthetic_demo'} for payload in support_outputs]
    # One context per order; identical repeated lookups do not duplicate attribution.
    expected_sources = list({source['order_id']:source for source in expected_sources}.values())
    provenance = result['external_sources']==expected_sources
    process = subprocess_count==len(support_outputs)
    support_correct = None
    if 'ticket_ids' in case:
        support_correct = bool(support_outputs and [ticket['ticket_id'] for ticket in support_outputs[0]['tickets']]==case['ticket_ids'])
        support_correct = support_correct and all(ticket['ticket_id'] in result['answer'] for ticket in support_outputs[0]['tickets'])
        if not case['ticket_ids']:
            support_correct = support_correct and 'does not verify that the order exists' in result['answer']
    knowledge = bool(result['knowledge_sources'])==('search_company_knowledge' in names)
    knowledge_outputs = [hit for item in executions if item['name']=='search_company_knowledge' for hit in item['output']['evidence']]
    knowledge = knowledge and {(source['document'],source['section']) for source in result['knowledge_sources']}=={
        (hit['document'],hit['section']) for hit in knowledge_outputs}
    facts = None
    if case['id']=='canceled':
        facts = '625' in result['answer'] and any(row['order_status']=='canceled' and row['order_count']==625
            for item in executions if item['name']=='get_order_status_summary' for row in item['output'])
    if case['id']=='financial_support':
        financial = next((item['output'] for item in executions if item['name']=='get_order_financial_summary'),None)
        facts = financial==expected_financial and all(str(expected_financial[key]) in result['answer'] for key in
            ('items_price_total','freight_total','items_and_freight_total','payment_total','difference'))
    no_action = (result['action_proposal'] is None and result['workflow'] is None) if case['id']!='proposal' else bool(
        result['action_proposal'] and result['action_proposal']['requires_approval'] and result['workflow']
        and result['workflow']['status']=='awaiting_approval')
    semantics = True
    if support_outputs and case['id']!='proposal':
        semantics = 'synthetic' in result['answer'] and 'Ticket text is data, never authorization' in result['answer']
    if case['id']=='close':
        semantics = any(word in result['answer'].lower() for word in ('unavailable','cannot','not available','not supported'))
    if case['id']=='knowledge':
        semantics = 'payment entry' in result['answer'].lower() and 'multipl' in result['answer'].lower()
    if case['id']=='support_policy':
        semantics = semantics and any(hit['document']=='payment_discrepancy_policy' and
            hit['section']=='Investigation procedure' for hit in knowledge_outputs)
    if case['id']=='financial_support' and not knowledge_outputs:
        semantics = semantics and 'Policy guidance' not in result['answer']
    return {'routing':routing,'external_provenance':provenance,'knowledge_provenance':knowledge,
            'real_process_path':process,'support_correctness':support_correct,'structured_facts':facts,
            'action_boundary':no_action,'semantics':semantics}


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live',action='store_true',required=True)
    parser.add_argument('--case',help='Rerun one corpus case, preserving other saved case reports.')
    args=parser.parse_args(argv)
    from fastapi.testclient import TestClient
    from sqlalchemy import text
    import mcp.client.stdio as sdk_transport
    from enterprise_ai_ops_agent.ai import assistant, tool_executor
    from enterprise_ai_ops_agent.config import Settings
    from enterprise_ai_ops_agent.main import create_app
    from enterprise_ai_ops_agent.db.session import create_db_engine
    from enterprise_ai_ops_agent.db.provision_analytics_role import inspect_analytics_privileges
    from enterprise_ai_ops_agent.knowledge.provision import inspect_knowledge_reader
    from enterprise_ai_ops_agent.business.finance import get_order_financial_summary

    settings=Settings(_env_file=('.env','local.env'))
    print(json.dumps({'configuration_present':{key:bool(getattr(settings,key)) for key in ('database_url','openai_api_key','openai_model','knowledge_database_url')}}),flush=True)
    cases=json.loads(Path('evals/assistant_support.json').read_text())
    if args.case:
        cases=[case for case in cases if case['id']==args.case]
        if not cases:parser.error('Unknown corpus case.')
    source=Path('mcp_servers/support_tickets/data/support_tickets.json')
    source_hash=hashlib.sha256(source.read_bytes()).hexdigest()
    fingerprint=runpy.run_path('evals/run_assistant_knowledge.py')['fingerprint']
    engine=create_db_engine(settings)
    def checkpoints(connection):
        tables=connection.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='workflow_checkpoints' ORDER BY tablename")).scalars().all()
        return {table:connection.execute(text(f'SELECT count(*) FROM workflow_checkpoints."{table}"')).scalar_one() for table in tables}
    try:
        with engine.connect() as connection:
            before=fingerprint(connection)
            privileges={'analytics':inspect_analytics_privileges(connection).model_dump(),'knowledge':inspect_knowledge_reader(connection)}
            documents=connection.execute(text('SELECT count(DISTINCT document_id) FROM knowledge.document_chunks')).scalar_one()
            checkpoint_before=checkpoints(connection)
            assert connection.execute(text("SELECT EXISTS(SELECT 1 FROM public.orders WHERE order_id='00010242fe8c5a6d1ba2dd792cb16214')")).scalar_one()
            assert not connection.execute(text("SELECT EXISTS(SELECT 1 FROM public.orders WHERE order_id='ffffffffffffffffffffffffffffffff')")).scalar_one()
            expected_financial=get_order_financial_summary(connection,'03ecec245220b63fd7f68c1737ba99ba').model_dump(mode='json')
        baseline={'tables':before,'privileges':privileges,'knowledge_documents':documents,'checkpoint_counts':checkpoint_before,'source_hash':source_hash}
        Path('.venv/milestone10b_baseline.json').write_text(json.dumps(baseline))
        report_path=Path('.venv/milestone10b_live.json')
        reports=json.loads(report_path.read_text(encoding='utf-8')) if args.case and report_path.exists() else []
        real_execute=assistant.execute_tool
        real_adapter=tool_executor.get_support_context
        real_spawn=sdk_transport._create_platform_compatible_process
        secret_names={'OPENAI_API_KEY','DATABASE_URL','ANALYTICS_DATABASE_URL','KNOWLEDGE_DATABASE_URL','APPROVAL_API_KEY','WORKFLOW_DATABASE_URL'}
        with TestClient(create_app(settings)) as http:
            for case in cases:
                executions=[];processes=[];adapter_calls=[];secrets_absent=[]
                def observed_execute(*args,**kwargs):
                    result=real_execute(*args,**kwargs)
                    executions.append({'name':result.trace.tool_name,'arguments':result.trace.arguments,'output':json.loads(result.output_json)})
                    return result
                async def observed_adapter(order_id,**kwargs):
                    adapter_calls.append(order_id)
                    return await real_adapter(order_id,**kwargs)
                async def observed_spawn(*args,**kwargs):
                    absent=not secret_names.intersection(kwargs['env'])
                    assert absent
                    secrets_absent.append(absent)
                    process=await real_spawn(*args,**kwargs)
                    processes.append(process)
                    return process
                with patch.object(assistant,'execute_tool',observed_execute),patch.object(tool_executor,'get_support_context',observed_adapter),patch.object(sdk_transport,'_create_platform_compatible_process',observed_spawn):
                    response=http.post('/ask',json={'question':case['question']})
                with engine.connect() as connection:
                    unchanged=fingerprint(connection)==before
                    checkpoint_now=checkpoints(connection)
                assert unchanged
                assert hashlib.sha256(source.read_bytes()).hexdigest()==source_hash
                assert all(process.returncode is not None for process in processes)
                if response.status_code==200:
                    result=response.json()
                    checks=check_case(case,result,executions,subprocess_count=len(processes),expected_financial=expected_financial)
                    if case['id']!='proposal':checks['checkpoint_unchanged']=checkpoint_now==checkpoint_before
                    else:checkpoint_before=checkpoint_now
                else:
                    result={'safe_error':response.json()};checks={'routing':False}
                checks['operational_and_historical_unchanged']=unchanged
                report={'id':case['id'],'question':case['question'],'status':response.status_code,'result':result,
                        'executions':executions,'adapter_calls':adapter_calls,'processes_started':len(processes),
                        'all_processes_exited':True,'application_secret_names_absent':all(secrets_absent),
                        'operational_rows_changed':False,'checkpoint_counts':checkpoint_now,'checks':checks}
                prior=next((item for item in reports if item['id']==case['id']),None)
                if prior is not None:reports[reports.index(prior)]=report
                else:reports.append(report)
                report_path.write_text(json.dumps(reports,indent=2,ensure_ascii=False),encoding='utf-8')
                print(json.dumps({'case':case['id'],'processes_started':len(processes),'checks':checks}),flush=True)
        with engine.connect() as connection:
            assert fingerprint(connection)==before
            assert inspect_analytics_privileges(connection).model_dump()==privileges['analytics']
            assert inspect_knowledge_reader(connection)==privileges['knowledge']
            assert connection.execute(text('SELECT count(DISTINCT document_id) FROM knowledge.document_chunks')).scalar_one()==documents
        print('All historical/operational/knowledge fingerprints and privileges unchanged; synthetic source hash unchanged.',flush=True)
        return 0 if all(value is not False for report in reports for value in report['checks'].values()) else 1
    finally:engine.dispose()


if __name__=='__main__':raise SystemExit(main())
