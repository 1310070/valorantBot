import asyncio
from discord.ext import commands
from discord import app_commands, Interaction

from views.buttons import MainButtons
from services.get_store import getStore


class UICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="button", description="ボタンを表示")
    async def show_buttons(self, interaction: Interaction):
        await interaction.response.send_message("ボタンをどうぞ", view=MainButtons())

    @app_commands.command(name="store", description="今日のストア情報を表示")
    async def store_command(self, interaction: Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            text = await asyncio.to_thread(getStore, str(interaction.user.id))
        except Exception as e:
            await interaction.followup.send(f"取得に失敗しました: {e}", ephemeral=True)
            return
        await interaction.followup.send(text, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(UICog(bot))

