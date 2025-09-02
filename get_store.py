import base64
import json
import os
import sys
from typing import Dict, List, Optional, Tuple

import requests
from dotenv import load_dotenv


AUTH_URL = "https://auth.riotgames.com/authorize"
USERINFO_URL = "https://auth.riotgames.com/userinfo"
ENTITLEMENTS_URL = "https://entitlements.auth.riotgames.com/api/token/v1"
PAS_URL = "https://riot-geo.pas.si.riotgames.com/pas/v1/product/valorant"
VERSION_URL = "https://valorant-api.com/v1/version"
SKINS_URL = "https://valorant-api.com/v1/weapons/skinlevels"
PD_URL = "https://pd.{shard}.a.pvp.net"

# Constant query parameters for /authorize taken from the official client
AUTH_PARAMS = {
    "client_id": "play-valorant-web-prod",
    "nonce": "1",
    "redirect_uri": "https://playvalorant.com/opt_in",
    "response_type": "token id_token",
    "scope": "openid link",
}

CLIENT_PLATFORM = base64.b64encode(
    json.dumps(
        {
            "platformType": "PC",
            "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit",
            "platformChipset": "Unknown",
        }
    ).encode()
).decode()


class ValorantStoreClient:
    """Client capable of retrieving the daily store for a user."""

    def __init__(self) -> None:
        self.session = requests.Session()
        self.access_token: Optional[str] = None
        self.id_token: Optional[str] = None
        self.entitlements_token: Optional[str] = None
        self.puuid: Optional[str] = None
        self.region: Optional[str] = None
        self.shard: Optional[str] = None
        self.client_version: Optional[str] = None

    # ------------------------------------------------------------------
    # Environment handling
    # ------------------------------------------------------------------
    @staticmethod
    def _load_env() -> None:
        """Load environment variables from the user specific .env file."""
        discord_id = os.getenv("DISCORD_USER_ID")
        if not discord_id:
            raise RuntimeError("DISCORD_USER_ID environment variable is required")
        env_path = f"/mnt/volume/env/.env{discord_id}"
        if not os.path.exists(env_path):
            raise RuntimeError(f".env file not found: {env_path}")
        load_dotenv(env_path)

    @staticmethod
    def _cookie_str_to_dict(cookie_str: str) -> Dict[str, str]:
        jar: Dict[str, str] = {}
        for item in cookie_str.split(";"):
            if "=" not in item:
                continue
            k, v = item.strip().split("=", 1)
            jar[k.strip()] = v.strip()
        return jar

    def _attach_cookies(self) -> None:
        """Attach AUTH_COOKIES and EXTRA_COOKIES to the session."""
        for env_var in ("AUTH_COOKIES", "EXTRA_COOKIES"):
            raw = os.getenv(env_var)
            if not raw:
                continue
            for key, val in self._cookie_str_to_dict(raw).items():
                self.session.cookies.set(key, val)

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------
    def _get_tokens_with_cookie_line(self, cookie_line: str) -> Optional[Tuple[str, str]]:
        headers = {"Cookie": cookie_line}
        resp = requests.get(
            AUTH_URL,
            params=AUTH_PARAMS,
            headers=headers,
            allow_redirects=False,
        )
        location = resp.headers.get("Location", "")
        if resp.status_code == 302 and "access_token" in location:
            return self._extract_tokens(location)
        return None

    def _get_tokens_with_cookiejar(self) -> Tuple[str, str]:
        self._attach_cookies()
        resp = self.session.get(AUTH_URL, params=AUTH_PARAMS, allow_redirects=False)
        location = resp.headers.get("Location", "")
        if resp.status_code == 302 and "access_token" in location:
            return self._extract_tokens(location)
        raise RuntimeError(f"Reauth failed: redirected to {location}")

    @staticmethod
    def _extract_tokens(location: str) -> Tuple[str, str]:
        """Extract access_token and id_token from the fragment of a URL."""
        fragment = location.split("#", 1)[1]
        params = {k: v for k, v in [pair.split("=") for pair in fragment.split("&")]}
        return params["access_token"], params["id_token"]

    def reauthenticate(self) -> None:
        cookie_line = os.getenv("COOKIE_LINE")
        tokens: Optional[Tuple[str, str]] = None
        if cookie_line:
            tokens = self._get_tokens_with_cookie_line(cookie_line)
        if not tokens:
            tokens = self._get_tokens_with_cookiejar()
        self.access_token, self.id_token = tokens

    # ------------------------------------------------------------------
    # Token & region helpers
    # ------------------------------------------------------------------
    def _get_entitlements(self) -> None:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.post(ENTITLEMENTS_URL, headers=headers, json={})
        resp.raise_for_status()
        self.entitlements_token = resp.json()["entitlements_token"]

    def _get_userinfo(self) -> None:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.get(USERINFO_URL, headers=headers)
        resp.raise_for_status()
        self.puuid = resp.json()["sub"]

    def _get_region(self) -> None:
        headers = {"Authorization": f"Bearer {self.access_token}"}
        resp = requests.put(PAS_URL, headers=headers, json={"id_token": self.id_token})
        resp.raise_for_status()
        self.region = resp.json()["affinities"]["live"]
        self.shard = self.region

    def _get_client_info(self) -> None:
        resp = requests.get(VERSION_URL)
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, dict) and "data" in data:
            self.client_version = data["data"]["riotClientVersion"]
        else:
            self.client_version = data["riotClientVersion"]

    # ------------------------------------------------------------------
    # Store retrieval
    # ------------------------------------------------------------------
    def _base_headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "X-Riot-Entitlements-JWT": self.entitlements_token,
            "X-Riot-ClientPlatform": CLIENT_PLATFORM,
            "X-Riot-ClientVersion": self.client_version,
        }

    def _store_v3(self) -> requests.Response:
        url = f"{PD_URL.format(shard=self.shard)}/store/v3/storefront/{self.puuid}"
        return requests.post(url, headers=self._base_headers(), json={})

    def _store_v2(self) -> requests.Response:
        url = f"{PD_URL.format(shard=self.shard)}/store/v2/storefront/{self.puuid}"
        return requests.get(url, headers=self._base_headers())

    def _diagnostics(self) -> None:
        base = PD_URL.format(shard=self.shard)
        wallet = requests.get(f"{base}/store/v1/wallet/{self.puuid}", headers=self._base_headers())
        name = requests.put(
            f"{base}/name-service/v2/players",
            headers=self._base_headers(),
            json=[self.puuid],
        )
        print(f"wallet status: {wallet.status_code}")
        print(f"name-service status: {name.status_code}")
        for candidate in ["ap", "na", "eu", "kr", "pbe"]:
            url = f"https://pd.{candidate}.a.pvp.net/store/v2/storefront/{self.puuid}"
            resp = requests.get(url, headers=self._base_headers())
            print(f"shard {candidate} -> {resp.status_code}")
            if resp.status_code == 200:
                self.shard = candidate
                return
        raise RuntimeError("全 shard で storefront が 200 を返しませんでした")

    def get_store(self) -> Dict:
        resp = self._store_v3()
        if resp.status_code == 200:
            return resp.json()
        if resp.status_code in (404, 405):
            resp = self._store_v2()
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                self._diagnostics()
                resp = self._store_v2()
                if resp.status_code == 200:
                    return resp.json()
        raise RuntimeError(f"storefront v3 失敗: {resp.status_code} {resp.text}")

    # ------------------------------------------------------------------
    # Skin resolution
    # ------------------------------------------------------------------
    @staticmethod
    def _load_skin_data() -> Dict[str, Dict[str, str]]:
        resp = requests.get(SKINS_URL, params={"language": "ja-JP"})
        resp.raise_for_status()
        mapping: Dict[str, Dict[str, str]] = {}
        for item in resp.json()["data"]:
            mapping[item["uuid"].lower()] = {
                "name": item["displayName"],
                "icon": item["displayIcon"],
            }
        return mapping

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------
    def fetch_items(self) -> List[Dict[str, object]]:
        self._load_env()
        self.reauthenticate()
        self._get_entitlements()
        self._get_userinfo()
        self._get_region()
        self._get_client_info()
        store = self.get_store()
        skins = self._load_skin_data()

        offers = store["SkinsPanelLayout"]["SingleItemStoreOffers"]
        items: List[Dict[str, object]] = []
        for offer in offers:
            offer_id = offer if isinstance(offer, str) else offer.get("OfferID")
            cost = (
                offer.get("Cost", {}).get("85ad13f6-4d74-0de1-ffff-ffffffffffff", 0)
                if isinstance(offer, dict)
                else None
            )
            info = skins.get(offer_id.lower())
            items.append(
                {
                    "name": info["name"] if info else offer_id,
                    "cost": cost,
                    "image": info.get("icon") if info else None,
                }
            )
        return items

    def run(self) -> None:
        items = self.fetch_items()
        print(f"[{self.region}, {self.shard}] Daily Skins ({len(items)} items)")
        for item in items:
            print(f"- {item['name']}: {item['cost']} VP")


def main() -> None:
    try:
        client = ValorantStoreClient()
        client.run()
    except Exception as exc:  # pragma: no cover - entry script
        print(str(exc), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
