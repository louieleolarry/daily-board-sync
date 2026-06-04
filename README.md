# Daily Board Sync

Bi-directional sync between Codazen's Confluence weekly standup page and personal [WorkflowShortcuts](https://workflowshortcuts.com) kanban boards. Auto-triages tasks, stack-ranks by priority, and syncs edits both ways.

Syncs are fully bidirectional and automatic — no local machine needed:
- **WFS → Confluence:** Instant via Cloudflare Worker webhook relay
- **Confluence → WFS:** Every 10 minutes via GitHub Actions cron (weekdays, 6am–8pm PT)

---

## For Codazen Team Members

**Brad has already configured the sync infrastructure.** To get started:

1. Go to [workflowshortcuts.com](https://workflowshortcuts.com)
2. Sign in with Google using your **Codazen email** (`@codazen.com`)
3. Your personal daily board is already created and syncing automatically

Your board pulls tasks from the shared Confluence weekly standup page and keeps them in sync. Move cards between columns on your board — those changes persist across syncs.

| Team Member | Board |
|---|---|
| Brad Weldy | [BWeldy Daily](https://workflowshortcuts.com/?boardId=6a15e10131d15ceb8d412054) |
| Sumana Thaduri | [SUThaduri Daily](https://workflowshortcuts.com/?boardId=6a21c7dfb61e1c042ef80739) |
| Jesse Burgess | [JBurgess Daily](https://workflowshortcuts.com/?boardId=6a21cbb5b61e1c042ef80771) |
| Rayan Tighiouart | [RTighiouart Daily](https://workflowshortcuts.com/?boardId=6a21cbb5b61e1c042ef80773) |
| Mario Melchor | [MMelchor Daily](https://workflowshortcuts.com/?boardId=6a21cbb5b61e1c042ef80775) |

---

## How It Works

### Sync Flow

```
Confluence Standup Page
        ↓ parse tasks per user
    sync.py --user all
        ↓ triage + stack-rank
WorkflowShortcuts Boards
        ↓ card moved/edited by user
    WFS Webhook
        ↓
    Cloudflare Worker Relay
        ↓
    GitHub Actions (repository_dispatch)
        ↓
    sync.py --auto --user all
        ↓ push edits back
Confluence Standup Page
```

### What Each Sync Does

1. **WFS → Confluence:** Only user-typed notes are pushed back. Auto-generated card metadata (Project, Refs, Status headers) is never written to Confluence. Content is deduplicated — lines already in Confluence are skipped.

2. **Confluence → WFS:** Fetches your tasks from the weekly standup, auto-triages into columns, stack-ranks by priority, and updates your board. User-added notes on WFS cards are preserved across syncs.

### Formatting Preserved

Rich text formatting survives the round-trip in both directions:

| Format | Supported |
|---|---|
| **Bold** | Yes |
| *Italic* | Yes |
| Underline | Yes |
| Unordered lists (bullets) | Yes |
| Ordered lists (numbered) | Yes |
| Line breaks | Yes |

### Debounced Webhook Relay

WFS board changes trigger a sync via Cloudflare Worker, but with a **60-second debounce** — the sync waits for 1 full minute of inactivity before firing. This prevents mid-edit triggers while you're still moving cards around.

### Dynamic Columns

Only appear when tasks need them:

| Column | Trigger Keywords |
|---|---|
| Todo | (default) |
| In Progress | "working on", "target EOD", "investigating today" |
| In Review | "awaiting review", "submitted for", "LGTM" |
| Blocked | "waiting on", "blocked", "still investigating" |
| On Hold | "on hold", "on pause", "low priority" |
| Needs Input | "approval request", "decision needed" |
| Done | "landed", "completed", "live", "closed" |

### Source Tracking

Cards are tagged with `sourceSystem: "confluence"` and `sourceResourceId` (the Confluence row ID). This means:
- Renaming a task in Confluence won't orphan its WFS card
- Matching is reliable even if titles change
- Cards link back to their Confluence source

---

## Architecture

### Auto-Sync (Production)

Triggered automatically when any user moves a card on their WFS board:

| Direction | Trigger | Latency |
|---|---|---|
| WFS → Confluence | Webhook → Cloudflare Worker → GitHub Actions | Instant (~30s) |
| Confluence → WFS | GitHub Actions cron schedule | Up to 10 min |
| Manual | GitHub Actions "Run workflow" or local CLI | On demand |

```
WFS Board Change → WFS Webhook POST → Cloudflare Worker → GitHub Actions → sync.py
GitHub Actions Cron (every 10m) → sync.py → Confluence changes → WFS boards updated
```

| Component | Location | Purpose |
|---|---|---|
| `sync.py` | This repo | Main sync script |
| Cloudflare Worker | `worker/index.js` | Webhook relay — receives WFS events, triggers GitHub Actions |
| GitHub Actions | `.github/workflows/auto-sync.yml` | Runs sync on dispatch + cron |
| WFS Subscriptions | WFS API | One webhook per board, pointing at the Worker URL |

**Worker URL:** `https://wfs-sync-relay.workflowshortcuts.workers.dev`

### Manual Sync (Local)

Still available for development and debugging:

```bash
python3 sync.py                        # Sync default user (bweldy)
python3 sync.py --user all             # Sync all users
python3 sync.py --user suthaduri       # Sync specific user
python3 sync.py --auto --user all      # Skip unchanged boards (event-based)
python3 sync.py --dry-run -v           # Preview without writing
python3 sync.py --push-only            # Only push board edits to Confluence
python3 sync.py --pull-only            # Only pull Confluence to board
python3 sync.py --daemon 10            # Run continuously every 10 minutes
```

---

## Configuration (for admins)

### Adding a New Team Member

1. Look up their Atlassian account ID:
   ```bash
   curl -s -u email:token \
     https://codazen.atlassian.net/rest/api/3/user/search?query=firstname
   ```

2. Create their WFS board via the bot API:
   ```bash
   curl -X POST https://workflowshortcuts.com/api/bot/actions \
     -H 'X-Bot-Token: <token>' \
     -H 'Content-Type: application/json' \
     -d '{"action":"create","resource":"boards","payload":{
       "title":"FLast Daily",
       "columns":[
         {"key":"todo","title":"Todo"},
         {"key":"in-progress","title":"In Progress"},
         {"key":"in-review","title":"In Review"},
         {"key":"blocked","title":"Blocked"},
         {"key":"done","title":"Done"}
       ]}}'
   ```

3. Add them to `config.json` under `users`:
   ```json
   "username": {
     "atlassian_account_id": "<account_id>",
     "display_name": "First Last",
     "board_id": "<board_id_from_step_2>",
     "board_title_prefix": "FLast Daily",
     "match_name": "first"
   }
   ```

4. Add a WFS webhook subscription for their board:
   ```bash
   curl -X POST https://workflowshortcuts.com/api/bot/subscriptions \
     -H 'X-Bot-Token: <token>' \
     -H 'Content-Type: application/json' \
     -d '{"boardId":"<board_id>",
       "targetUrl":"https://wfs-sync-relay.workflowshortcuts.workers.dev",
       "eventTypes":["card.updated","card.created","card.deleted"]}'
   ```

5. Run initial sync: `python3 sync.py --user <username>`

### Cloudflare Worker Setup

The webhook relay is already deployed at `wfs-sync-relay.workflowshortcuts.workers.dev`. To redeploy or modify:

```bash
cd worker
npx wrangler login                    # One-time Cloudflare auth
npx wrangler deploy                   # Deploy worker
npx wrangler secret put GITHUB_PAT    # Set GitHub PAT (needs repo Actions write scope)
```

The worker filters out bot-originated events to prevent infinite sync loops.

### GitHub Actions Secrets

Set in repo **Settings → Secrets and variables → Actions**:

| Secret | Purpose |
|---|---|
| `CONFLUENCE_EMAIL` | Atlassian account email |
| `CONFLUENCE_TOKEN` | Atlassian API token |
| `WFS_BOT_TOKEN` | WorkflowShortcuts bot API key |

### Cron Polling (Confluence → WFS)

The GitHub Actions workflow runs on a 10-minute cron schedule during weekday work hours (6am–8pm PT) to pick up Confluence changes. This complements the webhook relay which handles the WFS → Confluence direction.

Both triggers use `--auto` mode, which checks WFS events and skips boards with no changes — so cron runs are lightweight when nothing has changed.

To adjust the polling interval, edit the `schedule` block in `.github/workflows/auto-sync.yml`:

```yaml
schedule:
  - cron: '*/10 13-23 * * 1-5'   # UTC 13:00-23:59 Mon-Fri
  - cron: '*/10 0-3 * * 2-6'     # UTC 00:00-03:00 Tue-Sat (covers PT evening)
```

---

## Files

| File | Purpose |
|---|---|
| `sync.py` | Main sync script — multi-user, bi-directional |
| `config.template.json` | Config template with placeholders (committed) |
| `config.json` | Local config with secrets (gitignored) |
| `last_sync_state*.json` | Per-user card snapshots (gitignored) |
| `worker/index.js` | Cloudflare Worker relay source |
| `worker/wrangler.toml` | Worker deployment config |
| `.github/workflows/auto-sync.yml` | GitHub Actions workflow |

## Requirements

- Python 3.8+
- Atlassian API token
- WorkflowShortcuts account + bot API key
- Cloudflare account (for webhook relay)
- GitHub Actions (for automated sync)
