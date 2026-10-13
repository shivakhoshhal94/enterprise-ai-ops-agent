"""Explicit ingestion, provisioning, search and retrieval evaluation only."""

import argparse
import json
import sys
from contextlib import ExitStack
from pathlib import Path
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.ai.client import create_openai_client
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.knowledge.chunking import load_chunks
from enterprise_ai_ops_agent.knowledge.contracts import KnowledgeError
from enterprise_ai_ops_agent.knowledge.session import create_knowledge_engine
from enterprise_ai_ops_agent.knowledge.provision import provision_knowledge_reader
from enterprise_ai_ops_agent.knowledge.service import synchronize_knowledge, search_company_knowledge
from enterprise_ai_ops_agent.knowledge.evaluation import summarize_evaluation


def main(argv=None) -> int:
    parser=argparse.ArgumentParser(description=__doc__)
    commands=parser.add_subparsers(dest='command',required=True)
    commands.add_parser('provision')
    ingest=commands.add_parser('ingest'); ingest.add_argument('--source-root',default='knowledge/source')
    search=commands.add_parser('search'); search.add_argument('query'); search.add_argument('--top-k',type=int,default=5)
    evaluate=commands.add_parser('evaluate'); evaluate.add_argument('--cases',default='evals/knowledge_retrieval.json')
    args=parser.parse_args(argv)
    try:
        settings=Settings(_env_file=('.env','local.env'))
        if args.command=='provision':
            print(json.dumps(provision_knowledge_reader(settings)))
            return 0
        chunks=load_chunks(Path(args.source_root)) if args.command=='ingest' else None
        with ExitStack() as resources:
            engine=create_db_engine(settings) if args.command=='ingest' else create_knowledge_engine(settings)
            resources.callback(engine.dispose)
            client=create_openai_client(settings); resources.callback(client.close)
            if chunks is not None:
                print(synchronize_knowledge(engine,chunks,client=client,settings=settings).model_dump_json())
            elif args.command=='search':
                print(search_company_knowledge(args.query,top_k=args.top_k,engine=engine,client=client,settings=settings).model_dump_json())
            else:
                cases=json.loads(Path(args.cases).read_text(encoding='utf-8'))
                hits=[]
                for case in cases:
                    result=search_company_knowledge(case['query'],top_k=50,engine=engine,client=client,settings=settings)
                    hits.append([{'document_id':item.document_id,'section':item.section,'rank':item.rank,'distance':item.distance} for item in result.hits])
                    print(json.dumps({'evaluation_progress':case['id'],'top_hit':hits[-1][0] if hits[-1] else None}),file=sys.stderr,flush=True)
                print(json.dumps(summarize_evaluation(cases,hits)))
        return 0
    except KnowledgeError as error:
        print(str(error),file=sys.stderr); return 1
    except Exception:
        print('Knowledge command failed; check configuration, source files and explicit database setup.',file=sys.stderr); return 1


if __name__=='__main__': raise SystemExit(main())
