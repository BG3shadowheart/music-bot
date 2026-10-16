"""Direct Discord voice music player with no Lavalink dependency."""
from __future__ import annotations
import asyncio, logging, os
from collections import deque
from dataclasses import dataclass
import discord, yt_dlp
from discord.ext import commands

TOKEN=os.getenv("DISCORD_TOKEN", "")
PREFIX=os.getenv("BOT_PREFIX", ".")
DEFAULT_VOLUME=max(1,min(200,int(os.getenv("DEFAULT_VOLUME","120"))))
COOKIES=os.getenv("YTDLP_COOKIES_FILE", "").strip()

@dataclass
class Song:
    title:str; page:str; artist:str; duration:int; image:str|None=None

def opts(stream=False):
    value={"quiet":True,"no_warnings":True,"noplaylist":True,"source_address":"0.0.0.0"}
    if stream:value["format"]="bestaudio[acodec!=none]/bestaudio/best"
    if COOKIES:value["cookiefile"]=COOKIES
    return value

def lookup(query):
    target=query if query.startswith(("http://","https://")) else "ytsearch1:"+query
    with yt_dlp.YoutubeDL(opts()) as y: info=y.extract_info(target,download=False)
    if info.get("entries"):info=next(x for x in info["entries"] if x)
    return Song(info.get("title") or query,info.get("webpage_url") or info.get("original_url") or query,info.get("uploader") or info.get("channel") or "Unknown artist",info.get("duration") or 0,info.get("thumbnail"))
def stream(song):
    with yt_dlp.YoutubeDL(opts(True)) as y: info=y.extract_info(song.page,download=False)
    return info["url"]
def text(song,n=70):return discord.utils.escape_markdown(song.title)[:n]
def stamp(n):
    m,s=divmod(n,60);h,m=divmod(m,60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"

class Player:
 def __init__(self,gid):
  self.gid=gid;self.queue=deque();self.current=None;self.volume=DEFAULT_VOLUME;self.loop=False;self.manual_pause=False;self.room_pause=False;self.channel=None;self.message=None;self.lock=asyncio.Lock();self.generation=0
 def vc(self):return discord.utils.get(bot.voice_clients,guild__id=self.gid)
players={}
def player(guild):return players.setdefault(guild.id,Player(guild.id))
def humans(channel):return [x for x in channel.members if not x.bot]

intents=discord.Intents.default();intents.message_content=True;intents.voice_states=True
bot=commands.Bot(command_prefix=PREFIX,intents=intents,help_command=None)

def make_embed(p,message=None):
 e=discord.Embed(title="🌸 Miyazono Music • Anime Radio 🌸",colour=0x000000)
 if p.current:
  s=p.current;e.description=f"🎀 **Now playing**\n[{text(s)}]({s.page})";e.add_field(name="🌷 Artist",value=s.artist[:48]);e.add_field(name="⌛ Length",value=stamp(s.duration));e.add_field(name="🎧 Source",value="Direct audio")
  if s.image:e.set_thumbnail(url=s.image)
 else:e.description=message or "Use `.play <song or link>` to begin."
 if p.queue:e.add_field(name="💗 Up next",value="\n".join(f"{i}. {text(s,55)}" for i,s in enumerate(list(p.queue)[:5],1)),inline=False)
 state="Paused" if p.manual_pause or p.room_pause else "Playing" if p.current else "Ready"
 e.set_footer(text=f"{state} • Volume {p.volume}% • Loop {'on' if p.loop else 'off'} • 24/7 connected")
 return e

class Controls(discord.ui.View):
 def __init__(self,gid):super().__init__(timeout=None);self.gid=gid
 async def okay(self,i):
  p=players.get(self.gid);vc=p and p.vc();uv=getattr(i.user,"voice",None)
  if not p or not vc or not uv or uv.channel!=vc.channel:await i.response.send_message("Join my voice channel to use this.",ephemeral=True);return None
  return p
 @discord.ui.button(label="Pause / Resume",emoji="🌸",style=discord.ButtonStyle.primary,custom_id="m:p")
 async def pause(self,i,b):
  p=await self.okay(i)
  if not p:return
  vc=p.vc();p.manual_pause=not p.manual_pause
  if p.manual_pause and vc.is_playing():vc.pause()
  elif not p.room_pause and vc.is_paused():vc.resume()
  await panel(p);await i.response.send_message("Paused." if p.manual_pause else "Resumed.",ephemeral=True)
 @discord.ui.button(label="Next",emoji="🍓",style=discord.ButtonStyle.secondary,custom_id="m:n")
 async def next(self,i,b):
  p=await self.okay(i)
  if p:await i.response.defer(ephemeral=True);await advance(p,skip=True);await i.followup.send("Skipped.",ephemeral=True)
 @discord.ui.button(label="Loop",emoji="🎀",style=discord.ButtonStyle.secondary,custom_id="m:l")
 async def loop_(self,i,b):
  p=await self.okay(i)
  if p:p.loop=not p.loop;await panel(p);await i.response.send_message(f"Loop {'on' if p.loop else 'off'}.",ephemeral=True)
 @discord.ui.button(label="Volume −",emoji="🌷",style=discord.ButtonStyle.secondary,custom_id="m:d",row=1)
 async def down(self,i,b):await self.volume_(i,-10)
 @discord.ui.button(label="Volume +",emoji="💗",style=discord.ButtonStyle.secondary,custom_id="m:u",row=1)
 async def up(self,i,b):await self.volume_(i,10)
 async def volume_(self,i,change):
  p=await self.okay(i)
  if not p:return
  p.volume=max(10,min(200,p.volume+change));vc=p.vc()
  if isinstance(getattr(vc,"source",None),discord.PCMVolumeTransformer):vc.source.volume=p.volume/100
  await panel(p);await i.response.send_message(f"Volume: {p.volume}%",ephemeral=True)
 @discord.ui.button(label="Queue",emoji="📜",style=discord.ButtonStyle.secondary,custom_id="m:q",row=1)
 async def queue_(self,i,b):
  p=await self.okay(i)
  if p:await i.response.send_message("\n".join(f"{n}. {text(s)}" for n,s in enumerate(list(p.queue)[:10],1)) or "The queue is empty.",ephemeral=True)
 @discord.ui.button(label="Stop",emoji="🧸",style=discord.ButtonStyle.danger,custom_id="m:s",row=1)
 async def stop_(self,i,b):
  p=await self.okay(i)
  if not p:return
  await i.response.defer(ephemeral=True);p.queue.clear();p.current=None;p.generation+=1;vc=p.vc()
  if vc and (vc.is_playing() or vc.is_paused()):vc.stop()
  await panel(p,"Stopped. Queue cleared. I will stay connected.");await i.followup.send("Stopped.",ephemeral=True)

async def panel(p,message=None):
 ch=bot.get_channel(p.channel) if p.channel else None
 if not ch:return
 try:m=await ch.fetch_message(p.message) if p.message else None
 except (discord.NotFound,discord.Forbidden,discord.HTTPException):m=None
 if m:await m.edit(embed=make_embed(p,message),view=Controls(p.gid))
 else:p.message=(await ch.send(embed=make_embed(p,message),view=Controls(p.gid))).id
async def begin(p):
 vc=p.vc()
 if not vc or not p.current:return
 if not humans(vc.channel):p.room_pause=True;await panel(p);return
 try:
  raw=await asyncio.to_thread(stream,p.current);audio=discord.PCMVolumeTransformer(discord.FFmpegPCMAudio(raw,before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",options="-vn -loglevel warning"),volume=p.volume/100)
 except Exception as e:logging.warning("stream unavailable: %s",e);await advance(p,failed=True);return
 serial=p.generation
 def done(error):asyncio.run_coroutine_threadsafe(advance(p,failed=bool(error),serial=serial),bot.loop)
 vc.play(audio,after=done)
 if p.manual_pause or p.room_pause:vc.pause()
 await panel(p)
async def advance(p,skip=False,failed=False,serial=None):
 async with p.lock:
  if serial is not None and serial!=p.generation:return
  p.generation+=1
  if p.current and p.loop and not skip and not failed:next_song=p.current
  elif p.queue:next_song=p.queue.popleft()
  else:p.current=None;await panel(p,"Queue finished. I will stay in voice. Use `.play` for another song.");return
  p.current=next_song;p.manual_pause=False;await begin(p)
async def connect(ctx,p):
 dest=getattr(ctx.author.voice,"channel",None)
 if not dest:await ctx.send("Join a voice channel first.",delete_after=12);return None
 vc=p.vc()
 try:
  if vc and vc.channel!=dest:await vc.move_to(dest)
  elif not vc:vc=await dest.connect(self_deaf=False,reconnect=True)
  return vc
 except discord.ClientException:await ctx.send("I could not connect to that voice channel.",delete_after=12)

@bot.event
async def on_ready():logging.info("connected as %s",bot.user)
@bot.event
async def on_voice_state_update(member,before,after):
 p=players.get(member.guild.id);vc=p and p.vc()
 if not vc:return
 mine=vc.guild.me.voice;pause=not humans(vc.channel) or bool(mine and (mine.mute or mine.deaf))
 if pause!=p.room_pause:
  p.room_pause=pause
  if pause and vc.is_playing():vc.pause()
  elif not pause and vc.is_paused() and not p.manual_pause:vc.resume()
  await panel(p)
@bot.command()
@commands.guild_only()
async def join(ctx):
 p=player(ctx.guild);p.channel=ctx.channel.id
 if await connect(ctx,p):await panel(p,"Connected and staying in this voice channel. Use `.play` to begin.");await ctx.message.delete(delay=2)
@bot.command()
@commands.guild_only()
async def play(ctx,*,query=""):
 if not query:await ctx.send("Usage: `.play <song name or link>`",delete_after=12);return
 p=player(ctx.guild);p.channel=ctx.channel.id
 if not await connect(ctx,p):return
 try:s=await asyncio.to_thread(lookup,query)
 except Exception:await ctx.send("I could not find a playable result. Try another link or song name.",delete_after=12);return
 p.queue.append(s);vc=p.vc()
 if not p.current and not vc.is_playing() and not vc.is_paused():await advance(p)
 else:await panel(p)
 await ctx.message.delete(delay=2)
@bot.command()
@commands.guild_only()
async def stop(ctx):
 p=player(ctx.guild);p.queue.clear();p.current=None;p.generation+=1;vc=p.vc()
 if vc and (vc.is_playing() or vc.is_paused()):vc.stop()
 await panel(p,"Stopped. Queue cleared. I will stay connected.");await ctx.message.delete(delay=2)
@bot.command(name="help")
async def help_(ctx):
 e=discord.Embed(title="🌸 Miyazono Music",description="A cute 24/7 music player",colour=0xF4A7C3)
 for name,value in [("`.join`","Join your voice channel and stay connected."),("`.play <song or link>`","Play now or add to the queue."),("`.stop`","Stop music and clear the queue."),("Controller","Pause, next, loop, volume, queue, and stop are in the player panel.")]:e.add_field(name=name,value=value,inline=False)
 await ctx.send(embed=e,delete_after=40)
if __name__=="__main__":
 if not TOKEN:raise SystemExit("DISCORD_TOKEN is required")
 logging.basicConfig(level=os.getenv("LOG_LEVEL","INFO"),format="%(asctime)s %(levelname)s %(message)s")
 bot.run(TOKEN,log_handler=None)
