# /views/buttons.py
import discord
from discord import ui, ButtonStyle, Interaction

# services/profile_service.py からURLビルダーをインポート
try:
    from services.profile_service import build_tracker_url
except ModuleNotFoundError as e:
    # 実行場所のズレ対策（/views から一階層上＝プロジェクトルートをパスに追加）
    import sys, os
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from services.profile_service import build_tracker_url  # 再挑戦


class TrackerModal(ui.Modal, title="tracker.gg プロフィールURL作成"):
    def __init__(self) -> None:
        super().__init__(timeout=300)
        self.game_name = ui.TextInput(
            label="Riot ID（名前部分）例: いのすけ",
            placeholder="Riot ID の名前部分を入力",
            required=True,
            max_length=32,
        )
        self.tag = ui.TextInput(
            label="タグ（#以降）例: 5070（#は不要）",
            placeholder="例: 5070",
            required=True,
            max_length=16,
        )
        self.add_item(self.game_name)
        self.add_item(self.tag)

    async def on_submit(self, interaction: Interaction) -> None:
        name = str(self.game_name.value).strip()
        tag = str(self.tag.value).strip().lstrip("#")  # 先頭の # は除去
        try:
            url = build_tracker_url(name, tag)
        except Exception as e:
            await interaction.response.send_message(f"URL 生成に失敗しました: {e}", ephemeral=True)
            return

        # 便利用にリンクボタンも付ける
        view = ui.View()
        view.add_item(ui.Button(label="tracker.gg を開く", style=ButtonStyle.link, url=url))
        await interaction.response.send_message(f"🔗 生成したURL:\n{url}", view=view, ephemeral=True)


class TrackerButtons(ui.View):
    """
    以前の StoreButtons は廃止。tracker ボタンのみ提供する View。
    """
    def __init__(self) -> None:
        super().__init__(timeout=120)

    @ui.button(label="tracker", style=ButtonStyle.primary, emoji="📊")
    async def tracker_btn(self, interaction: Interaction, _button: ui.Button) -> None:
        # モーダルを開いて Riot ID の name / tag を入力してもらう
        await interaction.response.send_modal(TrackerModal())
