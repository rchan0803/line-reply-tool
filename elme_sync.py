"""エルメ（L Message）から会話履歴と友だち情報を取り込む。"""
import json
from datetime import datetime, timedelta, timezone

import elme_mcp
import database as db

JST = timezone(timedelta(hours=9))
SYNC_DAYS = 60  # 履歴の取り込み対象期間


def _jst_to_utc_iso(text: str) -> str:
    """エルメの "2026-07-07 17:33:49" (JST) をUTCのISO形式に変換。"""
    dt = datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
    return dt.astimezone(timezone.utc).isoformat()


def _resolve_friend(user_id: str, display_name: str, bot_id: str):
    """LINEのuserIdからエルメの友だちIDを特定する（表示名で検索して照合）。"""
    names = [n for n in [display_name, (display_name or "")[:3]] if n]
    for name in names:
        result = elme_mcp.call_tool_json("list_friends", {"bot_id": bot_id, "line_names": [name]})
        for f in result.get("friends", []):
            if f.get("line_friend_id") == user_id:
                return f.get("lmessage_friend_id")
    return None


def sync_user(user_id: str, bot_id: str) -> dict:
    """1人の顧客について、エルメの会話履歴とプロフィールを取り込む。"""
    if not elme_mcp.is_connected():
        return {"status": "not_connected"}
    if not bot_id:
        return {"status": "no_bot_id"}

    user = db.get_user(user_id)
    if not user:
        return {"status": "user_not_found"}

    elme_id = user.get("elme_friend_id")
    if not elme_id:
        elme_id = _resolve_friend(user_id, user.get("display_name") or "", bot_id)
        if not elme_id:
            return {"status": "friend_not_found"}
        db.set_elme_friend(user_id, elme_id)

    imported = 0
    # ── 会話履歴（ボット側の送信メッセージを取り込む）──
    from_date = (datetime.now(JST) - timedelta(days=SYNC_DAYS)).strftime("%Y-%m-%d")
    for page in (1, 2):
        convs = elme_mcp.call_tool_json("get_conversation_detail", {
            "bot_id": bot_id,
            "lmessage_friend_ids": str(elme_id),
            "from_date": from_date,
            "page": page,
        })
        conv = convs[0] if isinstance(convs, list) and convs else {}
        messages = conv.get("messages", [])
        for m in messages:
            if m.get("sender_type") != "bot":
                continue  # 顧客側はWebhookで受信済み（重複を避ける）
            if m.get("message_type") != "text" or not m.get("content"):
                continue
            if db.save_elme_message(
                user_id, "outbound", m["content"],
                _jst_to_utc_iso(m["sent_at"]), m["message_id"],
            ):
                imported += 1
        if len(messages) < 100:
            break

    # ── 友だち情報（フォーム回答・タグ・鑑定文など）──
    details = elme_mcp.call_tool_json("get_friend_detail", {
        "bot_id": bot_id,
        "lmessage_friend_ids": str(elme_id),
    })
    detail = details[0] if isinstance(details, list) and details else {}
    profile = {
        "tags": [t.get("tag_name") for t in detail.get("tags", [])],
        "friend_info": [
            {"title": f.get("title"), "value": f.get("value")}
            for f in detail.get("friend_info", []) if f.get("value")
        ],
    }
    db.set_profile(user_id, json.dumps(profile, ensure_ascii=False))

    # 呼び名が未設定なら、フォームの「システム表示名」を自動設定
    if not user.get("call_name"):
        for f in detail.get("friend_info", []):
            if f.get("field_id") == -1 and (f.get("value") or "").strip():
                db.set_call_name(user_id, f["value"].strip())
                break

    return {"status": "ok", "imported": imported, "elme_friend_id": elme_id}


def format_profile(profile_json: str, max_chars: int = 6000) -> str:
    """プロフィールJSONをAIに渡す文字列に整形する。"""
    if not profile_json:
        return ""
    try:
        profile = json.loads(profile_json)
    except Exception:
        return ""
    lines = []
    tags = profile.get("tags") or []
    if tags:
        lines.append("タグ: " + "、".join(tags))
    for f in profile.get("friend_info") or []:
        value = str(f.get("value") or "").strip()
        if not value:
            continue
        lines.append(f"{f.get('title')}: {value}")
    text = "\n".join(lines)
    return text[:max_chars]
