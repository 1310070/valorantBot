from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
import time, secrets, json, re
from pathlib import Path

# CORS（開発中は緩め。本番は絞る）
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],         # TODO: 本番で許可ドメインを限定
    allow_credentials=True,
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

    # --- .env の中身を組み立て ---
    lines = []
    cookies = data.get("cookies", {}) or {}
    a = cookies.get("auth", {}) or {}
    r = cookies.get("root", {}) or {}

    for k, envk in [
        (a.get("ssid"), "RIOT_SSID"),
        (a.get("clid"), "RIOT_CLID"),
        (a.get("sub"),  "RIOT_SUB"),
        (a.get("tdid"), "RIOT_TDID"),
        (a.get("csid"), "RIOT_CSID"),
        (r.get("_cf_bm"), "RIOT_CF_BM"),
        (r.get("__Secure-refresh_token_presence"), "RIOT_SEC_REFRESH_PRESENCE"),
        (r.get("__Secure-session_state"), "RIOT_SEC_SESSION_STATE"),
    ]:
        if k: lines.append(f"{envk}={k}")

    if (v := data.get("cookie_line")):
        # cookie_line はダブルクオートで囲む（セミコロン等を含むため）
        lines.append(f'RIOT_COOKIE_LINE="{v}"')

    # --- 保存先: env/.env<user_id> ---
    env_dir = Path("env")
    env_dir.mkdir(parents=True, exist_ok=True)
    target = env_dir / f".env{user_id}"
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return {"ok": True, "saved": [l.split("=")[0] for l in lines], "path": str(target)}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=5177)
