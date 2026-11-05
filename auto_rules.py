"""自動返信の判定ルール。外部に触れない純粋な関数だけを置く（テストしやすくするため）。"""
import re
from dataclasses import dataclass, field

VIP_URL = "https://lin.ee/0yy2q5ed"
STANDARD_URL_KEY = "686bde43b3910e6864cb33ff"
FREE_MARK = "本鑑定で視えること"
HEARING_MARK = "【ご記入いただきたい内容】"
OPENING_ANCHOR = "本当に嬉しいです。\n\n"
RECOMMEND_ANCHOR = "\n\nお支払い後は"

STATUS_PROSPECT = 1718131  # 見込みあり
STATUS_HANDLING = 1718132  # 対応中

# 含まれていたら自動で送らない言葉
STOP_WORDS = (
    "返金", "返品", "キャンセル", "解約", "詐欺", "騙", "訴え", "消費者", "弁護士", "警察",
    "クレーム", "死に", "死にたい", "自殺", "消えたい", "未成年", "高校生", "中学生",
)
FLOW_TEXT = ("【受け取る】", "運命")
FLOW_PREFIX = ("【理想の私】",)
NAME_RE = re.compile(r"^[ぁ-んァ-ヶー一-龥々a-zA-Z0-9]{1,12}$")
ORDER_NO_RE = re.compile(r"\d{10}")
MAX_DECLINE_CHARS = 120
MAX_FIRST_VIP_CHARS = 60
MAX_OPENING_CHARS = 200
MAX_RECOMMEND_CHARS = 120


@dataclass
class Decision:
    action: str  # send / review（人に確認）/ ignore（何もしない）
    kind: str = ""
    reason: str = ""
    messages: list = field(default_factory=list)
    status_id: int = 0
    buyer: dict | None = None


def escalate(reason: str, kind: str = "") -> Decision:
    return Decision("review", kind, reason)


def ignore(reason: str) -> Decision:
    return Decision("ignore", "", reason)


def add_night_greeting(text: str) -> str:
    """夜のひと言を添える。宛名行がある文は宛名の次の行に入れる。"""
    first, _, rest = text.partition("\n")
    if first.endswith("様") and len(first) <= 15:
        return "\n".join([first, "夜遅くに失礼いたします。", rest])
    return "\n".join(["夜遅くに失礼いたします。", text])


def is_manual_reply(m: dict) -> bool:
    """こちらが手で送った文章か（シナリオ・自動応答は含めない）。"""
    return m.get("sender_type") == "bot" and m.get("message_type") in ("text", "pdf", "image") \
        and bool(m.get("content")) and not (m.get("source") or {}).get("type")


def last_id(messages: list[dict]):
    return messages[-1].get("message_id") if messages else None


def waiting_for_reply(messages: list[dict]) -> bool:
    """最後の手動の送信より後に、お客様のメッセージがあるか。"""
    for m in reversed(messages):
        if is_manual_reply(m):
            return False
        if m.get("sender_type") == "user":
            return True
    return False


def pending_block(messages: list[dict]) -> list[dict]:
    """前回こちらが送ったあとに届いた、お客様のメッセージのまとまり。"""
    block = []
    for m in reversed(messages):
        if is_manual_reply(m):
            break
        if m.get("sender_type") == "user":
            block.append(m)
    return list(reversed(block))


def block_text(block: list[dict]) -> str:
    return "\n".join(m["content"] for m in block if m.get("message_type") == "text" and m.get("content"))


def is_flow_only(block: list[dict]) -> bool:
    """無料鑑定の受付の流れで届くものだけか。"""
    if not block:
        return True
    for m in block:
        text = (m.get("content") or "").strip()
        if m.get("message_type") != "text":
            return False
        if text not in FLOW_TEXT and not text.startswith(FLOW_PREFIX):
            return False
    return True


def check_block(block: list[dict]) -> Decision | None:
    """内容を読む前に止める条件。画像や動画があるものは人が見る。"""
    if any(m.get("message_type") not in ("text", "sticker") for m in block):
        return escalate("画像などが届いている")
    text = block_text(block)
    for word in STOP_WORDS:
        if word in text:
            return escalate(f"注意する言葉が含まれている（{word}）")
    return None


def names_of(conv: dict) -> list[str]:
    """list_conversations の「システム名 (LINE名)」を分けて返す。"""
    raw = conv.get("line_name") or ""
    m = re.match(r"^(.*?) \((.*)\)$", raw)
    return [m.group(1).strip(), m.group(2).strip()] if m else [raw.strip()]


def free_appraisal(messages: list[dict]) -> str:
    for m in reversed(messages):
        if is_manual_reply(m) and FREE_MARK in str(m.get("content")):
            return m["content"]
    return ""


def call_name(conv: dict, messages: list[dict]) -> str:
    """呼び名。無料鑑定の宛名を優先する。使えない名前なら空を返す。"""
    appraisal = free_appraisal(messages)
    m = re.match(r"^(.+?)様\n", appraisal)
    name = m.group(1).strip() if m else names_of(conv)[0]
    return name if NAME_RE.match(name) else ""


def sent_before(messages: list[dict], mark: str) -> bool:
    return any(is_manual_reply(m) and mark in str(m.get("content")) for m in messages)


def active_orders(orders: list[dict]) -> list[dict]:
    return [o for o in orders if o.get("ステータス") != "キャンセル"]


def fill_honkantei(template: str, name: str, opening: str, recommend: str) -> str | None:
    """テンプレ原文に、冒頭の一言とおすすめ文だけを差し込む。形が違えば None。"""
    if template.count(OPENING_ANCHOR) != 1 or template.count(RECOMMEND_ANCHOR) != 1:
        return None
    text = template.replace("〇〇", name)
    text = text.replace(OPENING_ANCHOR, f"{OPENING_ANCHOR}{opening}\n\n")
    return text.replace(RECOMMEND_ANCHOR, f"\n\n{recommend}{RECOMMEND_ANCHOR}")


def added_text_problem(opening: str, recommend: str, name: str) -> str:
    """AIが書いた部分の点検。問題があれば理由を返す。"""
    both = opening + recommend
    if not opening or not recommend:
        return "文章が足りない"
    if len(opening) > MAX_OPENING_CHARS or len(recommend) > MAX_RECOMMEND_CHARS:
        return "文章が長い"
    if any(s in both for s in ("「", "」", "http", "円", "%", "％")):
        return "使わない表現が含まれている"
    if not recommend.startswith(f"{name}様の場合は") or not recommend.endswith("コースをおすすめいたします。"):
        return "おすすめ文の形が違う"
    if "スタンダード" not in recommend:
        return "おすすめのコースが違う"
    return ""


def decide_honkantei(name: str, result: dict, orders: list, messages: list, template: str) -> Decision:
    kind = "honkantei"
    if not name:
        return escalate("呼び名を確認できない", kind)
    if not free_appraisal(messages):
        return escalate("無料鑑定の送付を確認できない", kind)
    if sent_before(messages, STANDARD_URL_KEY):
        return escalate("本鑑定の案内を送ったことがある方", kind)
    if active_orders(orders):
        return escalate("同じ名前の注文がある", kind)
    if result.get("has_other_question") or result.get("needs_care"):
        return escalate(result.get("reason") or "質問や気がかりなことが含まれている", kind)
    opening = (result.get("opening") or "").strip()
    recommend = (result.get("recommendation") or "").strip()
    problem = added_text_problem(opening, recommend, name)
    if problem:
        return escalate(f"文面の確認が必要（{problem}）", kind)
    text = fill_honkantei(template, name, opening, recommend)
    if not text:
        return escalate("テンプレの形が変わっている", kind)
    return Decision("send", kind, "本鑑定希望", [text], STATUS_PROSPECT)


def decide_decline(result: dict, text: str, orders: list, messages: list, template: str) -> Decision:
    kind = "decline"
    if not free_appraisal(messages):
        return escalate("無料鑑定の送付を確認できない", kind)
    if result.get("money_reason"):
        return escalate("お金の理由での辞退", kind)
    if result.get("has_other_question") or result.get("needs_care"):
        return escalate(result.get("reason") or "質問や気がかりなことが含まれている", kind)
    if len(text) > MAX_DECLINE_CHARS:
        return escalate("長文の辞退", kind)
    if active_orders(orders):
        return escalate("同じ名前の注文がある", kind)
    return Decision("send", kind, "辞退", [f"ご連絡ありがとうございます。\n{template}"])


def match_order(conv: dict, text: str, orders: list) -> list:
    """注文番号か、フルネームの完全一致で結びつく注文だけを残す。"""
    numbers = set(ORDER_NO_RE.findall(text))
    plain = re.sub(r"\s", "", text)
    names = [re.sub(r"\s", "", n) for n in names_of(conv)]
    out = []
    for o in orders:
        full = re.sub(r"\s", "", o.get("氏名") or "")
        if str(o.get("注文番号")) in numbers or (len(full) >= 3 and (full in plain or full in names)):
            out.append(o)
    return out


def given_name(order: dict) -> str:
    parts = (order.get("氏名") or "").split()
    return parts[-1] if len(parts) >= 2 and NAME_RE.match(parts[-1]) else ""


def decide_purchase(name: str, conv: dict, text: str, orders: list, messages: list, has_other_order,
                    template: str) -> Decision:
    kind = "purchase"
    if sent_before(messages, VIP_URL):
        return escalate("専用LINEを案内したことがある方", kind)
    matched = [o for o in match_order(conv, text, active_orders(orders)) if not o.get("アップセル")]
    if len(matched) != 1:
        return escalate("注文と結びつかない" if not matched else "注文が複数ある", kind)
    order = matched[0]
    if not order.get("有効"):
        return escalate(f"入金を確認できない（{order.get('ステータス')}）", kind)
    if order.get("コース") not in ("ライト", "スタンダード"):
        return escalate("鑑定コースを確認できない", kind)
    name = name or given_name(order)
    if not name:
        return escalate("呼び名を確認できない", kind)
    if has_other_order(order):
        return escalate("過去の購入がある方（リピーター）", kind)
    if VIP_URL not in template or "〇〇" not in template:
        return escalate("テンプレの形が変わっている", kind)
    body = template.replace("〇〇", name)
    buyer = {"line_name": names_of(conv)[0], "customer_name": order["氏名"], "order": order}
    return Decision("send", kind, f"購入確認 {order['注文番号']}", [body], STATUS_HANDLING, buyer)


def vip_has_history(messages: list[dict]) -> bool:
    return any(is_manual_reply(m) and HEARING_MARK in str(m.get("content")) for m in messages) \
        or sum(1 for m in messages if is_manual_reply(m)) > 0


def decide_hearing(rows: list, block: list, templates: dict) -> Decision:
    kind = "hearing"
    if len(block_text(block)) > MAX_FIRST_VIP_CHARS:
        return escalate("入室時に長文が届いている", kind)
    if len(rows) != 1:
        return escalate("購入者リストと結びつかない" if not rows else "購入者リストの候補が複数ある", kind)
    row = rows[0]
    if not row.get("有効"):
        return escalate("入金を確認できない", kind)
    if row.get("鑑定書送付済み") or row.get("リピーター"):
        return escalate("過去の購入がある方（リピーター）", kind)
    course = "ライト" if "ライト" in row.get("コース", "") else "スタンダード" if "スタンダード" in row.get("コース", "") else ""
    name = row.get("名") or ""
    if not course or not name or not NAME_RE.match(name):
        return escalate("コースか呼び名を確認できない", kind)
    pair = templates.get(course)
    if not pair or not all(pair):
        return escalate("ヒアリングのテンプレが見つからない", kind)
    return Decision("send", kind, f"ヒアリング {course}", [t.replace("〇〇", name) for t in pair])
