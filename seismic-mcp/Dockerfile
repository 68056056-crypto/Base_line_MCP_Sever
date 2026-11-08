FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    MCP_TRANSPORT=streamable-http \
    HOST=0.0.0.0 \
    PORT=8000 \
    MCP_PATH=/mcp \
    SEISMIC_TZ=Asia/Bangkok

WORKDIR /app

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY seismic_mcp_server.py .

# Run as non-root
RUN useradd --create-home --uid 10001 mcp
USER mcp

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/health', timeout=4)" || exit 1

CMD ["python", "seismic_mcp_server.py"]
