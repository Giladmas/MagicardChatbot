import html
import secrets
from datetime import datetime
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.runtime_config import CONFIG_HELP, DEFAULTS, get_config, update_config
from app.services.answer_cache import clear_cache, delete_cached, list_cached
from app.services.conversation_history import clear_all_history
from app.services.conversation_log import clear_conversations, delete_conversation_entry, read_conversations
from app.services.miss_log import clear_misses, delete_miss, read_misses

router = APIRouter()
security = HTTPBasic()

PREVIEW_COUNT = 5

# Plain-English definitions for the terms used on this page - written for an
# operator who doesn't read code, shown in the right-hand Dictionary panel.
DICTIONARY: list[tuple[str, str]] = [
    (
        "Knowledge Gap",
        "A question the assistant couldn't answer because nothing relevant was found in its "
        "knowledge base. Review these and add the missing information to a knowledge file.",
    ),
    (
        "Conversation Memory (History)",
        "Lets the assistant remember what was said earlier in the same chat, so a follow-up like "
        "“what about the fee?” is understood in context instead of answered blindly. This is "
        "live and temporary — it's what the AI actually uses, not a saved record.",
    ),
    (
        "Conversation Log",
        "A saved, browsable record of real chat exchanges (who asked what, and the answer given), "
        "kept for review purposes. Separate from Conversation Memory above — clearing this log "
        "does not affect any ongoing conversation.",
    ),
    (
        "Semantic Cache",
        "A shortcut that remembers answers to questions people have already asked. If a new "
        "question is close enough to one already answered, the cached answer is reused instantly "
        "instead of asking the AI again — faster and cheaper.",
    ),
    (
        "Similarity Threshold",
        "How closely a new question must match a cached one to reuse its answer. Higher = stricter "
        "(fewer accidental wrong matches, but fewer cache hits). Shown as a number from 0 to 1.",
    ),
    (
        "Rate Limit",
        "The minimum wait time between two questions from the same person, used to stop spam or "
        "accidental double-submits.",
    ),
    (
        "Retrieval / Chunks",
        "Before answering, the assistant searches its knowledge base for the most relevant small "
        "pieces of text (“chunks”) and only uses those to write its answer.",
    ),
    (
        "Temperature",
        "How predictable the assistant's wording is. 0 means it always answers the same way for the "
        "same question; higher values allow more varied phrasing.",
    ),
    (
        "Tokens",
        "Roughly a chunk of a word. AI providers measure both cost and answer length in tokens "
        "rather than characters.",
    ),
    (
        "Time-To-Live (TTL)",
        "How long of a gap, in seconds, before a person's conversation is considered “over” "
        "and the assistant forgets it, starting fresh next time they ask something.",
    ),
]

# One-time flash messages shown after a redirect - keeps a browser refresh from
# ever re-showing "Saved" or re-submitting the action that triggered it.
FLASH_MESSAGES: dict[str, str] = {
    "saved": "Saved.",
    "misses_cleared": "Knowledge gaps cleared.",
    "cache_cleared": "Answer cache cleared.",
    "history_cleared": "Conversation history cleared.",
    "conversations_cleared": "Conversation log cleared.",
}

PAGE_CSS = """
  :root {
    --bg: #f4f6f9;
    --surface: #ffffff;
    --border: #e2e6ec;
    --text: #1a2233;
    --text-muted: #667085;
    --primary: #2c4bff;
    --primary-hover: #1f39d6;
    --success-bg: #ecfdf3;
    --success-text: #12704b;
    --success-border: #a7e8c6;
    --error-bg: #fef3f2;
    --error-text: #b3261e;
    --error-border: #f3b6b1;
    --danger: #b3261e;
    --radius: 12px;
  }
  * { box-sizing: border-box; }
  body {
    font-family: -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    margin: 0;
    padding: 0 0 4rem 0;
  }
  header.topbar {
    background: linear-gradient(135deg, #0d1730, #1d2f5c 60%, #2c4bff);
    color: #fff;
    padding: 2.25rem 2rem;
    margin-bottom: 2rem;
    display: grid;
    grid-template-columns: 1fr auto 1fr;
    align-items: center;
    gap: 1rem;
  }
  header.topbar .topbar-side { min-width: 0; display: flex; align-items: center; }
  header.topbar .topbar-right { display: flex; justify-content: flex-end; }
  header.topbar .topbar-center { text-align: center; }
  header.topbar h1 {
    margin: 0;
    font-size: 2.1rem;
    font-weight: 800;
    letter-spacing: 0.01em;
  }
  header.topbar h1.small { font-size: 1.5rem; }
  header.topbar p {
    margin: 0.35rem 0 0 0;
    color: #b9c3dc;
    font-size: 0.88rem;
  }
  header.topbar .stats { display: flex; gap: 0.75rem; flex-wrap: wrap; }
  .stat-pill {
    background: rgba(255,255,255,0.1);
    border: 1px solid rgba(255,255,255,0.18);
    border-radius: 999px;
    padding: 0.4rem 0.9rem;
    font-size: 0.78rem;
    color: #e6eaf5;
    white-space: nowrap;
  }
  .stat-pill strong { color: #fff; font-size: 0.95rem; }
  .topbar-back {
    color: #d7deef;
    text-decoration: none;
    font-size: 0.85rem;
    font-weight: 600;
    padding: 0.4rem 0.8rem;
    border: 1px solid rgba(255,255,255,0.25);
    border-radius: 8px;
  }
  .topbar-back:hover { background: rgba(255,255,255,0.1); }
  @media (max-width: 700px) {
    header.topbar { grid-template-columns: 1fr; text-align: center; }
    header.topbar .topbar-right, header.topbar .topbar-side { justify-content: center; }
  }
  .layout {
    max-width: 1180px;
    margin: 0 auto;
    padding: 0 1.5rem;
    display: grid;
    grid-template-columns: minmax(0, 1fr) 300px;
    gap: 1.75rem;
    align-items: start;
  }
  .layout.single-col { grid-template-columns: minmax(0, 1fr); max-width: 900px; }
  @media (max-width: 880px) {
    .layout { grid-template-columns: 1fr; }
    .sidebar { order: 2; }
  }
  .main-col { min-width: 0; }
  .sidebar { position: sticky; top: 1.5rem; }
  .card {
    background: var(--surface);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.5rem 1.75rem;
    margin-bottom: 1.75rem;
    box-shadow: 0 1px 3px rgba(16, 24, 40, 0.05);
  }
  .sidebar-card { max-height: calc(100vh - 3rem); overflow-y: auto; }
  .card-header {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    margin-bottom: 0.5rem;
    flex-wrap: wrap;
    gap: 0.5rem;
  }
  .card-header h2 {
    margin: 0;
    font-size: 1.05rem;
    font-weight: 650;
  }
  .card-header .subtitle {
    color: var(--text-muted);
    font-size: 0.82rem;
  }
  .sidebar-intro { margin: 0 0 1rem 0; }
  .dict-item { padding: 0.7rem 0; border-top: 1px solid var(--border); }
  .dict-item:first-of-type { border-top: none; }
  .dict-term { font-weight: 650; font-size: 0.85rem; margin-bottom: 0.2rem; }
  .dict-def { font-size: 0.79rem; color: var(--text-muted); line-height: 1.45; }
  .banner {
    border-radius: var(--radius);
    padding: 0.7rem 1.1rem;
    font-size: 0.88rem;
    margin-bottom: 1rem;
    border: 1px solid transparent;
    grid-column: 1 / -1;
  }
  .banner-success {
    background: var(--success-bg);
    color: var(--success-text);
    border-color: var(--success-border);
  }
  .banner-error {
    background: var(--error-bg);
    color: var(--error-text);
    border-color: var(--error-border);
  }
  table {
    border-collapse: collapse;
    width: 100%;
  }
  td, th {
    padding: 0.65rem 0.5rem;
    text-align: left;
    vertical-align: top;
    border-bottom: 1px solid var(--border);
    font-size: 0.87rem;
  }
  th {
    color: var(--text-muted);
    font-weight: 650;
    font-size: 0.75rem;
    text-transform: uppercase;
    letter-spacing: 0.05em;
  }
  tr:last-child td { border-bottom: none; }
  tbody tr:hover td { background: #fafbfd; }
  .table-scroll { overflow-x: auto; }
  code {
    background: #eef1f6;
    padding: 0.15rem 0.4rem;
    border-radius: 5px;
    font-size: 0.82rem;
    color: #33415c;
  }
  .hint {
    font-size: 0.76rem;
    color: var(--text-muted);
    margin-top: 0.3rem;
  }
  .field-label {
    display: block;
    font-size: 0.78rem;
    font-weight: 650;
    color: var(--text-muted);
    margin-bottom: 0.3rem;
  }
  .field-optional { font-weight: 400; text-transform: none; letter-spacing: normal; }
  .text-input {
    width: 100%;
    padding: 0.45rem 0.6rem;
    border: 1px solid var(--border);
    border-radius: 7px;
    font-size: 0.88rem;
    background: #fff;
    color: var(--text);
  }
  .text-input:focus {
    outline: none;
    border-color: var(--primary);
    box-shadow: 0 0 0 3px rgba(44, 75, 255, 0.12);
  }
  .search-row { margin-bottom: 1rem; position: relative; }
  .search-row .text-input { padding-left: 2.1rem; }
  .search-row .search-icon {
    position: absolute; left: 0.7rem; top: 50%; transform: translateY(-50%);
    color: var(--text-muted); font-size: 0.85rem; pointer-events: none;
  }
  .search-count { font-size: 0.78rem; color: var(--text-muted); margin-top: 0.4rem; }
  .switch {
    position: relative;
    display: inline-block;
    width: 40px;
    height: 22px;
    cursor: pointer;
  }
  .switch input[type="checkbox"] {
    position: absolute;
    opacity: 0;
    width: 100%;
    height: 100%;
    margin: 0;
    cursor: pointer;
  }
  .switch-track {
    position: absolute;
    inset: 0;
    background: #d0d5dd;
    border-radius: 999px;
    transition: background 0.15s ease;
  }
  .switch-thumb {
    position: absolute;
    top: 2px;
    left: 2px;
    width: 18px;
    height: 18px;
    background: #fff;
    border-radius: 50%;
    transition: transform 0.15s ease;
    box-shadow: 0 1px 2px rgba(0,0,0,0.25);
  }
  .switch input[type="checkbox"]:checked ~ .switch-track { background: var(--primary); }
  .switch input[type="checkbox"]:checked ~ .switch-track .switch-thumb { transform: translateX(18px); }
  .btn {
    display: inline-block;
    font-size: 0.87rem;
    font-weight: 650;
    padding: 0.55rem 1.1rem;
    border-radius: 8px;
    border: 1px solid transparent;
    cursor: pointer;
    text-decoration: none;
    line-height: 1.3;
  }
  .btn-primary {
    background: var(--primary);
    color: #fff;
  }
  .btn-primary:hover { background: var(--primary-hover); }
  .btn-primary:disabled { opacity: 0.6; cursor: default; }
  .btn-outline {
    background: #fff;
    color: var(--text);
    border-color: var(--border);
  }
  .btn-outline:hover { background: #f8f9fb; }
  .btn-outline-danger {
    background: #fff;
    color: var(--danger);
    border-color: #f0c4c1;
  }
  .btn-outline-danger:hover { background: var(--error-bg); }
  .btn-sm { padding: 0.35rem 0.75rem; font-size: 0.8rem; }
  .btn-link-danger {
    background: none;
    border: none;
    color: var(--danger);
    font-size: 0.82rem;
    font-weight: 550;
    padding: 0.2rem 0.4rem;
    cursor: pointer;
  }
  .btn-link-danger:hover { text-decoration: underline; }
  .col-timestamp {
    white-space: nowrap;
    color: var(--text-muted);
    font-size: 0.82rem;
  }
  .col-action { text-align: right; white-space: nowrap; }
  .empty-state {
    text-align: center;
    color: var(--text-muted);
    padding: 1.5rem 0;
  }
  .actions-row {
    display: flex;
    justify-content: flex-end;
    gap: 0.6rem;
    margin-top: 1rem;
    flex-wrap: wrap;
  }
  .actions-row.split { justify-content: space-between; align-items: center; }
  .danger-zone {
    border-top: 1px dashed var(--border);
    margin-top: 1.25rem;
    padding-top: 1.1rem;
  }
  .danger-zone .label {
    font-size: 0.76rem;
    color: var(--text-muted);
    margin-bottom: 0.5rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .chat-headers-row {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 1rem;
    margin-bottom: 1rem;
  }
  @media (max-width: 560px) {
    .chat-headers-row { grid-template-columns: 1fr; }
  }
  .chat-messages {
    border: 1px solid var(--border);
    border-radius: var(--radius);
    background: #fafbfc;
    padding: 1rem;
    height: 300px;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.6rem;
    margin-bottom: 1rem;
  }
  .chat-empty {
    margin: auto;
    color: var(--text-muted);
    font-size: 0.85rem;
  }
  .chat-msg { display: flex; flex-direction: column; max-width: 80%; }
  .chat-msg-user { align-self: flex-end; align-items: flex-end; }
  .chat-msg-bot { align-self: flex-start; align-items: flex-start; }
  .chat-msg-error { align-self: center; align-items: center; max-width: 100%; }
  .chat-bubble {
    padding: 0.55rem 0.85rem;
    border-radius: 12px;
    font-size: 0.87rem;
    line-height: 1.4;
    white-space: pre-wrap;
    word-break: break-word;
  }
  .chat-msg-user .chat-bubble { background: var(--primary); color: #fff; border-bottom-right-radius: 4px; }
  .chat-msg-bot .chat-bubble { background: #eef1f6; color: var(--text); border-bottom-left-radius: 4px; }
  .chat-msg-error .chat-bubble { background: var(--error-bg); color: var(--error-text); }
  .chat-meta { font-size: 0.72rem; color: var(--text-muted); margin-top: 0.2rem; }
  .chat-input-row { display: flex; gap: 0.6rem; }
  .chat-input-row .text-input { flex: 1; }
"""


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="Admin panel disabled: ADMIN_PASSWORD not set")
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def _humanize_seconds(value: object) -> str | None:
    """For a *_seconds config value, a plain-English equivalent shown next to
    the field - e.g. so "1800" reads as "= 30 min" instead of being mistaken
    for minutes at a glance."""
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if seconds < 60:
        return f"= {seconds:g} sec"
    minutes = seconds / 60
    if minutes < 60:
        return f"= {minutes:g} min"
    hours = minutes / 60
    return f"= {hours:g} hr"


def _format_timestamp(iso_str: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_str)
    except ValueError:
        return iso_str
    return dt.strftime("%Y-%m-%d, %H:%M:%S")


def _render_field(key: str, value: object) -> str:
    if isinstance(value, bool):
        checked = "checked" if value else ""
        # Hidden "false" always submits; the checkbox's "true" comes after it
        # and wins (last value for a repeated form key) only when checked -
        # this is how an unchecked HTML checkbox still sends a value.
        return (
            f"<label class='switch'>"
            f"<input type='hidden' name='{html.escape(key)}' value='false'>"
            f"<input type='checkbox' name='{html.escape(key)}' value='true' {checked}>"
            f"<span class='switch-track'><span class='switch-thumb'></span></span>"
            f"</label>"
        )
    return f"<input class='text-input' name='{html.escape(key)}' value='{html.escape(str(value))}'>"


def _render_config_rows(config: dict) -> str:
    rows = ""
    for k in DEFAULTS:
        value = config[k]
        help_text = CONFIG_HELP.get(k, "")
        if k.endswith("_seconds"):
            duration = _humanize_seconds(value)
            if duration:
                help_text = f"{help_text} ({duration} at current value)" if help_text else duration
        help_html = f"<div class='hint'>{html.escape(help_text)}</div>" if help_text else ""
        rows += f"""
<tr>
  <td><code>{html.escape(k)}</code></td>
  <td>{_render_field(k, value)}{help_html}</td>
</tr>"""
    return rows


def _truncate(text: str, max_len: int = 140) -> str:
    text = text.strip()
    return text if len(text) <= max_len else text[: max_len - 1].rstrip() + "…"


def _delete_form(action: str, entry_id: str, next_path: str) -> str:
    return f"""<form method="post" action="{action}">
      <input type="hidden" name="id" value="{html.escape(entry_id)}">
      <input type="hidden" name="next" value="{html.escape(next_path)}">
      <button type="submit" class="btn btn-link-danger">Delete</button>
    </form>"""


def _render_miss_rows(misses: list[dict], next_path: str) -> str:
    if not misses:
        return "<tr><td colspan='3' class='empty-state'>No knowledge gaps logged. Nice.</td></tr>"
    rows = ""
    for m in misses:
        search_key = m["question"].lower()
        rows += f"""
<tr data-search="{html.escape(search_key)}">
  <td class="col-timestamp">{html.escape(_format_timestamp(m['timestamp']))}</td>
  <td>{html.escape(m['question'])}</td>
  <td class="col-action">{_delete_form('/admin/misses/delete', m['id'], next_path)}</td>
</tr>"""
    return rows


def _render_cache_rows(entries: list[dict], next_path: str) -> str:
    if not entries:
        return "<tr><td colspan='4' class='empty-state'>Cache is empty.</td></tr>"
    rows = ""
    for e in entries:
        search_key = f"{e['question']} {e['answer']}".lower()
        rows += f"""
<tr data-search="{html.escape(search_key)}">
  <td class="col-timestamp">{html.escape(_format_timestamp(e['cached_at'])) if e['cached_at'] else '—'}</td>
  <td>{html.escape(_truncate(e['question']))}</td>
  <td>{html.escape(_truncate(e['answer']))}</td>
  <td class="col-action">{_delete_form('/admin/cache/delete', e['id'], next_path)}</td>
</tr>"""
    return rows


def _render_conversation_rows(entries: list[dict], next_path: str) -> str:
    if not entries:
        return "<tr><td colspan='5' class='empty-state'>No conversations logged yet.</td></tr>"
    rows = ""
    for e in entries:
        search_key = f"{e['user_id']} {e['question']} {e['answer']}".lower()
        rows += f"""
<tr data-search="{html.escape(search_key)}">
  <td class="col-timestamp">{html.escape(_format_timestamp(e['timestamp']))}</td>
  <td><code>{html.escape(e['user_id'])}</code></td>
  <td>{html.escape(_truncate(e['question']))}</td>
  <td>{html.escape(_truncate(e['answer']))}</td>
  <td class="col-action">{_delete_form('/admin/conversations/delete', e['id'], next_path)}</td>
</tr>"""
    return rows


def _render_sidebar() -> str:
    items = "".join(
        f"<div class='dict-item'><div class='dict-term'>{html.escape(term)}</div>"
        f"<div class='dict-def'>{html.escape(definition)}</div></div>"
        for term, definition in DICTIONARY
    )
    return f"""
<aside class="sidebar">
  <div class="card sidebar-card">
    <div class="card-header">
      <h2>Dictionary</h2>
    </div>
    <p class="subtitle sidebar-intro">Plain-English meanings for the terms used on this page.</p>
    {items}
  </div>
</aside>
"""


def _render_chatbox() -> str:
    return """
<div class="card">
  <div class="card-header">
    <h2>Test Chatbot</h2>
    <span class="subtitle">Sends real requests to POST /chat - no Postman needed</span>
  </div>
  <div class="chat-headers-row">
    <div class="chat-field">
      <label class="field-label" for="chat-user-id">X-User-Id</label>
      <input id="chat-user-id" class="text-input" placeholder="e.g. admin-test-abc123">
    </div>
    <div class="chat-field">
      <label class="field-label" for="chat-secret">X-Chat-Secret <span class="field-optional">(only if configured)</span></label>
      <input id="chat-secret" class="text-input" placeholder="leave blank if not set">
    </div>
  </div>
  <div id="chat-messages" class="chat-messages">
    <div class="chat-empty">Send a message to start a test conversation.</div>
  </div>
  <form id="chat-form" class="chat-input-row">
    <input id="chat-input" class="text-input" placeholder="Type a question..." autocomplete="off">
    <button id="chat-send" type="submit" class="btn btn-primary">Send</button>
  </form>
</div>

<script>
(function () {
  var userIdInput = document.getElementById('chat-user-id');
  var secretInput = document.getElementById('chat-secret');
  var messagesEl = document.getElementById('chat-messages');
  var formEl = document.getElementById('chat-form');
  var inputEl = document.getElementById('chat-input');
  var sendBtn = document.getElementById('chat-send');
  var emptyEl = messagesEl.querySelector('.chat-empty');

  function randomId() {
    return 'admin-test-' + Math.random().toString(36).slice(2, 8);
  }
  userIdInput.value = randomId();

  function appendMessage(role, text, meta) {
    if (emptyEl) { emptyEl.remove(); emptyEl = null; }
    var wrap = document.createElement('div');
    wrap.className = 'chat-msg chat-msg-' + role;
    var bubble = document.createElement('div');
    bubble.className = 'chat-bubble';
    bubble.textContent = text;
    wrap.appendChild(bubble);
    if (meta) {
      var metaEl = document.createElement('div');
      metaEl.className = 'chat-meta';
      metaEl.textContent = meta;
      wrap.appendChild(metaEl);
    }
    messagesEl.appendChild(wrap);
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  formEl.addEventListener('submit', function (e) {
    e.preventDefault();
    var question = inputEl.value.trim();
    if (!question) return;
    var userId = userIdInput.value.trim() || randomId();
    userIdInput.value = userId;

    appendMessage('user', question);
    inputEl.value = '';
    sendBtn.disabled = true;

    var headers = { 'Content-Type': 'application/json', 'X-User-Id': userId };
    if (secretInput.value.trim()) headers['X-Chat-Secret'] = secretInput.value.trim();

    fetch('/chat', { method: 'POST', headers: headers, body: JSON.stringify({ question: question }) })
      .then(function (res) {
        return res.json().catch(function () { return {}; }).then(function (data) {
          return { status: res.status, data: data };
        });
      })
      .then(function (result) {
        if (result.status >= 200 && result.status < 300) {
          var sources = (result.data.sources || []).join(', ');
          appendMessage('bot', result.data.answer || '(empty answer)', sources ? 'Sources: ' + sources : 'No sources matched');
        } else {
          appendMessage('error', 'Error ' + result.status + ': ' + (result.data.detail || 'request failed'));
        }
      })
      .catch(function (err) {
        appendMessage('error', 'Request failed: ' + err);
      })
      .finally(function () {
        sendBtn.disabled = false;
        inputEl.focus();
      });
  });
})();
</script>
"""


def _search_script() -> str:
    return """
<script>
(function () {
  var search = document.getElementById('search');
  var countEl = document.getElementById('search-count');
  var rows = Array.prototype.slice.call(document.querySelectorAll('#data-table tr[data-search]'));
  var total = rows.length;
  function apply() {
    var q = search.value.trim().toLowerCase();
    var shown = 0;
    rows.forEach(function (r) {
      var match = !q || r.getAttribute('data-search').indexOf(q) !== -1;
      r.style.display = match ? '' : 'none';
      if (match) shown++;
    });
    if (countEl) countEl.textContent = q ? ('Showing ' + shown + ' of ' + total) : '';
  }
  search.addEventListener('input', apply);
})();
</script>
"""


def _render_top_card(
    title: str,
    subtitle: str,
    rows_html: str,
    header_cells: str,
    total: int,
    view_all_url: str,
    clear_action: str,
) -> str:
    footer = f"""
  <div class="actions-row split">
    <span class="subtitle">{'Showing latest ' + str(min(PREVIEW_COUNT, total)) + ' of ' + str(total) if total > PREVIEW_COUNT else ''}</span>
    <div class="actions-row" style="margin: 0;">
      {f'<a class="btn btn-outline btn-sm" href="{view_all_url}" target="_blank" rel="noopener">View all ({total})</a>' if total else ''}
      <form method="post" action="{clear_action}">
        <input type="hidden" name="next" value="/admin">
        <button type="submit" class="btn btn-outline-danger btn-sm">Clear all</button>
      </form>
    </div>
  </div>"""
    return f"""
<div class="card">
  <div class="card-header">
    <h2>{html.escape(title)}</h2>
    <span class="subtitle">{html.escape(subtitle)}</span>
  </div>
  <div class="table-scroll">
  <table>
    <tr>{header_cells}</tr>
    {rows_html}
  </table>
  </div>
  {footer}
</div>
"""


def _page_head(title: str) -> str:
    return f"<title>{html.escape(title)}</title>\n<style>{PAGE_CSS}</style>"


def _render_page(
    config: dict,
    misses: list[dict],
    cached: list[dict],
    conversations: list[dict],
    banner: tuple[str, str] | None = None,
) -> str:
    total_misses = len(misses)
    total_cached = len(cached)
    total_conversations = len(conversations)
    rows = _render_config_rows(config)
    miss_rows = _render_miss_rows(misses[:PREVIEW_COUNT], "/admin")
    cache_rows = _render_cache_rows(cached[:PREVIEW_COUNT], "/admin")
    conversation_rows = _render_conversation_rows(conversations[:PREVIEW_COUNT], "/admin")
    sidebar_html = _render_sidebar()
    chatbox_html = _render_chatbox()

    banner_html = ""
    if banner:
        kind, message = banner
        banner_html = f"<div class='banner banner-{html.escape(kind)}'>{html.escape(message)}</div>"

    misses_card = _render_top_card(
        "Knowledge Gaps",
        "Most recent first",
        miss_rows,
        "<th>When</th><th>Question</th><th></th>",
        total_misses,
        "/admin/misses",
        "/admin/misses/clear",
    )
    cache_card = _render_top_card(
        "Cached Answers",
        "Most recently cached first",
        cache_rows,
        "<th>Cached</th><th>Question</th><th>Answer</th><th></th>",
        total_cached,
        "/admin/cache",
        "/admin/cache/clear",
    )
    conversations_card = _render_top_card(
        "Conversations",
        "Saved chat log, most recent first — search by user id on the full page to see one person's thread",
        conversation_rows,
        "<th>When</th><th>User</th><th>Question</th><th>Answer</th><th></th>",
        total_conversations,
        "/admin/conversations",
        "/admin/conversations/clear",
    )

    return f"""
<!doctype html>
<html>
<head>
{_page_head("Magicard Chatbot Admin")}
</head>
<body>
<header class="topbar">
  <div class="topbar-side"></div>
  <div class="topbar-center">
    <h1>Magicard Chatbot Admin</h1>
    <p>Live configuration, knowledge gaps &amp; answer cache</p>
  </div>
  <div class="topbar-side topbar-right">
    <div class="stats">
      <span class="stat-pill"><strong>{total_misses}</strong> knowledge gaps</span>
      <span class="stat-pill"><strong>{total_cached}</strong> cached answers</span>
      <span class="stat-pill"><strong>{total_conversations}</strong> conversations logged</span>
    </div>
  </div>
</header>

<div class="layout">
{banner_html}
<div class="main-col">

{chatbox_html}

<div class="card">
  <div class="card-header">
    <h2>Live configuration</h2>
    <span class="subtitle">Applies immediately, no redeploy needed</span>
  </div>
  <form method="post" action="/admin/config">
    <table>
      <tr><th>Setting</th><th>Value</th></tr>
      {rows}
    </table>
    <div class="actions-row">
      <button type="submit" class="btn btn-primary">Save changes</button>
    </div>
  </form>
</div>

{misses_card}

{cache_card}

{conversations_card}

<div class="card">
  <div class="card-header">
    <h2>Reset actions</h2>
    <span class="subtitle">Immediate, cannot be undone</span>
  </div>
  <div class="danger-zone">
    <div class="label">Conversation memory</div>
    <p class="subtitle" style="margin: 0 0 0.6rem 0;">
      Forgets every ongoing conversation for every user. Nobody loses their next answer &mdash;
      their next question is just treated as a fresh start instead of a follow-up.
    </p>
    <form method="post" action="/admin/history/clear">
      <button type="submit" class="btn btn-outline-danger">Clear all conversation history</button>
    </form>
  </div>
  <div class="danger-zone">
    <div class="label">Answer cache</div>
    <p class="subtitle" style="margin: 0 0 0.6rem 0;">
      Wipes every cached answer. The next matching question will be answered fresh (slightly
      slower, normal cost) and re-cached.
    </p>
    <form method="post" action="/admin/cache/clear">
      <button type="submit" class="btn btn-outline-danger">Clear entire cache</button>
    </form>
  </div>
</div>

</div>
{sidebar_html}
</div>
</body>
</html>
"""


def _render_full_list_page(
    page_title: str,
    subtitle: str,
    rows_html: str,
    header_cells: str,
    total: int,
    search_placeholder: str,
    clear_action: str,
    banner: tuple[str, str] | None = None,
) -> str:
    banner_html = ""
    if banner:
        kind, message = banner
        banner_html = f"<div class='banner banner-{html.escape(kind)}' style='grid-column: auto;'>{html.escape(message)}</div>"

    return f"""
<!doctype html>
<html>
<head>
{_page_head(f"{page_title} - Magicard Chatbot Admin")}
</head>
<body>
<header class="topbar">
  <div class="topbar-side">
    <a class="topbar-back" href="/admin">&larr; Back to Admin</a>
  </div>
  <div class="topbar-center">
    <h1 class="small">{html.escape(page_title)}</h1>
    <p>{total} total &middot; {html.escape(subtitle)}</p>
  </div>
  <div class="topbar-side topbar-right"></div>
</header>

<div class="layout single-col">
<div class="main-col">
{banner_html}
<div class="card">
  <div class="search-row">
    <span class="search-icon">&#128269;</span>
    <input id="search" class="text-input" placeholder="{html.escape(search_placeholder)}">
    <div id="search-count" class="search-count"></div>
  </div>
  <div class="table-scroll">
  <table id="data-table">
    <tr>{header_cells}</tr>
    {rows_html}
  </table>
  </div>
  <div class="actions-row">
    <form method="post" action="{clear_action}">
      <input type="hidden" name="next" value="{clear_action.rsplit('/', 1)[0]}">
      <button type="submit" class="btn btn-outline-danger">Clear all</button>
    </form>
  </div>
</div>
</div>
</div>
{_search_script()}
</body>
</html>
"""


def _admin_redirect(path: str = "/admin", msg_key: str | None = None, error: str | None = None) -> RedirectResponse:
    """Redirect back to a GET page (PRG pattern) so a browser refresh re-fetches
    the page instead of resubmitting the action, and a one-time flash banner
    doesn't linger forever. `path` lets an action taken from a "view all" page
    redirect back to that same page instead of always landing on /admin."""
    url = path
    if error:
        url += f"?error={quote(error)}"
    elif msg_key:
        url += f"?msg={msg_key}"
    return RedirectResponse(url=url, status_code=303)


def _banner_from_query(request: Request) -> tuple[str, str] | None:
    error = request.query_params.get("error")
    msg_key = request.query_params.get("msg")
    if error:
        return ("error", f"Error: {error}")
    if msg_key in FLASH_MESSAGES:
        return ("success", FLASH_MESSAGES[msg_key])
    return None


@router.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request, _: None = Depends(require_admin)) -> str:
    return _render_page(
        get_config(),
        read_misses(),
        list_cached(),
        read_conversations(),
        banner=_banner_from_query(request),
    )


@router.get("/admin/misses", response_class=HTMLResponse)
def admin_misses_page(request: Request, _: None = Depends(require_admin)) -> str:
    misses = read_misses()
    rows_html = _render_miss_rows(misses, "/admin/misses")
    return _render_full_list_page(
        "Knowledge Gaps",
        "most recent first",
        rows_html,
        "<th>When</th><th>Question</th><th></th>",
        len(misses),
        "Search questions...",
        "/admin/misses/clear",
        banner=_banner_from_query(request),
    )


@router.get("/admin/cache", response_class=HTMLResponse)
def admin_cache_page(request: Request, _: None = Depends(require_admin)) -> str:
    cached = list_cached()
    rows_html = _render_cache_rows(cached, "/admin/cache")
    return _render_full_list_page(
        "Cached Answers",
        "most recently cached first",
        rows_html,
        "<th>Cached</th><th>Question</th><th>Answer</th><th></th>",
        len(cached),
        "Search questions or answers...",
        "/admin/cache/clear",
        banner=_banner_from_query(request),
    )


@router.get("/admin/conversations", response_class=HTMLResponse)
def admin_conversations_page(request: Request, _: None = Depends(require_admin)) -> str:
    conversations = read_conversations()
    rows_html = _render_conversation_rows(conversations, "/admin/conversations")
    return _render_full_list_page(
        "Conversations",
        "most recent first",
        rows_html,
        "<th>When</th><th>User</th><th>Question</th><th>Answer</th><th></th>",
        len(conversations),
        "Search by user id, question, or answer...",
        "/admin/conversations/clear",
        banner=_banner_from_query(request),
    )


@router.post("/admin/config")
async def admin_update_config(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    try:
        update_config(dict(form))
    except ValueError as exc:
        return _admin_redirect(error=str(exc))
    return _admin_redirect(msg_key="saved")


@router.post("/admin/misses/delete")
async def admin_delete_miss(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    delete_miss(str(form.get("id", "")))
    return _admin_redirect(str(form.get("next", "/admin")))


@router.post("/admin/misses/clear")
async def admin_clear_misses(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    clear_misses()
    return _admin_redirect(str(form.get("next", "/admin")), msg_key="misses_cleared")


@router.post("/admin/cache/delete")
async def admin_delete_cached(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    delete_cached(str(form.get("id", "")))
    return _admin_redirect(str(form.get("next", "/admin")))


@router.post("/admin/cache/clear")
async def admin_clear_cache(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    clear_cache()
    return _admin_redirect(str(form.get("next", "/admin")), msg_key="cache_cleared")


@router.post("/admin/history/clear")
async def admin_clear_history(_: None = Depends(require_admin)) -> RedirectResponse:
    clear_all_history()
    return _admin_redirect(msg_key="history_cleared")


@router.post("/admin/conversations/delete")
async def admin_delete_conversation(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    delete_conversation_entry(str(form.get("id", "")))
    return _admin_redirect(str(form.get("next", "/admin")))


@router.post("/admin/conversations/clear")
async def admin_clear_conversations(request: Request, _: None = Depends(require_admin)) -> RedirectResponse:
    form = await request.form()
    clear_conversations()
    return _admin_redirect(str(form.get("next", "/admin")), msg_key="conversations_cleared")
