import os
from dotenv import load_dotenv
import discord
from discord.ext import commands

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

intents = discord.Intents.default()
intents.guilds = True  # 起動時の送信先探索で使用
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# 起動時案内の重複送信防止フラグ
bot._announced = False  # type: ignore[attr-defined]


def build_startup_text() -> str:
    # 必要ならここで動的に増やせます
    return (
        "**利用可能なコマンド（testing now）**\n"
        "- `!button` … ボタンの一覧を表示\n"
        "- `tracker` … あなたのトラッカーURLを生成（表示されたボタンから入力して作成）"
    )


def pick_startup_channel() -> discord.abc.Messageable | None:
    """送信先チャンネルを決定:
    1) 環境変数 STARTUP_CHANNEL_ID があればそこ
    2) 最初のギルドの system channel
    3) 最初に送信可能なテキストチャンネル
    """
    # 1) 固定チャンネルID
    scid = os.getenv("STARTUP_CHANNEL_ID")
    if scid and scid.isdigit():
        ch = bot.get_channel(int(scid))
        if isinstance(ch, discord.abc.Messageable):
            return ch

    # 2) ギルドのシステムチャンネル
    for g in bot.guilds:
        if g.system_channel:
            perms = g.system_channel.permissions_for(g.me) if g.me else None
            if perms and perms.send_messages and perms.view_channel:
                return g.system_channel

    # 3) 最初に送信可能なテキストチャンネル
    for g in bot.guilds:
        for ch in g.text_channels:
            perms = ch.permissions_for(g.me) if g.me else None
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
            channel = pick_startup_channel()
            if channel:
                await channel.send(build_startup_text())
            else:
                # 送信先が見つからない場合はオーナーにDM
                app_info = await bot.application_info()
                try:
                    await app_info.owner.send(
                        "⚠️ 起動案内を送るチャンネルが見つかりませんでした。権限とチャンネル設定をご確認ください。"
                    )
                except Exception:
                    pass
        except Exception as e:
            print("起動時コマンド一覧送信に失敗:", e)


async def setup_hook():
    # cogs/ui をロード（!button で View を出す想定）
    await bot.load_extension("cogs.ui")


bot.setup_hook = setup_hook

if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN が .env に設定されていません。")

bot.run(TOKEN)
