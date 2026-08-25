# League of Legends Global BanPick

An independently deployable League of Legends draft application. It provides an
administrator console, blue/red captain links, a spectator link, real-time draft
updates, optional Fearless (global) draft rules, and an optional bot-facing API.

## Run with Docker

1. Copy `.env.example` to `.env` and set strong values for both secrets.
2. Start the application:

   ```sh
   docker compose up --build -d
   ```

3. Open `http://localhost:8000/admin`, log in with the initial password, refresh
   the champion catalogue, and create a series.

For production, put Caddy or Nginx in front of the container, terminate HTTPS,
and set `BANPICK_PUBLIC_BASE_URL` to the public origin. Do not expose a database:
the application stores SQLite under the mounted `data` directory.

If the server cannot access Docker Hub, set `BANPICK_NODE_BASE_IMAGE` and
`BANPICK_PYTHON_BASE_IMAGE` in `.env` to a reachable registry proxy before
building. For example, the DaoCloud paths are
`m.daocloud.io/docker.io/library/node:22-alpine` and
`m.daocloud.io/docker.io/library/python:3.12-slim`.
If PyPI downloads stall, set `BANPICK_PIP_INDEX_URL` to a reachable Python
package index, such as `https://mirrors.aliyun.com/pypi/simple/`.

## Operations

- `BANPICK_CHAMPION_REFRESH_INTERVAL_SECONDS=0` disables scheduled refreshes.
  A positive value enables the specified interval; administrators can also change
  it from the console.
- The OP.GG MCP provider is tried first. The public-page scraper is a fallback.
  Failed refreshes never replace the latest successful catalogue.
- Captain and spectator URLs are bearer capabilities. Treat them like passwords.

## Optional bot integration

Any bot can use the `/api/internal/*` endpoints with the
`X-Banpick-Api-Key` header. This repository deliberately contains no QQ-specific
code. RenneBot can later be mounted as a submodule consumer and call those APIs
over its Docker network.

## Development

```sh
python -m pip install -r backend/requirements.txt
cd frontend && npm install && npm run build
python -m uvicorn app.main:app --app-dir backend --reload
```

Run backend tests with `pytest backend/tests`.
