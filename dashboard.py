# -*- coding: utf-8 -*-
"""運営ダッシュボード（Railway版）。

画面の「更新」ボタンが押されたときだけ数字を取り直す。
取ってくるもの:
  売上        STORES API
  無料鑑定    【ミラ】【れんじゅ】無料鑑定リスト
  有料鑑定    【ミラ】鑑定購入者リスト／有料鑑定文作成
  LINE        エルメ（このアプリに入っているMCP連携をそのまま使う）
  SNS         Instagram / Threads
  ツール状態  投稿シートの最終投稿・ログ・/health・STORES取込のズレ
"""
import collections
import datetime
import json
import os
import pathlib
import re
import statistics
import threading
import time

import gspread
import httpx
from google.oauth2.service_account import Credentials

import dashboard_sns as sns_api
import elme_mcp

HERE = pathlib.Path(__file__).resolve().parent
JST = datetime.timezone(datetime.timedelta(hours=9))

# ── どのシートのどの列を見るか ───────────────────────────────
SHEETS = {
    "free_mira": {"sheet": os.getenv("FREE_LIST_SHEET_ID", "1IXBSQLNHZDzY77okHwMXyeLr77eSiXOsJ4JGqpl4LoQ"),
                  "ws": "顧客リスト", "header": 1,
                  "col_applied": 2, "col_sent": 12, "col_name": 6, "col_genre": 9},
    "free_renju": {"sheet": os.getenv("RENJU_FREE_SHEET_ID", "1NWaEDHL8fJK34FfVHaHBmuMFMMHFljiiMOe6dFaF8qw"),
                   "ws": "顧客リスト", "header": 1,
                   "col_applied": 8, "col_sent": 9, "col_name": 2, "col_genre": 5},
    "buyers": {"sheet": os.getenv("BUYER_LIST_SHEET_ID", "1VTsF-pq1Ua7D7e4USnTTlZTURgEIIDZsfXbIIqurrf4"),
               "ws": "鑑定購入者リスト", "header": 2, "col_received": 5, "col_sent": 10,
               "col_name": 1, "col_item": 7, "col_price": 8, "col_worry": 9, "col_order_no": 6},
    "orders_sheet": {"sheet": os.getenv("BUYER_LIST_SHEET_ID", "1VTsF-pq1Ua7D7e4USnTTlZTURgEIIDZsfXbIIqurrf4"),
                     "ws": "オーダー", "header": 1, "col_date": 1},
    "appraisal": {"sheet": os.getenv("APPRAISAL_SHEET_ID", "15pVk9MtHgNZS7SfmU1zfjDAFGqpm-XmTbjRXsX4QdhQ"),
                  "ws": "鑑定文出力", "header": 1, "col_status": 1},
    "threads_mira": {"sheet": os.getenv("THREADS_SHEET_ID", "1zv5gAgYuDmv_u2avpxDXdSmiyY2QtW1yZYwhajjNw5E"),
                     "ws": "登録内容", "header": 1, "col_time": 0, "col_status": 3,
                     "done": "登録済", "creds": "threads", "config_ws": "config"},
    "insta_log": {"sheet": os.getenv("INSTA_SHEET_ID", "1ZNowgdKBR35mn7SplbwyQCs_7taXNiNPLBM-0_oRO7I"),
                  "ws": "ログ"},
}

CHECKS = {
    "threads_mira": {"ok_hours": 24, "warn_hours": 48},
    "insta_auto": {"ok_hours": 2, "warn_hours": 12},
    "stores_sync": {"ok_hours": 2, "warn_hours": 24},
    "renju_health": {"url": os.getenv("RENJU_HEALTH_URL",
                                      "https://renju-line-tool-production.up.railway.app/health"),
                     "ok_minutes": 30, "warn_minutes": 180},
    "comment_reply": {"log_rows": int(os.getenv("INSTA_LOG_ROWS", "14000"))},
}

RULES = {"paid_warn_days": 7, "paid_alert_days": 14, "free_warn_days": 3,
         "line_recent_days": 7, "paid_stale_days": 60}

PRODUCT_KINDS = {
    "アップセル": ["ブレス", "パワーストーン", "ヒーリング", "オルゴナイト", "セッション", "運命調整", "お守り"],
    "鑑定書ローンチ": ["収穫月", "ライオンズゲート", "羅針盤"],
}
PAID_NOT_APPRAISAL = ["パワーストーン", "ブレス", "ヒーリング", "オルゴナイト", "セッション"]
AUTO_REPLY_PREFIXES = ["お申し込みありがとうございます", "無料鑑定を受け付けました"]

ELME_BOTS = [{"bot_id": "2l97wR", "name": "ミラ 公式LINE"},
             {"bot_id": "OoboML", "name": "ミラ VIPルーム"}]

HINTS = {
    "ig_posts": "投稿そのものが減っている。予約投稿が埋まっているか確認する",
    "ig_reach": "リーチが落ちている。保存・シェアされる投稿を増やす",
    "ig_comments": "コメントが減ると自動DMの入口が減る。コメント誘導の一文を入れ直す",
    "ig_total_comments": "コメントの総数が減ると、自動DMで誘導できる人数がそのまま減る",
    "th_posts": "Threadsの投稿数が減っている。スプシの予約を埋める",
    "friend_adds": "リストインが止まっている。インスタの自動DMが返せているかを先に見る",
    "free_applied": "申込が減っている。友だち追加の減りと、既存リストへの案内が止まっていないか",
    "free_sent": "送るのが止まっている。鑑定文の生成と送付の手が足りているか",
    "paid_orders": "無料鑑定のあとの本鑑定の案内を見直す",
    "upsell_orders": "鑑定書のアップセル文と特典ページを見直す",
    "sales": "上に出ている原因の結果。上から順に手を打つ",
    "comment_to_friend": "コメントした人をLINEに送れていない。自動DMの失敗を直すのが最優先",
    "apply_rate": "追加した人が申し込んでいない。あいさつメッセージと無料鑑定の案内を見直す",
    "send_rate": "申込に対して送付が追いついていない。鑑定文づくりの手を増やす",
    "cvr": "無料鑑定を送ったあとの本鑑定の案内文とタイミングを見直す",
    "upsell_rate": "アップセルのCVRが落ちている（目標8%）。鑑定書の「さいごに」と特典ページを直す",
    "aov": "単価が落ちている。低単価の鑑定書ローンチの比率が上がっていないか",
}

MANUAL_TOOLS = [
    {"name": "れんじゅ Threads自動投稿", "who": "れんじゅ", "state": "down", "auto": False,
     "fact": "自動判定なし（手で記録）",
     "detail": "2026-09-17から投稿停止。renju_ama がMetaのcheckpointでロック", "rule": ""},
    {"name": "れんじゅ 無料鑑定 生成GAS", "who": "れんじゅ", "state": "warn", "auto": False,
     "fact": "自動判定なし（手で記録）",
     "detail": "コードは完成。スプシへの貼付・APIキー設定・自動化オンが未", "rule": ""},
]


# ── 小物 ────────────────────────────────────────────────
def now():
    return datetime.datetime.now(JST)


def month_key(d):
    return f"{d.year}-{d.month:02d}"


def parse_dt(s):
    s = (s or "").strip().replace("　", " ")
    if not s or s == "-":
        return None
    s = s.replace("年", "/").replace("月", "/").replace("日", "")
    m = re.match(r"(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:[ T](\d{1,2}):(\d{2}))?", s)
    if not m:
        return None
    y, mo, da, hh, mi = m.groups()
    try:
        return datetime.datetime(int(y), int(mo), int(da), int(hh or 0), int(mi or 0), tzinfo=JST)
    except ValueError:
        return None


def days_since(dt):
    return (now() - dt).days if dt else None


def cell(row, idx):
    return row[idx].strip() if idx < len(row) else ""


_clients = {}


def _client(which="main"):
    """Googleスプレッドシートの読み取りクライアント。

    main    … GOOGLE_CREDENTIALS_JSON（このアプリの既存の鍵）
    threads … THREADS_CREDENTIALS_JSON（Threads投稿スプシ用。無ければ main を使う）
    """
    if which in _clients:
        return _clients[which]
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    raw = os.getenv("THREADS_CREDENTIALS_JSON", "") if which == "threads" else ""
    if not raw:
        raw = os.getenv("GOOGLE_CREDENTIALS_JSON", "")
    if raw:
        creds = Credentials.from_service_account_info(json.loads(raw), scopes=scopes)
    else:
        path = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        creds = Credentials.from_service_account_file(path, scopes=scopes)
    _clients[which] = gspread.authorize(creds)
    return _clients[which]


def sheet_values(key):
    s = SHEETS[key]
    ws = _client(s.get("creds", "main")).open_by_key(s["sheet"]).worksheet(s["ws"])
    return ws.get_all_values()


# ── 売上（STORES） ──────────────────────────────────────
def fetch_stores():
    token = os.getenv("STORES_CREDENTIAL", "")
    if not token:
        return {"error": "STORES_CREDENTIAL が設定されていません", "orders": []}
    out, offset = [], 0
    with httpx.Client(timeout=40) as c:
        while True:
            r = c.get("https://api.stores.dev/retail/202211/orders",
                      params={"limit": 50, "offset": offset},
                      headers={"Authorization": f"Bearer {token}"})
            if r.status_code == 429:
                time.sleep(20)
                continue
            if r.status_code != 200:
                return {"error": f"STORES APIエラー {r.status_code}", "orders": out}
            body = r.json()
            got = body if isinstance(body, list) else (body.get("orders") or [])
            out.extend(got)
            if len(got) < 50:
                break
            offset += 50
            time.sleep(0.2)
    return {"error": None, "orders": out}


def sales_metrics(raw):
    N = now()
    orders = []
    for o in raw.get("orders", []):
        dt = parse_dt((o.get("ordered_at") or "")[:16].replace("T", " "))
        amount = (o.get("sales_amount") or 0) - (o.get("cancel_amount") or 0)
        items = [i.get("name", "") for d in (o.get("deliveries") or []) for i in (d.get("items") or [])]
        canceled = all(d.get("canceled_at") for d in (o.get("deliveries") or [])) if o.get("deliveries") else False
        if not dt or canceled or amount <= 0:
            continue
        orders.append({"at": dt, "amount": amount, "items": items, "no": o.get("number", "")})
    orders.sort(key=lambda x: x["at"], reverse=True)

    def kind_of(names):
        joined = " ".join(names)
        for key, words in PRODUCT_KINDS.items():
            if any(w in joined for w in words):
                return key
        return "本鑑定"

    by_kind = collections.defaultdict(lambda: collections.defaultdict(lambda: {"count": 0, "amount": 0}))
    for o in orders:
        box = by_kind[kind_of(o["items"])][month_key(o["at"])]
        box["count"] += 1
        box["amount"] += o["amount"]

    months = []
    for back in range(5, -1, -1):
        y, m = N.year, N.month - back
        while m <= 0:
            m += 12
            y -= 1
        key = f"{y}-{m:02d}"
        rows = [o for o in orders if month_key(o["at"]) == key]
        months.append({"key": key, "label": f"{m}月", "amount": sum(o["amount"] for o in rows),
                       "count": len(rows), "current": back == 0})

    cur = [o for o in orders if month_key(o["at"]) == month_key(N)]
    by_item = collections.Counter()
    for o in cur:
        by_item[o["items"][0] if o["items"] else "（商品名なし）"] += o["amount"]
    today_rows = [o for o in orders if o["at"].date() == N.date()]
    return {
        "error": raw.get("error"), "months": months,
        "this_month": {"amount": sum(o["amount"] for o in cur), "count": len(cur)},
        "today": {"amount": sum(o["amount"] for o in today_rows), "count": len(today_rows)},
        "by_item": [{"name": k, "amount": v} for k, v in by_item.most_common(8)],
        "recent": [{"at": o["at"].strftime("%m/%d %H:%M"), "amount": o["amount"],
                    "item": (o["items"][0] if o["items"] else ""), "no": o["no"]} for o in orders[:10]],
        "latest_at": orders[0]["at"].isoformat() if orders else None,
        "by_kind": {k: dict(v) for k, v in by_kind.items()},
        "m_amount": {m: sum(by_kind[k].get(m, {}).get("amount", 0) for k in by_kind)
                     for m in {month_key(o["at"]) for o in orders}},
        "order_dates": {o["no"]: o["at"] for o in orders if o["no"]},
    }


# ── 無料鑑定 ────────────────────────────────────────────
def free_metrics(key, label):
    N = now()
    s = SHEETS[key]
    rows = sheet_values(key)[s["header"]:]
    applied, pending = [], []
    m_applied, m_sent = collections.Counter(), collections.Counter()
    for r in rows:
        a = parse_dt(cell(r, s["col_applied"]))
        if not a:
            continue
        applied.append(a)
        m_applied[month_key(a)] += 1
        sent = cell(r, s["col_sent"])
        sd = parse_dt(sent)
        if sd:
            m_sent[month_key(sd)] += 1
        if sent == "":
            pending.append({"at": a.strftime("%m/%d"), "days": days_since(a),
                            "name": cell(r, s["col_name"]) or "（名前なし）",
                            "genre": cell(r, s["col_genre"])})
    prev_month = N.replace(day=1) - datetime.timedelta(days=1)
    by_day = collections.Counter(a.date().isoformat() for a in applied)
    return {"label": label, "total": len(applied),
            "this_month": sum(1 for a in applied if month_key(a) == month_key(N)),
            "last_month": sum(1 for a in applied if month_key(a) == month_key(prev_month)),
            "last7": sum(1 for a in applied if (N - a).days < 7),
            "pending": sorted(pending, key=lambda x: -(x["days"] or 0)),
            "by_day": by_day, "m_applied": dict(m_applied), "m_sent": dict(m_sent)}


# ── 有料鑑定 ────────────────────────────────────────────
def paid_metrics(order_dates):
    N = now()
    s = SHEETS["buyers"]
    rows = sheet_values("buyers")[s["header"]:]
    pending, other, received = [], [], []
    for r in rows:
        name = cell(r, s["col_name"])
        rec = parse_dt(cell(r, s["col_received"])) or order_dates.get(cell(r, s["col_order_no"]))
        if not rec and not name:
            continue
        if rec:
            received.append(rec)
        if cell(r, s["col_sent"]):
            continue
        item = cell(r, s["col_item"])
        row = {"at": rec.strftime("%m/%d") if rec else "日付なし", "days": days_since(rec),
               "from_order": bool(rec and not cell(r, s["col_received"])),
               "name": name or "（名前なし）", "item": item,
               "price": cell(r, s["col_price"]), "worry": cell(r, s["col_worry"])[:40]}
        (other if any(k in item for k in PAID_NOT_APPRAISAL) else pending).append(row)

    def key(x):
        return -(x["days"] if x["days"] is not None else -1)

    pending.sort(key=key)
    other.sort(key=key)
    stale = [x for x in pending if (x["days"] or 0) > RULES["paid_stale_days"]]
    pending = [x for x in pending if (x["days"] or 0) <= RULES["paid_stale_days"]]

    a = SHEETS["appraisal"]
    st = [cell(r, a["col_status"]) for r in sheet_values("appraisal")[a["header"]:]]
    counts = collections.Counter(x for x in st if x)
    return {"pending": pending, "stale": stale, "other": other,
            "stale_days": RULES["paid_stale_days"],
            "this_month": sum(1 for d in received if month_key(d) == month_key(N)),
            "writing_queue": {"確認待ち": counts.get("確認待ち", 0), "生成済み": counts.get("生成済み", 0)}}


# ── インスタのログ（ツール判定とコメント返信率で使い回す） ─────
_log_cache = {"rows": None}


def insta_log_tail(rows_wanted):
    if _log_cache["rows"] is not None:
        return _log_cache["rows"]
    s = SHEETS["insta_log"]
    ws = _client().open_by_key(s["sheet"]).worksheet(s["ws"])
    n = ws.row_count
    got = ws.get(f"A{max(2, n - rows_wanted)}:B{n}")
    _log_cache["rows"] = [r for r in got if len(r) > 1 and r[0].strip()]
    return _log_cache["rows"]


def comment_reply_stats():
    try:
        rows = insta_log_tail(CHECKS["comment_reply"]["log_rows"])
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"[:120]}
    if not rows:
        return {"ok": False, "error": "ログが読めませんでした"}
    user = re.compile(r"@([A-Za-z0-9._]+)")
    ok, ng, skip = collections.defaultdict(set), collections.defaultdict(set), collections.defaultdict(set)
    reasons = collections.Counter()
    ok_all, ng_all = set(), set()
    for ts, msg in [(r[0], r[1]) for r in rows]:
        m = user.search(msg)
        if not m:
            continue
        day, who = ts.split()[0], m.group(1)
        if msg.startswith("自動返信"):
            ok[day].add(who)
            ok_all.add(who)
        elif msg.startswith("DM送信失敗"):
            ng[day].add(who)
            ng_all.add(who)
            tail = msg.split("→", 1)[-1]
            if "too old" in tail:
                reasons["コメントから時間が経ちすぎてDMを返せない（7日の決まり）"] += 1
            elif "[100]" in tail:
                reasons["相手がDMを受け取れない状態［100］"] += 1
            else:
                reasons[tail.strip()[:44] or "理由なし"] += 1
        elif msg.startswith("スキップ"):
            skip[day].add(who)
    days = []
    for day in sorted(set(list(ok) + list(ng) + list(skip))):
        o, x = len(ok[day]), len(ng[day] - ok[day])
        days.append({"date": day, "ok": o, "ng": x, "skip": len(skip[day]),
                     "rate": round(o / (o + x) * 100) if (o + x) else None})
    only_ng = ng_all - ok_all
    total = len(ok_all) + len(only_ng)
    return {"ok": True, "from": rows[0][0], "to": rows[-1][0], "lines": len(rows),
            "replied": len(ok_all), "missed": len(only_ng),
            "rate": round(len(ok_all) / total * 100) if total else None, "days": days,
            "reasons": [{"label": k, "count": v} for k, v in reasons.most_common(4)]}


# ── ツールの稼働状況 ────────────────────────────────────
def hours_since(dt):
    return (now() - dt).total_seconds() / 3600 if dt else None


def grade(h, ok, warn):
    if h is None:
        return "unknown"
    return "ok" if h <= ok else ("warn" if h <= warn else "down")


def check_tools(sales):
    tools = []
    try:
        s = SHEETS["threads_mira"]
        rows = sheet_values("threads_mira")[s["header"]:]
        done = [parse_dt(cell(r, s["col_time"])) for r in rows if cell(r, s["col_status"]) == s["done"]]
        last = max([d for d in done if d], default=None)
        h = hours_since(last)
        tools.append({"name": "ミラ Threads自動投稿", "who": "ミラ", "auto": True,
                      "state": grade(h, CHECKS["threads_mira"]["ok_hours"], CHECKS["threads_mira"]["warn_hours"]),
                      "fact": f"最後に投稿できたのは {last.strftime('%m/%d %H:%M')}" if last else "投稿済みの記録なし",
                      "rule": f"{CHECKS['threads_mira']['ok_hours']}時間以内に投稿があれば正常",
                      "detail": f"投稿登録シートで「登録済」になった最新の行（{len(done)}件）"})
    except Exception as e:
        tools.append({"name": "ミラ Threads自動投稿", "who": "ミラ", "auto": True, "state": "unknown",
                      "fact": f"確認できず（{type(e).__name__}）",
                      "detail": "THREADS_CREDENTIALS_JSON の設定か、スプシの共有を確認", "rule": ""})
    try:
        tail = insta_log_tail(CHECKS["comment_reply"]["log_rows"])
        last = parse_dt(tail[-1][0]) if tail else None
        errs = [r for r in tail if len(r) > 1 and parse_dt(r[0])
                and (now() - parse_dt(r[0])).total_seconds() < 86400
                and ("エラー" in r[1] or "失敗" in r[1] or "スパム" in r[1] or "レート制限" in r[1])]
        spam = [r for r in errs if "スパム" in r[1] or "レート制限" in r[1]]
        kinds = collections.Counter(re.split(r"[:：]", r[1])[0] for r in errs)
        top = "／".join(f"{k} {v}件" for k, v in kinds.most_common(2))
        h = hours_since(last)
        state = grade(h, CHECKS["insta_auto"]["ok_hours"], CHECKS["insta_auto"]["warn_hours"])
        if spam or (state == "ok" and len(errs) >= 5):
            state = "warn"
        tools.append({"name": "insta-auto（インスタ自動投稿・DM）", "who": "ミラ", "auto": True, "state": state,
                      "fact": (f"最後に動いたのは {last.strftime('%m/%d %H:%M')}" if last else "ログなし")
                              + (f"／直近24時間のエラー {len(errs)}件" if errs else "／エラーなし")
                              + (f"／スパム判定・レート制限 {len(spam)}件" if spam else ""),
                      "rule": f"{CHECKS['insta_auto']['ok_hours']}時間以内に動作・エラー5件未満なら正常",
                      "detail": top or "ログシートの最終行で判定"})
    except Exception as e:
        tools.append({"name": "insta-auto（インスタ自動投稿・DM）", "who": "ミラ", "auto": True,
                      "state": "unknown", "fact": f"確認できず（{type(e).__name__}）", "detail": "", "rule": ""})
    try:
        s = SHEETS["orders_sheet"]
        rows = sheet_values("orders_sheet")[s["header"]:]
        last = max([d for d in (parse_dt(cell(r, s["col_date"])) for r in rows) if d], default=None)
        api_last = parse_dt(sales["latest_at"][:16].replace("T", " ")) if sales.get("latest_at") else None
        gap = (api_last - last).total_seconds() / 3600 if (api_last and last) else None
        tools.append({"name": "STORES注文の取り込み", "who": "ミラ", "auto": True,
                      "state": "unknown" if gap is None else grade(gap, CHECKS["stores_sync"]["ok_hours"],
                                                                   CHECKS["stores_sync"]["warn_hours"]),
                      "fact": (f"シートの最新 {last.strftime('%m/%d %H:%M')}／STORESの最新 "
                               f"{api_last.strftime('%m/%d %H:%M')}") if (last and api_last) else "比較できず",
                      "rule": f"ズレが{CHECKS['stores_sync']['ok_hours']}時間以内なら正常",
                      "detail": "このツールが25分ごとに取り込む。ズレていれば取り込みが止まっている"})
    except Exception as e:
        tools.append({"name": "STORES注文の取り込み", "who": "ミラ", "auto": True, "state": "unknown",
                      "fact": f"確認できず（{type(e).__name__}）", "detail": "", "rule": ""})
    try:
        c = CHECKS["renju_health"]
        d = httpx.get(c["url"], timeout=20).json()
        last = None
        if d.get("last_poll_at"):
            hh, mm = str(d["last_poll_at"]).split(":")[:2]
            last = now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if last > now() + datetime.timedelta(minutes=10):
                last -= datetime.timedelta(days=1)
            last = min(last, now())
        mins = (now() - last).total_seconds() / 60 if last else None
        state = "down" if not d.get("poll_enabled") else (
            "warn" if d.get("last_poll_error") else grade(mins, c["ok_minutes"], c["warn_minutes"]))
        res = d.get("last_poll_result") or {}
        tools.append({"name": "れんじゅ LINE返信ツール", "who": "れんじゅ", "auto": True, "state": state,
                      "fact": f"最後の同期 {d.get('last_poll_at')}"
                              + (f"／エラー: {d['last_poll_error']}" if d.get("last_poll_error") else "／エラーなし"),
                      "rule": f"自動同期オンで{c['ok_minutes']}分以内に同期していれば正常",
                      "detail": f"/health より（前回 取込{res.get('synced', '-')}件／下書き{res.get('drafted', '-')}件）"})
    except Exception as e:
        tools.append({"name": "れんじゅ LINE返信ツール", "who": "れんじゅ", "auto": True, "state": "down",
                      "fact": f"/health につながらない（{type(e).__name__}）",
                      "detail": CHECKS["renju_health"]["url"], "rule": ""})
    return tools + MANUAL_TOOLS


# ── LINE（エルメ） ──────────────────────────────────────
def line_metrics():
    if not elme_mcp.is_connected():
        return {"missing": True, "bots": [], "error": "エルメが未接続です（設定画面から接続してください）"}
    bots, adds = [], {}
    try:
        for b in ELME_BOTS:
            res = elme_mcp.call_tool_json("list_conversations",
                                          {"bot_id": b["bot_id"], "confirm_status": 1, "limit": 100})
            convs = res.get("conversations", []) if isinstance(res, dict) else []
            waiting = []
            for w in convs:
                at = parse_dt((w.get("last_time_message") or "")[:16])
                if not at:
                    continue
                last = (w.get("last_message") or "").replace("\n", " ")[:90]
                waiting.append({"name": w.get("line_name") or "（名前なし）",
                                "at_label": at.strftime("%m/%d %H:%M"), "days": (now() - at).days,
                                "status": w.get("handling_status_name"), "last": last,
                                "auto_reply": any(a in last[:40] for a in AUTO_REPLY_PREFIXES)})
            stats = {}
            try:
                stats = elme_mcp.call_tool_json("get_friend_stats", {"bot_id": b["bot_id"]}) or {}
            except Exception:
                pass
            if b["bot_id"] == ELME_BOTS[0]["bot_id"]:
                per = collections.Counter()
                for d in stats.get("friend_changes", []):
                    dt = parse_dt(d.get("statistic_date", ""))
                    if dt:
                        per[month_key(dt)] += d.get("count_user_followed", 0)
                adds = dict(per)
            bots.append({"name": b["name"], "bot_id": b["bot_id"],
                         "friends_active": stats.get("total_follow_friend") or 0,
                         "unread_total": res.get("total", len(waiting)) if isinstance(res, dict) else len(waiting),
                         "waiting": waiting,
                         "recent": sum(1 for w in waiting if w["days"] < RULES["line_recent_days"]),
                         "need_reply": sum(1 for w in waiting
                                           if w["days"] < RULES["line_recent_days"] and not w["auto_reply"])})
    except Exception as e:
        return {"missing": True, "bots": [], "error": f"{type(e).__name__}: {e}"[:150]}
    return {"missing": False, "fetched": now().strftime("%Y-%m-%d %H:%M"), "stale_hours": 0,
            "recent_days": RULES["line_recent_days"], "bots": bots, "friend_adds": adds}


# ── SNS ────────────────────────────────────────────────
def sns_collect():
    out = {}
    try:
        s = SHEETS["threads_mira"]
        cfg = _client("threads").open_by_key(s["sheet"]).worksheet(s["config_ws"]).get_all_values()
        token = next((c.strip() for r in cfg for c in r if len(c.strip()) > 60), "")
        out["threads"] = sns_api.threads_metrics(token) if token else {
            "ok": False, "error": "configシートにトークンがありません", "months": {}, "recent": []}
    except Exception as e:
        out["threads"] = {"ok": False, "error": f"{type(e).__name__}: {e}"[:120],
                          "months": {}, "recent": []}
    token = os.getenv("IG_DM_TOKEN", "")
    out["instagram"] = sns_api.instagram_metrics(token) if token else {
        "ok": False, "error": "IG_DM_TOKEN が設定されていません", "months": {}, "recent": []}
    return out


# ── ファネルと診断 ──────────────────────────────────────
def last_months(n):
    out, y, m = [], now().year, now().month
    for _ in range(n):
        out.append(f"{y}-{m:02d}")
        m -= 1
        if m == 0:
            y, m = y - 1, 12
    return list(reversed(out))


def _recent_avg(rows, field):
    xs = [r.get(field) for r in rows if r.get(field) is not None]
    return round(sum(xs) / len(xs)) if xs else None


def funnel(sales, free, line, sns_data):
    months = last_months(7)
    adds = (line or {}).get("friend_adds", {}) or {}
    ig, th = sns_data.get("instagram", {}), sns_data.get("threads", {})

    def pick(src, field):
        since = src.get("since")
        out = {}
        for m in months:
            box = src.get("months", {}).get(m)
            out[m] = box.get(field) if box else (0 if (since and m >= since) else None)
        return out

    def kind(k, field):
        return {m: sales.get("by_kind", {}).get(k, {}).get(m, {}).get(field, 0) for m in months}

    ig_posts, ig_comments = pick(ig, "posts"), pick(ig, "comments_avg")
    ig_total = {m: (round(ig_posts[m] * ig_comments[m]) if (ig_posts.get(m) and ig_comments.get(m)) else None)
                for m in months}
    mira = free[0]
    stages = [
        {"key": "ig_posts", "label": "Instagram 投稿数", "unit": "本", "values": ig_posts},
        {"key": "ig_comments", "label": "Instagram 1投稿あたりのコメント", "unit": "件", "values": ig_comments},
        {"key": "ig_total_comments", "label": "Instagram コメント合計", "unit": "件", "values": ig_total,
         "note": "投稿数×平均コメント。自動DMを送る相手の数にあたる"},
        {"key": "th_posts", "label": "Threads 投稿数", "unit": "本", "values": pick(th, "posts")},
        {"key": "friend_adds", "label": "LINE友だち追加", "unit": "人", "values": {m: adds.get(m, 0) for m in months}},
        {"key": "free_applied", "label": "無料鑑定の申込（ミラ）", "unit": "件",
         "values": {m: mira["m_applied"].get(m, 0) for m in months}},
        {"key": "free_sent", "label": "無料鑑定の送付（ミラ）", "unit": "件",
         "values": {m: mira["m_sent"].get(m, 0) for m in months}},
        {"key": "paid_orders", "label": "本鑑定の購入", "unit": "件", "values": kind("本鑑定", "count")},
        {"key": "launch_orders", "label": "鑑定書ローンチの購入", "unit": "件", "values": kind("鑑定書ローンチ", "count")},
        {"key": "upsell_orders", "label": "アップセルの購入", "unit": "件", "values": kind("アップセル", "count"),
         "note": "STORES経由ぶんだけ"},
        {"key": "sales", "label": "売上", "unit": "円", "values": {m: sales.get("m_amount", {}).get(m, 0) for m in months}},
    ]
    v = {x["key"]: x["values"] for x in stages}

    def rate(num, den):
        return {m: (round(v[num][m] / v[den][m] * 100, 1)
                    if (v[den].get(m) and v[num].get(m) is not None) else None) for m in months}

    def aov(m):
        n = sum(v[k].get(m) or 0 for k in ("paid_orders", "launch_orders", "upsell_orders"))
        return round(v["sales"][m] / n) if n else None

    rates = [
        {"key": "comment_to_friend", "label": "インスタのコメント → LINE友だち追加", "unit": "%",
         "values": rate("friend_adds", "ig_total_comments"),
         "note": "コメントした人に自動DMを送り、LINEに来た割合"},
        {"key": "apply_rate", "label": "友だち追加 → 無料鑑定の申込", "unit": "%",
         "values": rate("free_applied", "friend_adds"),
         "note": "もとからいる友だちの申込も入るので100%を超えることがある"},
        {"key": "send_rate", "label": "申込 → 送付", "unit": "%", "values": rate("free_sent", "free_applied")},
        {"key": "cvr", "label": "無料鑑定の送付 → 本鑑定の購入", "unit": "%", "values": rate("paid_orders", "free_sent")},
        {"key": "upsell_rate", "label": "本鑑定の購入 → アップセル", "unit": "%",
         "values": rate("upsell_orders", "paid_orders"), "note": "目標8%"},
        {"key": "aov", "label": "客単価", "unit": "円", "values": {m: aov(m) for m in months}},
    ]
    return {"months": months, "stages": stages, "rates": rates,
            "now": {"ig_followers": ig.get("followers"), "th_followers": th.get("followers"),
                    "ig_reach": _recent_avg(ig.get("recent", []), "reach"),
                    "ig_likes": _recent_avg(ig.get("recent", []), "likes"),
                    "th_views": _recent_avg(th.get("recent", []), "views"),
                    "th_likes": _recent_avg(th.get("recent", []), "likes")}}


def diagnose(fn):
    months = fn["months"]
    now_m, base_ms = months[-2], months[-7:-2]
    out = []
    for row in fn["stages"] + fn["rates"]:
        vals = row["values"]
        cur = vals.get(now_m)
        base_vals = [vals.get(m) for m in base_ms if vals.get(m)]
        if cur is None or len(base_vals) < 3:
            continue
        base = statistics.median(base_vals)
        if not base:
            continue
        change = (cur - base) / base * 100
        if change > -10:
            continue
        out.append({"key": row["key"], "label": row["label"], "unit": row.get("unit", ""),
                    "now": cur, "base": round(base, 1), "change": round(change), "month": now_m,
                    "base_label": f"{int(base_ms[0][5:])}〜{int(base_ms[-1][5:])}月の真ん中の月",
                    "severity": "high" if change <= -50 else ("mid" if change <= -25 else "low"),
                    "hint": HINTS.get(row["key"], "")})
    out.sort(key=lambda x: x["change"])
    return {"month": now_m, "items": out}


# ── 全部まとめる ────────────────────────────────────────
def collect():
    _log_cache["rows"] = None
    _clients.clear()
    N = now()
    sales = sales_metrics(fetch_stores())
    order_dates = sales.pop("order_dates", {})
    free = [free_metrics("free_mira", "ミラ"), free_metrics("free_renju", "れんじゅ")]
    paid = paid_metrics(order_dates)
    tools = check_tools(sales)
    replies = comment_reply_stats()
    line = line_metrics()
    sns_data = sns_collect()
    fn = funnel(sales, free, line, sns_data)

    days = [(N.date() - datetime.timedelta(days=i)).isoformat() for i in range(29, -1, -1)]
    series = [{"date": d, "mira": free[0]["by_day"].get(d, 0), "renju": free[1]["by_day"].get(d, 0)}
              for d in days]
    for f in free:
        f.pop("by_day", None)
    return {"generated": N.strftime("%Y-%m-%d %H:%M"), "today": N.date().isoformat(),
            "sales": sales, "free": free, "free_series": series, "paid": paid, "tools": tools,
            "line": line, "sns": sns_data, "comment_reply": replies,
            "funnel": fn, "diagnosis": diagnose(fn), "rules": RULES}


# ── 画面に出す（キャッシュと更新ボタン） ─────────────────
_state = {"html": None, "data": None, "running": False, "started_at": None,
          "finished_at": None, "error": None}
_lock = threading.Lock()


def render(data):
    tpl = (HERE / "dashboard_template.html").read_text(encoding="utf-8")
    return tpl.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False, default=str))


def refresh():
    """数字を取り直してHTMLを作る（時間がかかるのでバックグラウンドで動かす）。"""
    with _lock:
        if _state["running"]:
            return False
        _state["running"] = True
        _state["started_at"] = now().strftime("%H:%M:%S")
        _state["error"] = None
    try:
        data = collect()
        _state["data"] = data
        _state["html"] = render(data)
        _state["finished_at"] = data["generated"]
    except Exception as e:
        _state["error"] = f"{type(e).__name__}: {e}"[:300]
        print(f"[dashboard] 更新に失敗: {_state['error']}")
    finally:
        _state["running"] = False
    return True


def refresh_async():
    if _state["running"]:
        return False
    threading.Thread(target=refresh, daemon=True).start()
    return True


def status():
    return {"running": _state["running"], "started_at": _state["started_at"],
            "finished_at": _state["finished_at"], "error": _state["error"],
            "has_html": bool(_state["html"])}


def html():
    return _state["html"]
