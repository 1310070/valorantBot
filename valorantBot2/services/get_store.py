"""Valorant のデイリーストア取得サービス"""

from typing import List, Dict
import os

# ルートに置かれている get_store モジュールからクライアントを読み込む
try:  # プロジェクト直下からの実行を想定
    from get_store import ValorantStoreClient
except ModuleNotFoundError:  # 実行場所のズレ対策
    import sys
    sys.path.append(os.path.dirname(os.path.dirname(os.path.dirname(__file__))))
    from get_store import ValorantStoreClient  # type: ignore


def getStore(discord_user_id: int) -> List[Dict[str, object]]:
    """
    指定された Discord ユーザーIDに対応する .env ファイルを読み込み、
    Valorant のデイリーストアアイテムを取得して返す。

    戻り値は [{'name': str, 'cost': int | None, 'image': str | None}, ...]
    の形式のリスト。
    """

    # get_store モジュールでは DISCORD_USER_ID 環境変数を参照して
    # /mnt/volume/env/.env<ID> を開くため、ここで設定しておく。
    os.environ["DISCORD_USER_ID"] = str(discord_user_id)

    client = ValorantStoreClient()
    return client.fetch_items()

