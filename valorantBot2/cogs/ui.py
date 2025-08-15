# cogs/ui.py
from discord.ext import commands
from discord import app_commands, Interaction

from views.buttons import MainButtons

class UICog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="button")
    async def show_buttons(self, ctx: commands.Context):
        await ctx.send("ボタンをどうぞ", view=MainButtons())

async def setup(bot: commands.Bot):
    await bot.add_cog(UICog(bot))
