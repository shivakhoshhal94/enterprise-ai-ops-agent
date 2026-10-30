# Experimental question-answer orchestration

`AGENT_ORCHESTRATOR=responses` (default) uses the unchanged direct OpenAI Responses
loop in `ai/assistant.py`. `AGENT_ORCHESTRATOR=langgraph` selects
`ai/langgraph_assistant.py` for both `/ask` and the existing AI CLI. Both functions
have the same calling convention and return `AssistantResult`; no additional
interface hierarchy is needed for two implementations.

Set this optional value in the backend process environment or its existing env
file, then restart that backend. Native Settings continues to read `.env`;
`uvicorn --env-file local.env` continues to load that file. Existing process
environment precedence is unchanged. The setting does not change Streamlit.
For Compose, explicitly supply the setting to the app process (for example with
`docker compose run --rm -e AGENT_ORCHESTRATOR=langgraph ...`); merely adding it to
the Compose interpolation env file does not automatically export it to containers.

Before: `/ask -> direct loop -> controlled executor -> AssistantResult`.

Alternative: `/ask -> START -> agent -> controlled_tools -> agent -> END`.
The agent node uses the existing OpenAI SDK and seven shared strict schemas.
Conditional edges choose tool execution or completion. Typed request-local state
tracks Responses continuation items, tool traces, source metadata, presentation
data, proposal, execution count and analytical correction state. All provider
output items, including internal reasoning items, are replayed for continuation;
only the application response model is exposed publicly.

The graph permits up to three sequential tool calls, including different tools
within one request, plus a final model response. It retains one analytical
correction opportunity within that bound. Multiple simultaneous calls in a
single provider response are rejected, matching the stable path. Tool execution
uses the existing validated dispatcher, SQL guardrails/readers, RAG and MCP
boundaries. Existing answer-formatting helpers preserve financial precision,
provenance and the treatment of external text as untrusted data.

There is no conversational checkpointer, cross-request memory, automatic retry,
database setup or direct write node. The durable human-approval graph under
`workflows/` remains separate: the existing API handoff can persist a proposal
at its approval interrupt, and only the protected approval route can execute it.
The graph itself returns an ephemeral proposal with no workflow or business write.

No LangChain dependency is added. The already-installed LangGraph and OpenAI SDK
are sufficient. Existing provider/tool timing and request IDs are reused; the
API emits only the selected orchestrator name as additional safe metadata.

Offline validation:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_langgraph_assistant.py -W always -ra
.\.venv\Scripts\python.exe -m pytest -W always -ra
```

Scripted-provider parity tests exercise all seven tools, mixed-source calls,
metadata, guarded failures and API selection. They do not measure live model
routing accuracy, latency or cost. Orchestration control flow exists in two
modules temporarily to preserve the stable implementation for comparison; future
policy changes require parity review. No live provider or database check is
performed by these unit tests.

## Comparing the paths

The shared cases in `evals/orchestrator_scenarios.json` cover business lookup,
analytics, company guidance, support, financial-plus-support and a review proposal.
The runner follows the existing deterministic evaluation convention: it runs
pytest with the existing SDK-shaped fake provider and controlled-tool fixtures.
No new model adapter, evaluation framework or live API mode is introduced.

```powershell
.\.venv\Scripts\python.exe evals/compare_orchestrators.py
.\.venv\Scripts\python.exe evals/compare_orchestrators.py --orchestrator responses
.\.venv\Scripts\python.exe evals/compare_orchestrators.py --orchestrator langgraph
```

Each scenario emits a safe `ORCHESTRATION_RESULT` JSON record with completion,
expected tool match, executed call count, multi-tool completion, public contract,
source separation, proposal-only checks and elapsed milliseconds. Pytest failures
produce a nonzero exit code. Dependency exceptions appear as failed scenarios,
not fabricated success records. Timing covers orchestration with fake dependencies,
including graph construction; it excludes real provider, database, embedding and
MCP transport latency. It is diagnostic timing, not a production benchmark.

FastAPI startup tests select each implementation through the actual environment
setting and exercise `/health`, `/ask` and OpenAPI using TestClient. External
dependencies are fake; this does not certify live OpenAI credentials or real
database/MCP availability. Existing guardrail and orchestration regression tests
remain necessary: six representative scenarios are not a security proof.

Both implementations serve the same contract. The direct loop has less control-flow
machinery; LangGraph makes the agent/tool branches and request state explicit.
Neither changes tool permissions or replaces the separate human-approval graph.
These offline checks compare handling of scripted decisions, not which framework
makes a model select better tools. Live task success, latency and cost require a
separately authorized, repeated experiment with actual dependencies.

Recorded offline comparison: both paths passed all six scenarios (seven controlled
tool executions per path, including the two-tool financial/support case). Contract,
source separation and proposal-only checks passed for both. No behavioral difference
was detected within this set. Both FastAPI startup/HTTP-contract tests passed.
An explicit local Uvicorn run with `AGENT_ORCHESTRATOR=langgraph` also verified
`/health`, the `/ask` OpenAPI response schema and invalid-input rejection. Positive
`/ask` execution was verified with fake dependencies, not live OpenAI/database access.
