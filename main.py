import os
import hmac
import hashlib
import base64
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from database import (
    init_db, upsert_user, save_message, save_draft,
    get_conversations, get_messages, get_latest_draft, get_user,
    search_conversations,
)
from sheets import load_manual, get_manual_content
from claude_service import generate_reply, refine_reply

TEMPLATES = Jinja2Templates(directory="templates")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_manual()
    yield


app = FastAPI(lifespan=lifespan)


# ─── Basic認証（/webhook 以外を保護） ─────────────────────────

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    import secrets as _secrets
    from fastapi.responses import Response as PlainResponse

    admin_password = os.getenv("ADMIN_PASSWORD", "")
    # /webhook はLINEからの通知用なので認証不要（署名検証で保護済み）
    if request.url.path == "/webhook" or not admin_password:
        return await call_next(request)

    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, _, password = decoded.partition(":")
            if _secrets.compare_digest(password, admin_password):
                return await call_next(request)
        except Exception:
            pass

    return PlainResponse(
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="line-reply-tool"'},
    )


# ─── Utilities ────────────────────────────────────────────────

def verify_line_signature(body: bytes, signature: str) -> bool:
    secret = os.getenv("LINE_CHANNEL_SECRET", "")
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def get_manual_or_reload() -> str:
    """マニュアルが未読み込み（起動時の読み込み失敗など）なら読み直す。"""
    manual = get_manual_content()
    if not manual:
        manual = load_manual()
    return manual


def get_line_profile(user_id: str) -> str:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    try:
        resp = httpx.get(
            f"https://api.line.me/v2/bot/profile/{user_id}",
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        )
        if resp.status_code == 200:
            return resp.json().get("displayName", user_id)
    except Exception:
        pass
    return user_id


# ─── LINE Webhook ──────────────────────────────────────────────

async def forward_to_elme(body: bytes, headers: dict):
    elme_url = os.getenv("ELME_WEBHOOK_URL", "https://cb.lmes.jp/line/callback/add/148826")
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(elme_url, content=body, headers=headers)
    except Exception as e:
        print(f"[elme] 転送エラー: {e}")


@app.post("/webhook")
async def webhook(request: Request):
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # エルメに転送
    forward_headers = {
        "Content-Type": "application/json",
        "X-Line-Signature": signature,
    }
    await forward_to_elme(body, forward_headers)

    data = json.loads(body)
    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        text = event["message"]["text"]

        # ユーザー情報を保存
        display_name = get_line_profile(user_id)
        upsert_user(user_id, display_name)

        # メッセージを保存
        save_message(user_id, "inbound", text)

        # 返信案を生成して保存
        history = get_messages(user_id)
        manual = get_manual_or_reload()
        draft = generate_reply(history, manual, customer_name=display_name)
        save_draft(user_id, draft)

    return {"status": "ok"}


# ─── Admin API ─────────────────────────────────────────────────

@app.get("/api/conversations")
async def api_conversations():
    return get_conversations()


@app.get("/api/search")
async def api_search(q: str = ""):
    q = q.strip()
    if not q:
        return get_conversations()
    return search_conversations(q)


@app.get("/api/messages/{user_id}")
async def api_messages(user_id: str):
    messages = get_messages(user_id)
    draft = get_latest_draft(user_id)
    return {"messages": messages, "draft": draft}


@app.post("/api/regenerate/{user_id}")
async def api_regenerate(user_id: str):
    history = get_messages(user_id)
    if not history:
        raise HTTPException(status_code=404, detail="No messages found")
    manual = get_manual_or_reload()
    user = get_user(user_id)
    display_name = user["display_name"] if user else ""
    draft = generate_reply(history, manual, customer_name=display_name)
    save_draft(user_id, draft)
    return {"draft": draft}


class RefineRequest(BaseModel):
    instruction: str
    draft: str


@app.post("/api/refine/{user_id}")
async def api_refine(user_id: str, req: RefineRequest):
    instruction = req.instruction.strip()
    if not instruction:
        raise HTTPException(status_code=400, detail="修正指示が空です")

    history = get_messages(user_id)
    if not history:
        raise HTTPException(status_code=404, detail="No messages found")

    manual = get_manual_or_reload()
    user = get_user(user_id)
    display_name = user["display_name"] if user else ""
    draft = refine_reply(history, manual, display_name, req.draft, instruction)
    save_draft(user_id, draft)
    return {"draft": draft}


class SendRequest(BaseModel):
    text: str


@app.post("/api/send/{user_id}")
async def api_send(user_id: str, req: SendRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="メッセージが空です")

    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "")
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(
            "https://api.line.me/v2/bot/message/push",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json={"to": user_id, "messages": [{"type": "text", "text": text}]},
        )

    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=f"LINE送信エラー: {resp.text}")

    save_message(user_id, "outbound", text)
    return {"status": "ok"}


@app.get("/api/health")
async def api_health():
    db_path = os.getenv("DB_PATH", "line_chat.db")
    data_dir = os.path.dirname(db_path) or "."
    return {
        "db_path": db_path,
        "db_exists": os.path.exists(db_path),
        "data_dir_exists": os.path.isdir(data_dir),
        "data_dir_files": os.listdir(data_dir) if os.path.isdir(data_dir) else [],
        "manual_chars": len(get_manual_content()),
    }


@app.post("/api/reload-manual")
async def api_reload_manual():
    content = load_manual()
    length = len(content)
    return {"status": "ok", "characters": length}


# ─── Admin UI ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html")
