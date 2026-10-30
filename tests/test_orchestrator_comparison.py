"""Shared scripted scenarios: real orchestration/dispatch, fake external dependencies."""

import json
from pathlib import Path
from time import perf_counter
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine

from enterprise_ai_ops_agent.ai import assistant, langgraph_assistant, tool_executor
from enterprise_ai_ops_agent.api import assistant as api
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from test_langgraph_assistant import ARGS, controlled_dependencies  # Reuse existing fixture.


ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = json.loads((ROOT / 'evals/orchestrator_scenarios.json').read_text(encoding='utf-8'))
ORCHESTRATORS = {
    'responses': assistant.ask_business_assistant,
    'langgraph': langgraph_assistant.ask_langgraph_assistant,
}


@pytest.mark.parametrize('orchestrator', ORCHESTRATORS)
@pytest.mark.parametrize('scenario', SCENARIOS, ids=lambda case: case['id'])
def test_scenario(orchestrator, scenario, controlled_dependencies, response_factory, fake_client, monkeypatch):
    """Both implementations receive identical scripted model outputs and tool fixtures."""
    expected = scenario['tools']
    provider = fake_client([
        *[response_factory(calls=[(name, json.dumps(ARGS[name]))]) for name in expected],
        response_factory(answer='Scripted completion; not a live-model answer.'),
    ])
    engine = MagicMock(spec=Engine)
    settings = Settings(_env_file=None, agent_orchestrator=orchestrator,
                        analytics_database_url='postgresql+psycopg://reader:dummy@localhost/db')
    dispatch = MagicMock(wraps=tool_executor.execute_tool)
    module = assistant if orchestrator == 'responses' else langgraph_assistant
    monkeypatch.setattr(module, 'execute_tool', dispatch)
    start = perf_counter()
    result = ORCHESTRATORS[orchestrator](scenario['question'], client=provider, engine=engine,
                                       model='offline-fixture', settings=settings)
    elapsed_ms = (perf_counter() - start) * 1000
    # Validate the public wire projection, not internal provider objects/state.
    validated = assistant.AssistantResult.model_validate_json(result.model_dump_json())
    actual = [call.args[1] for call in dispatch.call_args_list]
    proposal = validated.action_proposal
    checks = {
        'contract_valid': True,
        'tool_match': actual == expected,
        'trace_match': [call.tool_name for call in validated.tool_calls] == [
            name for name in expected if name != 'propose_order_review_case'],
        'multi_tool_complete': len(expected) < 2 or actual == expected,
        'proposal_only': (proposal is not None and proposal.requires_approval
                          and 'No case has been created' in validated.answer)
                         if 'propose_order_review_case' in expected else proposal is None,
        'sources_separated': (
            bool(validated.knowledge_sources) == ('search_company_knowledge' in expected)
            and bool(validated.external_sources) == ('get_support_context' in expected)),
        'no_workflow_execution': validated.workflow is None,
        'provider_rounds_match': provider.responses.create.call_count == len(expected) + 1,
        'no_business_write': controlled_dependencies[2].call_count == 0,
    }
    if 'run_analytical_query' in expected:
        controlled_dependencies[1].assert_called_once()
        assert controlled_dependencies[1].call_args.args[0] is settings
        engine.connect.assert_not_called()
    if 'get_support_context' in expected:
        controlled_dependencies[0].assert_awaited_once()
    else:
        controlled_dependencies[0].assert_not_awaited()
    if 'get_order_financial_summary' in expected:
        assert '375.73' in validated.answer and '0.00' in validated.answer
    record = {
        'scenario': scenario['id'], 'orchestrator': orchestrator, 'success': all(checks.values()),
        'tool_calls': dispatch.call_count, 'elapsed_ms': round(elapsed_ms, 3),
        'timing_scope': 'offline_orchestration_with_fake_dependencies', **checks,
    }
    print('ORCHESTRATION_RESULT ' + json.dumps(record, sort_keys=True))
    assert all(checks.values()), record


@pytest.mark.parametrize('orchestrator', ORCHESTRATORS)
def test_application_startup_and_ask_contract(orchestrator, monkeypatch, controlled_dependencies, response_factory, fake_client):
    """Real FastAPI lifespan/router; fake provider and database, no network required."""
    monkeypatch.setenv('AGENT_ORCHESTRATOR', orchestrator)
    app = create_app(Settings(_env_file=None))
    assert app.state.settings.agent_orchestrator == orchestrator
    provider = fake_client([
        response_factory(calls=[('get_order_status_summary', '{}')]),
        response_factory(answer='625 canceled orders.'),
    ])
    monkeypatch.setattr(api, 'create_db_engine', lambda settings: MagicMock(spec=Engine))
    monkeypatch.setattr(api, 'create_openai_client', lambda settings: provider)
    with TestClient(app) as client:
        assert client.get('/health').json() == {'status': 'ok'}
        response = client.post('/ask', json={'question': SCENARIOS[0]['question']})
        assert response.status_code == 200
        result = assistant.AssistantResult.model_validate(response.json())
        assert result.answer == '625 canceled orders.'
        assert result.presentation_data
        assert result.tool_calls[0].tool_name == 'get_order_status_summary'
        assert 'X-Request-ID' in response.headers
        schema = client.get('/openapi.json').json()
        assert schema['paths']['/ask']['post']['responses']['200']['content']['application/json']['schema'] == {
            '$ref': '#/components/schemas/AssistantResult'}
