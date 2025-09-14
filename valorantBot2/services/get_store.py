"""
VALORANT storefront fetcher with auto PUUID discovery + robust reauth fallback.
Console output: item number, SkinName(en-US), VP (from SingleItemStoreOffers)

Source of cookies:
  ./cookies/616105796941381642.txt  (relative to this file)

File format (key=value, one per line):
  RIOT_SSID=...
  RIOT_CLID=...
  RIOT_SUB=...
  RIOT_CSID=...
  RIOT_TDID=...
  (optional) RIOT_PUUID=...

Usage:
  python storefront.py
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, List

import requests
from requests.adapters import HTTPAdapter, Retry

# --- Auth endpoints ---
AUTH_URL = "https://auth.riotgames.com/authorize"
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


# === /authorize を GET して Location ヘッダーからトークンを抽出 ===
def _reauth_get_tokens(session: requests.Session) -> Tuple[str, str]:
    """
    Use /authorize endpoint with stored auth cookies (e.g. SSID) to obtain tokens.
    Tokens are encoded in the Location header of the redirect response.
    """
    r = session.get(
        AUTH_URL,
        params=AUTH_PARAMS,
        allow_redirects=False,
        timeout=TIMEOUT,
    )

    # 成功時は 301/302 で playvalorant.com/opt_in にリダイレクト
    if r.status_code in (301, 302):
        loc = r.headers.get("Location")
        if loc:
            at = _extract_from_uri(loc, "access_token")
            it = _extract_from_uri(loc, "id_token")
            if at and it:
                return at, it

    # 失敗時は login URL 等にリダイレクトされる
    raise RuntimeError("Reauth failed: tokens not found; SSID may be invalid/expired")


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


# ---------- Cookie loader (fixed relative path) ----------
def _load_env() -> Dict[str, Optional[str]]:
    """
    固定ファイル ./cookies/616105796941381642.txt から key=value を読み込み、
    既存コードが期待するキー名（ssid, puuid, clid, sub, csid, tdid）で返す。
    """
    cookie_path = Path(__file__).resolve().parent / "cookies" / "616105796941381642.txt"
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
        "ssid": raw.get("RIOT_SSID") or raw.get("SSID"),
        "puuid": raw.get("RIOT_PUUID") or raw.get("PUUID"),
        "clid": raw.get("RIOT_CLID") or raw.get("CLID"),
        "sub":  raw.get("RIOT_SUB")  or raw.get("SUB"),
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


def get_storefront(auto_fetch_puuid: bool = True) -> Dict[str, Any]:
    env = _load_env()
    if not env["ssid"]:
        raise ValueError("Missing SSID. Put RIOT_SSID in ./cookies/616105796941381642.txt")

    session = _new_session()
    _set_auth_cookies_for_both_domains(session, env)

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

    resp = _get_storefront_v2(
        session, shard, puuid, access_token, entitlements, client_version, client_platform_b64
    )
    if resp.status_code == 404:
        resp = _get_storefront_v3(
            session, shard, puuid, access_token, entitlements, client_version, client_platform_b64
        )
    if resp.status_code == 403:
        raise RuntimeError("Storefront failed (403). 権限/クッキー/トークンを確認してください。")
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


if __name__ == "__main__":  # manual invocation
    try:
        store = get_storefront(auto_fetch_puuid=True)
        api_session = _new_session()
        # 1回だけ全スキンを取得してインデックス作成（skin/level/chroma → 親スキン名）
        name_index = _build_skin_name_index(api_session, lang="en-US")
        _print_item_number_name_price(store, name_index)
    except Exception as e:
        print(f"[error] {e}", file=sys.stderr)
        sys.exit(1)
