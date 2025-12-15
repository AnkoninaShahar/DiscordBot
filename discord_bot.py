import os
import logging  # Added so we can surface interaction/voice issues in the console.
import discord
from discord.abc import Connectable  # Gives us a protocol for any connectable voice target (stage/voice channel).
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio
from collections import deque

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

GUILD_ID = 820497690609713153

SONG_QUEUES = {}  # Per-guild FIFO queues so music continues even when multiple requests overlap.
PLAY_LOCKS = {}  # Per-guild asyncio locks to serialize queue/voice mutations and avoid race conditions.
VOICE_CHANNELS = {}  # Tracks the channel each guild last requested so reconnects know where to go.
LOGGER = logging.getLogger("discord_bot")  # Shared logger for interaction/voice diagnostics.


async def send_interaction_message(
    interaction: discord.Interaction,
    *,
    content: str,
    ephemeral: bool = False,
    force_followup: bool = False,
):
    """Send a response or followup depending on whether the interaction already has an initial response."""
    # `force_followup` lets callers skip the auto-detection when they know the interaction was already deferred.
    # Handles the Discord rule that an interaction can only be acknowledged once.
    use_followup = force_followup or interaction.response.is_done()
    try:
        use_followup = force_followup or interaction.response.is_done()
        if use_followup:
            await interaction.followup.send(content, ephemeral=ephemeral)
        else:
            await interaction.response.send_message(content, ephemeral=ephemeral)
    except discord.HTTPException as exc:
        if exc.code == 40060 and not use_followup:
            # Response already acknowledged elsewhere; fall back to followup.
            try:
                await interaction.followup.send(content, ephemeral=ephemeral)
            except discord.DiscordException as followup_exc:
                LOGGER.warning("Could not send followup (code=%s): %s", getattr(followup_exc, "code", "n/a"), followup_exc)
        else:
            LOGGER.warning("Failed to send interaction message (code=%s): %s", exc.code, exc)
    except discord.NotFound:
        LOGGER.warning("Interaction expired before a response could be sent.")


async def defer_interaction(interaction: discord.Interaction) -> bool:
    """Attempt to defer the interaction, returning False if it no longer exists."""
    # Centralizes defer logic so we can gracefully handle expired interactions.
    if interaction.response.is_done():
        return True
    try:
        await interaction.response.defer(thinking=True)
        return True
    except discord.NotFound:
        LOGGER.warning("Interaction expired before defer() could be called.")
        return False
    except discord.DiscordException as exc:
        LOGGER.error("Failed to defer interaction (code=%s): %s", getattr(exc, "code", "n/a"), exc)
        return False

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)


async def ensure_voice_client(guild: discord.Guild, target_channel: Connectable) -> discord.VoiceClient:
    """Connect or move the bot to the desired voice channel, retrying on transient voice gateway errors."""
    # Guarantees the bot is in the right channel before dequeueing audio, even after Discord drops the connection.
    if target_channel is None:
        raise RuntimeError("No target voice channel recorded for this guild.")

    voice_client = guild.voice_client
    if voice_client and voice_client.channel == target_channel:
        if voice_client.is_connected():
            return voice_client

        # Allow discord.py's internal reconnection logic a brief window to finish before forcing a reconnect.
        try:
            reconnect_wait = await asyncio.wait_for(
                asyncio.to_thread(voice_client.wait_until_connected, 5.0),
                timeout=6.0,
            )
        except Exception:
            reconnect_wait = False

        if reconnect_wait:
            return voice_client

    last_exception = None
    for attempt in range(3):
        # Retry a few times because Discord occasionally closes the voice gateway with code 4006 mid-handshake.
        try:
            voice_client = guild.voice_client
            if voice_client is None:
                voice_client = await target_channel.connect(reconnect=True)
            elif voice_client.channel != target_channel:
                await voice_client.move_to(target_channel, reconnect=True)
            elif not voice_client.is_connected():
                await voice_client.disconnect(force=True)
                voice_client = await target_channel.connect(reconnect=True)
            return voice_client
        except discord.errors.ConnectionClosed as exc:
            last_exception = exc
            LOGGER.warning(
                "Voice gateway closed with code %s while connecting to %s (attempt %s/3).",
                exc.code,
                target_channel.id,
                attempt + 1,
            )
            await asyncio.sleep(1)
        except discord.DiscordException as exc:
            last_exception = exc
            LOGGER.error("Voice connect/move failed on attempt %s: %s", attempt + 1, exc)
            await asyncio.sleep(1)

    if last_exception:
        raise last_exception
    raise RuntimeError("Unable to establish a voice connection.")

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    test_guild = discord.Object(id=GUILD_ID)
    await bot.tree.sync(guild=test_guild)
    print(f"{bot.user} is online")

# @bot.event 
# async def on_message(msg):
#     print(msg.guild.id)

@bot.tree.command(name="play", description="Plays audio or add it to the queue.")
@app_commands.describe(song_query="Search query")
async def play(interaction: discord.Interaction, song_query: str):
    try:
        interaction_deferred = False  # Tracks whether we already sent a defer so later replies use followups.
        voice_state = interaction.user.voice  # Capture the caller's voice channel for validation/reconnects.
        if voice_state is None or voice_state.channel is None:
            await send_interaction_message(interaction, content="You must be in a voice channel.", ephemeral=True)
            return

        interaction_deferred = await defer_interaction(interaction)
        if not interaction_deferred:
            # Interaction is no longer valid; nothing we can do.
            return

        ydl_options = {
            "format": "bestaudio[abr<=96]/bestaudio",
            "noplaylist": True,
            "youtube_include_dash_manifest": False,
            "youtube_include_hls_manifest": False,
        }

        query = "ytsearch1: " + song_query
        results = await search_ytdlp_async(query, ydl_options)
        tracks = results.get("entries", [])

        if not tracks:
            await send_interaction_message(
                interaction,
                content="No results found.",
                ephemeral=True,
                force_followup=interaction_deferred,
            )
            return

        first_track = tracks[0]
        audio_url = first_track["url"]
        title = first_track.get("title", "Untitled")

        guild_id = str(interaction.guild_id)
        lock = PLAY_LOCKS.setdefault(guild_id, asyncio.Lock())
        start_playback = False  # Indicates whether this request should start playback or just enqueue.
        voice_client = None
        VOICE_CHANNELS[guild_id] = voice_state.channel  # Remember the caller's channel for future reconnect attempts.
        async with lock:
            voice_client = await ensure_voice_client(interaction.guild, voice_state.channel)

            queue = SONG_QUEUES.setdefault(guild_id, deque())
            queue.append((audio_url, title))
            if not (voice_client.is_playing() or voice_client.is_paused()):
                start_playback = True

        if start_playback:
            await send_interaction_message(
                interaction,
                content=f"Now playing: **{title}**",
                force_followup=interaction_deferred,
            )
            await play_next_song(voice_client, guild_id, interaction.channel)
        else:
            await send_interaction_message(
                interaction,
                content=f"Added to queue: **{title}**",
                force_followup=interaction_deferred,
            )
    except Exception as e:
        await send_interaction_message(
            interaction,
            content=f"Error: {e}",
            ephemeral=True,
            force_followup=interaction.response.is_done(),
        )


@bot.tree.command(name="pause", description="Pauses the audio.")
async def pause(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_playing():
        voice_client.pause()
        await interaction.response.send_message("Pausing audio...")
    else:
        await interaction.response.send_message("No audio is playing")

@bot.tree.command(name="resume", description="Resumes the audio.")
async def resume(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and voice_client.is_paused():
        voice_client.resume()
        await interaction.response.send_message("Resuming audio...")
    else:
        await interaction.response.send_message("No audio is paused")

@bot.tree.command(name="skip", description="Skips the song currently playing.")
async def skip(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client and (voice_client.is_playing() or voice_client.is_paused()):
        voice_client.stop()
        await interaction.response.send_message("Skipped the current song")
    else:
        await interaction.response.send_message("No audio is playing")
        
@bot.tree.command(name="stop", description="Stops the bot.")
async def stop(interaction: discord.Interaction):
    voice_client = interaction.guild.voice_client
    if voice_client:
        voice_client.disconnect()
        SONG_QUEUES[str(interaction.guild_id)] = deque()
        await interaction.response.send_message("Stopping...")
    else:
        await interaction.response.send_message("There's nothing to stop")

async def play_next_song(voice_client, guild_id, channel):
    lock = PLAY_LOCKS.setdefault(guild_id, asyncio.Lock())
    async with lock:
        queue = SONG_QUEUES.setdefault(guild_id, deque())
        target_voice_channel = VOICE_CHANNELS.get(guild_id)
        guild = getattr(channel, "guild", None) or bot.get_guild(int(guild_id))
        if guild is None:
            LOGGER.warning("Cannot resolve guild from channel; aborting playback.")
            return
        if target_voice_channel is None:
            LOGGER.warning("No voice channel recorded for guild %s; aborting playback.", guild_id)
            return

        try:
            voice_client = await ensure_voice_client(guild, target_voice_channel)
        except Exception as exc:
            LOGGER.error("Unable to ensure voice client for guild %s: %s", guild_id, exc)
            return

        if voice_client.is_playing() or voice_client.is_paused():
            return

        if queue:
            audio_url, title = queue.popleft()

            ffmpeg_options = {
                "before_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                "options": "-vn -c:a libopus -b:a 96k",
            }

            source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="bin\\ffmpeg\\ffmpeg.exe")

            def after_play(error):
                if error:
                    print(f"Error playing {title}: {error}")
                asyncio.run_coroutine_threadsafe(play_next_song(voice_client, guild_id, channel), bot.loop)

            voice_client.play(source, after=after_play)
            asyncio.create_task(channel.send(f"Now playing: **{title}**"))
        else:
            if voice_client.is_connected():
                await voice_client.disconnect()
            SONG_QUEUES[guild_id] = deque()
            VOICE_CHANNELS.pop(guild_id, None)  # Drop stale channel data once the queue is empty.

bot.run(TOKEN)
