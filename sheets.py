import os
import gspread
from google.oauth2.service_account import Credentials

_manual_cache: str = ""
_cache_loaded: bool = False


def _get_client():
    creds_file = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
    scopes = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    creds = Credentials.from_service_account_file(creds_file, scopes=scopes)
    return gspread.authorize(creds)


def load_manual() -> str:
    global _manual_cache, _cache_loaded
    sheet_id = os.getenv("GOOGLE_SHEET_ID", "")
    if not sheet_id:
        return ""
    try:
        client = _get_client()
        spreadsheet = client.open_by_key(sheet_id)
        lines = []
        for sheet in spreadsheet.worksheets():
            rows = sheet.get_all_values()
            if not rows:
                continue
            lines.append(f"=== {sheet.title} ===")
            for row in rows:
                row_text = " | ".join(cell.strip() for cell in row if cell.strip())
                if row_text:
                    lines.append(row_text)
        _manual_cache = "\n".join(lines)
        _cache_loaded = True
    except Exception as e:
        print(f"[sheets] マニュアル読み込みエラー: {e}")
        if not _cache_loaded:
            _manual_cache = ""
    return _manual_cache


def get_manual_content() -> str:
    return _manual_cache
