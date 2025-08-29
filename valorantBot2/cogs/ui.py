import asyncio
import io
from datetime import datetime, timezone

import requests
import discord
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
            items = await asyncio.to_thread(getStore, interaction.user.id)
        except Exception as e:
            await interaction.followup.send(f"取得に失敗しました: {e}", ephemeral=True)
            return
        date_str = datetime.now(timezone.utc).strftime("%Y/%m/%d")

        embeds: list[discord.Embed] = []
        files: list[discord.File] = []
        for i, item in enumerate(items, 1):
            embed = discord.Embed(title=item["name"], description=f"{item['cost']} VP")
            if item["image"]:
                resp = requests.get(item["image"], timeout=10)
                resp.raise_for_status()
                filename = f"img{i}.png"
                file = discord.File(io.BytesIO(resp.content), filename=filename)
                files.append(file)
                embed.set_thumbnail(url=f"attachment://{filename}")
            embeds.append(embed)

        await interaction.followup.send(
            content=f"{date_str}のストアオファー", embeds=embeds, files=files, ephemeral=True
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(UICog(bot))

