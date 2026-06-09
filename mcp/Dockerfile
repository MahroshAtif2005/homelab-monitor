# HomeLab Monitor — MCP server sidecar.
#
# Build context is the repo ROOT (so we can bundle CHANGELOG.md):
#   docker build -f mcp/Dockerfile -t sikamikaniko123/homelab-monitor-mcp:latest .
#
# Defaults to stdio so `docker run -i --rm ... image` works directly with
# `claude mcp add`. The docker-compose service flips it to streamable-http.
FROM python:3.12-slim
WORKDIR /app

COPY mcp/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY mcp/homelab_client.py /app/homelab_client.py
COPY mcp/server.py         /app/server.py
# Bundled so the homelab://changelog resource can give the agent version context.
COPY CHANGELOG.md          /app/CHANGELOG.md

# Sensible defaults; override at run time.
ENV HOMELAB_MONITOR_URL=http://localhost:9800 \
    HOMELAB_HTTP_TIMEOUT=10 \
    MCP_HOST=0.0.0.0 \
    MCP_PORT=9810

EXPOSE 9810
CMD ["python", "server.py"]
