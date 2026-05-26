# Daily Board Sync

Bi-directional sync between your Confluence weekly standup page and a personal WorkflowShortcuts kanban board. Auto-triages tasks, stack-ranks by priority, and syncs edits both ways.

## Quick Start

```bash
# 1. Copy this folder to your machine
cp -r daily-board-sync-dist ~/.claude/skills/daily-board-sync

# 2. Run the setup wizard
python3 ~/.claude/skills/daily-board-sync/sync.py --setup

# 3. Run your first sync
python3 ~/.claude/skills/daily-board-sync/sync.py
```

The setup wizard will:
- Open the Atlassian API token page in your browser
- Validate your credentials
- Auto-detect your account ID and display name
- Create a personal WorkflowShortcuts board
- Write your `config.json`
- Optionally set up a weekday 6AM cron via macOS launchd

## What It Does

**Every sync runs two phases:**

1. **WFS → Confluence:** If you edited a card on your board (added notes, status updates), those additions get pushed back to the matching row in Confluence.

2. **Confluence → WFS:** Fetches your tasks from the weekly standup page, auto-triages into dynamic columns, stack-ranks by priority, and refreshes your board.

**Dynamic columns** — only appear when tasks need them:

| Column | Trigger Keywords |
|--------|-----------------|
| Todo | (default) |
| In Progress | "working on", "target EOD", "investigating today" |
| In Review | "awaiting review", "submitted for", "to review", "LGTM" |
| Blocked | "waiting on", "blocked", "still investigating" |
| On Hold | "on hold", "on pause", "low priority" |
| Needs Input | "approval request", "decision needed" |
| Done | "landed", "completed", "live", "closed" |

## Usage

```bash
python3 sync.py                # Full bi-directional sync
python3 sync.py --dry-run -v   # Preview without writing
python3 sync.py --push-only    # Only push board edits to Confluence
python3 sync.py --pull-only    # Only pull Confluence to board
python3 sync.py --setup        # Re-run setup wizard
```

## Files

| File | Purpose |
|------|---------|
| `sync.py` | Main script (sync + setup wizard) |
| `config.template.json` | Template with placeholders (do not edit) |
| `config.json` | Your personal config (created by setup) |
| `last_sync_state.json` | Card snapshot for diff detection (auto-managed) |
| `sync.log` | Cron output log |

## Requirements

- Python 3.8+
- macOS (for launchd cron; Linux users can use crontab directly)
- Atlassian API token (free, generated during setup)
- WorkflowShortcuts account (boards are created automatically)
