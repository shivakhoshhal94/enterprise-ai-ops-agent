# Enterprise AI Operations Agent — Development Instructions

## Project purpose

This repository is a portfolio-grade AI Engineering project.

The goal is to build an enterprise-style AI operations agent that demonstrates practical skills commonly required in AI Engineer / LLM Engineer roles.

The final system should be able to combine:

- structured business data
- LLM tool calling
- stateful agent workflows
- deterministic safety controls
- retrieval over business knowledge
- controlled external actions
- human approval
- evaluation
- observability
- API deployment

The project should use a realistic e-commerce dataset rather than a purely synthetic demo database.

## Learning requirement

The repository owner is learning production-oriented agent engineering.

Do not implement the entire architecture at once.

Work incrementally.

For every milestone:

1. Explain what we are building.
2. Explain why it exists in a real company system.
3. Explain the important technical concepts before adding abstractions.
4. Identify the files that will be created or modified.
5. Implement only the current milestone.
6. Explain important code decisions.
7. Add or update tests.
8. Run the relevant tests.
9. Summarize what changed.
10. Stop before starting the next milestone.

Do not silently introduce multiple frameworks or architectural layers in one step.

## Engineering philosophy

Prefer established libraries and framework features over reinventing infrastructure.

Do not implement from scratch functionality already provided reliably by a standard framework unless there is a strong project-specific reason.

Preferred direction:

- FastAPI for the API layer
- Pydantic for schemas and validation
- SQLAlchemy for database access
- PostgreSQL as the main relational database
- OpenAI model APIs for LLM capabilities
- LangGraph when stateful orchestration becomes necessary
- pgvector when retrieval is introduced
- standard MCP SDKs when MCP is introduced
- pytest for testing
- Docker for reproducible local/runtime environments

Do not add a framework simply because it is popular.

Every dependency must solve a concrete architectural problem.

## Architecture rules

Keep clear boundaries between:

- API layer
- agent/workflow layer
- LLM provider layer
- tools
- database access
- deterministic guardrails
- retrieval
- evaluation
- observability

LLMs must not be treated as security boundaries.

Any operation that changes application state must use application-owned, parameterized code.

Never execute arbitrary model-generated write SQL.

Sensitive actions should eventually require explicit human approval.

## Agent design

Do not create multiple agents merely to call the system "multi-agent".

Start with the simplest architecture that solves the task.

Use deterministic nodes/functions for deterministic responsibilities such as:

- permissions
- SQL validation
- schema validation
- execution
- approval checks
- formatting

Use LLM-driven components only where language understanding or reasoning is genuinely useful.

## Data

Primary dataset:

Brazilian E-Commerce Public Dataset by Olist.

Raw external dataset files should not be committed to Git.

Use a `data/raw/` directory ignored by Git and provide reproducible ingestion scripts.

Preserve original source fields where practical and document transformations.

## Development quality

Code should be:

- modular
- testable
- readable
- typed where useful
- documented without excessive comments
- free of unnecessary abstraction

Never expose secrets.

Use environment variables and `.env.example`.

## Testing and evaluation

Each meaningful component should have appropriate tests.

Agent behaviour should eventually be evaluated separately for:

- routing
- tool selection
- SQL correctness
- safety
- retrieval
- action correctness
- end-to-end task success

Do not claim production quality based only on deterministic unit tests.

## Git and GitHub

Git is local during development.

Do not connect to GitHub, configure remotes, push code, create pull requests, or use GitHub integrations.

The user will publish the repository manually later.

Create logical commits only when explicitly requested by the user.

## Current workflow

Implement exactly one milestone at a time.

Do not jump ahead to RAG, MCP, multi-agent orchestration, human approval, or deployment until prerequisite layers are understood and working.

Before changing architecture, explain why the change is needed and what problem it solves.
