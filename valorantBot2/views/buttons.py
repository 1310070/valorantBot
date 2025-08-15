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


class CallResponseView(ui.View):
    """募集DM内で使用する参加/不参加ボタン"""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    @ui.button(label="参加", style=ButtonStyle.success)
    async def accept(self, interaction: Interaction, _button: ui.Button) -> None:
        owner = interaction.client.get_user(self.owner_id)
        if owner:
            try:
                await owner.send(f"{interaction.user.display_name} さんが参加を希望しました。")
            except Exception:
                pass
        await interaction.response.send_message("参加を送信しました。", ephemeral=True)

    @ui.button(label="不参加", style=ButtonStyle.secondary)
    async def decline(self, interaction: Interaction, _button: ui.Button) -> None:
        await interaction.response.send_message("またお願いします。", ephemeral=True)


class MissingNumberModal(ui.Modal):
    def __init__(self, owner_id: int, game: str) -> None:
        super().__init__(title="募集人数入力", timeout=300)
        self.owner_id = owner_id
        self.game = game
        self.missing = ui.TextInput(label="足りない人数", placeholder="例: 2", required=True)
        self.add_item(self.missing)

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            missing = int(str(self.missing.value))
        except ValueError:
            await interaction.response.send_message("人数は整数で入力してください", ephemeral=True)
            return

        await send_call_dm(interaction, self.owner_id, self.game, missing)


class OtherGameModal(ui.Modal):
    def __init__(self, owner_id: int) -> None:
        super().__init__(title="募集内容入力", timeout=300)
        self.owner_id = owner_id
        self.game = ui.TextInput(label="ゲーム名", placeholder="ゲーム名", required=True)
        self.missing = ui.TextInput(label="足りない人数", placeholder="例: 2", required=True)
        self.add_item(self.game)
        self.add_item(self.missing)

    async def on_submit(self, interaction: Interaction) -> None:
        try:
            missing = int(str(self.missing.value))
        except ValueError:
            await interaction.response.send_message("人数は整数で入力してください", ephemeral=True)
            return

        await send_call_dm(interaction, self.owner_id, str(self.game.value), missing)


class CallSetupView(ui.View):
    """call ボタンを押した際にゲーム選択を行う View"""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id

    @ui.select(placeholder="ゲームを選択", options=[
        discord.SelectOption(label="valorant"),
        discord.SelectOption(label="APEX"),
        discord.SelectOption(label="その他"),
    ])
    async def select_game(self, interaction: Interaction, select: ui.Select) -> None:
        choice = select.values[0]
        if choice == "その他":
            await interaction.response.send_modal(OtherGameModal(self.owner_id))
        else:
            await interaction.response.send_modal(MissingNumberModal(self.owner_id, choice))


async def send_call_dm(interaction: Interaction, owner_id: int, game: str, missing: int) -> None:
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("サーバー内で使用してください", ephemeral=True)
        return

    owner = interaction.client.get_user(owner_id)
    if owner is None:
        owner = interaction.user

    msg = f"{owner.display_name} さんが {game} を一緒に遊ぶ人を募集しています。残り {missing} 人です。参加しますか？"
    sent = 0
    for m in guild.members:
        if m.bot or m.id == owner.id:
            continue
        if m.status == discord.Status.offline or m.voice:
            continue
        try:
            await m.send(msg, view=CallResponseView(owner.id))
            sent += 1
        except Exception:
            pass

    await interaction.response.send_message(f"{sent}人に募集を送信しました。", ephemeral=True)


class MainButtons(ui.View):
    """tracker と call ボタンを提供する View"""

    def __init__(self) -> None:
        super().__init__(timeout=120)

    @ui.button(label="tracker", style=ButtonStyle.primary, emoji="📊")
    async def tracker_btn(self, interaction: Interaction, _button: ui.Button) -> None:
        await interaction.response.send_modal(TrackerModal())

    @ui.button(label="call", style=ButtonStyle.success, emoji="📢")
    async def call_btn(self, interaction: Interaction, _button: ui.Button) -> None:
        await interaction.response.send_message(
            "募集するゲームを選択してください",
            view=CallSetupView(interaction.user.id),
            ephemeral=True,
        )
