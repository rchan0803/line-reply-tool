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
B) 返信案の本文のみ

【手順1：返信要否の判定（返信文を書く前に必ず行う）】
判定の対象は「最後にまとまって届いている顧客メッセージ」です。それより前のメッセージ（過去のテスト送信など）は判定に含めません。
最新のメッセージが以下のいずれかに当てはまる場合のみ、形式Aで出力してください。

- お礼・相槌だけで会話が自然に完結している（例:「ありがとうございました」「わかりました」「了解です」）
- テスト送信と思われるもの（例:「テスト」「テスト用送信」「test」）
- 意味を持たない文字列・誤送信と思われるもの
- 返答を求めていない一方的な報告

注意: 判定するのは最新のメッセージ1件だけです。過去にテスト送信があっても、最新のメッセージがマニュアルの「相談者様からの返信例」に該当するキーワード（例:「本鑑定希望」）や実質的な内容であれば、必ず形式Bで返信案を作ってください。

【手順2：返信案の作成】
手順1に当てはまらない場合のみ、以下のルールで返信案を作成してください。

- 最優先ルール: 顧客の最新メッセージの内容に直接答えること。マニュアルのどのテンプレにも当てはまらない場合や、顧客が何を求めているか会話から読み取れない場合は、営業案内（本鑑定の料金案内など）を送らず、用件を優しく確認する短い返信にすること
- 返信マニュアルは「雛形」です。丸写しは禁止。顧客の名前・相談内容・これまでのやり取りに合わせて、自然な文章に書き換えてください
- マニュアル内の「〇〇様」はこの顧客の呼び名に置き換えてください
- 料金・コースの内容・URL・決済方法・営業条件（割引率や期限など）は、マニュアルの記載を正確にそのまま使ってください（自分で創作・変更しない）
- 顧客が具体的な悩みを書いている場合は、必ずその内容に一言以上触れて、寄り添う一文を入れてください
- 必ず丁寧な敬語。占い師「ミラ」として温かく寄り添うトーン。絵文字はマニュアルと同程度に控えめに使う
- 返信案の本文のみを出力してください（「返信案：」などのラベル、説明文、前置きは一切付けない）"""


def generate_reply(messages: list[dict], manual: str, customer_name: str = "") -> str:
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

    conversation = []
    for msg in messages:
        role = "user" if msg["direction"] == "inbound" else "assistant"
        conversation.append({"role": role, "content": msg["content"]})

    # Ensure the last message is from user (inbound)
    if not conversation or conversation[-1]["role"] != "user":
        return "（返信案を生成できませんでした）"

    # 判定対象を明示して、過去のメッセージに引っ張られないようにする
    system.append({
        "type": "text",
        "text": f"返信要否の判定対象となる最新の顧客メッセージは次の1件です:\n「{conversation[-1]['content']}」",
    })

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        thinking={"type": "disabled"},
        system=system,
        messages=conversation,
    )
    return response.content[0].text


def refine_reply(
    messages: list[dict],
    manual: str,
    customer_name: str,
    current_draft: str,
    instruction: str,
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
                + (f"\nこの顧客のLINE表示名は「{customer_name}」です。" if customer_name else "")
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
        max_tokens=2048,
        thinking={"type": "disabled"},
        system=system,
        messages=conversation,
    )
    return response.content[0].text
