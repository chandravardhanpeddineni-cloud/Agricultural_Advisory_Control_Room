# Architecture and provider integration

## Request flow

The React client sends authenticated requests to FastAPI. FastAPI verifies an expiring JWT and checks revocation and account status against MongoDB. Each advisory is scoped by both session ID and owner ID. Session, blackboard, and agent execution collections retain the input, timestamps, findings, status transitions, errors, and final result.

The Microsoft Agent Framework workflow begins with intake, fetches weather, then fans out crop, market, and scheme work. Crop findings feed risk assessment. Risk, market, and scheme executors join at the advisor coordinator. Each executor publishes its finding to a shared persistent blackboard. The final advisory retains source references and availability limitations. High/critical risk alerts are also persisted to the risk collection.

## API routes

- `POST /api/auth/register`, `/login`, `/logout`; `GET /api/auth/me`
- `GET/PUT /api/farmer/profile`
- `POST /api/advisory/create`; authenticated history, session, status, agents, blackboard, timeline, and PDF routes under `/api/advisory`
- `GET/POST /api/knowledge` and `DELETE /api/knowledge/{document_id}` (administrator only)
- `GET /api/health`

## Normalized provider contracts

Weather adapter sends `{ "location", "crop", "stage" }` as JSON by POST. It expects `summary` (required), optional numeric `temperature_c`, `humidity_percent` (0–100), `rainfall_next_48h_mm`, `precipitation_probability_percent` (0–100), `source`, and ISO-8601 `retrieved_at`.

Market adapter sends `{ "location", "crop" }` as JSON by POST. It expects `market`, non-negative `current_price`, `unit`, `source`, optional `retrieved_at`, and optional `history`: date/price records. Trend is calculated only when at least two history records exist. API key is sent as a bearer token when configured.

Both adapters have bounded connect and request timeouts. A provider timeout, invalid response, or unset URL is represented as unavailable/failed data; no synthetic live values are introduced. Adapter URLs should be trusted server configuration, since requests originate from the API server.

## Language model

Set `LLM_PROVIDER` to `openai`, `openai_compatible`, `azure_openai`, or `groq`; set the server-side `LLM_API_KEY` and model ID. OpenAI-compatible endpoints need `LLM_BASE_URL`; Groq defaults to `https://api.groq.com/openai/v1` if it is omitted; Azure needs its endpoint and may set an API version. Each specialist is a Microsoft Agent Framework `Agent`; structured responses are validated with Pydantic. Model output is a proposed synthesis, not verified live data. The advisory coordinator receives only the data published by the specialist agents.

## Knowledge base

Administrators may upload text PDFs or UTF-8 `.txt` reference material with title, issuer, and optional official URL. Documents are size-limited, extracted, chunked with page metadata, and embedded in ChromaDB. Original uploaded bytes are removed after indexing. Scheme-agent answers are restricted to retrieved passages, preserve source snippets, and mark eligibility as requiring verification. Scanned PDFs without extractable text need OCR before indexing. Treat uploaded passages as untrusted reference content; never follow instructions embedded in source material.

## Security and operational boundaries

Passwords use Argon2; JWT signing secret is required to be strong in production; token revocation uses MongoDB TTL; admin operations are role-checked; session reads are owner-scoped; upload MIME/signature, extension, byte limit, and text encoding are checked. Provider calls remain server-side. Production must supply TLS, trusted CORS origins, managed secret storage, MongoDB and Chroma access controls, backups, monitoring, data retention, and an external durable job queue before horizontal scaling. PDF exports are generated from plain text and are not a substitute for local agronomic review.

## Run and validate

See the root README for local commands. Environment validation rejects production startup without a strong key and MongoDB URI and requires server-backed Chroma in production. The live weather/market/LLM integrations require the user's chosen services and credentials; they are not bundled with demo data.
