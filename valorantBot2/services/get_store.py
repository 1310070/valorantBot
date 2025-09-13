"""
VALORANT storefront fetcher with auto PUUID discovery + robust reauth fallback.
Console output: item number, SkinName(en-US), VP (from SingleItemStoreOffers)

Env:
  - RIOT_SSID or SSID              : Riot authentication cookie (required)
  - (optional) RIOT_PUUID or PUUID : if present, it's used; otherwise auto-fetched

Usage:
  python storefront.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from typing import Any, Dict, Optional, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

from .cookiesDB import get_cookies


class ReauthExpired(Exception):
    """SSID が無効/期限切れのときに使う専用例外"""
    pass

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

# --- Common params for Riot auth ---
AUTH_PARAMS = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "account openid",
    "prompt": "none",
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

def _new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    retry = Retry(
        total=3,
        backoff_factor=0.5,
        status_forcelist=(403, 409, 429, 500, 502, 503, 504),
        allowed_methods={"GET", "POST", "PUT"},
        raise_on_status=False,
    )
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s

# --- Helpers to extract tokens from URI fragment ---
def _extract_from_uri(uri: str, key: str) -> Optional[str]:
    m = re.search(rf"{re.escape(key)}=([^&]+)", uri)
    return m.group(1) if m else None

def _reauth_get_tokens(session: requests.Session) -> Tuple[str, str]:
    r = session.post(AUTH_URL_LEGACY, json=AUTH_PARAMS, timeout=TIMEOUT)
    if r.status_code == 403:
        raise ReauthExpired("Reauth failed (403). SSID が無効/期限切れの可能性。")
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

    r2 = session.get(AUTH_URL_V2, params=AUTH_PARAMS, allow_redirects=False, timeout=TIMEOUT)
    if r2.status_code in (301, 302, 303, 307, 308):
        loc = r2.headers.get("Location") or r2.headers.get("location") or ""
        # login_required なら SSID が失効しているので専用例外を投げる
        if "error=login_required" in loc:
            raise ReauthExpired("login_required（SSID 無効/期限切れ）")
        if loc:
            at = _extract_from_uri(loc, "access_token")
            it = _extract_from_uri(loc, "id_token")
            if at and it:
                return at, it

    try:
        dbg = r.json()
    except Exception:
        dbg = r.text[:500]
    # Riot が login_required を返す典型
    if isinstance(dbg, dict) and (dbg.get("response") or {}).get("parameters", {}).get("uri", "").find("error=login_required") != -1:
        raise ReauthExpired(f"login_required（SSID 期限切れの可能性） dbg={dbg!r}")
    raise RuntimeError(f"Reauth failed: tokens not found (dbg={dbg!r})")


def _check_ssid_valid(session: requests.Session) -> None:
    """軽い疎通確認: /authorize の Location をみて login_required なら即エラー"""
    r = session.get(AUTH_URL_V2, params=AUTH_PARAMS, allow_redirects=False, timeout=TIMEOUT)
    loc = r.headers.get("Location") or ""
    if "error=login_required" in loc or ("access_token=" not in loc and r.status_code in (301, 302, 303, 307, 308)):
        raise ReauthExpired("login_required（SSID 無効/期限切れ）")

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

def _load_env(discord_user_id: Optional[int] = None) -> Dict[str, Optional[str]]:
    raw: Dict[str, str] = {}
    if discord_user_id is not None:
        cookies = get_cookies(str(discord_user_id))
        if not cookies:
            raise FileNotFoundError(f"Cookies not found for discord_user_id={discord_user_id}")
        raw = {
            "RIOT_SSID": cookies.get("ssid"),
            "RIOT_PUUID": cookies.get("puuid"),
            "RIOT_CLID": cookies.get("clid"),
            "RIOT_SUB": cookies.get("sub"),
            "RIOT_CSID": cookies.get("csid"),
            "RIOT_TDID": cookies.get("tdid"),
        }
    else:
        raw.update(os.environ)
    return {
        "ssid": raw.get("RIOT_SSID") or raw.get("SSID"),
        "puuid": raw.get("RIOT_PUUID") or raw.get("PUUID"),
        "clid": raw.get("RIOT_CLID") or raw.get("CLID"),
        "sub": raw.get("RIOT_SUB") or raw.get("SUB"),
        "csid": raw.get("RIOT_CSID") or raw.get("CSID"),
        "tdid": raw.get("RIOT_TDID") or raw.get("TDID"),
    }

def _set_auth_cookies_for_both_domains(session: requests.Session, env: Dict[str, Optional[str]]) -> None:
    def _set(k: str, v: Optional[str]) -> None:
        if not v:
            return
        session.cookies.set(k, v, domain=".riotgames.com")
        session.cookies.set(k, v, domain="auth.riotgames.com")
    _set("ssid", env.get("ssid"))
    for k in ("clid", "sub", "csid", "tdid"):
        _set(k, env.get(k))

# ---------- Valorant API: build skin name index (covers skin/level/chroma UUID) ----------
def _build_skin_name_index(session: requests.Session, lang: str = "en-US") -> Dict[str, str]:
    """
    Fetch all skins once and build a map from ANY uuid (skin / level / chroma) to parent skin displayName.
    This is robust even when storefront returns level/chroma UUIDs.
    """
    idx: Dict[str, str] = {}
    r = session.get(f"{VALAPI_BASE}/v1/weapons/skins", params={"language": lang}, timeout=TIMEOUT)
    r.raise_for_status()
    for skin in r.json().get("data") or []:
        name = skin.get("displayName")
        skin_uuid = skin.get("uuid")
        if not name or not skin_uuid:
            continue
        # map parent
        idx[str(skin_uuid).lower()] = name
        # map levels
        for lv in (skin.get("levels") or []):
            u = (lv or {}).get("uuid")
            if u:
                idx[str(u).lower()] = name
        # map chromas
        for ch in (skin.get("chromas") or []):
            u = (ch or {}).get("uuid")
            if u:
                idx[str(u).lower()] = name
    return idx

def _name_from_index(uuid: Optional[str], idx: Dict[str, str]) -> str:
    if not uuid:
        return "UNKNOWN"
    return idx.get(str(uuid).lower(), uuid)

# ---------- PD storefront helpers (v2 → 404 then v3) ----------
def _get_storefront_v2(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64):
    url = STOREFRONT_V2_URL.format(shard=shard, puuid=puuid)
    return session.get(url, headers={
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
    }, timeout=TIMEOUT)

def _get_storefront_v3(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64):
    url = STOREFRONT_V3_URL.format(shard=shard, puuid=puuid)
    return session.post(url, json={}, headers={
        "Authorization": f"Bearer {access_token}",
        "X-Riot-Entitlements-JWT": entitlements,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
    }, timeout=TIMEOUT)

def get_storefront(auto_fetch_puuid: bool = True, discord_user_id: Optional[int] = None) -> Dict[str, Any]:
    env = _load_env(discord_user_id)
    if not env["ssid"]:
        raise ValueError("Missing SSID. Set RIOT_SSID or SSID in environment/.env")

    session = _new_session()
    _set_auth_cookies_for_both_domains(session, env)
    # SSID を早期検査して無駄な API を避ける
    _check_ssid_valid(session)

    access_token, id_token = _reauth_get_tokens(session)
    entitlements = _get_entitlements_token(session, access_token)
    shard = _get_shard(session, access_token, id_token)

    client_version = _get_client_version(session)
    client_platform_b64 = _build_client_platform_b64()

    puuid = env["puuid"]
    if not puuid and auto_fetch_puuid:
        puuid = _get_puuid(session, access_token)
    if not puuid:
        raise ValueError("PUUID not provided and auto-fetch disabled.")

    resp = _get_storefront_v2(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
    if resp.status_code == 404:
        resp = _get_storefront_v3(session, shard, puuid, access_token, entitlements, client_version, client_platform_b64)
    if resp.status_code == 403:
        raise ReauthExpired("Storefront failed (403). ログインが必要です（SSID が失効している可能性）。")
    resp.raise_for_status()
    return resp.json()

# ---------- Price helper ----------
def _price_vp(offer: Dict[str, Any]) -> Optional[int]:
    for key in ("DiscountedCost", "Cost"):
        cost = offer.get(key) or {}
        if isinstance(cost, dict) and VP_ID in cost:
            try:
                return int(cost[VP_ID])
            except Exception:
                return None
    return None

# ---------- Print: item number  SkinName  VP ----------
def _print_item_number_name_price(store: Dict[str, Any], name_idx: Dict[str, str]) -> None:
    """
    SkinsPanelLayout.SingleItemStoreOffers を対象に、
    左から: item number / SkinName(en-US) / VP を出力
    """
    offers = (store.get("SkinsPanelLayout") or {}).get("SingleItemStoreOffers") or []
    if not isinstance(offers, list) or not offers:
        print("item offers が見つかりませんでした。")
        return

    for idx, offer in enumerate(offers, start=1):
        # Rewards から武器スキンの UUID を探す（level/chromaでもOK→indexで解決）
        skin_uuid = None
        for rw in (offer.get("Rewards") or []):
            if rw.get("ItemTypeID") == ITEMTYPE_WEAPON_SKIN:
                skin_uuid = rw.get("ItemID")
                break
        if not skin_uuid:
            continue

        name = _name_from_index(skin_uuid, name_idx)
        price = _price_vp(offer)
        price_str = f"{price} VP" if price is not None else "N/A"
        print(f"{idx}\t{name}\t{price_str}")

def get_store_text(discord_user_id: int) -> str:
    store = get_storefront(auto_fetch_puuid=True, discord_user_id=discord_user_id)
    api_session = _new_session()
    name_index = _build_skin_name_index(api_session, lang="en-US")
    offers = (store.get("SkinsPanelLayout") or {}).get("SingleItemStoreOffers") or []
    if not isinstance(offers, list) or not offers:
        return "item offers が見つかりませんでした。"
    lines: List[str] = []
    for idx, offer in enumerate(offers, start=1):
        skin_uuid = None
        for rw in (offer.get("Rewards") or []):
            if rw.get("ItemTypeID") == ITEMTYPE_WEAPON_SKIN:
                skin_uuid = rw.get("ItemID")
                break
        if not skin_uuid:
            continue
        name = _name_from_index(skin_uuid, name_index)
        price = _price_vp(offer)
        price_str = f"{price} VP" if price is not None else "N/A"
        lines.append(f"{idx}\t{name}\t{price_str}")
    return "\n".join(lines)

if __name__ == "__main__":  # manual invocation
    try:
        user_id = int(sys.argv[1]) if len(sys.argv) > 1 else None
        store = get_storefront(auto_fetch_puuid=True, discord_user_id=user_id)
        api_session = _new_session()
        # 1回だけ全スキンを取得してインデックス作成（skin/level/chroma → 親スキン名）
        name_index = _build_skin_name_index(api_session, lang="en-US")
        _print_item_number_name_price(store, name_index)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
