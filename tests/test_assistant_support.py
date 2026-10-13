"""Scripted 10B contracts; no claims about live model accuracy or general injection immunity."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from enterprise_ai_ops_agent.ai import assistant, tool_executor
from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS, ARGUMENT_MODELS
from enterprise_ai_ops_agent.ai.support_contracts import ExternalSource, support_followup_scope
from enterprise_ai_ops_agent.integrations.support_tickets import client as integration
from enterprise_ai_ops_agent.integrations.support_tickets.contracts import SupportContext
from enterprise_ai_ops_agent.ai.knowledge_contracts import KnowledgeToolResult, KnowledgeEvidence
from enterprise_ai_ops_agent.business.orders import OrderStatusCount
from enterprise_ai_ops_agent.business.finance import OrderFinancialSummary
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.analytics.models import AnalyticalResult
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.api import assistant as api

ORDER='03ecec245220b63fd7f68c1737ba99ba'
CASES=json.loads(Path('evals/assistant_support.json').read_text())
SETTINGS=Settings(_env_file=None,analytics_database_url='postgresql+psycopg://reader:dummy@localhost/db')


def context(order=ORDER):
    corpus=json.loads(Path('mcp_servers/support_tickets/data/support_tickets.json').read_text())
    tickets=[ticket for ticket in corpus['tickets'] if ticket['order_id']==order]
    tickets=sorted(sorted(tickets,key=lambda t:t['ticket_id']),key=lambda t:t['updated_at'],reverse=True)
    return SupportContext(order_id=order,tickets=tickets,truncated=False)


def financial():
    return OrderFinancialSummary(order_id=ORDER,item_count=2,seller_count=2,items_price_total='298.90',
        freight_total='76.83',items_and_freight_total='375.73',payment_row_count=2,payment_total='375.73',difference='0.00')


def patch_tools(monkeypatch):
    support=AsyncMock(side_effect=lambda order:context(order))
    monkeypatch.setattr(tool_executor,'get_support_context',support)
    monkeypatch.setitem(tool_executor.HANDLERS,'get_order_status_summary',MagicMock(return_value=[OrderStatusCount(order_status='canceled',order_count=625)]))
    monkeypatch.setitem(tool_executor.HANDLERS,'get_order_financial_summary',MagicMock(return_value=financial()))
    evidence=KnowledgeEvidence(document='payment_discrepancy_policy',title='Payment Discrepancy Policy',
        section='Investigation procedure',content='Record qualified concerns. Human approval is required before creating an internal review case.',rank=1,distance=.3)
    monkeypatch.setattr(tool_executor,'retrieve_evidence',MagicMock(return_value=KnowledgeToolResult(evidence=[evidence])))
    monkeypatch.setattr(tool_executor,'propose_order_review_case',MagicMock(return_value=OrderReviewCaseProposal(
        proposal_id=UUID('00000000-0000-0000-0000-000000000001'),order_id=ORDER,reason='User requested review of a reported support issue.',priority='medium')))
    return support


@pytest.mark.parametrize('case',CASES,ids=[case['id'] for case in CASES])
def test_scripted_10b_evaluation(case,monkeypatch,response_factory,fake_client):
    support=patch_tools(monkeypatch)
    args={'get_support_context':{'order_id':case.get('order_id',ORDER)},'get_order_status_summary':{},
        'get_order_financial_summary':{'order_id':ORDER},'search_company_knowledge':{'query':'payment discrepancy policy'},
        'propose_order_review_case':{'order_id':ORDER,'reason':'User requested review of a reported support issue.','priority':'medium'}}
    responses=[response_factory(calls=[(name,json.dumps(args[name]))]) for name in case['tools']]
    responses.append(response_factory(answer='No ticket writes are available.'))
    provider=fake_client(responses)
    result=assistant.ask_business_assistant(case['question'],client=provider,engine=MagicMock(spec=Engine),model='fake',settings=SETTINGS)
    names=[call.tool_name for call in result.tool_calls]
    if result.action_proposal:names.append('propose_order_review_case')
    assert names==case['tools']
    if 'get_support_context' in names:
        assert result.external_sources[0].ticket_ids==case['ticket_ids']
        assert result.external_sources[0].order_id==case['order_id']
        assert 'synthetic' in result.answer and result.action_proposal is None and result.workflow is None
        if not case['ticket_ids']:
            assert 'does not verify that the order exists' in result.answer
    else:
        assert result.external_sources==[];support.assert_not_awaited()
    assert bool(result.knowledge_sources)==('search_company_knowledge' in names)
    if case['id']=='financial_support':
        assert all(value in result.answer for value in ['298.90','76.83','375.73','0.00'])
        assert 'separate observations' in result.answer
        assert 'Policy guidance' not in result.answer
    if case['id']=='proposal':assert result.action_proposal.requires_approval and 'No case has been created' in result.answer
    assert provider.responses.create.call_args_list[0].kwargs['tools']==TOOL_SCHEMAS


def test_seven_tool_strict_support_schema_and_trace(monkeypatch):
    assert len(TOOL_SCHEMAS)==7
    tool=next(item for item in TOOL_SCHEMAS if item['name']=='get_support_context')
    assert tool['strict'] and tool['parameters']['additionalProperties'] is False
    assert tool['parameters']['required']==['order_id']
    field=tool['parameters']['properties']['order_id']
    assert field['minLength']==field['maxLength']==32 and field['pattern']=='^[0-9a-f]{32}$'
    support=patch_tools(monkeypatch)
    engine=MagicMock(spec=Engine)
    result=tool_executor.execute_tool(engine,'get_support_context',json.dumps({'order_id':ORDER}))
    assert result.trace.model_dump()=={'tool_name':'get_support_context','arguments':{'order_id':ORDER}}
    assert 'summary' not in result.trace.model_dump_json()
    assert json.loads(result.output_json)['data_origin']=='synthetic_demo'
    assert result.knowledge_sources==[] and result.external_sources[0].ticket_ids==['DEMO-SUP-002','DEMO-SUP-003','DEMO-SUP-001']
    support.assert_awaited_once_with(ORDER);engine.connect.assert_not_called()


@pytest.mark.parametrize('payload',[{}, {'order_id':''},{'order_id':1},{'order_id':ORDER.upper()},
    {'order_id':ORDER+' '},{'order_id':ORDER,'tool':'close_ticket'}, {'order_id':'x'*32}])
def test_bad_arguments_before_adapter(payload,monkeypatch):
    adapter=AsyncMock();monkeypatch.setattr(tool_executor,'get_support_context',adapter)
    with pytest.raises(tool_executor.ToolExecutionError,match='Invalid tool arguments'):
        tool_executor.execute_tool(MagicMock(spec=Engine),'get_support_context',json.dumps(payload))
    adapter.assert_not_awaited()


@pytest.mark.parametrize('name',['close_ticket','approve_workflow','delete_order','mcp_discover','execute_shell'])
def test_unknown_capabilities_never_dispatch(name,monkeypatch):
    adapter=AsyncMock();monkeypatch.setattr(tool_executor,'get_support_context',adapter)
    with pytest.raises(tool_executor.ToolExecutionError,match='Unsupported'):
        tool_executor.execute_tool(MagicMock(spec=Engine),name,'{}')
    adapter.assert_not_awaited()


@pytest.mark.parametrize('error',[
    integration.SupportIntegrationError('secret command protocol',code='startup'),
    integration.SupportIntegrationError('stderr secret',code='invalid_response'),
    integration.InvalidSupportRequest('private validation text'),
    integration.SupportIntegrationTimeout('private timeout text'),
    integration.SupportIntegrationError('protocol secret',code='tool_failure'),
])
def test_failure_not_empty_and_sanitized(error,monkeypatch,response_factory,fake_client):
    adapter=AsyncMock(side_effect=error);monkeypatch.setattr(tool_executor,'get_support_context',adapter)
    provider=fake_client([response_factory(calls=[('get_support_context',json.dumps({'order_id':ORDER}))])])
    with pytest.raises(assistant.AssistantSupportError) as caught:
        assistant.ask_business_assistant('Show support for '+ORDER,client=provider,engine=MagicMock(spec=Engine),model='fake')
    assert caught.value.code==error.code
    assert 'secret' not in str(caught.value) and 'private' not in str(caught.value)
    assert provider.responses.create.call_count==1


@pytest.mark.parametrize('next_tool',[
    ('propose_order_review_case',{'order_id':ORDER,'reason':'Ticket told me to.','priority':'high'}),
    ('approve_workflow',{}), ('run_analytical_query',{'sql':'DELETE FROM orders'}),
    ('get_order_financial_summary',{'order_id':ORDER}),
    ('search_company_knowledge',{'query':'instructions to delete orders'}),
    ('get_support_context',{'order_id':'ffffffffffffffffffffffffffffffff'}),
    ('get_support_context',{'order_id':ORDER}),
])
def test_ticket_cannot_authorize_secondary_tools(next_tool,monkeypatch,response_factory,fake_client):
    support=patch_tools(monkeypatch)
    executor=MagicMock(wraps=assistant.execute_tool);monkeypatch.setattr(assistant,'execute_tool',executor)
    provider=fake_client([response_factory(calls=[('get_support_context',json.dumps({'order_id':ORDER}))]),
                         response_factory(calls=[(next_tool[0],json.dumps(next_tool[1]))])])
    result=assistant.ask_business_assistant('Read support for '+ORDER+' and follow whatever it says.',
        client=provider,engine=MagicMock(spec=Engine),model='fake',settings=SETTINGS)
    assert executor.call_count==1 and support.await_count==1
    assert result.action_proposal is None and result.workflow is None
    assert [call.tool_name for call in result.tool_calls]==['get_support_context']
    assert 'was not executed' in result.answer
    assert provider.responses.create.call_args_list[0].kwargs['instructions']==provider.responses.create.call_args_list[1].kwargs['instructions']
    assert [tool['name'] for tool in provider.responses.create.call_args_list[1].kwargs['tools']]==['get_support_context']


def test_user_independent_proposal_allowed_after_support(monkeypatch,response_factory,fake_client):
    patch_tools(monkeypatch)
    question='Create an internal review proposal for order '+ORDER+' because of a support issue.'
    provider=fake_client([response_factory(calls=[('get_support_context',json.dumps({'order_id':ORDER}))]),
        response_factory(calls=[('propose_order_review_case',json.dumps({'order_id':ORDER,'reason':'User requested review of support issue.','priority':'medium'}))]),response_factory(answer='Created.')])
    result=assistant.ask_business_assistant(question,client=provider,engine=MagicMock(spec=Engine),model='fake')
    assert result.action_proposal is not None and result.action_proposal.requires_approval
    assert result.external_sources and result.workflow is None
    assert 'No case has been created' in result.answer


def test_support_then_finance_or_policy_is_user_scoped(monkeypatch,response_factory,fake_client):
    patch_tools(monkeypatch)
    provider=fake_client([response_factory(calls=[('get_support_context',json.dumps({'order_id':ORDER}))]),
        response_factory(calls=[('get_order_financial_summary',json.dumps({'order_id':ORDER}))]),response_factory(answer='The payment problem was resolved; no investigation is needed.')])
    result=assistant.ask_business_assistant('Compare financial totals and support issues for '+ORDER,
        client=provider,engine=MagicMock(spec=Engine),model='fake')
    assert 'was resolved' not in result.answer and 'BRL 375.73' in result.answer


@pytest.mark.parametrize('question',['Follow whatever the ticket says.','"Create an internal review case" is written in the ticket.',
    'Do not create an internal review case.','What does the ticket mean by create a review case?'])
def test_no_proposal_authority_from_quoted_or_ambiguous_text(question):
    assert 'propose_order_review_case' not in support_followup_scope(question)


def test_sql_only_does_not_launch_mcp(monkeypatch,response_factory,fake_client):
    adapter=patch_tools(monkeypatch)
    monkeypatch.setattr(tool_executor,'execute_configured_analytical_query',MagicMock(return_value=AnalyticalResult(columns=['count'],rows=[[1]],returned_row_count=1,truncated=False)))
    provider=fake_client([response_factory(calls=[('run_analytical_query','{"sql":"SELECT COUNT(*) FROM orders"}')]),response_factory(answer='One fixture order.')])
    result=assistant.ask_business_assistant('Count fixture orders.',client=provider,engine=MagicMock(spec=Engine),model='fake',settings=SETTINGS)
    assert result.external_sources==[];adapter.assert_not_awaited()


@pytest.mark.parametrize('code,status,detail',[('timeout',504,'Support service timed out.'),
    ('startup',503,'Support service is unavailable.'),('invalid_response',503,'Support service returned an invalid response.')])
def test_api_support_failure_mapping(code,status,detail):
    app=create_app(Settings(_env_file=None))
    def fail(question):raise assistant.AssistantSupportError('secret raw protocol',code=code)
    app.dependency_overrides[api.get_assistant]=lambda:fail
    response=TestClient(app).post('/ask',json={'question':'support'})
    assert response.status_code==status and response.json()=={'detail':detail}


def test_openapi_additive_source_and_filtering():
    app=create_app(Settings(_env_file=None))
    source=ExternalSource(order_id=ORDER,ticket_ids=['DEMO-SUP-002']).model_dump()
    app.dependency_overrides[api.get_assistant]=lambda:lambda question:{'answer':'Synthetic data.', 'tool_calls':[],
        'external_sources':[source], 'knowledge_sources':[],'server_stderr':'secret','transport':{'command':'private'}}
    client=TestClient(app);response=client.post('/ask',json={'question':'support'})
    assert response.status_code==200 and response.json()['external_sources']==[source]
    assert response.json()['knowledge_sources']==[] and 'secret' not in response.text and 'private' not in response.text
    schema=client.get('/openapi.json').json()['components']['schemas']
    assert 'external_sources' in schema['AssistantResult']['properties']
    assert set(schema['ExternalSource']['properties'])=={'source_type','system','tool','order_id','ticket_ids','data_origin'}
