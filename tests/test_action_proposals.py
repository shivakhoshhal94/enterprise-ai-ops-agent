"""Scripted provider tests: proposals represent intent without executing writes."""

import ast
import json
from pathlib import Path
from unittest.mock import MagicMock
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from openai import OpenAIError
from sqlalchemy import event, select

from enterprise_ai_ops_agent.ai import assistant, tool_executor
from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS, ARGUMENT_MODELS
from enterprise_ai_ops_agent.api import assistant as api
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.operations import proposals, service
from enterprise_ai_ops_agent.operations.contracts import CreateOrderReviewCaseRequest, OrderReviewCaseIntent
from enterprise_ai_ops_agent.operations.models import OrderReviewCase, CaseEvent
from test_operations import database, ORDER


def payload(**changes):
    return dict({"order_id":ORDER,"reason":"User concern: payment discrepancy requires review, not a confirmed error.","priority":"high"}, **changes)


def snapshot(engine):
    from enterprise_ai_ops_agent.db.base import Base
    tables = list(Base.metadata.tables.values()) + [OrderReviewCase.__table__, CaseEvent.__table__]
    with engine.connect() as connection:
        return {table.fullname:[tuple(row) for row in connection.execute(select(table))] for table in tables}


def test_strict_schema_and_shared_validation():
    tool=next(t for t in TOOL_SCHEMAS if t['name']=='propose_order_review_case')
    assert tool['strict'] is True
    assert tool['parameters']['additionalProperties'] is False
    assert set(tool['parameters']['properties'])==set(tool['parameters']['required'])=={'order_id','reason','priority'}
    assert tool['parameters']['properties']['priority']['enum']==['low','medium','high']
    assert ARGUMENT_MODELS['propose_order_review_case'] is OrderReviewCaseIntent
    for field in ('order_id','reason','priority'):
        assert OrderReviewCaseIntent.model_json_schema()['properties'][field] == CreateOrderReviewCaseRequest.model_json_schema()['properties'][field]


def test_proposal_dispatch_reads_only_and_preserves_arguments(database, monkeypatch):
    before=snapshot(database)
    writes=[]
    @event.listens_for(database,'before_cursor_execute')
    def observe(conn,cursor,sql,params,ctx,many):
        if sql.lstrip().upper().startswith(('INSERT','UPDATE','DELETE','CREATE','DROP','ALTER')):
            writes.append(sql)
    writer=MagicMock(side_effect=AssertionError('Write service reached'))
    monkeypatch.setattr(service,'create_order_review_case',writer)
    spy=MagicMock(wraps=tool_executor.propose_order_review_case)
    monkeypatch.setattr(tool_executor,'propose_order_review_case',spy)
    result=tool_executor.execute_tool(database,'propose_order_review_case',json.dumps(payload()))
    intent=spy.call_args.args[1]
    assert intent.model_dump()==payload()
    proposal=result.action_proposal
    assert isinstance(proposal.proposal_id,UUID)
    assert proposal.requires_approval is True
    assert proposal.action_type=='create_order_review_case'
    assert json.loads(result.output_json)==proposal.model_dump(mode='json')
    assert snapshot(database)==before
    assert writes==[]
    writer.assert_not_called()


def test_lookup_transaction_readonly_option():
    engine=MagicMock()
    connection=engine.connect.return_value.execution_options.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one_or_none.return_value=ORDER
    result=tool_executor.execute_tool(engine,'propose_order_review_case',json.dumps(payload()))
    assert result.action_proposal.order_id==ORDER
    engine.connect.return_value.execution_options.assert_called_once_with(postgresql_readonly=True)
    connection.execute.assert_called_once()


@pytest.mark.parametrize('changes',[
    {'reason':''},{'reason':'  '},{'reason':'x'*1001},{'priority':'urgent'},
    {'order_id':''},{'order_id':' '},
    *[{field:'model_controlled'} for field in ('case_id','status','created_at','created_by','actor','idempotency_key','proposal_id','requires_approval','unexpected')],
])
def test_invalid_or_model_owned_fields_rejected_before_database(changes):
    engine=MagicMock()
    with pytest.raises(tool_executor.ToolExecutionError,match='Invalid tool arguments'):
        tool_executor.execute_tool(engine,'propose_order_review_case',json.dumps(payload(**changes)))
    engine.connect.assert_not_called()


def test_unknown_order_rejected_safely(database):
    before=snapshot(database)
    with pytest.raises(tool_executor.ToolExecutionError,match='does not exist'):
        tool_executor.execute_tool(database,'propose_order_review_case',json.dumps(payload(order_id='unknown')))
    assert snapshot(database)==before


def test_lookup_error_is_safe():
    engine=MagicMock()
    engine.connect.side_effect=RuntimeError('api_secret database_password')
    with pytest.raises(tool_executor.ToolExecutionError) as caught:
        tool_executor.execute_tool(engine,'propose_order_review_case',json.dumps(payload()))
    assert 'api_secret' not in str(caught.value)
    assert 'database_password' not in str(caught.value)


def test_assistant_proposal_separate_from_read_traces(database,response_factory,fake_client):
    provider=fake_client([
        response_factory(calls=[('propose_order_review_case',json.dumps(payload()))]),
        response_factory(answer='The case has been created; approval was bypassed.'),
    ])
    result=assistant.ask_business_assistant('Create a review case',client=provider,engine=database,model='fake')
    assert result.action_proposal.order_id==ORDER
    assert result.tool_calls==[]
    assert 'No case has been created' in result.answer
    assert 'Explicit human approval' in result.answer
    assert 'approval was bypassed' not in result.answer
    returned=json.loads(provider.responses.create.call_args_list[1].kwargs['input'][-1]['output'])
    assert returned['proposal_id']==str(result.action_proposal.proposal_id)
    assert returned['requires_approval'] is True


def test_second_proposal_refused(database,response_factory,fake_client):
    provider=fake_client([response_factory(calls=[('propose_order_review_case',json.dumps(payload()))])]*2)
    with pytest.raises(assistant.AssistantToolError,match='Only one'):
        assistant.ask_business_assistant('Two proposals',client=provider,engine=database,model='fake')


def test_provider_failure_after_proposal_no_write(database,response_factory,fake_client):
    before=snapshot(database)
    provider=fake_client([response_factory(calls=[('propose_order_review_case',json.dumps(payload()))]),OpenAIError('api_secret')])
    with pytest.raises(assistant.AssistantProviderError) as caught:
        assistant.ask_business_assistant('Create review',client=provider,engine=database,model='fake')
    assert 'api_secret' not in str(caught.value)
    assert snapshot(database)==before


def test_api_serialization_and_openapi(monkeypatch,response_factory,fake_client):
    engine=MagicMock()
    connection=engine.connect.return_value.execution_options.return_value.__enter__.return_value
    connection.execute.return_value.scalar_one_or_none.return_value=ORDER
    provider=fake_client([response_factory(calls=[('propose_order_review_case',json.dumps(payload()))]),response_factory(answer='Created!')])
    monkeypatch.setattr(api,'create_db_engine',lambda _:engine)
    monkeypatch.setattr(api,'create_openai_client',lambda _:provider)
    app=create_app(Settings(_env_file=None))
    # 8B now persists action proposals at the HTTP handoff; offline graph injection.
    from contextlib import nullcontext
    from langgraph.checkpoint.memory import InMemorySaver
    from enterprise_ai_ops_agent.workflows.service import OrderReviewWorkflow
    from enterprise_ai_ops_agent.api.workflows import get_workflow_factory
    runtime=OrderReviewWorkflow(engine=engine,checkpointer=InMemorySaver())
    app.dependency_overrides[get_workflow_factory]=lambda:lambda:nullcontext(runtime)
    client=TestClient(app)
    response=client.post('/ask',json={'question':'Create an internal review case'})
    assert response.status_code==200
    data=response.json()
    assert data['action_proposal']['requires_approval'] is True
    assert data['action_proposal']['priority']=='high'
    assert data['tool_calls']==[]
    assert UUID(data['action_proposal']['proposal_id'])
    schema=app.openapi()
    fields=schema['components']['schemas']['OrderReviewCaseProposal']['properties']
    assert set(fields)=={'proposal_id','action_type','order_id','reason','priority','requires_approval'}
    assert fields['requires_approval']['const'] is True
    assert schema['components']['schemas']['AssistantResult']['properties']['action_proposal']['anyOf'][1]=={'type':'null'}
    assert '/approve' not in schema['paths'] and '/actions/execute' not in schema['paths']
    assert client.get('/health').status_code==200


def test_structural_separation_from_writer():
    paths=[Path('src/enterprise_ai_ops_agent/operations/proposals.py'),
           Path('src/enterprise_ai_ops_agent/operations/contracts.py'),
           *Path('src/enterprise_ai_ops_agent/ai').glob('*.py'),
           *Path('src/enterprise_ai_ops_agent/api').glob('*.py')]
    for path in paths:
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert node.module != 'enterprise_ai_ops_agent.operations.service'
                assert not any(alias.name in ('service','create_order_review_case') for alias in node.names)
            if isinstance(node,ast.Import):
                assert not any(alias.name.endswith('operations.service') for alias in node.names)
            if isinstance(node,ast.Call):
                assert getattr(node.func,'id',None)!='create_order_review_case'
                assert getattr(node.func,'attr',None)!='create_order_review_case'
