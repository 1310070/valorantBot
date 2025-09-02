
# Valorant Storefront 取得 + スキンUUID→名称/アイコン解決（valorant-api.com 利用）
# - .env の Cookie を使って Cookie Reauth
# - entitlements → puuid → shard
# - storefront を v3(POST) 優先で取得
# - skinlevels 辞書で ItemID を displayName / displayIcon に解決して出力
#
# 必要パッケージ: requests, python-dotenv
#   pip install requests python-dotenv

import os
import base64
import json
import requests
from dotenv import load_dotenv

load_dotenv()

# ---- 環境変数から Cookie を組み立て（存在するものだけ使う） ----
AUTH_COOKIES = {
    # auth.riotgames.com 側
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
    # auth.riotgames.com 側
    for k, v in AUTH_COOKIES.items():
        c = requests.cookies.create_cookie(
            domain="auth.riotgames.com", name=k, value=v, path="/",
            secure=True, rest={"HttpOnly": True}
        )
        jar.set_cookie(c)
    # .riotgames.com 側
    for k, v in EXTRA_COOKIES.items():
        c = requests.cookies.create_cookie(
            domain=".riotgames.com", name=k, value=v, path="/",
            secure=True, rest={"HttpOnly": True}
        )
        jar.set_cookie(c)
    SESSION.cookies = jar

def cookie_reauth():
    """
    優先1: .env の RIOT_COOKIE_LINE をそのまま Cookie ヘッダとして送る
    優先2: CookieJar に積んだクッキーで送る
    成功時: Location に #access_token=... を含む
    """
    base_headers = {
        "Referer": "https://playvalorant.com/",
        "Origin": "https://playvalorant.com",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    # --- フォールバック1: ブラウザの Cookie ヘッダを“そのまま”使う ---
    if COOKIE_LINE and COOKIE_LINE.strip():
        SESSION.cookies.clear()  # 競合を避ける
        h = dict(SESSION.headers)
        h.update(base_headers)
        h["Cookie"] = COOKIE_LINE.strip()
        r = SESSION.get(AUTH_URL, headers=h, allow_redirects=False, timeout=20)
        loc = r.headers.get("Location", "")
        if "access_token=" in loc:
            frag = loc.split("#", 1)[-1]
            pairs = dict(kv.split("=", 1) for kv in frag.split("&") if "=" in kv)
            return pairs["access_token"], pairs.get("id_token")

    # --- 通常ルート: CookieJar ---
    _attach_cookies()
    r = SESSION.get(AUTH_URL, headers=base_headers, allow_redirects=False, timeout=20)
    loc = r.headers.get("Location", "")
    if "access_token=" not in loc:
        raise RuntimeError(f"Reauth failed: redirected to login. Location: {loc}")
    frag = loc.split("#", 1)[-1]
    pairs = dict(kv.split("=", 1) for kv in frag.split("&") if "=" in kv)
    return pairs["access_token"], pairs.get("id_token")

def post_entitlements(auth_token: str) -> str:
    """entitlements トークンを取得（Content-Type と空 JSON 必須）。"""
    url = "https://entitlements.auth.riotgames.com/api/token/v1"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    r = SESSION.post(url, headers=headers, json={}, timeout=20)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"entitlements 失敗: {r.status_code} {r.text}") from e
    return r.json()["entitlements_token"]

def get_player_info(auth_token: str) -> str:
    """PUUID を取得。/userinfo の sub が PUUID。"""
    url = "https://auth.riotgames.com/userinfo"
    headers = {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}
    r = SESSION.get(url, headers=headers, timeout=20)
    r.raise_for_status()
    return r.json()["sub"]

def get_region_and_shard(auth_token: str, id_token: str):
    """affinities.live（na/eu/ap/kr/pbe など）を取得。"""
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
    return region, region  # (region, shard)

def get_client_platform_b64() -> str:
    payload = {
        "platformType": "PC",
        "platformOS": "Windows",
        "platformOSVersion": "10.0.19042.1.256.64bit",
        "platformChipset": "Unknown",
    }
    return base64.b64encode(json.dumps(payload).encode()).decode()

def get_client_version() -> str:
    """client_version の取得。"""
    try:
        r = requests.get("https://valorant-api.com/v1/version", timeout=8)
        r.raise_for_status()
        return r.json()["data"]["riotClientVersion"]
    except Exception as e:
        raise RuntimeError("client_version を取得できませんでした。") from e

def get_storefront(shard: str, puuid: str, auth_token: str, ent_token: str,
                   client_version: str, client_platform_b64: str) -> dict:
    """v3(POST) を優先、ダメなら v2(GET) を試す。"""
    base_headers = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
    }
    # v3
    url_v3 = f"https://pd.{shard}.a.pvp.net/store/v3/storefront/{puuid}"
    h_v3 = dict(base_headers); h_v3["Content-Type"] = "application/json"
    r = SESSION.post(url_v3, headers=h_v3, json={}, timeout=20)
    if r.status_code == 200:
        return r.json()
    if r.status_code not in (404, 405):
        raise RuntimeError(f"storefront v3 失敗: {r.status_code} {r.text}")
    # v2
    url_v2 = f"https://pd.{shard}.a.pvp.net/store/v2/storefront/{puuid}"
    r2 = SESSION.get(url_v2, headers=base_headers, timeout=20)
    try:
        r2.raise_for_status()
    except requests.HTTPError as e:
        raise RuntimeError(f"storefront v2 失敗: {r2.status_code} {r2.text}") from e
    return r2.json()

# ---------- 診断＆フォールバック ----------

def try_pd(path: str, shard: str, headers: dict):
    url = f"https://pd.{shard}.a.pvp.net{path}"
    r = SESSION.get(url, headers=headers, timeout=15)
    return r.status_code, r.text

def sanity_checks(shard: str, puuid: str, auth_token: str, ent_token: str,
                  client_version: str, client_platform_b64: str):
    h = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
        "Content-Type": "application/json",
    }
    ws, _wt = try_pd(f"/store/v1/wallet/{puuid}", shard, h)
    print(f"[diag] wallet ({shard}) -> {ws}")
    url = f"https://pd.{shard}.a.pvp.net/name-service/v2/players"
    r = SESSION.put(url, headers=h, json=[puuid], timeout=15)
    print(f"[diag] name-service ({shard}) -> {r.status_code}")
    return ws, r.status_code

def find_working_shard(puuid: str, auth_token: str, ent_token: str,
                       client_version: str, client_platform_b64: str,
                       prefer: str | None):
    h = {
        "Authorization": f"Bearer {auth_token}",
        "X-Riot-Entitlements-JWT": ent_token,
        "X-Riot-ClientVersion": client_version,
        "X-Riot-ClientPlatform": client_platform_b64,
    }
    path = f"/store/v2/storefront/{puuid}"
    candidates = ["ap", "na", "eu", "kr", "pbe"]
    if prefer in candidates:
        candidates.remove(prefer); candidates.insert(0, prefer)
    print(f"[diag] probing storefront across shards: {candidates}")
    for s in candidates:
        status, _ = try_pd(path, s, h)
        print(f"[diag] storefront ({s}) -> {status}")
        if status == 200:
            return s
    return None

# ---------- 追加: skinlevels を使って UUID→名称/アイコン を解決 ----------

def fetch_skinlevel_dict(lang: str = "ja-JP") -> dict:
    """
    valorant-api.com の skinlevels 一覧を取得し、
    {uuid: {"name": displayName, "icon": displayIcon}} の辞書を返す。
    """
    url = f"https://valorant-api.com/v1/weapons/skinlevels?language={lang}"
    r = requests.get(url, timeout=15)
    r.raise_for_status()
    data = r.json().get("data", [])
    mapping = {}
    for item in data:
        uuid = item.get("uuid")
        name = item.get("displayName")
        icon = item.get("displayIcon") or item.get("streamedVideo")  # アイコンが無い場合の保険
        if uuid and name:
            mapping[uuid.lower()] = {"name": name, "icon": icon}
    return mapping

# ---------- Discord bot helper ----------

def getStore(_discord_id: int | str | None = None) -> list[dict[str, object]]:
    """Daily store offers as a list of dicts.

    Parameters
    ----------
    _discord_id: int | str | None
        Discord user ID.  The value is currently unused but kept for
        backwards compatibility with callers expecting this argument.

    Returns
    -------
    list[dict[str, object]]
        List of offers with ``name``, ``image`` and ``cost`` keys.
    """
    if not (COOKIE_LINE and COOKIE_LINE.strip()) and not AUTH_COOKIES.get("ssid"):
        raise RuntimeError(
            "環境変数 RIOT_SSID または RIOT_COOKIE_LINE がありません（最低限どちらか必要）。.env を確認してください。"
        )

    auth_token, id_token = cookie_reauth()
    ent_token = post_entitlements(auth_token)
    puuid = get_player_info(auth_token)
    region, shard = get_region_and_shard(auth_token, id_token)
    client_version = get_client_version()
    client_platform_b64 = get_client_platform_b64()

    try:
        store = get_storefront(
            shard,
            puuid,
            auth_token,
            ent_token,
            client_version,
            client_platform_b64,
        )
    except RuntimeError as e:
        msg = str(e)
        if "storefront v2 失敗: 404" in msg or "storefront v3 失敗: 404" in msg:
            sanity_checks(
                shard, puuid, auth_token, ent_token, client_version, client_platform_b64
            )
            working = find_working_shard(
                puuid,
                auth_token,
                ent_token,
                client_version,
                client_platform_b64,
                prefer=shard,
            )
            if not working:
                raise RuntimeError(
                    "全 shard で storefront が 200 を返しませんでした。headers/shard/puuid を再確認してください。"
                ) from e
            shard = working
            store = get_storefront(
                shard,
                puuid,
                auth_token,
                ent_token,
                client_version,
                client_platform_b64,
            )
        else:
            raise

    skin_dict = fetch_skinlevel_dict(lang="ja-JP")
    offers: list[dict[str, object]] = []
    skins = store.get("SkinsPanelLayout", {}).get("SingleItemStoreOffers", [])
    for offer in skins:
        cost = next(iter(offer["Cost"].values()))
        item_id = offer["Rewards"][0]["ItemID"].lower()
        info = skin_dict.get(item_id, {})
        offers.append(
            {
                "name": info.get("name", item_id),
                "image": info.get("icon"),
                "cost": cost,
            }
        )
    return offers

# ---------- メイン ----------

def main() -> None:
    items = getStore()
    print(f"[ok] Daily Skins ({len(items)} items)")
    for item in items:
        print(f"- {item['name']}: {item['cost']} VP")

if __name__ == "__main__":
    main()

