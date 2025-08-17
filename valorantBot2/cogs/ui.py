--- a/cogs/ui.py
+++ b/cogs/ui.py
@@
-<<<<<<< codex/add-call-button-feature-eaf1oj
 from discord.ext import commands
 from discord import app_commands, Interaction
 
 from views.buttons import MainButtons
 
 
 class UICog(commands.Cog):
     def __init__(self, bot: commands.Bot):
         self.bot = bot
 
     @app_commands.command(name="button", description="ボタンを表示")
     async def show_buttons(self, interaction: Interaction):
         await interaction.response.send_message("ボタンをどうぞ", view=MainButtons())
-=======
-from discord.ext import commands
-from discord import app_commands, Interaction
-
-from views.buttons import MainButtons
-
-class UICog(commands.Cog):
-    def __init__(self, bot: commands.Bot):
-        self.bot = bot
-
-    @commands.command(name="button")
-    async def show_buttons(self, ctx: commands.Context):
-        await ctx.send("ボタンをどうぞ", view=MainButtons())
->>>>>>> main
@@
 async def setup(bot: commands.Bot):
     await bot.add_cog(UICog(bot))
