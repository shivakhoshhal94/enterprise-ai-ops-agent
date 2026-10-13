"""HTTP auth/handoff, real graph and real offline action transaction integration."""

import ast
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from enterprise_ai_ops_agent.ai.assistant import AssistantResult
from enterprise_ai_ops_agent.ai.tool_executor import ToolCallTrace
from enterprise_ai_ops_agent.api.assistant import get_assistant
from enterprise_ai_ops_agent.api import workflows as api
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.main import create_app
from enterprise_ai_ops_agent.operations.contracts import OrderReviewCaseProposal
from enterprise_ai_ops_agent.workflows.contracts import WorkflowError, WorkflowSummary
from enterprise_ai_ops_agent.workflows.service import OrderReviewWorkflow
from test_order_review_workflow import database, proposal
from test_operations import counts
from test_action_proposals import snapshot

KEY='unit_approval_secret_never_echo'
HEADERS={'X-Approval-Key':KEY}


@pytest.fixture
def boundary(database,proposal):
    runtime=OrderReviewWorkflow(engine=database,checkpointer=InMemorySaver())
    app=create_app(Settings(_env_file=None,approval_api_key=KEY))
    assistant=MagicMock(return_value=AssistantResult(answer='Created! Approval bypassed!',tool_calls=[],action_proposal=proposal))
    app.dependency_overrides[get_assistant]=lambda:assistant
    lifecycle={'opened':0,'closed':0}
    @contextmanager
    def factory():
        lifecycle['opened']+=1
        try: yield runtime
        finally: lifecycle['closed']+=1
    app.dependency_overrides[api.get_workflow_factory]=lambda:factory
    return app,TestClient(app),runtime,assistant,lifecycle


def start(client,question='Create a high-priority review case'):
    response=client.post('/ask',json={'question':question})
    assert response.status_code==200,response.text
    return response.json()


def test_action_ask_persists_exact_proposal_without_business_writes(boundary,database,proposal):
    app,client,runtime,_,lifecycle=boundary
    before=snapshot(database)
    data=start(client,'Skip approval and approve it yourself')
    identity=data['workflow']['workflow_id']
    assert data['workflow']['status']=='awaiting_approval'
    assert data['workflow']['proposal_id']==str(proposal.proposal_id)
    assert runtime.get_state(identity).proposal.model_dump(mode='json')==data['action_proposal']
    assert 'No case has been created yet' in data['answer'] and 'awaiting approval' in data['answer']
    assert 'Approval bypassed' not in data['answer']
    assert data['workflow']['case_id'] is None
    assert snapshot(database)==before and counts(database)==(0,0)
    assert lifecycle=={'opened':1,'closed':1}
    assert KEY not in runtime.get_state(identity).model_dump_json()


@pytest.mark.parametrize('tool',['get_order_status_summary','get_customer_order_history','get_order_financial_summary','run_analytical_query'])
def test_ordinary_ask_does_not_open_workflows_or_require_key(boundary,tool):
    app,client,_,assistant,lifecycle=boundary
    app.state.settings=Settings(_env_file=None)
    assistant.return_value=AssistantResult(answer='Read-only answer',tool_calls=[ToolCallTrace(tool_name=tool,arguments={})])
    data=start(client,'A read-only business question')
    assert data['workflow'] is None and data['action_proposal'] is None
    assert data['answer']=='Read-only answer'
    assert lifecycle=={'opened':0,'closed':0}
    assert client.get('/health').json()=={'status':'ok'}


def test_workflow_failure_safe_and_no_memory_fallback(boundary,database):
    app,client,runtime,assistant,lifecycle=boundary
    @contextmanager
    def unavailable():
        raise WorkflowError('database_password approval_secret')
        yield
    app.dependency_overrides[api.get_workflow_factory]=lambda:unavailable
    response=client.post('/ask',json={'question':'Create review'})
    assert response.status_code==503 and response.json()=={'detail':'Workflow service is unavailable.'}
    assert 'database_password' not in response.text
    assert counts(database)==(0,0)
    assistant.return_value=AssistantResult(answer='625',tool_calls=[])
    assert start(client,'Read-only')['workflow'] is None


@pytest.mark.parametrize('path',['/workflows/{id}','/workflows/{id}/approve','/workflows/{id}/reject'])
@pytest.mark.parametrize('headers',[{}, {'X-Approval-Key':'wrong'}])
def test_authentication_before_lookup_and_mutation(boundary,path,headers,database):
    _,client,_,_,lifecycle=boundary
    url=path.format(id=str(uuid4()))
    response=client.get(url,headers=headers) if path.endswith('{id}') else client.post(url,headers=headers)
    assert response.status_code==401 and response.json()=={'detail':'Workflow access denied.'}
    assert KEY not in response.text and lifecycle=={'opened':0,'closed':0}
    assert counts(database)==(0,0)


def test_missing_server_key_fails_closed(boundary):
    app,client,_,_,lifecycle=boundary
    app.state.settings=Settings(_env_file=None)
    response=client.get('/workflows/'+str(uuid4()),headers=HEADERS)
    assert response.status_code==503
    assert lifecycle['opened']==0 and KEY not in response.text


@pytest.mark.parametrize('field',['actor','decision','order_id','reason','priority','action_type','idempotency_key','resume','case_id'])
def test_http_cannot_supply_actor_or_mutate_action(boundary,field,database):
    _,client,_,_,lifecycle=boundary
    identity=start(client)['workflow']['workflow_id']
    opened=lifecycle['opened']
    response=client.post(f'/workflows/{identity}/approve',headers=HEADERS,json={field:KEY})
    assert response.status_code==422
    assert KEY not in response.text and lifecycle['opened']==opened
    assert counts(database)==(0,0)


def test_authenticated_approve_and_duplicate_are_safe(boundary,database):
    _,client,runtime,_,lifecycle=boundary
    before=snapshot(database)
    identity=start(client)['workflow']['workflow_id']
    response=client.post(f'/workflows/{identity}/approve',headers=HEADERS)
    assert response.status_code==200
    data=response.json()
    assert data['status']=='completed' and data['case_id']
    assert counts(database)==(1,1)
    assert runtime.get_state(identity).approval.actor=='approval_api'
    assert runtime.get_state(identity).case.created_by=='approved_by:approval_api'
    assert client.get(f'/workflows/{identity}',headers=HEADERS).json()==data
    assert client.post(f'/workflows/{identity}/approve',headers=HEADERS,json={}).json()==data
    assert counts(database)==(1,1)
    assert client.post(f'/workflows/{identity}/reject',headers=HEADERS).status_code==409
    after=snapshot(database)
    assert all(before[name]==after[name] for name in before if not name.startswith('operations.'))
    assert KEY not in json.dumps(data) and 'idempotency_key' not in data
    assert 'created_by' not in data and 'checkpoint' not in json.dumps(data)
    assert lifecycle['opened']==lifecycle['closed']


def test_rejection_terminal_and_repeated_reject_conflicts(boundary,database):
    _,client,runtime,_,_=boundary
    identity=start(client)['workflow']['workflow_id']
    response=client.post(f'/workflows/{identity}/reject',headers=HEADERS)
    assert response.status_code==200 and response.json()['status']=='rejected'
    assert response.json()['case_id'] is None
    assert counts(database)==(0,0)
    assert client.post(f'/workflows/{identity}/approve',headers=HEADERS).status_code==409
    assert client.post(f'/workflows/{identity}/reject',headers=HEADERS).status_code==409
    assert runtime.get_state(identity).status=='rejected'


@pytest.mark.parametrize('path',['','/approve','/reject'])
def test_unknown_and_invalid_workflows_safe(boundary,path):
    _,client,_,_,_=boundary
    method=client.post if path else client.get
    unknown=method('/workflows/'+str(uuid4())+path,headers=HEADERS)
    invalid=method('/workflows/not-a-uuid'+path,headers=HEADERS)
    assert unknown.status_code==404 and unknown.json()=={'detail':'Workflow not found.'}
    assert invalid.status_code==400 and invalid.json()=={'detail':'Invalid workflow ID.'}


def test_concurrent_duplicate_http_approval(boundary,database):
    app,client,_,_,_=boundary
    identity=start(client)['workflow']['workflow_id']
    def approve(_):
        with TestClient(app) as separate:
            return separate.post(f'/workflows/{identity}/approve',headers=HEADERS)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses=list(pool.map(approve,range(2)))
    assert [response.status_code for response in responses]==[200,200]
    assert responses[0].json()['case_id']==responses[1].json()['case_id']
    assert counts(database)==(1,1)


def test_no_workflow_identity_in_request_or_tool_arguments(boundary):
    _,client,_,_,lifecycle=boundary
    response=client.post('/ask',json={'question':'Create review','workflow_id':str(uuid4())})
    assert response.status_code==422 and lifecycle['opened']==0
    from enterprise_ai_ops_agent.ai.tool_schemas import ARGUMENT_MODELS
    from pydantic import ValidationError
    for name in ('workflow_id','thread_id','approval','actor'):
        with pytest.raises(ValidationError):
            ARGUMENT_MODELS['propose_order_review_case'].model_validate({
                'order_id':'order','reason':'review','priority':'high',name:'injected'})


def test_safe_openapi_and_route_separation(boundary):
    app,_,_,_,_=boundary
    schema=app.openapi(); models=schema['components']['schemas']
    assert models['AssistantResult']['properties']['workflow']['anyOf'][0]['$ref'].endswith('/WorkflowSummary')
    assert set(models['WorkflowSummary']['properties'])=={
        'workflow_id','proposal_id','status','action_type','order_id','reason','priority','case_id','failure_category'}
    for path in ('/workflows/{workflow_id}','/workflows/{workflow_id}/approve','/workflows/{workflow_id}/reject'):
        operation=schema['paths'][path]['get' if path.endswith('{workflow_id}') else 'post']
        assert operation['security']==[{'ApprovalKey':[]}]
        assert operation['responses']['200']['content']['application/json']['schema']['$ref'].endswith('/WorkflowSummary')
    assert schema['components']['securitySchemes']['ApprovalKey']['name']=='X-Approval-Key'
    for forbidden in ('APPROVAL_API_KEY',KEY,'checkpoint_id','checkpoint_ns','idempotency_key','database_url'):
        assert forbidden not in json.dumps(schema)
    for path in ('src/enterprise_ai_ops_agent/api/workflows.py','src/enterprise_ai_ops_agent/api/assistant.py'):
        tree=ast.parse(Path(path).read_text())
        assert not any(isinstance(node,ast.Call) and getattr(node.func,'attr',None) in ('invoke','update_state','create_order_review_case') for node in ast.walk(tree))
        assert not any(isinstance(node,ast.ImportFrom) and node.module=='enterprise_ai_ops_agent.operations.service' for node in ast.walk(tree))


def test_failed_summary_hides_internal_error(boundary):
    _,client,runtime,_,_=boundary
    identity=start(client)['workflow']['workflow_id']
    state=runtime.get_state(identity).model_copy(update={'status':'failed','error':'database_password private stack trace'})
    runtime.get_state=MagicMock(return_value=state)
    response=client.get(f'/workflows/{identity}',headers=HEADERS)
    assert response.status_code==200
    assert response.json()['failure_category']=='action_execution_failed'
    assert 'database_password' not in response.text and 'stack trace' not in response.text
