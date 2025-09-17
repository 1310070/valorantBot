# valorantBot2/services/reauth_diag.py
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

# Riot endpoints
AUTH_URL_LEGACY = "https://auth.riotgames.com/api/v1/authorization"
AUTH_URL_V2     = "https://auth.riotgames.com/authorize"

# Try scopes in this order
AUTH_PARAMS_A = {
    "client_id":"play-valorant-web-prod","nonce":"1",
    "redirect_uri":"https://playvalorant.com/opt_in",
    "response_type":"token id_token","scope":"account openid","prompt":"none",
}
AUTH_PARAMS_B = {**AUTH_PARAMS_A, "scope":"openid link"}

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

TIMEOUT = 12

def _new_session(ua: Optional[str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua or DEFAULT_UA,
        "Accept": "application/json",
        "Origin": "https://playvalorant.com",
        "Referer": "https://playvalorant.com/opt_in",
    })
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1, backoff_factor=0.2, status_forcelist=(403,409,429,500,502,503,504)
    )))
    return s

def _extract(uri: str, key: str) -> Optional[str]:
    m = re.search(rf"{re.escape(key)}=([^&]+)", uri)
    return m.group(1) if m else None

def _mask(v: Optional[str]) -> str:
    if not v: return "<none>"
    return v[:4]+"…"+v[-4:] if len(v) > 8 else v

def _set_ssid_only(s: requests.Session, ssid: Optional[str]) -> None:
    s.cookies.clear()
    if not ssid: return
    for d in (".riotgames.com","auth.riotgames.com"):
        s.cookies.set("ssid", ssid, domain=d)

def _set_full(s: requests.Session, env: Dict[str,str]) -> None:
    s.cookies.clear()
    def _set(k):
        v = env.get(k)
        if not v: return
        for d in (".riotgames.com","auth.riotgames.com"):
            s.cookies.set(k, v, domain=d)
    for k in ("ssid","clid","sub","csid","tdid"):
        _set(k)

def _try_once(params: Dict[str,str], s: requests.Session) -> Tuple[int, int, bool]:
    # POST first
    r = s.post(AUTH_URL_LEGACY, json=params, timeout=TIMEOUT)
    ok = False
    if r.ok:
        try:
            uri = (r.json().get("response",{}).get("parameters",{}) or {}).get("uri")
            if uri and _extract(uri,"access_token") and _extract(uri,"id_token"):
                ok = True
        except Exception:
            pass
    if not ok:
        # GET fallback without redirects
        r2 = s.get(AUTH_URL_V2, params=params, allow_redirects=False, timeout=TIMEOUT)
        if r2.status_code in (301,302,303,307,308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc and _extract(loc,"access_token") and _extract(loc,"id_token"):
                ok = True
        return r.status_code, r2.status_code, ok
    else:
        return r.status_code, -1, ok

def _load_db(discord_user_id: str) -> Dict[str,str]:
    """
    DB から cookie と UA を取得する。get_cookies_and_meta があれば優先。
    """
    try:
        from .cookiesDB import get_cookies_and_meta as _get_meta  # type: ignore
    except Exception:
        _get_meta = None

    cookies, ua = None, None
    if _get_meta:
        meta = _get_meta(str(discord_user_id))
        if meta:
            cookies = meta.get("cookies") or {}
            ua = meta.get("user_agent")
    if cookies is None:
        from .cookiesDB import get_cookies as _get_cookies  # type: ignore
        cookies = _get_cookies(str(discord_user_id))

    # 正規化（lower/upper 両対応）
    out = {
        "ssid": cookies.get("ssid") or cookies.get("RIOT_SSID") or "",
        "clid": cookies.get("clid") or cookies.get("RIOT_CLID") or "",
        "sub":  cookies.get("sub")  or cookies.get("RIOT_SUB")  or "",
        "csid": cookies.get("csid") or cookies.get("RIOT_CSID") or "",
        "tdid": cookies.get("tdid") or cookies.get("RIOT_TDID") or "",
        "puuid": cookies.get("puuid") or cookies.get("RIOT_PUUID") or "",
        "user_agent": ua or cookies.get("user_agent") or cookies.get("ua") or "",
    }
    return out

def _candidate_paths(discord_user_id: str) -> List[Path]:
    here = Path(__file__).resolve()
    paths = [
        here.parent / "cookies" / f"{discord_user_id}.txt",             # services/cookies/
        here.parent.parent / "cookies" / f"{discord_user_id}.txt",      # repo_root/cookies/
        Path.cwd() / "cookies" / f"{discord_user_id}.txt",              # CWD/cookies/
    ]
    envdir = os.getenv("VALORANT_COOKIES_DIR")
    if envdir:
        paths.insert(0, Path(envdir) / f"{discord_user_id}.txt")
    # dedupe
    uniq, seen = [], set()
    for p in paths:
        if p not in seen:
            uniq.append(p); seen.add(p)
    return uniq

def _load_file(discord_user_id: str) -> Optional[Dict[str,str]]:
    for p in _candidate_paths(discord_user_id):
        try:
            if not p.exists(): continue
            raw: Dict[str,str] = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip()
            return {
                "ssid": raw.get("RIOT_SSID") or raw.get("SSID") or "",
                "clid": raw.get("RIOT_CLID") or raw.get("CLID") or "",
                "sub":  raw.get("RIOT_SUB")  or raw.get("SUB")  or "",
                "csid": raw.get("RIOT_CSID") or raw.get("CSID") or "",
                "tdid": raw.get("RIOT_TDID") or raw.get("TDID") or "",
                "puuid": raw.get("RIOT_PUUID") or raw.get("PUUID") or "",
            }
        except Exception:
            continue
    return None

def collect_reauth_diag(discord_user_id: str) -> str:
    """
    /store 失敗時に呼び出して、総当たりの reauth 結果をテキストで返す（マスク済み）。
    """
    uid = str(discord_user_id)
    db = _load_db(uid)
    file_env = _load_file(uid)

    attempts: List[Tuple[str, Dict[str,str], Optional[str], bool]] = [
        ("DB + defaultUA + SSID", db, None, True),
        ("DB + defaultUA + FULL", db, None, False),
        ("DB + DBUA     + SSID",  db, db.get("user_agent") or None, True),
        ("DB + DBUA     + FULL",  db, db.get("user_agent") or None, False),
    ]
    if file_env:
        attempts += [
            ("FILE + defaultUA + SSID", file_env, None, True),
            ("FILE + defaultUA + FULL", file_env, None, False),
            ("FILE + DBUA     + SSID",  file_env, db.get("user_agent") or None, True),
            ("FILE + DBUA     + FULL",  file_env, db.get("user_agent") or None, False),
        ]

    lines: List[str] = []
    lines.append(f"[diag] discord_user_id={uid}")
    enc_warn = os.getenv("COOKIE_ENC_KEY") is None
    if enc_warn:
        lines.append("WARN: COOKIE_ENC_KEY が未設定（再起動で以前のクッキーが復号できない可能性）")

    ssid_db_mask = _mask(db.get("ssid"))
    ua_db = db.get("user_agent") or ""
    lines.append(f"DB:  ssid={ssid_db_mask}  ua={_mask(ua_db)}")
    if file_env:
        lines.append(f"FILE: ssid={_mask(file_env.get('ssid'))}")

    for label, env, ua, ssid_only in attempts:
        s = _new_session(ua)
        if ssid_only: _set_ssid_only(s, env.get("ssid"))
        else:         _set_full(s, env)
        p1, p2, ok = _try_once(AUTH_PARAMS_A, s)
        if not ok:
            p1b, p2b, ok = _try_once(AUTH_PARAMS_B, s)
            post_str = f"{p1}/{p1b}"
            get_str  = f"{p2}/{p2b}"
        else:
            post_str = f"{p1}"
            get_str  = f"{p2}"
        lines.append(f"{label:24s} | UA={'DB' if ua else 'DEF'} | SSID={_mask(env.get('ssid'))} | POST={post_str} GET={get_str} | OK={ok}")

    lines.append("")
    lines.append("Hints:")
    lines.append(" - すべて OK=False → SSID 失効の可能性（ブラウザで再ログインして新しい ssid を保存）")
    lines.append(" - SSID-only は OK だが FULL で NG → clid/sub/csid/tdid が古い可能性。DB は ssid 中心で保存を推奨。")
    lines.append(" - DBUA だけ OK → 保存時の UA を常に使用する運用に。")
    if enc_warn:
        lines.append(" - COOKIE_ENC_KEY を固定設定してください（未設定だと再起動ごとに復号不可）。")

    return "\n".join(lines)


# セキュリティ: SSID 等は常にマスク表示。トークンは一切ログしないこと。
