from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError
from sqlalchemy.exc import OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.api import assistant as api
from enterprise_ai_ops_agent.ai.assistant import AssistantResult, AssistantError, AssistantProviderError, AssistantToolError
from enterprise_ai_ops_agent.ai.tool_executor import ToolCallTrace


def empty_settings():
    return Settings(_env_file=None, database_url=None, openai_api_key=None)


def client_with_boundary(boundary):
    app = create_app(empty_settings())
    app.dependency_overrides[api.get_assistant] = lambda: boundary
    return TestClient(app)


def test_health_and_startup_do_not_construct_resources(monkeypatch):
    engine = MagicMock(side_effect=AssertionError('must not construct'))
    provider = MagicMock(side_effect=AssertionError('must not construct'))
    monkeypatch.setattr(api,'create_db_engine',engine)
    monkeypatch.setattr(api,'create_openai_client',provider)
    with TestClient(create_app(empty_settings())) as client:
        assert client.get('/health').json()=={'status':'ok'}
        assert client.get('/health').status_code==200
    engine.assert_not_called(); provider.assert_not_called()


def test_typed_response_and_question_reused():
    boundary = MagicMock(return_value=AssistantResult(
        answer='625 canceled orders.',
        tool_calls=[ToolCallTrace(tool_name='get_order_status_summary',arguments={})],
    ))
    response=client_with_boundary(boundary).post('/ask',json={'question':'How many orders were canceled?'})
    assert response.status_code==200
    assert response.json()=={'answer':'625 canceled orders.','tool_calls':[{'tool_name':'get_order_status_summary','arguments':{}}], 'action_proposal':None, 'workflow':None, 'knowledge_sources':[], 'external_sources':[]}
    boundary.assert_called_once_with('How many orders were canceled?')


def test_direct_answer_and_response_filtering():
    boundary=MagicMock(return_value={'answer':'Installments are not a multiplier.','tool_calls':[],
                                    'provider_object':'api_secret','hidden_reasoning':'database_password'})
    response=client_with_boundary(boundary).post('/ask',json={'question':'Explain installments.'})
    assert response.status_code==200
    assert response.json()=={'answer':'Installments are not a multiplier.','tool_calls':[], 'action_proposal':None, 'workflow':None, 'knowledge_sources':[], 'external_sources':[]}
    assert 'api_secret' not in response.text and 'database_password' not in response.text


@pytest.mark.parametrize('payload',[
    {'question':''}, {'question':'   '}, {'question':'x'*(api.MAX_QUESTION_LENGTH+1)},
    {'question':123}, {'question':None}, {}, [], 'wrong body',
    {'question':'valid','extra':'unexpected'},
])
def test_invalid_request_rejected_before_execution(payload):
    boundary=MagicMock()
    response=client_with_boundary(boundary).post('/ask',json=payload)
    assert response.status_code==422
    boundary.assert_not_called()


def test_malformed_json():
    boundary=MagicMock()
    response=client_with_boundary(boundary).post('/ask',content='{bad',headers={'Content-Type':'application/json'})
    assert response.status_code==422
    boundary.assert_not_called()


@pytest.mark.parametrize('missing',['database','key'])
def test_missing_configuration_safe(missing):
    settings=Settings(_env_file=None,
                      database_url=None if missing=='database' else 'postgresql+psycopg://user:database_password@localhost/example',
                      openai_api_key=None if missing=='key' else 'api_secret')
    response=TestClient(create_app(settings)).post('/ask',json={'question':'Count orders.'})
    assert response.status_code==503
    assert response.json()=={'detail':'Assistant service is not configured or available.'}
    assert 'api_secret' not in response.text and 'database_password' not in response.text
    assert 'postgresql' not in response.text


@pytest.mark.parametrize('exception,status',[
    (AssistantProviderError('api_secret'),502),
    (OpenAIError('api_secret'),502),
    (AssistantError('api_secret'),502),
    (AssistantToolError('postgresql://user:database_password@host/db'),503),
    (OperationalError(None,None,Exception('database_password')),503),
    (RuntimeError('api_secret database_password'),500),
])
def test_failure_mapping_never_exposes_details(exception,status):
    boundary=MagicMock(side_effect=exception)
    response=client_with_boundary(boundary).post('/ask',json={'question':'Count orders.'})
    assert response.status_code==status
    assert set(response.json())=={'detail'}
    assert 'api_secret' not in response.text and 'database_password' not in response.text
    assert 'postgresql' not in response.text
    assert 'Traceback' not in response.text


def test_real_assistant_reused_with_fake_provider(monkeypatch,response_factory,fake_client):
    engine=MagicMock()
    provider=fake_client([response_factory(answer='A direct semantic explanation.')])
    monkeypatch.setattr(api,'create_db_engine',lambda settings:engine)
    monkeypatch.setattr(api,'create_openai_client',lambda settings:provider)
    original=api.ask_business_assistant
    spy=MagicMock(wraps=original)
    monkeypatch.setattr(api,'ask_business_assistant',spy)
    response=TestClient(create_app(empty_settings())).post('/ask',json={'question':'Explain the tools.'})
    assert response.status_code==200
    assert response.json()=={'answer':'A direct semantic explanation.','tool_calls':[], 'action_proposal':None, 'workflow':None, 'knowledge_sources':[], 'external_sources':[]}
    spy.assert_called_once()
    provider.responses.create.assert_called_once()
    engine.dispose.assert_called_once()
    provider.close.assert_called_once()


def test_provider_failure_through_real_assistant(monkeypatch,fake_client):
    engine=MagicMock(); provider=fake_client([OpenAIError('api_secret')])
    monkeypatch.setattr(api,'create_db_engine',lambda settings:engine)
    monkeypatch.setattr(api,'create_openai_client',lambda settings:provider)
    response=TestClient(create_app(empty_settings())).post('/ask',json={'question':'Count orders.'})
    assert response.status_code==502 and 'api_secret' not in response.text
    engine.dispose.assert_called_once(); provider.close.assert_called_once()


def test_unsupported_tool_guard_still_applies(monkeypatch,response_factory,fake_client):
    engine=MagicMock(); provider=fake_client([response_factory(calls=[('execute_sql','{"sql":"DROP TABLE orders"}')])])
    monkeypatch.setattr(api,'create_db_engine',lambda settings:engine)
    monkeypatch.setattr(api,'create_openai_client',lambda settings:provider)
    response=TestClient(create_app(empty_settings())).post('/ask',json={'question':'Run SQL.'})
    assert response.status_code==503
    engine.connect.assert_not_called()
    assert 'DROP' not in response.text


def test_openapi_contract():
    schema=create_app(empty_settings()).openapi()
    assert '/health' in schema['paths']
    operation=schema['paths']['/ask']['post']
    assert operation['requestBody']['content']['application/json']['schema']['$ref'].endswith('/AskRequest')
    assert operation['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/AssistantResult')
    models=schema['components']['schemas']
    assert models['AskRequest']['additionalProperties'] is False
    assert models['AskRequest']['properties']['question']['maxLength']==api.MAX_QUESTION_LENGTH
    assert set(models['AssistantResult']['properties'])=={'answer','tool_calls','action_proposal','workflow','knowledge_sources','external_sources','presentation_data'}
    assert 'presentation_data' not in models['AssistantResult']['required']
    assert set(models['ToolCallTrace']['properties'])=={'tool_name','arguments'}
    for name in ('OPENAI_API_KEY','DATABASE_URL','openai_api_key','database_url'):
        assert name not in str(schema)
