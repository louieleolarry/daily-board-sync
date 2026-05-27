#!/usr/bin/env python3
"""
daily-board-sync: Bi-directional sync between Codazen Confluence weekly standup
and a WorkflowShortcuts kanban board.

Flow:
  1. WFS → Confluence: Push any new content added to board cards back to Confluence
  2. Confluence → WFS: Fetch tasks, triage, stack-rank, and populate the board

Usage:
    python3 sync.py                  # Full bi-directional sync
    python3 sync.py --dry-run        # Preview without writing
    python3 sync.py --date 2026-05-25 # Override the week start date
    python3 sync.py --push-only      # Only push WFS changes to Confluence
    python3 sync.py --pull-only      # Only pull Confluence to WFS (skip reverse)
"""

import argparse
import json
import os
import re
import sys
import urllib.request
import urllib.error
import urllib.parse
import uuid
from base64 import b64encode
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

SCRIPT_DIR = Path(__file__).parent
CONFIG_PATH = SCRIPT_DIR / "config.json"
STATE_PATH = SCRIPT_DIR / "last_sync_state.json"


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_state():
    if STATE_PATH.exists():
        with open(STATE_PATH) as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_PATH, "w") as f:
        json.dump(state, f, indent=2)


def load_credentials(config):
    creds = config.get("credentials", {})
    if creds.get("email") and creds.get("token"):
        return creds["email"], creds["token"]
    creds_source = config.get("confluence", {}).get("credentials_source", "")
    if creds_source:
        creds_path = Path(creds_source).expanduser()
        if creds_path.exists():
            with open(creds_path) as f:
                jira_config = json.load(f)
            return jira_config["jira"]["email"], jira_config["jira"]["token"]
    print("  ERROR: No credentials found. Run: python3 sync.py --setup", file=sys.stderr)
    sys.exit(1)


def api_request(url, method="GET", data=None, auth=None):
    headers = {"Content-Type": "application/json"}
    if auth:
        email, token = auth
        cred = b64encode(f"{email}:{token}".encode()).decode()
        headers["Authorization"] = f"Basic {cred}"

    body = json.dumps(data).encode() if data else None
    req = urllib.request.Request(url, data=body, headers=headers, method=method)

    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code} for {method} {url}", file=sys.stderr)
        body = e.read().decode()
        print(f"  {body[:200]}", file=sys.stderr)
        return None


def get_current_week_start(override_date=None, start_day="monday"):
    """Compute the start date of the current work week.

    The Confluence page title encodes the week start date directly in the URL
    (e.g., 'Week of 2026-05-25'). This function must match that convention.
    If the team changes their week start day, update config.json week_start_day.
    """
    day_map = {"monday": 0, "sunday": 6, "tuesday": 1, "wednesday": 2,
               "thursday": 3, "friday": 4, "saturday": 5}
    target = day_map.get(start_day.lower(), 0)

    if override_date:
        dt = datetime.strptime(override_date, "%Y-%m-%d")
    else:
        dt = datetime.now()

    days_back = (dt.weekday() - target) % 7
    week_start = dt - timedelta(days=days_back)
    return week_start.strftime("%Y-%m-%d")


def find_weekly_page(config, auth, week_date):
    """Find the Confluence weekly standup page by title.

    IMPORTANT: The page URL and title embed the week start date
    (e.g., 'Week of 2026-05-25'). The date in the title MUST match
    the computed week_date or we'll pull the wrong week's tasks.
    """
    base = config["confluence"]["base_url"]
    space = config["confluence"]["space_key"]
    title = f"Week of {week_date}"

    encoded_title = urllib.parse.quote(title)
    url = (
        f"{base}/wiki/rest/api/content?"
        f"spaceKey={space}&title={encoded_title}&expand=body.storage,version"
    )
    result = api_request(url, auth=auth)

    if not result or not result.get("results"):
        print(f"  WARN: No page found for '{title}', trying adjacent weeks...", file=sys.stderr)
        dt = datetime.strptime(week_date, "%Y-%m-%d")
        for offset in [-1, 1, -7, 7]:
            alt_date = (dt + timedelta(days=offset)).strftime("%Y-%m-%d")
            alt_title = f"Week of {alt_date}"
            alt_url = (
                f"{base}/wiki/rest/api/content?"
                f"spaceKey={space}&title={urllib.parse.quote(alt_title)}&expand=body.storage,version"
            )
            alt_result = api_request(alt_url, auth=auth)
            if alt_result and alt_result.get("results"):
                print(f"  WARN: Using '{alt_title}' instead (week start day mismatch?)", file=sys.stderr)
                return alt_result["results"][0]
        print(f"  FAILED: Could not find page for '{title}' or adjacent dates", file=sys.stderr)
        return None

    page = result["results"][0]
    if page["title"] != title:
        print(f"  WARN: Title mismatch — expected '{title}', got '{page['title']}'", file=sys.stderr)

    return page


# ---------------------------------------------------------------------------
# Reverse sync: WFS → Confluence
# ---------------------------------------------------------------------------

def strip_html_tags(html_str):
    """Strip HTML tags to get plain text, preserving line breaks."""
    text = re.sub(r'<br\s*/?>', '\n', html_str)
    text = re.sub(r'</(?:div|p|li|tr)>', '\n', text)
    text = re.sub(r'<[^>]+>', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text)
    return text.strip()


def extract_new_content(current_text, synced_text):
    """Find content added to a WFS card since last sync.

    Compares plain-text versions of current card vs what we generated.
    Returns list of new lines added at the end.
    """
    current_plain = strip_html_tags(current_text).strip()
    synced_plain = strip_html_tags(synced_text).strip()

    if current_plain == synced_plain:
        return []

    if current_plain.startswith(synced_plain):
        new_part = current_plain[len(synced_plain):].strip()
        if new_part:
            return [line.strip() for line in new_part.split("\n") if line.strip()]

    current_lines = [l.strip() for l in current_plain.split("\n") if l.strip()]
    synced_lines = [l.strip() for l in synced_plain.split("\n") if l.strip()]

    new_lines = []
    for line in current_lines:
        if line not in synced_lines:
            new_lines.append(line)

    return new_lines


_GENERIC_WORDS = {
    "the", "and", "for", "from", "with", "that", "this", "have", "will",
    "form", "submission", "dataset", "self", "service", "request", "update",
    "task", "issue", "item", "action", "status", "review", "work", "data",
    "test", "page", "note", "info", "detail", "report", "meeting", "call",
}


def _rank_word_distinctiveness(word):
    """Higher score = more distinctive, better search candidate."""
    w = word.lower()
    if w in _GENERIC_WORDS:
        return 0
    score = len(word)
    if any(c.isupper() for c in word[1:]):
        score += 20
    if not word.isascii():
        score += 10
    return score


def find_confluence_row_ending(html, task_title):
    """Find the end of the status cell for a task row in Confluence HTML.

    Returns insert position after the last </p> in the status cell, or None.
    """
    title_clean = task_title.split("[")[-1].split("]")[-1].strip() if "[" in task_title else task_title
    raw_words = [re.sub(r'["\'\(\)\[\],;:]+', '', w) for w in title_clean.split()]
    title_words = [w for w in raw_words if len(w) > 3][:6]

    if not title_words:
        return None

    candidates = sorted(title_words, key=_rank_word_distinctiveness, reverse=True)

    for search_term in candidates:
        idx = html.find(search_term)
        attempts = 0
        while idx >= 0 and attempts < 30:
            tr_start = html.rfind("<tr", 0, idx)
            tr_end = html.find("</tr>", idx)
            if tr_start >= 0 and tr_end >= 0:
                row = html[tr_start:tr_end + 5]
                match_count = sum(1 for w in title_words if w in row)
                if match_count >= min(3, len(title_words)):
                    cells = list(re.finditer(r'<td[^>]*>', row))
                    if len(cells) >= 2:
                        status_cell_start = cells[1].start()
                        status_cell_rel_end = row.find("</td>", status_cell_start)
                        if status_cell_rel_end >= 0:
                            last_p_end = row.rfind("</p>", status_cell_start, status_cell_rel_end)
                            if last_p_end >= 0:
                                abs_pos = tr_start + last_p_end + 4
                                return abs_pos
            idx = html.find(search_term, idx + 1)
            attempts += 1

    return None


def detect_new_wfs_cards(board_cards, state):
    """Find WFS cards that don't match any saved state entry."""
    synced_titles = {s["title"] for s in state.get("cards", {}).values()}
    new_cards = []
    for card in board_cards:
        title = card.get("title", "")
        if title and title not in synced_titles:
            new_cards.append(card)
    return new_cards


def insert_new_confluence_row(html, section_name, task_title, status_text, user_account_id):
    """Insert a new task row at the top of a section's table in Confluence HTML."""
    pattern = f'<ac:parameter ac:name="title">{re.escape(section_name)}</ac:parameter>'
    section_match = re.search(pattern, html)
    if not section_match:
        return None

    table_start = html.find('<table', section_match.start())
    if table_start < 0:
        return None
    table_end = html.find('</table>', table_start)
    if table_end < 0:
        return None

    first_tr_end = html.find('</tr>', table_start)
    if first_tr_end < 0:
        return None
    insert_pos = first_tr_end + 5

    lid1 = uuid.uuid4().hex[:8]
    lid2 = uuid.uuid4().hex[:8]
    lid3 = uuid.uuid4().hex[:8]
    today = datetime.now().strftime("%Y-%m-%d")

    user_tag = (
        f'<ac:link><ri:user ri:account-id="{user_account_id}"/></ac:link>'
    )

    status_lines = [l.strip() for l in status_text.split('\n') if l.strip()]
    status_html = f'<p local-id="{lid3}"><time datetime="{today}" /></p>'
    for line in status_lines:
        slid = uuid.uuid4().hex[:8]
        status_html += f'<p local-id="{slid}">{line}</p>'

    new_row = (
        f'<tr>'
        f'<td><p local-id="{lid1}">{task_title}</p>'
        f'<p local-id="{lid2}">{user_tag}</p></td>'
        f'<td>{status_html}</td>'
        f'</tr>'
    )

    return html[:insert_pos] + new_row + html[insert_pos:]


def push_updates_to_confluence(config, auth, page, board_cards, state, dry_run=False):
    """Push new content from WFS cards back to the Confluence page."""
    if not state:
        print("  No previous sync state — skipping reverse sync")
        return False

    synced_cards = state.get("cards", {})
    updates = []

    for card in board_cards:
        card_title = card.get("title", "")
        card_text = card.get("text", "")

        matched_key = None
        for key, saved in synced_cards.items():
            if saved.get("title") == card_title:
                matched_key = key
                break

        if not matched_key:
            continue

        saved = synced_cards[matched_key]
        if card.get("updatedAt") == card.get("createdAt"):
            continue

        new_lines = extract_new_content(card_text, saved.get("text", ""))
        if new_lines:
            updates.append({
                "title": card_title,
                "confluence_title": saved.get("confluence_title", card_title),
                "new_lines": new_lines,
            })

    if not updates:
        print("  No WFS edits to push to Confluence")
        return False

    html = page["body"]["storage"]["value"]
    version = page["version"]["number"]
    modified = False

    for update in updates:
        if dry_run:
            print(f"  WOULD PUSH: {update['title']}")
            for line in update["new_lines"]:
                print(f"    + {line}")
            continue

        insert_pos = find_confluence_row_ending(html, update["confluence_title"])
        if insert_pos is None:
            print(f"  WARN: Could not find Confluence row for '{update['title']}'", file=sys.stderr)
            continue

        new_p_tags = ""
        for line in update["new_lines"]:
            lid = uuid.uuid4().hex[:8]
            new_p_tags += f'<p local-id="{lid}">{line}</p>'

        html = html[:insert_pos] + new_p_tags + html[insert_pos:]
        modified = True
        print(f"  Pushed to Confluence: {update['title']} (+{len(update['new_lines'])} lines)")

    if modified and not dry_run:
        base = config["confluence"]["base_url"]
        today = datetime.now().strftime("%Y-%m-%d")
        update_payload = {
            "id": page["id"],
            "type": "page",
            "title": page["title"],
            "body": {
                "storage": {
                    "value": html,
                    "representation": "storage"
                }
            },
            "version": {
                "number": version + 1,
                "message": f"daily-board-sync: pushed WFS updates ({today})"
            }
        }
        result = api_request(
            f"{base}/wiki/rest/api/content/{page['id']}",
            method="PUT", data=update_payload, auth=auth
        )
        if result:
            print(f"  Confluence updated to version {result['version']['number']}")
            return True
        else:
            print("  WARN: Confluence update failed", file=sys.stderr)

    return modified


# ---------------------------------------------------------------------------
# Forward sync: Confluence → WFS
# ---------------------------------------------------------------------------

class ConfluenceTaskParser(HTMLParser):
    """Parse Confluence storage format HTML to extract task rows from tables."""

    def __init__(self, target_account_id):
        super().__init__()
        self.target_id = target_account_id
        self.tasks = []
        self.current_section = ""

        self._in_table = False
        self._in_row = False
        self._in_cell = False
        self._cell_index = 0
        self._row_data = {"title_html": "", "status_html": "", "title_text": "", "status_text": ""}
        self._current_text = ""
        self._current_html = ""
        self._row_has_target_user = False
        self._row_mentions_target = False
        self._is_header_row = False
        self._highlight_color = None
        self._section_stack = []
        self._skip_depth = 0

        self._href_links = []
        self._current_href = None

    def handle_starttag(self, tag, attrs):
        attr_dict = dict(attrs)

        if tag == "ac:structured-macro":
            name = attr_dict.get("ac:name", "")
            if name == "expand":
                self._section_stack.append("")

        if tag == "ac:parameter":
            name = attr_dict.get("ac:name", "")
            if name == "title" and self._section_stack:
                self._section_stack[-1] = "__collecting_title__"

        if tag == "table":
            self._in_table = True

        if tag == "tr" and self._in_table:
            self._in_row = True
            self._cell_index = 0
            self._row_data = {"title_html": "", "status_html": "", "title_text": "", "status_text": ""}
            self._row_has_target_user = False
            self._row_mentions_target = False
            self._is_header_row = False
            self._highlight_color = None

        if tag in ("td", "th") and self._in_row:
            self._in_cell = True
            self._current_text = ""
            self._current_html = ""
            self._href_links = []
            if tag == "th":
                self._is_header_row = True
            color = attr_dict.get("data-highlight-colour", "")
            if color and color not in ("#ffffff", "#f4f5f7"):
                self._highlight_color = color

        if tag == "ri:user" and self._in_cell:
            acct = attr_dict.get("ri:account-id", "")
            if acct == self.target_id:
                self._row_has_target_user = True

        if tag == "a" and self._in_cell:
            href = attr_dict.get("href", "")
            self._current_href = href
            self._href_links.append(href)

        if tag == "time" and self._in_cell:
            dt = attr_dict.get("datetime", "")
            if dt:
                self._current_html += f"<time>{dt}</time> "

        if tag == "p" and self._in_cell:
            if self._current_text and not self._current_text.endswith("\n"):
                self._current_text += "\n"

        if tag == "br" and self._in_cell:
            self._current_text += "\n"
            self._current_html += "<br>"

        if tag == "hr" and self._in_cell:
            self._current_text += "\n---\n"
            self._current_html += "<hr>"

        if tag in ("strong", "b") and self._in_cell:
            self._current_html += "<b>"

    def handle_endtag(self, tag):
        if tag in ("strong", "b") and self._in_cell:
            self._current_html += "</b>"

        if tag == "ac:parameter":
            if self._section_stack and self._section_stack[-1] == "__collecting_title__":
                self._section_stack[-1] = ""

        if tag == "ac:structured-macro" and self._section_stack:
            self._section_stack.pop()

        if tag == "a" and self._in_cell:
            self._current_href = None

        if tag in ("td", "th") and self._in_row:
            if self._cell_index == 0:
                self._row_data["title_text"] = self._current_text.strip()
                self._row_data["title_html"] = self._current_html.strip()
                self._row_data["title_links"] = list(self._href_links)
            elif self._cell_index == 1:
                self._row_data["status_text"] = self._current_text.strip()
                self._row_data["status_html"] = self._current_html.strip()
                self._row_data["status_links"] = list(self._href_links)
            self._cell_index += 1
            self._in_cell = False

        if tag == "tr" and self._in_table:
            self._in_row = False
            if not self._is_header_row and self._row_data["title_text"].strip():
                target_name_lower = config.get("user", {}).get("first_name_lower", "")
                if not target_name_lower:
                    target_name_lower = config.get("user", {}).get("display_name", "").split()[0].lower()
                status_lower = self._row_data["status_text"].lower()
                title_lower = self._row_data["title_text"].lower()
                if target_name_lower in status_lower or target_name_lower in title_lower:
                    self._row_mentions_target = True

                if self._row_has_target_user or self._row_mentions_target:
                    self.tasks.append({
                        "title": self._extract_title(self._row_data["title_text"]),
                        "title_full": self._row_data["title_text"],
                        "status_text": self._row_data["status_text"],
                        "section": self.current_section,
                        "highlight_color": self._highlight_color,
                        "is_assigned": self._row_has_target_user,
                        "is_mentioned": self._row_mentions_target and not self._row_has_target_user,
                        "links": {
                            "tasks": self._extract_task_ids(self._row_data.get("title_links", [])),
                            "jira": self._extract_jira_keys(self._row_data["title_text"]),
                            "diffs": self._extract_diff_ids(
                                self._row_data["title_text"] + " " + self._row_data["status_text"]
                            ),
                        },
                    })

        if tag == "table":
            self._in_table = False

    def handle_data(self, data):
        if self._section_stack and self._section_stack[-1] == "__collecting_title__":
            self.current_section = data.strip()
            self._section_stack[-1] = data.strip()
            return

        if self._in_cell:
            self._current_text += data
            self._current_html += data

    def _extract_title(self, text):
        lines = text.strip().split("\n")
        title = lines[0].strip()
        title = re.sub(r'\s+', ' ', title)
        title = re.sub(r'\s*[A-Z]+-\d+.*$', '', title)
        title = re.sub(r'\s*T\d{9,}.*$', '', title)
        title = re.sub(r'\s*https?://\S+.*$', '', title)
        title = re.sub(r'\s*Priority:.*$', '', title)
        title = title.strip()
        if len(title) > 100:
            title = title[:97] + "..."
        return title

    def _extract_task_ids(self, links):
        ids = []
        for link in links:
            m = re.search(r'T(\d{9,})', link)
            if m:
                ids.append(f"T{m.group(1)}")
            m = re.search(r't=(\d{9,})', link)
            if m:
                ids.append(f"T{m.group(1)}")
        return list(set(ids))

    def _extract_jira_keys(self, text):
        return list(set(re.findall(r'[A-Z]+-\d+', text)))

    def _extract_diff_ids(self, text):
        return list(set(re.findall(r'D\d{9,}', text)))


def classify_status(task, config):
    """Determine board column based on priority signals in the task."""
    signals = config["triage"]["priority_signals"]
    status = task["status_text"].lower()
    title = task["title_full"].lower()
    combined = f"{title} {status}"

    if task["highlight_color"] in config["triage"].get("highlight_colors", {}):
        mapped = config["triage"]["highlight_colors"][task["highlight_color"]]
        if any(re.search(kw, combined) for kw in signals["on_hold_keywords"]):
            return "on-hold"
        if any(re.search(kw, combined) for kw in signals["blocked_keywords"]):
            return "blocked"
        return mapped

    if any(re.search(kw, combined) for kw in signals["done_keywords"]):
        if "need" not in combined and "waiting" not in combined:
            return "done"

    if any(re.search(kw, combined) for kw in signals["on_hold_keywords"]):
        return "on-hold"

    if any(re.search(kw, combined, re.IGNORECASE) for kw in signals.get("in_review_keywords", [])):
        return "in-review"

    if any(re.search(kw, combined) for kw in signals["blocked_keywords"]):
        return "blocked"

    if any(re.search(kw, combined, re.IGNORECASE) for kw in signals.get("needs_input_keywords", [])):
        return "needs-input"

    if any(re.search(kw, combined) for kw in signals["in_progress_keywords"]):
        return "in-progress"

    return "todo"


def compute_priority_score(task, config):
    """Higher score = higher priority. Used for stack ranking within columns."""
    score = 50
    signals = config["triage"]["priority_signals"]
    combined = f"{task['title_full']} {task['status_text']}".lower()

    if any(kw.lower() in combined for kw in signals["p0_keywords"]):
        score += 40
    elif any(kw.lower() in combined for kw in signals["p1_keywords"]):
        score += 20

    if "today" in combined or "eod" in combined:
        score += 30
    if "this week" in combined:
        score += 10

    if task["is_assigned"]:
        score += 5
    if task["is_mentioned"] and not task["is_assigned"]:
        score -= 5

    if task.get("links", {}).get("diffs"):
        score += 3

    today_str = datetime.now().strftime("%Y-%m-%d")
    if today_str in task["status_text"]:
        score += 15

    return score


def build_card_html(task):
    """Build rich card body from task data."""
    parts = []

    parts.append(f"<b>Project:</b> {task['section']}")

    if task["is_mentioned"] and not task["is_assigned"]:
        parts.append("<b>Role:</b> Reviewing / Mentioned")

    refs = []
    for tid in task["links"].get("tasks", []):
        refs.append(tid)
    for jk in task["links"].get("jira", []):
        refs.append(jk)
    for did in task["links"].get("diffs", []):
        refs.append(did)
    if refs:
        parts.append(f"<b>Refs:</b> {' · '.join(refs)}")

    parts.append("")

    status_lines = task["status_text"].strip()
    date_sections = re.split(r'(?=\d{4}-\d{2}-\d{2})', status_lines)
    if len(date_sections) > 1:
        latest = date_sections[-1].strip()
        latest = re.sub(r'^\d{4}-\d{2}-\d{2}\s*', '', latest).strip()
        if latest:
            parts.append(f"<b>Latest:</b> {latest}")
    elif status_lines:
        cleaned = re.sub(r'\d{4}-\d{2}-\d{2}\s*', '', status_lines).strip()
        cleaned = re.sub(r'\n-+\n', '\n', cleaned)
        if cleaned:
            parts.append(f"<b>Status:</b> {cleaned}")

    return "<div>" + "</div><div>".join(parts) + "</div>"


def sync_board(tasks, config, dry_run=False, protected_card_ids=None):
    """Clear board and repopulate with triaged tasks. Returns card state for saving."""
    board_id = config["board"]["board_id"]
    api_base = config["board"]["api_base"]
    today = datetime.now().strftime("%Y-%m-%d")
    all_columns = config["board"]["all_columns"]
    permanent = set(config["board"]["permanent_columns"])
    protected = set(protected_card_ids or [])

    classified = []
    for task in tasks:
        col = classify_status(task, config)
        priority = compute_priority_score(task, config)
        classified.append((task, col, priority))

    classified.sort(key=lambda x: -x[2])

    needed_columns = set(c for _, c, _ in classified) | permanent
    active_columns = []
    for key in sorted(needed_columns, key=lambda k: all_columns.get(k, {}).get("order", 99)):
        meta = all_columns.get(key, {"title": key.replace("-", " ").title(), "order": 99})
        active_columns.append({"key": key, "title": meta["title"]})

    if not dry_run:
        board_url = f"{api_base}/boards/{board_id}"
        api_request(board_url, method="PATCH", data={
            "title": f"BWeldy Daily — {today}",
            "columns": active_columns,
        })
        added_cols = [c["title"] for c in active_columns if c["key"] not in permanent]
        if added_cols:
            print(f"  Dynamic columns added: {', '.join(added_cols)}")
    else:
        col_names = [c["title"] for c in active_columns]
        print(f"  Columns: {' → '.join(col_names)}")

    docs_url = f"{api_base}/documents?boardId={board_id}"
    existing = api_request(docs_url) or []
    board_docs = [d for d in existing if d.get("boardId") == board_id]

    if not dry_run:
        deleted = 0
        preserved = 0
        for doc in board_docs:
            if doc["_id"] in protected:
                preserved += 1
            else:
                api_request(f"{api_base}/documents/{doc['_id']}", method="DELETE")
                deleted += 1
        msg = f"  Cleared {deleted} existing cards"
        if preserved:
            msg += f" (preserved {preserved} WFS-only)"
        print(msg)

    columns_order = [c["key"] for c in active_columns]
    created = 0
    card_state = {}

    for col_key in columns_order:
        col_tasks = [(t, c, p) for t, c, p in classified if c == col_key]
        col_tasks.sort(key=lambda x: -x[2])

        for task, _, priority in col_tasks:
            card_html = build_card_html(task)
            card = {
                "title": task["title"],
                "text": card_html,
                "status": col_key,
                "type": "document",
                "boardId": board_id,
            }

            if dry_run:
                role = "assigned" if task["is_assigned"] else "mentioned"
                print(f"  [{col_key:12s}] (p={priority:3d}) [{role:8s}] {task['title']}")
            else:
                result = api_request(f"{api_base}/documents", method="POST", data=card)
                if result:
                    created += 1
                    card_state[result["_id"]] = {
                        "title": task["title"],
                        "confluence_title": task["title_full"].split("\n")[0].strip(),
                        "text": card_html,
                        "status": col_key,
                    }

    if not dry_run:
        print(f"  Created {created} cards across {len(columns_order)} columns")

    return classified, card_state


def run_setup():
    """Interactive setup wizard for first-time configuration."""
    print("=" * 60)
    print("  Daily Board Sync — First-Time Setup")
    print("=" * 60)
    print()

    # 1. Atlassian credentials
    print("Step 1: Atlassian API Token")
    print("-" * 40)
    print("Generate a token at:")
    print("  https://id.atlassian.com/manage-profile/security/api-tokens")
    print()

    import webbrowser
    open_browser = input("Open that page in your browser now? [Y/n] ").strip().lower()
    if open_browser != "n":
        webbrowser.open("https://id.atlassian.com/manage-profile/security/api-tokens")
        print("  Opened in browser. Create a token labeled 'daily-board-sync'.")
        print()

    email = input("Your Atlassian email: ").strip()
    token = input("Your API token: ").strip()

    if not email or not token:
        print("ERROR: Email and token are required.")
        sys.exit(1)

    # Validate credentials and get account info
    print("\n  Validating credentials...")
    cred = b64encode(f"{email}:{token}".encode()).decode()
    headers = {"Authorization": f"Basic {cred}", "Content-Type": "application/json"}

    try:
        req = urllib.request.Request(
            "https://codazen.atlassian.net/rest/api/3/myself",
            headers=headers
        )
        with urllib.request.urlopen(req) as resp:
            user_data = json.loads(resp.read().decode())
    except urllib.error.HTTPError:
        alt_url = input("Default org (codazen) failed. Enter your Atlassian URL\n  (e.g., https://yourorg.atlassian.net): ").strip().rstrip("/")
        try:
            req = urllib.request.Request(f"{alt_url}/rest/api/3/myself", headers=headers)
            with urllib.request.urlopen(req) as resp:
                user_data = json.loads(resp.read().decode())
        except Exception as e:
            print(f"ERROR: Could not validate credentials: {e}")
            sys.exit(1)
    else:
        alt_url = "https://codazen.atlassian.net"

    account_id = user_data["accountId"]
    display_name = user_data["displayName"]
    first_name = display_name.split()[0].lower()
    print(f"  Authenticated as: {display_name} ({account_id})")

    # 2. Confluence space and sections
    print(f"\nStep 2: Confluence Configuration")
    print("-" * 40)
    space_key = input(f"Confluence space key [TEAM]: ").strip() or "TEAM"

    print("Enter the project section names from your weekly standup page")
    print("(comma-separated, e.g.: Family Center, AI, MDC)")
    sections_input = input("Sections: ").strip()
    sections = [s.strip() for s in sections_input.split(",") if s.strip()]

    if not sections:
        print("ERROR: At least one section is required.")
        sys.exit(1)

    # 3. Create WFS board
    print(f"\nStep 3: WorkflowShortcuts Board")
    print("-" * 40)
    board_name = input(f"Board name [{first_name.title()} Daily]: ").strip()
    if not board_name:
        board_name = f"{first_name.title()} Daily"

    print(f"  Creating board '{board_name}'...")
    board_data = {
        "title": board_name,
        "text": f"Daily task board for {display_name}. Auto-synced from Confluence.",
        "columns": [
            {"key": "todo", "title": "Todo"},
            {"key": "in-progress", "title": "In Progress"},
            {"key": "done", "title": "Done"}
        ]
    }
    req = urllib.request.Request(
        "https://workflowshortcuts.com/api/boards",
        data=json.dumps(board_data).encode(),
        headers={"Content-Type": "application/json"},
        method="POST"
    )
    try:
        with urllib.request.urlopen(req) as resp:
            board = json.loads(resp.read().decode())
        board_id = board["_id"]
        print(f"  Board created: https://workflowshortcuts.com/?boardId={board_id}")
    except Exception as e:
        print(f"ERROR: Could not create board: {e}")
        board_id = input("Enter an existing board ID instead: ").strip()

    # 4. Write config
    template_path = SCRIPT_DIR / "config.template.json"
    with open(template_path) as f:
        config = json.load(f)

    config["confluence"]["base_url"] = alt_url
    config["confluence"]["space_key"] = space_key
    config["confluence"]["sections"] = sections
    config["board"]["board_id"] = board_id
    config["user"]["atlassian_account_id"] = account_id
    config["user"]["display_name"] = display_name
    config["user"]["first_name_lower"] = first_name
    config["credentials"]["email"] = email
    config["credentials"]["token"] = token

    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)

    print(f"\nStep 4: Configuration saved to {CONFIG_PATH}")

    # 5. Cron setup
    print(f"\nStep 5: Scheduled Sync (optional)")
    print("-" * 40)
    setup_cron = input("Set up daily 6AM weekday sync? [Y/n] ").strip().lower()
    if setup_cron != "n":
        plist_name = f"com.daily-board-sync.{first_name}.plist"
        plist_path = Path.home() / "Library" / "LaunchAgents" / plist_name
        python_path = sys.executable
        script_path = SCRIPT_DIR / "sync.py"
        log_path = SCRIPT_DIR / "sync.log"

        plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.daily-board-sync.{first_name}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{python_path}</string>
        <string>{script_path}</string>
    </array>
    <key>StartCalendarInterval</key>
    <array>
        <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>2</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>3</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>4</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
        <dict><key>Weekday</key><integer>5</integer><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
    </array>
    <key>StandardOutPath</key>
    <string>{log_path}</string>
    <key>StandardErrorPath</key>
    <string>{log_path}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>"""

        with open(plist_path, "w") as f:
            f.write(plist_content)

        print(f"  Plist written to: {plist_path}")
        print(f"  To activate, run:")
        print(f"    launchctl load {plist_path}")

    # Done
    print()
    print("=" * 60)
    print("  Setup complete!")
    print(f"  Board: https://workflowshortcuts.com/?boardId={board_id}")
    print(f"  Run your first sync: python3 {SCRIPT_DIR / 'sync.py'}")
    print("=" * 60)


def main():
    parser = argparse.ArgumentParser(description="Bi-directional sync: Confluence ↔ WorkflowShortcuts board")
    parser.add_argument("--dry-run", action="store_true", help="Preview without writing")
    parser.add_argument("--date", help="Override week start date (YYYY-MM-DD)")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show detailed output")
    parser.add_argument("--push-only", action="store_true", help="Only push WFS changes to Confluence")
    parser.add_argument("--pull-only", action="store_true", help="Only pull Confluence to WFS")
    parser.add_argument("--approve-new", action="store_true", help="Insert new WFS-only cards into Confluence")
    parser.add_argument("--section", help="Confluence section for new tasks (e.g. 'AI', 'Family Center')")
    parser.add_argument("--setup", action="store_true", help="Run first-time setup wizard")
    args = parser.parse_args()

    if args.setup:
        run_setup()
        return

    if not CONFIG_PATH.exists():
        print("No config.json found. Running first-time setup...")
        print()
        run_setup()
        return

    config = load_config()
    email, token = load_credentials(config)
    auth = (email, token)

    week_start_day = config.get("confluence", {}).get("week_start_day", "monday")
    week_date = get_current_week_start(args.date, start_day=week_start_day)
    print(f"Daily Board Sync — {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"  Week: {week_date} ({week_start_day})")

    print("  Fetching Confluence page...")
    page = find_weekly_page(config, auth, week_date)
    if not page:
        print("  FAILED: Could not find weekly page", file=sys.stderr)
        sys.exit(1)

    html_content = page["body"]["storage"]["value"]
    print(f"  Page: {page['title']} (id={page['id']}, v{page['version']['number']})")

    # --- Step 1: Reverse sync (WFS → Confluence) ---
    pending_new_card_ids = []
    if not args.pull_only:
        print("  Checking WFS board for edits to push back...")
        state = load_state()
        board_id = config["board"]["board_id"]
        api_base = config["board"]["api_base"]
        docs_url = f"{api_base}/documents?boardId={board_id}"
        current_cards = api_request(docs_url) or []
        board_cards = [d for d in current_cards if d.get("boardId") == board_id]

        pushed = push_updates_to_confluence(config, auth, page, board_cards, state, dry_run=args.dry_run)
        if pushed and not args.dry_run:
            print("  Re-fetching Confluence page after push...")
            page = find_weekly_page(config, auth, week_date)
            html_content = page["body"]["storage"]["value"]

        new_cards = detect_new_wfs_cards(board_cards, state)
        if new_cards:
            if args.approve_new:
                section = (
                    args.section
                    or config.get("board", {}).get("default_new_task_section")
                    or config.get("confluence", {}).get("sections", ["General"])[0]
                )
                print(f"  Inserting {len(new_cards)} new task(s) into Confluence section '{section}'...")
                html = page["body"]["storage"]["value"]
                version = page["version"]["number"]
                user_id = config["user"]["atlassian_account_id"]
                inserted = 0
                for card in new_cards:
                    status = strip_html_tags(card.get("text", "")).strip() or "Added from WFS board"
                    result_html = insert_new_confluence_row(html, section, card["title"], status, user_id)
                    if result_html:
                        html = result_html
                        inserted += 1
                        print(f"    + {card['title']}")
                    else:
                        print(f"    WARN: Could not find section '{section}' for: {card['title']}", file=sys.stderr)

                if inserted and not args.dry_run:
                    base = config["confluence"]["base_url"]
                    today = datetime.now().strftime("%Y-%m-%d")
                    update_payload = {
                        "id": page["id"],
                        "type": "page",
                        "title": page["title"],
                        "body": {"storage": {"value": html, "representation": "storage"}},
                        "version": {
                            "number": version + 1,
                            "message": f"daily-board-sync: added {inserted} new task(s) from WFS ({today})"
                        }
                    }
                    result = api_request(
                        f"{base}/wiki/rest/api/content/{page['id']}",
                        method="PUT", data=update_payload, auth=auth
                    )
                    if result:
                        print(f"  Confluence updated to version {result['version']['number']}")
                        page = find_weekly_page(config, auth, week_date)
                        html_content = page["body"]["storage"]["value"]
                    else:
                        print("  WARN: Confluence update failed", file=sys.stderr)
            else:
                print(f"  PENDING: {len(new_cards)} new WFS-only card(s) not in Confluence:")
                for card in new_cards:
                    text_preview = strip_html_tags(card.get("text", "")).strip()[:80]
                    print(f"    • {card['title']}")
                    if text_preview:
                        print(f"      {text_preview}")
                print("  Re-run with --approve-new [--section SECTION] to add to Confluence.")
                pending_new_card_ids = [c["_id"] for c in new_cards]

    if args.push_only:
        print("  Done (push-only mode).")
        return

    # --- Step 2: Forward sync (Confluence → WFS) ---
    print("  Parsing tasks...")

    top_level_sections = config.get("confluence", {}).get("sections", [])
    if not top_level_sections:
        print("  ERROR: No sections configured. Run: python3 sync.py --setup", file=sys.stderr)
        sys.exit(1)

    def find_section_ranges(html, sections):
        ranges = []
        for section in sections:
            pattern = f'<ac:parameter ac:name="title">{re.escape(section)}</ac:parameter>'
            match = re.search(pattern, html)
            if match:
                ranges.append((match.start(), section))
        ranges.sort()
        result = []
        for i, (pos, name) in enumerate(ranges):
            end = ranges[i + 1][0] if i + 1 < len(ranges) else len(html)
            result.append((name, html[pos:end]))
        return result

    all_tasks = []
    for section_name, section_html in find_section_ranges(html_content, top_level_sections):
        task_parser = ConfluenceTaskParser(config["user"]["atlassian_account_id"])
        task_parser.current_section = section_name
        task_parser.feed(section_html)
        for t in task_parser.tasks:
            t["section"] = section_name
        all_tasks.extend(task_parser.tasks)

    tasks = all_tasks
    print(f"  Found {len(tasks)} tasks for {config['user']['display_name']}")

    if args.verbose:
        for t in tasks:
            role = "ASSIGNED" if t["is_assigned"] else "MENTIONED"
            print(f"    [{role:8s}] [{t['section']:20s}] {t['title']}")

    print("  Triaging and syncing board...")
    classified, card_state = sync_board(tasks, config, dry_run=args.dry_run, protected_card_ids=pending_new_card_ids)

    if not args.dry_run and card_state:
        save_state({
            "synced_at": datetime.now().isoformat(),
            "week_date": week_date,
            "cards": card_state,
        })

    summary = {}
    for _, col, _ in classified:
        summary[col] = summary.get(col, 0) + 1
    print(f"  Board: {' | '.join(f'{k}={v}' for k, v in summary.items())}")
    print("  Done.")


if __name__ == "__main__":
    main()
