import asyncio
import os
from typing import Any, TypedDict, cast

import discord
import yt_dlp
from discord.ext import commands


class YtdlOptions(TypedDict):
    format: str
    noplaylist: bool
    quiet: bool
    default_search: str
    extractor_args: dict[str, dict[str, list[str]]]


class FFmpegOptions(TypedDict):
    before_options: str
    options: str


YTDL_OPTS: YtdlOptions = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "default_search": "ytsearch",  # lets !play accept plain search terms too
    # The default YouTube clients return googlevideo URLs that ffmpeg gets a
    # 403 for. The visionos client returns an audio-only Opus stream that
    # ffmpeg can read directly.
    "extractor_args": {"youtube": {"player_client": ["visionos"]}},
}
FFMPEG_OPTS: FFmpegOptions = {
    "before_options": "-nostdin -reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}

intents = discord.Intents.default()
intents.message_content = True  # also enable "Message Content Intent" in the dev portal
bot = commands.Bot(command_prefix="!", intents=intents)

queues: dict[int, list[str]] = {}  # guild_id -> pending links/queries
disconnect_tasks: dict[int, asyncio.Task[None]] = {}
DISCONNECT_DELAY = 10.0


def cancel_disconnect(guild_id: int) -> None:
    task = disconnect_tasks.pop(guild_id, None)
    if task is not None:
        task.cancel()


def schedule_disconnect(guild_id: int, vc: discord.VoiceClient) -> None:
    cancel_disconnect(guild_id)

    async def disconnect_when_idle() -> None:
        current = asyncio.current_task()
        try:
            await asyncio.sleep(DISCONNECT_DELAY)

            if vc.is_playing() or queues.get(guild_id):
                return

            if vc.is_connected():
                try:
                    await vc.disconnect()
                    queues.pop(guild_id, None)
                except Exception as e:
                    print(f"Failed to disconnect idle voice client: {type(e).__name__}: {e}")
        except asyncio.CancelledError:
            pass
        finally:
            if disconnect_tasks.get(guild_id) is current:
                disconnect_tasks.pop(guild_id, None)

    disconnect_tasks[guild_id] = asyncio.create_task(disconnect_when_idle())


def extract(query: str) -> tuple[str, str]:
    """Blocking call: resolve a link/search to (title, direct stream URL)."""
    # yt-dlp's params type is an exhaustive private TypedDict, so cast our
    # partial options dict (still checked against YtdlOptions above).
    with yt_dlp.YoutubeDL(cast(Any, YTDL_OPTS)) as ydl:
        info = ydl.extract_info(query, download=False)
    if info is None:
        raise RuntimeError("No results found")

    entries = info.get("entries")
    if entries:
        entry = next(iter(entries), None)
        if entry is None:
            raise RuntimeError("No results found")
        info = entry

    title = info.get("title") or query
    url = info.get("url")
    if not url:
        raise RuntimeError(f"No playable stream for {title!r}")
    return title, url


def get_voice_client(ctx: commands.Context) -> discord.VoiceClient | None:
    """ctx.voice_client is a VoiceProtocol, so narrow it to the concrete client."""
    vc = ctx.voice_client
    return vc if isinstance(vc, discord.VoiceClient) else None


async def play_next(ctx: commands.Context) -> None:
    guild = ctx.guild
    vc = get_voice_client(ctx)
    if guild is None or vc is None:
        return

    queue = queues.get(guild.id)
    if not queue:
        schedule_disconnect(guild.id, vc)
        return

    cancel_disconnect(guild.id)
    loop = asyncio.get_running_loop()

    def after(error: Exception | None) -> None:
        if error:
            print(f"Player error: {type(error).__name__}: {error}")
        asyncio.run_coroutine_threadsafe(play_next(ctx), ctx.bot.loop)

    # Loop instead of recursing: a run of bad tracks shouldn't grow the stack.
    while queue:
        query = queue.pop(0)
        try:
            title, stream_url = await loop.run_in_executor(None, extract, query)
            # Opus output avoids Python's libopus dependency entirely.
            source = discord.FFmpegOpusAudio(stream_url, **FFMPEG_OPTS)
            try:
                vc.play(source, after=after)
            except Exception:
                source.cleanup()
                raise
        except Exception as e:
            print(f"Failed to load {query!r}: {type(e).__name__}: {e}")
            details = str(e) or type(e).__name__
            await ctx.send(f"Couldn't load `{query}`: {details}")
            continue

        await ctx.send(f"Now playing: **{title}**")
        return

    schedule_disconnect(guild.id, vc)


@bot.command()
async def play(ctx: commands.Context, *, query: str) -> None:
    guild = ctx.guild
    if guild is None:
        await ctx.send("This command only works in servers.")
        return

    author = ctx.author
    if (
        not isinstance(author, discord.Member)
        or author.voice is None
        or author.voice.channel is None
    ):
        await ctx.send("Join a voice channel first.")
        return

    vc = get_voice_client(ctx)
    if vc is None:
        vc = await author.voice.channel.connect()

    cancel_disconnect(guild.id)
    queues.setdefault(guild.id, []).append(query)

    if vc.is_playing() or vc.is_paused():
        await ctx.send("Added to the queue.")
    else:
        await play_next(ctx)


@bot.command()
async def skip(ctx: commands.Context) -> None:
    vc = get_voice_client(ctx)
    if vc and vc.is_playing():
        vc.stop()  # triggers `after`, which starts the next track


@bot.command()
async def stop(ctx: commands.Context) -> None:
    guild = ctx.guild
    if guild is None:
        return
    cancel_disconnect(guild.id)
    queues[guild.id] = []
    vc = get_voice_client(ctx)
    if vc:
        await vc.disconnect()


@bot.event
async def on_ready() -> None:
    print(f"Logged in as {bot.user}")


def main() -> None:
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        raise SystemExit(
            "DISCORD_TOKEN is not set. Run with `uv run --env-file .env python bot.py`."
        )
    bot.run(token)


if __name__ == "__main__":
    main()
