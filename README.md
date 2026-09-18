# kanban-worker — API

The HTTP API for the macOS work console. One of two deployable services:

| repo | service | Dokploy |
|---|---|---|
| **kanban-worker** (this one) | API | domain → port 8080 |
| [kanban-agent](https://github.com/cleriko/kanban-agent) | transcription + analysis | **no domain** |

Both run against the same Postgres and the same object-storage volume. They never
talk to each other directly — the API writes a job row, the agent claims it.

## Deploy on Dokploy

Create → Application → this repo, Build Type **Dockerfile**. No Build Stage
needed; this repo builds the API and nothing else.

- Domain → port **8080**
- Volume: `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`

Environment:

```env
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@PG_HOST:5432/DBNAME
WC_API_TOKEN=openssl rand -hex 24
WC_GEMINI_API_KEY=your-google-ai-studio-key
WC_LLM_PROVIDER=gemini
WC_GEMINI_MODEL=gemini-2.5-flash
WC_STORAGE_BACKEND=local
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=true
WC_LOG_LEVEL=info
```

`WC_RUN_MIGRATIONS=true` here and `false` on the agent, so they do not race to
migrate. `WC_DATABASE_URL` must use `postgresql+asyncpg://`, and `PG_HOST` is
Postgres's **internal** Dokploy host — never `localhost`.

Paste into **Environment**, not Build-time variables, then **Redeploy**. Saving
alone does not restart the container.

## Verifying

```
curl https://your-domain/health
```

`200` with `"database": true`. A `503` carries a `detail` field saying why. The
startup log lists which `WC_*` variables actually reached the container.

## API

Everything under `/api/v1`, bearer-authenticated except `/health`.

| | |
|---|---|
| `GET /health` | open, 503 when Postgres is unreachable |
| `GET/POST /tasks`, `GET/PATCH/DELETE /tasks/{id}` | task CRUD |
| `POST /tasks/{id}/move`, `/complete`, `/reopen` | transitions |
| `GET /summary` | counts + agenda |
| `GET/POST /meetings`, `GET/DELETE /meetings/{id}` | meeting records |
| `PUT /meetings/{id}/audio` | streams the recording into object storage |
| `POST /meetings/{id}/process` | queues a job for the agent, returns immediately |
| `GET /meetings/{id}/transcript`, `/action-items` | results |
| `GET /jobs/{id}`, `GET /jobs/{id}/events` | polling, and SSE progress |
| `POST /agent/chat` | one agent turn |
| `POST /agent/actions/{id}/execute` | runs one confirmed operation |

Docs at `/api/docs`.

## How the agent is kept on a leash

A read tool runs immediately. A **write** does not: it is validated, summarised,
and stored in `agent_actions` with a snapshot of the row it would change. The
client shows it and the user presses EXECUTE, which is the only path by which an
agent write reaches the database.

The task id is resolved **when the action is proposed**, so you confirm the task
you were shown even if a better title match appears in between. `undo_state`
already holds the pre-change row, so undo is a feature to add, not a migration.
