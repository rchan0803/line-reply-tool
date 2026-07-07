"""エルメ（L Message）のMCPサーバー連携。

OAuth 2.0（PKCE + 動的クライアント登録）で認証し、
https://mcp.lmes.jp/mcp からデータを読み取る。
トークン類は DB と同じディレクトリの elme_oauth.json に保存する。
"""
import base64
import hashlib
import json
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

AUTH_BASE = "https://step.lme.jp"
MCP_URL = "https://mcp.lmes.jp/mcp"
AUTHORIZE_URL = AUTH_BASE + "/mcp/oauth/authorize"
TOKEN_URL = AUTH_BASE + "/api/mcp/oauth/token"
REGISTER_URL = AUTH_BASE + "/api/mcp/oauth/register"


def _store_path() -> str:
    db = os.getenv("DB_PATH", "line_chat.db")
    return os.path.join(os.path.dirname(db) or ".", "elme_oauth.json")


def _load() -> dict:
    try:
        with open(_store_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data: dict):
    with open(_store_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


# ── OAuth ──────────────────────────────────────────────────────

def ensure_client(redirect_uri: str) -> dict:
    """クライアント登録（済みなら再利用）。"""
    store = _load()
    client = store.get("client")
    if client and redirect_uri in (client.get("redirect_uris") or []):
        return client
    resp = httpx.post(REGISTER_URL, json={
        "client_name": "LINE返信案ツール",
        "redirect_uris": [redirect_uri],
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
        "token_endpoint_auth_method": "none",
    }, timeout=30)
    resp.raise_for_status()
    client = resp.json()
    store["client"] = client
    _save(store)
    return client


def start_auth(redirect_uri: str) -> str:
    """認可URLを生成して返す（PKCE）。"""
    client = ensure_client(redirect_uri)
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()
    ).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)

    store = _load()
    store["pending"] = {"verifier": verifier, "state": state, "redirect_uri": redirect_uri}
    _save(store)

    params = {
        "response_type": "code",
        "client_id": client["client_id"],
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    return AUTHORIZE_URL + "?" + urlencode(params)


def finish_auth(code: str, state: str) -> dict:
    """コールバックで受け取ったcodeをトークンに交換する。"""
    store = _load()
    pending = store.get("pending") or {}
    if not pending or pending.get("state") != state:
        raise ValueError("stateが一致しません（もう一度接続をやり直してください）")
    client = store.get("client") or {}

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": pending["redirect_uri"],
        "client_id": client.get("client_id", ""),
        "code_verifier": pending["verifier"],
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    resp.raise_for_status()
    token = resp.json()
    token["obtained_at"] = int(time.time())
    store["token"] = token
    store.pop("pending", None)
    _save(store)
    return token


def _refresh(store: dict) -> dict:
    token = store.get("token") or {}
    client = store.get("client") or {}
    if not token.get("refresh_token"):
        raise ValueError("エルメ未接続です（/elme/connect から接続してください）")
    data = {
        "grant_type": "refresh_token",
        "refresh_token": token["refresh_token"],
        "client_id": client.get("client_id", ""),
    }
    if client.get("client_secret"):
        data["client_secret"] = client["client_secret"]
    resp = httpx.post(TOKEN_URL, data=data, timeout=30)
    resp.raise_for_status()
    new_token = resp.json()
    new_token["obtained_at"] = int(time.time())
    if "refresh_token" not in new_token:
        new_token["refresh_token"] = token["refresh_token"]
    store["token"] = new_token
    _save(store)
    return new_token


def get_access_token() -> str | None:
    store = _load()
    token = store.get("token")
    if not token:
        return None
    expires_in = token.get("expires_in")
    if expires_in and time.time() > token.get("obtained_at", 0) + int(expires_in) - 60:
        token = _refresh(store)
    return token.get("access_token")


def is_connected() -> bool:
    return bool((_load().get("token") or {}).get("access_token"))


# ── MCPプロトコル ──────────────────────────────────────────────

def _parse_mcp_response(resp: httpx.Response):
    ctype = resp.headers.get("content-type", "")
    if "text/event-stream" in ctype:
        result = None
        for line in resp.text.splitlines():
            if line.startswith("data:"):
                try:
                    result = json.loads(line[len("data:"):].strip())
                except Exception:
                    pass
        return result
    if resp.text.strip():
        return resp.json()
    return None


def _post(payload: dict, token: str, session_id: str | None = None) -> httpx.Response:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return httpx.post(MCP_URL, json=payload, headers=headers, timeout=60)


def _open_session(token: str) -> str | None:
    resp = _post({
        "jsonrpc": "2.0", "id": 0, "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "line-reply-tool", "version": "1.0"},
        },
    }, token)
    resp.raise_for_status()
    session_id = resp.headers.get("mcp-session-id")
    try:
        _post({"jsonrpc": "2.0", "method": "notifications/initialized"}, token, session_id)
    except Exception:
        pass
    return session_id


def list_tools():
    token = get_access_token()
    if not token:
        raise ValueError("エルメ未接続です")
    session_id = _open_session(token)
    resp = _post({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}, token, session_id)
    resp.raise_for_status()
    return _parse_mcp_response(resp)


def call_tool(name: str, arguments: dict):
    token = get_access_token()
    if not token:
        raise ValueError("エルメ未接続です")
    session_id = _open_session(token)
    resp = _post({
        "jsonrpc": "2.0", "id": 2, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    }, token, session_id)
    resp.raise_for_status()
    return _parse_mcp_response(resp)
