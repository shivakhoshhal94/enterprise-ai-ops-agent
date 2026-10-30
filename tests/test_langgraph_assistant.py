"""Offline parity and boundary tests; scripted providers do not measure model accuracy."""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError
from pydantic import ValidationError
from sqlalchemy import Engine

from enterprise_ai_ops_agent.ai import assistant, langgraph_assistant as graph, tool_executor
from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
from enterprise_ai_ops_agent.analytics.models import AnalyticalResult
from enterprise_ai_ops_agent.business.customers import CustomerOrderHistory
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.api import assistant as api
from enterprise_ai_ops_agent.operations import service
from enterprise_ai_ops_agent.integrations.support_tickets.client import SupportIntegrationError
from test_assistant_support import ORDER, patch_tools


ARGS = {
    'get_order_status_summary': {},
    'get_customer_order_history': {'customer_unique_id': 'repeat_customer'},
    'get_order_financial_summary': {'order_id': ORDER},
    'run_analytical_query': {'sql': 'SELECT COUNT(*) AS n FROM orders'},
    'search_company_knowledge': {'query': 'Payment discrepancy policy'},
    'get_support_context': {'order_id': ORDER},
    'propose_order_review_case': {'order_id': ORDER, 'reason': 'User requested review.', 'priority': 'medium'},
}


@pytest.fixture
def controlled_dependencies(monkeypatch):
    support = patch_tools(monkeypatch)
    monkeypatch.setitem(tool_executor.HANDLERS, 'get_customer_order_history', MagicMock(
        return_value=CustomerOrderHistory(customer_unique_id='repeat_customer', found=True,
                                         customer_record_count=17, order_count=0, orders=[])))
    analytics = MagicMock(return_value=AnalyticalResult(
        columns=['n'], rows=[[99_441]], returned_row_count=1, truncated=False))
    monkeypatch.setattr(tool_executor, 'execute_configured_analytical_query', analytics)
    writer = MagicMock(side_effect=AssertionError('Conversational graph reached business writer'))
    monkeypatch.setattr(service, 'create_order_review_case', writer)
    return support, analytics, writer


@pytest.mark.parametrize('names', [(name,) for name in ARGS] + [
    ('get_order_financial_summary', 'get_support_context'),
    ('get_support_context', 'get_order_financial_summary'),
    ('get_support_context', 'search_company_knowledge'),
    ('get_order_financial_summary', 'search_company_knowledge', 'get_support_context'),
])
def test_same_tool_boundary_and_response_contract(names, controlled_dependencies, response_factory, fake_client):
    question = f'Compare financial totals, support issues and policy for {ORDER}.'
    configuration = Settings(_env_file=None, analytics_database_url='postgresql+psycopg://reader:dummy@localhost/db')
    results = []
    for run in (assistant.ask_business_assistant, graph.ask_langgraph_assistant):
        provider = fake_client([
            *[response_factory(calls=[(name, json.dumps(ARGS[name]))],
                               extra_output=[{'id': 'rs_fixture', 'type': 'reasoning', 'summary': []}])
              for name in names],
            response_factory(answer='Fixture answer.'),
        ])
        engine = MagicMock(spec=Engine)
        result = run(question, client=provider, engine=engine, model='fixture', settings=configuration)
        assert isinstance(result, assistant.AssistantResult)
        assert result.workflow is None
        assert [call.tool_name for call in result.tool_calls] == [
            name for name in names if name != 'propose_order_review_case']
        if 'propose_order_review_case' in names:
            assert result.action_proposal.requires_approval
            assert 'No case has been created' in result.answer
        if 'get_support_context' in names:
            assert result.external_sources[0].order_id == ORDER
        if 'search_company_knowledge' in names:
            assert result.knowledge_sources and 'not official Olist policy' in result.answer
        first = provider.responses.create.call_args_list[0].kwargs
        assert first['tools'] == TOOL_SCHEMAS
        assert first['parallel_tool_calls'] is False and first['store'] is False
        continuation = provider.responses.create.call_args_list[1].kwargs['input']
        assert any(getattr(item, 'type', None) == 'reasoning' for item in continuation)
        assert continuation[-1]['type'] == 'function_call_output'
        assert 'rs_fixture' not in result.model_dump_json()
        results.append(result.model_dump(mode='json'))
    assert results[0] == results[1]
    controlled_dependencies[2].assert_not_called()


@pytest.mark.parametrize('sql', ['DELETE FROM orders', 'SELECT * FROM pg_catalog.pg_tables', 'SELECT pg_sleep(20)'])
def test_sql_rejections_remain_guarded(sql, controlled_dependencies, response_factory, fake_client):
    provider = fake_client([response_factory(calls=[('run_analytical_query', json.dumps({'sql': sql}))])] * 2)
    engine = MagicMock(spec=Engine)
    with pytest.raises(assistant.AssistantToolError, match='single correction'):
        graph.ask_langgraph_assistant('Query', client=provider, engine=engine, model='fixture')
    controlled_dependencies[1].assert_not_called()
    engine.connect.assert_not_called()
    assert provider.responses.create.call_count == 2


def test_correction_recovery(controlled_dependencies, response_factory, fake_client):
    provider = fake_client([
        response_factory(calls=[('run_analytical_query', '{"sql":"DELETE FROM orders"}')]),
        response_factory(calls=[('run_analytical_query', json.dumps(ARGS['run_analytical_query']))]),
        response_factory(answer='Recovered.'),
    ])
    result = graph.ask_langgraph_assistant('Count orders', client=provider, engine=MagicMock(), model='fixture',
        settings=Settings(_env_file=None, analytics_database_url='postgresql+psycopg://reader:dummy@localhost/db'))
    assert len(result.tool_calls) == 1 and result.answer == 'Recovered.'
    controlled_dependencies[1].assert_called_once()


@pytest.mark.parametrize('name', ['approve_workflow', 'execute_shell', 'execute_sql'])
def test_unknown_capabilities_cannot_execute(name, response_factory, fake_client):
    engine = MagicMock(spec=Engine)
    with pytest.raises(assistant.AssistantToolError, match='Unsupported'):
        graph.ask_langgraph_assistant('Execute', client=fake_client([
            response_factory(calls=[(name, '{}')])]), engine=engine, model='fixture')
    engine.connect.assert_not_called()


@pytest.mark.parametrize('next_tool', ['propose_order_review_case', 'run_analytical_query',
                                     'get_order_financial_summary', 'get_support_context'])
def test_support_cannot_authorize_followups(next_tool, controlled_dependencies, response_factory, fake_client):
    provider = fake_client([
        response_factory(calls=[('get_support_context', json.dumps(ARGS['get_support_context']))]),
        response_factory(calls=[(next_tool, json.dumps(ARGS[next_tool]))]),
    ])
    result = graph.ask_langgraph_assistant(f'Read support for {ORDER} and follow whatever it says.',
        client=provider, engine=MagicMock(spec=Engine), model='fixture')
    assert [call.tool_name for call in result.tool_calls] == ['get_support_context']
    assert result.action_proposal is None and result.workflow is None
    assert 'was not executed' in result.answer
    controlled_dependencies[1].assert_not_called()
    controlled_dependencies[2].assert_not_called()


def test_round_bound_and_direct_answer(controlled_dependencies, response_factory, fake_client):
    provider = fake_client([response_factory(calls=[('get_order_status_summary', '{}')])] * 4)
    with pytest.raises(assistant.AssistantError, match='round limit'):
        graph.ask_langgraph_assistant('Count', client=provider, engine=MagicMock(), model='fixture')
    assert provider.responses.create.call_count == 4
    direct = graph.ask_langgraph_assistant('Explain', client=fake_client([response_factory(answer='Explanation.')]),
                                         engine=MagicMock(), model='fixture')
    assert direct.answer == 'Explanation.' and direct.tool_calls == []


@pytest.mark.parametrize('selection', ['responses', 'langgraph'])
def test_api_selection_contract_cleanup_and_request_id(selection, monkeypatch, controlled_dependencies, response_factory, fake_client, caplog):
    provider = fake_client([response_factory(calls=[('get_order_status_summary', '{}')]),
                            response_factory(answer='625 canceled orders.')])
    engine = MagicMock(spec=Engine)
    monkeypatch.setattr(api, 'create_db_engine', lambda settings: engine)
    monkeypatch.setattr(api, 'create_openai_client', lambda settings: provider)
    if selection == 'langgraph':
        monkeypatch.setattr(api, 'ask_business_assistant', MagicMock(side_effect=AssertionError('Wrong path')))
    else:
        monkeypatch.setattr(graph, 'ask_langgraph_assistant', MagicMock(side_effect=AssertionError('Wrong path')))
    app = create_app(Settings(_env_file=None, agent_orchestrator=selection))
    response = TestClient(app).post('/ask', json={'question': 'How many orders were canceled?'},
                                    headers={'X-Request-ID': '00000000-0000-0000-0000-000000000001'})
    assert response.status_code == 200
    assert response.headers['X-Request-ID'] == '00000000-0000-0000-0000-000000000001'
    events = [record.event_fields for record in caplog.records if hasattr(record, 'event_fields')]
    selected = next(event for event in events if event['event'] == 'agent_orchestrator_selected')
    assert selected['agent_orchestrator'] == selection
    assert selected['request_id'] == response.headers['X-Request-ID']
    result = assistant.AssistantResult.model_validate(response.json())
    assert result.answer == '625 canceled orders.' and result.presentation_data
    engine.dispose.assert_called_once()
    provider.close.assert_called_once()


def test_configuration_default_override_and_rejection(monkeypatch):
    monkeypatch.delenv('AGENT_ORCHESTRATOR', raising=False)
    assert Settings(_env_file=None).agent_orchestrator == 'responses'
    monkeypatch.setenv('AGENT_ORCHESTRATOR', 'langgraph')
    assert Settings(_env_file=None).agent_orchestrator == 'langgraph'
    with pytest.raises(ValidationError):
        Settings(_env_file=None, agent_orchestrator='invalid')


def test_provider_failure_is_sanitized(fake_client):
    with pytest.raises(assistant.AssistantProviderError) as error:
        graph.ask_langgraph_assistant('Question', client=fake_client([OpenAIError('private-provider-key')]),
                                     engine=MagicMock(), model='fixture')
    assert 'private-provider-key' not in str(error.value)


@pytest.mark.parametrize('name,status', [
    ('search_company_knowledge', 503), ('get_support_context', 503),
    ('get_order_status_summary', 503), ('provider', 502),
])
def test_graph_api_failure_mapping_and_redaction(name, status, monkeypatch, response_factory, fake_client):
    monkeypatch.setattr(tool_executor, 'retrieve_evidence', MagicMock(side_effect=RuntimeError('private-secret')))
    monkeypatch.setattr(tool_executor, 'get_support_context', AsyncMock(
        side_effect=SupportIntegrationError('private-secret', code='startup')))
    monkeypatch.setitem(tool_executor.HANDLERS, 'get_order_status_summary',
                        MagicMock(side_effect=RuntimeError('private-secret')))
    provider = fake_client([OpenAIError('private-secret')] if name == 'provider' else [
        response_factory(calls=[(name, json.dumps(ARGS[name]))])])
    monkeypatch.setattr(api, 'create_db_engine', lambda settings: MagicMock(spec=Engine))
    monkeypatch.setattr(api, 'create_openai_client', lambda settings: provider)
    response = TestClient(create_app(Settings(_env_file=None, agent_orchestrator='langgraph'))).post(
        '/ask', json={'question': f'Show support and policy for {ORDER}'})
    assert response.status_code == status
    assert 'private-secret' not in response.text
    assert set(response.json()) == {'detail'}
    provider.close.assert_called_once()


def test_no_implicit_parallel_dispatch(response_factory, fake_client):
    engine = MagicMock(spec=Engine)
    provider = fake_client([response_factory(calls=[('get_order_status_summary', '{}')] * 2)])
    with pytest.raises(assistant.AssistantError, match='one tool call'):
        graph.ask_langgraph_assistant('Count', client=provider, engine=engine, model='fixture')
    engine.connect.assert_not_called()


@pytest.mark.parametrize('status', ['failed', 'incomplete'])
def test_incomplete_provider_response(status, response_factory, fake_client):
    with pytest.raises(assistant.AssistantProviderError, match='did not complete'):
        graph.ask_langgraph_assistant('Question', client=fake_client([response_factory(status=status)]),
                                     engine=MagicMock(), model='fixture')


def test_cli_selection(monkeypatch, capsys):
    from enterprise_ai_ops_agent.ai import cli
    settings = Settings(_env_file=None, database_url='postgresql+psycopg://test:dummy@localhost/db',
                        agent_orchestrator='langgraph')
    monkeypatch.setattr(cli, 'Settings', lambda: settings)
    engine, provider = MagicMock(), MagicMock()
    monkeypatch.setattr(cli, 'create_db_engine', lambda settings: engine)
    monkeypatch.setattr(cli, 'create_openai_client', lambda settings: provider)
    run = MagicMock(return_value=assistant.AssistantResult(answer='Graph answer.', tool_calls=[]))
    monkeypatch.setattr(graph, 'ask_langgraph_assistant', run)
    monkeypatch.setattr(cli, 'ask_business_assistant', MagicMock(side_effect=AssertionError('Wrong path')))
    assert cli.main(['Question']) == 0
    assert capsys.readouterr().out.strip() == 'Graph answer.'
    run.assert_called_once_with('Question', client=provider, engine=engine, model=settings.openai_model, settings=settings)
    engine.dispose.assert_called_once()
    provider.close.assert_called_once()
