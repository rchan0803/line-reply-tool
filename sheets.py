import os
import gspread
from google.oauth2.service_account import Credentials

# アカウントごとのマニュアル本文キャッシュ { account_id: text }
_manual_cache: dict = {}


def _get_client():
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        import json
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
        creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    return gspread.authorize(creds)


def load_manuals(account_sheets: dict) -> dict:
    """スプレッドシートを読み込み、アカウントごとに参照シートを振り分ける。

    account_sheets: { account_id: [シート名, ...] }
    戻り値: { account_id: 文字数 }
    """
    global _manual_cache
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return {}
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(sheet_id)
        contents = {}
        for sheet in spreadsheet.worksheets():
            if sheet.title.endswith("_bk"):  # バックアップ用シートは読み込まない
                continue
            rows = sheet.get_all_values()
            lines = [f"=== {sheet.title} ==="]
            for row in rows:
                row_text = " | ".join(cell.strip() for cell in row if cell.strip())
                if row_text:
                    lines.append(row_text)
            contents[sheet.title] = "\n".join(lines)

        new_cache = {}
        for account, sheet_names in account_sheets.items():
            parts = [contents[name] for name in sheet_names if name in contents]
            missing = [name for name in sheet_names if name not in contents]
            if missing:
                print(f"[sheets] {account}: シートが見つかりません: {missing}")
            new_cache[account] = "\n\n".join(parts)
        _manual_cache = new_cache
    except Exception as e:
        print(f"[sheets] マニュアル読み込みエラー: {e}")
    return {account: len(text) for account, text in _manual_cache.items()}


def get_manual_content(account: str = "main") -> str:
    return _manual_cache.get(account, "")
