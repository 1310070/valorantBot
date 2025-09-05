import os
import threading

import uvicorn
from dotenv import load_dotenv
import discord
from discord.ext import commands

# bot.py
from valorantBot2.rec import app as rec_app


load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True  # 起動時の送信先探索で使用
intents.members = True
intents.presences = True
intents.voice_states = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 起動時案内の重複送信防止フラグ
bot._announced = False  # type: ignore[attr-defined]


def run_api_server() -> None:
    """Start the FastAPI server in a background thread."""
    port = int(os.getenv("PORT", "8190"))
    uvicorn.run(rec_app, host="0.0.0.0", port=port, log_level="info")


# Launch the API server so that the container exposes the endpoint while the
# Discord bot runs. The thread is marked as daemon so it will not block
# shutdown.
threading.Thread(target=run_api_server, daemon=True).start()


def build_startup_text() -> str:
    # 必要ならここで動的に増やせます
    return (
        "**利用可能なコマンド（testing now）**\n"
        "- `/call` … 募集DMを送信\n"
        "- `/profile` … あなたのトラッカーURLを生成\n"
        "- `/store` … VALORANT のストアを表示"
    )


def pick_startup_channel(guild: discord.Guild) -> discord.abc.Messageable | None:
    """ギルドごとに案内送信先チャンネルを決定:
    1) 環境変数 STARTUP_CHANNEL_ID がそのギルドのチャンネルならそこ
    2) ギルドの system channel
    3) 最初に送信可能なテキストチャンネル
    """
    # 1) 固定チャンネルID
    scid = os.getenv("STARTUP_CHANNEL_ID")
    if scid and scid.isdigit():
        ch = bot.get_channel(int(scid))
        if isinstance(ch, discord.abc.Messageable) and getattr(ch, "guild", None) == guild:
            return ch

    # 2) ギルドのシステムチャンネル
    if guild.system_channel:
        perms = guild.system_channel.permissions_for(guild.me) if guild.me else None
        if perms and perms.send_messages and perms.view_channel:
            return guild.system_channel

    # 3) 最初に送信可能なテキストチャンネル
    for ch in guild.text_channels:
        perms = ch.permissions_for(guild.me) if guild.me else None
        if perms and perms.send_messages and perms.view_channel:
            return ch

    return None


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} / bot is ready")

    # 起動時に一度だけ案内送信
    if not getattr(bot, "_announced", False):  # type: ignore[attr-defined]
        bot._announced = True  # type: ignore[attr-defined]
        try:
            text = build_startup_text()
            missing: list[str] = []
            for g in bot.guilds:
                channel = pick_startup_channel(g)
                if channel:
                    await channel.send(text)
                else:
                    missing.append(g.name)
            if missing:
                # 送信先が見つからないギルドがあればオーナーにDM
                app_info = await bot.application_info()
                try:
                    await app_info.owner.send(
                        "⚠️ 起動案内を送るチャンネルが見つかりませんでした: "
                        + ", ".join(missing)
                    )
                except Exception:
                    pass
        except Exception as e:
            print("起動時コマンド一覧送信に失敗:", e)


async def setup_hook():
    # cogs/ui をロード
    await bot.load_extension("valorantBot2.cogs.ui")
    # スラッシュコマンドを同期
    await bot.tree.sync()


bot.setup_hook = setup_hook

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が .env に設定されていません。")

bot.run(TOKEN)


