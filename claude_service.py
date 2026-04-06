import os
import anthropic

_client = None


def get_client():
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
    return _client


def generate_reply(messages: list[dict], manual: str) -> str:
    system_prompt = """あなたはLINE公式アカウントの返信案を作成するアシスタントです。

以下の返信マニュアルに従って、顧客への返信案を作成してください。
マニュアルに記載のないケースは、マニュアルのトーンや方針に合わせて対応してください。

【返信のルール】
- 必ず丁寧な敬語を使う
- 簡潔かつ明確に答える
- 返信案のみを出力する（説明文や前置きは不要）

【返信マニュアル】
{manual}
""".format(manual=manual if manual else "（マニュアル未設定）")

    conversation = []
    for msg in messages:
        role = "user" if msg["direction"] == "inbound" else "assistant"
        conversation.append({"role": role, "content": msg["content"]})

    # Ensure the last message is from user (inbound)
    if not conversation or conversation[-1]["role"] != "user":
        return "（返信案を生成できませんでした）"

    client = get_client()
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=system_prompt,
        messages=conversation,
    )
    return response.content[0].text
