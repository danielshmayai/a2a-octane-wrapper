# Deployment & Production Setup

This short guide helps a developer or operator who clones the repo get the wrapper running in development and deploy it to production.

## Quickstart (developer) — step-by-step

This section walks you through a minimal local development workflow so you and other contributors can start working immediately.

1. Clone the repository and change into the project directory:

```bash
git clone <repo-url>
cd a2a-octane-wrapper
```

2. Create and activate a Python virtual environment (recommended):

Windows (PowerShell):
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

macOS / Linux:
```bash
python -m venv .venv
source .venv/bin/activate
```

3. Install dependencies:

```bash
pip install -r requirements.txt
```

4. Create your local configuration file from the template and edit values:

```bash
copy env.example .env         # Windows (PowerShell)
# or
cp env.example .env           # macOS / Linux
```

Open `.env` in your editor and set at minimum:
- `OCTANE_BASE_URL` — the Opentext SDP MCP server base URL (e.g. `http://localhost:8080`)
- `API_KEY` — Opentext SDP bearer token
- `DEFAULT_SHARED_SPACE_ID` and `DEFAULT_WORKSPACE_ID`

Optional (to enable Gemini agent):
- `GEMINI_API_KEY` — set this if you want LLM-powered responses
Note: the project now uses the official `mcp` Python SDK (streamable HTTP transport)
and the `google-adk` (Agent Development Kit). Ensure your environment can install
these packages (they're listed in `requirements.txt`) and that outbound HTTPS
access to Google's Generative API is allowed if `GEMINI_API_KEY` is set.

5. Run the server locally (development mode with auto-reload):

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 9000 --reload
```

Or run without reload (production-like):

```bash
python -m uvicorn main:app --host 0.0.0.0 --port 9000
```

6. Verify the service is healthy and discoverable:

```bash
curl http://localhost:9000/health
curl http://localhost:9000/.well-known/agent-card.json
```

Note: the AgentCard now advertises local-only helper skills (for example
`tell_joke`) and exposes an OAuth2 flow named `adm_oauth` (authorization code
with PKCE + client credentials token URL). Use the JSON returned above when
registering the agent with Gemini Enterprise.

Configuration reminders for A2A auth:

- `AGENT_URL` should be your agent's public base URL (no path). The AgentCard
	`url` field is built from this value and is included in the JSON you paste
	into the Gemini Enterprise console.
- Set `OAUTH2_AUTH_URL` and `OAUTH2_TOKEN_URL` in `.env` to point at your
	identity provider (OTDS, Keycloak, etc.). These values are embedded into the
	AgentCard so clients know where to perform Authorization Code and token
	exchange flows.

Example `.env` snippet:

```env
AGENT_URL=https://your-public-host.example.com
OAUTH2_AUTH_URL=https://otdsauth.dev.ca.opentext.com/oauth2/auth
OAUTH2_TOKEN_URL=https://otdsauth.dev.ca.opentext.com/oauth2/token
```

7. Try the built-in chat UI in a browser:

Open http://localhost:9000 and ask e.g. `Get defect 1314`.

8. If you need to change the Opentext SDP server or API key at runtime (without restarting), use the runtime config endpoint from the UI `⚙` or call it directly:

```bash
curl -X POST http://localhost:9000/config \
	-H "Content-Type: application/json" \
	-d '{"octane_url":"http://localhost:8080","api_key":"NEW_TOKEN"}'
```

9. Inspect logs and troubleshooting tips:

- Watch the terminal where `uvicorn` runs for startup messages and discovery logs.
- If the wrapper cannot contact Opentext SDP, you will see `Could not auto-discover MCP tools` warnings — check `OCTANE_BASE_URL` and network reachability.
- If the Gemini agent fails to initialise, the app will fall back to the keyword router; check that `GEMINI_API_KEY` is set and valid if you expect LLM behaviour.

That’s it — you should now be able to develop and iterate locally. When you are ready to containerise or deploy, follow the Docker or systemd sections below.

## Docker (recommended for reproducible deploys)

You can run the wrapper in a container. Here is a minimal `Dockerfile` you can use in the project root:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt
COPY . /app
ENV A2A_HOST=0.0.0.0 A2A_PORT=9000
EXPOSE 9000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "9000"]
```

Build and run:

```bash
docker build -t a2a-octane-wrapper:latest .
docker run -e API_KEY=$API_KEY -e OCTANE_BASE_URL=$OCTANE_BASE_URL -p 9000:9000 a2a-octane-wrapper:latest
```

For production, run the container behind a reverse proxy (nginx) with TLS or deploy to a managed service (Cloud Run, ECS, Kubernetes).

## Systemd (Linux VM)

Example `systemd` unit for production (place in `/etc/systemd/system/a2a-wrapper.service`):

```ini
[Unit]
Description=A2A Opentext SDP Wrapper
After=network.target

[Service]
User=www-data
Group=www-data
WorkingDirectory=/opt/a2a-octane-wrapper
EnvironmentFile=/opt/a2a-octane-wrapper/.env
ExecStart=/opt/a2a-octane-wrapper/.venv/bin/uvicorn main:app --host 0.0.0.0 --port 9000
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Reload systemd and start:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now a2a-wrapper.service
sudo journalctl -u a2a-wrapper -f
```

## Windows (developer/ops)

- Use the included `restart.ps1` to run the server in a background process during development.
- For a service on Windows use `nssm` or Windows Service wrappers to run the Python process persistently; set environment variables in the system service configuration.

## Expose the app securely (TLS)

- Gemini Enterprise requires a publicly reachable HTTPS URL with a valid CA-signed certificate.
- For quick dev tunnels use `ngrok` or `cloudflared` as described in `README.md`.
- For production use managed TLS (Cloud Run, Google Load Balancer + Let's Encrypt, or nginx + certbot).

## Secrets & configuration

- Keep secret keys out of version control. Do NOT commit `.env` to git.
- Store production secrets in a secrets manager (GCP Secret Manager, AWS Secrets Manager, or environment variables injected by your orchestrator).
- The wrapper reads configuration from environment variables (see Section 4.3 in `README.md`). After editing `.env`, restart the service to pick up changes.

## Health checks and monitoring

- Use `/health` for liveness probes. Example Kubernetes readiness/liveness probe: `httpGet: path: /health port: 9000`.
- Monitor logs for `Periodic discovery` messages and MCP errors to ensure connectivity to Opentext SDP.


If you'd like, I can also add a sample `docker-compose.yml` and a `systemd` unit file to the repo — tell me which one you prefer and I'll add it.
