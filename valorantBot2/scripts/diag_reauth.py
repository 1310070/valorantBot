from __future__ import annotations
import os, re, json, logging
from typing import Optional, Dict, Tuple, List
from pathlib import Path

import requests
from requests.adapters import HTTPAdapter, Retry

logging.basicConfig(level=os.getenv("LOGLEVEL","INFO"))
log = logging.getLogger("diag_reauth")

try:
    from valorantBot2.services.cookiesDB import get_cookies_and_meta as _get_meta
except Exception:
    _get_meta = None

AUTH_URL_LEGACY = "https://auth.riotgames.com/api/v1/authorization"
AUTH_URL_V2     = "https://auth.riotgames.com/authorize"

AUTH_PARAMS_A = {
    "client_id":"play-valorant-web-prod","nonce":"1",
    "redirect_uri":"https://playvalorant.com/opt_in",
    "response_type":"token id_token","scope":"account openid","prompt":"none",
}
AUTH_PARAMS_B = {**AUTH_PARAMS_A, "scope": "openid link"}

DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/120.0.0.0 Safari/537.36")

def _new_session(ua: Optional[str]) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua or DEFAULT_UA,
        "Accept":"application/json",
        "Origin":"https://playvalorant.com",
        "Referer":"https://playvalorant.com/opt_in",
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
    return v[:4]+"…"+v[-4:] if len(v)>8 else v

def _set_ssid_only(s: requests.Session, ssid: Optional[str]):
    s.cookies.clear()
    if not ssid: return
    for d in (".riotgames.com","auth.riotgames.com"):
        s.cookies.set("ssid", ssid, domain=d)

def _set_full(s: requests.Session, env: Dict[str,str]):
    s.cookies.clear()
    def _set(k):
        v = env.get(k)
        if not v: return
        for d in (".riotgames.com","auth.riotgames.com"):
            s.cookies.set(k, v, domain=d)
    for k in ("ssid","clid","sub","csid","tdid"): _set(k)

def _try(params, s) -> Tuple[int,int,bool]:
    r = s.post(AUTH_URL_LEGACY, json=params, timeout=10)
    ok = False
    if r.ok:
        try:
            uri = (r.json().get("response",{}).get("parameters",{}) or {}).get("uri")
            if uri and _extract(uri,"access_token") and _extract(uri,"id_token"):
                ok = True
        except Exception:
            pass
    if not ok:
        r2 = s.get(AUTH_URL_V2, params=params, allow_redirects=False, timeout=10)
        if r2.status_code in (301,302,303,307,308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc and _extract(loc,"access_token") and _extract(loc,"id_token"):
                ok = True
        return r.status_code, r2.status_code, ok
    else:
        return r.status_code, -1, ok

def _load_db(uid: str) -> Dict[str,str]:
    cookies, ua = None, None
    if _get_meta:
        meta = _get_meta(uid)
        if meta:
            cookies = meta.get("cookies") or {}
            ua = meta.get("user_agent")
    if cookies is None:
        from valorantBot2.services.cookiesDB import get_cookies as _gc
        cookies = _gc(uid)
    out = {k.lower(): (cookies.get(k) or cookies.get(k.upper()) or "") for k in ("ssid","clid","sub","csid","tdid","puuid","user_agent","ua")}
    if not out.get("user_agent"): out["user_agent"] = out.get("ua") or ""
    return out

def _candidate_paths(uid: str) -> List[Path]:
    here = Path(__file__).resolve()
    paths = [
        here.parents[1]/"services"/"cookies"/f"{uid}.txt",
        here.parents[2]/"cookies"/f"{uid}.txt",
        Path.cwd()/ "cookies"/f"{uid}.txt",
    ]
    envdir = os.getenv("VALORANT_COOKIES_DIR")
    if envdir: paths.insert(0, Path(envdir)/f"{uid}.txt")
    # dedupe
    seen, uniq = set(), []
    for p in paths:
        if p not in seen:
            uniq.append(p); seen.add(p)
    return uniq

def _load_file(uid: str) -> Optional[Dict[str,str]]:
    for p in _candidate_paths(uid):
        try:
            if not p.exists(): continue
            raw = {}
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or "=" not in line or line.startswith("#"): continue
                k,v = line.split("=",1)
                raw[k.strip()] = v.strip()
            out = { "ssid":raw.get("RIOT_SSID") or raw.get("SSID") or "",
                    "clid":raw.get("RIOT_CLID") or raw.get("CLID") or "",
                    "sub": raw.get("RIOT_SUB")  or raw.get("SUB")  or "",
                    "csid":raw.get("RIOT_CSID") or raw.get("CSID") or "",
                    "tdid":raw.get("RIOT_TDID") or raw.get("TDID") or "" }
            log.info("found cookie file: %s", p)
            return out
        except Exception:
            continue
    return None

def run(uid: str):
    db = _load_db(uid)
    file = _load_file(uid)

    db_user_agent = db.get("user_agent") or None
    matrix: List[Tuple[str, Dict[str,str], Optional[str], bool]] = [
        ("DB + DBUA + FULL",     db, db_user_agent, False),
        ("DB + DBUA + SSID",     db, db_user_agent, True),
        ("DB + defaultUA + FULL", db, None, False),
        ("DB + defaultUA + SSID", db, None, True),
    ]
    if file:
        matrix += [
            ("FILE + DBUA + FULL",      file, db_user_agent, False),
            ("FILE + DBUA + SSID",      file, db_user_agent, True),
            ("FILE + defaultUA + FULL", file, None, False),
            ("FILE + defaultUA + SSID", file, None, True),
        ]

    for label, env, ua, ssid_only in matrix:
        s = _new_session(ua)
        if ssid_only: _set_ssid_only(s, env.get("ssid"))
        else:         _set_full(s, env)
        p1, p2, ok = _try(AUTH_PARAMS_A, s)
        if not ok:
            p1b, p2b, ok = _try(AUTH_PARAMS_B, s)
            p1 = f"{p1}/{p1b}"
            p2 = f"{p2}/{p2b}"
        log.info("%-26s | UA=%s | SSID=%s | POST=%s GET=%s | OK=%s",
                 label, ("DB" if ua else "DEF"),
                 (env.get("ssid")[:4]+"…"+env.get("ssid")[-4:] if env.get("ssid") else "<none>"),
                 p1, p2, ok)

if __name__ == "__main__":
    import sys
    if len(sys.argv)<2:
        print("usage: python -m valorantBot2.scripts.diag_reauth <discord_user_id>", file=sys.stderr)
        sys.exit(1)
    run(sys.argv[1])
