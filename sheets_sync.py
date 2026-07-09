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
BUYER_WORKSHEET = os.getenv("BUYER_WORKSHEET", "鑑定購入者リスト")
BUYER_HEADER_ROW = 2  # 鑑定購入者リストのヘッダーは2行目

APPRAISAL_SHEET_ID = os.getenv("APPRAISAL_SHEET_ID", "15pVk9MtHgNZS7SfmU1zfjDAFGqpm-XmTbjRXsX4QdhQ")
APPRAISAL_WORKSHEET = os.getenv("APPRAISAL_WORKSHEET", "鑑定文出力")

# 有効な購入とみなすステータス（キャンセル・未払いは除外）
VALID_ORDER_STATUS = {"支払済み", "発送済み"}

# 転記対象のフォームID（キャンペーンフォームは別管理のため通常フォームのみ）
SYNC_FORM_IDS = [s.strip() for s in os.getenv("ELME_FORM_IDS", "118947").split(",") if s.strip()]

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
        if SYNC_FORM_IDS and str(form.get("id")) not in SYNC_FORM_IDS:
            continue  # キャンペーン用フォームなどは転記しない
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


def classify_order(product_name: str, status: str) -> dict:
    """注文の商品名・ステータスから、購入有効性とコース種別を判定する。"""
    name = product_name or ""
    valid = status in VALID_ORDER_STATUS
    # アップセル系（物販・施術）は鑑定コースではない
    is_upsell = any(k in name for k in ["ブレス", "ヒーリング", "パワーストーン", "セッション", "施術", "ストーン"])
    if is_upsell:
        course = "アップセル"
    elif "スタンダード" in name:
        course = "スタンダード"
    elif "ライト" in name:
        course = "ライト"
    else:
        course = "要確認"
    return {"valid": valid, "is_upsell": is_upsell, "course": course}


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
                status = row[5] if len(row) > 5 else ""
                product = row[4] if len(row) > 4 else ""
                cls = classify_order(product, status)
                results.append({
                    "注文番号": row[0], "注文日時": row[1],
                    "氏名": ((row[2] or "") + " " + (row[3] or "")).strip(),
                    "商品名": product, "ステータス": status,
                    "有効": cls["valid"], "コース": cls["course"], "アップセル": cls["is_upsell"],
                })
                break
    return results[-8:]  # 直近8件まで


def order_summary(names: list[str]) -> dict:
    """画面バッジ用の購入状況サマリを返す。"""
    orders = find_orders(names)
    valid = [o for o in orders if o["有効"] and not o["アップセル"]]
    upsell = [o for o in orders if o["有効"] and o["アップセル"]]
    cancelled = [o for o in orders if o["ステータス"] == "キャンセル"]
    unpaid = [o for o in orders if o["ステータス"] not in VALID_ORDER_STATUS and o["ステータス"] != "キャンセル"]
    if valid:
        latest = valid[-1]
        state = "purchased"
        label = f"✔ 購入確認済み：{latest['コース']}（{latest['ステータス']}・{latest['注文日時'][:10]}）"
    elif upsell:
        state = "upsell_only"
        label = f"✔ 特典購入あり：{upsell[-1]['商品名']}"
    elif cancelled:
        state = "cancelled"
        label = "⚠️ この名前の注文はキャンセルされています"
    elif unpaid:
        state = "unpaid"
        label = f"⚠️ 未確定の注文があります（{unpaid[-1]['ステータス']}）"
    else:
        state = "none"
        label = "該当する注文が見つかりません"
    return {"state": state, "label": label, "orders": orders}


# ── 鑑定購入者リストへの行追加・ヒアリング転記 ──────────────────

def _buyer_ws():
    return _get_client().open_by_key(BUYER_LIST_SHEET_ID).worksheet(BUYER_WORKSHEET)


def _find_buyer_row(ws, names: list[str], order_no: str = ""):
    """LINE名(B列)またはオーダー番号(G列)で既存行を探す。見つかれば行番号を返す。"""
    values = ws.get_values(f"A{BUYER_HEADER_ROW + 1}:G")
    keys = [re.sub(r"\s", "", n) for n in names if n]
    for i, row in enumerate(values):
        rownum = BUYER_HEADER_ROW + 1 + i
        b = re.sub(r"\s", "", row[1]) if len(row) > 1 else ""
        g = row[6].strip() if len(row) > 6 else ""
        if order_no and g and g == order_no:
            return rownum
        if b and any(b == k or (len(k) >= 3 and k in b) for k in keys):
            return rownum
    return None


def _first_empty_buyer_row(ws):
    """顧客データが未記入の最初の行を返す。
    No.(A)だけドラッグ済みの空行を追記先にする。既存顧客（顧客名等が入った行）は上書きしない。
    判定: B(LINE名)/C(顧客名)/F(受付日)/G(オーダー番号)/H(鑑定)/J(お悩み) がすべて空。
    """
    rng = ws.get_values(f"A{BUYER_HEADER_ROW + 1}:J")
    for i, r in enumerate(rng):
        def g(c):
            return r[c].strip() if len(r) > c else ""
        if not g(1) and not g(2) and not g(5) and not g(6) and not g(7) and not g(9):
            return BUYER_HEADER_ROW + 1 + i
    return len(ws.col_values(1)) + 1


def _next_buyer_no(ws):
    for v in reversed(ws.col_values(1)):
        if str(v).strip().isdigit():
            return int(v) + 1
    return 1


def buyer_preview(names: list[str], order_no: str = "") -> dict:
    """購入者リスト登録の事前確認（既存行があるか・追記先）。"""
    ws = _buyer_ws()
    existing = _find_buyer_row(ws, names, order_no)
    target = _first_empty_buyer_row(ws)
    # 追記先行に既にNo.があればそれを使い、なければ採番
    a = ws.col_values(1)
    existing_no = a[target - 1].strip() if target - 1 < len(a) else ""
    next_no = existing_no if existing_no.isdigit() else str(_next_buyer_no(ws))
    return {"existing_row": existing, "next_row": target, "next_no": next_no}


def add_buyer_row(line_name: str, customer_name: str, order: dict | None) -> dict:
    """鑑定購入者リストのB列が空の最初の行に登録する（重複時はスキップ）。"""
    ws = _buyer_ws()
    order_no = str(order.get("注文番号")) if order else ""
    names = [n for n in [customer_name, line_name] if n]
    existing = _find_buyer_row(ws, names, order_no)
    if existing:
        return {"status": "exists", "row": existing}

    target = _first_empty_buyer_row(ws)
    a = ws.col_values(1)
    existing_no = a[target - 1].strip() if target - 1 < len(a) else ""

    # B:LINE名 C:顧客名 F:受付日 G:オーダー番号 H:鑑定 ... M:購入（A列のNo.は既存があれば温存）
    if not existing_no.isdigit():
        ws.update(range_name=f"A{target}", values=[[_next_buyer_no(ws)]], value_input_option="USER_ENTERED")
        used_no = _next_buyer_no(ws) - 1
    else:
        used_no = int(existing_no)

    row_bm = [""] * 12  # B..M
    row_bm[0] = line_name       # B
    row_bm[1] = customer_name   # C
    if order:
        row_bm[5] = order_no                                    # G
        row_bm[6] = order.get("コース") or order.get("商品名") or ""  # H
        row_bm[11] = "○" if order.get("有効") else ""            # M
    ws.update(range_name=f"B{target}:M{target}", values=[row_bm], value_input_option="USER_ENTERED")
    return {"status": "added", "row": target, "no": used_no}


def transcribe_hearing(line_name: str, customer_name: str, hearing_text: str, order_no: str = "") -> dict:
    """ヒアリング回答を有料鑑定文作成A列へ転記し、購入者リストの受付日を記録する。"""
    text = (hearing_text or "").strip()
    if not text:
        return {"status": "empty"}

    # 1) 有料鑑定文作成「鑑定文出力」のA列・次の空行へ転記（B列は確認待ち＝自動生成はされない）
    aws = _get_client().open_by_key(APPRAISAL_SHEET_ID).worksheet(APPRAISAL_WORKSHEET)
    col_a = aws.col_values(1)
    arow = len(col_a) + 1
    aws.update(range_name=f"A{arow}:B{arow}", values=[[text, "確認待ち"]], value_input_option="USER_ENTERED")

    # 2) 鑑定購入者リストの該当行に受付日を記録（見つかった場合のみ）
    from datetime import datetime, timezone, timedelta
    today = datetime.now(timezone(timedelta(hours=9))).strftime("%Y/%m/%d")
    ws = _buyer_ws()
    names = [n for n in [customer_name, line_name] if n]
    buyer_row = _find_buyer_row(ws, names, order_no)
    if buyer_row:
        ws.update(range_name=f"F{buyer_row}", values=[[today]], value_input_option="USER_ENTERED")

    return {"status": "ok", "appraisal_row": arow, "buyer_row": buyer_row, "date": today}


# 鑑定文出力シートの列（1始まり）: A=お悩み E=現在のご状況 F=潜在的性格
# G=鑑定パート H=覚醒メソッド I=送付メッセージ
_APPRAISAL_PARTS = [
    ("お悩み・ヒアリング内容", 1),
    ("現在のご状況", 5),
    ("潜在的性格", 6),
    ("鑑定パート", 7),
    ("覚醒メソッド", 8),
]


def read_appraisal(row: int, max_chars: int = 20000) -> str:
    """有料鑑定文作成「鑑定文出力」の指定行から、鑑定内容の全文を組み立てて返す。"""
    if not row or row < 2:
        return ""
    aws = _get_client().open_by_key(APPRAISAL_SHEET_ID).worksheet(APPRAISAL_WORKSHEET)
    values = aws.get_values(f"A{row}:I{row}")
    if not values or not values[0]:
        return ""
    r = values[0]
    parts = []
    for label, col in _APPRAISAL_PARTS:
        v = r[col - 1].strip() if len(r) >= col else ""
        if v:
            parts.append(f"■{label}\n{v}")
    text = "\n\n".join(parts)
    return text[:max_chars]
