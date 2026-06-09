# Crypto Market Data MCP Server — container image for hosted (HTTP) deployment.
# Used by MCPize / any container host. Runs with auth + rate limiting enabled.
FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only (no venv, no keys, no example files).
COPY server.py auth.py ratelimit.py ./

# Configure API keys at runtime, never bake them in:
#   docker run -e CRYPTO_MCP_API_KEYS="key:client:tier" -p 8000:8000 <image>
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py", "--http"]
