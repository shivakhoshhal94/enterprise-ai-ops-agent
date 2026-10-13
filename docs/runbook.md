# Reproducible setup, verification, and demo

Run commands from the repository root. Requirements: Docker Desktop with Linux
containers and Compose supporting `!reset`/`!override` (2.24.4+; verified with 5.5.1).
Python 3.12 is needed for host tests/acceptance, not for the containerized service.
Obtain the public Olist dataset separately and place the original CSVs in ignored
`data/raw/`. Nothing here downloads, edits, or commits the raw dataset.

## Configuration

Copy `.env.example` to **ignored** `.env.docker` only if that file does not exist.
Edit placeholders locally. Use three different random URL-safe database passwords
(for example hexadecimal strings) and an independent approval secret. Do not print
the file, resolved Compose configuration, or container environment.

| Variable | Requirement / meaning |
| --- | --- |
| `POSTGRES_PASSWORD` | Docker bootstrap password; must match the primary URL password |
| `DATABASE_URL` | Required by packaged Compose; trusted primary/setup/operations connection |
| `OPENAI_API_KEY`, `OPENAI_MODEL` | Assistant provider configuration; use your approved local key/model |
| `ANALYTICS_DATABASE_URL` | Dedicated `enterprise_ai_ops_reader` login; no primary fallback |
| `KNOWLEDGE_DATABASE_URL` | Dedicated `enterprise_ai_ops_knowledge_reader`; no primary fallback |
| `OPENAI_EMBEDDING_MODEL` | Current profile `text-embedding-3-small`; do not change a populated corpus implicitly |
| `OPENAI_EMBEDDING_DIMENSIONS` | Current schema fixes this at 1536 |
| `APPROVAL_API_KEY` | Required for readiness and protected workflow endpoints; demo source authentication |
| `APP_NAME`, `ENVIRONMENT`, `LOG_LEVEL` | Safe defaults; optional operational settings |
| `APP_PORT` | Optional localhost host binding; default 8000; container always listens on 8000 |

Inside Compose all three database URLs use `postgres:5432` and database
`enterprise_ai_ops_agent`. Native host Python uses `localhost:5432` instead. The
variable names are identical: no duplicate container URL settings are introduced.
Checkpoint persistence uses the primary URL, with schema `workflow_checkpoints`;
there is no separate workflow URL. Compose supplies environment variables; secret
files are never copied into the image. Native `Settings()` reads `.env`, while the
knowledge CLI explicitly also reads `local.env`; process environment takes precedence.

Bare `/health` still starts without a database; the **configured Compose deployment**
requires the primary URL. `/ready` requires the full configuration and existing setup.
Configuration checks do not prove that OpenAI authorizes the key/model.
On Windows, Docker may be unable to bind a reserved port even when no process is
listening. Inspect `netsh interface ipv4 show excludedportrange protocol=tcp` and set
`APP_PORT=18001` (or another available port) in `.env.docker` if needed. Use that host
port in HTTP examples/acceptance. Do not alter system reservations just for this demo.

## One-time setup (PowerShell)

This block is for a new empty database. Do not run historical ingestion on a populated
database: loaders intentionally refuse. Do not overwrite an existing secret file.

```powershell
Copy-Item .env.example .env.docker # only if absent; edit it locally before continuing
$dc = @('compose', '--project-name', 'enterprise-ai-ops-agent', '--env-file', '.env.docker')
docker @dc build app
docker @dc up -d --wait --wait-timeout 120 postgres

# Three distinct explicit schema owners; none is automatic at startup.
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.db.create_schema
docker @dc run --rm -T --no-deps app python -m alembic upgrade head
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.workflows.cli setup
# Knowledge provisioning checks the protected checkpoint schema: setup above must run first.
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.db.provision_analytics_role
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.knowledge.cli provision

$raw = (Resolve-Path data/raw).Path
foreach ($dataset in @('customers','category_translations','sellers','products','orders','order_items','order_payments')) {
    docker @dc run --rm -T --no-deps --volume "${raw}:/app/data/raw:ro" app python -m "enterprise_ai_ops_agent.ingestion.$dataset"
    if ($LASTEXITCODE -ne 0) { throw "Ingestion failed: $dataset" }
}
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.knowledge.cli ingest
docker @dc run --rm -T --no-deps app python -m enterprise_ai_ops_agent.knowledge.cli evaluate
docker @dc up -d --wait --wait-timeout 120 app
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
```

Check every command's exit code before proceeding. Schema creation/migrations/role
provisioning/checkpointer setup are explicit repeatable commands, not daily startup.
Knowledge synchronization reuses unchanged chunks; it is an explicit write operation.
Expected source/database counts: customers 99,441; translations 71; sellers 3,095;
products 32,951; orders 99,441; items 112,650; payments 103,886. Knowledge: 6 documents,
25 chunks. Orders depends on customers; items depends on orders/products/sellers;
payments depends on orders. Geolocation/reviews are not loaded by this project.

Portable equivalents use `docker compose --project-name enterprise-ai-ops-agent
--env-file .env.docker ...` in place of `docker @dc ...`. For raw ingestion use
`--volume "$PWD/data/raw:/app/data/raw:ro"` and the same module names.

## Normal startup and shutdown

For an already configured and populated database:

```powershell
$dc = @('compose', '--project-name', 'enterprise-ai-ops-agent', '--env-file', '.env.docker')
docker @dc up -d --wait --wait-timeout 120
docker @dc ps
Invoke-RestMethod http://127.0.0.1:8000/health
Invoke-RestMethod http://127.0.0.1:8000/ready
docker @dc logs app
docker @dc down
```

`down` stops/removes containers and network, preserving the named PostgreSQL volume.
Deleting that volume is destructive. It is **not** a normal reset/restart command.
The primary volume must never be used for destructive clean-room tests.

The image installs project dependencies from metadata under verified constraints.
It runs a fixed source tree at `/app/src` so the existing fixed MCP asset path resolves
to `/app/mcp_servers`. The child still uses `python -I`, the SDK's restricted environment,
and the installed MCP SDK. The runtime UID is 10001, root filesystem read-only, with
a small `/tmp` tmpfs. Only the app port is exposed by the image; Compose binds app
8000 and existing PostgreSQL 5432 to localhost. No auto-reloader/setup/test runs on startup.

## Offline verification

```powershell
py -3.12 -m venv .venv # skip if a Python 3.12 environment already exists
.\.venv\Scripts\python.exe -m pip install -e ".[dev]" -c constraints.txt
.\.venv\Scripts\python.exe -m pytest -W always -ra
.\.venv\Scripts\python.exe evals/run_deterministic.py
.\.venv\Scripts\python.exe -m pip check
.\.venv\Scripts\python.exe scripts/secret_scan.py
git diff --check
```

`constraints.txt` pins the verified resolution while `pyproject.toml` remains the
dependency source. It is a version snapshot, not a hashes-based supply-chain lock.
No installed package is upgraded merely to obtain a newer version. The offline
evaluation runner executes the eleven existing suites unchanged and requires no
paid provider calls or PostgreSQL. MCP transport tests start short-lived local children.

Optional **paid live**, read-only API acceptance:

```powershell
.\.venv\Scripts\python.exe scripts/acceptance.py --base-url http://127.0.0.1:8000 --project enterprise-ai-ops-agent --report .venv/acceptance-readonly.json
```

This runs A-J only, never approval/proposal creation. It sends public historical
aggregate/lookup results, authored demo guidance, and synthetic support context
through the configured OpenAI integration. Reports contain outcomes/IDs, not payloads.
The Compose project enables process-exit and content-free log checks. Without it,
HTTP checks still run but Docker observability/cleanup evidence is not collected.
Standalone existing live corpora remain available as `python evals/run_assistant_knowledge.py
--live` and `python evals/run_assistant_support.py --live`; their proposal cases write
pending workflows. Use them only in a deliberately configured isolated environment,
not as the default real-database read-only command.

## Isolated fresh-install acceptance

Never reuse the primary volume. Choose a project starting with `eaiops-cleanroom-`.
Create ignored `.env.cleanroom` from the example, with **new temporary database and
approval credentials**, container hostname `postgres`, and your approved OpenAI key.

```powershell
$project = 'eaiops-cleanroom-' + [guid]::NewGuid().ToString('N').Substring(0,8)
$dc = @('compose','--project-name',$project,'--env-file','.env.cleanroom','-f','docker-compose.yml','-f','docker-compose.cleanroom.yml')
docker @dc build app
docker @dc up -d --wait --wait-timeout 120 postgres
```

Run the **same explicit one-time setup commands above with this `$dc`**. The override
removes the temporary database's host port, binds its app to localhost:18000, and
sets `ENVIRONMENT=cleanroom`. Compose generates `${project}_postgres_data` and its
own network; the primary project's volume is untouched. Re-run knowledge ingestion
and verify `embedded=0`, `reused=25`, `inserted=0`, `removed=0`. Run retrieval evaluation.

```powershell
docker @dc up -d --wait --wait-timeout 120 app
.\.venv\Scripts\python.exe scripts/acceptance.py --base-url http://127.0.0.1:18000 --project $project --isolated-actions --report .venv/acceptance-isolated.json
docker @dc exec -T app python scripts/integrity.py
```

The runner refuses action acceptance unless Docker labels, network, volume, database
hostname, clean-room setting, and localhost port match. K-M creates two new isolated
workflows: one is approved twice (one case/event), the other is rejected (no new
case/event). It does not accept an arbitrary pre-existing workflow ID.

Only after checking the selected name/volume belongs to this disposable project:

```powershell
docker volume inspect "${project}_postgres_data" --format '{{index .Labels "com.docker.compose.project"}}'
# Destructive ONLY for this verified disposable eaiops-cleanroom-* project:
docker @dc down --volumes
```

Do not substitute the primary project in this cleanup command. Do not run cleanup
against an unverified/shared external volume.

## Five-to-ten-minute reviewer demo

Use the configured isolated application at localhost:18000 for action demonstrations.
Read-only demonstrations may use localhost:8000. The expected facts below are
historical observations, not live business state.

```powershell
$base = 'http://127.0.0.1:18000'
Invoke-RestMethod "$base/health"
Invoke-RestMethod "$base/ready"
function Ask-Demo($question) {
    Invoke-WebRequest -Method Post -Uri "$base/ask" -ContentType 'application/json' -Body (@{question=$question}|ConvertTo-Json)
}
Ask-Demo 'How many orders were canceled?' # 625, deterministic tool
Ask-Demo 'Give me the financial breakdown for order 03ecec245220b63fd7f68c1737ba99ba. Include item price total, freight total, items plus freight, recorded payment total, and the difference in BRL with two decimal places, including zero-valued totals.'
Ask-Demo 'Which five product categories have the highest total item price?'
Ask-Demo 'Does three installments mean payment_value times three?'
Ask-Demo 'What support context exists for order 03ecec245220b63fd7f68c1737ba99ba?'
# Read the response Headers['X-Request-ID']; inspect tool/source attribution in Content.

# A-M runner safely demonstrates a new awaiting-approval proposal, durable status,
# authenticated approval, exactly-once duplicate approval, and rejection.
# Run it against a fresh isolated operational schema, not after another action demo:
.\.venv\Scripts\python.exe scripts/acceptance.py --base-url $base --project $project --isolated-actions
docker @dc exec -T app python scripts/integrity.py # shows the one case/event, not payloads
docker @dc logs app
.\.venv\Scripts\python.exe -m pytest -W always -ra
```

Portable read-only curl example (no keys in command):

```sh
curl -i http://127.0.0.1:18000/health
curl -i http://127.0.0.1:18000/ready
curl -i -H 'Content-Type: application/json' -d '{"question":"How many orders were canceled?"}' http://127.0.0.1:18000/ask
```

The protected action demo obtains its temporary key internally from the verified
isolated container and never prints it. Do not paste keys into commands/reports.
`/openapi.json` and `/docs` describe the six paths and current additive response fields.

## Operational evidence

Representative **fabricated**, content-free event block:

```json
{"timestamp":"2026-10-13T12:00:00.000+00:00","level":"INFO","event":"request_started","request_id":"12345678-1234-4234-8234-123456789abc","component":"http","method":"POST","route":"<unresolved>"}
{"timestamp":"2026-10-13T12:00:00.100+00:00","level":"INFO","event":"tool_execution_completed","request_id":"12345678-1234-4234-8234-123456789abc","component":"tool","tool_name":"get_order_status_summary","duration_ms":10.0,"outcome":"completed"}
{"timestamp":"2026-10-13T12:00:00.200+00:00","level":"INFO","event":"request_completed","request_id":"12345678-1234-4234-8234-123456789abc","component":"http","method":"POST","route":"/ask","status_code":200,"duration_ms":200.0,"outcome":"completed"}
```

For before/after real-data protection use `python scripts/integrity.py --output
.venv/before.json`, read-only acceptance, then an after snapshot and compare the
complete JSON objects. The script reports full-row MD5 fingerprints, source SHA-256,
reader privileges, checkpoint counts/hashes, and knowledge source-content fingerprint;
it never emits checkpoint rows or connection strings. Cross-environment knowledge
comparison uses content hashes because ingestion timestamps/embeddings can differ.
Raw-file SHA-256 should also be checked before/after fresh ingestion.

The existing timeout inventory and observability behavior are recorded in README's
Milestone 11A notes. No new retries, tracing backend, or monitoring stack is introduced.
