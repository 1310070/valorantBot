# valorantBot2/services/reauth_diag.py
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional, Dict, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry
from .net_diag import get_public_ip, mask_ip

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
        "Accept": "application/json,text/html;q=0.9",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
        "Origin": "https://playvalorant.com",
        "Referer": "https://playvalorant.com/opt_in",
    })
    # Do not retry on 403 (Cloudflare challenge) to avoid RetryError
    s.mount("https://", HTTPAdapter(max_retries=Retry(
        total=1,
        backoff_factor=0.2,
        status_forcelist=(409,429,500,502,503,504),
        raise_on_status=False
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
    cf_post = False
    if r.ok:
        try:
            uri = (r.json().get("response",{}).get("parameters",{}) or {}).get("uri")
            if uri and _extract(uri,"access_token") and _extract(uri,"id_token"):
                ok = True
        except Exception:
            pass
    else:
        # 403 などで HTML が来ている場合に Cloudflare 文言を拾う
        try:
            txt = (r.text or "")
            if "Attention Required! | Cloudflare" in txt or "cf-browser-verification" in txt:
                cf_post = True
        except Exception:
            pass
    if not ok:
        # GET fallback without redirects. Do not raise on 403 bursts.
        try:
            r2 = s.get(AUTH_URL_V2, params=params, allow_redirects=False, timeout=TIMEOUT)
        except requests.exceptions.RetryError:
            # In case adapter still escalates, treat as 403 to keep diagnostics flowing
            class _Dummy:
                status_code = 403
                headers = {}
                text = ""

            r2 = _Dummy()  # type: ignore
        cf_get = False
        if getattr(r2, "status_code", 0) and getattr(r2, "status_code") != -1:
            try:
                # Some 403 responses carry HTML; read a short snippet only
                txt2 = getattr(r2, "text", "") or ""
                if "Attention Required! | Cloudflare" in txt2 or "cf-browser-verification" in txt2:
                    cf_get = True
            except Exception:
                pass
        if r2.status_code in (301,302,303,307,308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc and _extract(loc,"access_token") and _extract(loc,"id_token"):
                ok = True
        # encode Cloudflare flag into negative thousand offsets (-1000/-2000)
        post_code = r.status_code - (1000 if cf_post else 0)
        get_code  = r2.status_code - (1000 if cf_get else 0)
        return post_code, get_code, ok
    else:
        post_code = r.status_code - (1000 if cf_post else 0)
        return post_code, -1, ok

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

    db_user_agent = db.get("user_agent") or None
    attempts: List[Tuple[str, Dict[str,str], Optional[str], bool]] = [
        ("DB + DBUA     + FULL",  db, db_user_agent, False),
        ("DB + DBUA     + SSID",  db, db_user_agent, True),
        ("DB + defaultUA + FULL", db, None, False),
        ("DB + defaultUA + SSID", db, None, True),
    ]
    if file_env:
        attempts += [
            ("FILE + DBUA     + FULL",  file_env, db_user_agent, False),
            ("FILE + DBUA     + SSID",  file_env, db_user_agent, True),
            ("FILE + defaultUA + FULL", file_env, None, False),
            ("FILE + defaultUA + SSID", file_env, None, True),
        ]

    lines: List[str] = []
    egress = mask_ip(get_public_ip())
    lines.append(f"[diag] discord_user_id={uid}  egress_ip={egress}")
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
            p1 = f"{p1}/{p1b}"
            p2 = f"{p2}/{p2b}"
        else:
            p1 = f"{p1}"
            p2 = f"{p2}"

        # Cloudflare の簡易表記: -1000 が付いていたら “CF”
        def _fmt(code: str | int) -> str:
            s = str(code)

            def mark(x: str) -> str:
                try:
                    v = int(x)
                except Exception:
                    return x
                if v == -1:
                    return "-1"
                cf = False
                orig = v
                if v <= -100:
                    for off in (1000, 2000):
                        cand = v + off
                        if 0 <= cand < 1000:
                            orig = cand
                            cf = True
                            break
                if orig < 0:
                    return str(orig)
                res = str(orig)
                return f"{res} CF" if cf else res

            if "/" in s:
                return "/".join(mark(part) for part in s.split("/"))
            return mark(s)

        post_str = _fmt(p1)
        get_str  = _fmt(p2)
        lines.append(f"{label:24s} | UA={'DB' if ua else 'DEF'} | SSID={_mask(env.get('ssid'))} | POST={post_str} GET={get_str} | OK={ok}")

    lines.append("")
    lines.append("Hints:")
    lines.append(" - すべて OK=False かつ POST/GET に 'CF' 表示 → Cloudflare によるブロック（出口IPの変更や専用プロキシの利用を検討）")
    lines.append(" - すべて OK=False → SSID 失効の可能性（ブラウザで再ログインして新しい ssid を保存）")
    lines.append(" - SSID-only は OK だが FULL で NG → clid/sub/csid/tdid が古い可能性。DB は ssid 中心で保存を推奨。")
    lines.append(" - DBUA だけ OK → 保存時の UA を常に使用する運用に。")
    if enc_warn:
        lines.append(" - COOKIE_ENC_KEY を固定設定してください（未設定だと再起動ごとに復号不可）。")

    return "\n".join(lines)


# セキュリティ: SSID 等は常にマスク表示。トークンは一切ログしないこと。
