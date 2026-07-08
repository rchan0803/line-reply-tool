"""スプレッドシートへの書き込み連携。

- エルメのフォーム回答 → 無料鑑定リスト（顧客リスト）への自動追記
- STORESオーダーシートの照合（購入確認）
"""
import json
import os
import re
from datetime import datetime

import gspread
from google.oauth2.service_account import Credentials

import elme_mcp

FREE_LIST_SHEET_ID = os.getenv("FREE_LIST_SHEET_ID", "1IXBSQLNHZDzY77okHwMXyeLr77eSiXOsJ4JGqpl4LoQ")
FREE_LIST_WORKSHEET = os.getenv("FREE_LIST_WORKSHEET", "顧客リスト")
BUYER_LIST_SHEET_ID = os.getenv("BUYER_LIST_SHEET_ID", "1VTsF-pq1Ua7D7e4USnTTlZTURgEIIDZsfXbIIqurrf4")
ORDER_WORKSHEET = os.getenv("ORDER_WORKSHEET", "オーダー")

_client = None


def _get_client():
    global _client
    if _client is None:
        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
        if creds_json:
            info = json.loads(creds_json)
            creds = Credentials.from_service_account_info(info, scopes=scopes)
        else:
            creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
            creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
        _client = gspread.authorize(creds)
    return _client


# ── フォーム回答 → 無料鑑定リスト ──────────────────────────────

def _parse_answers(answers: list) -> dict:
    """フォームの設問名から列の意味を推定して振り分ける。"""
    out = {"name": "", "gender": "", "birth": "", "category": "", "situation": "", "future": ""}
    for a in answers or []:
        q = a.get("name") or ""
        t = a.get("type") or ""
        v = a.get("value")
        if isinstance(v, dict):
            v = v.get("date") or v.get("datetime") or json.dumps(v, ensure_ascii=False)
        v = (str(v) if v is not None else "").strip()
        if not v:
            continue
        if t == "name" or "名前" in q:
            out["name"] = v
        elif t == "gender" or "性別" in q:
            out["gender"] = v
        elif t == "date_time" or "生年月日" in q:
            out["birth"] = v
        elif t.startswith("select"):
            out["category"] = v  # お悩みカテゴリ（選択式）→ J列
        elif "理想" in q:
            out["future"] = v
        elif "状況" in q or "悩み" in q:
            out["situation"] = v
    return out


def _fmt_answered_at(text: str) -> str:
    try:
        dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S")
        return dt.strftime("%Y/%m/%d %H:%M:%S")
    except Exception:
        return text


def sync_free_forms(bot_id: str, dry_run: bool = False, per_form_limit: int = 100) -> dict:
    """エルメの全フォームの回答を取得し、未転記分を無料鑑定リストに追記する。"""
    if not elme_mcp.is_connected():
        return {"status": "not_connected"}

    ws = _get_client().open_by_key(FREE_LIST_SHEET_ID).worksheet(FREE_LIST_WORKSHEET)
    col_b = ws.col_values(2)  # B列: 回答ID（ヘッダー含む）
    existing_ids = set(col_b)
    last_row = len(col_b)

    forms = elme_mcp.call_tool_json("list_forms", {"bot_id": bot_id})
    new_entries = []
    for form in forms.get("forms", []):
        res = elme_mcp.call_tool_json("get_form_responses", {
            "bot_id": bot_id,
            "form_id": str(form.get("id")),
            "limit": per_form_limit,
        })
        for r in res.get("responses", []):
            rid = str(r.get("id"))
            if not rid or rid in existing_ids:
                continue
            existing_ids.add(rid)
            ans = _parse_answers(r.get("answers"))
            answered = _fmt_answered_at(r.get("answered_at") or "")
            row = [
                r.get("line_friend_id") or "",   # A: LINEユーザーID
                rid,                              # B: 回答ID
                answered,                         # C: 回答日時
                str(r.get("lmessage_friend_id") or ""),  # D: 回答者ID
                r.get("line_name") or "",         # E: LINE名
                ans["name"],                      # F: システム表示名
                ans["name"],                      # G: お名前
                ans["gender"],                    # H: 性別
                ans["birth"],                     # I: 生年月日
                ans["category"],                  # J: ご状況（カテゴリ選択）
                ans["situation"],                 # K: お悩み詳細
                ans["future"],                    # L: 理想の未来
            ]
            new_entries.append((answered, row))

    # 回答日時の古い順に追記
    new_entries.sort(key=lambda x: x[0])
    rows = [e[1] for e in new_entries]

    if dry_run or not rows:
        return {
            "status": "ok", "dry_run": dry_run, "new_count": len(rows),
            "preview": [[c[:20] for c in r] for r in rows[:5]],
        }

    ws.update(
        range_name=f"A{last_row + 1}:L{last_row + len(rows)}",
        values=rows,
        value_input_option="USER_ENTERED",
    )
    return {"status": "ok", "dry_run": False, "new_count": len(rows), "start_row": last_row + 1}


# ── STORESオーダー照合（購入確認） ──────────────────────────────

_orders_cache = {"rows": None, "at": 0.0}
_ORDERS_TTL = 600  # 10分キャッシュ


def _load_orders():
    ws = _get_client().open_by_key(BUYER_LIST_SHEET_ID).worksheet(ORDER_WORKSHEET)
    return ws.get_values("A2:F")


def find_orders(names: list[str], refresh: bool = False) -> list[dict]:
    """氏名候補（呼び名・表示名など）でSTORES注文を検索する。"""
    import time as _time
    if _orders_cache["rows"] is None or refresh or _time.time() - _orders_cache["at"] > _ORDERS_TTL:
        _orders_cache["rows"] = _load_orders()
        _orders_cache["at"] = _time.time()
    results = []
    keys = [re.sub(r"\s", "", n) for n in names if n and len(re.sub(r"\s", "", n)) >= 2]
    if not keys:
        return results
    for row in _orders_cache["rows"]:
        if len(row) < 5:
            continue
        full = re.sub(r"\s", "", (row[2] or "") + (row[3] or ""))  # 姓+名
        if not full:
            continue
        for k in keys:
            if k in full or full in k:
                results.append({
                    "注文番号": row[0], "注文日時": row[1],
                    "氏名": (row[2] or "") + " " + (row[3] or ""),
                    "商品名": row[4] if len(row) > 4 else "",
                    "ステータス": row[5] if len(row) > 5 else "",
                })
                break
    return results[-5:]  # 直近5件まで
