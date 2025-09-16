"""
Valorant storefront fetcher with auto PUUID discovery and robust Riot reauth.

ポイント:
- ファイル版と同一挙動をまず実行（Cookie は Jar に積む / path 無指定 / UA 既定値）
- 失敗時に UA/ソース別フォールバック（DB UA → ファイル cookies → ファイル+DB UA）
- Reauth は POST → GET フォールバック、scope は account openid → openid link の順で再試行
"""

from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

# ---- DB cookie loader（UA付きがあれば優先）----
try:  # pragma: no cover
    from .cookiesDB import get_cookies_and_meta as _db_get_cookies_and_meta
except Exception:
    _db_get_cookies_and_meta = None
try:  # 互換: cookies のみ
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
    "Accept": "application/json",
    "Origin": "https://playvalorant.com",
    "Referer": "https://playvalorant.com/opt_in",
}

# VP currency UUID
VP_ID = "85ad13f7-3d1b-5128-9eb2-7cd8ee0b5741"
# ItemType: weapon skin
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


def _load_env_from_db(discord_user_id: str) -> Dict[str, Optional[str]]:
    """
    DBから cookies(+UA) を読み込む。
    戻り値: ssid, puuid, clid, sub, csid, tdid, user_agent（いずれもサニタイズ）
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


def _load_env_from_file(discord_user_id: str) -> Dict[str, Optional[str]]:
    """
    ファイル版互換: ./cookies/<discord_user_id>.txt から読み込み
    フォーマット:
      RIOT_SSID=..., RIOT_CLID=..., RIOT_SUB=..., RIOT_CSID=..., RIOT_TDID=..., (任意) RIOT_PUUID=...
    """
    cookie_path = Path(__file__).resolve().parent / "cookies" / f"{discord_user_id}.txt"
    if not cookie_path.exists():
        raise FileNotFoundError(f"Cookie file not found: {cookie_path}")
    raw: Dict[str, str] = {}
    with cookie_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, v = line.split("=", 1)
                raw[k.strip()] = v.strip()
    return {
        "ssid": _sanitize(raw.get("RIOT_SSID") or raw.get("SSID")),
        "puuid": _sanitize(raw.get("RIOT_PUUID") or raw.get("PUUID")),
        "clid": _sanitize(raw.get("RIOT_CLID") or raw.get("CLID")),
        "sub":  _sanitize(raw.get("RIOT_SUB")  or raw.get("SUB")),
        "csid": _sanitize(raw.get("RIOT_CSID") or raw.get("CSID")),
        "tdid": _sanitize(raw.get("RIOT_TDID") or raw.get("TDID")),
        "user_agent": None,  # ファイル版は UA を持たない
    }


def _set_auth_cookies_in_jar(session: requests.Session, env: Dict[str, Optional[str]]) -> None:
    """
    ファイル版と同一: path 指定なし、.riotgames.com と auth.riotgames.com の両方にセット。
    """
    def _set(k: str, v: Optional[str]) -> None:
        if not v:
            return
        session.cookies.set(k, v, domain=".riotgames.com")
        session.cookies.set(k, v, domain="auth.riotgames.com")
    _set("ssid", env.get("ssid"))
    for k in ("clid", "sub", "csid", "tdid"):
        _set(k, env.get(k))


def _reauth_get_tokens(session: requests.Session) -> Tuple[str, str]:
    """
    ファイル版準拠 + 強化:
      - まず scope=account openid で POST → JSON uri 抽出
        だめでも即エラーにせず、続けて GET /authorize の Location を試す
      - それでもダメなら scope=openid link で同様に再試行
      - 最後まで失敗なら RuntimeError
    """
    last_dbg = "<no response>"
    for params in (AUTH_PARAMS_A, AUTH_PARAMS_B):
        r = session.post(AUTH_URL_LEGACY, json=params, timeout=TIMEOUT)
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
        if r2.status_code in (301, 302, 303, 307, 308):
            loc = r2.headers.get("Location") or r2.headers.get("location")
            if loc:
                at = _extract_from_uri(loc, "access_token")
                it = _extract_from_uri(loc, "id_token")
                if at and it:
                    return at, it

    raise RuntimeError(f"Reauth failed: tokens not found (dbg={last_dbg!r})")


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
    まず DB cookies + 既定UA + Jar 送信（=ファイル版同等）で実行。
    → ダメなら順にフォールバック:
       (1) DB cookies + DB UA
       (2) ファイル cookies + 既定UA
       (3) ファイル cookies + DB UA（ある場合）
    """
    # 1) DB 環境読み込み
    db_env = _load_env_from_db(discord_user_id)
    file_env: Optional[Dict[str, Optional[str]]] = None  # 後で使うかも

    def _attempt(env: Dict[str, Optional[str]], ua: Optional[str]) -> Dict[str, Any]:
        if not env.get("ssid"):
            raise ValueError("Missing SSID in cookies.")
        session = _new_session(user_agent=ua)
        _set_auth_cookies_in_jar(session, env)

        access_token, id_token = _reauth_get_tokens(session)
        entitlements = _get_entitlements_token(session, access_token)
        shard = _get_shard(session, access_token, id_token)

        client_version = _get_client_version(session)
        client_platform_b64 = _build_client_platform_b64()

        puuid = env.get("puuid")
        if not puuid and auto_fetch_puuid:
            puuid = _get_puuid(session, access_token)
        if not puuid:
            raise ValueError("PUUID not provided and auto-fetch disabled.")

        resp = _get_storefront_v2(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
        if resp.status_code == 404:
            resp = _get_storefront_v3(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
        if resp.status_code == 403:
            raise RuntimeError("Storefront failed (403). 権限/クッキー/トークンを確認してください。")
        resp.raise_for_status()
        return resp.json()

    # A) DB cookies + 既定UA（ファイル版と同じUA）
    try:
        return _attempt(db_env, None)  # None → DEFAULT_UA
    except Exception:
        pass

    # B) DB cookies + DB UA
    if db_env.get("user_agent"):
        try:
            return _attempt(db_env, db_env.get("user_agent"))
        except Exception:
            pass

    # C) ファイル cookies + 既定UA
    try:
        file_env = _load_env_from_file(discord_user_id)
        return _attempt(file_env, None)
    except Exception:
        pass

    # D) ファイル cookies + DB UA（あれば）
    if (file_env is not None) and db_env.get("user_agent"):
        return _attempt(file_env, db_env.get("user_agent"))

    # 全て失敗
    raise RuntimeError("Reauth failed after all fallbacks. SSID/UA/cookies を再登録してください。")


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
    """
    uuid（skin/level/chroma 何でも）→ {name, icon}
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


def get_store_items(discord_user_id: str) -> List[Dict[str, Any]]:
    """
    UI 向け: [{name, price, icon}]
    """
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

        api_session = _new_session()
        # 名前解決用
        # （CLI では一覧を標準出力に流すだけ）
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
