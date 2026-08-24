# Nova — Parkline Dental Voice Agent

Nova is an inbound patient-facing voice agent for a fictional dental practice
(Parkline Dental, Harris Park NSW), built on [LiveKit Agents](https://docs.livekit.io/agents/)
v1.x. Callers talk to Nova in real time over WebRTC; Nova triages emergencies,
books routine appointments, takes messages for anything out of scope, and hangs
up on its own once a request is confirmed.

## What it does

- **Urgent path** — pain, swelling, bleeding, or a knocked-out/broken tooth
  breaks out of the routine flow immediately, checks for a life-threatening
  emergency (→ call 000), and books one of the two slots held daily for
  emergencies (11:30 and 15:30).
- **Routine path** — collects and reads back name, callback mobile, service,
  preferred day/time, and new-vs-existing patient. Sundays, out-of-hours times,
  and non-emergency bookings into the two reserved slots are rejected.
- **Escalation** — clinical advice, fees, health funds, HICAPS, Medicare, and
  complaints are never guessed at; Nova takes a message instead, and refuses to
  save one without a name and callback number.
- **Call termination** — after any confirmed booking or message (or if the
  caller loops on the same question four times) Nova says goodbye and deletes
  the LiveKit room, tearing down the session and room together.

Bookings and messages are appended as JSON records to `bookings.json` in
`DATA_DIR`.

## Layout

| File | Purpose |
| --- | --- |
| [agent.py](agent.py) | The agent: system prompt, `AgentSession` wiring, and the `create_booking` / `book_emergency` / `take_message` / `end_call` tools. |
| [server.py](server.py) | Deployment entrypoint. Runs the LiveKit worker and an aiohttp app (health check + `/api/token`) in one process and one event loop. |
| [examples/client.html](examples/client.html) | Minimal browser test client — fetches a token, joins the room, attaches Nova's audio. |
| [render.yaml](render.yaml) | Render Web Service blueprint, including the mounted disk for `bookings.json`. |

Speech and language run through LiveKit Inference, so no separate provider keys
are needed: Deepgram `nova-3` for STT, `openai/gpt-4o-mini` for the LLM, and
Deepgram `aura-2` (voice `thalia`) for TTS.

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # macOS/Linux: source venv/bin/activate
pip install -r requirements.txt
```

Create a `.env` file (it is gitignored):

```env
LIVEKIT_URL=wss://your-project.livekit.cloud
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
```

## Running locally

Talk to Nova straight from the terminal, no browser or LiveKit room needed:

```bash
python agent.py console
```

Or register a dev worker against your LiveKit project:

```bash
python agent.py dev
```

To exercise the full browser path, run the deployment entrypoint and open the
example client:

```bash
python server.py          # token API on :8080, worker health on :8081
```

Then serve `examples/client.html` (e.g. `python -m http.server` from
`examples/`) and click **Call Nova**. The client points at
`http://localhost:8080` by default — edit the `API` constant to target a
deployed service.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `LIVEKIT_URL` | — | Required. |
| `LIVEKIT_API_KEY` | — | Required. |
| `LIVEKIT_API_SECRET` | — | Required. Never exposed to the frontend; the browser only ever receives a short-lived access token. |
| `DATA_DIR` | project directory | Where `bookings.json` lives. On Render, point this at a mounted disk — container filesystems are wiped on every deploy. |
| `PORT` | `8080` | Public listener (health check + token endpoint). |
| `WORKER_HEALTH_PORT` | `8081` | Worker's own health port, bound to loopback. |
| `ALLOWED_ORIGINS` | `*` | Comma-separated frontend origins for CORS. |
| `TOKEN_API_KEY` | unset | When set, `/api/token` requires a matching `X-API-Key` header. |
| `TOKEN_TTL_MINUTES` | `15` | Access-token lifetime. |
| `LOG_LEVEL` | `INFO` | |

## HTTP API

- `GET /` and `GET /healthz` — liveness, returns `OK`.
- `POST /api/token` — mints a LiveKit access token. Body fields `room`,
  `identity`, and `name` are all optional; omitting `room` mints a fresh room
  per caller, so two callers are never dropped into the same one. Responds with
  `{ token, serverUrl, room, identity }`.

## Deploying to Render

`render.yaml` is a ready blueprint. It runs `python server.py` on a **starter**
plan (free instances spin down and drop the LiveKit worker registration),
health-checks `/healthz`, and mounts a 1 GB disk at `/var/data` with
`DATA_DIR` pointed at it. Set `LIVEKIT_URL`, `LIVEKIT_API_KEY`,
`LIVEKIT_API_SECRET`, and — recommended before going public — `ALLOWED_ORIGINS`
and `TOKEN_API_KEY` in the dashboard.
