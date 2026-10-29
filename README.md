# Systems integrator demo: bench vs project AI spend on agentgateway (standalone)

A demo for a systems integrator or consultancy that sends all of its AI traffic through one gateway. Between engagements, consultants spend from a personal bench budget to learn and explore. On a client engagement, they spend from that project's budget. Agents that the firm builds for clients bill their project too. The firm, its people, projects and data are all fictional.

Solo Enterprise for agentgateway **v2026.10.1**, standalone (no Kubernetes), with Docker Compose. Keycloak issues every credential. The gateway's built-in UI is the dashboard (Logs, Analytics, Budgets), backed by one SQLite database in the Docker volume `agw-data`.

**All budgets are in US dollars.** The gateway prices every response from its built-in model cost catalog (Haiku 4.5 $1/$5, Sonnet 4.5 $3/$15, Opus 4.5 $5/$25 per million input/output tokens) and charges the dollar cost to each matching budget.


## What runs

| Service | Host port | What it is |
|---|---|---|
| agentgateway | 4300 | LLM `/v1/messages` and federated MCP `/mcp` |
| agentgateway UI | 15300 | `http://localhost:15300/ui` |
| agentgateway metrics / ready | 15320 / 15321 | Prometheus `/metrics`, `/healthz/ready` |
| Keycloak | 8380 | realm `si-demo`, admin `admin` / `admin` |
| mock-llm | (internal) | Anthropic-compatible mock that returns realistic `usage`; costs nothing |
| mcp-tickets, mcp-docs, mcp-repo | (internal) | synthetic MCP servers, federated behind `/mcp` |

Ports are offset from the defaults so the demo does not collide with another local gateway. The gateway runs in Keycloak's network namespace (`network_mode: service:keycloak`), so the token issuer `http://localhost:8380` resolves to Keycloak from the gateway too. MCP OAuth discovery needs that. This is why the gateway's ports are published on the `keycloak` service.

The gateway listens on the same ports inside the container as on the host (4300, 15300, 15320, 15321), so the UI's links and playgrounds point at addresses that work from the browser.

**Config changes need a restart** (`docker compose restart agentgateway`). The file watch does not fire through Colima's bind mount.

`demo.sh` uses your current Docker context. To use a different one, set `DOCKER_CONTEXT`. If image pulls time out under Colima, run `./demo.sh colima-net`.

## How attribution works

All in `gateway/config.yaml`, `config.standardAttributes`:

| Login | Token carries | `group` in the UI | Budget charged |
|---|---|---|---|
| person via `bench-app` | `bench_user=<username>` | `bench` | `bench-monthly` for that person ($100 per 30 days, Block) |
| person via `project-<x>-app` | `project=<x>` | `project-<x>` | `<x>-monthly` (Atlas $2,000 Block, Beacon $1,200 Block, Cobalt $800 Audit) |
| service app (client credentials) | `project=<x>`, `app=<name>` | `project-<x>` | `<x>-monthly`, user shows as the app name |

A budget does not apply when its subject attribute is missing, so a bench call never touches a project counter and a project call never touches the person's bench counter. In the UI: **LLM > Analytics**, group by **Group** for bench vs. project, by **User** for people and apps.

Demo overrides: `tom-vega-daily` ($3 per 24h, Block) for the live 429, and `cobalt-daily` ($40 per 24h, Audit) for Audit vs. Block.

Wrong-app guard: a person can bill a project only if they are in that project's Keycloak group (`llm.policies.authorization`). `tom.vega` on `project-cobalt-app` gets a token from Keycloak and a 403 from the gateway.

**Budget windows are fixed and aligned to the Unix epoch, not calendar months.** For example, one `30d` window runs 2026-10-04 to 2026-11-03 UTC. A `24h` window resets at midnight UTC.

## First-time setup

```bash
cp .env.example .env        # set ENTERPRISE_AGENTGATEWAY_LICENSE_KEY; ANTHROPIC_API_KEY is optional
./demo.sh up
```

Needs Docker (Colima works) and Python 3.10+ on the host. The scripts use only the standard library.

## Run the demo

**Seed the history.**

```bash
./demo.sh seed
```

This stops the gateway, wipes the request logs and budget counters, writes 30 days of history ending now, and starts the gateway again. The writing runs in a short-lived `seeder` container against the `agw-data` volume. It prints the dollar counters it left behind. Same `--seed`, same history. The seeded month includes:

- About 66,000 requests and about $4,200 across 10 consultants, 3 projects and 4 service apps, on an ET weekday curve, growing about 40% across the month. Requests are Claude Code sized (15k to 25k input tokens per turn), so the dollar figures are realistic.
- Maria Gomez on bench, Atlas and Beacon.
- Atlas code migrator: a migration sprint mid-month on Opus.
- Beacon test generator: nightly batch runs, including weekends.
- Cobalt content agent: steady all day. Cobalt is on pace for about 135% of budget (Audit, so it runs over and is logged).
- Beacon on pace for about 105% of its cap.
- Nina Cruz on the bench doing certification work on Opus, near her cap.
- Omar Silva's runaway script, a few days before the current window opened. It shows as a spike in Analytics that stops dead when his $100 bench cap trips. More than 1,200 further calls were blocked. The gateway does not write blocked (429) or denied (403) requests to the request log, only to its stdout log, so the seed does not either. His counter has since reset.

- Tom Vega has no bench calls yet today, so his $3 daily cap is empty when the block moment runs.

Budget levels are set as a month pace (`scripts/personas.py`, `BENCH_PACE` and `PROJECT_PACE`): the share of each dollar budget a counter would reach by the end of its window. The bars look right whatever day you seed. Seeded on 2026-10-23: Atlas $1,207 of $2,000 (60%), Beacon $826 of $1,200 (69%), Cobalt $708 of $800 (89%), Nina $64 of $100, Maria $26 of $100.

**Keep traffic flowing.**

```bash
./demo.sh live                 # ~0.6 req/s, one real minute = one business hour
./demo.sh live --rps 1.5       # busier
```

Every request is real (Keycloak token, JWT validation, budget check and charge, request log). Only the model is mocked. `tom.vega` is excluded so his daily cap is still there for the block moment.

**Scripted moments** (second terminal):

| Command | What it shows |
|---|---|
| `./demo.sh moment tokens` | Maria's two tokens side by side; only `bench_user` / `project` differ |
| `./demo.sh moment maria` | same person on bench, then Atlas, then Beacon; shows which dollar counter moved, and by how much |
| `./demo.sh moment block-tom` | Tom loops Opus calls (about $0.38 each) until his $3 daily cap returns 429; Diego on Atlas still gets 200 |
| `./demo.sh moment wrong-app` | Tom tries to bill Cobalt; 403 |
| `./demo.sh moment service-app` | `atlas-claims-summarizer` gets its own token and bills Atlas |
| `./demo.sh moment real` | One call to the real Anthropic API (needs `ANTHROPIC_API_KEY`) |
| `./demo.sh status` | All dollar counters as bars, read from the gateway API |
| `./demo.sh reset-tom` | Clear Tom's daily counter to run the block again |

Real-model calls: the gateway sends a request to the real Anthropic API only when it carries `x-demo-backend: anthropic`. Everything else goes to the mock, so a mistake costs nothing. For Claude Code through the gateway:

```bash
export ANTHROPIC_BASE_URL=http://localhost:4300
export ANTHROPIC_AUTH_TOKEN=$(curl -s http://localhost:8380/realms/si-demo/protocol/openid-connect/token \
  -d grant_type=password -d client_id=project-atlas-app -d username=maria.gomez -d password=demo-pass | jq -r .access_token)
export ANTHROPIC_CUSTOM_HEADERS="x-demo-backend: anthropic"
claude
```

Keycloak tokens last 5 minutes (realm default). For a long Claude Code session, raise the access token lifespan for the realm in Keycloak.

## In the browser

| Page | What to show |
|---|---|
| `http://localhost:15300/ui/llm/analytics?range=30d&groupBy=group&metric=cost` | 30 days of spend in dollars, bench vs. each project. Weekend dips, growth across the month, Omar's spike. The **Measure** dropdown switches between Cost and Tokens |
| `...?range=30d&groupBy=user&metric=cost` | Dollars by person and app. The Atlas migrator sprint stands out |
| `http://localhost:15300/ui/llm/budgets` | All six dollar budgets ($100 bench, $2,000 / $1,200 / $800 projects, $3 Tom, $40 Cobalt daily). Click the bench-monthly usage for one dollar bar per person. Tom turns red at the block. The **Cost catalog** tab shows the per-model prices |
| `http://localhost:15300/ui/llm/logs` | Live rows with the dollar cost of each call. Expand one to see user, group and the input/output cost split |
| `http://localhost:15300/ui/llm/playground` | Pick `claude-*`, type a specific model such as `sonnet-4-5`, paste a Keycloak token into Raw API key, and send. It is billed to whatever the token says |
| `http://localhost:15300/ui/mcp/playground` | Paste a token under Authorization header and click Initialize. The tool list depends on the person (Tom does not see `repo_*`) |
| `http://localhost:8380/admin/master/console/#/si-demo/clients` | Keycloak apps and groups (`admin` / `admin`). A yellow "temporary admin" banner shows; ignore it or create a permanent admin |

Get a token to paste: `python3 -c "import sys; sys.path.insert(0,'scripts'); import agw; print(agw.token('project-atlas-app','maria.gomez'))"`. Tokens last 5 minutes.

## MCP

`/mcp` federates `tickets_*`, `docs_*` and `repo_*` tools. It is protected with MCP OAuth against Keycloak (`provider: keycloak`). `search_tickets` is rate limited to 5 calls a minute per person. `repo_*` tools need a project group.

- **Claude Code:** `claude mcp add --transport http si-demo http://localhost:4300/mcp`
- **Claude.ai custom connector:** needs public HTTPS for the gateway and Keycloak (for example, a cloudflared tunnel for each). Then set `MCP_PUBLIC_URL` and `KEYCLOAK_ISSUER` / `KC_HOSTNAME` to the public URLs, and use the pre-registered `claude-connector` client. Untested: Solo's docs cover Claude Desktop and Claude Code, not the Claude.ai web connector. Prefer the pre-registered client over dynamic client registration, which creates a Keycloak client per user.

**Security:** this demo uses well-known credentials: Keycloak `admin` / `admin`, user password `demo-pass` and client secrets `demo-secret-*`. Run it on localhost only. Before you expose Keycloak or the gateway through a tunnel, change all of these credentials.

## Files

| Path | What |
|---|---|
| `gateway/config.yaml` | the whole gateway: attributes, JWT, budgets, models, MCP |
| `keycloak/realm-si-demo.json` | users, groups, apps |
| `scripts/personas.py` | the fictional firm; keep in sync with the two files above |
| `scripts/backfill.py` | 30-day history into the gateway database (run by `./demo.sh seed` in the `seeder` container) |
| `scripts/live_traffic.py` | continuous traffic |
| `scripts/moments.py` | the scripted beats and `status` |
| `mock-llm/`, `mcp-servers/` | the synthetic backends |

## Verification status

Verified end to end on 2026-10-23, with the license, on v2026.10.1:

- [x] The gateway loads the seeded budget counters (`/api/budgets/usage` matches `./demo.sh status`).
- [x] The UI analytics API reads the seeded history, grouped by bench vs. project and by the custom `project` attribute.
- [x] Maria via bench-app charges only her bench budget ($0.075 for a 21k-token Sonnet call). Via project-atlas-app only Atlas, via project-beacon-app only Beacon.
- [x] Tom: 8 Opus calls ($3.00), then 429 `budget_exceeded` with `Retry-After`. Diego on Atlas still gets 200 in the same minute.
- [x] Wrong-app: Keycloak issues Tom a Cobalt token, and the gateway returns 403.
- [x] Service app: its own client-credentials token, billed to Atlas, logged as the app name.
- [x] Live generator: about 77 requests in 45 s at `--rps 1.5`, no errors.
- [x] MCP without a token returns 401 with `WWW-Authenticate` and protected-resource metadata. Authorization-server metadata points at Keycloak.
- [x] MCP federation lists `tickets_*`, `docs_*` and `repo_*` tools. `repo_*` is hidden from bench-only users.
- [x] `search_tickets`: the 6th call in a minute returns a rate-limit tool error, and other tools keep working.
- [x] The seeded rows use the same columns and attributes as live rows, checked against a live row.

Checked in Chrome (2026-10-23):

- [x] Analytics: 30 days in dollars, grouped by bench vs. project and by user.
- [x] Budgets: all six in dollars, and the per-person bench drill-down updates live.
- [x] Logs: live and seeded rows render the same, with user, group, client and cost.
- [x] Chat Playground call, billed to `project-atlas` from a Maria Atlas token.
- [x] MCP Tool Playground: 7 federated tools, and `tickets_list_tickets` returns Atlas tickets.
- [x] Keycloak admin console.

Not yet tested:

- [ ] Claude Code MCP OAuth sign-in through the browser. The metadata endpoints work.
- [ ] Real Anthropic routing with `x-demo-backend: anthropic`.
- [ ] The Claude.ai connector. It needs public HTTPS.

Notes:

- **Never open the database from the Mac** (no `sqlite3 data/...`, no host scripts) while the gateway runs. SQLite's write-ahead log needs shared memory that does not work across Colima's file share, and concurrent access corrupted the database once ("database disk image is malformed" in Analytics). That is why the database lives in a Docker volume and the scripts read budgets through `http://localhost:15300/api/budgets/usage`.
- The MCP session key comes from `SESSION_KEY` in `.env` (`./demo.sh up` generates one). Without it, the gateway writes a generated key into `gateway/config.yaml`; do not commit that.
- Keycloak runs `start-dev` and re-imports the realm whenever its container is recreated. User IDs (`sub`) then change, so run `./demo.sh seed` after any Keycloak recreate.
