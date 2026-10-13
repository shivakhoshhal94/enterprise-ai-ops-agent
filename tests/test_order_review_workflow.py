"""Real graph/interrupt and real action transactions, offline checkpoint storage."""

import json
from datetime import datetime
from unittest.mock import MagicMock
from uuid import UUID, uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from pydantic import ValidationError
from sqlalchemy import DateTime, create_engine, event, insert

from enterprise_ai_ops_agent.db.base import Base
from enterprise_ai_ops_agent.db.models import Customer, Order
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.operations.models import OperationsBase
from enterprise_ai_ops_agent.operations.service import create_order_review_case
from enterprise_ai_ops_agent.workflows import graph, service
from enterprise_ai_ops_agent.workflows.contracts import ApprovalDecision, WorkflowError, InvalidApprovalError
from test_operations import ORDER, UTCDateTime, counts
from test_action_proposals import snapshot


@pytest.fixture
def database(tmp_path):
    # LangGraph runs nodes in worker threads; a file-backed SQLite fixture shares
    # the tiny test database across connections. This is never runtime persistence.
    engine=create_engine(f"sqlite:///{tmp_path / 'action.db'}", connect_args={"check_same_thread":False},
                         execution_options={"schema_translate_map":{"operations":None}})
    engine.dialect.colspecs={**engine.dialect.colspecs,DateTime:UTCDateTime}
    @event.listens_for(engine,'connect')
    def configure(connection,_):
        connection.isolation_level=None
        connection.execute('PRAGMA foreign_keys=ON')
    @event.listens_for(engine,'begin')
    def begin(connection): connection.exec_driver_sql('BEGIN')
    Base.metadata.create_all(engine)
    OperationsBase.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(insert(Customer),dict(customer_id='customer',customer_unique_id='unique',
            customer_zip_code_prefix='01234',customer_city='city',customer_state='SP'))
        connection.execute(insert(Order),dict(order_id=ORDER,customer_id='customer',order_status='delivered',
            order_purchase_timestamp=datetime(2018,1,1),order_estimated_delivery_date=datetime(2018,2,1)))
    yield engine
    engine.dispose()


@pytest.fixture
def proposal():
    return OrderReviewCaseProposal(proposal_id=uuid4(),order_id=ORDER,
        reason='Synthetic internal review; no external action',priority='high')


def workflow(database,saver=None):
    return service.OrderReviewWorkflow(engine=database,checkpointer=saver or InMemorySaver())


def decision(value='approve',actor='developer_approver'):
    return ApprovalDecision(decision=value,actor=actor)


def test_start_interrupt_identity_and_no_side_effect(database,proposal,monkeypatch):
    before=snapshot(database)
    action=MagicMock(side_effect=AssertionError('Action before approval'))
    monkeypatch.setattr(graph,'create_order_review_case',action)
    result=workflow(database).start(proposal)
    assert UUID(result.workflow_id)
    assert service.thread_config(result.workflow_id)['configurable']['thread_id']==result.workflow_id
    assert result.status=='awaiting_approval'
    assert result.proposal==proposal and result.approval is None and result.case is None
    assert result.approval_request['proposal_id']==str(proposal.proposal_id)
    assert result.approval_request['order_id']==ORDER
    assert result.approval_request['reason']==proposal.reason
    assert result.approval_request['priority']=='high'
    assert result.approval_request['action_type']=='create_order_review_case'
    assert 'Approval creates one internal' in result.approval_request['effect']
    assert snapshot(database)==before
    action.assert_not_called()
    assert all(secret not in result.model_dump_json() for secret in ('DATABASE_URL','OPENAI_API_KEY','engine','connection'))


def test_approved_action_atomic_and_reconstruction(database,proposal,monkeypatch):
    saver=InMemorySaver(); first=workflow(database,saver)
    spy=MagicMock(wraps=create_order_review_case)
    monkeypatch.setattr(graph,'create_order_review_case',spy)
    started=first.start(proposal)
    rebuilt=workflow(database,saver)
    assert rebuilt.get_state(started.workflow_id).proposal==proposal
    completed=rebuilt.resume(started.workflow_id,decision())
    assert completed.status=='completed'
    assert completed.approval.actor=='developer_approver'
    assert completed.case.case_id
    assert completed.case.created_by=='approved_by:developer_approver'
    assert completed.case.idempotency_key==graph.execution_idempotency_key(started.workflow_id)
    assert counts(database)==(1,1)
    spy.assert_called_once()
    rebuilt_again=workflow(database,saver)
    assert rebuilt_again.get_state(started.workflow_id).case==completed.case
    assert rebuilt_again.resume(started.workflow_id,decision())==completed
    assert rebuilt_again.retry(started.workflow_id)==completed
    assert counts(database)==(1,1)
    assert spy.call_count==1


def test_rejected_terminal_after_reconstruction(database,proposal,monkeypatch):
    saver=InMemorySaver(); original=workflow(database,saver)
    spy=MagicMock(side_effect=AssertionError('Rejected action executed'))
    monkeypatch.setattr(graph,'create_order_review_case',spy)
    started=original.start(proposal)
    rebuilt=workflow(database,saver)
    result=rebuilt.resume(started.workflow_id,decision('reject'))
    assert result.status=='rejected' and result.case is None
    assert workflow(database,saver).get_state(started.workflow_id).status=='rejected'
    with pytest.raises(WorkflowError,match='not awaiting'):
        rebuilt.resume(started.workflow_id,decision())
    with pytest.raises(WorkflowError,match='already approved'):
        rebuilt.retry(started.workflow_id)
    assert counts(database)==(0,0)
    spy.assert_not_called()


@pytest.mark.parametrize('payload',[
    {'decision':'maybe','actor':'developer_approver'},
    {'decision':'approve','actor':''},{'decision':'approve','actor':'  '},
    {'decision':'approve','actor':'x'*81},
    *[{'decision':'approve','actor':'developer_approver',key:'altered'}
      for key in ('order_id','reason','priority','action_type','idempotency_key','proposal')],
])
def test_decision_rejects_invalid_fields_without_action(database,proposal,payload):
    runtime=workflow(database); started=runtime.start(proposal)
    with pytest.raises(ValidationError): ApprovalDecision.model_validate(payload)
    with pytest.raises(InvalidApprovalError): runtime.resume(started.workflow_id,payload)
    assert runtime.get_state(started.workflow_id).status=='awaiting_approval'
    assert counts(database)==(0,0)


def test_unchecked_decision_revalidated(database,proposal):
    runtime=workflow(database); started=runtime.start(proposal)
    with pytest.raises(InvalidApprovalError):
        runtime.resume(started.workflow_id,decision().model_copy(update={'actor':' '}))
    assert counts(database)==(0,0)


def test_initial_decision_smuggling_cannot_bypass_interrupt(database,proposal):
    runtime=workflow(database)
    with pytest.raises(WorkflowError,match='initial decisions'):
        runtime.start({'proposal':proposal,'approval':decision(),'status':'approved'})
    # Even a raw internal graph input drops/reset decision channels before interrupt.
    compiled=graph.build_order_review_graph(engine=database,checkpointer=InMemorySaver())
    identity=str(uuid4())
    result=compiled.invoke({'workflow_id':identity,'proposal':proposal.model_dump(mode='json'),
                           'approval':decision().model_dump(),'status':'executing'},service.thread_config(identity))
    assert result['status']=='awaiting_approval' and result['approval'] is None
    assert result['__interrupt__']
    assert counts(database)==(0,0)


def test_raw_invalid_resume_does_not_execute(database,proposal):
    compiled=graph.build_order_review_graph(engine=database,checkpointer=InMemorySaver())
    identity=str(uuid4()); config=service.thread_config(identity)
    compiled.invoke({'workflow_id':identity,'proposal':proposal.model_dump(mode='json')},config)
    with pytest.raises(ValidationError): compiled.invoke(Command(resume={'decision':'approve','actor':'','order_id':'changed'}),config)
    assert counts(database)==(0,0)


def test_uuid_collision_cannot_replace_proposal(database,proposal,monkeypatch):
    identity=uuid4()
    monkeypatch.setattr(service,'uuid4',lambda:identity)
    runtime=workflow(database); original=runtime.start(proposal)
    different=proposal.model_copy(update={'reason':'different concern'})
    with pytest.raises(WorkflowError,match='cannot be replaced'): runtime.start(different)
    assert runtime.get_state(original.workflow_id).proposal==proposal


def test_idempotency_key_is_stable_bounded_and_not_supplied_by_model():
    identity=str(uuid4())
    key=graph.execution_idempotency_key(identity)
    assert key==graph.execution_idempotency_key(identity.upper())
    assert key==f'workflow:{identity}:create_order_review_case'
    assert len(key)<=200


def test_action_failure_persisted_safe_and_retry(database,proposal,monkeypatch):
    saver=InMemorySaver(); runtime=workflow(database,saver)
    action=MagicMock(side_effect=RuntimeError('database_password postgresql://secret'))
    monkeypatch.setattr(graph,'create_order_review_case',action)
    started=runtime.start(proposal); failed=runtime.resume(started.workflow_id,decision())
    assert failed.status=='failed' and failed.case is None
    assert 'database_password' not in failed.model_dump_json()
    assert 'postgresql' not in failed.model_dump_json()
    assert workflow(database,saver).get_state(started.workflow_id).status=='failed'
    assert counts(database)==(0,0)
    monkeypatch.setattr(graph,'create_order_review_case',create_order_review_case)
    completed=workflow(database,saver).retry(started.workflow_id)
    assert completed.status=='completed' and counts(database)==(1,1)


def test_ambiguous_post_commit_failure_retry_is_idempotent(database,proposal,monkeypatch):
    keys=[]
    def commit_then_fail(engine,request,*,actor):
        keys.append(request.idempotency_key)
        result=create_order_review_case(engine,request,actor=actor)
        if len(keys)==1: raise RuntimeError('Private failure after business commit')
        return result
    monkeypatch.setattr(graph,'create_order_review_case',commit_then_fail)
    saver=InMemorySaver(); runtime=workflow(database,saver)
    started=runtime.start(proposal)
    assert runtime.resume(started.workflow_id,decision()).status=='failed'
    assert counts(database)==(1,1)
    recovered=workflow(database,saver).retry(started.workflow_id)
    assert recovered.status=='completed' and counts(database)==(1,1)
    assert keys[0]==keys[1]


def test_execution_node_replay_uses_same_case(database,proposal):
    saver=InMemorySaver(); runtime=workflow(database,saver)
    started=runtime.start(proposal); completed=runtime.resume(started.workflow_id,decision())
    # Controlled test-only checkpoint rewind to simulate replay after an action commit.
    config=service.thread_config(started.workflow_id)
    runtime._graph.update_state(config,{'status':'executing'},as_node='mark_executing')
    runtime._graph.invoke(None,config)
    assert runtime.get_state(started.workflow_id).case.case_id==completed.case.case_id
    assert counts(database)==(1,1)


def test_unknown_workflow_and_retry_before_approval_fail(database,proposal):
    runtime=workflow(database)
    with pytest.raises(WorkflowError,match='does not exist'): runtime.get_state(str(uuid4()))
    with pytest.raises(WorkflowError,match='UUID'): runtime.get_state('not-id')
    started=runtime.start(proposal)
    with pytest.raises(WorkflowError,match='already approved'): runtime.retry(started.workflow_id)
    assert counts(database)==(0,0)


def test_external_lock_also_normalizes_thread_identity(database,proposal):
    from contextlib import contextmanager
    observed=[]
    @contextmanager
    def lock(identity):
        observed.append(identity)
        yield
    runtime=service.OrderReviewWorkflow(engine=database,checkpointer=InMemorySaver(),lock=lock)
    started=runtime.start(proposal)
    assert runtime.get_state(started.workflow_id.upper()).workflow_id==started.workflow_id
    assert observed==[started.workflow_id,started.workflow_id]


def test_no_llm_or_workflow_api_dependency():
    import ast
    from pathlib import Path
    for path in Path('src/enterprise_ai_ops_agent/workflows').glob('*.py'):
        tree=ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.ImportFrom):
                assert not node.module.startswith(('openai','enterprise_ai_ops_agent.ai'))
    from fastapi.testclient import TestClient
    from enterprise_ai_ops_agent.main import create_app
    from enterprise_ai_ops_agent.config import Settings
    app=create_app(Settings(_env_file=None))
    assert TestClient(app).get('/health').json()=={'status':'ok'}
    assert set(app.openapi()['paths'])=={
        '/health','/ready','/ask','/workflows/{workflow_id}',
        '/workflows/{workflow_id}/approve','/workflows/{workflow_id}/reject',
    }
