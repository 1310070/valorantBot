import os
import base64
import json
from pathlib import Path

import requests
from dotenv import load_dotenv, dotenv_values

# プロジェクトルート（bot.py と同じ階層）を基準にする
BASE_DIR = Path(__file__).resolve().parent.parent
ENV_DIR = BASE_DIR / "env"

# 任意：プロジェクト共通の .env（存在すれば既定値として読み込む）
# ※ ユーザー別 .env<id> が後で上書きします
load_dotenv(BASE_DIR / ".env")

# ---- 環境変数から Cookie を組み立て（存在するものだけ使う） ----
AUTH_COOKIES = {
    "ssid": os.getenv("RIOT_SSID"),
    "clid": os.getenv("RIOT_CLID"),
    "sub": os.getenv("RIOT_SUB"),
    "tdid": os.getenv("RIOT_TDID"),
    "csid": os.getenv("RIOT_CSID"),
}
AUTH_COOKIES = {k: v for k, v in AUTH_COOKIES.items() if v and v.strip()}

# .riotgames.com 側（あれば送る）
EXTRA_COOKIES = {
    "__Secure-refresh_token_presence": os.getenv("RIOT_SEC_REFRESH_PRESENCE"),
    "__Secure-session_state": os.getenv("RIOT_SEC_SESSION_STATE"),
    "_cf_bm": os.getenv("RIOT_CF_BM"),
}
EXTRA_COOKIES = {k: v for k, v in EXTRA_COOKIES.items() if v and v.strip()}

# ブラウザから丸ごとコピペした Cookie ライン（最優先フォールバック）
COOKIE_LINE = os.getenv("RIOT_COOKIE_LINE")

# ---- 共通 セッションとヘッダ ----
SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/118.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
})

VAL_API = "https://valorant-api.com/v1"

AUTH_URL = (
    "https://auth.riotgames.com/authorize"
    "?redirect_uri=https%3A%2F%2Fplayvalorant.com%2Fopt_in"
    "&client_id=play-valorant-web-prod"
    "&response_type=token%20id_token"
    "&nonce=1"
    "&scope=account%20openid"
)


def _attach_cookies():
    """RequestsCookieJar にドメイン指定でクッキーを積む。"""
    jar = requests.cookies.RequestsCookieJar()
    for k, v in AUTH_COOKIES.items():
        c = requests.cookies.create_cookie(
            domain="auth.riotgames.com", name=k, value=v, path="/",
            secure=True, rest={"HttpOnly": True}
        )
        jar.set_cookie(c)
    for k, v in EXTRA_COOKIES.items():
        c = requests.cookies.create_cookie(
            domain=".riotgames.com", name=k, value=v, path="/",
            secure=True, rest={"HttpOnly": True}
        )
        jar.set_cookie(c)
    SESSION.cookies = jar


def cookie_reauth():
    base_headers = {
        "Referer": "https://playvalorant.com/",
        "Origin": "https://playvalorant.com",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    if COOKIE_LINE and COOKIE_LINE.strip():
        SESSION.cookies.clear()
        h = dict(SESSION.headers)
        h.update(base_headers)
        h["Cookie"] = COOKIE_LINE.strip()
        r = SESSION.get(AUTH_URL, headers=h, allow_redirects=False, timeout=20)
        loc = r.headers.get("Location", "")
        if "access_token=" in loc:
            frag = loc.split("#", 1)[-1]
            pairs = dict(kv.split("=", 1) for kv in frag.split("&") if "=" in kv)
            return pairs["access_token"], pairs.get("id_token")

    _attach_cookies()
    r = SESSION.get(AUTH_URL, headers=base_headers, allow_redirects=False, timeout=20)
    loc = r.headers.get("Location", "")
    if "access_token=" not in loc:
        raise RuntimeError(f"Reauth failed: redirected to login. Location: {loc}")
    frag = loc.split("#", 1)[-1]
    pairs = dict(kv.split("=", 1) for kv in frag.split("&") if "=" in kv)
    return pairs["access_token"], pairs.get("id_token")


def post_entitlements(auth_token: str) -> str:
    url = "https://entitlements.auth.riotgames.com/api/token/v1"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    r = SESSION.post(url, headers=headers, json={}, timeout=20)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"entitlements 失敗: {r.status_code} {r.text}") from e
    return r.json()["entitlements_token"]


def get_player_info(auth_token: str) -> str:
    url = "https://auth.riotgames.com/userinfo"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    r = SESSION.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()["sub"]


def get_region_and_shard(auth_token: str, id_token: str):
    url = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    r = SESSION.put(url, headers=headers, json={"id_token": id_token}, timeout=20)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"riot-geo 失敗: {r.status_code} {r.text}") from e
    region = r.json().get("affinities", {}).get("live")
    if not region:
        raise RuntimeError(f"riot-geo: live affinity が取得できませんでした: {r.text}")
    return region, region


def get_client_platform_b64() -> str:
    payload = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": "10.0.19042.1.256.64bit",
        "platformChipset": "Unknown",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()


def get_client_version() -> str:
    try:
        r = requests.get("https://valorant-api.com/v1/version", timeout=8)
        r.raise_for_status()
        return r.json()["data"]["riotClientVersion"]
    except Exception as e:
        raise RuntimeError("client_version を取得できませんでした。") from e


def get_storefront(shard: str, puuid: str, auth_token: str, ent_token: str,
                   client_version: str, client_platform_b64: str) -> dict:
    base_headers = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
    }
    url_v3 = f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"
    h_v3 = dict(base_headers); h_v3["Content-Type"] = "application/json"
    r = SESSION.post(url_v3, headers=h_v3, json={}, timeout=20)
    if r.status_code == 200:
        return r.json()
    if r.status_code not in (404, 405):
        raise RuntimeError(f"storefront v3 失敗: {r.status_code} {r.text}")
    url_v2 = f"https://pd.{shard}.a.pvp.net/store/v2/storefront/{puuid}"
    r2 = SESSION.get(url_v2, headers=base_headers, timeout=20)
    try:
        r2.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"storefront v2 失敗: {r2.status_code} {r2.text}") from e
    return r2.json()


def resolve_skin_images_from_item_id(item_id: str, lang: str = "ja-JP") -> list[str]:
    """SkinLevel UUID から画像URL候補を最大4件返す"""
    images: list[str] = []

    # 1) SkinLevel
    lv = requests.get(f"{VAL_API}/weapons/skinlevels/{item_id}?language={lang}", timeout=10)
    if lv.status_code != 200:
        return images
    lvj = lv.json().get("data") or {}
    level_icon = lvj.get("displayIcon")
    skin_uuid = lvj.get("skinUuid")

    # 2) Skin（親）
    skin_json = None
    if skin_uuid:
        rs = requests.get(f"{VAL_API}/weapons/skins/{skin_uuid}?language={lang}", timeout=10)
        if rs.status_code == 200:
            skin_json = rs.json().get("data")

    # 3) 画像候補を優先度で積む
    if skin_json and skin_json.get("chromas"):
        default_full = None
        for ch in skin_json["chromas"]:
            if ch.get("displayName") and "Standard" in ch["displayName"]:
                default_full = ch.get("fullRender") or ch.get("displayIcon")
                break
        if not default_full and skin_json["chromas"]:
            ch0 = skin_json["chromas"][0]
            default_full = ch0.get("fullRender") or ch0.get("displayIcon")
        if default_full:
            images.append(default_full)

    if skin_json and skin_json.get("chromas"):
        for ch in skin_json["chromas"]:
            url = ch.get("fullRender") or ch.get("displayIcon")
            if url and url not in images:
                images.append(url)
            if len(images) >= 3:
                break

    if skin_json and skin_json.get("levels"):
        for lvobj in skin_json["levels"]:
            url = lvobj.get("displayIcon")
            if url and url not in images:
                images.append(url)
            if len(images) >= 4:
                break

    if len(images) < 4 and level_icon:
        images.append(level_icon)

    images = [u for u in images if u][:4]
    return images


def resolve_bundle_images(bundle_uuid: str, lang: str = "ja-JP") -> list[str]:
    """バンドル画像の候補を返す"""
    r = requests.get(f"{VAL_API}/bundles/{bundle_uuid}?language={lang}", timeout=10)
    if r.status_code != 200:
        return []
    data = r.json().get("data") or {}
    picks = []
    if data.get("displayIcon"):
        picks.append(data["displayIcon"])
    if data.get("verticalPromoImage"):
        picks.append(data["verticalPromoImage"])
    return picks[:2]


def fetch_skinlevel_dict(lang: str = "ja-JP") -> dict:
    url = f"https://valorant-api.com/v1/weapons/skinlevels?language={lang}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    mapping = {}
    for item in data:
        uuid = item.get("uuid")
        name = item.get("displayName")
        icon = item.get("displayIcon") or item.get("streamedVideo")
        if uuid and name:
            mapping[uuid.lower()] = {"name": name, "icon": icon}
    return mapping


def get_daily_store() -> tuple[str, list[str]]:
    if not (COOKIE_LINE and COOKIE_LINE.strip()) and not AUTH_COOKIES.get("ssid"):
        msg = "環境変数 RIOT_SSID または RIOT_COOKIE_LINE がありません（最低限どちらか必要）。.env を確認してください。"
        return msg, []

    try:
        auth_token, id_token = cookie_reauth()
        ent_token = post_entitlements(auth_token)
        puuid = get_player_info(auth_token)
        region, shard = get_region_and_shard(auth_token, id_token)
        client_version = get_client_version()
        client_platform_b64 = get_client_platform_b64()
        store = get_storefront(shard, puuid, auth_token, ent_token, client_version, client_platform_b64)
        skin_dict = fetch_skinlevel_dict(lang="ja-JP")
        skins = store.get("SkinsPanelLayout", {}).get("SingleItemStoreOffers", [])
    except Exception as e:
        return f"store 情報の取得に失敗しました: {e}", []

    lines = [f"[region={region}, shard={shard}] Daily Skins ({len(skins)} items)"]
    images: list[str] = []
    for offer in skins:
        cost = next(iter(offer["Cost"].values()))
        item_id = offer["Rewards"][0]["ItemID"]
        info = skin_dict.get(item_id.lower())
        if info:
            name = info["name"]
            lines.append(f"- {name}: {cost} VP")
        else:
            lines.append(f"- {item_id}: {cost} VP")
        candidates = resolve_skin_images_from_item_id(item_id)
        if candidates:
            images.append(candidates[0])
    return "\n".join(lines), images


def getStore(discord_user_id: int | str) -> tuple[str, list[str]]:
    """ユーザーごとの Cookie 設定を読み込んでストア情報を取得"""
    discord_user_id = str(discord_user_id)

    # ✨ 重要：project_root/env/.env<discord_user_id> を解決（絶対パス /env ではない）
    env_path = ENV_DIR / f".env{discord_user_id}"
    if not env_path.exists():
        return f"環境変数ファイルが見つかりません: {env_path}", []

    # ユーザー固有envを辞書として読み込む（os.environは汚さない）
    env = dotenv_values(env_path)

    # ここでグローバルのクッキー設定を上書き
    global AUTH_COOKIES, EXTRA_COOKIES, COOKIE_LINE
    AUTH_COOKIES = {
        "ssid": env.get("RIOT_SSID"),
        "clid": env.get("RIOT_CLID"),
        "sub": env.get("RIOT_SUB"),
        "tdid": env.get("RIOT_TDID"),
        "csid": env.get("RIOT_CSID"),
    }
    AUTH_COOKIES = {k: v for k, v in AUTH_COOKIES.items() if v and v.strip()}

    EXTRA_COOKIES = {
        "__Secure-refresh_token_presence": env.get("RIOT_SEC_REFRESH_PRESENCE"),
        "__Secure-session_state": env.get("RIOT_SEC_SESSION_STATE"),
        "_cf_bm": env.get("RIOT_CF_BM"),
    }
    EXTRA_COOKIES = {k: v for k, v in EXTRA_COOKIES.items() if v and v.strip()}

    COOKIE_LINE = env.get("RIOT_COOKIE_LINE")

    return get_daily_store()


if __name__ == "__main__":
    text, _ = get_daily_store()
    print(text)
