FROM python:3.12-slim
WORKDIR /app

# Install Node.js 20 LTS + Claude CLI
# The claude binary at /usr/local/bin/claude is used by CustomAnthropicClient
# when CLAUDE_CLI_PATH=/usr/local/bin/claude is set in the environment.
# Credentials are mounted from the host at /root/.claude (see docker-compose.yml).
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl gnupg && \
    mkdir -p /etc/apt/keyrings && \
    curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
      | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg && \
    echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
      > /etc/apt/sources.list.d/nodesource.list && \
    apt-get update && apt-get install -y --no-install-recommends nodejs && \
    npm install -g @anthropic-ai/claude-code && \
    apt-get purge -y --auto-remove curl gnupg && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-u", "orchestrator.py"]
