"""Offline parsing, embedding shape, real sync transactions and retrieval contracts."""

import ast
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine, event, select, func
from sqlalchemy.dialects import postgresql
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.knowledge import chunking, service, cli
from enterprise_ai_ops_agent.knowledge.contracts import DIMENSIONS, KnowledgeError, KnowledgeSearchResult
from enterprise_ai_ops_agent.knowledge.embeddings import embed_texts
from enterprise_ai_ops_agent.knowledge.models import KnowledgeBase, DocumentChunk
from enterprise_ai_ops_agent.knowledge.session import KNOWLEDGE_ROLE, create_knowledge_engine
from enterprise_ai_ops_agent.knowledge.evaluation import summarize_evaluation

SETTINGS=Settings(_env_file=None)
VECTOR=[1.0]+[0.0]*(DIMENSIONS-1)


def markdown(body='Policy paragraph one.\n\nPolicy paragraph two.'):
    return '# Demo Title\n\n'+chunking.DISCLAIMER+'\n\n## Section A\n\n'+body


def fake_client():
    client=MagicMock()
    def embeddings(**kwargs):
        return SimpleNamespace(model=kwargs['model'],data=[SimpleNamespace(index=i,embedding=VECTOR.copy()) for i,_ in enumerate(kwargs['input'])])
    client.embeddings.create.side_effect=embeddings
    return client


@pytest.fixture
def database():
    engine=create_engine('sqlite://',execution_options={'schema_translate_map':{'knowledge':None}})
    KnowledgeBase.metadata.create_all(engine)
    yield engine
    engine.dispose()


def rows(engine):
    with engine.connect() as connection:
        return list(connection.execute(select(DocumentChunk.__table__)).mappings())


def test_discovery_metadata_determinism(tmp_path):
    file=tmp_path/'sample.md'; file.write_text(markdown(),encoding='utf-8')
    assert chunking.discover_documents(tmp_path)==[file]
    first=chunking.load_chunks(tmp_path); second=chunking.load_chunks(tmp_path)
    assert first==second and len(first)==2  # Paragraphs packed within a section.
    assert first[1].title=='Demo Title' and first[1].section=='Section A'
    assert first[1].source_path=='knowledge/source/sample.md'
    assert first[1].document_id=='sample' and first[1].source_name=='sample.md'
    assert first[1].content_hash==second[1].content_hash
    assert len(first[1].content_hash)==64


def test_long_paragraph_bounded_and_nested_fenced_headings():
    text=markdown('word '*1500)+'\n\n### Nested\n\n```\n## Not a heading\n```\n'
    chunks=chunking.chunk_markdown(text,document_id='doc',source_name='doc.md')
    assert len(chunks)>3 and all(len(item.content)<=1200 for item in chunks)
    assert chunks[-1].section=='Section A / Nested'
    assert '## Not a heading' in chunks[-1].content
    assert chunks==chunking.chunk_markdown(text,document_id='doc',source_name='doc.md')


@pytest.mark.parametrize('text',['','   ','# Title','text without H1','# Title\n## Other\n```\nunterminated'])
def test_bad_document_rejected(text):
    with pytest.raises(KnowledgeError): chunking.chunk_markdown(text,document_id='doc',source_name='doc.md')


def test_unsupported_file_and_missing_disclaimer(tmp_path):
    path=tmp_path/'doc.txt'; path.write_text(markdown())
    with pytest.raises(KnowledgeError,match='Markdown'): chunking.load_chunks(tmp_path)
    path.unlink(); (tmp_path/'doc.md').write_text('# Title\n\nNot an official policy')
    with pytest.raises(KnowledgeError,match='disclaimer'): chunking.load_chunks(tmp_path)


def test_duplicate_document_id_and_empty_source(tmp_path,monkeypatch):
    with pytest.raises(KnowledgeError,match='empty'): chunking.load_chunks(tmp_path)
    path=tmp_path/'doc.md'; path.write_text(markdown())
    monkeypatch.setattr(chunking,'discover_documents',lambda root:[path,path])
    with pytest.raises(KnowledgeError,match='Duplicate'): chunking.load_chunks(tmp_path)


@pytest.mark.parametrize('vector',[[1.0], [0.0]*DIMENSIONS, [float('nan')]*DIMENSIONS, [True]*DIMENSIONS])
def test_invalid_embedding_vectors(vector):
    client=fake_client(); client.embeddings.create.side_effect=None
    client.embeddings.create.return_value=SimpleNamespace(model=SETTINGS.openai_embedding_model,data=[SimpleNamespace(index=0,embedding=vector)])
    with pytest.raises(KnowledgeError,match='incompatible'): embed_texts(client,['query'],SETTINGS)


def test_embedding_index_order_and_safe_provider_failure():
    client=fake_client()
    assert len(embed_texts(client,['first','second'],SETTINGS))==2
    assert client.embeddings.create.call_args.kwargs['dimensions']==1536
    client.embeddings.create.side_effect=RuntimeError('api_secret password')
    with pytest.raises(KnowledgeError) as caught: embed_texts(client,['query'],SETTINGS)
    assert 'api_secret' not in str(caught.value)


def test_model_shape_and_separate_metadata():
    table=DocumentChunk.__table__
    assert table.schema=='knowledge' and table.c.embedding.type.dim==1536
    assert table.c.created_at.type.timezone is True
    assert table.c.embedding.nullable is False
    assert set(table.primary_key.columns.keys())=={'chunk_id'}
    from enterprise_ai_ops_agent.db.base import Base
    from enterprise_ai_ops_agent.db import models
    assert len(Base.metadata.tables)==7


def test_embedding_dimension_environment_override(monkeypatch):
    monkeypatch.setenv('OPENAI_EMBEDDING_DIMENSIONS','1536')
    assert Settings(_env_file=None).openai_embedding_dimensions==1536
    monkeypatch.setenv('OPENAI_EMBEDDING_DIMENSIONS','512')
    from pydantic import ValidationError
    with pytest.raises(ValidationError): Settings(_env_file=None)


def test_sync_rerun_changed_removed_and_metadata(database,tmp_path):
    a=tmp_path/'a.md'; b=tmp_path/'b.md'
    a.write_text(markdown('Policy A')); b.write_text(markdown('Policy B'))
    chunks=chunking.load_chunks(tmp_path); client=fake_client()
    first=service.synchronize_knowledge(database,chunks,client=client,settings=SETTINGS)
    assert first.documents==2 and first.chunks==4 and len(rows(database))==4
    initial_calls=client.embeddings.create.call_count
    rerun=service.synchronize_knowledge(database,chunks,client=client,settings=SETTINGS)
    assert rerun.inserted==rerun.removed==rerun.embedded==0
    assert client.embeddings.create.call_count==initial_calls
    assert rows(database)[0]['source_path'].startswith('knowledge/source/')
    a.write_text(markdown('Changed policy A')); b.unlink()
    changed=service.synchronize_knowledge(database,chunking.load_chunks(tmp_path),client=client,settings=SETTINGS)
    assert changed.documents==1 and changed.chunks==2 and changed.embedded==1
    assert changed.removed==3 and changed.inserted==1 and len(rows(database))==2
    assert all(row['document_id']=='a' for row in rows(database))


def test_model_switch_rejected_before_embedding(database):
    chunks=chunking.chunk_markdown(markdown(),document_id='a',source_name='a.md'); client=fake_client()
    service.synchronize_knowledge(database,chunks,client=client,settings=SETTINGS)
    before=client.embeddings.create.call_count
    different=Settings(_env_file=None,openai_embedding_model='text-embedding-3-large')
    with pytest.raises(KnowledgeError,match='profile'): service.synchronize_knowledge(database,chunks,client=client,settings=different)
    assert client.embeddings.create.call_count==before and len(rows(database))==2


def test_sync_provider_failure_rollback(database):
    client=fake_client(); client.embeddings.create.side_effect=RuntimeError('api_secret')
    chunks=chunking.chunk_markdown(markdown(),document_id='a',source_name='a.md')
    with pytest.raises(KnowledgeError): service.synchronize_knowledge(database,chunks,client=client,settings=SETTINGS)
    assert rows(database)==[]


def test_sync_insert_failure_rolls_back_deletions(database):
    client=fake_client()
    chunks=chunking.chunk_markdown(markdown('old'),document_id='a',source_name='a.md')
    service.synchronize_knowledge(database,chunks,client=client,settings=SETTINGS)
    old_ids=[row['chunk_id'] for row in rows(database)]
    @event.listens_for(database,'before_cursor_execute')
    def fail(connection,cursor,statement,params,context,many):
        if statement.startswith('INSERT'): raise RuntimeError('database_password')
    with pytest.raises(KnowledgeError) as caught:
        service.synchronize_knowledge(database,chunking.chunk_markdown(markdown('changed'),document_id='a',source_name='a.md'),client=client,settings=SETTINGS)
    assert 'database_password' not in str(caught.value)
    assert [row['chunk_id'] for row in rows(database)]==old_ids


@pytest.mark.parametrize('query,k',[('',5),(' ',5),('x'*2001,5),('query',0),('query',51),('query',True)])
def test_search_validation_before_connections(query,k):
    engine=MagicMock()
    with pytest.raises(KnowledgeError): service.search_company_knowledge(query,top_k=k,engine=engine,client=fake_client(),settings=SETTINGS)
    engine.connect.assert_not_called()


def test_ranked_retrieval_and_sql_contract():
    engine=MagicMock(); engine.dialect.name='postgresql'; engine.url.username=KNOWLEDGE_ROLE
    connection=engine.connect.return_value.execution_options.return_value.__enter__.return_value
    profiles=MagicMock(); profiles.mappings.return_value.__iter__.return_value=iter([{'embedding_model':SETTINGS.openai_embedding_model,'embedding_dimensions':1536}])
    chunks=chunking.chunk_markdown(markdown(),document_id='a',source_name='a.md')
    nearest=MagicMock(); nearest.mappings.return_value.all.return_value=[{**item.model_dump(),'distance':distance} for item,distance in zip(chunks,(0.1,0.2))]
    connection.execute.side_effect=[MagicMock(),profiles,nearest]
    result=service.search_company_knowledge('policy question',engine=engine,client=fake_client(),settings=SETTINGS,top_k=2)
    assert isinstance(result,KnowledgeSearchResult) and [hit.rank for hit in result.hits]==[1,2]
    assert result.hits[0].similarity==0.9
    assert result.hits[0].source_path=='knowledge/source/a.md'
    sql=str(connection.execute.call_args.args[0].compile(dialect=postgresql.dialect()))
    assert '<=>' in sql and 'knowledge.document_chunks' in sql and 'ORDER BY' in sql
    assert 'embedding' not in result.model_dump()['hits'][0]
    engine.connect.return_value.execution_options.assert_called_once_with(postgresql_readonly=True)


def test_admin_engine_for_retrieval_rejected():
    engine=MagicMock(); engine.dialect.name='postgresql'; engine.url.username='admin'
    with pytest.raises(KnowledgeError,match='dedicated'): service.search_company_knowledge('query',engine=engine,client=fake_client(),settings=SETTINGS)
    engine.connect.assert_not_called()


@pytest.mark.parametrize('url',[None,'invalid','postgresql+psycopg://admin:private_password@localhost/db'])
def test_reader_configuration_has_no_fallback(url):
    with pytest.raises(KnowledgeError) as caught: create_knowledge_engine(Settings(_env_file=None,knowledge_database_url=url))
    assert 'private_password' not in str(caught.value)


def test_missing_embedding_key_cli_safe(monkeypatch,capsys):
    monkeypatch.setattr(cli,'Settings',lambda **kwargs:Settings(_env_file=None,openai_api_key=None))
    monkeypatch.setattr(cli,'create_knowledge_engine',lambda settings:MagicMock())
    assert cli.main(['search','query'])==1
    assert 'OPENAI_API_KEY' not in capsys.readouterr().out


def test_metrics_are_deterministic_and_section_specific():
    cases=[{'id':'a','kind':'policy','query':'q','source':'doc','section':'relevant'},
           {'id':'b','kind':'policy','query':'q','source':'doc','section':'missing'}]
    hits=[[{'document_id':'doc','section':'wrong'},{'document_id':'doc','section':'relevant'}],[]]
    result=summarize_evaluation(cases,hits)
    assert result['metrics']=={'Hit@1':0.0,'Hit@3':0.5,'Hit@5':0.5,'MRR':0.25}


def test_single_read_only_assistant_registration_and_separate_service():
    from enterprise_ai_ops_agent.ai.tool_schemas import TOOL_SCHEMAS
    assert len(TOOL_SCHEMAS)==7 and [item['name'] for item in TOOL_SCHEMAS if 'knowledge' in item['name']]==['search_company_knowledge']
    tree=ast.parse(Path('src/enterprise_ai_ops_agent/knowledge/service.py').read_text())
    for node in ast.walk(tree):
        if isinstance(node,ast.ImportFrom): assert not node.module.startswith(('langgraph','enterprise_ai_ops_agent.operations','enterprise_ai_ops_agent.workflows','enterprise_ai_ops_agent.business'))
    from enterprise_ai_ops_agent.main import create_app
    assert '/knowledge' not in create_app(Settings(_env_file=None)).openapi()['paths']


def test_import_startup_performs_no_network_or_database(monkeypatch):
    import importlib
    from enterprise_ai_ops_agent.db import session
    from enterprise_ai_ops_agent.ai import client as provider
    db=MagicMock(); network=MagicMock()
    with monkeypatch.context() as patch:
        patch.setattr(session,'create_db_engine',db)
        patch.setattr(provider,'create_openai_client',network)
        importlib.reload(cli)
        from enterprise_ai_ops_agent.main import create_app
        from fastapi.testclient import TestClient
        assert TestClient(create_app(Settings(_env_file=None))).get('/health').status_code==200
        db.assert_not_called(); network.assert_not_called()
    importlib.reload(cli)
