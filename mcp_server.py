"""GradMap MCP server.

A thin translation layer between Claude (via MCP) and portal.gradmap.com/api.
Tool handlers never touch Postgres or decode a JWT to decide trust -- they
forward the student's GradMap access token as a Bearer header and let
GradMap's API be the source of truth. See auth_provider.py for how that
token gets minted in the first place (GradMap has no browser-redirect OAuth
endpoint yet, so this server bridges to its email/password login).

Scope for this first pass: `add_award` only, per gradmap-ai's current
add_award-first rollout plan.
"""

import html
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

from mcp.server.auth.middleware.auth_context import get_access_token
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response

from auth_provider import GradMapLoginError, GradMapOAuthProvider
from gradmap_client import GradMapAPIError, gradmap_request

# Claude's connector setup fetches this server from Anthropic's own
# infrastructure, not from your browser -- a bare localhost URL (http or
# https) isn't reachable at all. Run this behind a tunnel (e.g. `cloudflared
# tunnel --url http://127.0.0.1:8788`) and set MCP_ISSUER_URL to the public
# https URL the tunnel prints before starting this server; the tunnel
# terminates TLS with a real certificate, so the local hop stays plain HTTP.
ISSUER_URL = os.environ.get("MCP_ISSUER_URL", "http://127.0.0.1:8788")
RESOURCE_SERVER_URL = os.environ.get("MCP_RESOURCE_URL", f"{ISSUER_URL}/mcp")

# --- GradMap help-center context ----------------------------------------
#
# context/gradmap_context.json is a static export of GradMap's help center
# articles (url/title/author/published_date/content, no category field).
# It's local reference material, not per-student data, so it's loaded and
# searched in-process here rather than round-tripping through main.py/Postgres.

CONTEXT_FILE = Path(__file__).parent / "context" / "gradmap_context.json"
_WORD_RE = re.compile(r"[a-z0-9']+")
_STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "do", "does", "for",
    "from", "how", "i", "if", "in", "is", "it", "my", "of", "on", "or", "our",
    "should", "that", "the", "their", "there", "this", "to", "was", "we", "what",
    "when", "where", "which", "who", "why", "will", "with", "you", "your",
}

with open(CONTEXT_FILE, "r", encoding="utf-8") as _f:
    _HELP_ARTICLES = [
        a for a in json.load(_f).get("articles", []) if a.get("title") and a.get("url")
    ]


def _tokenize(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(text.lower()) if w not in _STOPWORDS and len(w) > 2]


def _search_help_articles(query: str, limit: int = 3) -> list[dict]:
    query_words = set(_tokenize(query))
    if not query_words:
        return []

    scored = []
    for article in _HELP_ARTICLES:
        title_words = _tokenize(article.get("title", ""))
        content = article.get("content", "")
        content_words = _tokenize(content)
        score = 3 * sum(title_words.count(w) for w in query_words) + sum(
            content_words.count(w) for w in query_words
        )
        if score > 0:
            scored.append((score, article, content))

    scored.sort(key=lambda item: item[0], reverse=True)

    results = []
    for _score, article, content in scored[:limit]:
        paragraphs = [p.strip() for p in content.split("\n\n") if p.strip()]
        best_paragraph = max(
            paragraphs,
            key=lambda p: sum(_tokenize(p).count(w) for w in query_words),
            default=content,
        )
        excerpt = best_paragraph[:800]
        results.append(
            {
                "title": article.get("title", ""),
                "url": article.get("url", ""),
                "published_date": article.get("published_date", ""),
                "excerpt": excerpt,
            }
        )
    return results


auth_provider = GradMapOAuthProvider()

server = MCPServer(
    "gradmap",
    title="GradMap",
    instructions=(
        "Helps a high school student manage their GradMap college-admissions profile. "
        "Every tool acts on the profile of whichever student is signed in -- always confirm "
        "ambiguous details with the student rather than guessing them."
    ),
    auth_server_provider=auth_provider,
    auth=AuthSettings(
        issuer_url=ISSUER_URL,
        resource_server_url=RESOURCE_SERVER_URL,
        client_registration_options=ClientRegistrationOptions(
            enabled=True,
            valid_scopes=["gradmap"],
            default_scopes=["gradmap"],
        ),
    ),
)


# --- login bridge: GradMap has no browser /authorize endpoint, so we render --
# --- our own form and call POST /auth/login server-side on submit -----------


_URL_RE = re.compile(r"https?://[^\s<>\"]+")


def _linkify(text: str) -> str:
    def _wrap(match: "re.Match[str]") -> str:
        url = match.group(0).rstrip(".,;:!?)")
        return f'<a href="{url}" target="_blank" rel="noopener noreferrer" style="color:#3346c9">{url}</a>'

    return _URL_RE.sub(_wrap, text)


def _login_page_html(flow_id: str, error: str | None = None) -> str:
    error_html = f'<p style="color:#b00020">{_linkify(error)}</p>' if error else ""
    return f"""
    <!doctype html>
    <html>
      <head><title>Sign in to GradMap</title></head>
      <body style="font-family: sans-serif; max-width: 360px; margin: 80px auto;">
        <h2>Sign in to GradMap</h2>
        <p>Claude needs your GradMap login to act on your behalf.</p>
        {error_html}
        <form method="post" action="{auth_provider.login_path}">
          <input type="hidden" name="flow_id" value="{flow_id}" />
          <label>Email<br/><input type="email" name="email" required style="width:100%" /></label><br/><br/>
          <label>Password<br/><input type="password" name="password" required style="width:100%" /></label><br/><br/>
          <button type="submit">Sign in</button>
        </form>
      </body>
    </html>
    """


@server.custom_route(auth_provider.login_path, methods=["GET"])
async def gradmap_login_page(request: Request) -> Response:
    flow_id = request.query_params.get("flow_id", "")
    if auth_provider.get_pending(flow_id) is None:
        return HTMLResponse(_login_page_html(flow_id, error="This login link has expired. Please reconnect from Claude."), status_code=400)
    return HTMLResponse(_login_page_html(flow_id))


@server.custom_route(auth_provider.login_path, methods=["POST"])
async def gradmap_login_submit(request: Request) -> Response:
    form = await request.form()
    flow_id = str(form.get("flow_id", ""))
    email = str(form.get("email", ""))
    password = str(form.get("password", ""))

    try:
        redirect_uri = await auth_provider.complete_login(flow_id, email, password)
    except GradMapLoginError as error:
        return HTMLResponse(_login_page_html(flow_id, error=error.message), status_code=401)

    return RedirectResponse(url=redirect_uri, status_code=302)


# --- dashboard page --------------------------------------------------------
#
# get_dashboard hands Claude a link, not embedded content -- MCP resource/
# widget rendering for custom connectors is unconfirmed (see git history for
# the earlier attempt), but a plain link is guaranteed to render. The page
# itself is a normal webpage this server serves directly (same pattern as the
# login page above): real JS, same-origin fetch() calls back to this same
# server, no CORS, no sandboxed-host dependency. It authenticates with a
# short-lived per-render session token (never the real GradMap OAuth token --
# that stays server-side) so a leaked/old link can't be used indefinitely.

CATEGORY_LABELS = {
    "essay_planning": "Essay planning",
    "course_planning": "Course planning",
    "major": "Major",
    "financial_aid": "Financial aid",
    "upcoming_events": "Upcoming events",
    "letters_of_recommendation": "Letters of recommendation",
}

_SECTION_META = {
    "due": {"urgency": "due_soon", "label": "Due soon", "sub": "needs attention right away"},
    "week": {"urgency": "coming_up", "label": "This week", "sub": "wrap up in the next 7 days"},
    "later": {"urgency": "later", "label": "Later", "sub": "future years / down the line"},
}
_SECTION_ORDER = ("due", "week", "later")
_URGENCY_TO_SECTION = {meta["urgency"]: key for key, meta in _SECTION_META.items()}

DASHBOARD_SESSION_TTL_SECONDS = 24 * 60 * 60
_dashboard_sessions: dict[str, dict] = {}


def _create_dashboard_session(student_id: str, access_token: str) -> str:
    session_id = secrets.token_urlsafe(24)
    _dashboard_sessions[session_id] = {
        "student_id": student_id,
        "access_token": access_token,
        "expires_at": time.time() + DASHBOARD_SESSION_TTL_SECONDS,
    }
    return session_id


def _get_dashboard_session(session_id: str) -> dict | None:
    session = _dashboard_sessions.get(session_id)
    if session is None:
        return None
    if session["expires_at"] < time.time():
        del _dashboard_sessions[session_id]
        return None
    return session


_CAL_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4.5" width="18" height="16" rx="2.5"/>'
    '<path d="M3 9.5h18"/><path d="M8 2.5v4M16 2.5v4"/></svg>'
)
_UNDO_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="M9 14 4 9l5-5"/>'
    '<path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/></svg>'
)
_CHEVRON_ICON_SVG = (
    '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
    'stroke-linecap="round" stroke-linejoin="round"><path d="m6 9 6 6 6-6"/></svg>'
)
_PLUS_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>'
)


def _dashboard_card_html(rec: dict) -> str:
    rec_id = rec.get("id")
    section = _URGENCY_TO_SECTION.get(rec.get("urgency_rank"), "later")
    status = rec.get("status") or "not_started"
    title = html.escape(str(rec.get("title") or ""))
    subtext = html.escape(str(rec.get("subtext") or ""))
    category_key = html.escape(rec.get("category") or "")
    category_label = html.escape(CATEGORY_LABELS.get(rec.get("category"), rec.get("category") or ""))
    estimated_time = html.escape(str(rec.get("estimated_time") or ""))
    link = rec.get("link")
    calendar_url = rec.get("google_calendar")

    link_html = (
        f'<a class="article-link" href="{html.escape(link)}" target="_blank" rel="noopener">Open &#8599;</a>'
        if link
        else ""
    )
    cal_html = (
        f'<a class="icon-btn" href="{html.escape(calendar_url)}" target="_blank" rel="noopener" '
        f'title="Add to Google Calendar" aria-label="Add to Google Calendar">{_CAL_ICON_SVG}</a>'
        if calendar_url
        else ""
    )

    def option(value, label):
        selected = " selected" if status == value else ""
        return f'<option value="{value}"{selected}>{label}</option>'

    is_done_class = " is-done" if status == "done" else ""
    checked = " checked" if status == "done" else ""

    return f"""
    <div class="card {section}{is_done_class}" data-id="{rec_id}" data-section="{section}" data-status="{status}">
      <div class="card-top">
        <label class="check-wrap"><input type="checkbox" class="done-check" aria-label="Mark complete"{checked}><span class="check-ui"></span></label>
        <div class="card-main">
          <div class="card-title">{title}</div>
          <p class="card-subtext">{subtext}</p>
          <div class="card-meta">
            <span class="chip category category-{category_key}">{category_label}</span>
            <span class="chip time">{estimated_time}</span>
            {link_html}
          </div>
        </div>
        <div class="card-side">
          {cal_html}
          <select class="status-select" aria-label="Task status">
            {option("not_started", "Not started")}
            {option("in_progress", "In progress")}
            {option("done", "Done")}
          </select>
          <button type="button" class="icon-btn undo-btn" title="Move back to active" aria-label="Move back to active">{_UNDO_ICON_SVG}</button>
        </div>
      </div>
    </div>
    """


_DASHBOARD_CSS = """
:root {
  --bg: #ffffff; --surface: #ffffff; --border: #e5e8f0; --border-soft: #eef0f6;
  --text: #1c2333; --text-muted: #656c7c; --text-faint: #8b90a3;
  --accent: #3b6fed; --accent-ink: #2354d1; --accent-tint: #edf1fe; --accent-contrast: #ffffff;
  --due: #c97a1f; --due-tint: #fdf1e2; --week: #2f9e58; --week-tint: #e7f7ec;
  --later: #7c5ce0; --later-tint: #efecfc; --done: #5b6472; --done-tint: #eef1f5;
  --track: #e7e9f3;
  --cat-essay-bg: #ede7fb; --cat-essay-ink: #6e56cf;
  --cat-course-bg: #e3eefc; --cat-course-ink: #2f6fed;
  --cat-major-bg: #fbe7f0; --cat-major-ink: #c23b79;
  --cat-aid-bg: #e3f6ea; --cat-aid-ink: #2f9e58;
  --cat-events-bg: #fbf0d2; --cat-events-ink: #9c7a1e;
  --cat-letters-bg: #e0f5f5; --cat-letters-ink: #1f8a8a;
  --shadow: 0 1px 2px rgba(30,34,60,.04), 0 8px 24px -12px rgba(30,34,60,.12);
  --shadow-modal: 0 24px 60px -20px rgba(20,22,40,.35);
  --focus: #3b6fed; --done-bg: #fafbfd; --scrim: rgba(16,18,32,.44); --danger: #c94a3f;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #14151e; --surface: #1b1d29; --border: #2b2d3d; --border-soft: #262838;
    --text: #e9eaf3; --text-muted: #a4a8bd; --text-faint: #767a91;
    --accent: #6f92ff; --accent-ink: #b7c6ff; --accent-tint: #232a45; --accent-contrast: #10142a;
    --due: #dda05a; --due-tint: #362c1c; --week: #6fc290; --week-tint: #1c3324;
    --later: #a695ef; --later-tint: #2b2646; --done: #9aa1b0; --done-tint: #23262f;
    --track: #2b2d3d;
    --cat-essay-bg: #2c2748; --cat-essay-ink: #b7a7f5;
    --cat-course-bg: #1f2c42; --cat-course-ink: #8ab0f7;
    --cat-major-bg: #3a2230; --cat-major-ink: #f0a3c6;
    --cat-aid-bg: #1c3324; --cat-aid-ink: #7fd39d;
    --cat-events-bg: #3a3320; --cat-events-ink: #e3c877;
    --cat-letters-bg: #1a3535; --cat-letters-ink: #7fd6d6;
    --shadow: 0 1px 2px rgba(0,0,0,.2), 0 12px 28px -14px rgba(0,0,0,.5);
    --shadow-modal: 0 24px 60px -20px rgba(0,0,0,.6);
    --focus: #9aa4ff; --done-bg: #191a24; --scrim: rgba(6,7,14,.6); --danger: #e28178;
  }
}
:root[data-theme="dark"] {
  --bg: #14151e; --surface: #1b1d29; --border: #2b2d3d; --border-soft: #262838;
  --text: #e9eaf3; --text-muted: #a4a8bd; --text-faint: #767a91;
  --accent: #6f92ff; --accent-ink: #b7c6ff; --accent-tint: #232a45; --accent-contrast: #10142a;
  --due: #dda05a; --due-tint: #362c1c; --week: #6fc290; --week-tint: #1c3324;
  --later: #a695ef; --later-tint: #2b2646; --done: #9aa1b0; --done-tint: #23262f;
  --track: #2b2d3d;
  --cat-essay-bg: #2c2748; --cat-essay-ink: #b7a7f5;
  --cat-course-bg: #1f2c42; --cat-course-ink: #8ab0f7;
  --cat-major-bg: #3a2230; --cat-major-ink: #f0a3c6;
  --cat-aid-bg: #1c3324; --cat-aid-ink: #7fd39d;
  --cat-events-bg: #3a3320; --cat-events-ink: #e3c877;
  --cat-letters-bg: #1a3535; --cat-letters-ink: #7fd6d6;
  --shadow: 0 1px 2px rgba(0,0,0,.2), 0 12px 28px -14px rgba(0,0,0,.5);
  --shadow-modal: 0 24px 60px -20px rgba(0,0,0,.6);
  --focus: #9aa4ff; --done-bg: #191a24; --scrim: rgba(6,7,14,.6); --danger: #e28178;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--text);
  font-family: "Source Sans 3", -apple-system, "Segoe UI", sans-serif; -webkit-font-smoothing: antialiased; }
.page { max-width: 780px; margin: 0 auto; padding: 40px 20px 80px; }
.banner { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 22px; }
.eyebrow { font-size: 12px; font-weight: 600; letter-spacing: .09em; text-transform: uppercase; color: var(--accent-ink); }
.head-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; margin-bottom: 28px; }
h1 { font-family: "Fraunces", Georgia, serif; font-weight: 600; font-size: clamp(28px,4vw,34px); margin: 4px 0 6px; text-wrap: balance; }
.lede { color: var(--text-muted); font-size: 15px; margin: 0; max-width: 44ch; }
.btn { font: inherit; font-weight: 600; font-size: 13.5px; border-radius: 10px; padding: 10px 16px; cursor: pointer;
  border: 1px solid transparent; display: inline-flex; align-items: center; gap: 7px; white-space: nowrap; }
.btn-primary { background: var(--accent); color: var(--accent-contrast); }
.btn-primary:hover { filter: brightness(1.06); }
.btn-primary:disabled { opacity: .6; cursor: not-allowed; }
.btn-ghost { background: transparent; color: var(--text-muted); border-color: var(--border); }
.btn-ghost:hover { background: var(--border-soft); }
.btn:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.stat-panel { background: var(--accent-tint); border: none; border-radius: 16px;
  padding: 20px 24px; display: grid; grid-template-columns: auto 1fr; gap: 20px 28px; align-items: center; margin-bottom: 32px; }
.stat-percent { font-variant-numeric: tabular-nums; font-size: 40px; font-weight: 700; line-height: 1; color: var(--accent-ink); }
.stat-percent span { display: block; font-size: 11.5px; font-weight: 600; color: var(--text-faint); letter-spacing: .04em; text-transform: uppercase; margin-top: 6px; }
.progress-track { grid-column: 1/-1; height: 6px; border-radius: 999px; background: var(--track); overflow: hidden; }
.progress-fill { height: 100%; border-radius: 999px; background: var(--accent); transition: width .25s ease; }
.stat-breakdown { display: flex; gap: 24px; flex-wrap: wrap; }
.stat-breakdown div { font-size: 13px; color: var(--text-muted); }
.stat-breakdown b { font-variant-numeric: tabular-nums; color: var(--text); font-weight: 700; margin-right: 5px; }
.section { margin-bottom: 22px; }
.section-head { display: flex; align-items: center; gap: 9px; margin-bottom: 12px;
  padding: 10px 16px; border-radius: 10px; background: var(--border-soft); }
.section[data-section="due"] .section-head { background: var(--due-tint); }
.section[data-section="week"] .section-head { background: var(--week-tint); }
.section[data-section="later"] .section-head { background: var(--later-tint); }
.done-section .section-head { background: var(--done-tint); }
.dot { width: 9px; height: 9px; border-radius: 50%; flex: none; }
.dot.due { background: var(--due); } .dot.week { background: var(--week); } .dot.later { background: var(--later); }
.dot.done { background: var(--done); }
.section-head h2 { font-size: 14px; font-weight: 700; margin: 0; }
.section[data-section="due"] .section-head h2 { color: var(--due); }
.section[data-section="week"] .section-head h2 { color: var(--week); }
.section[data-section="later"] .section-head h2 { color: var(--later); }
.done-section .section-head h2 { color: var(--done); }
.section-head .count { color: var(--text-faint); font-size: 12.5px; font-variant-numeric: tabular-nums; }
.cards { display: flex; flex-direction: column; gap: 10px; }
.card { background: var(--surface); border: 1px solid var(--border);
  border-radius: 12px; padding: 14px 16px; display: flex; flex-direction: column; gap: 8px; }
.card.is-done { background: var(--done-bg); }
.card.is-done .check-wrap { display: none; } .card.is-done .status-select { display: none; } .card.is-done .undo-btn { display: grid; }
.card-top { display: flex; align-items: flex-start; gap: 12px; }
.check-wrap { flex: none; padding-top: 1px; cursor: pointer; position: relative; }
.check-wrap input { position: absolute; opacity: 0; width: 20px; height: 20px; margin: 0; cursor: pointer; }
.check-ui { display: block; width: 20px; height: 20px; border-radius: 50%; border: 1.6px solid var(--border); background: var(--surface);
  position: relative; transition: background .15s ease, border-color .15s ease; }
.check-ui::after { content: ""; position: absolute; inset: 0; margin: auto; width: 10px; height: 6px;
  border-left: 2px solid #fff; border-bottom: 2px solid #fff; transform: translateY(-1px) rotate(-45deg) scale(0); transition: transform .15s ease; }
.check-wrap input:checked + .check-ui { background: var(--accent); border-color: var(--accent); }
.check-wrap input:checked + .check-ui::after { transform: translateY(-1px) rotate(-45deg) scale(1); }
.check-wrap input:focus-visible + .check-ui { outline: 2px solid var(--focus); outline-offset: 2px; }
.card-main { flex: 1; min-width: 0; }
.card-title { font-weight: 600; font-size: 15px; line-height: 1.35; }
.card.is-done .card-title { color: var(--text-faint); } .card.is-done .card-subtext { opacity: .75; }
.card-side { flex: none; display: flex; align-items: center; gap: 8px; }
.status-select { font: inherit; font-size: 12.5px; font-weight: 600; color: var(--text-muted); background: var(--surface);
  border: 1px solid var(--border); border-radius: 8px; padding: 5px 24px 5px 10px; cursor: pointer; appearance: none;
  background-image: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6' viewBox='0 0 10 6' fill='none'%3E%3Cpath d='M1 1l4 4 4-4' stroke='%238b90a3' stroke-width='1.5' stroke-linecap='round' stroke-linejoin='round'/%3E%3C/svg%3E");
  background-repeat: no-repeat; background-position: right 9px center; }
.status-select:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.icon-btn { flex: none; width: 30px; height: 30px; border-radius: 9px; border: 1px solid var(--border); background: var(--surface);
  color: var(--text-muted); display: grid; place-items: center; text-decoration: none; cursor: pointer;
  transition: background .15s ease, border-color .15s ease, color .15s ease, transform .1s ease; }
.icon-btn:hover { background: var(--accent-tint); border-color: var(--accent); color: var(--accent-ink); }
.icon-btn:active { transform: scale(.94); }
.icon-btn:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; }
.icon-btn svg { width: 16px; height: 16px; }
.undo-btn { display: none; }
.card-subtext { color: var(--text-muted); font-size: 13.5px; line-height: 1.5; margin: 0; }
.card-meta { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.chip { font-size: 11px; font-weight: 600; padding: 3px 9px; border-radius: 999px; }
.chip.category { background: var(--border-soft); color: var(--text-muted); }
.chip.category-essay_planning { background: var(--cat-essay-bg); color: var(--cat-essay-ink); }
.chip.category-course_planning { background: var(--cat-course-bg); color: var(--cat-course-ink); }
.chip.category-major { background: var(--cat-major-bg); color: var(--cat-major-ink); }
.chip.category-financial_aid { background: var(--cat-aid-bg); color: var(--cat-aid-ink); }
.chip.category-upcoming_events { background: var(--cat-events-bg); color: var(--cat-events-ink); }
.chip.category-letters_of_recommendation { background: var(--cat-letters-bg); color: var(--cat-letters-ink); }
.chip.time { background: transparent; border: 1px solid var(--border); color: var(--text-faint); font-weight: 500; }
.card-meta a.article-link { font-size: 12px; font-weight: 600; color: var(--accent-ink); text-decoration: none; }
.card-meta a.article-link:hover { text-decoration: underline; }
.card-meta a:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 3px; }
.done-section .section-head { cursor: pointer; user-select: none; }
.chevron { margin-left: auto; color: var(--text-faint); display: grid; place-items: center; transition: transform .18s ease;
  background: none; border: none; cursor: pointer; padding: 4px; }
.chevron svg { width: 16px; height: 16px; }
.done-section.collapsed .chevron { transform: rotate(-90deg); }
.done-section.collapsed .cards, .done-section.collapsed .empty-note { display: none; }
.empty-note { font-size: 13px; color: var(--text-faint); font-style: italic; padding: 4px 2px; }
.empty-note.is-hidden { display: none; }
.foot-note { margin-top: 34px; font-size: 12.5px; color: var(--text-faint); border-top: 1px solid var(--border-soft); padding-top: 16px; }
.modal-overlay { position: fixed; inset: 0; background: var(--scrim); display: none; align-items: flex-start; justify-content: center; padding: 8vh 16px; z-index: 50; }
.modal-overlay.open { display: flex; }
.modal { background: var(--surface); border-radius: 16px; box-shadow: var(--shadow-modal); width: 100%; max-width: 440px; padding: 22px 24px 20px; }
.modal-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; }
.modal-head h3 { font-family: "Fraunces", Georgia, serif; font-size: 19px; font-weight: 600; margin: 0; }
.modal-close { background: none; border: none; font-size: 20px; line-height: 1; color: var(--text-faint); cursor: pointer; padding: 4px; }
.modal-close:hover { color: var(--text); }
.modal-close:focus-visible { outline: 2px solid var(--focus); outline-offset: 2px; border-radius: 6px; }
.field { margin-bottom: 14px; }
.field label { display: block; font-size: 12.5px; font-weight: 600; color: var(--text-muted); margin-bottom: 5px; }
.field label .req { color: var(--danger); }
.field input[type="text"], .field input[type="url"], .field input[type="number"], .field textarea, .field select {
  width: 100%; font: inherit; font-size: 13.5px; color: var(--text); background: var(--bg);
  border: 1px solid var(--border); border-radius: 9px; padding: 9px 11px; }
.field textarea { resize: vertical; min-height: 64px; }
.field input:focus-visible, .field textarea:focus-visible, .field select:focus-visible { outline: 2px solid var(--focus); outline-offset: 1px; }
.field-row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.time-row { display: grid; grid-template-columns: 1fr 110px; gap: 8px; }
.char-count { text-align: right; font-size: 11px; color: var(--text-faint); margin-top: 3px; }
.modal-error { color: var(--danger); font-size: 12.5px; margin: -4px 0 10px; min-height: 1em; }
.modal-actions { display: flex; justify-content: flex-end; gap: 10px; margin-top: 6px; }
@media (prefers-reduced-motion: reduce) { .progress-fill, .icon-btn, .chevron { transition: none; } }
"""

_DASHBOARD_JS_TEMPLATE = """
(function () {
  "use strict";
  var SESSION_ID = __SESSION_ID_JSON__;
  var CATEGORY_LABELS = __CATEGORY_LABELS_JSON__;
  var CAL_ICON = __CAL_ICON_JSON__;
  var UNDO_ICON = __UNDO_ICON_JSON__;

  function escapeHtml(str) {
    var div = document.createElement("div");
    div.textContent = str == null ? "" : String(str);
    return div.innerHTML;
  }

  function apiFetch(path, options) {
    options = options || {};
    options.headers = Object.assign({ "X-Dashboard-Session": SESSION_ID }, options.headers || {});
    return fetch(path, options).then(function (res) {
      if (!res.ok) {
        return res.text().catch(function () { return ""; }).then(function (text) {
          throw new Error("Request failed (" + res.status + "): " + text);
        });
      }
      return res.status === 204 ? null : res.json();
    });
  }

  var SECTION_SELECTOR = {
    due: '.section[data-section="due"] .cards',
    week: '.section[data-section="week"] .cards',
    later: '.section[data-section="later"] .cards'
  };
  var doneCardsEl = document.getElementById("done-cards");
  var doneEmptyEl = document.getElementById("done-empty");
  var doneCountEl = document.getElementById("done-count");

  function updateStats() {
    var cards = document.querySelectorAll(".card");
    var total = cards.length, done = 0, dueOpen = 0;
    cards.forEach(function (c) {
      if (c.dataset.status === "done") done++;
      if (c.dataset.section === "due" && c.dataset.status !== "done") dueOpen++;
    });
    var percent = total ? Math.round((done / total) * 100) : 0;
    document.getElementById("stat-total").textContent = total;
    document.getElementById("stat-done").textContent = done;
    document.getElementById("stat-due").textContent = dueOpen;
    document.getElementById("stat-percent").innerHTML = percent + "%<span>Complete</span>";
    document.getElementById("progress-fill").style.width = percent + "%";
    doneCountEl.textContent = done;
    doneEmptyEl.classList.toggle("is-hidden", done > 0);
  }

  function moveCard(card, status) {
    card.dataset.status = status;
    card.querySelector(".status-select").value = status;
    card.querySelector(".done-check").checked = status === "done";
    card.classList.toggle("is-done", status === "done");
    var home = document.querySelector(SECTION_SELECTOR[card.dataset.section]);
    var target = status === "done" ? doneCardsEl : home;
    if (card.parentElement !== target) target.appendChild(card);
    updateStats();
  }

  function setStatus(card, status) {
    var previous = card.dataset.status;
    moveCard(card, status);
    apiFetch("/gradmap/dashboard/api/tasks/" + card.dataset.id + "/status", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: status })
    }).catch(function (err) {
      console.error(err);
      moveCard(card, previous);
      alert("Couldn't save that change. Please try again.");
    });
  }

  document.addEventListener("change", function (e) {
    var card = e.target.closest(".card");
    if (!card) return;
    if (e.target.classList.contains("done-check")) {
      setStatus(card, e.target.checked ? "done" : "not_started");
    } else if (e.target.classList.contains("status-select")) {
      setStatus(card, e.target.value);
    }
  });

  document.addEventListener("click", function (e) {
    var undoBtn = e.target.closest(".undo-btn");
    if (undoBtn) setStatus(undoBtn.closest(".card"), "not_started");
  });

  var doneSection = document.getElementById("done-section");
  var doneToggle = document.getElementById("done-toggle");
  function toggleDone() {
    var collapsed = doneSection.classList.toggle("collapsed");
    doneToggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
  }
  doneToggle.addEventListener("click", toggleDone);
  doneToggle.addEventListener("keydown", function (e) {
    if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleDone(); }
  });

  var overlay = document.getElementById("task-modal-overlay");
  var addBtn = document.getElementById("add-task-btn");
  var form = document.getElementById("add-task-form");
  var descField = document.getElementById("task-desc");
  var charCount = document.getElementById("task-desc-count");
  var errorBox = document.getElementById("task-error");

  function updateCharCount() { charCount.textContent = descField.value.length; }
  descField.addEventListener("input", updateCharCount);

  function openModal() {
    errorBox.textContent = "";
    overlay.classList.add("open");
    document.getElementById("task-title").focus();
    document.addEventListener("keydown", onKeydown);
  }
  function closeModal() {
    overlay.classList.remove("open");
    document.removeEventListener("keydown", onKeydown);
    form.reset();
    updateCharCount();
    addBtn.focus();
  }
  function onKeydown(e) { if (e.key === "Escape") closeModal(); }

  addBtn.addEventListener("click", openModal);
  document.getElementById("task-close-btn").addEventListener("click", closeModal);
  document.getElementById("task-cancel-btn").addEventListener("click", closeModal);
  overlay.addEventListener("click", function (e) { if (e.target === overlay) closeModal(); });

  function buildCardHTML(d) {
    var linkHtml = d.link
      ? '<a class="article-link" href="' + escapeHtml(d.link) + '" target="_blank" rel="noopener">Open &#8599;</a>'
      : "";
    var calHtml = d.google_calendar
      ? '<a class="icon-btn" href="' + escapeHtml(d.google_calendar) + '" target="_blank" rel="noopener" title="Add to Google Calendar" aria-label="Add to Google Calendar">' + CAL_ICON + "</a>"
      : "";
    return (
      '<div class="card ' + d.section + '" data-id="' + d.id + '" data-section="' + d.section + '" data-status="not_started">' +
        '<div class="card-top">' +
          '<label class="check-wrap"><input type="checkbox" class="done-check" aria-label="Mark complete"><span class="check-ui"></span></label>' +
          '<div class="card-main">' +
            '<div class="card-title">' + escapeHtml(d.title) + "</div>" +
            '<p class="card-subtext">' + escapeHtml(d.subtext || "") + "</p>" +
            '<div class="card-meta">' +
              '<span class="chip category category-' + escapeHtml(d.category || "") + '">' + escapeHtml(d.categoryLabel || "") + "</span>" +
              '<span class="chip time">' + escapeHtml(d.estimated_time || "") + "</span>" +
              linkHtml +
            "</div>" +
          "</div>" +
          '<div class="card-side">' +
            calHtml +
            '<select class="status-select" aria-label="Task status">' +
              '<option value="not_started">Not started</option>' +
              '<option value="in_progress">In progress</option>' +
              '<option value="done">Done</option>' +
            "</select>" +
            '<button type="button" class="icon-btn undo-btn" title="Move back to active" aria-label="Move back to active">' + UNDO_ICON + "</button>" +
          "</div>" +
        "</div>" +
      "</div>"
    );
  }

  form.addEventListener("submit", function (e) {
    e.preventDefault();
    if (!form.reportValidity()) return;

    var title = document.getElementById("task-title").value.trim();
    var subtext = descField.value.trim();
    var link = document.getElementById("task-link").value.trim();
    var category = document.getElementById("task-category").value;
    var section = document.getElementById("task-when").value;
    var timeValue = document.getElementById("task-time-value").value.trim();
    var timeUnit = document.getElementById("task-time-unit").value;
    var unitLabel = timeUnit === "hours" ? (timeValue === "1" ? "hr" : "hrs") : "min";
    var estimatedTime = timeValue + " " + unitLabel;

    var submitBtn = form.querySelector('button[type="submit"]');
    submitBtn.disabled = true;
    errorBox.textContent = "";

    apiFetch("/gradmap/dashboard/api/tasks", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title, subtext: subtext, link: link || null,
        category: category, section: section, estimated_time: estimatedTime
      })
    }).then(function (created) {
      var data = {
        id: created.id,
        section: section,
        title: title,
        subtext: subtext,
        link: link,
        category: category,
        categoryLabel: CATEGORY_LABELS[category] || category,
        estimated_time: estimatedTime,
        google_calendar: created.google_calendar
      };
      document.querySelector(SECTION_SELECTOR[section]).insertAdjacentHTML("afterbegin", buildCardHTML(data));
      updateStats();
      closeModal();
    }).catch(function (err) {
      console.error(err);
      errorBox.textContent = "Couldn't add that task. Please try again.";
    }).finally(function () {
      submitBtn.disabled = false;
    });
  });

  updateStats();
})();
"""


def _render_dashboard_page(session_id: str, recommendations: list) -> str:
    total = len(recommendations)
    done = sum(1 for r in recommendations if (r.get("status") or "not_started") == "done")
    due_open = sum(
        1
        for r in recommendations
        if r.get("urgency_rank") == "due_soon" and (r.get("status") or "not_started") != "done"
    )
    percent = round((done / total) * 100) if total else 0

    active_by_section = {key: [] for key in _SECTION_ORDER}
    done_cards = []
    for rec in recommendations:
        if (rec.get("status") or "not_started") == "done":
            done_cards.append(rec)
        else:
            active_by_section[_URGENCY_TO_SECTION.get(rec.get("urgency_rank"), "later")].append(rec)

    def render_section(key):
        meta = _SECTION_META[key]
        cards_html = "\n".join(_dashboard_card_html(r) for r in active_by_section[key])
        return f"""
        <div class="section" data-section="{key}">
          <div class="section-head">
            <span class="dot {key}"></span>
            <h2>{meta['label']}</h2>
            <span class="count">{meta['sub']}</span>
          </div>
          <div class="cards">{cards_html}</div>
        </div>
        """

    sections_html = "\n".join(render_section(key) for key in _SECTION_ORDER)
    done_cards_html = "\n".join(_dashboard_card_html(r) for r in done_cards)
    done_empty_class = "is-hidden" if done_cards else ""

    js = (
        _DASHBOARD_JS_TEMPLATE.replace("__SESSION_ID_JSON__", json.dumps(session_id))
        .replace("__CATEGORY_LABELS_JSON__", json.dumps(CATEGORY_LABELS))
        .replace("__CAL_ICON_JSON__", json.dumps(_CAL_ICON_SVG))
        .replace("__UNDO_ICON_JSON__", json.dumps(_UNDO_ICON_SVG))
    )

    return f"""<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>My Plan</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Fraunces:opsz,wght@9..144,500;9..144,600&family=Source+Sans+3:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>{_DASHBOARD_CSS}</style>
</head>
<body>
  <div class="page">
    <div class="banner"><span class="eyebrow">GradMap &middot; My Plan</span></div>
    <div class="head-row">
      <div>
        <h1>My Plan</h1>
        <p class="lede">Your personalized next steps for college applications, generated from your profile and updated as it grows.</p>
      </div>
      <button type="button" class="btn btn-primary" id="add-task-btn">{_PLUS_ICON_SVG} Add task</button>
    </div>

    <div class="stat-panel">
      <div class="stat-percent" id="stat-percent">{percent}%<span>Complete</span></div>
      <div class="stat-breakdown">
        <div><b id="stat-total">{total}</b>tasks total</div>
        <div><b id="stat-due">{due_open}</b>due soon</div>
        <div><b id="stat-done">{done}</b>completed</div>
      </div>
      <div class="progress-track"><div class="progress-fill" id="progress-fill" style="width: {percent}%"></div></div>
    </div>

    {sections_html}

    <div class="section done-section" id="done-section" data-section="done">
      <div class="section-head" id="done-toggle" role="button" tabindex="0" aria-expanded="true" aria-controls="done-cards">
        <span class="dot done"></span>
        <h2>Done</h2>
        <span class="count" id="done-count">{done}</span>
        <button type="button" class="chevron" aria-hidden="true" tabindex="-1">{_CHEVRON_ICON_SVG}</button>
      </div>
      <p class="empty-note {done_empty_class}" id="done-empty">Nothing completed yet &mdash; checked-off tasks land here.</p>
      <div class="cards" id="done-cards">{done_cards_html}</div>
    </div>

    <p class="foot-note">Changes here save directly to your GradMap account. This link expires after 24 hours -- ask Claude to show your dashboard again for a fresh one.</p>
  </div>

  <div class="modal-overlay" id="task-modal-overlay">
    <div class="modal" role="dialog" aria-modal="true" aria-labelledby="modal-title">
      <div class="modal-head">
        <h3 id="modal-title">Add a task</h3>
        <button type="button" class="modal-close" id="task-close-btn" aria-label="Close">&times;</button>
      </div>
      <form id="add-task-form" novalidate>
        <div class="field">
          <label for="task-title">Task name <span class="req">*</span></label>
          <input type="text" id="task-title" placeholder="e.g. Register for the October SAT" required maxlength="120" />
        </div>
        <div class="field">
          <label for="task-desc">Short description <span class="req">*</span></label>
          <textarea id="task-desc" placeholder="Why this matters / what to do" required maxlength="200"></textarea>
          <div class="char-count"><span id="task-desc-count">0</span>/200</div>
        </div>
        <div class="field">
          <label for="task-link">Link (optional)</label>
          <input type="url" id="task-link" placeholder="https://..." />
        </div>
        <div class="field field-row">
          <div>
            <label for="task-category">Category <span class="req">*</span></label>
            <select id="task-category" required>
              <option value="" disabled selected>Choose one</option>
              <option value="course_planning">Course planning</option>
              <option value="essay_planning">Essay planning</option>
              <option value="major">Major</option>
              <option value="financial_aid">Financial aid</option>
              <option value="upcoming_events">Upcoming events</option>
              <option value="letters_of_recommendation">Letters of recommendation</option>
            </select>
          </div>
          <div>
            <label for="task-when">When <span class="req">*</span></label>
            <select id="task-when" required>
              <option value="" disabled selected>Choose one</option>
              <option value="due">Due soon</option>
              <option value="week">This week</option>
              <option value="later">Later</option>
            </select>
          </div>
        </div>
        <div class="field">
          <label for="task-time-value">Estimated time <span class="req">*</span></label>
          <div class="time-row">
            <input type="number" id="task-time-value" placeholder="e.g. 45" min="1" required />
            <select id="task-time-unit">
              <option value="minutes">minutes</option>
              <option value="hours">hours</option>
            </select>
          </div>
        </div>
        <p class="modal-error" id="task-error"></p>
        <div class="modal-actions">
          <button type="button" class="btn btn-ghost" id="task-cancel-btn">Cancel</button>
          <button type="submit" class="btn btn-primary">Add task</button>
        </div>
      </form>
    </div>
  </div>

  <script>{js}</script>
</body>
</html>"""


@server.custom_route("/gradmap/dashboard", methods=["GET"])
async def dashboard_page(request: Request) -> Response:
    session_id = request.query_params.get("session", "")
    session = _get_dashboard_session(session_id)
    if session is None:
        return HTMLResponse(
            "<p>This dashboard link has expired. Ask Claude to show your dashboard again.</p>",
            status_code=401,
        )

    try:
        result = await gradmap_request(
            "GET", f"/students/{session['student_id']}/recommendations", session["access_token"]
        )
    except GradMapAPIError as error:
        return HTMLResponse(f"<p>Couldn't load your dashboard: {html.escape(error.detail)}</p>", status_code=502)

    return HTMLResponse(_render_dashboard_page(session_id, result.get("recommendations", [])))


def _require_dashboard_session(request: Request) -> dict:
    session = _get_dashboard_session(request.headers.get("x-dashboard-session", ""))
    if session is None:
        raise PermissionError("Dashboard session expired")
    return session


@server.custom_route("/gradmap/dashboard/api/tasks/{task_id}/status", methods=["PATCH"])
async def dashboard_update_status(request: Request) -> Response:
    try:
        session = _require_dashboard_session(request)
    except PermissionError:
        return JSONResponse({"error": "Session expired"}, status_code=401)

    task_id = request.path_params["task_id"]
    body = await request.json()

    try:
        result = await gradmap_request(
            "PATCH",
            f"/students/{session['student_id']}/recommendations/{task_id}",
            session["access_token"],
            json={"status": body.get("status")},
        )
    except GradMapAPIError as error:
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    return JSONResponse(result)


@server.custom_route("/gradmap/dashboard/api/tasks", methods=["POST"])
async def dashboard_add_task(request: Request) -> Response:
    try:
        session = _require_dashboard_session(request)
    except PermissionError:
        return JSONResponse({"error": "Session expired"}, status_code=401)

    body = await request.json()
    urgency_rank = _SECTION_META.get(body.get("section"), {}).get("urgency")

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{session['student_id']}/recommendations/custom",
            session["access_token"],
            json={
                "title": body.get("title"),
                "subtext": body.get("subtext"),
                "link": body.get("link"),
                "category": body.get("category"),
                "urgency_rank": urgency_rank,
                "estimated_time": body.get("estimated_time"),
            },
        )
    except GradMapAPIError as error:
        return JSONResponse({"error": error.detail}, status_code=error.status_code)

    return JSONResponse(result)


# --- tools -------------------------------------------------------------


def _current_student_id() -> str:
    access_token = get_access_token()
    if access_token is None or not access_token.subject:
        raise RuntimeError("Not signed in to GradMap. Please reconnect this MCP server and sign in again.")
    return access_token.subject


@server.tool(
    description=(
        "Show the signed-in student's GradMap dashboard by giving them a link to their live, "
        "interactive task list (AI-generated recommendations plus anything they've added), "
        "covering essay planning, course planning, major exploration, financial aid, upcoming "
        "events, and letters of recommendation. Call this whenever the student asks what they "
        "should work on next, checks their dashboard, or wants an overview of their progress.\n\n"
        "This tool deliberately returns ONLY dashboard_url, total_tasks, and due_soon_tasks -- "
        "no per-item data. That is the complete, correct response, not a partial or degraded one; "
        "never call this tool again expecting more detail, and never tell the student GradMap "
        "sent back a 'lighter' or 'partial' result. The full list, with checkboxes and status "
        "controls, lives on the page itself.\n\n"
        "Your entire reply must be 1-2 sentences in this shape: render dashboard_url as a "
        "markdown link with real link text (e.g. '[Open your dashboard](<dashboard_url>)'), state "
        "the total_tasks/due_soon_tasks counts, and invite the student to ask questions or say "
        "what they'd like to do. Do NOT enumerate, list, or tabulate individual recommendations, "
        "and do NOT offer to fetch more detail -- there is no more detail to fetch from this "
        "tool. Only describe one specific recommendation in text if the student names it."
    )
)
async def get_dashboard() -> dict:
    print("[get_dashboard] tool invoked")
    try:
        student_id = _current_student_id()
        access_token = get_access_token()
        print(f"[get_dashboard] student_id={student_id!r}")

        try:
            result = await gradmap_request(
                "POST",
                f"/students/{student_id}/recommendations",
                access_token.token,
            )
        except GradMapAPIError as error:
            raise RuntimeError(f"GradMap couldn't load the dashboard ({error.status_code}): {error.detail}")

        session_id = _create_dashboard_session(student_id, access_token.token)
        recommendations = result.get("recommendations", [])
        total = len(recommendations)
        due_soon = sum(1 for r in recommendations if r.get("urgency_rank") == "due_soon")
    except Exception:
        import traceback

        print("[get_dashboard] FAILED:")
        traceback.print_exc()
        raise

    return {
        "dashboard_url": f"{ISSUER_URL}/gradmap/dashboard?session={session_id}",
        "total_tasks": total,
        "due_soon_tasks": due_soon,
    }


@server.tool(
    description=(
        "Alias for get_dashboard -- trigger this when the student types '/dashboard' (or "
        "otherwise uses that exact shorthand) to pull up their GradMap dashboard. This tool does "
        "nothing on its own; it just calls get_dashboard and returns the same result, so follow "
        "get_dashboard's own reply instructions afterward (share dashboard_url as a markdown "
        "link plus the counts, don't list individual recommendations)."
    )
)
async def dashboard() -> dict:
    return await get_dashboard()


@server.tool(
    description=(
        "Add a new honor or award to the signed-in student's GradMap profile. Gather the details "
        "through a natural back-and-forth conversation -- do NOT dump every field into one long "
        "question. Ask a couple of related things at a time, react briefly to what the student "
        "says, then move to the next group. A natural flow: (1) start with the honor's exact "
        "title and a short answer to what they actually did to earn it (action_to_achieve); (2) "
        "then ask whether it's 'Academic' or 'Non-academic' (honor_type) and how wide the "
        "recognition was -- school, state, national, or international (recognition_level), "
        "asking which fits best rather than guessing; (3) then ask which grade level(s) they "
        "received it in (grade_levels) and a short description of who qualifies for it / what "
        "the requirements were (eligibility_requirements). Never guess honor_type, "
        "recognition_level, or grade_levels -- a wrong guess misrepresents the honor on the "
        "student's college applications. action_to_achieve and eligibility_requirements are "
        "required too; never skip asking for them or leave them blank. Leave the "
        "include_in_*_app flags at their default (true, included everywhere) unless the student "
        "says they don't want this honor listed on a specific application. Only call this tool "
        "once the conversation has naturally covered all the needed fields."
    )
)
async def add_award(
    honor_title: str,
    honor_type: Literal["Academic", "Non-academic"],
    recognition_level: Literal["school", "state", "national", "international"],
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]],
    action_to_achieve: str,
    eligibility_requirements: str,
    include_in_common_app: bool = True,
    include_in_uc_app: bool = True,
    include_in_csu_app: bool = True,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/honors",
            access_token.token,
            json={
                "honor_title": honor_title,
                "honor_type": honor_type,
                "recognition_level": recognition_level,
                "grade_levels": grade_levels,
                "action_to_achieve": action_to_achieve,
                "eligibility_requirements": eligibility_requirements,
                "include_in_common_app": include_in_common_app,
                "include_in_uc_app": include_in_uc_app,
                "include_in_csu_app": include_in_csu_app,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't save this award ({error.status_code}): {error.detail}")

    return {"added": True, "honor": result}


@server.tool(
    description=(
        "Add a new extracurricular activity, club, volunteer work, job, or academic program to "
        "the signed-in student's GradMap profile. Gather the details through a natural "
        "back-and-forth conversation -- do NOT dump every field into one long question. Ask a "
        "couple of related things at a time, react briefly to what the student says, then move "
        "to the next group. A natural flow: (1) start with the organization/program name and "
        "what they did there, in their own words (this covers position_description and "
        "description); (2) once you know roughly what it is, ask what category it falls under -- "
        "'Academic Activity', 'Club/Organization', 'Volunteer Work', 'Work', 'Sports', etc. -- and "
        "the closest UC application category ('Educational Prep', 'Extracurricular Activity', "
        "'Volunteer/Community Service', 'Work Experience'), asking which fits best rather than "
        "guessing; (3) then ask about their role -- was it a leadership position, which grade "
        "level(s) were they involved, and any notable distinctions or awards within the activity; "
        "(4) then ask about time commitment -- when during the year it happens (school year, "
        "a break, or all year), hours per week, and weeks per year; (5) wrap up by asking if "
        "they're still doing it and whether they plan to continue. Never guess category, "
        "category_uc, activity_type, grade_levels, or timing -- these materially affect how the "
        "activity reads on a college application, so always ask rather than inferring. If the "
        "activity is a paid job, also ask for the employer description and set is_paid_work to "
        "true; start_date/end_date/hours_per_week_low/hours_per_week_high only apply to paid "
        "work and should otherwise be left blank. Only call this tool once the conversation has "
        "naturally covered all the needed fields."
    )
)
async def add_activity(
    program_name: str,
    category: str,
    category_uc: str,
    activity_type: str,
    position_description: str,
    is_leadership_role: bool,
    grade_levels: list[Literal["9", "10", "11", "12", "post_graduate"]],
    timing: Literal["during_school_year", "during_break", "all_year"],
    hours_per_week: float,
    weeks_per_year: float,
    description: str,
    is_currently_participating: bool = True,
    intends_to_continue: bool = False,
    notable_distinctions: str | None = None,
    is_paid_work: bool = False,
    organization_description: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    hours_per_week_low: float | None = None,
    hours_per_week_high: float | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/activities",
            access_token.token,
            json={
                "program_name": program_name,
                "category": category,
                "category_uc": category_uc,
                "activity_type": activity_type,
                "position_description": position_description,
                "is_leadership_role": is_leadership_role,
                "grade_levels": grade_levels,
                "timing": timing,
                "hours_per_week": hours_per_week,
                "weeks_per_year": weeks_per_year,
                "description": description,
                "is_currently_participating": is_currently_participating,
                "intends_to_continue": intends_to_continue,
                "notable_distinctions": notable_distinctions,
                "is_paid_work": is_paid_work,
                "organization_description": organization_description,
                "start_date": start_date,
                "end_date": end_date,
                "hours_per_week_low": hours_per_week_low,
                "hours_per_week_high": hours_per_week_high,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't save this activity ({error.status_code}): {error.detail}")

    return {"added": True, "activity": result}


@server.tool(
    description=(
        "Add a new SAT test score to the signed-in student's GradMap profile. Before calling "
        "this tool, ask the student directly for the test date, total score, math score, and "
        "reading/writing score for the test they want to report -- never guess these. Also ask "
        "whether they have a future SAT test already scheduled; if so, ask for that date too and "
        "pass has_future_test=true with future_test_date set, otherwise leave has_future_test "
        "false and future_test_date unset. Don't ask for the student's College Board ID up "
        "front -- call this tool first without collegeboard_id. If the student doesn't have one "
        "on file yet, this tool will fail specifically because of that; only then ask the "
        "student for their College Board ID and call this tool again with collegeboard_id set."
    )
)
async def add_sat_score(
    test_date: str,
    total_score: int,
    math_score: int,
    reading_writing_score: int,
    collegeboard_id: str | None = None,
    has_future_test: bool = False,
    future_test_date: str | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/sat-scores",
            access_token.token,
            json={
                "test_date": test_date,
                "total_score": total_score,
                "math_score": math_score,
                "reading_writing_score": reading_writing_score,
                "collegeboard_id": collegeboard_id,
                "has_future_test": has_future_test,
                "future_test_date": future_test_date,
            },
        )
    except GradMapAPIError as error:
        if error.status_code == 409:
            raise RuntimeError(
                "This student doesn't have a College Board ID on file yet. Ask the student for "
                "their College Board ID, then call add_sat_score again with collegeboard_id set."
            )
        raise RuntimeError(f"GradMap couldn't save this SAT score ({error.status_code}): {error.detail}")

    return {"added": True, "sat_score": result}


@server.tool(
    description=(
        "Add a new ACT test score to the signed-in student's GradMap profile. Gather the "
        "details through a natural back-and-forth conversation, not one long question-dump. A "
        "natural flow: (1) ask for the test date and composite score; (2) ask for the four "
        "section scores -- English, Math, Reading, and Science -- these can all go in one "
        "message since they're closely related; (3) ask whether they took the writing section "
        "on that test, and only if so, ask for the writing score too; (4) ask whether they have "
        "a future ACT test already scheduled, and if so ask for that date and pass "
        "has_future_test=true with future_test_date set, otherwise leave has_future_test false "
        "and future_test_date unset. Never guess any of these scores or dates. Don't ask for "
        "the student's ACT ID number up front -- call this tool first without act_id_number. If "
        "the student doesn't have one on file yet, this tool will fail specifically because of "
        "that; only then ask the student for their ACT ID number and call this tool again with "
        "act_id_number set."
    )
)
async def add_act_score(
    test_date: str,
    composite_score: int,
    english_score: int,
    math_score: int,
    reading_score: int,
    science_score: int,
    took_writing_section: bool = False,
    writing_score: int | None = None,
    act_id_number: str | None = None,
    has_future_test: bool = False,
    future_test_date: str | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/act-scores",
            access_token.token,
            json={
                "test_date": test_date,
                "composite_score": composite_score,
                "english_score": english_score,
                "math_score": math_score,
                "reading_score": reading_score,
                "science_score": science_score,
                "took_writing_section": took_writing_section,
                "writing_score": writing_score,
                "act_id_number": act_id_number,
                "has_future_test": has_future_test,
                "future_test_date": future_test_date,
            },
        )
    except GradMapAPIError as error:
        if error.status_code == 409:
            raise RuntimeError(
                "This student doesn't have an ACT ID number on file yet. Ask the student for "
                "their ACT ID number, then call add_act_score again with act_id_number set."
            )
        raise RuntimeError(f"GradMap couldn't save this ACT score ({error.status_code}): {error.detail}")

    return {"added": True, "act_score": result}


@server.tool(
    description=(
        "Add a custom task to the signed-in student's GradMap dashboard, separate from the "
        "AI-generated recommendations -- same form as the dashboard page's 'Add a task' dialog. "
        "Before calling this, ask the student for every one of these, in order, and do not guess "
        "any of them:\n"
        "1. Task name -- a short title.\n"
        "2. Short description -- why it matters / what to do (this is subtext).\n"
        "3. Link (optional) -- only if they have one; otherwise leave it out.\n"
        "4. Category -- one of: essay_planning, course_planning, major, financial_aid, "
        "upcoming_events, letters_of_recommendation. Ask which fits, don't infer it from the "
        "title.\n"
        "5. When -- due_soon, coming_up (this week), or later. Ask which, don't guess urgency.\n"
        "6. Estimated time -- ask for a number and a unit (minutes or hours), then combine them "
        "into a string like '45 minutes' or '2 hours' for estimated_time.\n"
        "Only the link is optional; task name, description, category, when, and estimated time "
        "are all required and must come from the student, not be assumed."
    )
)
async def add_recommendation(
    title: str,
    subtext: str,
    category: Literal[
        "essay_planning",
        "course_planning",
        "major",
        "financial_aid",
        "upcoming_events",
        "letters_of_recommendation",
    ],
    urgency_rank: Literal["due_soon", "coming_up", "later"],
    estimated_time: str,
    link: str | None = None,
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "POST",
            f"/students/{student_id}/recommendations/custom",
            access_token.token,
            json={
                "title": title,
                "subtext": subtext,
                "link": link,
                "category": category,
                "urgency_rank": urgency_rank,
                "estimated_time": estimated_time,
            },
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't add that task ({error.status_code}): {error.detail}")

    return {"added": True, "recommendation": result}


@server.tool(
    description=(
        "Remove a recommendation or task from the signed-in student's active GradMap dashboard "
        "list by marking it done (this does not delete it from the database -- it moves it into "
        "the Done section, same as checking it off on the dashboard page). Before calling this, "
        "make sure you've shown the student their current list (e.g. via get_dashboard) and "
        "they've explicitly confirmed which one by its title. Use the exact numeric "
        "recommendation_id from that list -- never guess or infer an id."
    )
)
async def remove_recommendation(recommendation_id: int) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "PATCH",
            f"/students/{student_id}/recommendations/{recommendation_id}",
            access_token.token,
            json={"status": "done"},
        )
    except GradMapAPIError as error:
        if error.status_code == 404:
            raise RuntimeError(
                f"No recommendation with id {recommendation_id} was found for this student. "
                "Double-check the id from a recent get_dashboard call."
            )
        raise RuntimeError(f"GradMap couldn't update that recommendation ({error.status_code}): {error.detail}")

    return result


@server.tool(
    description=(
        "Update the status of a recommendation or task on the signed-in student's GradMap "
        "dashboard -- e.g. mark it in_progress once they've started it, or move it back to "
        "not_started. (To mark something done, prefer remove_recommendation, which does the same "
        "PATCH with status=done and reads more naturally when the student says they finished "
        "something.) Before calling this, make sure you've shown the student their current list "
        "(e.g. via get_dashboard) and they've confirmed which task by its title. Use the exact "
        "numeric recommendation_id from that list -- never guess or infer an id."
    )
)
async def edit_recommendation(
    recommendation_id: int,
    status: Literal["not_started", "in_progress", "done"],
) -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "PATCH",
            f"/students/{student_id}/recommendations/{recommendation_id}",
            access_token.token,
            json={"status": status},
        )
    except GradMapAPIError as error:
        if error.status_code == 404:
            raise RuntimeError(
                f"No recommendation with id {recommendation_id} was found for this student. "
                "Double-check the id from a recent get_dashboard call."
            )
        raise RuntimeError(f"GradMap couldn't update that recommendation ({error.status_code}): {error.detail}")

    return result


@server.tool(
    description=(
        "Look up the signed-in student's saved college list from GradMap -- every school "
        "they've added, grouped by list name (e.g. 'My Colleges', or a custom list like "
        "'Mom's list'), each with its likelihood category (reach/target/likely/etc, when set), "
        "admission result (not_yet/applied/submitted/accepted/committed/etc), city/state, and "
        "intended major if one was set for that school. Use this to answer questions like 'what "
        "colleges am I applying to' or 'what's on my list', or to make other advice more "
        "specific to schools the student has actually saved (e.g. UC-specific guidance for UC "
        "schools on their list). This is read-only -- there's no tool yet to add or remove a "
        "college from the list, so don't imply you can do that."
    )
)
async def get_college_list() -> dict:
    student_id = _current_student_id()
    access_token = get_access_token()

    try:
        result = await gradmap_request(
            "GET",
            f"/students/{student_id}/college-list",
            access_token.token,
        )
    except GradMapAPIError as error:
        raise RuntimeError(f"GradMap couldn't load the college list ({error.status_code}): {error.detail}")

    return result


@server.tool(
    description=(
        "Search GradMap's own help center articles for guidance on college planning, "
        "applications, financial aid, testing, essays, activities, and the general admissions "
        "process. Call this whenever a student asks a general 'how does X work' / 'what should "
        "I do about Y' question that isn't about their own saved profile data, to ground your "
        "answer in GradMap's actual guidance instead of general knowledge. Pass the student's "
        "question (or the key topic words from it) as the query. If a good match comes back, "
        "use its content to inform your answer, then end your reply on its own line with a "
        "'Read more:' link so the student can go deeper if they want -- e.g. 'Read more: "
        "[<article title>](<url>)'. If more than one article was genuinely useful, list each on "
        "its own 'Read more:' line. If no good match comes back (empty articles list), just "
        "answer from your own knowledge and don't add a Read more line -- don't tell the "
        "student you searched and found nothing."
    )
)
async def search_help_articles(query: str) -> dict:
    results = _search_help_articles(query)
    return {"found": bool(results), "articles": results}


if __name__ == "__main__":
    # DNS-rebinding protection only trusts the Host header values listed here.
    # Behind a tunnel, requests arrive with Host: <tunnel-domain> (no port), so
    # that domain -- derived from ISSUER_URL -- has to be allowed explicitly,
    # on top of the plain localhost access used for direct local testing.
    issuer_host = urlparse(ISSUER_URL).netloc
    allowed_hosts = ["127.0.0.1:*", "localhost:*"]
    allowed_origins = ["http://127.0.0.1:*", "http://localhost:*"]
    if issuer_host and issuer_host not in allowed_hosts:
        allowed_hosts.append(issuer_host)
        allowed_origins.append(ISSUER_URL)

    server.run(
        transport="streamable-http",
        host=os.environ.get("MCP_HOST", "127.0.0.1"),
        port=int(os.environ.get("MCP_PORT", "8788")),
        transport_security=TransportSecuritySettings(
            allowed_hosts=allowed_hosts,
            allowed_origins=allowed_origins,
        ),
    )
