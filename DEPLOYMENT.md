# Deployment & Production Setup

This short guide helps a developer or operator who clones the repo get the wrapper running in development and deploy it to production.

## Quickstart (developer)

- Clone the repo and create a virtualenv as described in `README.md`.
- Copy `.env.example` to `.env` and fill in the values (see the example file for required keys).
- Install dependencies: `pip install -r requirements.txt`.
- Start the server (development, auto-reload):

```bash
python main.py
# or with uvicorn directly for faster startup iterations
python -m uvicorn main:app --host 0.0.0.0 --port 9000 --reload
```

Visit `http://localhost:9000` for the chat UI and verify `/health` and `/.well-known/agent-card.json` are reachable.

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
Description=A2A Octane Wrapper
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
- Monitor logs for `Periodic discovery` messages and MCP errors to ensure connectivity to Octane.


If you'd like, I can also add a sample `docker-compose.yml` and a `systemd` unit file to the repo — tell me which one you prefer and I'll add it.
