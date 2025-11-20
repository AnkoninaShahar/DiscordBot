import os
import discord
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import yt_dlp
import asyncio

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

# GUILD_ID = 

async def search_ytdlp_async(query, ydl_opts):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: _extract(query, ydl_opts))

def _extract(query, ydl_opts):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(query, download=False)

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def On_ready():
    # test_guilf = discord.Object(id=GUILD_ID)
    await bot.tree.sync() # guild=test_guild
    print(f"{bot.user} is online")

@bot.event 
async def on_message(msg):
    print(msg.guild.id)

@bot.command(name="Play", description="Plays a song or add it to the queue.")
@app_commands.describe(song_query="Search query")
async def play(interaction:discord.Interaction, song_query: str):
    await interaction.response.defer()
    voice_channel = interaction.user.voice.channel

    if voice_channel is None:
        await interaction.followup.send("You must be in a voice channel.")
        return
    
    voice_client = interaction.guild.voice_client

    if voice_client is None:
        voice_client = await voice_channel.connect()
    elif voice_channel != voice_client.channel:
        await voice_client.move_to(voice_channel)

    ydl_options = {
        "format": "bestaudio[abr<=96]/bestaudio",
        "noplaylist": True,
        "youtube_include_dash_manifest": False,
        "youtube_include_hls_manifest": False,
    }

    query = "ytsearch1: " + song_query
    results = await search_ytdlp_async(query, ydl_options)
    tracks = results.get("entries", [])

    if tracks is None:
        await interaction.followup.send("No results found")
        return
    
    first_track = tracks[0]
    audio_url = first_track["url"]
    title = first_track.get("title", "Untitled")

    ffmpeg_options = {
        "befor_options": "-reconnect 1 -reconnect_streamed 1 -reconnect_delay max 5",
        "options": "-vn -c:a libopus -b:a 96k",
    }

    source = discord.FFmpegOpusAudio(audio_url, **ffmpeg_options, executable="bin\\ffmpeg\\ffmpeg.exe")

    voice_client.play(source)


bot.run(TOKEN)