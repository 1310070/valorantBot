# services/profile_service.py
from urllib.parse import quote

def build_tracker_url(game_name: str, tag: str) -> str:
    """
    Riot ID (例: 'いのすけ' と '5070') を受け取り、
    tracker.gg のプロフィールURLを作る。
    """
    # 各パートを個別にURLエンコード
    name_enc = quote(game_name, safe="")
    tag_enc  = quote(tag, safe="")
    # 形式: https://tracker.gg/valorant/profile/riot/<NAME>%23<TAG>/overview
    return f"https://tracker.gg/valorant/profile/riot/{name_enc}%23{tag_enc}/overview"
