"""
Valorant storefront fetcher with auto PUUID discovery and robust Riot reauth.

- DBから SSID/CLID/SUB/CSID/TDID(+optional PUUID) と user_agent を取得
- reauth (POST→GETフォールバック、scopeの差分も吸収) で access_token/id_token を取得
- entitlements / PAS / storefront を順に呼び出し
- storefront の daily offers を UI 向けの name/price/icon 形式に変換するユーティリティも提供
"""

from __future__ import annotations

import base64
import json
import re
import sys
from typing import Any, Dict, Optional, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

# ---- DB cookie loader import (prefer meta version) ----
try:  # pragma: no cover
    from .cookiesDB import get_cookies_and_meta as _db_get_cookies_and_meta
except Exception:  # pragma: no cover
    _db_get_cookies_and_meta = None

try:  # pragma: no cover
    from .cookiesDB import get_cookies as _db_get_cookies
except Exception:  # pragma: no cover
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

# --- Auth params (both tried) ---
AUTH_PARAMS_A = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "account openid",
    "prompt": "none",
}
AUTH_PARAMS_B = {
    **AUTH_PARAMS_A,
    "scope": "openid link",
}

TIMEOUT = 15  # seconds
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
    "Origin": "https://playvalorant.com",
    "Referer": "https://playvalorant.com/opt_in",
}

# VP currency UUID
VP_ID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"

# ItemType: weapon skin のみを対象
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
        status_forcelist=(403, 409, 429, 500, 502, 503, 504),
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


def _cookie_kv(env: Dict[str, Optional[str]]) -> Dict[str, str]:
    """requests の cookies= で明示送信するための辞書を構築"""
    out: Dict[str, str] = {}
    for k in ("ssid", "clid", "sub", "csid", "tdid"):
        v = env.get(k)
        if v:
            out[k] = v
    return out


def _reauth_get_tokens(session: requests.Session, cookies_kv: Dict[str, str]) -> Tuple[str, str]:
    """
    Reauth flow（頑強版）:
      1) scope=account openid で POST → JSON uri 抽出（失敗でもすぐは落とさない）
         → GET /authorize (no redirects) で Location から抽出を試す
      2) ダメなら scope=openid link で同様に再試行
      3) 401/403 は「ログイン要」として RuntimeError を送出
    """
    last_text = "<no response>"
    for params in (AUTH_PARAMS_A, AUTH_PARAMS_B):
        # 1) POST
        r = session.post(AUTH_URL_LEGACY, json=params, timeout=TIMEOUT, cookies=cookies_kv)
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
        last_text = r.text[:500]

        # 2) GET fallback
        r2 = session.get(AUTH_URL_V2, params=params, allow_redirects=False, timeout=TIMEOUT, cookies=cookies_kv)
        if r2.status_code in (301, 302, 303, 307, 308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc:
                at = _extract_from_uri(loc, "access_token")
                it = _extract_from_uri(loc, "id_token")
                if at and it:
                    return at, it

        # 3) 判定
        if r.status_code in (401, 403) or r2.status_code in (401, 403):
            if params is AUTH_PARAMS_B:
                raise RuntimeError("Reauth failed (403/401). SSID が無効/期限切れの可能性。")
            continue  # 次の params で再試行

    raise RuntimeError(f"Reauth failed: tokens not found (dbg={last_text!r})")


def _get_entitlements_token(session: requests.Session, access_token: str) -> str:
    r = session.post(
        ENTITLEMENTS_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        json={},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("entitlements_token")
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
    r = session.post(
        USERINFO_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    data = r.json()
    puuid = data.get("sub")
    if not puuid:
        raise RuntimeError(f"PUUID (sub) not found in userinfo: {data!r}")
    return puuid


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


# ---------------- Cookie (DB) ----------------
def _load_cookies_from_db(discord_user_id: str) -> Dict[str, Optional[str]]:
    """
    Load stored Riot auth cookies and user-agent for ``discord_user_id`` from DB.
    Returns keys: ssid, puuid, clid, sub, csid, tdid, user_agent (sanitized).
    """
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

    return {
        "ssid": _sanitize(cookies.get("ssid") or cookies.get("RIOT_SSID")),
        "puuid": _sanitize(cookies.get("puuid") or cookies.get("RIOT_PUUID")),
        "clid": _sanitize(cookies.get("clid") or cookies.get("RIOT_CLID")),
        "sub":  _sanitize(cookies.get("sub")  or cookies.get("RIOT_SUB")),
        "csid": _sanitize(cookies.get("csid") or cookies.get("RIOT_CSID")),
        "tdid": _sanitize(cookies.get("tdid") or cookies.get("RIOT_TDID")),
        "user_agent": _sanitize(ua) or _sanitize(cookies.get("user_agent") or cookies.get("ua")),
    }


# ---------------- Skin indices ----------------
def _build_skin_info_index(session: requests.Session, lang: str = "en-US") -> Dict[str, Dict[str, Optional[str]]]:
    """
    Map ANY of skin/level/chroma UUID → {"name": displayName, "icon": displayIcon}
    """
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


# ---------------- Storefront HTTP helpers ----------------
def _get_storefront_v2(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64):
    url = STOREFRONT_V2_URL.format(shard=shard, puuid=puuid)
    return session.get(
        url,
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-Riot-Entitlements-JWT": entitlements,
            "X-Riot-ClientVersion": client_version,
            "X-Riot-ClientPlatform": client_platform_b64,
        },
        timeout=TIMEOUT,
    )


def _get_storefront_v3(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64):
    url = STOREFRONT_V3_URL.format(shard=shard, puuid=puuid)
    return session.post(
        url,
        json={},
        headers={
            "Authorization": f"Bearer {access_token}",
            "X-Riot-Entitlements-JWT": entitlements,
            "X-Riot-ClientVersion": client_version,
            "X-Riot-ClientPlatform": client_platform_b64,
        },
        timeout=TIMEOUT,
    )


# ---------------- Public APIs ----------------
def get_storefront(discord_user_id: str, auto_fetch_puuid: bool = True) -> Dict[str, Any]:
    """
    Fetch storefront JSON for the given Discord user ID, using cookies from DB.
    Raises:
      - ValueError: when cookies are missing / SSID missing / PUUID missing (and auto-fetch disabled)
      - RuntimeError: when reauth / entitlements / PAS / storefront request fails
    """
    env = _load_cookies_from_db(discord_user_id)
    if not env.get("ssid"):
        raise ValueError("Missing SSID in stored cookies.")

    # Session with stored UA if available (UA ミスマッチ回避)
    session = _new_session(user_agent=env.get("user_agent"))

    # Cookie は Jar に積む + reauth では cookies= で明示送信
    for domain in (".riotgames.com", "auth.riotgames.com"):
        for k in ("ssid", "clid", "sub", "csid", "tdid"):
            v = env.get(k)
            if v:
                session.cookies.set(k, v, domain=domain, path="/")

    # Reauth（POST→GET fallback、scope 2種で自動再試行）
    access_token, id_token = _reauth_get_tokens(session, _cookie_kv(env))

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
    resp = _get_storefront_v2(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
    if resp.status_code == 404:
        resp = _get_storefront_v3(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
    if resp.status_code == 403:
        raise RuntimeError("Storefront failed (403). 権限/クッキー/トークンを確認してください。")
    resp.raise_for_status()
    return resp.json()


def _price_vp(offer: Dict[str, Any]) -> Optional[int]:
    for key in ("DiscountedCost", "Cost"):
        cost = offer.get(key) or {}
        if isinstance(cost, dict) and VP_ID in cost:
            try:
                return int(cost[VP_ID])
            except Exception:
                return None
    return None


def get_store_items(discord_user_id: str) -> List[Dict[str, Any]]:
    """
    Return UI-ready item list: [{name, price, icon}] for the user's daily offers.
    Raises:
      - ValueError / RuntimeError (see get_storefront)
    """
    store = get_storefront(discord_user_id, auto_fetch_puuid=True)

    offers = (store.get("SkinsPanelLayout") or {}).get("SingleItemStoreOffers") or []
    if not isinstance(offers, list) or not offers:
        return []

    api_session = _new_session()
    info_idx = _build_skin_info_index(api_session, lang="en-US")
    items: List[Dict[str, Any]] = []
    for offer in offers:
        # Rewards から武器スキンUUID（level/chromaでもOK）を抽出
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

        api_session = _new_session()
        info_idx = _build_skin_info_index(api_session, lang="en-US")

        for idx, offer in enumerate(offers, start=1):
            skin_uuid = None
            for rw in (offer.get("Rewards") or []):
                if rw.get("ItemTypeID") == ITEMTYPE_WEAPON_SKIN:
                    skin_uuid = rw.get("ItemID")
                    break
            if not skin_uuid:
                continue
            info = info_idx.get(str(skin_uuid).lower(), {})
            name = info.get("name", str(skin_uuid))
            price = _price_vp(offer)
            price_str = f"{price} VP" if price is not None else "N/A"
            print(f"{idx}\t{name}\t{price_str}")

    except (ValueError, RuntimeError) as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
