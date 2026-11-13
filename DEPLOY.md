# Deploying the API on Dokploy

This repo is one half of the deployable split: it builds the HTTP API and nothing
else. Transcription and analysis live in
[kanban-agent](https://github.com/cleriko/kanban-agent), which runs against the
same Postgres and the same object-storage volume. The combined reference
implementation is `kanban-backend`.

## The two services

| # | Dokploy type | Build | Domain |
|---|---|---|---|
| 1 | Database → PostgreSQL | — | none |
| 2 | Application → this repo | Dockerfile | yes, → port **8080** |

If you already run the API from another repo, skip to the agent — do not create a
second Postgres.

---

## 1. Postgres

Dokploy → Create → **Database** → PostgreSQL. Any user/password/database name.

When it is running, open it and copy the **internal** host — something like
`workconsole-db-a1b2c3`. That is what the API and the agent connect to. It is
*not* `localhost`, and it is not the external host.

If you launch a Compose stack that creates Postgres instead,
`POSTGRES_PASSWORD` is required. There is no built-in default.

## 2. API

Create → **Application** → this repo, branch `main`.

- Build Type: **Dockerfile**. No Build Stage — the Dockerfile is single-stage and
  builds the API only.
- Domain: your hostname → port **8080**
- Volume: `/var/lib/workconsole/objects` → `/var/lib/workconsole/objects`

Environment:

```env
WC_DATABASE_URL=postgresql+asyncpg://USER:PASSWORD@PG_HOST:5432/DBNAME
WC_API_TOKEN=generate-with-openssl-rand-hex-24
WC_GEMINI_API_KEY=your-google-ai-studio-key
WC_LLM_PROVIDER=gemini
WC_GEMINI_MODEL=gemini-2.5-flash
WC_STORAGE_BACKEND=local
WC_STORAGE_PATH=/var/lib/workconsole/objects
WC_RUN_MIGRATIONS=true
WC_LOG_LEVEL=info
```

`WC_API_TOKEN` is required. With it unset, every authenticated route returns
**503** — auth fails closed rather than serving requests. A wrong token returns
**401**. `WC_ALLOW_UNAUTHENTICATED=1` is an explicit opt-out for a private tunnel
or local tests only; never set it on a host with a public domain.

Audio uploads (`PUT /meetings/{id}/audio`) must be a recognised container —
m4a/mp4, wav, mp3, ogg or webm — with matching leading bytes. Anything else is
**400**; the cap is 512 MB.

## 3. Agent

Create a second Dokploy Application from
[kanban-agent](https://github.com/cleriko/kanban-agent). It serves no HTTP, so it
gets **no domain and no port**. Mount the same object-storage host path, plus a
volume for Whisper weights. Full walkthrough in that repo's `DEPLOY.md`.

Both services need `WC_GEMINI_API_KEY`: the API runs the agent, the worker runs
meeting analysis. Only the API sets `WC_RUN_MIGRATIONS=true`.

---

## "None of my environment variables are set"

If the startup log shows `db=localhost:5432` **and** `WC_API_TOKEN is empty`
**and** `WC_GEMINI_API_KEY is empty` all at once, none of them are reaching the
container. That is one problem, not three. The log says so directly:

```
ERROR  No WC_* environment variables are set in this container.
```

In order of likelihood:

1. **You saved the environment but did not redeploy.** Dokploy bakes the
   environment in at deploy time; editing it does nothing to a running container.
   Hit **Redeploy** after saving.
2. **It went into the wrong box.** Dokploy has a separate *Build-time variables*
   section. These are runtime settings — they belong in **Environment**.
3. **It is on the wrong service.** Each Application has its own environment. The
   API and the worker each need their own copy.
4. **Blank lines or quotes.** Use `WC_API_TOKEN=abc`, not `WC_API_TOKEN="abc"`
   and not `export WC_API_TOKEN=abc`.

To confirm from inside, open the service's terminal in Dokploy:

```
env | grep WC_
```

Nothing printed means the environment never arrived, and no amount of changing
the values will help until that is fixed.

## The three things that break this

**1. The driver prefix.** Dokploy hands you `postgresql://…`. It must be
`postgresql+asyncpg://…` — the driver is async and a plain URL fails at startup.

**2. The host.** `WC_DATABASE_URL` must point at Postgres's *internal* Dokploy
host. If the startup log says `db=localhost:5432`, the variable is not set and
the API is talking to itself:

```
work console API 1.0.0 up · db=localhost:5432 · ...
ERROR  WC_DATABASE_URL is not set. Falling back to localhost ...
GET /health 503 Service Unavailable
```

**3. The shared volume.** The API writes the uploaded audio; the agent reads it.
If they are not on the same host path, every job fails with a missing file. (To
avoid sharing a filesystem, use `WC_STORAGE_BACKEND=s3` instead.)

---

## Checking it worked

```
curl https://your-domain/health
```

`200` with `"database": true`. A `503` carries a `detail` field saying why. The
`/health` route is unauthenticated on purpose, so it can be probed without a key.

The API log should read `db=<your-pg-host>:5432`.

Then point the Mac app at it: Settings → VPS → Server URL `https://your-domain`,
API key = your `WC_API_TOKEN`.

---

## Footprint

| | |
|---|---|
| postgres | ~256 MB |
| api | ~256 MB |
| agent | ~600 MB while transcribing, near zero idle |

The agent is a separate container, so the API image carries no ffmpeg and no
Whisper weights. Total is around 1 GB across the stack; a 2 GB VPS is fine.

### Where your data goes

- **Meeting audio** — never leaves the VPS. The agent transcribes it locally with
  Whisper, and the file is deleted once results are stored.
- **Transcripts, task titles, meeting notes** — sent to Google as part of analysis
  and agent requests.

If that split is not acceptable, set `WC_LLM_PROVIDER=ollama` and point
`WC_OLLAMA_URL` at an Ollama service. Model choices and sizes are documented in
the agent repo's `DEPLOY.md`.

### Transcribing with Gemini too

Removes ffmpeg and the Whisper weights from the agent, ~512 MB total for the
stack, but uploads the meeting audio to Google:

```
WC_TRANSCRIPTION_PROVIDER=gemini
```

---

## Troubleshooting

**`db=localhost:5432` in the startup log** — `WC_DATABASE_URL` is not set.

**`503` on `/health`** — read the `detail` field. Usually Postgres is still
starting, or the URL is missing `+asyncpg`.

**Health is fine but meetings never finish processing** — look at the agent. It
is a separate service and never appears in the API's logs. The usual causes are
the model not being pulled, or the object-storage volume not being shared.

**`WC_GEMINI_API_KEY is empty`** — the API logs this at startup; every analysis
and agent request will fail until it is set.

**Agent logs `FileNotFoundError` on an audio key** — the API and the agent are not
sharing `/var/lib/workconsole/objects`.

**The app shows OFFLINE but curl works** — the API key does not match
`WC_API_TOKEN` (401), or `WC_API_TOKEN` is unset on the server (503). `/health` is
unauthenticated, so curl succeeds while everything else is refused.
