import os
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


SYSTEM_RULES = """あなたはLINE公式アカウントの占い師「ミラ」の返信案を作成するアシスタントです。

【出力形式（厳守）】
出力は必ず次のどちらか一方のみ。両方を混ぜて出力することは絶対に禁止です。
A) 「【返信不要】」+ 改行 + 理由1行（合計2行だけ。返信文は書かない）
B) 返信案の本文のみ（1文字目から顧客に送る文章で始めること。判定理由・運用ルールへの言及・「〜を送ります」などの説明文を本文の前後に付けてはいけない）

【手順1：返信要否の判定（返信文を書く前に必ず行う）】
判定の対象は「前回こちらから送信した後に届いた、顧客メッセージのまとまり全体」です。最後の1通だけで判断してはいけません。
例: 鑑定の感想と「特典希望」が届いた後に外出報告などの雑談が続いた場合、雑談だけを見て返信不要にせず、感想と特典希望に返信する形式Bを出力します。

以下の**すべて**に当てはまる場合のみ、形式Aで出力してください。
- まとまり全体が、お礼・相槌・雑談・独り言・テスト送信・意味のない文字列だけで構成されている
- 質問・依頼・関心の表明（「特典希望」「詳しく知りたい」等）・悩みの相談・購入報告が1つも含まれていない

補足: それより前の古いメッセージ（過去のテスト送信など）は判定に含めません。「（スタンプ）」「（画像）」「（友だち追加）」はテキスト以外の出来事を表す記号です。
最優先: 【このアカウントの運用ルール】に該当するケース（例: 新規入室への案内を作る等）は、この判定基準より運用ルールを優先し、形式Bで返信案を作ってください。

【手順2：返信案の作成】
手順1に当てはまらない場合のみ、以下のルールで返信案を作成してください。

- 最優先ルール: 顧客の最新メッセージの内容に直接答えること。マニュアルのどのテンプレにも当てはまらない場合や、顧客が何を求めているか会話から読み取れない場合は、営業案内（本鑑定の料金案内など）を送らず、用件を優しく確認する短い返信にすること
- 返信マニュアルは「雛形」です。丸写しは禁止。顧客の名前・相談内容・これまでのやり取りに合わせて、自然な文章に書き換えてください
- マニュアル内の「〇〇様」はこの顧客の呼び名に置き換えてください
- 料金・コースの内容・URL・決済方法・営業条件（割引率や期限など）は、マニュアルの記載を正確にそのまま使ってください（自分で創作・変更しない）
- 顧客が具体的な悩みを書いている場合は、必ずその内容に一言以上触れて、寄り添う一文を入れてください
- 必ず丁寧な敬語。占い師「ミラ」として温かく寄り添うトーン。絵文字はマニュアルと同程度に控えめに使う
- 返信案の本文のみを出力してください（「返信案：」などのラベル、説明文、前置きは一切付けない）"""


def generate_reply(messages: list[dict], manual: str, customer_name: str = "", customer_profile: str = "", account_rules: str = "") -> str:
    system = [
        {
            "type": "text",
            "text": SYSTEM_RULES + "\n\n【返信マニュアル】\n" + (manual if manual else "（マニュアル未設定）"),
            # マニュアルは全顧客共通なのでキャッシュして入力コストを削減する
            "cache_control": {"type": "ephemeral"},
        }
    ]
    if customer_name:
        system.append({
            "type": "text",
            "text": (
                f"この顧客の呼び名は「{customer_name}」です。"
                f"呼びかけには「{customer_name}様」のように使ってください。"
                "ただし、会話の中で顧客の本名や希望する呼び名が判明している場合は、そちらを優先してください。"
                "呼び名が記号やニックネームで呼びかけに不自然な場合は、名前を使わない自然な文面にしてください。"
            ),
        })
    if account_rules:
        system.append({"type": "text", "text": "【このアカウントの運用ルール】\n" + account_rules})
    if customer_profile:
        system.append({
            "type": "text",
            "text": (
                "この顧客について顧客管理システム（エルメ）に登録されている情報です。"
                "相談内容や過去の鑑定内容に触れる際の参考にしてください（そのまま転載はしない）:\n"
                + customer_profile
            ),
        })

    conversation = []
    for msg in messages:
        role = "user" if msg["direction"] == "inbound" else "assistant"
        conversation.append({"role": role, "content": msg["content"]})

    # Ensure the last message is from user (inbound)
    if not conversation or conversation[-1]["role"] != "user":
        return "（返信案を生成できませんでした）"

    # 判定対象（前回送信以降に届いた顧客メッセージ群）を明示する
    recent = []
    for m in reversed(conversation):
        if m["role"] != "user":
            break
        recent.append(m["content"])
    recent.reverse()
    joined = "\n---\n".join(recent)
    system.append({
        "type": "text",
        "text": f"返信要否の判定対象（前回こちらが送信した後に届いた顧客メッセージのまとまり）:\n{joined}",
    })

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=6000,
        thinking={"type": "adaptive"},
        system=system,
        messages=conversation,
    )
    return next(
        (block.text for block in response.content if block.type == "text"),
        "（返信案を生成できませんでした）",
    )


def refine_reply(
    messages: list[dict],
    manual: str,
    customer_name: str,
    current_draft: str,
    instruction: str,
    customer_profile: str = "",
) -> str:
    """オペレーターの指示に従って現在の返信案を修正する。"""
    system = [
        {
            "type": "text",
            "text": SYSTEM_RULES + "\n\n【返信マニュアル】\n" + (manual if manual else "（マニュアル未設定）"),
            "cache_control": {"type": "ephemeral"},
        },
        {
            "type": "text",
            "text": (
                "今回のタスクは「既存の返信案の修正」です。返信要否の判定（【返信不要】）は行わず、"
                "オペレーターの修正指示に従って返信案を書き直し、修正後の返信案本文のみを出力してください。"
                "指示された箇所以外は、できるだけ元の文章を保ってください。"
                + (f"\nこの顧客の呼び名は「{customer_name}」です。" if customer_name else "")
                + (f"\n\n【顧客の登録情報（参考）】\n{customer_profile}" if customer_profile else "")
            ),
        },
    ]

    conversation = []
    for msg in messages:
        role = "user" if msg["direction"] == "inbound" else "assistant"
        conversation.append({"role": role, "content": msg["content"]})

    conversation.append({
        "role": "user",
        "content": (
            "【オペレーターからの修正依頼（顧客のメッセージではありません）】\n"
            f"現在の返信案:\n---\n{current_draft}\n---\n"
            f"修正指示: {instruction}"
        ),
    })

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=6000,
        thinking={"type": "adaptive"},
        system=system,
        messages=conversation,
    )
    return next(
        (block.text for block in response.content if block.type == "text"),
        "（返信案を生成できませんでした）",
    )
