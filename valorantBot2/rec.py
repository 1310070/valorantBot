from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import time, secrets, re, os, logging
from pathlib import Path

# ---- ログ設定（INFO以上を出力）----
logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

app = FastAPI()

# CORS（拡張からのfetch想定。allow_originsは本番で絞るのが安全）
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],        # 例: ["https://pure-cherrita-inosuke-6597cf0f.koyeb.app"]
    allow_credentials=False,    # "*" と True は両立しないため False 推奨
    allow_methods=["*"],
    allow_headers=["*"],
)

_nonces = {}

# ---- 保存先ディレクトリ: /app/mnt/cookies ----
COOKIE_DIR = Path("/app/mnt/cookies")
COOKIE_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/nonce")
def nonce():
    n = secrets.token_urlsafe(24)
    _nonces[n] = time.time() + 180
    return {"nonce": n, "expiry": 180}

@app.post("/riot-cookies")
async def receive(req: Request):
    data = await req.json()

    # --- Nonce 検証 ---
    n = data.get("nonce")
    if not n or n not in _nonces or _nonces[n] < time.time():
        return JSONResponse({"ok": False, "error": "invalid_or_expired_nonce"}, status_code=400)
    del _nonces[n]

    # --- Discord ユーザーID検証（数字のみ 5〜25桁を許容）---
    user_id = str(data.get("user_id", "")).strip()
    if not re.fullmatch(r"\d{5,25}", user_id):
        return JSONResponse({"ok": False, "error": "invalid_user_id"}, status_code=400)

    # --- テキストファイルの中身を組み立て ---
    cookies = data.get("cookies", {}) or {}
    a = cookies.get("auth", {}) or {}
    r = cookies.get("root", {}) or {}

    lines = []
    for k, envk in [
        (a.get("ssid"), "RIOT_SSID"),
        (a.get("clid"), "RIOT_CLID"),
        (a.get("sub"),  "RIOT_SUB"),
        (a.get("tdid"), "RIOT_TDID"),
        (a.get("csid"), "RIOT_CSID"),
        (r.get("_cf_bm"), "_RIOT_CF_BM"),  # 先頭_は環境変数として不要なら RIOT_CF_BM に戻す
        (r.get("__Secure-refresh_token_presence"), "RIOT_SEC_REFRESH_PRESENCE"),
        (r.get("__Secure-session_state"), "RIOT_SEC_SESSION_STATE"),
    ]:
        if k:
            lines.append(f"{envk}={str(k).strip()}")

    if (v := data.get("cookie_line")):
        # cookie_line はダブルクオートで囲む（セミコロン等を含むため）
        lines.append(f'RIOT_COOKIE_LINE="{v}"')

    # --- 保存先: /app/mnt/cookies/<user_id>.txt ---
    target = COOKIE_DIR / f"{user_id}.txt"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    # ---- 保存先をログに出力 ----
    log.info("Saved cookies for user_id=%s -> %s", user_id, target)

    return {"ok": True, "saved": [l.split("=")[0] for l in lines], "path": str(target)}

# 任意：ヘルスチェック用
@app.get("/")
def root():
    return {"ok": True, "hint": "use /nonce or /riot-cookies"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8190"))  # Koyebなら PORT を使う
    uvicorn.run(app, host="0.0.0.0", port=port)
