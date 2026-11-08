"""
Discord music bot: plays YouTube audio in a voice channel.

Slash commands:
  /play <url or search>   join your voice channel and play (or queue) a track
  /skip                   skip the current track
  /pause, /resume         pause or resume playback
  /stop                   stop and clear the queue
  /queue                  show what's playing and what's next
  /volume <0-100>         set playback volume
  /leave                  disconnect from voice

Environment variables:
  DISCORD_TOKEN   (required) your bot token
  GUILD_ID        (optional) your server ID, makes slash commands appear instantly
  YTDLP_COOKIES   (optional) contents of a cookies.txt file, only if YouTube blocks the host
"""

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("musicbot")

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("GUILD_ID")
IDLE_DISCONNECT_SECONDS = 60
STREAM_URL_MAX_AGE = 300  # YouTube stream links expire, so refetch if older than this

YDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

if os.getenv("YTDLP_COOKIES"):
    cookie_path = "/tmp/cookies.txt"
    with open(cookie_path, "w", encoding="utf-8") as f:
        f.write(os.environ["YTDLP_COOKIES"])
    YDL_OPTS["cookiefile"] = cookie_path
    log.info("Using YouTube cookies from YTDLP_COOKIES")

FFMPEG_OPTS = {
    "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
    "options": "-vn",
}


async def extract(query: str) -> dict:
    """Look up a URL or search term with yt-dlp, without downloading."""

    def run():
        with yt_dlp.YoutubeDL(YDL_OPTS) as ydl:
            info = ydl.extract_info(query, download=False)
            if info and "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise RuntimeError("No results found.")
                info = entries[0]
            return info

    return await asyncio.get_running_loop().run_in_executor(None, run)


def fmt_duration(seconds) -> str:
    if not seconds:
        return "live"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


@dataclass
class Track:
    title: str
    page_url: str
    duration: int | None
    requester: str
    stream_url: str | None = None
    fetched_at: float = field(default_factory=time.time)


class GuildPlayer:
    def __init__(self, bot: "MusicBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.current: Track | None = None
        self.volume = 0.5
        self.text_channel: discord.abc.Messageable | None = None

    async def announce(self, msg: str):
        if self.text_channel:
            try:
                await self.text_channel.send(msg)
            except discord.HTTPException:
                pass

    async def play_next(self):
        vc = self.guild.voice_client
        if not vc or not vc.is_connected():
            self.current = None
            return
        if not self.queue:
            self.current = None
            return

        track = self.queue.popleft()
        try:
            if not track.stream_url or time.time() - track.fetched_at > STREAM_URL_MAX_AGE:
                info = await extract(track.page_url)
                track.stream_url = info["url"]
                track.fetched_at = time.time()
            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track.stream_url, **FFMPEG_OPTS), volume=self.volume
            )
        except Exception as e:
            log.exception("Failed to load track")
            await self.announce(f"Couldn't play **{track.title}**: {e}")
            await self.play_next()
            return

        def after(err):
            if err:
                log.error("Playback error: %s", err)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        self.current = track
        vc.play(source, after=after)
        await self.announce(f"Now playing: **{track.title}** ({fmt_duration(track.duration)}), requested by {track.requester}")


class MusicBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)
        self.tree = app_commands.CommandTree(self)
        self.players: dict[int, GuildPlayer] = {}

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def setup_hook(self):
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Slash commands synced to server %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Slash commands synced globally (can take up to an hour to appear)")

    async def on_ready(self):
        log.info("Logged in as %s", self.user)

    async def on_voice_state_update(self, member, before, after):
        # Leave if everyone else leaves the channel.
        vc = member.guild.voice_client
        if not vc or not vc.channel:
            return
        if any(not m.bot for m in vc.channel.members):
            return
        await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
        vc = member.guild.voice_client
        if vc and vc.channel and not any(not m.bot for m in vc.channel.members):
            p = self.player(member.guild)
            p.queue.clear()
            await vc.disconnect()
            await p.announce("Everyone left, so I disconnected.")


bot = MusicBot()


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    user_voice = getattr(interaction.user, "voice", None)
    if not user_voice or not user_voice.channel:
        await interaction.followup.send("Join a voice channel first.", ephemeral=True)
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        return await user_voice.channel.connect(self_deaf=True)
    if vc.channel != user_voice.channel:
        await vc.move_to(user_voice.channel)
    return vc


@bot.tree.command(description="Play a YouTube URL or search term")
@app_commands.describe(query="YouTube link or what to search for")
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    vc = await ensure_voice(interaction)
    if not vc:
        return
    p = bot.player(interaction.guild)
    p.text_channel = interaction.channel

    try:
        info = await extract(query)
    except Exception as e:
        log.exception("Lookup failed")
        await interaction.followup.send(f"Couldn't find that: {e}")
        return

    track = Track(
        title=info.get("title", "Unknown"),
        page_url=info.get("webpage_url") or query,
        duration=info.get("duration"),
        requester=interaction.user.display_name,
        stream_url=info.get("url"),
    )
    p.queue.append(track)

    if not vc.is_playing() and not vc.is_paused():
        await interaction.followup.send(f"Loading **{track.title}**...")
        await p.play_next()
    else:
        await interaction.followup.send(f"Queued **{track.title}** (position {len(p.queue)})")


@bot.tree.command(description="Skip the current track")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and (vc.is_playing() or vc.is_paused()):
        vc.stop()  # triggers the next track
        await interaction.response.send_message("Skipped.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Pause playback")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("Paused.")
    else:
        await interaction.response.send_message("Nothing is playing.", ephemeral=True)


@bot.tree.command(description="Resume playback")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("Resumed.")
    else:
        await interaction.response.send_message("Nothing is paused.", ephemeral=True)


@bot.tree.command(description="Stop playback and clear the queue")
async def stop(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    p.queue.clear()
    vc = interaction.guild.voice_client
    if vc:
        vc.stop()
    await interaction.response.send_message("Stopped and cleared the queue.")


@bot.tree.command(name="queue", description="Show the current queue")
async def show_queue(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    lines = []
    if p.current:
        lines.append(f"**Now:** {p.current.title} ({fmt_duration(p.current.duration)})")
    for i, t in enumerate(list(p.queue)[:15], start=1):
        lines.append(f"{i}. {t.title} ({fmt_duration(t.duration)}), from {t.requester}")
    if len(p.queue) > 15:
        lines.append(f"...and {len(p.queue) - 15} more")
    await interaction.response.send_message("\n".join(lines) or "The queue is empty.")


@bot.tree.command(description="Set volume (0 to 100)")
@app_commands.describe(level="Volume from 0 to 100")
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    p = bot.player(interaction.guild)
    p.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    await interaction.response.send_message(f"Volume set to {level}%.")


@bot.tree.command(description="Disconnect from voice")
async def leave(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc:
        bot.player(interaction.guild).queue.clear()
        await vc.disconnect()
        await interaction.response.send_message("Bye!")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
