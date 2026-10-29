# Document API Backend

A local-development Django REST Framework backend for user-owned text documents and persistent query history. It uses SQLite by default, Django's password hashing, and an expiring custom token scheme.

## Requirements

- Python 3.11 or newer
- Django 5.x
- Django REST Framework

## Local setup

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python manage.py migrate
python manage.py runserver
```

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

For example:

```bash
export DJANGO_SECRET_KEY=replace-with-a-secret-from-a-secret-manager
export DJANGO_DEBUG=0
export DJANGO_ALLOWED_HOSTS=api.example.com
export TOKEN_SESSION_LIFETIME_SECONDS=86400
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

### JSON document creation

```bash
curl -X POST http://127.0.0.1:8000/documents -H 'Authorization: Token <raw-token>' -H 'Content-Type: application/json' -d '{"title":"Roadmap","content":"Draft milestones."}'
```

The authenticated user is always the document owner. Client-supplied owner, identifier, and timestamp fields are read-only and cannot be changed.

### File document creation

```bash
curl -X POST http://127.0.0.1:8000/documents -H 'Authorization: Token <raw-token>' -F 'file=@notes.md;type=text/markdown'
```

Only UTF-8 `.txt` and `.md` files are accepted. The file must be at most 5 MiB. The server checks the extension, checks a declared text MIME type, strictly decodes UTF-8, and rejects embedded NUL bytes. Accepted MIME types are:

- `.txt`: `text/plain`
- `.md`: `text/plain`, `text/markdown`, `text/x-markdown`, or `application/markdown`

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
