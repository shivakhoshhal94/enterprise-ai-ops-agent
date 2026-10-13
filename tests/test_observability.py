"""Offline operational contracts, including real SDK child cleanup (no database)."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from collections.abc import Mapping
import importlib
import io
import json
import logging
from pathlib import Path
from threading import Barrier
from unittest.mock import MagicMock
from uuid import UUID

import anyio
from fastapi.testclient import TestClient
from mcp.types import CallToolResult
from openai import OpenAIError
import pytest
from sqlalchemy.exc import OperationalError

from enterprise_ai_ops_agent import observability as obs
from enterprise_ai_ops_agent.api import assistant as api, readiness
from enterprise_ai_ops_agent.ai import tool_executor
from enterprise_ai_ops_agent.ai.assistant import (
    AssistantResult, AssistantProviderError, AssistantToolError, AssistantKnowledgeError, AssistantSupportError,
)
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db import session
from enterprise_ai_ops_agent.knowledge import session as knowledge_session
from enterprise_ai_ops_agent.main import create_app


@pytest.fixture
def events():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(obs.EventFormatter())
    obs.LOGGER.addHandler(handler)
    obs.LOGGER.setLevel(logging.INFO)
    yield stream
    obs.LOGGER.removeHandler(handler)
    handler.close()


def records(stream):
    return [json.loads(line) for line in stream.getvalue().splitlines()]


def settings(**kwargs):
    base = dict(database_url=None, analytics_database_url=None, knowledge_database_url=None,
                openai_api_key=None, approval_api_key=None)
    return Settings(_env_file=None, **(base | kwargs))


def client_with(boundary=None):
    app = create_app(settings())
    if boundary is not None:
        app.dependency_overrides[api.get_assistant] = lambda: boundary
    return TestClient(app)


@pytest.mark.parametrize("incoming", [None, "bad", "x"*10000, "\"injection\"", "f"*32])
def test_generated_bounded_request_identity(incoming, events):
    response = client_with().get('/health', headers={} if incoming is None else {'X-Request-ID':incoming})
    identity = response.headers['X-Request-ID']
    assert str(UUID(identity)) == identity and identity != incoming
    assert {row['request_id'] for row in records(events)} == {identity}
    assert obs.request_id.get() is None
    assert response.json() == {'status':'ok'}


@pytest.mark.parametrize('identity',['d96bd9f9-5d9c-4f50-8bab-bd87fc6a983e','D96BD9F9-5D9C-4F50-8BAB-BD87FC6A983E'])
def test_valid_identity_and_safe_http_metadata(events,identity):
    response = client_with().get('/health?password=do-not-log', headers={
        'X-Request-ID':identity, 'Authorization':'Bearer do-not-log', 'X-Approval-Key':'do-not-log'})
    assert response.headers['X-Request-ID'] == identity
    assert response.headers['X-Content-Type-Options'] == 'nosniff'
    assert response.headers['Cache-Control'] == 'no-store'
    row = records(events)[-1]
    assert row['event']=='request_completed' and row['route']=='/health' and row['status_code']==200
    assert row['duration_ms'] >= 0 and row['method']=='GET'
    assert 'do-not-log' not in events.getvalue()


def test_unmatched_path_is_never_logged(events):
    response=client_with().get('/attacker-controlled-content?token=do-not-log')
    assert response.status_code==404
    assert records(events)[-1]['route']=='<unmatched>'
    assert 'attacker-controlled-content' not in events.getvalue()
    assert 'do-not-log' not in events.getvalue()


def test_concurrent_worker_context_isolation(events):
    gate=Barrier(2)
    seen=[]
    def boundary(question):
        identity=obs.request_id.get()
        gate.wait(timeout=5)
        seen.append(identity)
        obs.emit('worker_observation','assistant')
        return AssistantResult(answer='safe',tool_calls=[])
    with client_with(boundary) as client:
        with ThreadPoolExecutor(max_workers=2) as workers:
            futures=[workers.submit(client.post,'/ask',json={'question':'not logged'}) for _ in range(2)]
            responses=[f.result(timeout=10) for f in futures]
    identities={r.headers['X-Request-ID'] for r in responses}
    assert len(identities)==2 and set(seen)==identities
    rows=records(events)
    for identity in identities:
        own=[r for r in rows if r['request_id']==identity]
        assert [r['event'] for r in own]==['request_started','assistant_request_started',
            'worker_observation','assistant_request_completed','request_completed']
    assert obs.request_id.get() is None


@pytest.mark.parametrize('key',['PASSWORD','client_secret','access_token','API_KEY','ApiKey',
    'Authorization','reader_credentials','database_url','ANALYTICS_DATABASE_URL','knowledge_database_url'])
def test_nested_secret_key_redaction(key):
    value={'safe':{'list':[{key:'never-show'}]},key:'never-show'}
    normalized=obs.redact(value)
    assert normalized[key]=='[redacted]'
    assert normalized['safe']['list'][0][key]=='[redacted]'
    assert 'never-show' not in json.dumps(normalized)


def test_json_formatter_deterministic_content_free():
    record=logging.LogRecord('test',logging.INFO,'',1,'raw-provider-secret',(),None)
    record.event_fields={'outcome':'completed','password':'never-show','duration_ms':1.0}
    formatter=obs.EventFormatter()
    assert formatter.format(record)==formatter.format(record)
    assert json.loads(formatter.format(record))=={'outcome':'completed','password':'[redacted]','duration_ms':1.0}
    assert '\n' not in formatter.format(record)


def test_redaction_bounded_and_hostile_object_safe():
    class BadMapping(Mapping):
        def __iter__(self):raise RuntimeError('never render')
        def __len__(self):raise RuntimeError('never render')
        def __getitem__(self,key):raise RuntimeError('never render')
    class BadObject:
        def __str__(self):raise RuntimeError('never render')
        def __repr__(self):raise RuntimeError('never render')
    assert obs.redact(BadMapping())=='[unavailable]'
    assert obs.redact(BadObject())=='[unsupported]'
    cycle={};cycle['self']=cycle
    assert '[truncated]' in json.dumps(obs.redact(cycle))
    assert obs.redact(float('nan')) is None


@pytest.mark.parametrize('failure',['logger','normalizer'])
def test_logging_failure_does_not_break_requests(monkeypatch,failure):
    target=(obs.LOGGER,'info') if failure=='logger' else (obs,'redact')
    monkeypatch.setattr(*target,MagicMock(side_effect=RuntimeError('logging broken')))
    response=client_with(lambda q:AssistantResult(answer='safe',tool_calls=[])).post('/ask',json={'question':'safe'})
    assert response.status_code==200 and response.json()['answer']=='safe'


def test_assistant_telemetry_no_question_answer_body_or_result(events):
    result=AssistantResult(answer='ANSWER-MUST-NOT-LOG',tool_calls=[
        tool_executor.ToolCallTrace(tool_name='run_analytical_query',arguments={'sql':'SELECT SENSITIVE'})])
    response=client_with(lambda q:result).post('/ask',json={'question':'QUESTION-MUST-NOT-LOG'},
                                             headers={'Authorization':'SECRET-AUTH'})
    assert response.status_code==200
    row=next(r for r in records(events) if r['event']=='assistant_request_completed')
    assert row['tool_names']==['run_analytical_query'] and row['tool_count']==1
    assert not row['has_workflow'] and not row['has_action_proposal']
    assert not row['has_knowledge_sources'] and not row['has_external_sources']
    for text in ('ANSWER-MUST-NOT-LOG','QUESTION-MUST-NOT-LOG','SELECT SENSITIVE','SECRET-AUTH'):
        assert text not in events.getvalue()


@pytest.mark.parametrize('name',sorted(obs.TOOLS))
def test_central_tool_and_dependency_telemetry_no_payload(monkeypatch,events,name):
    result=tool_executor.ToolExecution(trace=tool_executor.ToolCallTrace(tool_name=name,arguments={}),
                                      output_json='SQL PASSAGE TICKET PROVIDER CONTENT')
    monkeypatch.setattr(tool_executor,'_execute_tool',lambda *a,**k:result)
    assert tool_executor.execute_tool(None,name,'ARGUMENT-SECRET') is result
    rows=records(events)
    assert [r['event'] for r in rows]==['tool_execution_started','dependency_started','dependency_completed','tool_execution_completed']
    assert rows[-1]['tool_name']==name and rows[-1]['duration_ms']>=0
    assert 'ARGUMENT-SECRET' not in events.getvalue() and result.output_json not in events.getvalue()


def test_unknown_tool_name_and_arguments_not_logged(events):
    with pytest.raises(tool_executor.ToolExecutionError):
        tool_executor.execute_tool(None,'attacker-secret','password')
    assert 'attacker-secret' not in events.getvalue() and 'password' not in events.getvalue()
    assert records(events)[-1]['event']=='tool_execution_failed'


@pytest.mark.parametrize('error,category,status',[
    (AssistantProviderError('never-log'),'provider_unavailable',502),
    (OpenAIError('never-log'),'provider_unavailable',502),
    (AssistantToolError('never-log'),'database_unavailable',503),
    (AssistantKnowledgeError('never-log'),'knowledge_unavailable',503),
    (AssistantSupportError('never-log'),'support_system_unavailable',503),
    (AssistantSupportError('never-log',code='timeout'),'timeout',504),
    (OperationalError(None,None,Exception('never-log')),'database_unavailable',503),
    (RuntimeError('never-log'),'internal_error',500),
])
def test_safe_classification_and_unchanged_http_mapping(error,category,status,events):
    assert obs.error_category(error)==category
    response=client_with(MagicMock(side_effect=error)).post('/ask',json={'question':'not logged'})
    assert response.status_code==status and set(response.json())=={'detail'}
    row=next(r for r in records(events) if r['event']=='assistant_request_failed')
    assert row['error_category']==category
    assert 'never-log' not in events.getvalue() and 'never-log' not in response.text
    assert records(events)[-1]['event']=='request_failed'


def test_unhandled_500_has_headers_and_preserves_default_response(events):
    app=create_app(settings())
    @app.get('/broken')
    def broken():raise RuntimeError('secret')
    with TestClient(app,raise_server_exceptions=False) as client:
        response=client.get('/broken')
    assert response.status_code==500 and response.text=='Internal Server Error'
    assert UUID(response.headers['X-Request-ID'])
    assert response.headers['X-Content-Type-Options']=='nosniff'
    assert 'secret' not in events.getvalue()


def ready_settings():
    return settings(database_url='postgresql+psycopg://admin:admin-secret@localhost/example',
        analytics_database_url='postgresql+psycopg://enterprise_ai_ops_reader:reader-secret@localhost/example',
        knowledge_database_url='postgresql+psycopg://enterprise_ai_ops_knowledge_reader:knowledge-secret@localhost/example',
        openai_api_key='key-secret',approval_api_key='approval-secret')


def fake_ready_engines(monkeypatch):
    factories={}
    def engine(*args,**kwargs):
        value=MagicMock()
        connection=value.connect.return_value.execution_options.return_value.__enter__.return_value
        connection.execute.return_value.mappings.return_value.all.return_value=[
            {'embedding_model':'text-embedding-3-small','embedding_dimensions':1536}]
        return value
    for name in ('create_db_engine','create_analytics_engine','create_knowledge_engine'):
        factory=MagicMock(side_effect=engine)
        monkeypatch.setattr(readiness,name,factory)
        factories[name]=factory
    return factories


def test_ready_success_readonly_bounded_and_disposes(monkeypatch):
    factories=fake_ready_engines(monkeypatch)
    engines=[]
    for factory in factories.values():
        original=factory.side_effect
        factory.side_effect=lambda *a,_original=original,**k:engines.append(_original(*a,**k)) or engines[-1]
    result=readiness.check_readiness(ready_settings())
    assert result.ready and set(result.checks.model_dump().values())=={'ready'}
    assert len(engines)==4
    for engine in engines:
        engine.connect.return_value.execution_options.assert_called_once_with(postgresql_readonly=True)
        connection=engine.connect.return_value.execution_options.return_value.__enter__.return_value
        statements=[str(call.args[0]) for call in connection.execute.call_args_list]
        assert all(s.startswith('SELECT ') for s in statements)
        assert "SELECT set_config('statement_timeout','1000',true)" in statements
        assert not any('pg_advisory' in s or 'setup' in s for s in statements)
        engine.dispose.assert_called_once()
    for factory in factories.values():
        assert all(call.kwargs=={'connect_timeout':2} for call in factory.call_args_list)


@pytest.mark.parametrize('component',['database','analytics','knowledge','workflow'])
def test_ready_live_dependency_failure_safe_503(monkeypatch,events,component):
    factories=fake_ready_engines(monkeypatch)
    factory=factories[{'database':'create_db_engine','workflow':'create_db_engine',
                       'analytics':'create_analytics_engine','knowledge':'create_knowledge_engine'}[component]]
    if component=='workflow':factory.side_effect=[MagicMock(),RuntimeError('password connection secret')]
    elif component=='database':factory.side_effect=[RuntimeError('password connection secret'),MagicMock()]
    else:factory.side_effect=RuntimeError('password connection secret')
    with TestClient(create_app(ready_settings())) as client:
        response=client.get('/ready')
        assert client.get('/health').json()=={'status':'ok'}
    assert response.status_code==503 and not response.json()['ready']
    assert response.json()['checks'][component]=='unavailable'
    assert 'password connection secret' not in response.text and 'password connection secret' not in events.getvalue()


@pytest.mark.parametrize('missing',['openai_api_key','approval_api_key','openai_model'])
def test_ready_missing_configuration_503(monkeypatch,missing,events):
    fake_ready_engines(monkeypatch)
    config=ready_settings().model_copy(update={missing:'' if missing=='openai_model' else None})
    with TestClient(create_app(config)) as client:response=client.get('/ready')
    name='approval_configuration' if missing=='approval_api_key' else 'openai_configuration'
    assert response.status_code==503 and response.json()['checks'][name]=='unavailable'
    assert any(row['component']==name and row.get('error_category')=='configuration_error' for row in records(events))


def test_ready_mcp_unavailable_no_launch(monkeypatch,tmp_path):
    from enterprise_ai_ops_agent.integrations.support_tickets import client as support
    fake_ready_engines(monkeypatch)
    monkeypatch.setattr(readiness,'SERVER_DIRECTORY',tmp_path)
    launch=MagicMock(side_effect=AssertionError('must not launch'))
    monkeypatch.setattr(support,'stdio_client',launch)
    with TestClient(create_app(ready_settings())) as client:response=client.get('/ready')
    assert response.status_code==503 and response.json()['checks']['mcp_configuration']=='unavailable'
    launch.assert_not_called()


@pytest.mark.parametrize('profile',[[], [{'embedding_model':'different','embedding_dimensions':1536}],
                                  [{'embedding_model':'text-embedding-3-small','embedding_dimensions':3}]])
def test_ready_requires_existing_compatible_knowledge_profile(monkeypatch,profile):
    engine=MagicMock()
    connection=engine.connect.return_value.execution_options.return_value.__enter__.return_value
    connection.execute.return_value.mappings.return_value.all.return_value=profile
    assert readiness.database_check(ready_settings(),lambda *a,**k:engine,'knowledge',[
        'SELECT DISTINCT embedding_model, embedding_dimensions FROM knowledge.document_chunks LIMIT 2'])=='unavailable'
    engine.dispose.assert_called_once()


def test_ready_cleanup_failure_is_safe_component_failure(events):
    engine=MagicMock()
    engine.dispose.side_effect=RuntimeError('password-secret')
    assert readiness.database_check(ready_settings(),lambda *a,**k:engine,'database',['SELECT 1'])=='unavailable'
    assert 'password-secret' not in events.getvalue()


def test_proposal_workflow_and_source_presence_telemetry_without_live_state(events):
    from uuid import uuid4
    from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
    from enterprise_ai_ops_agent.ai.knowledge_contracts import KnowledgeSource
    from enterprise_ai_ops_agent.ai.support_contracts import ExternalSource
    from enterprise_ai_ops_agent.workflows.contracts import WorkflowSummary
    proposal=OrderReviewCaseProposal(order_id='a'*32,reason='SECRET-REASON',priority='low',proposal_id=uuid4())
    # Only exercise observational flags: no database, graph, or durable state exists.
    result=AssistantResult(answer='SECRET-ANSWER',tool_calls=[],action_proposal=proposal,
        workflow=WorkflowSummary(workflow_id=str(uuid4()),status='awaiting_approval',proposal_id=proposal.proposal_id,
            order_id=proposal.order_id,reason=proposal.reason,priority=proposal.priority,action_type=proposal.action_type),
        knowledge_sources=[KnowledgeSource(document='demo',title='SECRET-DOCUMENT',section='section')],
        external_sources=[ExternalSource(order_id='a'*32,ticket_ids=['DEMO-SUP-001'])])
    observed=obs.observe_assistant(lambda:result)
    assert observed() is result
    row=records(events)[-1]
    assert all(row[key] for key in ('has_workflow','has_action_proposal','has_knowledge_sources','has_external_sources'))
    for value in ('SECRET-REASON','SECRET-ANSWER','SECRET-DOCUMENT',str(proposal.proposal_id),'DEMO-SUP-001'):
        assert value not in events.getvalue()


def test_health_ready_and_ordinary_ask_never_launch_or_call_provider(monkeypatch):
    from enterprise_ai_ops_agent.integrations.support_tickets import client as support
    fake_ready_engines(monkeypatch)
    no_provider=MagicMock(side_effect=AssertionError('unexpected provider'))
    no_process=MagicMock(side_effect=AssertionError('unexpected process'))
    monkeypatch.setattr(api,'create_openai_client',no_provider)
    monkeypatch.setattr(support,'stdio_client',no_process)
    app=create_app(ready_settings())
    app.dependency_overrides[api.get_assistant]=lambda:lambda q:AssistantResult(answer='625',tool_calls=[])
    with TestClient(app) as client:
        assert client.get('/health').status_code==200
        assert client.get('/ready').status_code==200
        assert client.post('/ask',json={'question':'Count canceled orders'}).status_code==200
    no_provider.assert_not_called();no_process.assert_not_called()


@pytest.mark.parametrize('kind',['analytics','knowledge'])
def test_readiness_reader_missing_no_admin_fallback(monkeypatch,kind):
    factory=MagicMock(side_effect=AssertionError('must not use admin'))
    module=session if kind=='analytics' else knowledge_session
    monkeypatch.setattr(module,'create_engine',factory)
    real=module.create_analytics_engine if kind=='analytics' else module.create_knowledge_engine
    assert readiness.database_check(settings(database_url=ready_settings().database_url),real,kind,['SELECT 1'])=='unavailable'
    factory.assert_not_called()


@pytest.mark.parametrize('kind',['database','analytics','knowledge'])
def test_pool_bounded_lazy_and_secret_safe(kind):
    factory={'database':session.create_db_engine,'analytics':session.create_analytics_engine,
             'knowledge':knowledge_session.create_knowledge_engine}[kind]
    engine=factory(ready_settings())
    try:
        assert engine.pool.checkedout()==0 and engine.pool.size()==2
        assert engine.pool._max_overflow==0 and engine.pool._pre_ping is True
        assert engine.pool.timeout()==2 and engine.hide_parameters is True
    finally:engine.dispose()


def test_imports_and_startup_no_connections(monkeypatch):
    import psycopg
    connect=MagicMock(side_effect=AssertionError('no import connection'))
    monkeypatch.setattr(psycopg,'connect',connect)
    monkeypatch.setattr(psycopg.Connection,'connect',connect)
    import enterprise_ai_ops_agent.main as main
    importlib.reload(main)
    with TestClient(main.create_app(settings())) as client:
        assert client.get('/health').status_code==200
    connect.assert_not_called()


def test_shutdown_owned_engine_and_provider_cleanup(monkeypatch,fake_client,response_factory):
    engine=MagicMock()
    provider=fake_client([response_factory(answer='safe')])
    monkeypatch.setattr(api,'create_db_engine',lambda settings:engine)
    monkeypatch.setattr(api,'create_openai_client',lambda settings:provider)
    with TestClient(create_app(settings())) as client:
        assert client.post('/ask',json={'question':'Explain semantics'}).status_code==200
    engine.dispose.assert_called_once();provider.close.assert_called_once()


def test_openapi_ready_and_unchanged_business_contract():
    from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
    schema=create_app(settings()).openapi()
    assert '/ready' in schema['paths']
    assert set(schema['components']['schemas']['ReadinessChecks']['properties'])=={
        'database','analytics','knowledge','workflow','openai_configuration','approval_configuration','mcp_configuration'}
    assert set(schema['components']['schemas']['AssistantResult']['properties'])=={
        'answer','tool_calls','action_proposal','workflow','knowledge_sources','external_sources'}
    assert len(TOOL_SCHEMAS)==7 and {tool['name'] for tool in TOOL_SCHEMAS}==obs.TOOLS


@pytest.mark.parametrize('mode',['success','malformed','failure','timeout'])
def test_real_mcp_process_is_reaped_after_every_outcome(monkeypatch,mode):
    import mcp.client.stdio as transport
    from enterprise_ai_ops_agent.integrations.support_tickets import client as support
    processes=[]
    injected=[]
    spawn=transport._create_platform_compatible_process
    async def observed(*args,**kwargs):
        process=await spawn(*args,**kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(transport,'_create_platform_compatible_process',observed)
    if mode!='success':
        async def controlled(*args,**kwargs):
            injected.append(mode)
            if mode=='timeout':
                await anyio.sleep(60)  # Cancelled by bounded adapter deadline, never waited out.
            if mode=='failure':raise RuntimeError('untrusted protocol secret')
            return CallToolResult(content=[],structured_content={'wrong':'shape'})
        monkeypatch.setattr(support.Client,'call_tool',controlled)
    async def run():
        with anyio.fail_after(20):
            if mode=='success':
                result=await support.get_support_context('03ecec245220b63fd7f68c1737ba99ba')
                assert len(result.tickets)==3
            else:
                with pytest.raises(support.SupportIntegrationError) as caught:
                    await support.get_support_context('03ecec245220b63fd7f68c1737ba99ba',
                        timeout_seconds=2 if mode=='timeout' else support.DEFAULT_TIMEOUT_SECONDS)
                assert caught.value.code=={'malformed':'invalid_response','timeout':'timeout','failure':'unavailable'}[mode]
    anyio.run(run)
    assert len(processes)==1 and processes[0].returncode is not None
    if mode in ('malformed','failure'):assert injected==[mode]
