# Crypto Market Data MCP Server — container image for hosted (HTTP) deployment.
# Used by MCPize / any container host. Designed to run BEHIND a gateway that
# handles auth + billing, so the server trusts the gateway (auth/rate-limit off).
# If you expose this image directly without a gateway, override with
#   -e CRYPTO_MCP_DISABLE_AUTH=0  and provide CRYPTO_MCP_API_KEYS.
FROM python:3.12-slim

WORKDIR /app

# Install deps first for better layer caching.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# App code only (no venv, no keys, no example files).
COPY server.py auth.py ratelimit.py ./

# Trust the upstream gateway (MCPize) for auth + rate limiting by default.
ENV CRYPTO_MCP_DISABLE_AUTH=1
ENV PORT=8000
EXPOSE 8000

CMD ["python", "server.py", "--http"]
