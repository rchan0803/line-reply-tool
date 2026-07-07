import os
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


SYSTEM_RULES = """あなたはLINE公式アカウントの占い師「ミラ」の返信案を作成するアシスタントです。

【手順1：返信要否の判定（返信文を書く前に必ず行う）】
顧客の最新メッセージが以下のいずれかに当てはまる場合は、返信文を一切書かず、次の2行だけを出力して終えてください。
1行目: 【返信不要】
2行目: 理由（1行）

- お礼・相槌だけで会話が自然に完結している（例:「ありがとうございました」「わかりました」「了解です」）
- テスト送信と思われるもの（例:「テスト」「テスト用送信」「test」）
- 意味を持たない文字列・誤送信と思われるもの
- 返答を求めていない一方的な報告

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
                f"この顧客のLINE表示名は「{customer_name}」です。"
                f"呼びかけには「{customer_name}様」のように使ってください。"
                "表示名が記号やニックネームで呼びかけに不自然な場合は、名前を使わない自然な文面にしてください。"
            ),
        })

    conversation = []
    for msg in messages:
        role = "user" if msg["direction"] == "inbound" else "assistant"
        conversation.append({"role": role, "content": msg["content"]})

    # Ensure the last message is from user (inbound)
    if not conversation or conversation[-1]["role"] != "user":
        return "（返信案を生成できませんでした）"

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=2048,
        thinking={"type": "disabled"},
        system=system,
        messages=conversation,
    )
    return response.content[0].text
