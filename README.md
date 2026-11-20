# Fieldwise Agricultural Advisory Control Room

Full-stack advisory workspace built with React, Vite, FastAPI, MongoDB, ChromaDB, and Microsoft Agent Framework. It coordinates farmer intake, weather, crop, market, scheme retrieval, risk assessment, and advisory synthesis through a persistent MongoDB blackboard.

## What is included

- Farmer registration, login, private farm profile, advisory history, session status and agent timeline.
- Parallel specialist-agent workflow with persisted findings, execution states, and final advisory.
- Weather provider adapter plus a bundled market snapshot at `backend/app/data/AgriAI.csv`. Market advice uses the dated CSV records locally and does not call `MARKET_API_URL`; it reports a single-day snapshot without inventing a price trend.
- PDF/TXT knowledge document indexing in ChromaDB. Scheme answers cite retrieved material and must be verified by the farmer.
- Admin-only knowledge source management and an optional first-admin bootstrap.
- PDF advisory export, responsive control-room interface, startup configuration checks, and deployment notes.

## Requirements

- Node.js 20+ and npm
- Python 3.11–3.13 (Python 3.13 recommended)
- MongoDB 7+ (local or managed)
- ChromaDB: local persistent storage for development; a reachable Chroma server for production

## Local setup

1. Copy `backend/.env.example` to `backend/.env`. Set a random `SECRET_KEY` of at least 32 characters and a reachable `MONGODB_URI`. Keep API credentials on the server only.
2. Install the frontend and Python dependencies:

   ```sh
   npm install
   npm install --prefix client
   cd backend
   python3.13 -m venv .venv
   .venv/bin/pip install -e '.[dev]'
   cd ..
   ```

3. Start MongoDB, then run both services:

   ```sh
   npm run dev
   ```

   The frontend is at `http://localhost:5173`; FastAPI and interactive API docs are at `http://127.0.0.1:8000` and `/docs`.

4. To make a knowledge-base administrator, set both `BOOTSTRAP_ADMIN_EMAIL` and `BOOTSTRAP_ADMIN_PASSWORD` (16+ characters) before first startup. Registration always creates farmer accounts.

## Provider configuration

See [backend/.env.example](backend/.env.example) and [docs/architecture.md](docs/architecture.md) for accepted configuration and provider contracts. No credentials are required to start the application beyond MongoDB and the session secret. Without an LLM, specialist calls which need synthesis are marked unavailable/failed and the final response records limitations. Without weather, market, or indexed official material, those sections explicitly say they are unavailable.

The normalized weather and market adapters use server-side JSON POST endpoints. Do not paste live API keys into the browser or commit a `.env` file. Configure production CORS origins explicitly and use HTTPS, a secret manager, managed MongoDB backups, and a persistent Chroma deployment.

## Production deployment

- Set `APP_ENV=production`, a unique `SECRET_KEY`, `MONGODB_URI`, `CHROMA_SERVER_HOST` and port, `CORS_ORIGINS`, provider endpoints, and server-side credentials in the deployment secret store.
- Run `uvicorn app.main:app --host 0.0.0.0 --port 8000` from `backend/` behind a TLS-terminating reverse proxy. Build the frontend with `npm run build --prefix client` and serve `client/dist` on the same origin or configure its API base and CORS.
- Keep MongoDB and Chroma private to the application network. Use encrypted volumes, database credentials with least privilege, backups, monitoring, and explicit retention policy before onboarding real farmer data.
- Run one API process per deployment initially. BackgroundTasks keep the workflow simple; for durable work across process restarts or horizontal scaling, replace the in-process task runner with a persistent queue/worker before enabling those deployment patterns.
- This repository is an implementation and deployment package, not a deployed service. A production deployment still needs your chosen weather, market, and LLM provider credentials, a MongoDB/Chroma environment, and operational secrets.

## Checks

```sh
npm run build
cd backend && .venv/bin/pytest
```

API documentation: `/docs`. Health check: `/api/health`.
# Agricultural_Advisory_Control_Room
