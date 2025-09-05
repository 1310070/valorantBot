import discord
from discord.ext import commands
from discord import app_commands, Interaction

from ..views.buttons import CallSetupView, TrackerModal


class UICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="call", description="募集DMを送信")
    async def call_command(self, interaction: Interaction):
        await interaction.response.send_message(
            "募集するゲームを選択してください",
            view=CallSetupView(interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="profile", description="tracker.gg のプロフィールURLを生成")
    async def profile_command(self, interaction: Interaction):
        await interaction.response.send_modal(TrackerModal())


async def setup(bot: commands.Bot):
    await bot.add_cog(UICog(bot))

