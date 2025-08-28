import asyncio
import discord
from discord import ui, ButtonStyle, Interaction
from typing import Optional

# services/profile_service.py からURLビルダーをインポート
try:
    from services.profile_service import build_tracker_url
except ModuleNotFoundError as e:
    # 実行場所のズレ対策（/views から一階層上＝プロジェクトルートをパスに追加）
    import sys, os
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from services.profile_service import build_tracker_url  # 再挑戦

try:
    from services.get_store import get_daily_store_text
except ModuleNotFoundError:
    import sys, os
    sys.path.append(os.path.dirname(os.path.dirname(__file__)))
    from services.get_store import get_daily_store_text  # 再挑戦


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


class CallMessageModal(ui.Modal):
    """募集DMからのメッセージ送信用モーダル"""

    def __init__(self, owner_id: int, choice: str) -> None:
        super().__init__(title="メッセージ入力", timeout=300)
        self.owner_id = owner_id
        self.choice = choice
        self.message = ui.TextInput(
            label="メッセージ",
            placeholder="任意",
            required=False,
            max_length=200,
        )
        self.add_item(self.message)

    async def on_submit(self, interaction: Interaction) -> None:
        owner = interaction.client.get_user(self.owner_id)
        if owner:
            embed = discord.Embed(title="募集返信")
            embed.add_field(name="ユーザー", value=interaction.user.display_name, inline=False)
            embed.add_field(name="参加可否", value=self.choice, inline=False)
            embed.add_field(name="メッセージ", value=self.message.value or "(なし)", inline=False)
            try:
                await owner.send(embed=embed)
            except Exception:
                pass
        await interaction.response.send_message("送信しました。", ephemeral=True)


class CallResponseView(ui.View):
    """募集DM内での参加可否選択とメッセージ送信用 View"""

    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.choice: Optional[str] = None

    @ui.select(
        placeholder="参加可否を選択",
        options=[
            discord.SelectOption(label="参加", value="参加"),
            discord.SelectOption(label="不参加", value="不参加"),
        ],
    )
    async def choose(self, interaction: Interaction, select: ui.Select) -> None:
        self.choice = select.values[0]
        await interaction.response.send_message(f"{self.choice} を選択しました。メッセージを入力してください。", ephemeral=True)

    @ui.button(label="送信", style=ButtonStyle.primary)
    async def send(self, interaction: Interaction, _button: ui.Button) -> None:
        if not self.choice:
            await interaction.response.send_message("参加/不参加を選択してください。", ephemeral=True)
            return
        await interaction.response.send_modal(CallMessageModal(self.owner_id, self.choice))


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

        if missing == 0:
            await interaction.response.send_message("0 人は指定できません", ephemeral=True)
            return

        await interaction.response.send_message(
            "送信対象を選択してください",
            view=SendOptionView(self.owner_id, self.game, missing),
            ephemeral=True,
        )


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

        if missing == 0:
            await interaction.response.send_message("0 人は指定できません", ephemeral=True)
            return

        await interaction.response.send_message(
            "送信対象を選択してください",
            view=SendOptionView(self.owner_id, str(self.game.value), missing),
            ephemeral=True,
        )


class SendOptionView(ui.View):
    """募集DM送信先のオンライン/オフラインを選択する View"""

    def __init__(self, owner_id: int, game: str, missing: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.game = game
        self.missing = missing

    @ui.button(label="オンライン", style=ButtonStyle.success)
    async def send_online(self, interaction: Interaction, _button: ui.Button) -> None:
        await send_call_dm(interaction, self.owner_id, self.game, self.missing, online=True)

    @ui.button(label="オフライン", style=ButtonStyle.secondary)
    async def send_offline(self, interaction: Interaction, _button: ui.Button) -> None:
        await send_call_dm(interaction, self.owner_id, self.game, self.missing, online=False)


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


async def send_call_dm(
    interaction: Interaction,
    owner_id: int,
    game: str,
    missing: int,
    *,
    online: bool,
) -> None:
    guild = interaction.guild
    if not guild:
        await interaction.response.send_message("サーバー内で使用してください", ephemeral=True)
        return

    owner = interaction.client.get_user(owner_id) or interaction.user

    embed = discord.Embed(
        title="募集のお知らせ",
        description=f"{owner.display_name} さんが募集しています。",
    )
    embed.add_field(name="ゲーム", value=game, inline=False)
    embed.add_field(name="必要人数", value=str(missing), inline=False)

    recipients: list[discord.Member] = []
    for m in guild.members:
        if m.bot or m.id == owner.id:
            continue
        if m.voice:
            continue
        if online:
            if m.status == discord.Status.offline:
                continue
        else:
            if m.status != discord.Status.offline:
                continue
        try:
            await m.send(embed=embed, view=CallResponseView(owner.id))
            recipients.append(m)
        except Exception:
            pass

    names = ", ".join(m.display_name for m in recipients) or "なし"
    summary = discord.Embed(title="募集DM送信結果", description=f"{len(recipients)}人に募集を送信しました。")
    summary.add_field(name="送信者", value=owner.display_name, inline=False)
    summary.add_field(name="送信先", value=names, inline=False)
    await interaction.response.send_message(embed=summary, ephemeral=True)


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

    @ui.button(label="getStore", style=ButtonStyle.secondary, emoji="🛒")
    async def store_btn(self, interaction: Interaction, _button: ui.Button) -> None:
        # 即座に応答を確保しないとインタラクションが無効になってしまうため defer を使用
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await asyncio.to_thread(get_daily_store_text)
        except Exception as e:
            await interaction.followup.send(f"取得に失敗しました: {e}", ephemeral=True)
            return
        await interaction.followup.send(text, ephemeral=True)
