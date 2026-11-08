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
  GUILD_ID        (optional) server ID(s), comma-separated; makes slash commands appear instantly
  YTDLP_COOKIES   (optional) contents of a cookies.txt file, only if YouTube blocks the host
"""

import asyncio
import logging
import os
import glob
from collections import deque
from dataclasses import dataclass, field

import discord
import yt_dlp
from discord import app_commands

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("musicbot")

# Discord voice needs the Opus encoder; load it explicitly so it can't silently go missing.
if not discord.opus.is_loaded():
    for name in ("libopus.so.0", "libopus.so", "opus"):
        try:
            discord.opus.load_opus(name)
            break
        except OSError:
            continue
log.info("Opus loaded: %s", discord.opus.is_loaded())

TOKEN = os.environ["DISCORD_TOKEN"]
GUILD_ID = os.getenv("GUILD_ID")
IDLE_DISCONNECT_SECONDS = 60
DOWNLOAD_TIMEOUT = 180  # give up on a song if YouTube takes longer than this to deliver it
DOWNLOAD_DIR = "/tmp/audio"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
YDL_OPTS = {
    "format": "ba/ba*",  # best audio-only, else best format that has audio
    "noplaylist": True,
    "quiet": True,
    "no_warnings": False,  # warnings go to the Railway logs
    "socket_timeout": 20,
    "noprogress": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
}

def write_cookie_file(raw: str, path: str) -> int:
    """Save pasted cookies.txt contents, repairing tabs that got turned into spaces."""
    lines = ["# Netscape HTTP Cookie File"]
    count = 0
    for line in raw.replace("\r", "").split("\n"):
        line = line.strip()
        if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) < 7:
            parts = line.split(None, 6)
        if len(parts) == 7:
            lines.append("\t".join(parts))
            count += 1
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return count


cookie_path = None
if os.getenv("YTDLP_COOKIES"):
    cookie_path = "/tmp/cookies.txt"
    n = write_cookie_file(os.environ["YTDLP_COOKIES"], cookie_path)
    YDL_OPTS["cookiefile"] = cookie_path
    log.info("Loaded %d YouTube cookies from YTDLP_COOKIES", n)


async def extract(query: str) -> dict:
    """Look up a URL or search term with yt-dlp, without downloading."""

    def run():
        # The lookup only needs the title, so don't fail it over format availability.
        with yt_dlp.YoutubeDL(dict(YDL_OPTS, ignore_no_formats_error=True)) as ydl:
            info = ydl.extract_info(query, download=False)
            if info and "entries" in info:
                entries = [e for e in info["entries"] if e]
                if not entries:
                    raise RuntimeError("No results found.")
                info = entries[0]
            return info

    return await asyncio.get_running_loop().run_in_executor(None, run)


async def download(url: str) -> str:
    """Download a track's audio to a local file and return its path."""

    def run():
        opts = dict(YDL_OPTS, outtmpl=f"{DOWNLOAD_DIR}/%(id)s.%(ext)s")
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if info and "entries" in info:
                info = next(e for e in info["entries"] if e)
            path = ydl.prepare_filename(info)
        if not os.path.exists(path):
            matches = glob.glob(os.path.splitext(path)[0] + ".*")
            if not matches:
                raise RuntimeError("Download finished but no audio file was found.")
            path = matches[0]
        return path

    fut = asyncio.get_running_loop().run_in_executor(None, run)
    try:
        return await asyncio.wait_for(fut, DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        raise RuntimeError(f"YouTube took over {DOWNLOAD_TIMEOUT} seconds to send it, giving up.")


def remove_file(path: str | None):
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


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
    download_task: asyncio.Task | None = field(default=None, repr=False)

    def start_download(self) -> asyncio.Task:
        if self.download_task is None:
            self.download_task = asyncio.create_task(download(self.page_url))
        return self.download_task


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
        self.current = track  # marks the player busy while the song downloads
        try:
            path = await track.start_download()
        except Exception as e:
            log.exception("Failed to download %s", track.page_url)
            await self.announce(f"Couldn't play **{track.title}**: {e}")
            await self.play_next()
            return

        vc = self.guild.voice_client
        if not vc or not vc.is_connected():
            remove_file(path)
            self.current = None
            return

        source = discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(path, options="-vn"), volume=self.volume)

        def after(err):
            if err:
                log.error("Playback error: %s", err)
            remove_file(path)
            asyncio.run_coroutine_threadsafe(self.play_next(), self.bot.loop)

        vc.play(source, after=after)
        self.prefetch()
        await self.announce(f"Now playing: **{track.title}** ({fmt_duration(track.duration)}), requested by {track.requester}")

    def prefetch(self):
        """Start downloading the next song while the current one plays."""
        if self.queue:
            self.queue[0].start_download()

    def clear(self):
        for t in self.queue:
            if t.download_task:
                t.download_task.add_done_callback(
                    lambda task: remove_file(task.result()) if not task.cancelled() and not task.exception() else None
                )
        self.queue.clear()

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
            # GUILD_ID can be one server ID or several separated by commas.
            for gid in [g.strip() for g in GUILD_ID.split(",") if g.strip()]:
                guild = discord.Object(id=int(gid))
                self.tree.copy_global_to(guild=guild)
                try:
                    await self.tree.sync(guild=guild)
                    log.info("Slash commands synced to server %s", gid)
                except discord.HTTPException as e:
                    log.error("Couldn't sync commands to server %s (is the bot in it?): %s", gid, e)
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
            p.clear()
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
    )
    p.queue.append(track)

    if not vc.is_playing() and not vc.is_paused() and p.current is None:
        await interaction.followup.send(f"Downloading **{track.title}**, starting in a few seconds...")
        await p.play_next()
    else:
        p.prefetch()
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
    p.clear()
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
        bot.player(interaction.guild).clear()
        await vc.disconnect()
        await interaction.response.send_message("Bye!")
    else:
        await interaction.response.send_message("I'm not in a voice channel.", ephemeral=True)


if __name__ == "__main__":
    bot.run(TOKEN, log_handler=None)
