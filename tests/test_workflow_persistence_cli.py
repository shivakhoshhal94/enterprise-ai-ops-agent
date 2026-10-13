"""Explicit persistence setup and developer boundary tests; no PostgreSQL required."""

import importlib
from contextlib import contextmanager
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from psycopg import OperationalError

from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.workflows import cli, persistence, service
from enterprise_ai_ops_agent.workflows.contracts import ApprovalDecision, OrderReviewWorkflowState, WorkflowError
from test_operations import ORDER


def output(status='awaiting_approval'):
    return OrderReviewWorkflowState(workflow_id=str(uuid4()),
        proposal=OrderReviewCaseProposal(proposal_id=uuid4(),order_id=ORDER,reason='Synthetic review',priority='high'),status=status)


def mock_resources(monkeypatch):
    engine=MagicMock(); engine.url.username='application_admin'
    engine.url.set.return_value.render_as_string.return_value='private_connection_secret'
    connection=MagicMock()
    factory=MagicMock(); factory.connect.return_value.__enter__.return_value=connection
    saver=MagicMock()
    monkeypatch.setattr(persistence,'create_db_engine',lambda _:engine)
    monkeypatch.setattr(persistence,'Connection',factory)
    monkeypatch.setattr(persistence,'PostgresSaver',lambda _:saver)
    return engine,connection,factory,saver


def test_runtime_injects_official_saver_without_setup(monkeypatch):
    engine,connection,factory,saver=mock_resources(monkeypatch)
    constructor=MagicMock(return_value='runtime')
    monkeypatch.setattr(persistence,'OrderReviewWorkflow',constructor)
    with persistence.postgres_workflow(Settings(_env_file=None)) as value: assert value=='runtime'
    assert constructor.call_args.kwargs['checkpointer'] is saver
    assert constructor.call_args.kwargs['engine'] is engine
    saver.setup.assert_not_called()
    assert factory.connect.call_args.kwargs['autocommit'] is True
    assert factory.connect.call_args.kwargs['prepare_threshold']==0
    assert factory.connect.call_args.kwargs['options']=='-csearch_path=workflow_checkpoints -cstatement_timeout=5000 -clock_timeout=3000'
    engine.dispose.assert_called_once()


def test_setup_is_explicit_and_framework_owned(monkeypatch):
    _,connection,_,saver=mock_resources(monkeypatch)
    cursor=connection.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect=[{'present':True},{'allowed':False}]
    persistence.initialize_checkpointer(Settings(_env_file=None))
    saver.setup.assert_called_once()
    statements=[call.args[0] for call in cursor.execute.call_args_list]
    assert any('CREATE SCHEMA IF NOT EXISTS workflow_checkpoints' in sql for sql in statements)
    assert not any('CREATE TABLE' in sql for sql in statements)  # saver owns its DDL


def test_reader_login_cannot_construct_workflow_runtime(monkeypatch):
    engine,_,factory,_=mock_resources(monkeypatch)
    engine.url.username='enterprise_ai_ops_reader'
    with pytest.raises(WorkflowError,match='not the analytics reader'):
        with persistence.postgres_workflow(Settings(_env_file=None)): pass
    factory.connect.assert_not_called()


def test_connection_failure_hides_secret(monkeypatch):
    _,_,factory,_=mock_resources(monkeypatch)
    factory.connect.side_effect=OperationalError('private_connection_secret password')
    with pytest.raises(WorkflowError) as caught:
        with persistence.postgres_workflow(Settings(_env_file=None)): pass
    assert 'private_connection_secret' not in str(caught.value)
    assert caught.value.__suppress_context__


def test_import_and_graph_construction_have_no_database_side_effects(monkeypatch):
    saver=MagicMock()
    monkeypatch.setattr(persistence,'PostgresSaver',saver)
    engine=MagicMock()
    from langgraph.checkpoint.memory import InMemorySaver
    service.OrderReviewWorkflow(engine=engine,checkpointer=InMemorySaver())
    engine.connect.assert_not_called()
    engine.begin.assert_not_called()
    factory=MagicMock()
    monkeypatch.setattr(persistence,'create_db_engine',factory)
    importlib.reload(cli)
    factory.assert_not_called()
    saver.setup.assert_not_called()


@pytest.mark.parametrize('command',['approve','reject','status','retry'])
def test_cli_dispatch(command,monkeypatch,capsys):
    runtime=MagicMock()
    result=output('rejected' if command=='reject' else 'awaiting_approval')
    runtime.resume.return_value=result; runtime.get_state.return_value=result; runtime.retry.return_value=result
    @contextmanager
    def open_runtime(settings): yield runtime
    monkeypatch.setattr(cli,'postgres_workflow',open_runtime)
    args=[command,'--workflow-id',result.workflow_id]
    if command in ('approve','reject'): args+=['--actor','developer_approver']
    assert cli.main(args)==0
    assert result.workflow_id in capsys.readouterr().out
    if command in ('approve','reject'):
        assert runtime.resume.call_args.args[1]==ApprovalDecision(decision=command,actor='developer_approver')
    elif command=='status': runtime.get_state.assert_called_once_with(result.workflow_id)
    else: runtime.retry.assert_called_once_with(result.workflow_id)


def test_cli_setup(monkeypatch,capsys):
    setup=MagicMock(); monkeypatch.setattr(cli,'initialize_checkpointer',setup)
    assert cli.main(['setup'])==0
    setup.assert_called_once()
    assert 'initialized' in capsys.readouterr().out


def test_cli_start_reuses_proposal_path(monkeypatch,capsys):
    result=output(); runtime=MagicMock(); runtime.start.return_value=result
    @contextmanager
    def open_runtime(settings): yield runtime
    monkeypatch.setattr(cli,'postgres_workflow',open_runtime)
    engine=MagicMock(); monkeypatch.setattr(cli,'create_db_engine',lambda _:engine)
    proposer=MagicMock(return_value=result.proposal); monkeypatch.setattr(cli,'propose_order_review_case',proposer)
    assert cli.main(['start','--order-id',ORDER,'--reason','Synthetic review','--priority','high'])==0
    assert proposer.call_args.args[1].order_id==ORDER
    runtime.start.assert_called_once_with(result.proposal)
    assert 'awaiting_approval' in capsys.readouterr().out


def test_cli_invalid_input_no_runtime(monkeypatch,capsys):
    runtime=MagicMock(); monkeypatch.setattr(cli,'postgres_workflow',runtime)
    assert cli.main(['approve','--workflow-id',str(uuid4()),'--actor',' '])==1
    assert 'input error' in capsys.readouterr().err
    runtime.assert_not_called()


def test_cli_configuration_error_safe(monkeypatch,capsys):
    monkeypatch.setattr(cli,'Settings',MagicMock(side_effect=ValueError('private_connection_secret')))
    assert cli.main(['setup'])==1
    assert 'private_connection_secret' not in capsys.readouterr().err


def test_cli_failed_workflow_nonzero(monkeypatch,capsys):
    runtime=MagicMock(); runtime.retry.return_value=output('failed')
    @contextmanager
    def open_runtime(settings): yield runtime
    monkeypatch.setattr(cli,'postgres_workflow',open_runtime)
    assert cli.main(['retry','--workflow-id',str(uuid4())])==1
    assert 'failed' in capsys.readouterr().out
