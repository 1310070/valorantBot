# valorantBot2/services/get_store.py
"""
Valorant storefront fetcher with auto PUUID discovery and very-robust Riot reauth.

狙い:
- まず「SSIDだけ」クリーンな Jar で reauth を試し、ダメなら「全cookie」を載せて再試行
- これを DB/ファイル × UA(既定 or DB保存UA) の4通りで試す（計8通り）
- cookieファイルは複数の候補パスを探索（services/ 配下でない配置にも対応）

ログは必要最小限にし、値はマスクして出す。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

# ---- logging ----
log = logging.getLogger(__name__)


class ReauthExpired(RuntimeError):
    """Raised when Riot reauth flow fails to yield tokens."""


# ---- DB cookie loader（UA付きがあれば優先）----
try:
    from .cookiesDB import get_cookies_and_meta as _db_get_cookies_and_meta
except Exception:
    _db_get_cookies_and_meta = None
try:
    from .cookiesDB import get_cookies as _db_get_cookies
except Exception:
    try:
        from cookiesDB import get_cookies as _db_get_cookies  # type: ignore
    except Exception:
        _db_get_cookies = None

# --- Auth endpoints ---
AUTH_URL_LEGACY = "https://auth.riotgames.com/api/v1/authorization"
AUTH_URL_V2 = "https://auth.riotgames.com/authorize"
USERINFO_URL = "https://auth.riotgames.com/userinfo"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
PAS_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"

# --- PD endpoints ---
STOREFRONT_V2_URL = "https://pd.{shard}.a.pvp.net/store/v2/storefront/{puuid}"
STOREFRONT_V3_URL = "https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"

# --- Valorant-API base ---
VALAPI_BASE = "https://valorant-api.com"

# --- Auth params（まず A、ダメなら B）---
AUTH_PARAMS_A = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "account openid",
    "prompt": "none",
}
AUTH_PARAMS_B = {**AUTH_PARAMS_A, "scope": "openid link"}

TIMEOUT = 15  # seconds
DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "User-Agent": DEFAULT_UA,
    "Accept": "application/json,text/html;q=0.9",
    "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    "Origin": "https://playvalorant.com",
    "Referer": "https://playvalorant.com/opt_in",
}

# VP currency UUID / ItemType
VP_ID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
ITEMTYPE_WEAPON_SKIN = "e7c63390-eda7-46e0-bb7a-a6abdacd2433"


# ---------------- Session / Retry ----------------
def _new_session(user_agent: Optional[str] = None) -> requests.Session:
    s = requests.Session()
    headers = DEFAULT_HEADERS.copy()
    if user_agent:
        headers["User-Agent"] = user_agent
    s.headers.update(headers)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        # Do not retry on 403 (Cloudflare challenge) to avoid RetryError
        status_forcelist=(409, 429, 500, 502, 503, 504),
        allowed_methods={"GET", "POST", "PUT"},
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


# ---------------- Helpers ----------------
def _extract_from_uri(uri: str, key: str) -> Optional[str]:
    m = re.search(rf"{re.escape(key)}=([^&]+)", uri)
    return m.group(1) if m else None


def _sanitize(v: Optional[str]) -> Optional[str]:
    if v is None:
        return None
    v = str(v).strip()
    if len(v) >= 2 and ((v[0] == v[-1] == '"') or (v[0] == v[-1] == "'")):
        v = v[1:-1].strip()
    return v or None


def _mask(v: Optional[str]) -> str:
    if not v:
        return "<none>"
    if len(v) <= 8:
        return v[0:2] + "…" + v[-2:]
    return v[0:4] + "…" + v[-4:]


def _load_env_from_db(discord_user_id: str) -> Dict[str, Optional[str]]:
    cookies: Optional[Dict[str, str]] = None
    ua: Optional[str] = None

    if _db_get_cookies_and_meta:
        meta = _db_get_cookies_and_meta(discord_user_id)  # type: ignore[call-arg]
        if meta:
            cookies = meta.get("cookies") or {}
            ua = meta.get("user_agent")  # type: ignore[assignment]
    if cookies is None and _db_get_cookies:
        cookies = _db_get_cookies(discord_user_id)  # type: ignore[call-arg]
    if not cookies:
        raise ValueError(f"No cookies stored for Discord user {discord_user_id}")

    env = {
        "ssid": _sanitize(cookies.get("ssid") or cookies.get("RIOT_SSID")),
        "puuid": _sanitize(cookies.get("puuid") or cookies.get("RIOT_PUUID")),
        "clid": _sanitize(cookies.get("clid") or cookies.get("RIOT_CLID")),
        "sub":  _sanitize(cookies.get("sub")  or cookies.get("RIOT_SUB")),
        "csid": _sanitize(cookies.get("csid") or cookies.get("RIOT_CSID")),
        "tdid": _sanitize(cookies.get("tdid") or cookies.get("RIOT_TDID")),
        "user_agent": _sanitize(ua) or _sanitize(cookies.get("user_agent") or cookies.get("ua")),
    }
    log.debug("DB cookies loaded: ssid=%s, puuid=%s, ua=%s",
              _mask(env["ssid"]), _mask(env["puuid"]), _mask(env["user_agent"]))
    return env


def _candidate_cookie_paths(discord_user_id: str) -> List[Path]:
    """
    単体スクリプトと同じ配置/別配置の両方を探索:
      1) services/ 配下:                <this_dir>/cookies/<id>.txt
      2) プロジェクト直下:              <repo_root>/cookies/<id>.txt
      3) 実行時カレント:                CWD/cookies/<id>.txt
      4) 環境変数 VALORANT_COOKIES_DIR: $VALORANT_COOKIES_DIR/<id>.txt
    """
    here = Path(__file__).resolve()
    paths: List[Path] = [
        here.parent / "cookies" / f"{discord_user_id}.txt",
        here.parent.parent / "cookies" / f"{discord_user_id}.txt",
        Path.cwd() / "cookies" / f"{discord_user_id}.txt",
    ]
    env_dir = os.getenv("VALORANT_COOKIES_DIR")
    if env_dir:
        paths.insert(0, Path(env_dir) / f"{discord_user_id}.txt")
    # 重複除去
    uniq: List[Path] = []
    seen = set()
    for p in paths:
        if p not in seen:
            uniq.append(p)
            seen.add(p)
    return uniq


def _load_env_from_file(discord_user_id: str) -> Dict[str, Optional[str]]:
    last_err: Optional[Exception] = None
    for p in _candidate_cookie_paths(discord_user_id):
        try:
            if not p.exists():
                continue
            raw: Dict[str, str] = {}
            with p.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    if "=" in line:
                        k, v = line.split("=", 1)
                        raw[k.strip()] = v.strip()
            env = {
                "ssid": _sanitize(raw.get("RIOT_SSID") or raw.get("SSID")),
                "puuid": _sanitize(raw.get("RIOT_PUUID") or raw.get("PUUID")),
                "clid": _sanitize(raw.get("RIOT_CLID") or raw.get("CLID")),
                "sub":  _sanitize(raw.get("RIOT_SUB")  or raw.get("SUB")),
                "csid": _sanitize(raw.get("RIOT_CSID") or raw.get("CSID")),
                "tdid": _sanitize(raw.get("RIOT_TDID") or raw.get("TDID")),
                "user_agent": None,
            }
            log.debug("File cookies loaded from %s: ssid=%s, puuid=%s",
                      str(p), _mask(env["ssid"]), _mask(env["puuid"]))
            return env
        except Exception as e:
            last_err = e
            continue
    if last_err:
        raise last_err
    raise FileNotFoundError("No cookie file found in any candidate path.")


def _set_only_ssid(session: requests.Session, ssid: Optional[str]) -> None:
    """まずは SSID のみを Jar に積む（余計な cookie が 403 を誘発するケースの切り分け）"""
    session.cookies.clear()
    if not ssid:
        return
    for domain in (".riotgames.com", "auth.riotgames.com"):
        session.cookies.set("ssid", ssid, domain=domain)


def _set_full_cookies(session: requests.Session, env: Dict[str, Optional[str]]) -> None:
    """SSID + CLID/SUB/CSID/TDID を Jar に積む（path は指定しない＝単体スクリプト準拠）"""
    session.cookies.clear()
    def _set(k: str, v: Optional[str]) -> None:
        if not v:
            return
        for d in (".riotgames.com", "auth.riotgames.com"):
            session.cookies.set(k, v, domain=d)
    _set("ssid", env.get("ssid"))
    for k in ("clid", "sub", "csid", "tdid"):
        _set(k, env.get(k))


def _reauth_get_tokens(session: requests.Session) -> Tuple[str, str]:
    import logging
    log = logging.getLogger(__name__)
    last_dbg = "<no response>"
    for params in (AUTH_PARAMS_A, AUTH_PARAMS_B):
        r = session.post(AUTH_URL_LEGACY, json=params, timeout=TIMEOUT)
        log.debug("reauth POST scope=%s -> %s", params.get("scope"), r.status_code)
        if r.ok:
            try:
                data = r.json()
                uri = data.get("response", {}).get("parameters", {}).get("uri")
                if uri:
                    at = _extract_from_uri(uri, "access_token")
                    it = _extract_from_uri(uri, "id_token")
                    if at and it:
                        return at, it
            except Exception:
                pass
        last_dbg = r.text[:500]

        r2 = session.get(AUTH_URL_V2, params=params, allow_redirects=False, timeout=TIMEOUT)
        log.debug("reauth GET  scope=%s -> %s", params.get("scope"), r2.status_code)
        if r2.status_code in (301, 302, 303, 307, 308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc:
                at = _extract_from_uri(loc, "access_token")
                it = _extract_from_uri(loc, "id_token")
                if at and it:
                    return at, it
    # Cloudflare 文言検知でメッセージを差し替え
    if isinstance(last_dbg, str) and (
        "Attention Required! | Cloudflare" in last_dbg or "cf-browser-verification" in last_dbg
    ):
        raise ReauthExpired(
            "Reauth blocked by Cloudflare (403). Please change egress IP or use a trusted proxy."
        )
    raise ReauthExpired(f"Reauth failed: tokens not found (dbg={last_dbg!r})")


def _get_entitlements_token(session: requests.Session, access_token: str) -> str:
    r = session.post(
        ENTITLEMENTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    # 両対応
    token = data.get("entitlements_token") or data.get("token")
    if not token:
        raise RuntimeError(f"Entitlements token missing: {data!r}")
    return token


def _get_shard(session: requests.Session, access_token: str, id_token: str) -> str:
    r = session.put(
        PAS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={"id_token": id_token},
        timeout=TIMEOUT,
    )
    if r.status_code == 400:
        raise RuntimeError(f"PAS 400 Bad Request（id_token/Authorization を確認）: {r.text[:300]}")
    r.raise_for_status()
    data = r.json()
    try:
        shard = data["affinities"]["live"]
    except KeyError:
        raise RuntimeError(f"PAS response missing affinities.live: {data!r}")
    if not isinstance(shard, str) or not shard:
        raise RuntimeError(f"Invalid shard: {shard!r}")
    return shard


def _get_puuid(session: requests.Session, access_token: str) -> str:
    # ★ GET を推奨（POSTから変更）
    r = session.get(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    puuid = data.get("sub")
    if not puuid:
        raise RuntimeError(f"PUUID (sub) not found in userinfo: {data!r}")
    return puuuid


def _build_client_platform_b64() -> str:
    payload = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": "10.0.19042.1.256.64bit",
        "platformChipset": "Unknown",
    }
    js = json.dumps(payload, separators=(",", ":")).encode()
    return base64.b64encode(js).decode()


def _get_client_version(session: requests.Session) -> str:
    r = session.get(f"{VALAPI_BASE}/v1/version", timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json().get("data", {}) or {}
    return data.get("riotClientVersion") or data.get("riotClientBuild") or data.get("version") or ""


# ---- storefront GET helpers（新規実装） ----
def _get_storefront_v2(
    session: requests.Session,
    shard: str,
    puuid: str,
    access: str,
    ent: str,
    ver: str,
    plat_b64: str,
) -> requests.Response:
    url = STOREFRONT_V2_URL.format(shard=shard, puuid=puuid)
    return session.get(
        url,
        headers={
            "Authorization": f"Bearer {access}",
            "X-Riot-Entitlements-JWT": ent,
            "X-Riot-ClientVersion": ver,
            "X-Riot-ClientPlatform": plat_b64,
        },
        timeout=TIMEOUT,
    )


def _get_storefront_v3(
    session: requests.Session,
    shard: str,
    puuid: str,
    access: str,
    ent: str,
    ver: str,
    plat_b64: str,
) -> requests.Response:
    url = STOREFRONT_V3_URL.format(shard=shard, puuid=puuid)
    return session.get(
        url,
        headers={
            "Authorization": f"Bearer {access}",
            "X-Riot-Entitlements-JWT": ent,
            "X-Riot-ClientVersion": ver,
            "X-Riot-ClientPlatform": plat_b64,
        },
        timeout=TIMEOUT,
    )


# ---------------- Public APIs ----------------
def get_storefront(discord_user_id: str, auto_fetch_puuid: bool = True) -> Dict[str, Any]:
    """
    順番:
      DB(default UA, SSID only) →
      DB(default UA, FULL cookies) →
      DB(DB UA, SSID only) →
      DB(DB UA, FULL cookies) →
      FILE(default UA, SSID only) →
      FILE(default UA, FULL cookies) →
      FILE(DB UA, SSID only) →
      FILE(DB UA, FULL cookies)
    """
    # ---- load envs ----
    db_env = _load_env_from_db(discord_user_id)
    file_env: Optional[Dict[str, Optional[str]]] = None
    try:
        file_env = _load_env_from_file(discord_user_id)
    except Exception:
        # ファイルが無くても続行
        pass

    def _attempt(env: Dict[str, Optional[str]], ua: Optional[str], *, only_ssid: bool) -> Dict[str, Any]:
        if not env.get("ssid"):
            raise ValueError("Missing SSID in cookies.")
        session = _new_session(user_agent=ua)
        if only_ssid:
            _set_only_ssid(session, env.get("ssid"))
        else:
            _set_full_cookies(session, env)

        # Reauth
        access_token, id_token = _reauth_get_tokens(session)
        # Tokens & shard
        entitlements = _get_entitlements_token(session, access_token)
        shard = _get_shard(session, access_token, id_token)
        # Client headers
        client_version = _get_client_version(session)
        client_platform_b64 = _build_client_platform_b64()
        # PUUID
        puuid = env.get("puuid")
        if not puuid and auto_fetch_puuid:
            puuid = _get_puuid(session, access_token)
        if not puuid:
            raise ValueError("PUUID not provided and auto-fetch disabled.")
        # storefront: v2 → 404ならv3
        resp = _get_storefront_v2(
            session, shard, puuid, access_token, entitlements, client_version, client_platform_b64
        )
        if resp.status_code == 404:
            resp = _get_storefront_v3(
                session, shard, puuid, access_token, entitlements, client_version, client_platform_b64
            )
        if resp.status_code == 403:
            raise RuntimeError("Storefront failed (403).")
        resp.raise_for_status()
        return resp.json()

    # 試行セット
    attempts: List[Tuple[Dict[str, Optional[str]], Optional[str], bool, str]] = [
        (db_env, None, True,  "DB + defaultUA + SSID"),
        (db_env, None, False, "DB + defaultUA + FULL"),
        (db_env, db_env.get("user_agent"), True,  "DB + DBUA + SSID"),
        (db_env, db_env.get("user_agent"), False, "DB + DBUA + FULL"),
    ]
    if file_env:
        attempts += [
            (file_env, None, True,  "FILE + defaultUA + SSID"),
            (file_env, None, False, "FILE + defaultUA + FULL"),
            (file_env, db_env.get("user_agent"), True,  "FILE + DBUA + SSID"),
            (file_env, db_env.get("user_agent"), False, "FILE + DBUA + FULL"),
        ]

    last_err: Optional[Exception] = None
    for env, ua, only_ssid, label in attempts:
        try:
            log.debug("Attempt: %s (ssid=%s, ua=%s)", label, _mask(env.get("ssid")), _mask(ua))
            return _attempt(env, ua, only_ssid=only_ssid)
        except Exception as e:
            last_err = e
            log.info("Attempt failed: %s -> %s", label, repr(e))
            continue

    if last_err:
        if isinstance(last_err, ReauthExpired):
            raise last_err
        raise RuntimeError("Reauth failed after all fallbacks.") from last_err
    raise RuntimeError("Reauth failed (no attempts executed).")


def _price_vp(offer: Dict[str, Any]) -> Optional[int]:
    for key in ("DiscountedCost", "Cost"):
        cost = offer.get(key) or {}
        if isinstance(cost, dict) and VP_ID in cost:
            try:
                return int(cost[VP_ID])
            except Exception:
                return None
    return None


def _build_skin_info_index(session: requests.Session, lang: str = "en-US") -> Dict[str, Dict[str, Optional[str]]]:
    idx: Dict[str, Dict[str, Optional[str]]] = {}
    r = session.get(f"{VALAPI_BASE}/v1/weapons/skins", params={"language": lang}, timeout=TIMEOUT)
    r.raise_for_status()
    for skin in r.json().get("data") or []:
        name = skin.get("displayName")
        icon = skin.get("displayIcon")
        if not icon:
            levels = skin.get("levels") or []
            if levels:
                icon = (levels[0] or {}).get("displayIcon")
        skin_uuid = skin.get("uuid")
        if not name or not skin_uuid:
            continue
        info = {"name": name, "icon": icon}
        idx[str(skin_uuid).lower()] = info
        for lv in (skin.get("levels") or []):
            u = (lv or {}).get("uuid")
            if u:
                idx[str(u).lower()] = info
        for ch in (skin.get("chromas") or []):
            u = (ch or {}).get("uuid")
            if u:
                idx[str(u).lower()] = info
    return idx


def get_store_items(discord_user_id: str) -> List[Dict[str, Any]]:
    store = get_storefront(discord_user_id, auto_fetch_puuid=True)
    offers = (store.get("SkinsPanelLayout") or {}).get("SingleItemStoreOffers") or []
    if not isinstance(offers, list) or not offers:
        return []
    api_session = _new_session()
    info_idx = _build_skin_info_index(api_session, lang="en-US")
    items: List[Dict[str, Any]] = []
    for offer in offers:
        skin_uuid: Optional[str] = None
        for rw in (offer.get("Rewards") or []):
            if rw.get("ItemTypeID") == ITEMTYPE_WEAPON_SKIN:
                skin_uuid = rw.get("ItemID")
                break
        if not skin_uuid:
            continue
        info = info_idx.get(str(skin_uuid).lower())
        name = info["name"] if info else str(skin_uuid)
        icon = info.get("icon") if info else None
        price = _price_vp(offer)
        items.append({"name": name, "price": price, "icon": icon})
    return items


# ---------------- CLI ----------------
def _usage() -> None:
    print("Usage: python get_store.py <discord_user_id>", file=sys.stderr)


if __name__ == "__main__":
    if len(sys.argv) < 2:
        _usage()
        sys.exit(1)

    user_id = sys.argv[1]
    try:
        store = get_storefront(user_id, auto_fetch_puuid=True)
        offers = (store.get("SkinsPanelLayout") or {}).get("SingleItemStoreOffers") or []
        if not isinstance(offers, list) or not offers:
            print("item offers が見つかりませんでした。")
            sys.exit(0)
        for idx, offer in enumerate(offers, start=1):
            skin_uuid = None
            for rw in (offer.get("Rewards") or []):
                if rw.get("ItemTypeID") == ITEMTYPE_WEAPON_SKIN:
                    skin_uuid = rw.get("ItemID")
                    break
            if not skin_uuid:
                continue
            price = _price_vp(offer)
            price_str = f"{price} VP" if price is not None else "N/A"
            print(f"{idx}\t{skin_uuid}\t{price_str}")
    except (ValueError, RuntimeError) as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
