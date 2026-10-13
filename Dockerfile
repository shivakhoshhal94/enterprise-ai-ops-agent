FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app/src \
    PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /app

COPY pyproject.toml constraints.txt README.md ./
COPY src ./src
RUN python -m pip install --no-cache-dir -c constraints.txt . \
    && python -m pip check \
    && groupadd --gid 10001 agent \
    && useradd --uid 10001 --gid 10001 --create-home agent

COPY alembic.ini ./
COPY migrations ./migrations
COPY knowledge ./knowledge
COPY mcp_servers ./mcp_servers
COPY evals ./evals
COPY scripts ./scripts

USER 10001:10001
EXPOSE 8000
HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=6 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).close()"
CMD ["python", "-m", "uvicorn", "enterprise_ai_ops_agent.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
