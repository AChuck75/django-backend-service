# Document API Backend

A local-development GenAI assistant with a Streamlit UI, authenticated Django REST Framework API, Claude answers, a branching LangGraph agent, SQLite-backed document chunks, and live weather lookup through Open-Meteo.

## Requirements

- Python 3.11 or newer
- Django 5.x
- Django REST Framework
- Streamlit, LangChain Core, LangGraph, and pypdf (installed from `requirements.txt`)

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

In a second terminal with the same virtual environment activated, run the UI:

```bash
streamlit run streamlit_app.py
```

The UI connects to `http://127.0.0.1:8000` by default. Set `DJANGO_API_BASE_URL` if the Django API is hosted elsewhere.

Run the automated test suite with:

```bash
python manage.py test
```

An initial migration is included. After changing models during development, create and apply a new migration with:

```bash
python manage.py makemigrations api
python manage.py migrate
```

## Environment configuration

The application has deliberately local-only defaults:

- `DJANGO_SECRET_KEY`: Django secret key. A clearly unsafe development fallback is used only when this is absent.
- `DJANGO_DEBUG`: `1`, `true`, `yes`, or `on` enables debug mode. It defaults to enabled for local work.
- `DJANGO_ALLOWED_HOSTS`: comma-separated allowed host names. Defaults include `localhost`, `127.0.0.1`, `[::1]`, and `testserver`.
- `DJANGO_DB_NAME`: optional path to the local SQLite database file.
- `TOKEN_SESSION_LIFETIME_SECONDS`: positive session lifetime in seconds. It defaults to `86400` (24 hours).
- `ANTHROPIC_API_KEY`: Claude API key. AI answer requests return `503` until this is configured.
- `ANTHROPIC_BASE_URL`: Anthropic API base URL; defaults to `https://api.anthropic.com`.
- `CLAUDE_MODEL`: Claude model name; defaults to `claude-sonnet-4-5-20250929`.
- `AI_PROVIDER_TIMEOUT_SECONDS`: positive provider timeout; defaults to `30`.
- `AI_MAX_RESPONSE_TOKENS`: positive maximum response size; defaults to `1024`.

For example:

```bash
export DJANGO_SECRET_KEY=replace-with-a-secret-from-a-secret-manager
export DJANGO_DEBUG=0
export DJANGO_ALLOWED_HOSTS=api.example.com
export TOKEN_SESSION_LIFETIME_SECONDS=86400
export ANTHROPIC_API_KEY=replace-with-your-claude-api-key
```

## Authentication

Register and login endpoints are public. Both return a user identity, a raw random token, and its expiration time. The raw token is returned only in those responses. The database stores only a SHA-256 digest of it.

```bash
curl -X POST http://127.0.0.1:8000/auth/register -H 'Content-Type: application/json' -d '{"username":"alice","password":"BrightComet!4729","email":"alice@example.com"}'
```

```bash
curl -X POST http://127.0.0.1:8000/auth/login -H 'Content-Type: application/json' -d '{"username":"alice","password":"BrightComet!4729"}'
```

Use the returned token on every document or query request:

```text
Authorization: Token <raw-token>
```

Missing, malformed, invalid, expired, and inactive-user tokens receive a standard `401` response. A user may have more than one active token session.

## API endpoints

Paths shown without a trailing slash also accept one trailing slash.

| Method | Path | Authentication | Purpose |
| --- | --- | --- | --- |
| POST | `/auth/register` | No | Create a user and token session. Accepts `username`, `password`, optional `email`. |
| POST | `/auth/login` | No | Validate credentials and create a token session. |
| POST | `/documents` | Yes | Create a document from JSON or a multipart text file. |
| GET | `/documents` | Yes | List only the caller's documents. |
| GET | `/documents/{id}` | Yes | Retrieve an owned document. Non-owned documents are hidden with `404`. |
| PUT | `/documents/{id}` | Yes | Replace an owned document's title and content. |
| PATCH | `/documents/{id}` | Yes | Partially update an owned document's title and/or content. |
| DELETE | `/documents/{id}` | Yes | Delete an owned document. Existing documents owned by another user return `403`. |
| POST | `/query` | Yes | Save one query-history entry. |
| GET | `/queries` | Yes | List only the caller's query history. |
| POST | `/api/ask-ai` | Yes | Generate an answer to a prompt using a fixed system prompt and save it to query history. |
| POST | `/api/rag-query` | Yes | Answer a query using retrieved, ingested document chunks and return those source chunks. |
| POST | `/api/ingest-doc` | Yes | Chunk an owned document, create embeddings, and store them for retrieval. |
| GET | `/api/query-history` | Yes | Paginated AI query history belonging to the caller. |
| POST | `/api/assistant-chat` | Yes | Run the branching LangGraph assistant with chat history, document retrieval, and weather tool routing. |

### AI document ingestion and retrieval

Answer generation uses the Anthropic Messages API with `ANTHROPIC_API_KEY` and `CLAUDE_MODEL`. No second API key is needed: chunk and query vectors are generated locally using deterministic signed feature hashing over words and adjacent word pairs (512 dimensions), then stored in SQLite. This is a lightweight lexical vectorizer, not a neural semantic embedding model, so retrieval may not match the quality of a dedicated embedding model. Retrieval searches all indexed chunks owned by the requester, including chunks from multiple documents.

The Streamlit chat stores visible turns in `st.session_state` and sends the previous turns with each request, providing session-scoped conversational context (it resets when the Streamlit session is cleared). The LangGraph agent first routes weather/forecast requests to the Open-Meteo tool and other requests to document retrieval; both branches then pass tool results and available history to Claude to generate the response. The weather tool geocodes the requested location and fetches current conditions or a next-day forecast when asked. Open-Meteo is a public service and does not require an additional API key.

Use the Streamlit sidebar to upload PDF or TXT documents. PDF text is extracted with pypdf; encrypted, malformed, or image-only PDFs without extractable text are rejected. The UI creates the document, calls ingestion, and displays the chunk count. Retrieved source chunks and document IDs are displayed directly below document-grounded answers in the chat.

Ingest an owned document by ID:

```bash
curl -X POST http://127.0.0.1:8000/api/ingest-doc -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"document_id":7}'
```

Documents are split on whitespace into chunks of at most **500 words**, with a **50-word overlap** between adjacent chunks. Locally generated feature-hash vectors and chunk text are persisted in the SQLite `DocumentChunk` table. Retrieval computes cosine similarity against the requester's stored vectors and returns up to five chunks; documents owned by other users are never searched. Re-ingestion replaces the existing chunks for that document. If the document has no text, ingestion succeeds with a chunk count of zero.

Ask a standalone prompt or a retrieval-augmented question:

```bash
curl -X POST http://127.0.0.1:8000/api/ask-ai -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"prompt":"Explain the key points of a project plan."}'
```

```bash
curl -X POST http://127.0.0.1:8000/api/rag-query -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"query":"What are the project milestones?"}'
```

The RAG response contains `answer` and `sources`; every source has `document_id` and `chunk_text`. Both AI endpoints save successful requests and answers to the same owner-isolated history returned by `GET /api/query-history?page=1&page_size=10`. The SQLite vector store is intended for this local-development backend; use a dedicated vector database for larger deployments.

### JSON document creation

```bash
curl -X POST http://127.0.0.1:8000/documents -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"title":"Roadmap","content":"Draft milestones."}'
```

The authenticated user is always the document owner. Client-supplied owner, identifier, and timestamp fields are read-only and cannot be changed.

### File document creation

```bash
curl -X POST http://127.0.0.1:8000/documents -H 'Authorization: Token <raw-token>' -F 'file=@notes.md;type=text/markdown'
```

Only UTF-8 `.txt` and `.md` text files and extractable-text `.pdf` files are accepted. The file must be at most 5 MiB. The server checks the extension and declared MIME type, strictly decodes text files as UTF-8, and rejects embedded NUL bytes in text files. Accepted MIME types are:

- `.txt`: `text/plain`
- `.md`: `text/plain`, `text/markdown`, `text/x-markdown`, or `application/markdown`
- `.pdf`: `application/pdf`

The filename stem becomes the document title. Original uploaded file bytes are not stored; only decoded content is saved in `Document.content`.

### Pagination

Document and query lists use DRF-style pagination. `page` defaults to `1`; `page_size` defaults to `10`; page sizes must be between `1` and `100`. Invalid bounds return `400`.

```text
GET /documents?page=1&page_size=10
```

A list response has this shape:

```json
{
  "count": 23,
  "next": "http://127.0.0.1:8000/documents?page=2&page_size=10",
  "previous": null,
  "results": [
    {
      "id": 7,
      "title": "Roadmap",
      "content": "Draft milestones.",
      "uploaded_at": "2025-01-01T12:00:00Z",
      "owner": 1
    }
  ]
}
```

### Query history

```bash
curl -X POST http://127.0.0.1:8000/query -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"query_text":"Summarize the roadmap"}'
```

There is intentionally no query-processing engine in this assignment. Every created query log receives this neutral placeholder value:

```text
Query accepted; processing is not configured.
```

Integrate a real processor later by replacing that behavior while keeping query ownership and history isolation intact.

## Production hardening

This project is not deployment-ready. Before production use, at minimum:

- set a unique secret through a secret manager, disable debug mode, and set exact allowed hosts;
- replace SQLite with an appropriately managed production database, backups, and migration workflow;
- terminate and enforce HTTPS correctly, including trusted proxy and security-header configuration;
- add login/token rate limiting, monitoring, audit logging, and a CORS policy appropriate to real clients;
- add malware scanning, content validation, quota controls, and suitable private object storage if upload handling is extended beyond the current text parsing behavior;
- review token revocation, session cleanup, incident response, and data-retention requirements.
