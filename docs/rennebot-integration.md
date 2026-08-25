# RenneBot optional integration

The BanPick service remains fully usable without RenneBot. To integrate it after
this repository's first commit is pushed, add it to the deployment repository:

```sh
git submodule add git@github.com:AeolianRenne/LeagueOfLegendsGlobalBanPickWebsite.git lol-banpick
```

Add the service to the deployment Compose file on the same Docker network as
AstrBot. Do not mount the BanPick SQLite database into the plugin container.

```yaml
services:
  banpick:
    build: ./lol-banpick
    env_file:
      - ${BOT_ENV_FILE:-.env.rennebot}
    environment:
      BANPICK_DATA_DIR: /data
      BANPICK_PUBLIC_BASE_URL: ${BANPICK_PUBLIC_BASE_URL}
      BANPICK_ADMIN_INITIAL_PASSWORD: ${BANPICK_ADMIN_INITIAL_PASSWORD}
      BANPICK_BOT_API_KEY: ${BANPICK_BOT_API_KEY}
    volumes:
      - ${RUNTIME_DIR:-./runtime}/banpick-data:/data
```

Configure the plugin with `BANPICK_INTERNAL_URL=http://banpick:8000` and the
same `BANPICK_BOT_API_KEY`. It calls only these stable endpoints:

| Command behavior | Endpoint |
| --- | --- |
| Create | `POST /api/internal/series` |
| Status | `GET /api/internal/series/{code}` |
| Next game | `POST /api/internal/series/{code}/next` |
| End | `POST /api/internal/series/{code}/end` |
| Admin refresh | `POST /api/internal/sync` |

Pass the shared key in `X-Banpick-Api-Key`. Creating a series returns `code`,
`blue`, `red`, and `spectator` links for the QQ reply.
