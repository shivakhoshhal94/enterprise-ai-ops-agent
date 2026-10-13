"""Read-only fingerprint report; never prints rows, URLs, or checkpoint contents."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from sqlalchemy import text
from enterprise_ai_ops_agent.config import Settings
from enterprise_ai_ops_agent.db.session import create_db_engine
from enterprise_ai_ops_agent.db.provision_analytics_role import inspect_analytics_privileges
from enterprise_ai_ops_agent.knowledge.provision import inspect_knowledge_reader

TABLE_KEYS = {
    "public.customers":"customer_id", "public.category_translations":"product_category_name",
    "public.sellers":"seller_id", "public.products":"product_id", "public.orders":"order_id",
    "public.order_items":"order_id,order_item_id", "public.order_payments":"order_id,payment_sequential",
    "operations.order_review_cases":"case_id", "operations.case_events":"event_id",
    "knowledge.document_chunks":"chunk_id",
}


def snapshot(settings: Settings):
    engine = create_db_engine(settings)
    try:
        with engine.connect().execution_options(postgresql_readonly=True) as connection:
            tables = {}
            for table, keys in TABLE_KEYS.items():
                row = connection.execute(text(f"SELECT count(*),md5(string_agg(r::text,'' ORDER BY {keys})) FROM {table} r")).one()
                tables[table] = {"count":row[0], "fingerprint":row[1]}
            checkpoints = {}
            for name in ("checkpoint_blobs","checkpoint_migrations","checkpoint_writes","checkpoints"):
                row = connection.execute(text(f"SELECT count(*),md5(string_agg(r::text,'' ORDER BY r::text)) FROM workflow_checkpoints.{name} r")).one()
                checkpoints[name] = {"count":row[0], "fingerprint":row[1]}
            documents = connection.execute(text("SELECT count(DISTINCT document_id) FROM knowledge.document_chunks")).scalar_one()
            # Excludes ingestion timestamps/embeddings: useful for clean-room source reconciliation.
            content = connection.execute(text("SELECT md5(string_agg(document_id||':'||chunk_index::text||':'||content_hash,'' ORDER BY document_id,chunk_index)) FROM knowledge.document_chunks")).scalar_one()
            privileges = {"analytics":inspect_analytics_privileges(connection).model_dump(),
                          "knowledge":inspect_knowledge_reader(connection)}
            foreign_keys = connection.execute(text("SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace WHERE n.nspname='public' AND c.contype='f' AND c.convalidated")).scalar_one()
        root = Path(__file__).resolve().parents[1]
        return {"tables":tables, "documents":documents, "knowledge_content_fingerprint":content,
                "checkpoints":checkpoints, "privileges":privileges, "validated_historical_foreign_keys":foreign_keys,
                "support_sha256":hashlib.sha256((root/'mcp_servers/support_tickets/data/support_tickets.json').read_bytes()).hexdigest(),
                "corpus_sha256":{p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in (root/'knowledge/source').glob('*.md')}}
    finally:
        engine.dispose()


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path)
    args=parser.parse_args(argv)
    try:
        result=snapshot(Settings(_env_file=('.env','local.env')))
        serialized=json.dumps(result,sort_keys=True)
        if args.output:args.output.write_text(serialized,encoding='utf-8')
        print(serialized)
        return 0
    except Exception:
        print('Integrity verification failed; check explicit database setup and configuration.',file=sys.stderr)
        return 1


if __name__=='__main__':raise SystemExit(main())
