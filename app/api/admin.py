import html
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.config import settings
from app.runtime_config import DEFAULTS, get_config, update_config
from app.services.miss_log import read_misses

router = APIRouter()
security = HTTPBasic()


def require_admin(credentials: HTTPBasicCredentials = Depends(security)) -> None:
    if not settings.admin_password:
        raise HTTPException(status_code=503, detail="Admin panel disabled: ADMIN_PASSWORD not set")
    valid_user = secrets.compare_digest(credentials.username, settings.admin_username)
    valid_pass = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (valid_user and valid_pass):
        raise HTTPException(status_code=401, detail="Invalid credentials", headers={"WWW-Authenticate": "Basic"})


def _render_page(config: dict, misses: list[dict]) -> str:
    rows = "".join(
        f"<tr><td>{html.escape(k)}</td>"
        f"<td><input name='{html.escape(k)}' value='{html.escape(str(config[k]))}'></td></tr>"
        for k in DEFAULTS
    )
    miss_rows = "".join(
        f"<tr><td>{html.escape(m['timestamp'])}</td><td>{html.escape(m['question'])}</td></tr>"
        for m in misses
    ) or "<tr><td colspan='2'>No missed questions logged yet.</td></tr>"

    return f"""
<!doctype html>
<html>
<head><title>Magicard Chatbot Admin</title>
<style>
body {{ font-family: system-ui, sans-serif; max-width: 900px; margin: 2rem auto; padding: 0 1rem; }}
table {{ border-collapse: collapse; width: 100%; margin-bottom: 2rem; }}
td, th {{ border: 1px solid #ccc; padding: 0.4rem 0.6rem; text-align: left; }}
input {{ width: 100%; box-sizing: border-box; }}
h2 {{ margin-top: 2rem; }}
</style>
</head>
<body>
<h1>Magicard Chatbot Admin</h1>

<h2>Live config</h2>
<form method="post" action="/admin/config">
<table>{rows}</table>
<button type="submit">Save</button>
</form>

<h2>Missed questions (most recent first, up to 200)</h2>
<table>
<tr><th>Timestamp (UTC)</th><th>Question</th></tr>
{miss_rows}
</table>
</body>
</html>
"""


@router.get("/admin", response_class=HTMLResponse)
def admin_page(_: None = Depends(require_admin)) -> str:
    return _render_page(get_config(), read_misses())


@router.post("/admin/config", response_class=HTMLResponse)
async def admin_update_config(request: Request, _: None = Depends(require_admin)) -> str:
    form = await request.form()
    update_config(dict(form))
    return _render_page(get_config(), read_misses())
