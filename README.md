# YouTube Music Bot for Discord

Plays YouTube audio in your voice channel with slash commands. Runs in the cloud, so nothing gets installed on your Windows PC.

## Commands

| Command | What it does |
|---|---|
| `/play <link or search>` | Joins your voice channel and plays, or adds to the queue |
| `/skip` | Skips the current track |
| `/pause` / `/resume` | Pauses or resumes |
| `/stop` | Stops and clears the queue |
| `/queue` | Shows what's playing and what's next |
| `/volume <0-100>` | Sets volume |
| `/leave` | Disconnects |

The bot also leaves on its own a minute after everyone else leaves the channel.

## Setup (all in your browser, about 15 minutes)

### 1. Create the bot in Discord

1. Go to https://discord.com/developers/applications and click **New Application**. Name it.
2. Open the **Bot** tab, click **Reset Token**, and copy the token. Keep it secret; anyone with it controls your bot.
3. Open **OAuth2 > URL Generator**:
   - Scopes: `bot` and `applications.commands`
   - Bot permissions: View Channels, Send Messages, Connect, Speak
4. Open the generated URL, pick your server, and authorize.
5. Get your server ID (optional but recommended, it makes commands show up instantly): in Discord, User Settings > Advanced > turn on **Developer Mode**, then right-click your server icon > **Copy Server ID**.

### 2. Put the code on GitHub

1. Create a new **private** repository at https://github.com/new.
2. Click **uploading an existing file** and drag in `bot.py`, `requirements.txt`, `Dockerfile` and this README. Commit.

### 3. Host it on Railway

1. Sign in at https://railway.com with your GitHub account.
2. **New Project > Deploy from GitHub repo** and pick the repo. Railway sees the Dockerfile and builds it.
3. In the service's **Variables** tab add:
   - `DISCORD_TOKEN` = the token from step 1
   - `GUILD_ID` = your server ID
4. It redeploys automatically. When the **Deploy Logs** show `Logged in as ...`, hop in a voice channel and try `/play lofi hip hop`.

Railway starts with a free trial, then its lowest paid tier is a few dollars a month; check their current pricing. Any host that runs a Dockerfile (Fly.io, Render background workers, a small VPS) works the same way.

## Keeping it working

- **YouTube changes break playback every so often.** The fix is almost always a newer yt-dlp. On Railway, click **Redeploy**; the build pulls the latest version automatically.
- **"Sign in to confirm you're not a bot" errors** mean YouTube is blocking the cloud server's IP. Fix: in Chrome, sign in to YouTube with a throwaway Google account (not your main one), export its cookies in Netscape `cookies.txt` format with a cookies.txt extension, and paste the whole file's contents into a Railway variable named `YTDLP_COOKIES`.
- **Bot joins but is silent:** check the Deploy Logs for errors, and make sure the bot has Connect and Speak permissions in that channel.

## How it works

- `discord.py` 2.7+ handles Discord, including the end-to-end encrypted voice that Discord has required since March 2026.
- `yt-dlp` finds the audio stream (no files are downloaded), and `ffmpeg` converts it for Discord.
