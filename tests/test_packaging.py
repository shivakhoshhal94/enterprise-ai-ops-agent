"""Offline protection for final packaging and the isolated approval gate."""

from pathlib import Path
import pytest
import yaml
from scripts import acceptance

PROJECT='eaiops-cleanroom-unit'


def target():
    app={'Config':{'Labels':{'com.docker.compose.project':PROJECT,'com.docker.compose.service':'app'},
        'Env':['ENVIRONMENT=cleanroom','DATABASE_URL=postgresql+psycopg://demo:fixture@postgres:5432/enterprise_ai_ops_agent',
               'APPROVAL_API_KEY=unit-only-approval']},
        'HostConfig':{'ExtraHosts':[]},'NetworkSettings':{'Networks':{PROJECT+'_default':{}},
            'Ports':{'8000/tcp':[{'HostIp':'127.0.0.1','HostPort':'18000'}]}}}
    postgres={'Config':{'Labels':{'com.docker.compose.project':PROJECT,'com.docker.compose.service':'postgres'}},
        'NetworkSettings':{'Networks':{PROJECT+'_default':{}}},
        'Mounts':[{'Destination':'/var/lib/postgresql/data','Type':'volume','Name':PROJECT+'_postgres_data'}]}
    return app,postgres


def test_gate_accepts_only_verified_isolated_resources():
    app,postgres=target()
    assert acceptance.verify_isolated_target('http://127.0.0.1:18000',PROJECT,app,postgres)=='unit-only-approval'


@pytest.mark.parametrize('change',['project','volume','bind','network','extra_network','database','environment','labels','key','extra_hosts','remote','port'])
def test_gate_refuses_primary_or_unverified_targets(change):
    app,postgres=target();project=PROJECT;url='http://127.0.0.1:18000'
    if change=='project':project='enterprise-ai-ops-agent'
    elif change=='volume':postgres['Mounts'][0]['Name']='enterprise-ai-ops-agent_postgres_data'
    elif change=='bind':postgres['Mounts'][0]['Type']='bind'
    elif change=='network':app['NetworkSettings']['Networks']={}
    elif change=='extra_network':app['NetworkSettings']['Networks']['enterprise-ai-ops-agent_default']={}
    elif change=='database':app['Config']['Env'][1]='DATABASE_URL=postgresql+psycopg://demo:fixture@localhost/example'
    elif change=='environment':app['Config']['Env'][0]='ENVIRONMENT=development'
    elif change=='labels':postgres['Config']['Labels']['com.docker.compose.project']='enterprise-ai-ops-agent'
    elif change=='key':app['Config']['Env'].pop()
    elif change=='extra_hosts':app['HostConfig']['ExtraHosts']=['postgres:external']
    elif change=='remote':url='http://external.invalid:18000'
    elif change=='port':url='http://127.0.0.1:8000'
    with pytest.raises(acceptance.AcceptanceError) as caught:
        acceptance.verify_isolated_target(url,project,app,postgres)
    assert 'fixture' not in str(caught.value) and 'unit-only-approval' not in str(caught.value)


def test_no_actions_without_explicit_isolated_flag(monkeypatch,capsys):
    invoked=[]
    monkeypatch.setattr(acceptance,'app_container',lambda *args:'app')
    monkeypatch.setattr(acceptance,'docker_output',lambda *args:invoked.append(args) or '')
    assert acceptance.main(['--isolated-actions'])==1
    assert not invoked and 'require --project' in capsys.readouterr().err


@pytest.mark.parametrize('answer',['BRL 1,258,681.34','BRL 1.258.681,34','1258681.34'])
def test_money_checks_accept_supported_formatting_without_float_rounding(answer):
    from decimal import Decimal
    assert acceptance.money_values(answer)=={Decimal('1258681.34')}


def status_result():
    return {'answer':'625 canceled orders.','tool_calls':[{'tool_name':'get_order_status_summary','arguments':{}}],
        'action_proposal':None,'workflow':None,'knowledge_sources':[],'external_sources':[]}


@pytest.mark.parametrize('change',['wrong_count','unexpected_tool','proposal','workflow','rag','mcp','arguments'])
def test_acceptance_rejects_wrong_facts_and_unrequested_capabilities(change):
    result=status_result()
    if change=='wrong_count':result['answer']='626 canceled orders.'
    elif change=='unexpected_tool':result['tool_calls'][0]['tool_name']='search_company_knowledge'
    elif change in ('proposal','workflow'):result['action_proposal' if change=='proposal' else 'workflow']={'present':True}
    elif change=='rag':result['knowledge_sources']=[{'document':'demo'}]
    elif change=='mcp':result['external_sources']=[{'source_type':'mcp_support'}]
    elif change=='arguments':result['tool_calls'][0]['arguments']={'unsupported':'value'}
    with pytest.raises(acceptance.AcceptanceError):acceptance.verify_answer('C',result)


def test_financial_acceptance_rejects_imprecise_totals():
    result=status_result()
    result['tool_calls']=[{'tool_name':'get_order_financial_summary','arguments':{'order_id':acceptance.ORDER}}]
    result['answer']='Price BRL 298.90, freight 76.83, combined and payments 375.73, difference 0.00.'
    acceptance.verify_answer('E',result)
    result['answer']=result['answer'].replace('375.73','375.74')
    with pytest.raises(acceptance.AcceptanceError):acceptance.verify_answer('E',result)


def test_packaging_preserves_volume_and_private_ports():
    compose=yaml.safe_load(Path('docker-compose.yml').read_text())
    assert set(compose['services'])=={'app','postgres'}
    assert compose['services']['postgres']['volumes']==['postgres_data:/var/lib/postgresql/data']
    assert compose['services']['postgres']['ports']==['127.0.0.1:5432:5432']
    app=compose['services']['app']
    assert app['ports']==['127.0.0.1:${APP_PORT:-8000}:8000']
    assert app['depends_on']['postgres']['condition']=='service_healthy'
    assert app['read_only'] and app['cap_drop']==['ALL'] and app['init']
    assert not app.get('volumes')
    assert set(app['environment'])>={'DATABASE_URL','ANALYTICS_DATABASE_URL','KNOWLEDGE_DATABASE_URL',
        'OPENAI_API_KEY','OPENAI_MODEL','APPROVAL_API_KEY','OPENAI_EMBEDDING_MODEL','OPENAI_EMBEDDING_DIMENSIONS'}


def test_image_excludes_secrets_and_automatic_setup():
    dockerfile=Path('Dockerfile').read_text()
    assert 'USER 10001:10001' in dockerfile and 'python:3.12-' in dockerfile
    assert '--reload' not in dockerfile and 'upgrade head' not in dockerfile and 'create_all' not in dockerfile
    assert 'COPY . ' not in dockerfile and 'PYTHONPATH=/app/src' in dockerfile
    ignore=set(Path('.dockerignore').read_text().splitlines())
    assert {'.git','.venv','.env','.env.*','local.env','data/raw','.history','.pytest_cache'}<=ignore
    assert not {'src','migrations','knowledge','mcp_servers','pyproject.toml'}&ignore


def test_offline_aggregate_keeps_original_suite_inventory():
    from evals.run_deterministic import SUITES
    assert len(SUITES)==11 and len(set(SUITES))==11
    assert all((Path('tests')/name).is_file() for name in SUITES)


@pytest.mark.parametrize('payload',[
    {'question':123,'OPENAI_API_KEY':'fixture-confidential-value'},
    {'question':'valid','DATABASE_URL':'postgresql://fixture:fixture-confidential-value@host/database'},
    {'question':'valid','SELECT fixture-confidential-value FROM orders':'unexpected'},
    {},
])
def test_validation_errors_do_not_reflect_input_credentials_or_sql(payload):
    from fastapi.testclient import TestClient
    from enterprise_ai_ops_agent.config import Settings
    from enterprise_ai_ops_agent.main import create_app
    client=TestClient(create_app(Settings(_env_file=None,database_url=None,openai_api_key=None)))
    response=client.post('/ask',json=payload)
    assert response.status_code==422
    assert response.json()=={'detail':[{'loc':['request'],'msg':'Invalid request.','type':'request_validation_error'}]}
    assert 'fixture-confidential-value' not in response.text and 'SELECT ' not in response.text
    assert response.headers['X-Request-ID']


def test_failed_acceptance_assertion_is_not_reported_as_passed(monkeypatch,tmp_path):
    class Response:
        status_code=200
        headers={'X-Request-ID':'12345678-1234-4234-8234-123456789abc',
                 'X-Content-Type-Options':'nosniff','Cache-Control':'no-store'}
        def __init__(self,value):self.value=value
        def json(self):return self.value
    class HTTP:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def get(self,*args):return Response({})
        def request(self,method,path,**kwargs):
            if path=='/health':return Response({'status':'ok'})
            if path=='/ready':return Response({'ready':True,'checks':{'database':'ready'}})
            wrong=status_result();wrong['answer']='626 canceled orders.'
            return Response(wrong)
    monkeypatch.setattr(acceptance.httpx2,'Client',lambda **kwargs:HTTP())
    monkeypatch.setattr(acceptance,'verify_openapi',lambda schema:None)
    report=tmp_path/'acceptance.json'
    assert acceptance.main(['--report',str(report)])==1
    import json
    result=json.loads(report.read_text())
    assert result['passed'] is False
    assert result['cases'][-1]['case']=='C' and result['cases'][-1]['passed'] is False
