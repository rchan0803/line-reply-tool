import os
import hmac
import hashlib
import base64
import json
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv()

from database import (
    init_db, upsert_user, save_message, save_draft,
    get_conversations, get_messages, get_latest_draft, get_user,
    search_conversations, set_call_name, set_appraisal_row,
)
from sheets import load_manuals, get_manual_content
from claude_service import generate_reply, refine_reply
import elme_mcp
import elme_sync
import sheets_sync

TEMPLATES = Jinja2Templates(directory="templates")

# ─── アカウント設定 ────────────────────────────────────────────
# main: 公式LINE（無料鑑定側） / paid: 鑑定購入者専用LINE

MAIN_RULES = """このアカウントは「公式LINE」（無料鑑定の窓口）です。テンプレは「公式LINE」シートのものを使用します。
- 「本鑑定希望」等の連絡には有料鑑定案内テンプレで返す
- 鑑定文送付後のお礼・感想には「お礼返信」系テンプレで返す"""

PAID_RULES = """このアカウントは「購入者専用LINE（VIPルーム）」です。鑑定を購入した顧客だけが入室します。
- 新規顧客から「（友だち追加）」「（スタンプ）」や挨拶のみが届いた場合は、購入直後の入室とみなし、「相談内容ヒアリング」シートのヒアリング案内テンプレで返信案を作る（返信不要にしない）
- ヒアリング回答を受領したら「ヒアリング内容受領時」テンプレをもとに受領メッセージを作る
- 【最重要】鑑定書送付後に、感想・「【特別特典】」への言及や引用・「特典希望」・特典への質問のいずれかが届いたら、感想への返信に続けて、必ず「アップセル提案」テンプレをその顧客の悩み・状況・感想に合わせて書き換えた特典の詳細案内を同じメッセージ内に含める。顧客がまだ鑑定書を読み終えていなくても含める
- 顧客が明確に辞退した場合のみアップセルはせず、「ダウンセル提案」の利用を検討する
- 鑑定書送付後の質問・相談には、【お届けした鑑定内容】が与えられている場合はその見立て・アドバイスと一貫した内容で返信する（鑑定書と矛盾しないこと）"""

ACCOUNTS = {
    "main": {
        "label": "公式LINE",
        "secret": os.getenv("LINE_CHANNEL_SECRET", ""),
        "token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN", ""),
        "elme_url": os.getenv("ELME_WEBHOOK_URL", ""),
        "sheets": [s.strip() for s in os.getenv("MANUAL_SHEETS_MAIN", "公式LINE").split(",") if s.strip()],
        "elme_bot_id": os.getenv("ELME_BOT_ID", "2l97wR"),  # ミラ|星々の声を届ける恋愛占い師
        "rules": MAIN_RULES,
    },
    "paid": {
        "label": "購入者専用LINE",
        "secret": os.getenv("LINE_CHANNEL_SECRET_PAID", ""),
        "token": os.getenv("LINE_CHANNEL_ACCESS_TOKEN_PAID", ""),
        "elme_url": os.getenv("ELME_WEBHOOK_URL_PAID", ""),
        "sheets": [s.strip() for s in os.getenv("MANUAL_SHEETS_PAID", "有料鑑定専用LINE,相談内容ヒアリング").split(",") if s.strip()],
        "elme_bot_id": os.getenv("ELME_BOT_ID_PAID", "OoboML"),  # ミラ【VIPルーム】
        "rules": PAID_RULES,
    },
}

ACCOUNT_SHEETS = {aid: acc["sheets"] for aid, acc in ACCOUNTS.items()}


async def form_sync_loop():
    """30分ごとにエルメのフォーム回答を無料鑑定リストへ自動転記する。"""
    import asyncio
    await asyncio.sleep(90)  # 起動直後は避ける
    while True:
        try:
            result = await asyncio.to_thread(
                sheets_sync.sync_free_forms, ACCOUNTS["main"]["elme_bot_id"]
            )
            if result.get("new_count"):
                print(f"[form_sync] 新規{result['new_count']}件を転記")
        except Exception as e:
            print(f"[form_sync] エラー: {e}")
        await asyncio.sleep(1800)


async def order_import_loop():
    """25分ごとにSTORESの注文をオーダーシートへ取り込む。"""
    import asyncio
    await asyncio.sleep(150)  # フォーム同期とずらす
    while True:
        try:
            result = await asyncio.to_thread(sheets_sync.import_stores_orders)
            if result.get("status") == "ok":
                print(f"[stores] {result['count']}件を取り込み")
        except Exception as e:
            print(f"[stores] エラー: {e}")
        await asyncio.sleep(1500)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import asyncio
    init_db()
    load_manuals(ACCOUNT_SHEETS)
    task = asyncio.create_task(form_sync_loop())
    task2 = asyncio.create_task(order_import_loop())
    yield
    task.cancel()
    task2.cancel()


app = FastAPI(lifespan=lifespan)


# ─── ログイン認証（/webhook 以外を保護） ─────────────────────────
# ブラウザのBasic認証ダイアログは再起動のたびに出て使いづらいため、
# ログイン画面 + 長期Cookie方式にした。パスワードを変えると全Cookieが失効する。

SESSION_COOKIE = "session"
SESSION_MAX_AGE = 60 * 60 * 24 * 180  # 180日


def session_token() -> str:
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    return hmac.new(admin_password.encode(), b"session-v1", hashlib.sha256).hexdigest()


LOGIN_HTML = """<!DOCTYPE html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>ログイン - LINE返信案ツール</title>
<style>
  body{margin:0;min-height:100vh;display:grid;place-items:center;background:#F2F4F0;
       font-family:"Hiragino Kaku Gothic ProN","Yu Gothic UI",Meiryo,sans-serif}
  .box{background:#fff;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.08);
       padding:36px 34px;width:min(360px,88vw);text-align:center}
  h1{font-size:17px;margin:0 0 4px;color:#222}
  p{font-size:12px;color:#888;margin:0 0 22px}
  input{width:100%;box-sizing:border-box;font-size:15px;padding:12px 14px;
        border:1.5px solid #ddd;border-radius:10px;margin-bottom:14px;text-align:center}
  input:focus{outline:none;border-color:#06C755}
  button{width:100%;font-size:15px;font-weight:700;padding:12px;border:none;cursor:pointer;
         border-radius:10px;background:#06C755;color:#fff}
  button:hover{filter:brightness(1.05)}
  .err{color:#D33;font-size:12.5px;margin:0 0 14px}
</style></head><body>
<form class="box" method="post" action="/login">
  <h1>LINE 返信案ツール</h1>
  <p>パスワードを入力してください（一度入れると記憶されます）</p>
  {error}
  <input type="password" name="password" placeholder="パスワード" autofocus autocomplete="current-password">
  <button type="submit">ログイン</button>
</form></body></html>"""


@app.get("/login")
async def login_page():
    return HTMLResponse(LOGIN_HTML.replace("{error}", ""))


@app.post("/login")
async def login_submit(request: Request):
    import secrets as _secrets
    form = await request.form()
    password = str(form.get("password", "")).strip()
    admin_password = os.getenv("ADMIN_PASSWORD", "")
    if admin_password and _secrets.compare_digest(password, admin_password):
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(
            SESSION_COOKIE, session_token(),
            max_age=SESSION_MAX_AGE, httponly=True, secure=True, samesite="lax",
        )
        return resp
    return HTMLResponse(LOGIN_HTML.replace(
        "{error}", '<p class="err">パスワードが違います。もう一度お試しください。</p>'))


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    import secrets as _secrets
    from fastapi.responses import Response as PlainResponse

    admin_password = os.getenv("ADMIN_PASSWORD", "")
    path = request.url.path
    # /webhook系 はLINEからの通知用なので認証不要（署名検証で保護済み）
    if path.startswith("/webhook") or path == "/login" or not admin_password:
        return await call_next(request)

    cookie = request.cookies.get(SESSION_COOKIE, "")
    if cookie and _secrets.compare_digest(cookie, session_token()):
        return await call_next(request)

    # 旧方式（Basic認証ヘッダ付きURL等）も引き続き通す
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Basic "):
        try:
            decoded = base64.b64decode(auth[6:]).decode()
            _, _, password = decoded.partition(":")
            if _secrets.compare_digest(password, admin_password):
                return await call_next(request)
        except Exception:
            pass

    # APIはリダイレクトせず401（ブラウザのダイアログを出さないようWWW-Authenticateは付けない）
    if path.startswith("/api/"):
        return PlainResponse(status_code=401)
    return RedirectResponse("/login", status_code=303)


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


def profile_text(user_id: str) -> str:
    user = get_user(user_id)
    if not user:
        return ""
    text = elme_sync.format_profile(user.get("profile") or "")
    # 購入者LINEの顧客はSTORESの注文履歴も照合してAIに渡す
    if (user.get("account") or "main") == "paid":
        try:
            summary = sheets_sync.order_summary(_name_keys(user), extra_text=_inbound_text(user["user_id"]))
            lines = [
                f"- {o['注文日時'][:10]} {o['商品名']}（{o['ステータス']}）"
                for o in summary["orders"]
            ]
            text += "\n\n【STORES購入状況】" + summary["label"]
            if lines:
                text += "\n" + "\n".join(lines)
        except Exception as e:
            print(f"[orders] 照合エラー: {e}")
    return text


def _name_keys(user: dict) -> list[str]:
    return [user.get("call_name") or "", user.get("display_name") or ""]


def _inbound_text(user_id: str, limit: int = 20) -> str:
    """顧客が送ってきたメッセージ本文（購入時の名前が含まれることがある）をまとめる。"""
    msgs = get_messages(user_id, limit=limit)
    return " ".join(m["content"] for m in msgs if m["direction"] == "inbound")


def appraisal_text(user: dict) -> str:
    """VIP顧客にお届けした鑑定内容の全文を取得する。
    ①ヒアリング転記時に記録した鑑定シートの行から読む（生成後の全パート）
    ②行がなければエルメのプロフィールの鑑定文A/Bを全文で使う
    """
    if not user or (user.get("account") or "main") != "paid":
        return ""
    row = user.get("appraisal_row")
    if row:
        try:
            text = sheets_sync.read_appraisal(int(row))
            if text:
                return text
        except Exception as e:
            print(f"[appraisal] sheet read error: {e}")
    return elme_sync.extract_appraisal(user.get("profile") or "")


def try_elme_sync(user_id: str, account_id: str) -> dict:
    """エルメから履歴・プロフィールを取り込む（失敗しても処理は止めない）。"""
    try:
        return elme_sync.sync_user(user_id, ACCOUNTS[account_id].get("elme_bot_id", ""))
    except Exception as e:
        print(f"[elme_sync] {user_id}: {e}")
        return {"status": "error", "detail": str(e)}


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
        etype = event.get("type")
        if etype == "message":
            msg = event.get("message", {})
            mtype = msg.get("type")
            if mtype == "text":
                text = msg.get("text", "")
            elif mtype == "sticker":
                text = "（スタンプ）"
            elif mtype == "image":
                text = "（画像）"
            elif mtype == "video":
                text = "（動画）"
            elif mtype == "audio":
                text = "（音声）"
            elif mtype == "file":
                text = f"（ファイル: {msg.get('fileName', '')}）"
            elif mtype == "location":
                text = "（位置情報）"
            else:
                continue
        elif etype == "follow":
            text = "（友だち追加）"
        else:
            continue

        user_id = (event.get("source") or {}).get("userId")
        if not user_id or not text:
            continue

        # ユーザー情報を保存
        display_name = get_line_profile(user_id, acc["token"])
        upsert_user(user_id, display_name, account=account_id)

        # メッセージを保存
        save_message(user_id, "inbound", text)

        # エルメから履歴・プロフィールを取り込み（ベストエフォート）
        try_elme_sync(user_id, account_id)

        # 返信案を生成して保存
        history = get_messages(user_id)
        manual = get_manual_or_reload(account_id)
        draft = generate_reply(
            history, manual,
            customer_name=preferred_name(user_id),
            customer_profile=profile_text(user_id),
            account_rules=acc.get("rules", ""),
            appraisal_content=appraisal_text(get_user(user_id)),
        )
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
    order = None
    if user:
        try:
            order = sheets_sync.order_summary(_name_keys(user), extra_text=_inbound_text(user_id))
            order.pop("orders", None)  # 画面バッジには要約だけ返す
        except Exception as e:
            print(f"[orders] badge error: {e}")
    return {"messages": messages, "draft": draft, "user": user, "order": order}


@app.post("/api/buyer-preview/{user_id}")
async def api_buyer_preview(user_id: str):
    import asyncio
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        summary = await asyncio.to_thread(sheets_sync.order_summary, _name_keys(user))
        valid = [o for o in summary["orders"] if o["有効"] and not o["アップセル"]]
        order = valid[-1] if valid else None
        preview = await asyncio.to_thread(
            sheets_sync.buyer_preview, _name_keys(user),
            str(order["注文番号"]) if order else "",
        )
        return {
            "line_name": user.get("display_name") or "",
            "customer_name": user.get("call_name") or "",
            "order": order, "order_label": summary["label"],
            "existing_row": preview["existing_row"], "next_no": preview["next_no"],
        }
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/buyer-add/{user_id}")
async def api_buyer_add(user_id: str):
    import asyncio
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        summary = await asyncio.to_thread(sheets_sync.order_summary, _name_keys(user))
        valid = [o for o in summary["orders"] if o["有効"] and not o["アップセル"]]
        order = valid[-1] if valid else None
        result = await asyncio.to_thread(
            sheets_sync.add_buyer_row,
            user.get("display_name") or "", user.get("call_name") or "", order,
        )
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class HearingRequest(BaseModel):
    text: str


@app.post("/api/hearing-transcribe/{user_id}")
async def api_hearing_transcribe(user_id: str, req: HearingRequest):
    import asyncio
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    try:
        summary = await asyncio.to_thread(sheets_sync.order_summary, _name_keys(user))
        valid = [o for o in summary["orders"] if o["有効"] and not o["アップセル"]]
        order_no = str(valid[-1]["注文番号"]) if valid else ""
        result = await asyncio.to_thread(
            sheets_sync.transcribe_hearing,
            user.get("display_name") or "", user.get("call_name") or "",
            req.text, order_no,
        )
        # 鑑定内容の紐づけのため、転記先の行を顧客に記録
        if result.get("appraisal_row"):
            set_appraisal_row(user_id, result["appraisal_row"])
        return result
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/import-orders")
async def api_import_orders():
    """STORESから注文を取り込み、オーダーシートを更新する。"""
    import asyncio
    try:
        return await asyncio.to_thread(sheets_sync.import_stores_orders)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/sync-forms")
async def api_sync_forms(dry_run: int = 0):
    """エルメのフォーム回答を無料鑑定リストへ転記（dry_run=1で書き込まず確認）。"""
    import asyncio
    try:
        return await asyncio.to_thread(
            sheets_sync.sync_free_forms, ACCOUNTS["main"]["elme_bot_id"], bool(dry_run)
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/sync/{user_id}")
async def api_sync(user_id: str):
    """会話を開いたときにエルメから履歴・プロフィールを取り込む。"""
    user = get_user(user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    account = user.get("account") or "main"
    return try_elme_sync(user_id, account)


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
    draft = generate_reply(
        history, manual,
        customer_name=preferred_name(user_id),
        customer_profile=profile_text(user_id),
        account_rules=ACCOUNTS.get(account, {}).get("rules", ""),
        appraisal_content=appraisal_text(user),
    )
    save_draft(user_id, draft)
    return {"draft": draft}


# ─── エルメMCP連携 ─────────────────────────────────────────────

@app.get("/elme/connect")
async def elme_connect(request: Request):
    """エルメとのOAuth接続を開始（管理者がブラウザで開く）。"""
    base = str(request.base_url).rstrip("/")
    if base.startswith("http://") and "localhost" not in base and "127.0.0.1" not in base:
        base = "https://" + base[len("http://"):]
    try:
        url = elme_mcp.start_auth(base + "/elme/callback")
    except Exception as e:
        return HTMLResponse(f"<h3>接続開始に失敗しました</h3><pre>{e}</pre>", status_code=500)
    return RedirectResponse(url)


@app.get("/elme/callback")
async def elme_callback(code: str = "", state: str = "", error: str = "", error_description: str = ""):
    if error:
        return HTMLResponse(f"<h3>エルメ連携エラー</h3><p>{error}: {error_description}</p>", status_code=400)
    try:
        elme_mcp.finish_auth(code, state)
        return HTMLResponse("<h3>✅ エルメ連携が完了しました</h3><p>この画面は閉じて大丈夫です。</p>")
    except Exception as e:
        return HTMLResponse(f"<h3>エルメ連携に失敗しました</h3><pre>{e}</pre>", status_code=400)


@app.get("/api/elme/status")
async def api_elme_status():
    return {"connected": elme_mcp.is_connected()}


@app.get("/api/elme/tools")
async def api_elme_tools():
    """エルメMCPで使えるツール一覧（連携内容の調査用）。"""
    try:
        return elme_mcp.list_tools()
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


class ElmeCallRequest(BaseModel):
    name: str
    arguments: dict = {}


@app.post("/api/elme/call")
async def api_elme_call(req: ElmeCallRequest):
    """エルメMCPのツールを呼び出す（連携内容の調査用）。"""
    try:
        return elme_mcp.call_tool(req.name, req.arguments)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))


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
    draft = refine_reply(
        history, manual, preferred_name(user_id), req.draft, instruction,
        customer_profile=profile_text(user_id),
        appraisal_content=appraisal_text(user),
    )
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
