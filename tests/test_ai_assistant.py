import json
from decimal import Decimal
from unittest.mock import MagicMock

import pytest
from openai import OpenAIError
from pydantic import SecretStr
from sqlalchemy import Engine, create_engine
from sqlalchemy.exc import OperationalError
from fastapi.testclient import TestClient

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.ai import assistant, cli, client as client_module, tool_executor
from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
from enterprise_ai_ops_agent.business.orders import OrderStatusCount
from enterprise_ai_ops_agent.business.customers import CustomerOrderHistory
from enterprise_ai_ops_agent.business.finance import OrderFinancialSummary


@pytest.fixture
def engine():
    return MagicMock(spec=Engine)


def financial_result():
    return OrderFinancialSummary(
        order_id='order1', item_count=2, seller_count=1, items_price_total=Decimal('0.30'),
        freight_total=Decimal('0.00'), items_and_freight_total=Decimal('0.30'),
        payment_row_count=1, payment_total=Decimal('0.30'), difference=Decimal('0.00'),
    )


@pytest.mark.parametrize('name,arguments',[
    ('get_order_status_summary',{}),
    ('get_customer_order_history',{'customer_unique_id':'unique1'}),
    ('get_order_financial_summary',{'order_id':'order1'}),
])
def test_loop_dispatch_result_and_trace(monkeypatch,response_factory,fake_client,engine,name,arguments):
    value = [OrderStatusCount(order_status='canceled',order_count=625)] if not arguments else (
        CustomerOrderHistory(customer_unique_id='unique1',found=True,customer_record_count=2,order_count=0,orders=[])
        if 'customer_unique_id' in arguments else financial_result()
    )
    handler=MagicMock(return_value=value)
    monkeypatch.setitem(tool_executor.HANDLERS,name,handler)
    client=fake_client([
        response_factory(calls=[(name,json.dumps(arguments))],extra_output=[{'id':'rs1','type':'reasoning','summary':[]}]),
        response_factory(answer='Final factual answer.'),
    ])
    result=assistant.ask_business_assistant('Business question',client=client,engine=engine,model='test-model')
    assert result.answer=='Final factual answer.'
    assert result.tool_calls[0].tool_name==name
    assert result.tool_calls[0].arguments==arguments
    handler.assert_called_once_with(engine.connect.return_value.execution_options.return_value.__enter__.return_value,**arguments)
    engine.connect.return_value.execution_options.assert_called_once_with(postgresql_readonly=True)
    second=client.responses.create.call_args_list[1].kwargs
    assert second['input'][-1]['type']=='function_call_output'
    assert second['input'][-1]['call_id']=='call_0'
    expected=[v.model_dump(mode='json') for v in value] if isinstance(value,list) else value.model_dump(mode='json')
    assert json.loads(second['input'][-1]['output'])==expected
    assert any(getattr(item,'type',None)=='reasoning' for item in second['input'])
    assert second['tools']==TOOL_SCHEMAS
    assert second['store'] is False and second['parallel_tool_calls'] is False
    assert 'instructions' in second
    assert 'rs1' not in result.model_dump_json()


@pytest.mark.parametrize('name,args',[
    ('execute_sql','{"sql":"DROP TABLE orders"}'),
    ('__import__','{}'),
    ('get_customer_order_history','not json'),
    ('get_customer_order_history','{}'),
    ('get_customer_order_history','{"customer_id":"secret_value"}'),
    ('get_customer_order_history','{"customer_unique_id":123}'),
    ('get_customer_order_history','{"customer_unique_id":"  "}'),
    ('get_customer_order_history','{"customer_unique_id":null}'),
    ('get_order_financial_summary','{"order_id":"o","sql":"secret_value"}'),
    ('get_order_financial_summary','{}'),
    ('get_order_status_summary','{"extra":"secret_value"}'),
    ('get_order_status_summary','[]'),
])
def test_invalid_dispatch_never_connects(engine,name,args):
    with pytest.raises(tool_executor.ToolExecutionError) as caught:
        tool_executor.execute_tool(engine,name,args)
    assert 'secret_value' not in str(caught.value)
    engine.connect.assert_not_called()


def test_unknown_tool_is_safe_through_loop(response_factory,fake_client,engine):
    client=fake_client([response_factory(calls=[('eval','{}')])])
    with pytest.raises(assistant.AssistantError,match='Unsupported'):
        assistant.ask_business_assistant('Run arbitrary code',client=client,engine=engine,model='test')
    engine.connect.assert_not_called()
    assert client.responses.create.call_count==1


@pytest.mark.parametrize('failure',[
    OperationalError(None,None,Exception('database_password')),
    RuntimeError('database_password'),
])
def test_tool_exception_redacted(monkeypatch,engine,failure):
    monkeypatch.setitem(tool_executor.HANDLERS,'get_order_status_summary',MagicMock(side_effect=failure))
    with pytest.raises(tool_executor.ToolExecutionError) as caught:
        tool_executor.execute_tool(engine,'get_order_status_summary','{}')
    assert 'database_password' not in str(caught.value)


def test_database_connect_error_redacted(engine):
    engine.connect.side_effect=OperationalError(None,None,Exception('database_password'))
    with pytest.raises(tool_executor.ToolExecutionError) as caught:
        tool_executor.execute_tool(engine,'get_order_status_summary','{}')
    assert 'database_password' not in str(caught.value)


def test_provider_exception_redacted(fake_client,engine):
    client=fake_client([OpenAIError('api_secret')])
    with pytest.raises(assistant.AssistantError) as caught:
        assistant.ask_business_assistant('question',client=client,engine=engine,model='test')
    assert 'api_secret' not in str(caught.value)
    engine.connect.assert_not_called()


def test_loop_limit(monkeypatch,response_factory,fake_client,engine):
    handler=MagicMock(return_value=[])
    monkeypatch.setitem(tool_executor.HANDLERS,'get_order_status_summary',handler)
    client=fake_client([response_factory(calls=[('get_order_status_summary','{}')]) for _ in range(4)])
    with pytest.raises(assistant.AssistantError,match='limit'):
        assistant.ask_business_assistant('question',client=client,engine=engine,model='test')
    assert handler.call_count==3
    assert client.responses.create.call_count==4


def test_multiple_calls_rejected(response_factory,fake_client,engine):
    client=fake_client([response_factory(calls=[('get_order_status_summary','{}')]*2)])
    with pytest.raises(assistant.AssistantError,match='one tool call'):
        assistant.ask_business_assistant('question',client=client,engine=engine,model='test')
    engine.connect.assert_not_called()


@pytest.mark.parametrize('status',['failed','incomplete'])
def test_incomplete_response(response_factory,fake_client,engine,status):
    with pytest.raises(assistant.AssistantError,match='did not complete'):
        assistant.ask_business_assistant('question',client=fake_client([response_factory(status=status)]),engine=engine,model='test')


def test_no_final_text(response_factory,fake_client,engine):
    with pytest.raises(assistant.AssistantError,match='no final answer'):
        assistant.ask_business_assistant('question',client=fake_client([response_factory()]),engine=engine,model='test')


def test_direct_answer_is_deliberately_supported(response_factory,fake_client,engine):
    result=assistant.ask_business_assistant('Explain the supported tools',client=fake_client([response_factory(answer='I support three queries.')]),engine=engine,model='test')
    assert result.answer=='I support three queries.' and result.tool_calls==[]
    engine.connect.assert_not_called()


def test_money_null_and_zero_serialization(monkeypatch,engine):
    value=financial_result()
    value.payment_total=None; value.difference=None
    handler=MagicMock(return_value=value)
    monkeypatch.setitem(tool_executor.HANDLERS,'get_order_financial_summary',handler)
    output=tool_executor.execute_tool(engine,'get_order_financial_summary','{"order_id":"order1"}').output_json
    payload=json.loads(output)
    assert payload['items_price_total']=='0.30'
    assert payload['freight_total']=='0.00'
    assert payload['payment_total'] is None
    handler.return_value=None
    assert tool_executor.execute_tool(engine,'get_order_financial_summary','{"order_id":"unknown"}').output_json=='null'


def test_schemas_expose_only_seven_strict_functions():
    assert {t['name'] for t in TOOL_SCHEMAS}==set(tool_executor.HANDLERS) | {'run_analytical_query', 'propose_order_review_case', 'search_company_knowledge', 'get_support_context'}
    assert len(TOOL_SCHEMAS)==7
    for tool in TOOL_SCHEMAS:
        assert tool['strict'] is True and tool['type']=='function'
        assert tool['parameters']['additionalProperties'] is False
        assert set(tool['parameters']['required'])==set(tool['parameters']['properties'])


def test_secret_settings_and_client_factory(monkeypatch):
    settings=Settings(_env_file=None,openai_api_key='api_secret',openai_model='test-model')
    assert 'api_secret' not in repr(settings)+settings.model_dump_json()
    factory=MagicMock()
    monkeypatch.setattr(client_module,'OpenAI',factory)
    assert client_module.create_openai_client(settings) is factory.return_value
    factory.assert_called_once_with(api_key='api_secret',timeout=30.0,max_retries=0)


def test_http_health_needs_no_key():
    settings=Settings(_env_file=None,openai_api_key=None)
    assert TestClient(create_app(settings)).get('/health').json()=={'status':'ok'}


@pytest.mark.parametrize('case',['missing_db','missing_key','invalid','api','success'])
def test_cli_safety_and_cleanup(monkeypatch,capsys,case):
    settings=Settings(_env_file=None,database_url=None if case=='missing_db' else 'postgresql+psycopg://localhost/test',openai_api_key=None if case=='missing_key' else 'api_secret')
    monkeypatch.setattr(cli,'Settings',lambda:settings)
    engine=MagicMock(); client=MagicMock()
    monkeypatch.setattr(cli,'create_db_engine',lambda s:engine)
    factory=MagicMock(return_value=client)
    if case in {'missing_key','invalid'}: factory.side_effect=ValueError('api_secret')
    monkeypatch.setattr(cli,'create_openai_client',factory)
    operation=MagicMock(return_value=assistant.AssistantResult(answer='Final answer',tool_calls=[]))
    if case=='api': operation.side_effect=assistant.AssistantError('OpenAI request failed.')
    monkeypatch.setattr(cli,'ask_business_assistant',operation)
    assert cli.main(['Question','--trace'])==(0 if case=='success' else 1)
    output=capsys.readouterr()
    assert 'api_secret' not in output.out+output.err
    if case!='missing_db': engine.dispose.assert_called_once()
    if case in {'api','success'}: client.close.assert_called_once()
    else: operation.assert_not_called()


def test_ai_environment_overrides_and_key_optional(monkeypatch):
    monkeypatch.setenv('OPENAI_API_KEY','api_secret')
    monkeypatch.setenv('OPENAI_MODEL','configured-model')
    settings=Settings(_env_file=None)
    assert settings.openai_model=='configured-model'
    assert settings.openai_api_key.get_secret_value()=='api_secret'
    assert 'api_secret' not in settings.model_dump_json()
    settings.openai_api_key=None
    with pytest.raises(ValueError,match='OPENAI_API_KEY'):
        client_module.create_openai_client(settings)
