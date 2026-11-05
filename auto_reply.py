"""定型の返信を、受信から約45分待ってエルメから自動送信する。

対象は4種類だけ（購入確認→専用LINE案内／専用LINE登録後のヒアリング2通／本鑑定希望への案内／辞退へのお礼）。
少しでも条件から外れたら送らず、スマホに通知して人の確認に回す。

AUTO_REPLY_MODE: off=動かさない / dry=判定だけ記録して送らない（既定） / on=送信する
"""
import hashlib
import os
import time
from datetime import datetime, timedelta, timezone

import httpx

import auto_rules
import database as db
import elme_mcp
import sheets_sync
from claude_service import classify_customer_message

JST = timezone(timedelta(hours=9))

MODE = os.getenv("AUTO_REPLY_MODE", "dry")
DELAY_MIN = int(os.getenv("AUTO_REPLY_DELAY_MIN", "45"))
JITTER_MIN = 5  # 毎回ぴったり45分にならないよう前後にずらす
MAX_AGE_HOURS = 18  # 夜間に届いた分を朝に処理できる長さ
SEND_START_HOUR = 7  # 0時〜7時は送らず朝に回す
NIGHT_HOUR = 22  # 22時以降は夜のひと言を添える
MAX_SENDS_PER_TICK = 10  # 1時間ごとの処理で送る上限
HEARING_GAP_SEC = 30  # ヒアリング2通の間隔


def parse_jst(text: str) -> datetime:
    return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)


def wait_minutes(key: str) -> int:
    offset = int(hashlib.sha256(key.encode()).hexdigest(), 16) % (JITTER_MIN * 2 + 1)
    return DELAY_MIN - JITTER_MIN + offset


def fetch_messages(bot_id: str, friend_id: int) -> list[dict]:
    """古い順の会話履歴。"""
    from_date = (datetime.now(JST) - timedelta(days=120)).strftime("%Y-%m-%d")
    convs = elme_mcp.call_tool_json("get_conversation_detail", {
        "bot_id": bot_id, "lmessage_friend_ids": str(friend_id),
        "from_date": from_date, "limit": 100,
    })
    conv = convs[0] if isinstance(convs, list) and convs else {}
    return sorted(conv.get("messages", []), key=lambda m: (m.get("sent_at") or "", m.get("message_id") or 0))


def send(bot_id: str, friend_id: int, text: str):
    # エルメ側の引数名が content から text に変わったため両方渡す（有効なのは片方だけ）
    result = elme_mcp.call_tool_json("send_message", {
        "bot_id": bot_id, "lmessage_friend_id": int(friend_id),
        "content": text, "text": text, "shorten_url": 0,
    })
    if "result=ok" not in str(result) and not (isinstance(result, dict) and result.get("result") == "ok"):
        raise RuntimeError(f"送信結果を確認できません: {str(result)[:200]}")


def set_status(bot_id: str, friend_id: int, status_id: int):
    elme_mcp.call_tool_json("update_conversation_handling_status", {
        "bot_id": bot_id, "lmessage_friend_id": int(friend_id), "handling_status_id": status_id,
    })


def decide_main(conv: dict, messages: list[dict]) -> auto_rules.Decision:
    block = auto_rules.pending_block(messages)
    if auto_rules.is_flow_only(block):
        return auto_rules.ignore("無料鑑定の受付の流れ")
    guard = auto_rules.check_block(block)
    if guard:
        return guard
    name = auto_rules.call_name(conv, messages)
    text = auto_rules.block_text(block)
    orders = sheets_sync.find_orders(auto_rules.names_of(conv), refresh=True, extra_text=text)
    result = classify_customer_message(
        text, name or "お客様", auto_rules.free_appraisal(messages),
    )
    if result is None:
        return auto_rules.escalate("判定できなかった")
    category = result["category"]
    if category == "purchase_report":
        return auto_rules.decide_purchase(
            name, conv, text, orders, messages, sheets_sync.buyer_has_other_order, sheets_sync.purchase_template(),
        )
    if category == "honkantei_request":
        return auto_rules.decide_honkantei(name, result, orders, messages, sheets_sync.honkantei_template())
    if category == "decline":
        return auto_rules.decide_decline(result, text, orders, messages, sheets_sync.decline_template())
    if category == "unsure":
        return auto_rules.escalate(result.get("reason") or "判定に迷った")
    return auto_rules.ignore(f"対象外（{category}）")


def decide_paid(conv: dict, messages: list[dict]) -> auto_rules.Decision:
    block = auto_rules.pending_block(messages)
    guard = auto_rules.check_block(block)
    if guard:
        return guard
    if auto_rules.vip_has_history(messages):
        return auto_rules.ignore("専用LINEでやりとり済み")
    names = auto_rules.names_of(conv)
    rows = sheets_sync.recent_buyers(names, auto_rules.block_text(block))
    return auto_rules.decide_hearing(rows, block, sheets_sync.hearing_templates())


def notify_review(name: str, reason: str):
    """スマホに通知（メッセージの中身は書かない）。"""
    topic = os.getenv("NTFY_TOPIC", "")
    if not topic:
        return
    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    base = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
    headers = {"Title": "ミラ 自動返信の確認".encode("utf-8"), "Tags": "warning"}
    if base:
        headers["Click"] = f"{base}/auto"
    try:
        httpx.post(f"{server}/{topic}", content=f"{name}様：{reason}".encode("utf-8"), headers=headers, timeout=10)
    except Exception as e:
        print(f"[auto] 通知に失敗: {e}")


def carry_out(account: str, bot_id: str, friend_id: int, decision: auto_rules.Decision):
    for i, text in enumerate(decision.messages):
        if i:
            time.sleep(HEARING_GAP_SEC)
        send(bot_id, friend_id, text)
    if decision.status_id:
        set_status(bot_id, friend_id, decision.status_id)
    if decision.buyer:
        sheets_sync.add_buyer_row(**decision.buyer)


def process(account: str, bot_id: str, conv: dict, key: str, decide) -> bool:
    """1件を判定して実行する。送信（またはdryの記録）をしたら True。"""
    friend_id = conv["lmessage_friend_id"]
    name = conv.get("line_name") or ""
    messages = fetch_messages(bot_id, friend_id)
    if not auto_rules.waiting_for_reply(messages):
        db.save_auto_action(key, account, friend_id, name, "skip", "skipped", "すでに返信済み")
        return False
    try:
        decision = decide(conv, messages)
    except Exception as e:
        decision = auto_rules.escalate(f"判定中のエラー: {e}")

    if decision.action != "send":
        db.save_auto_action(key, account, friend_id, name, decision.kind, decision.action, decision.reason)
        if decision.action == "review":
            notify_review(name, decision.reason)
        return False

    if datetime.now(JST).hour >= NIGHT_HOUR:
        decision.messages[0] = auto_rules.add_night_greeting(decision.messages[0])
    body = "\n\n----\n\n".join(decision.messages)
    if MODE != "on":
        db.save_auto_action(key, account, friend_id, name, decision.kind, "dry", decision.reason, body)
        return True

    # 同じ人に2回送らないよう、先に記録してから送る。失敗しても自動で送り直さない
    if not db.save_auto_action(key, account, friend_id, name, decision.kind, "sending", decision.reason, body):
        return False
    if auto_rules.last_id(fetch_messages(bot_id, friend_id)) != auto_rules.last_id(messages):
        db.update_auto_action(key, "skipped", "判定中に新しいやりとりがあった")
        return False
    try:
        carry_out(account, bot_id, friend_id, decision)
    except Exception as e:
        db.update_auto_action(key, "error", f"送信エラー: {e}")
        notify_review(name, "自動送信に失敗。トークを確認してください")
        return False
    db.update_auto_action(key, "sent", decision.reason)
    return True


def run_once(accounts: dict) -> dict:
    now = datetime.now(JST)
    if MODE == "off":
        return {"status": "off"}
    if not elme_mcp.is_connected():
        return {"status": "not_connected"}
    start_at = parse_jst(db.auto_start_at(now.strftime("%Y-%m-%d %H:%M:%S")))
    if now.hour < SEND_START_HOUR:
        return {"status": "night"}

    done = 0
    for account, decide in (("main", decide_main), ("paid", decide_paid)):
        bot_id = accounts[account]["elme_bot_id"]
        listing = elme_mcp.call_tool_json("list_conversations", {
            "bot_id": bot_id, "confirm_status": 1, "limit": 60,
        })
        for conv in listing.get("conversations", []):
            if done >= MAX_SENDS_PER_TICK:
                return {"status": "ok", "done": done}
            received = conv.get("last_time_message")
            if not received:
                continue
            at = parse_jst(received)
            key = f"{bot_id}:{conv['lmessage_friend_id']}:{received}"
            if at < start_at or now - at > timedelta(hours=MAX_AGE_HOURS):
                continue
            if now - at < timedelta(minutes=wait_minutes(key)) or db.auto_action_exists(key):
                continue
            if process(account, bot_id, conv, key, decide):
                done += 1
    return {"status": "ok", "done": done}
