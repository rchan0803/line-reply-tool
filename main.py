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
    search_conversations, set_call_name,
)
from sheets import load_manuals, get_manual_content
from claude_service import generate_reply, refine_reply

TEMPLATES = Jinja2Templates(directory="templates")

# ─── アカウント設定 ────────────────────────────────────────────
# main: 公式LINE（無料鑑定側） / paid: 鑑定購入者専用LINE

ACCOUNTS = {
    "main": {
        "label": "公式LINE",
        "secret": os.getenv("LINE_CHANNEL_SECRET", ""),
        "token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        "elme_url": os.getenv("ELME_WEBHOOK_URL", ""),
        "sheets": [s.strip() for s in os.getenv("MANUAL_SHEETS_MAIN", "公式LINE").split(",") if s.strip()],
    },
    "paid": {
        "label": "購入者専用LINE",
        "secret": os.getenv("LINE_CHANNEL_SECRET_PAID", ""),
        "token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN_PAID", ""),
        "elme_url": os.getenv("ELME_WEBHOOK_URL_PAID", ""),
        "sheets": [s.strip() for s in os.getenv("MANUAL_SHEETS_PAID", "有料鑑定専用LINE,相談内容ヒアリング").split(",") if s.strip()],
    },
}

ACCOUNT_SHEETS = {aid: acc["sheets"] for aid, acc in ACCOUNTS.items()}


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    load_manuals(ACCOUNT_SHEETS)
    yield


app = FastAPI(lifespan=lifespan)


# ─── Basic認証（/webhook 以外を保護） ─────────────────────────

@app.middleware("http")
async def basic_auth_middleware(request: Request, call_next):
    import secrets as _secrets
    from fastapi.responses import Response as PlainResponse

    admin_password = os.getenv("ADMIN_PASSWORD", "")
    # /webhook系 はLINEからの通知用なので認証不要（署名検証で保護済み）
    if request.url.path.startswith("/webhook") or not admin_password:
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

def verify_line_signature(body: bytes, signature: str, secret: str) -> bool:
    if not secret:
        return False
    digest = hmac.new(secret.encode(), body, hashlib.sha256).digest()
    expected = base64.b64encode(digest).decode()
    return hmac.compare_digest(expected, signature)


def preferred_name(user_id: str) -> str:
    """呼び名（設定済みならそれ、なければLINE表示名）を返す。"""
    user = get_user(user_id)
    if not user:
        return ""
    return user.get("call_name") or user.get("display_name") or ""


def get_manual_or_reload(account: str) -> str:
    """マニュアルが未読み込み（起動時の読み込み失敗など）なら読み直す。"""
    manual = get_manual_content(account)
    if not manual:
        load_manuals(ACCOUNT_SHEETS)
        manual = get_manual_content(account)
    return manual


def get_line_profile(user_id: str, token: str) -> str:
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

async def forward_to_elme(elme_url: str, body: bytes, headers: dict):
    if not elme_url:
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(elme_url, content=body, headers=headers)
    except Exception as e:
        print(f"[elme] 転送エラー: {e}")


async def process_webhook(account_id: str, request: Request):
    acc = ACCOUNTS[account_id]
    body = await request.body()
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body, signature, acc["secret"]):
        raise HTTPException(status_code=400, detail="Invalid signature")

    # エルメに転送
    forward_headers = {
        "Content-Type": "application/json",
        "X-Line-Signature": signature,
    }
    await forward_to_elme(acc["elme_url"], body, forward_headers)

    data = json.loads(body)
    for event in data.get("events", []):
        if event.get("type") != "message":
            continue
        if event["message"].get("type") != "text":
            continue

        user_id = event["source"]["userId"]
        text = event["message"]["text"]

        # ユーザー情報を保存
        display_name = get_line_profile(user_id, acc["token"])
        upsert_user(user_id, display_name, account=account_id)

        # メッセージを保存
        save_message(user_id, "inbound", text)

        # 返信案を生成して保存
        history = get_messages(user_id)
        manual = get_manual_or_reload(account_id)
        draft = generate_reply(history, manual, customer_name=preferred_name(user_id))
        save_draft(user_id, draft)

    return {"status": "ok"}


@app.post("/webhook")
async def webhook_main(request: Request):
    return await process_webhook("main", request)


@app.post("/webhook/paid")
async def webhook_paid(request: Request):
    return await process_webhook("paid", request)


# ─── Admin API ─────────────────────────────────────────────────

@app.get("/api/accounts")
async def api_accounts():
    return [
        {"id": aid, "label": acc["label"], "configured": bool(acc["secret"])}
        for aid, acc in ACCOUNTS.items()
    ]


@app.get("/api/conversations")
async def api_conversations(account: str = "main"):
    return get_conversations(account)


@app.get("/api/search")
async def api_search(q: str = "", account: str = "main"):
    q = q.strip()
    if not q:
        return get_conversations(account)
    return search_conversations(q, account)


@app.get("/api/messages/{user_id}")
async def api_messages(user_id: str):
    messages = get_messages(user_id)
    draft = get_latest_draft(user_id)
    user = get_user(user_id)
    return {"messages": messages, "draft": draft, "user": user}


class CallNameRequest(BaseModel):
    name: str


@app.post("/api/call-name/{user_id}")
async def api_call_name(user_id: str, req: CallNameRequest):
    if not get_user(user_id):
        raise HTTPException(status_code=404, detail="User not found")
    set_call_name(user_id, req.name)
    return {"status": "ok", "call_name": req.name.strip()}


@app.post("/api/regenerate/{user_id}")
async def api_regenerate(user_id: str):
    history = get_messages(user_id)
    if not history:
        raise HTTPException(status_code=404, detail="No messages found")
    user = get_user(user_id)
    account = (user.get("account") if user else None) or "main"
    manual = get_manual_or_reload(account)
    draft = generate_reply(history, manual, customer_name=preferred_name(user_id))
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

    user = get_user(user_id)
    account = (user.get("account") if user else None) or "main"
    manual = get_manual_or_reload(account)
    draft = refine_reply(history, manual, preferred_name(user_id), req.draft, instruction)
    save_draft(user_id, draft)
    return {"draft": draft}


class SendRequest(BaseModel):
    text: str


@app.post("/api/send/{user_id}")
async def api_send(user_id: str, req: SendRequest):
    text = req.text.strip()
    if not text:
        raise HTTPException(status_code=400, detail="メッセージが空です")

    user = get_user(user_id)
    account = (user.get("account") if user else None) or "main"
    token = ACCOUNTS[account]["token"]
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
        "manual_chars": {aid: len(get_manual_content(aid)) for aid in ACCOUNTS},
    }


@app.post("/api/reload-manual")
async def api_reload_manual():
    lengths = load_manuals(ACCOUNT_SHEETS)
    total = sum(lengths.values())
    return {"status": "ok", "characters": total, "by_account": lengths}


# ─── Admin UI ──────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TEMPLATES.TemplateResponse(request, "index.html")
