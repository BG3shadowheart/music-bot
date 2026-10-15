"""Self-hosted, multi-source Discord music bot.

Stack: discord.py 2.7+ (voice extra, DAVE capable) + wavelink 3.5+ + Lavalink v4.2.2+.
Source chain for every play request:
    1. YouTube Music (ytmsearch)  2. SoundCloud (scsearch)  3. YouTube (ytsearch)  4. yt-dlp
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import random
import re
import sys
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, NamedTuple, Optional, TypeVar, get_args
from urllib.parse import quote

import aiohttp
import discord
import wavelink
import yt_dlp
from discord import app_commands
from discord.ext import commands, tasks

T = TypeVar("T")
log = logging.getLogger("musicbot")


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _parse_ids(raw: str) -> set[int]:
    ids: set[int] = set()
    for part in raw.replace(";", ",").split(","):
        part = part.strip()
        if part.isdigit():
            ids.add(int(part))
    return ids


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
SIBLING_BOT_IDS = _parse_ids(os.getenv("SIBLING_BOT_IDS", ""))
LAVALINK_URI = os.getenv("LAVALINK_URI", "http://localhost:2333")
LAVALINK_PASSWORD = os.getenv("LAVALINK_PASSWORD", "")
STATE_FILE = Path(os.getenv("STATE_FILE", "/data/state.json"))
BOT_PREFIX = os.getenv("BOT_PREFIX", ".")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
SYNC_SLASH = os.getenv("SYNC_SLASH_COMMANDS", "true").lower() in ("1", "true", "yes", "on")
YTDLP_COOKIES = os.getenv("YTDLP_COOKIES_FILE", "")

DEFAULT_VOLUME = max(0, min(100, _env_int("DEFAULT_VOLUME", 60)))
SEARCH_TIMEOUT = _env_float("SEARCH_TIMEOUT", 12.0)
LAVALINK_TIMEOUT = _env_float("LAVALINK_TIMEOUT", 20.0)
VOICE_CONNECT_TIMEOUT = _env_float("VOICE_CONNECT_TIMEOUT", 20.0)
YTDLP_TIMEOUT = _env_float("YTDLP_TIMEOUT", 30.0)
IDLE_TIMEOUT = _env_int("IDLE_TIMEOUT_SECONDS", 300)
EMPTY_LEAVE_SECONDS = _env_int("EMPTY_LEAVE_SECONDS", 600)  # 0 = never leave an empty channel
MAX_QUEUE = 500
MAX_PLAYLISTS_PER_USER = 25
MAX_PLAYLIST_TRACKS = 200
MIN_LAVALINK = (4, 2, 2)
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
EMBED_COLOR = 0x5865F2


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)) + "Z",
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging() -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(LOG_LEVEL if LOG_LEVEL in logging._nameToLevel else "INFO")
    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("discord.http").setLevel(logging.WARNING)


def one_line(exc: BaseException | str) -> str:
    text = " ".join(str(exc).split())
    name = type(exc).__name__ if isinstance(exc, BaseException) else "error"
    return f"{name}: {text[:220]}"


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
class UserFacingError(commands.CommandError):
    """Raised to show a friendly message to the user."""


Ctx = commands.Context
FilterName = Literal[
    "off", "bassboost", "nightcore", "vaporwave", "8d", "karaoke", "tremolo", "vibrato", "soft", "treble"
]
LoopMode = Literal["track", "queue", "off"]
Switch = Literal["on", "off"]
ROUTED_COMMANDS = {"join", "play", "247", "playlist load"}


class SilentIgnore(commands.CommandError):
    """Raised when another bot instance should handle this command."""


def humans_in(channel: Any) -> list[discord.Member]:
    if channel is None:
        return []
    return [member for member in channel.members if not member.bot]


def fmt_ms(ms: int | float) -> str:
    seconds = max(0, int(ms // 1000))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def track_line(track: wavelink.Playable) -> str:
    title = discord.utils.escape_markdown(track.title or "Unknown title")
    if len(title) > 70:
        title = title[:69] + "…"
    return f"[{title}]({track.uri})" if track.uri else title


def is_stale_session(exc: BaseException) -> bool:
    text = str(exc).lower()
    return getattr(exc, "status", None) == 404 or "session not found" in text or "session_not_found" in text


def track_to_raw(track: wavelink.Playable | None) -> dict[str, Any] | None:
    if track is None:
        return None
    raw = getattr(track, "raw_data", None)
    return copy.deepcopy(raw) if isinstance(raw, dict) else None


def track_from_raw(raw: Any) -> wavelink.Playable | None:
    if not isinstance(raw, dict):
        return None
    try:
        return wavelink.Playable(copy.deepcopy(raw))
    except Exception as exc:  # corrupt entry; skip it
        log.debug("could not rebuild track from saved data: %s", one_line(exc))
        return None


async def reply(
    ctx: Ctx,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    ephemeral: bool = False,
) -> discord.Message | None:
    delete_after = 20.0 if (ephemeral and ctx.interaction is None) else None
    try:
        return await ctx.send(content, embed=embed, ephemeral=ephemeral, delete_after=delete_after)
    except discord.Forbidden:
        log.warning("cannot send messages guild=%s channel=%s", getattr(ctx.guild, "id", None), ctx.channel.id)
    except discord.HTTPException as exc:
        log.warning("failed to send reply: %s", one_line(exc))
    return None


# --------------------------------------------------------------------------- #
# Filters
# --------------------------------------------------------------------------- #
def _eq(*gains: float) -> list[dict[str, float]]:
    return [{"band": index, "gain": gain} for index, gain in enumerate(gains)]


def build_filters(name: str) -> wavelink.Filters:
    filters = wavelink.Filters()
    if name == "bassboost":
        filters.equalizer.set(bands=_eq(0.25, 0.2, 0.15, 0.1, 0.05, 0.0, -0.05))
    elif name == "nightcore":
        filters.timescale.set(speed=1.2, pitch=1.2, rate=1.0)
    elif name == "vaporwave":
        filters.timescale.set(speed=0.85, pitch=0.8, rate=1.0)
    elif name == "8d":
        filters.rotation.set(rotation_hz=0.2)
    elif name == "karaoke":
        filters.karaoke.set(level=1.0, mono_level=1.0, filter_band=220.0, filter_width=100.0)
    elif name == "tremolo":
        filters.tremolo.set(frequency=4.0, depth=0.5)
    elif name == "vibrato":
        filters.vibrato.set(frequency=4.0, depth=0.5)
    elif name == "soft":
        filters.low_pass.set(smoothing=20.0)
    elif name == "treble":
        filters.equalizer.set(bands=[{"band": band, "gain": 0.15} for band in range(8, 14)])
    return filters


assert set(get_args(FilterName)) == {
    "off", "bassboost", "nightcore", "vaporwave", "8d", "karaoke", "tremolo", "vibrato", "soft", "treble"
}


# --------------------------------------------------------------------------- #
# Persistent state
# --------------------------------------------------------------------------- #
class StateStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.lock = asyncio.Lock()
        self.data: dict[str, Any] = self._blank()

    @staticmethod
    def _blank() -> dict[str, Any]:
        return {"version": 1, "guilds": {}, "playlists": {}}

    def load(self) -> None:
        if not self.path.exists():
            log.info("no state file at %s; starting clean", self.path)
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict) or not isinstance(raw.get("guilds"), dict):
                raise ValueError("unexpected structure")
            playlists = raw.get("playlists", {})
            if not isinstance(playlists, dict):
                raise ValueError("playlists is not an object")
            self.data = {"version": 1, "guilds": raw["guilds"], "playlists": playlists}
            log.info("state loaded path=%s guilds=%d", self.path, len(self.data["guilds"]))
        except (OSError, ValueError) as exc:
            log.warning("state file unreadable (%s); starting clean", one_line(exc))
            try:
                self.path.replace(self.path.with_suffix(".corrupt"))
            except OSError:
                pass
            self.data = self._blank()

    def set_guild(self, guild_id: int, snapshot: dict[str, Any]) -> None:
        self.data["guilds"][str(guild_id)] = snapshot

    def drop_guild(self, guild_id: int) -> None:
        self.data["guilds"].pop(str(guild_id), None)

    def _write(self, payload: str) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, self.path)

    async def flush(self) -> None:
        async with self.lock:
            try:
                payload = json.dumps(self.data, ensure_ascii=False)
                await asyncio.to_thread(self._write, payload)
            except (OSError, TypeError, ValueError) as exc:
                log.error("failed to write state file path=%s err=%s", self.path, one_line(exc))


# --------------------------------------------------------------------------- #
# Player (with DAVE-compliant voice payload)
# --------------------------------------------------------------------------- #
class BotPlayer(wavelink.Player):
    """wavelink Player with bot state and a voice update that always carries channelId (DAVE)."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.loop_mode: str = "off"
        self.autoplay_enabled: bool = False
        self.stay_247: bool = False
        self.filter_name: str = "off"
        self.volume_pref: int = DEFAULT_VOLUME
        self.text_channel_id: int | None = None
        self.suppress_advance: bool = False
        self.last_started: str | None = None
        self.history_keys: deque[str] = deque(maxlen=50)
        self.fallbacks_tried: set[str] = set()
        self.auto_pause_reasons: set[str] = set()  # "empty" / "muted"
        self.user_paused: bool = False
        self.pause_lock = asyncio.Lock()
        # Queue advancement is handled by the bot (own loop/autoplay/fallback logic).
        self.autoplay = wavelink.AutoPlayMode.disabled

    def remember(self, track: wavelink.Playable | None) -> None:
        if track is None:
            return
        self.history_keys.append(track.identifier or track.title.lower())
        self.history_keys.append(track.title.lower())

    async def _dispatch_voice_update(self) -> None:  # type: ignore[override]
        voice = getattr(self, "_voice_state", {}).get("voice", {})
        session_id = voice.get("session_id")
        token = voice.get("token")
        endpoint = voice.get("endpoint")
        channel = self.channel
        guild = self.guild
        if not (session_id and token and endpoint and channel and guild):
            return  # incomplete voice state; Discord sends the rest shortly
        payload = {
            "voice": {
                "sessionId": session_id,
                "token": token,
                "endpoint": endpoint,
                "channelId": str(channel.id),  # mandatory for DAVE
            }
        }
        try:
            await self.node._update_player(guild.id, data=payload)
        except wavelink.exceptions.LavalinkException as exc:
            if is_stale_session(exc):
                log.warning("voice update hit stale lavalink session guild=%s", guild.id)
                self.client.dispatch("lavalink_session_stale", self)
                return
            log.warning("voice update rejected guild=%s err=%s", guild.id, one_line(exc))
            try:
                await self.disconnect()
            except Exception as inner:
                log.debug("disconnect after rejected voice update failed: %s", one_line(inner))
            return
        event = getattr(self, "_connection_event", None)
        if event is not None:
            event.set()


def snapshot_player(player: BotPlayer, previous: dict[str, Any] | None = None) -> dict[str, Any]:
    current = player.current
    me = player.guild.me if player.guild else None
    voice = me.voice if me else None
    live_channel = voice.channel if voice and voice.channel else player.channel
    channel_id = live_channel.id if live_channel else (previous or {}).get("channel_id")
    queue_raw = [raw for raw in (track_to_raw(t) for t in list(player.queue)[:MAX_QUEUE]) if raw]
    return {
        "channel_id": channel_id,
        "text_channel_id": player.text_channel_id,
        "current": track_to_raw(current),
        "position": int(player.position) if current else 0,
        "paused": bool(player.paused),
        "user_paused": player.user_paused,
        "queue": queue_raw,
        "volume": player.volume_pref,
        "loop": player.loop_mode,
        "stay_247": player.stay_247,
        "autoplay": player.autoplay_enabled,
        "filter": player.filter_name,
        "saved_at": int(time.time()),
    }


# --------------------------------------------------------------------------- #
# Source resolution
# --------------------------------------------------------------------------- #
class SourceSpec(NamedTuple):
    label: str
    prefix: str
    source_name: str


SOURCE_CHAIN: tuple[SourceSpec, ...] = (
    SourceSpec("YouTube Music", "ytmsearch", "youtube"),
    SourceSpec("SoundCloud", "scsearch", "soundcloud"),
    SourceSpec("YouTube", "ytsearch", "youtube"),
)


@dataclass
class Resolution:
    tracks: list[wavelink.Playable]
    source: str
    playlist_name: str | None = None


def _ytdlp_extract_sync(query: str) -> dict[str, Any] | None:
    options: dict[str, Any] = {
        "format": "bestaudio[protocol^=http]/bestaudio/best",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "socket_timeout": 10,
        "retries": 1,
    }
    if YTDLP_COOKIES:
        options["cookiefile"] = YTDLP_COOKIES
    target = query if URL_RE.match(query) else f"ytsearch1:{query}"
    with yt_dlp.YoutubeDL(options) as ydl:
        info = ydl.extract_info(target, download=False)
    if not info:
        return None
    if info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        info = entries[0] if entries else None
    if not info:
        return None
    stream_url = info.get("url")
    if not stream_url and info.get("requested_formats"):
        stream_url = info["requested_formats"][0].get("url")
    if not stream_url:
        return None
    return {
        "url": stream_url,
        "title": info.get("title") or "Unknown title",
        "author": info.get("uploader") or info.get("channel") or "Unknown artist",
        "webpage": info.get("webpage_url") or target,
        "thumbnail": info.get("thumbnail"),
    }


# --------------------------------------------------------------------------- #
# Bot
# --------------------------------------------------------------------------- #
class MusicBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        intents.members = bool(SIBLING_BOT_IDS)  # needed to see sibling bots
        super().__init__(
            command_prefix=commands.when_mentioned_or(BOT_PREFIX),
            intents=intents,
            help_command=None,
            case_insensitive=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        self.store = StateStore(STATE_FILE)
        self.sibling_ids = set(SIBLING_BOT_IDS)
        self.session: aiohttp.ClientSession | None = None
        self.leaving: set[int] = set()
        self.recovering = False
        self.last_recover = 0.0
        self.initial_restore_done = False
        self.tree.error(self.on_tree_error)

    async def setup_hook(self) -> None:
        self.store.load()
        self.session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=10),
            headers={"User-Agent": "SelfHostedMusicBot/1.0"},
        )
        await self.add_cog(Music(self))
        if SYNC_SLASH:
            try:
                synced = await self.tree.sync()
                log.info("slash commands synced count=%d", len(synced))
            except discord.HTTPException as exc:
                log.warning("slash command sync failed: %s", one_line(exc))

    async def on_ready(self) -> None:
        log.info("bot ready user=%s guilds=%d", self.user, len(self.guilds))

    async def close(self) -> None:
        self.leaving.update(guild.id for guild in self.guilds)
        cog = self.get_cog("Music")
        if isinstance(cog, Music):
            await cog.persist_all()
        try:
            await wavelink.Pool.close()
        except Exception as exc:
            log.debug("pool close failed: %s", one_line(exc))
        if self.session and not self.session.closed:
            await self.session.close()
        await super().close()

    # ---- global error handling ------------------------------------------- #
    async def on_command_error(self, ctx: Ctx, error: commands.CommandError) -> None:  # type: ignore[override]
        for _ in range(4):
            if isinstance(
                error, (commands.CommandInvokeError, commands.HybridCommandError, app_commands.CommandInvokeError)
            ) and getattr(error, "original", None) is not None:
                error = error.original  # type: ignore[assignment]
            else:
                break

        guild_id = getattr(ctx.guild, "id", None)
        command_name = ctx.command.qualified_name if ctx.command else "?"

        if isinstance(error, (commands.CommandNotFound, SilentIgnore)):
            return
        if isinstance(error, UserFacingError):
            message = str(error)
        elif isinstance(error, commands.NoPrivateMessage):
            message = "This command only works in a server."
        elif isinstance(error, commands.MissingRequiredArgument):
            usage = f"{ctx.clean_prefix}{command_name} {ctx.command.signature}" if ctx.command else ""
            message = f"Missing `{error.param.name}`. Usage: `{usage.strip()}`"
        elif isinstance(error, commands.BadLiteralArgument):
            options = ", ".join(f"`{literal}`" for literal in error.literals)
            message = f"`{error.param.name}` must be one of: {options}"
        elif isinstance(error, commands.UserInputError):
            message = "That input doesn't look right. Check the command usage and try again."
        elif isinstance(error, commands.CommandOnCooldown):
            message = f"Slow down! Try again in {error.retry_after:.0f}s."
        elif isinstance(error, commands.CheckFailure):
            message = "You can't use that command here."
        elif isinstance(error, wavelink.exceptions.LavalinkLoadException):
            log.debug("load failed command=%s guild=%s err=%s", command_name, guild_id, one_line(error))
            message = "I couldn't load that track from any source. Try a different search or link."
        elif isinstance(error, wavelink.exceptions.ChannelTimeoutException):
            log.warning("voice timeout command=%s guild=%s", command_name, guild_id)
            message = "Timed out while connecting to the voice channel. Please try again."
        elif isinstance(error, asyncio.TimeoutError):
            log.warning("operation timed out command=%s guild=%s", command_name, guild_id)
            message = "That took too long. Please try again in a moment."
        elif isinstance(error, discord.Forbidden):
            log.warning("missing permissions command=%s guild=%s", command_name, guild_id)
            message = "I'm missing permissions for that. I need Connect, Speak, Send Messages and Embed Links."
        elif isinstance(error, wavelink.exceptions.LavalinkException):
            log.warning("lavalink error command=%s guild=%s err=%s", command_name, guild_id, one_line(error))
            message = (
                "The music backend is restarting. I'm reconnecting; please try again in a few seconds."
                if is_stale_session(error)
                else "The music backend returned an error. Please try again."
            )
        elif isinstance(error, wavelink.exceptions.WavelinkException):
            log.warning("wavelink error command=%s guild=%s err=%s", command_name, guild_id, one_line(error))
            message = "Something went wrong talking to the music backend. Please try again."
        else:
            log.error(
                "unhandled command error command=%s guild=%s",
                command_name,
                guild_id,
                exc_info=(type(error), error, error.__traceback__),
            )
            message = "Something unexpected went wrong. It has been logged; please try again."
        await reply(ctx, f"⚠️ {message}", ephemeral=True)

    async def on_tree_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, commands.HybridCommandError):
            return  # routed to on_command_error
        log.warning("app command error: %s", one_line(error))
        try:
            text = "⚠️ Something went wrong with that command. Please try again."
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            pass


# --------------------------------------------------------------------------- #
# Music cog
# --------------------------------------------------------------------------- #
class Music(commands.Cog):
    def __init__(self, bot: MusicBot) -> None:
        self.bot = bot
        self.idle_tasks: dict[int, asyncio.Task[None]] = {}
        self.empty_tasks: dict[int, asyncio.Task[None]] = {}
        self.node_task: asyncio.Task[None] | None = None
        self.background: set[asyncio.Task[Any]] = set()

    # ---- lifecycle ---------------------------------------------------------- #
    def spawn(self, coro: Awaitable[Any]) -> asyncio.Task[Any]:
        task = asyncio.ensure_future(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    async def cog_load(self) -> None:
        log.info("dave: channelId voice payload patch active; required lavalink >= %d.%d.%d", *MIN_LAVALINK)
        if not await self.connect_node_once():
            self.node_task = self.spawn(self.node_connect_loop())
        self.persist_loop.start()

    async def cog_unload(self) -> None:
        self.persist_loop.cancel()
        for task in list(self.background):
            task.cancel()

    async def cog_check(self, ctx: Ctx) -> bool:  # type: ignore[override]
        if ctx.guild is None:
            raise commands.NoPrivateMessage()
        if not self.should_handle(ctx):
            raise SilentIgnore()
        return True

    def should_handle(self, ctx: Ctx) -> bool:
        """Multi-bot routing: decide whether THIS bot instance answers a prefix command.

        1. A bot already in the author's voice channel handles it.
        2. For join/play-type commands, the lowest-ID *free* bot handles it
           (free = not in a voice channel, or in one with no humans).
        3. Everything else is answered by the lowest-ID bot only (no duplicate replies).
        Slash commands are never routed: the user explicitly picked the bot.
        """
        if ctx.interaction is not None or not self.bot.sibling_ids:
            return True
        guild = ctx.guild
        me = guild.me
        siblings = [m for m in (guild.get_member(i) for i in self.bot.sibling_ids if i != me.id) if m]
        if not siblings:
            return True
        bots = sorted([me, *siblings], key=lambda m: m.id)

        def channel_of(member: discord.Member) -> Any:
            return member.voice.channel if member.voice else None

        user_channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if user_channel is None:
            return bots[0].id == me.id
        present = [m for m in bots if channel_of(m) is not None and channel_of(m).id == user_channel.id]
        if present:
            return present[0].id == me.id
        name = ctx.command.qualified_name if ctx.command else ""
        if name not in ROUTED_COMMANDS:
            return bots[0].id == me.id
        free = [m for m in bots if channel_of(m) is None or not humans_in(channel_of(m))]
        if not free:
            return bots[0].id == me.id
        return free[0].id == me.id

    # ---- node management ---------------------------------------------------- #
    async def connect_node_once(self) -> bool:
        node = wavelink.Node(identifier="main", uri=LAVALINK_URI, password=LAVALINK_PASSWORD)
        try:
            await wavelink.Pool.connect(nodes=[node], client=self.bot)
            return True
        except wavelink.exceptions.WavelinkException as exc:
            log.warning("lavalink connect failed uri=%s err=%s", LAVALINK_URI, one_line(exc))
        except Exception as exc:
            log.warning("lavalink connect error uri=%s err=%s", LAVALINK_URI, one_line(exc))
        return False

    async def node_connect_loop(self) -> None:
        while not self.bot.is_closed():
            try:
                wavelink.Pool.get_node()
                return
            except wavelink.exceptions.InvalidNodeException:
                pass
            try:
                if wavelink.Pool.nodes:
                    await wavelink.Pool.reconnect()
                else:
                    await self.connect_node_once()
            except Exception as exc:
                log.warning("lavalink reconnect attempt failed: %s", one_line(exc))
            await asyncio.sleep(10)

    async def require_node(self, wait: float = 10.0) -> wavelink.Node:
        deadline = asyncio.get_running_loop().time() + wait
        while True:
            try:
                return wavelink.Pool.get_node()
            except wavelink.exceptions.InvalidNodeException:
                if asyncio.get_running_loop().time() >= deadline:
                    if self.node_task is None or self.node_task.done():
                        self.node_task = self.spawn(self.node_connect_loop())
                    raise UserFacingError(
                        "The music backend (Lavalink) is offline right now. It reconnects automatically; "
                        "please try again shortly."
                    )
                await asyncio.sleep(1)

    async def verify_lavalink_version(self, node: wavelink.Node) -> None:
        try:
            info = await node.fetch_info()
            version = info.version
            parsed = (int(version.major), int(version.minor), int(version.patch))
        except Exception as exc:
            log.warning("could not determine lavalink version: %s", one_line(exc))
            return
        if parsed < MIN_LAVALINK:
            log.error(
                "lavalink %d.%d.%d is older than required %d.%d.%d; DAVE voice connections will fail",
                *parsed,
                *MIN_LAVALINK,
            )
        else:
            log.info("lavalink version ok version=%d.%d.%d (DAVE capable)", *parsed)

    async def recover_lavalink(self, reason: str, *, rebuild_node: bool) -> None:
        """Rebuild the node (optional) and every player from the persisted state."""
        bot = self.bot
        if bot.recovering:
            while bot.recovering:
                await asyncio.sleep(0.5)
            return
        if time.monotonic() - bot.last_recover < 10:
            return
        bot.recovering = True
        log.warning("recovering lavalink session reason=%s rebuild_node=%s", reason, rebuild_node)
        try:
            await self.persist_all()
            players = [vc for vc in bot.voice_clients if isinstance(vc, BotPlayer)]
            guild_ids = [vc.guild.id for vc in players if vc.guild]
            bot.leaving.update(guild_ids)
            for player in players:
                try:
                    await player.disconnect()
                except Exception as exc:
                    log.debug("disconnect during recovery failed: %s", one_line(exc))
            if rebuild_node:
                try:
                    await wavelink.Pool.close()
                except Exception as exc:
                    log.debug("pool close during recovery failed: %s", one_line(exc))
                if not await self.connect_node_once():
                    if self.node_task is None or self.node_task.done():
                        self.node_task = self.spawn(self.node_connect_loop())
            try:
                await self.require_node(wait=30.0)
            except UserFacingError:
                log.error("lavalink did not come back during recovery")
                return
            await self.restore_state()
        except Exception as exc:
            log.error("recovery failed: %s", one_line(exc))
        finally:
            bot.recovering = False
            bot.last_recover = time.monotonic()
            asyncio.get_running_loop().call_later(15, self.bot.leaving.clear)

    # ---- persistence ---------------------------------------------------------- #
    async def persist(self, player: BotPlayer | None = None) -> None:
        try:
            if player is not None and player.guild is not None:
                previous = self.bot.store.data["guilds"].get(str(player.guild.id))
                self.bot.store.set_guild(player.guild.id, snapshot_player(player, previous))
            await self.bot.store.flush()
        except Exception as exc:
            log.error("persist failed: %s", one_line(exc))

    async def persist_all(self) -> None:
        try:
            for vc in list(self.bot.voice_clients):
                if isinstance(vc, BotPlayer) and vc.guild is not None and vc.guild.id not in self.bot.leaving:
                    previous = self.bot.store.data["guilds"].get(str(vc.guild.id))
                    self.bot.store.set_guild(vc.guild.id, snapshot_player(vc, previous))
            await self.bot.store.flush()
        except Exception as exc:
            log.error("persist_all failed: %s", one_line(exc))

    @tasks.loop(seconds=30)
    async def persist_loop(self) -> None:
        if not self.bot.recovering:
            await self.persist_all()

    @persist_loop.before_loop
    async def before_persist_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def restore_state(self) -> None:
        entries = list(self.bot.store.data["guilds"].items())
        if not entries:
            log.info("no saved player state to restore")
            return
        restored = 0
        for guild_key, data in entries:
            try:
                guild_id = int(guild_key)
            except ValueError:
                self.bot.store.data["guilds"].pop(guild_key, None)
                continue
            try:
                if await self.restore_guild(guild_id, data):
                    restored += 1
            except Exception as exc:
                log.warning("restore failed guild=%s err=%s", guild_id, one_line(exc))
        await self.bot.store.flush()
        log.info("state restore finished restored=%d saved=%d", restored, len(entries))

    async def restore_guild(self, guild_id: int, data: dict[str, Any]) -> bool:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return False
        try:
            channel = guild.get_channel(int(data["channel_id"]))
        except (KeyError, TypeError, ValueError):
            channel = None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            self.bot.store.drop_guild(guild_id)
            return False
        stay_247 = bool(data.get("stay_247"))
        current = track_from_raw(data.get("current"))
        queued = [t for t in (track_from_raw(raw) for raw in data.get("queue", [])[:MAX_QUEUE]) if t]
        if not stay_247 and not current and not queued:
            self.bot.store.drop_guild(guild_id)
            return False
        if not stay_247 and not any(not member.bot for member in channel.members):
            self.bot.store.drop_guild(guild_id)
            return False

        existing = guild.voice_client
        if existing is not None:
            try:
                await existing.disconnect(force=True)
            except Exception as exc:
                log.debug("clearing stale voice client failed: %s", one_line(exc))
        try:
            player = await self.connect_player(channel)
        except (wavelink.exceptions.WavelinkException, asyncio.TimeoutError, discord.DiscordException) as exc:
            log.warning("restore could not join voice guild=%s err=%s", guild_id, one_line(exc))
            return False

        player.stay_247 = stay_247
        player.autoplay_enabled = bool(data.get("autoplay"))
        player.loop_mode = data.get("loop") if data.get("loop") in ("off", "track", "queue") else "off"
        player.filter_name = data.get("filter") if data.get("filter") in get_args(FilterName) else "off"
        player.volume_pref = max(0, min(100, int(data.get("volume", DEFAULT_VOLUME))))
        player.text_channel_id = data.get("text_channel_id")
        for track in queued:
            player.queue.put(track)
        player.user_paused = bool(data.get("user_paused"))
        await self.update_listener_state(player)

        if current is not None:
            position = int(data.get("position") or 0)
            if current.length and position >= current.length - 5000:
                position = 0
            try:
                await player.play(
                    current,
                    start=position,
                    volume=player.volume_pref,
                    paused=player.user_paused or bool(player.auto_pause_reasons),
                )
            except Exception as exc:
                log.warning("restore could not resume track guild=%s err=%s", guild_id, one_line(exc))
                player.queue.put_at(0, current)
                try:
                    await self.play_next(player)
                except Exception as inner:
                    log.warning("restore could not start queue guild=%s err=%s", guild_id, one_line(inner))
        elif not player.queue.is_empty:
            try:
                await self.play_next(player)
            except Exception as exc:
                log.warning("restore could not start queue guild=%s err=%s", guild_id, one_line(exc))

        if player.filter_name != "off":
            try:
                await player.set_filters(build_filters(player.filter_name))
            except Exception as exc:
                log.debug("restore filter failed: %s", one_line(exc))
        if player.current is None and not player.stay_247:
            self.schedule_idle(player)
        await self.persist(player)
        log.info("restored player guild=%s channel=%s queue=%d", guild_id, channel.id, player.queue.count)
        return True

    # ---- voice connection ------------------------------------------------------ #
    async def connect_player(self, channel: discord.VoiceChannel | discord.StageChannel) -> BotPlayer:
        await self.require_node()
        last_error: BaseException | None = None
        for attempt in (1, 2):
            try:
                return await channel.connect(cls=BotPlayer, self_deaf=True, timeout=VOICE_CONNECT_TIMEOUT)
            except (wavelink.exceptions.ChannelTimeoutException, asyncio.TimeoutError) as exc:
                last_error = exc
                log.warning("voice connect timeout guild=%s attempt=%d", channel.guild.id, attempt)
                stale = channel.guild.voice_client
                if stale is not None:
                    try:
                        await stale.disconnect(force=True)
                    except Exception as inner:
                        log.debug("cleanup after voice timeout failed: %s", one_line(inner))
                if attempt == 1:
                    await asyncio.sleep(5)
        assert last_error is not None
        raise last_error

    @staticmethod
    def user_voice_channel(ctx: Ctx) -> discord.VoiceChannel | discord.StageChannel:
        channel = getattr(getattr(ctx.author, "voice", None), "channel", None)
        if channel is None:
            raise UserFacingError("Join a voice channel first.")
        return channel

    def player_of(self, ctx: Ctx) -> BotPlayer:
        vc = ctx.guild.voice_client if ctx.guild else None
        if not isinstance(vc, BotPlayer) or not vc.connected:
            raise UserFacingError(f"I'm not connected to a voice channel. Use `{ctx.clean_prefix}join` or `{ctx.clean_prefix}play`.")
        return vc

    async def move_player(self, player: BotPlayer, channel: discord.VoiceChannel | discord.StageChannel) -> None:
        permissions = channel.permissions_for(channel.guild.me)
        if not (permissions.connect and permissions.speak):
            raise UserFacingError(f"I need **Connect** and **Speak** permissions in {channel.mention}.")
        old = player.channel
        await player.move_to(channel)
        log.info("moved voice guild=%s from=%s to=%s", channel.guild.id, old.id if old else None, channel.id)
        await self.update_listener_state(player)
        await self.persist(player)

    async def ensure_player(self, ctx: Ctx, *, connect: bool) -> BotPlayer:
        user_channel = self.user_voice_channel(ctx)
        vc = ctx.guild.voice_client
        if isinstance(vc, BotPlayer) and vc.connected:
            if vc.channel and vc.channel.id != user_channel.id:
                busy = vc.current is not None and bool(humans_in(vc.channel))
                if busy:
                    raise UserFacingError(
                        f"I'm playing music in {vc.channel.mention} right now. "
                        "Join it to listen, or wait until I'm free."
                    )
                if not connect:
                    raise UserFacingError(f"Join {vc.channel.mention} to control me.")
                await self.move_player(vc, user_channel)
            vc.text_channel_id = ctx.channel.id
            return vc
        if not connect:
            raise UserFacingError(f"I'm not connected. Use `{ctx.clean_prefix}join` or `{ctx.clean_prefix}play` first.")
        if vc is not None:
            try:
                await vc.disconnect(force=True)
            except Exception as exc:
                log.debug("clearing old voice client failed: %s", one_line(exc))
        permissions = user_channel.permissions_for(ctx.guild.me)
        if not (permissions.connect and permissions.speak):
            raise UserFacingError(f"I need **Connect** and **Speak** permissions in {user_channel.mention}.")
        player = await self.connect_player(user_channel)
        player.text_channel_id = ctx.channel.id
        self.bot.leaving.discard(ctx.guild.id)
        log.info("joined voice guild=%s channel=%s", ctx.guild.id, user_channel.id)
        return player

    async def guarded(
        self,
        ctx: Ctx,
        op: Callable[[BotPlayer], Awaitable[T]],
        *,
        what: str,
    ) -> T:
        """Run a Lavalink operation; on a stale session (404) rebuild everything and retry once."""
        player = self.player_of(ctx)
        for attempt in (1, 2):
            try:
                return await asyncio.wait_for(op(player), timeout=LAVALINK_TIMEOUT)
            except wavelink.exceptions.LavalinkException as exc:
                if attempt == 1 and is_stale_session(exc):
                    log.warning("stale session during %s guild=%s; reconnecting", what, ctx.guild.id)
                    await self.recover_lavalink(f"404 during {what}", rebuild_node=True)
                    player = self.player_of(ctx)
                    continue
                raise
            except asyncio.TimeoutError:
                log.warning("lavalink operation timed out op=%s guild=%s", what, ctx.guild.id)
                raise
        raise UserFacingError("The music backend is unavailable right now. Please try again shortly.")

    async def leave_guild(self, guild: discord.Guild) -> None:
        self.bot.leaving.add(guild.id)
        self.cancel_task(self.idle_tasks, guild.id)
        self.cancel_task(self.empty_tasks, guild.id)
        self.bot.store.drop_guild(guild.id)
        vc = guild.voice_client
        try:
            if vc is not None:
                await vc.disconnect()
        except Exception as exc:
            log.debug("disconnect failed guild=%s err=%s", guild.id, one_line(exc))
        await self.bot.store.flush()
        asyncio.get_running_loop().call_later(15, self.bot.leaving.discard, guild.id)
        log.info("left voice guild=%s", guild.id)

    async def notify(
        self, player: BotPlayer, content: str | None = None, *, embed: discord.Embed | None = None
    ) -> None:
        if player.text_channel_id is None:
            return
        channel = self.bot.get_channel(player.text_channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        try:
            await channel.send(content, embed=embed)
        except discord.Forbidden:
            log.warning("cannot notify channel=%s (missing permissions)", player.text_channel_id)
        except discord.HTTPException as exc:
            log.warning("notify failed: %s", one_line(exc))

    # ---- idle / empty channel handling -------------------------------------------- #
    @staticmethod
    def cancel_task(registry: dict[int, asyncio.Task[None]], guild_id: int) -> None:
        task = registry.pop(guild_id, None)
        if task and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def schedule_idle(self, player: BotPlayer) -> None:
        if player.guild is None or player.stay_247:
            return
        guild_id = player.guild.id
        self.cancel_task(self.idle_tasks, guild_id)
        self.idle_tasks[guild_id] = self.spawn(self._idle_leave(guild_id))

    async def _idle_leave(self, guild_id: int) -> None:
        await asyncio.sleep(IDLE_TIMEOUT)
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if guild and isinstance(vc, BotPlayer) and not vc.stay_247 and vc.current is None and vc.queue.is_empty:
            await self.notify(vc, "👋 Leaving the voice channel due to inactivity.")
            await self.leave_guild(guild)

    def check_empty(self, player: BotPlayer, empty: bool) -> None:
        if player.guild is None:
            return
        guild_id = player.guild.id
        if not empty or player.stay_247 or EMPTY_LEAVE_SECONDS <= 0:
            self.cancel_task(self.empty_tasks, guild_id)
            return
        task = self.empty_tasks.get(guild_id)
        if task is None or task.done():
            self.empty_tasks[guild_id] = self.spawn(self._leave_when_empty(guild_id))

    async def _leave_when_empty(self, guild_id: int) -> None:
        await asyncio.sleep(EMPTY_LEAVE_SECONDS)
        guild = self.bot.get_guild(guild_id)
        vc = guild.voice_client if guild else None
        if guild and isinstance(vc, BotPlayer) and vc.channel and not vc.stay_247 and not humans_in(vc.channel):
            await self.notify(vc, "👋 Leaving the voice channel because nobody has been listening for a while.")
            await self.leave_guild(guild)

    @staticmethod
    def pause_reason_text(player: BotPlayer) -> str:
        if "muted" in player.auto_pause_reasons:
            return "I'm server muted or deafened"
        return "nobody is listening"

    async def update_listener_state(self, player: BotPlayer) -> None:
        """Recompute auto-pause reasons (no humans / server mute or deafen) and apply them."""
        guild = player.guild
        if guild is None:
            return
        me = guild.me
        voice = me.voice if me else None
        channel = voice.channel if voice and voice.channel else player.channel
        empty = not humans_in(channel)
        muted = bool(voice and (voice.mute or voice.deaf))
        for reason, active in (("empty", empty), ("muted", muted)):
            if active:
                player.auto_pause_reasons.add(reason)
            else:
                player.auto_pause_reasons.discard(reason)
        self.check_empty(player, empty)
        await self.apply_auto_pause(player)

    async def apply_auto_pause(self, player: BotPlayer) -> None:
        if player.guild is None or not player.connected:
            return
        async with player.pause_lock:
            if player.current is None:
                return
            want_pause = bool(player.auto_pause_reasons)
            try:
                if want_pause and not player.paused:
                    await player.pause(True)
                    await self.notify(player, f"⏸️ Paused: {self.pause_reason_text(player)}.")
                elif not want_pause and player.paused and not player.user_paused:
                    await player.pause(False)
                    await self.notify(player, "▶️ Resumed.")
                else:
                    return
            except wavelink.exceptions.LavalinkException as exc:
                if is_stale_session(exc):
                    self.spawn(self.recover_lavalink("404 during auto-pause", rebuild_node=True))
                else:
                    log.warning("auto-pause failed guild=%s err=%s", player.guild.id, one_line(exc))
                return
            except Exception as exc:
                log.warning("auto-pause unexpected error guild=%s err=%s", player.guild.id, one_line(exc))
                return
        await self.persist(player)

    # ---- source resolution -------------------------------------------------------------- #
    async def lavalink_search(self, query: str, prefix: str | None) -> Any:
        await self.require_node()
        if prefix:
            coro = wavelink.Playable.search(query, source=prefix)
        else:
            coro = wavelink.Playable.search(query)
        return await asyncio.wait_for(coro, timeout=SEARCH_TIMEOUT)

    async def try_source(self, label: str, prefix: str | None, query: str) -> Resolution | None:
        try:
            found = await self.lavalink_search(query, prefix)
        except asyncio.TimeoutError:
            log.debug("source=%s timed out after %.0fs", label, SEARCH_TIMEOUT)
            return None
        except wavelink.exceptions.LavalinkLoadException as exc:
            log.debug("source=%s failed: %s", label, one_line(exc))
            return None
        except wavelink.exceptions.LavalinkException as exc:
            log.debug("source=%s lavalink error: %s", label, one_line(exc))
            return None
        except UserFacingError:
            raise
        except Exception as exc:
            log.warning("source=%s unexpected error: %s", label, one_line(exc))
            return None
        if isinstance(found, wavelink.Playlist):
            tracks = list(found.tracks)
            if tracks:
                return Resolution(tracks[:MAX_QUEUE], label, found.name)
        elif found:
            return Resolution([found[0]], label)
        log.debug("source=%s returned no results", label)
        return None

    async def ytdlp_extract(self, query: str) -> dict[str, Any] | None:
        try:
            return await asyncio.wait_for(asyncio.to_thread(_ytdlp_extract_sync, query), timeout=YTDLP_TIMEOUT)
        except asyncio.TimeoutError:
            log.debug("source=yt-dlp timed out after %.0fs", YTDLP_TIMEOUT)
        except Exception as exc:
            log.debug("source=yt-dlp failed: %s", one_line(exc))
        return None

    async def ytdlp_to_resolution(self, info: dict[str, Any]) -> Resolution | None:
        try:
            found = await self.lavalink_search(info["url"], None)
        except UserFacingError:
            raise
        except Exception as exc:
            log.debug("source=yt-dlp stream rejected by lavalink: %s", one_line(exc))
            return None
        if not found or isinstance(found, wavelink.Playlist):
            return None
        track = found[0]
        raw = track_to_raw(track)
        if raw is not None:
            raw["info"].update(
                {
                    "title": info["title"],
                    "author": info["author"],
                    "uri": info["webpage"],
                    "artworkUrl": info.get("thumbnail"),
                }
            )
            patched = track_from_raw(raw)
            if patched is not None:
                track = patched
        return Resolution([track], "yt-dlp")

    async def resolve(self, query: str, exclude: frozenset[str] = frozenset()) -> Resolution | None:
        """Try every source in order; the first that yields tracks wins."""
        query = query.strip()
        search_term = query
        ytdlp_info: dict[str, Any] | None = None

        if URL_RE.match(query):
            direct = await self.try_source("Direct link", None, query)
            if direct:
                log.info("resolved source=%s tracks=%d", direct.source, len(direct.tracks))
                return direct
            ytdlp_info = await self.ytdlp_extract(query)
            if ytdlp_info is None:
                return None
            search_term = f"{ytdlp_info['title']} {ytdlp_info['author']}".strip()

        for spec in SOURCE_CHAIN:
            if spec.source_name in exclude:
                continue
            result = await self.try_source(spec.label, spec.prefix, search_term)
            if result:
                log.info("resolved source=%s tracks=%d", result.source, len(result.tracks))
                return result

        if "http" in exclude:
            return None
        if ytdlp_info is None:
            ytdlp_info = await self.ytdlp_extract(search_term)
        if ytdlp_info is not None:
            result = await self.ytdlp_to_resolution(ytdlp_info)
            if result:
                log.info("resolved source=%s tracks=%d", result.source, len(result.tracks))
                return result
        log.info("no source could resolve the request")
        return None

    async def fallback_track(self, player: BotPlayer, failed: wavelink.Playable) -> wavelink.Playable | None:
        key = failed.identifier or failed.title
        if key in player.fallbacks_tried:
            return None
        player.fallbacks_tried.add(key)
        await self.notify(
            player, f"⚠️ **{discord.utils.escape_markdown(failed.title)}** failed to stream; trying another source…"
        )
        try:
            result = await self.resolve(f"{failed.title} {failed.author}", exclude=frozenset({failed.source or ""}))
        except Exception as exc:
            log.debug("fallback resolve failed: %s", one_line(exc))
            return None
        return result.tracks[0] if result else None

    # ---- playback core ---------------------------------------------------------------------- #
    async def play_next(self, player: BotPlayer) -> bool:
        if player.queue.is_empty:
            return False
        track = player.queue.get()
        player.user_paused = False
        try:
            await player.play(track, volume=player.volume_pref, paused=bool(player.auto_pause_reasons))
        except BaseException:
            player.queue.put_at(0, track)
            raise
        return True

    async def advance(self, player: BotPlayer, ended: wavelink.Playable | None, reason: str) -> None:
        guild = player.guild
        if guild is None or not player.connected or guild.id in self.bot.leaving:
            return
        player.remember(ended)

        candidate: wavelink.Playable | None = None
        if ended is not None and reason == "finished" and player.loop_mode == "track":
            candidate = ended
        else:
            if ended is not None and player.loop_mode == "queue" and reason in ("finished", "stopped"):
                player.queue.put(ended)
            if ended is not None and reason == "loadfailed":
                candidate = await self.fallback_track(player, ended)

        for _ in range(6):
            track = candidate
            candidate = None
            if track is None:
                if player.queue.is_empty:
                    break
                track = player.queue.get()
            try:
                player.user_paused = False
                await player.play(track, volume=player.volume_pref, paused=bool(player.auto_pause_reasons))
                return
            except wavelink.exceptions.LavalinkException as exc:
                if is_stale_session(exc):
                    player.queue.put_at(0, track)
                    await self.recover_lavalink("404 while advancing queue", rebuild_node=True)
                    return
                log.warning("could not play queued track title=%r err=%s", track.title, one_line(exc))
            except Exception as exc:
                log.warning("unexpected play failure title=%r err=%s", track.title, one_line(exc))

        if player.autoplay_enabled and ended is not None and await self.autoplay_next(player, ended):
            return
        if not player.autoplay_enabled:
            await self.notify(player, "✅ Queue finished.")
        self.schedule_idle(player)
        await self.persist(player)

    async def autoplay_next(self, player: BotPlayer, seed: wavelink.Playable) -> bool:
        queries = [
            f"{seed.author} mix",
            f"{seed.title} {seed.author} similar songs",
            seed.author or seed.title,
        ]
        seen = set(player.history_keys)
        for query in queries:
            try:
                result = await self.resolve(query)
            except Exception as exc:
                log.debug("autoplay search failed: %s", one_line(exc))
                continue
            if result is None:
                continue
            pool_tracks = [
                track
                for track in result.tracks
                if (track.identifier or track.title.lower()) not in seen
                and track.title.lower() not in seen
                and not track.is_stream
                and 60_000 <= track.length <= 15 * 60_000
            ]
            if not pool_tracks:
                continue
            choice = random.choice(pool_tracks[:5])
            player.queue.put(choice)
            try:
                await self.play_next(player)
            except Exception as exc:
                log.warning("autoplay could not start track: %s", one_line(exc))
                return False
            await self.notify(player, f"🔁 Autoplay: {track_line(choice)}")
            return True
        return False

    # ---- wavelink events ---------------------------------------------------------------------- #
    @commands.Cog.listener()
    async def on_wavelink_node_ready(self, payload: wavelink.NodeReadyEventPayload) -> None:
        log.info("lavalink node ready identifier=%s resumed=%s", payload.node.identifier, payload.resumed)
        await self.verify_lavalink_version(payload.node)
        if self.bot.recovering:
            return
        if not self.bot.initial_restore_done:
            self.bot.initial_restore_done = True
            self.spawn(self._initial_restore())
        elif not payload.resumed:
            self.spawn(self.recover_lavalink("node session not resumed", rebuild_node=False))

    async def _initial_restore(self) -> None:
        await self.bot.wait_until_ready()
        await self.restore_state()

    @commands.Cog.listener()
    async def on_wavelink_node_closed(self, node: wavelink.Node, disconnected: list[wavelink.Player]) -> None:
        log.warning("lavalink node closed identifier=%s affected_players=%d", node.identifier, len(disconnected))

    @commands.Cog.listener()
    async def on_lavalink_session_stale(self, player: BotPlayer) -> None:
        await self.recover_lavalink("stale session on voice update", rebuild_node=True)

    @commands.Cog.listener()
    async def on_wavelink_websocket_closed(self, payload: wavelink.WebsocketClosedEventPayload) -> None:
        code = getattr(payload, "code", None)
        reason = getattr(payload, "reason", "")
        if code == 4017:
            log.error("discord voice gateway closed with 4017 (DAVE required); lavalink is too old reason=%s", reason)
        else:
            log.warning("discord voice websocket closed code=%s reason=%s", code, reason)

    @commands.Cog.listener()
    async def on_wavelink_track_start(self, payload: wavelink.TrackStartEventPayload) -> None:
        player = payload.player
        if not isinstance(player, BotPlayer) or player.guild is None:
            return
        track = payload.track
        self.cancel_task(self.idle_tasks, player.guild.id)
        log.info("track started guild=%s title=%r source=%s", player.guild.id, track.title, track.source)
        if not (player.loop_mode == "track" and player.last_started == track.identifier):
            await self.notify(player, embed=self.now_playing_embed(player, track))
        player.last_started = track.identifier
        await self.persist(player)

    @commands.Cog.listener()
    async def on_wavelink_track_end(self, payload: wavelink.TrackEndEventPayload) -> None:
        player = payload.player
        if not isinstance(player, BotPlayer) or player.guild is None:
            return
        reason = (payload.reason or "").replace("_", "").lower()
        if reason in ("replaced", "cleanup"):
            return
        if player.suppress_advance:
            player.suppress_advance = False
            await self.persist(player)
            return
        try:
            await self.advance(player, payload.track, reason)
        except Exception as exc:
            log.error("advance failed guild=%s err=%s", player.guild.id, one_line(exc))

    @commands.Cog.listener()
    async def on_wavelink_track_exception(self, payload: wavelink.TrackExceptionEventPayload) -> None:
        player = payload.player
        guild_id = player.guild.id if isinstance(player, wavelink.Player) and player.guild else None
        log.warning(
            "track exception guild=%s title=%r source=%s detail=%s",
            guild_id,
            payload.track.title,
            payload.track.source,
            one_line(str(payload.exception)),
        )

    @commands.Cog.listener()
    async def on_wavelink_track_stuck(self, payload: wavelink.TrackStuckEventPayload) -> None:
        player = payload.player
        if not isinstance(player, BotPlayer) or player.guild is None:
            return
        log.warning("track stuck guild=%s title=%r; skipping", player.guild.id, payload.track.title)
        try:
            await player.skip(force=True)
        except Exception as exc:
            log.warning("skip after stuck track failed: %s", one_line(exc))

    @commands.Cog.listener()
    async def on_voice_state_update(
        self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState
    ) -> None:
        guild = member.guild
        if self.bot.user and member.id == self.bot.user.id:
            if before.channel is not None and after.channel is None and guild.id not in self.bot.leaving:
                await self.handle_external_disconnect(guild)
            elif after.channel is not None and guild.id not in self.bot.leaving:
                own = guild.voice_client
                if isinstance(own, BotPlayer):
                    await self.update_listener_state(own)  # server mute/deafen or moved by a moderator
            return
        vc = guild.voice_client
        if not isinstance(vc, BotPlayer) or vc.channel is None:
            return
        touched = {before.channel.id if before.channel else None, after.channel.id if after.channel else None}
        if vc.channel.id in touched:
            await self.update_listener_state(vc)

    async def handle_external_disconnect(self, guild: discord.Guild) -> None:
        saved = self.bot.store.data["guilds"].get(str(guild.id))
        self.cancel_task(self.idle_tasks, guild.id)
        if saved and saved.get("stay_247"):
            log.warning("disconnected from voice in 24/7 mode guild=%s; rejoining", guild.id)
            self.spawn(self.rejoin_247(guild.id))
            return
        log.info("disconnected from voice by someone else guild=%s", guild.id)
        self.bot.store.drop_guild(guild.id)
        await self.bot.store.flush()

    async def rejoin_247(self, guild_id: int) -> None:
        await asyncio.sleep(3)
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return
        vc = guild.voice_client
        if isinstance(vc, BotPlayer) and vc.connected:
            return
        saved = self.bot.store.data["guilds"].get(str(guild_id))
        if saved:
            await self.restore_guild(guild_id, saved)

    # ---- embeds --------------------------------------------------------------------------------- #
    def now_playing_embed(self, player: BotPlayer, track: wavelink.Playable) -> discord.Embed:
        embed = discord.Embed(title="Now playing", description=track_line(track), color=EMBED_COLOR)
        embed.add_field(name="Artist", value=discord.utils.escape_markdown(track.author or "Unknown"), inline=True)
        embed.add_field(name="Length", value="LIVE" if track.is_stream else fmt_ms(track.length), inline=True)
        embed.add_field(name="Source", value=str(track.source or "unknown"), inline=True)
        if track.artwork:
            embed.set_thumbnail(url=track.artwork)
        embed.set_footer(
            text=f"Volume {player.volume_pref}% • Loop: {player.loop_mode} • Filter: {player.filter_name}"
            f" • Autoplay: {'on' if player.autoplay_enabled else 'off'}"
        )
        return embed

    # ---- lyrics ---------------------------------------------------------------------------------- #
    @staticmethod
    def split_artist_title(title: str, author: str) -> tuple[str, str]:
        cleaned = re.sub(r"[\(\[\{].*?[\)\]\}]", "", title)
        cleaned = re.sub(
            r"(?i)\b(official|video|audio|lyrics?|hd|hq|4k|remastered|music video)\b", "", cleaned
        )
        cleaned = re.sub(r"\s+", " ", cleaned).strip(" -–|")
        if " - " in cleaned:
            artist, song = cleaned.split(" - ", 1)
            return artist.strip(), song.strip()
        artist = re.sub(r"(?i)\s*(-\s*topic|vevo|official)$", "", author or "").strip()
        return artist, cleaned

    async def fetch_lyrics(self, title: str, author: str, length_ms: int) -> str | None:
        session = self.bot.session
        if session is None:
            return None
        artist, song = self.split_artist_title(title, author)

        async def lrclib_get() -> str | None:
            params = {"artist_name": artist, "track_name": song}
            if length_ms:
                params["duration"] = str(int(length_ms / 1000))
            async with session.get("https://lrclib.net/api/get", params=params) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json()).get("plainLyrics")

        async def lrclib_search() -> str | None:
            async with session.get("https://lrclib.net/api/search", params={"q": f"{artist} {song}".strip()}) as resp:
                if resp.status != 200:
                    return None
                for item in await resp.json():
                    if item.get("plainLyrics"):
                        return item["plainLyrics"]
            return None

        async def lyrics_ovh() -> str | None:
            url = f"https://api.lyrics.ovh/v1/{quote(artist, safe='')}/{quote(song, safe='')}"
            async with session.get(url) as resp:
                if resp.status != 200:
                    return None
                return (await resp.json()).get("lyrics")

        for provider in (lrclib_get, lrclib_search, lyrics_ovh):
            try:
                text = await provider()
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
                log.debug("lyrics provider=%s failed: %s", provider.__name__, one_line(exc))
                continue
            if text and text.strip():
                return text.strip()
        return None

    # ====================================================================================== #
    # Commands
    # ====================================================================================== #
    @commands.hybrid_command(name="help", description="Show all commands")
    async def help_command(self, ctx: Ctx) -> None:
        p = ctx.clean_prefix
        embed = discord.Embed(title="Music bot commands", color=EMBED_COLOR)
        embed.add_field(
            name="Playback",
            value=(
                f"`{p}play <song/url>` `{p}pause` `{p}resume` `{p}skip` `{p}stop`\n"
                f"`{p}seek <seconds>` `{p}volume <0-100>` `{p}nowplaying`"
            ),
            inline=False,
        )
        embed.add_field(
            name="Queue",
            value=f"`{p}queue` `{p}shuffle` `{p}loop <track|queue|off>` `{p}autoplay <on|off>`",
            inline=False,
        )
        embed.add_field(
            name="Extras",
            value=(
                f"`{p}filter <name>` `{p}lyrics` `{p}247 <on|off>`\n"
                f"`{p}playlist save|load|list|delete <name>`"
            ),
            inline=False,
        )
        embed.add_field(name="Voice", value=f"`{p}join` `{p}leave`", inline=False)
        embed.set_footer(text="Sources: YouTube Music → SoundCloud → YouTube → yt-dlp")
        await reply(ctx, embed=embed)

    @commands.hybrid_command(name="join", aliases=["summon"], description="Join your voice channel")
    async def join(self, ctx: Ctx) -> None:
        channel = self.user_voice_channel(ctx)
        player = await self.ensure_player(ctx, connect=True)
        if player.current is None:
            self.schedule_idle(player)
        await self.persist(player)
        await reply(ctx, f"🔊 Joined {channel.mention}.")

    @commands.hybrid_command(name="leave", aliases=["disconnect", "dc"], description="Leave the voice channel")
    async def leave(self, ctx: Ctx) -> None:
        self.player_of(ctx)
        await self.leave_guild(ctx.guild)
        await reply(ctx, "👋 Left the voice channel.")

    @commands.hybrid_command(name="play", aliases=["p"], description="Play a song or add it to the queue")
    async def play(self, ctx: Ctx, *, query: str) -> None:
        await ctx.defer()
        self.user_voice_channel(ctx)
        try:
            resolution = await self.resolve(query)
            if resolution is None:
                await reply(
                    ctx,
                    "⚠️ I couldn't find that on YouTube Music, SoundCloud, YouTube or via yt-dlp. "
                    "Try different keywords or a direct link.",
                    ephemeral=True,
                )
                return
            player = await self.ensure_player(ctx, connect=True)
            room = MAX_QUEUE - player.queue.count
            if room <= 0:
                raise UserFacingError(f"The queue is full ({MAX_QUEUE} tracks).")
            tracks = resolution.tracks[:room]
            was_idle = player.current is None
            for track in tracks:
                player.queue.put(track)
            if was_idle:
                await self.guarded(ctx, self.play_next, what="play")
            player = self.player_of(ctx)
            await self.persist(player)
            self.cancel_task(self.idle_tasks, ctx.guild.id)
            if resolution.playlist_name:
                await reply(
                    ctx, f"📃 Queued **{len(tracks)}** tracks from **{discord.utils.escape_markdown(resolution.playlist_name)}**."
                )
            elif was_idle:
                await reply(ctx, f"▶️ Now playing {track_line(tracks[0])} *(via {resolution.source})*")
            else:
                await reply(
                    ctx,
                    f"➕ Queued {track_line(tracks[0])} at position **{player.queue.count}** *(via {resolution.source})*",
                )
        except wavelink.exceptions.LavalinkLoadException as exc:
            log.debug("play load failure: %s", one_line(exc))
            await reply(ctx, "⚠️ That track couldn't be loaded. Try another one.", ephemeral=True)
        except wavelink.exceptions.ChannelTimeoutException:
            log.warning("voice connect gave up guild=%s", ctx.guild.id)
            await reply(ctx, "⚠️ I couldn't connect to the voice channel in time. Please try again.", ephemeral=True)
        except asyncio.TimeoutError:
            log.warning("play timed out guild=%s", ctx.guild.id)
            await reply(ctx, "⚠️ That took too long. Please try again.", ephemeral=True)
        except discord.Forbidden:
            log.warning("play forbidden guild=%s", ctx.guild.id)
            await reply(
                ctx, "⚠️ I'm missing permissions (Connect/Speak in the voice channel, or Send Messages here).", ephemeral=True
            )

    @commands.hybrid_command(name="stop", description="Stop playback and clear the queue")
    async def stop(self, ctx: Ctx) -> None:
        player = await self.ensure_player(ctx, connect=False)
        player.queue.clear()
        player.loop_mode = "off"
        player.fallbacks_tried.clear()
        if player.current is not None:
            player.suppress_advance = True
        try:
            await self.guarded(ctx, lambda p: p.skip(force=True), what="stop")
        except BaseException:
            player.suppress_advance = False
            raise
        self.schedule_idle(player)
        await self.persist(player)
        await reply(ctx, "⏹️ Stopped and cleared the queue.")

    @commands.hybrid_command(name="skip", aliases=["s", "next"], description="Skip the current track")
    async def skip(self, ctx: Ctx) -> None:
        player = await self.ensure_player(ctx, connect=False)
        current = player.current
        if current is None:
            raise UserFacingError("Nothing is playing right now.")
        await self.guarded(ctx, lambda p: p.skip(force=True), what="skip")
        await reply(ctx, f"⏭️ Skipped **{discord.utils.escape_markdown(current.title)}**.")

    @commands.hybrid_command(name="pause", description="Pause playback")
    async def pause(self, ctx: Ctx) -> None:
        player = await self.ensure_player(ctx, connect=False)
        if player.current is None:
            raise UserFacingError("Nothing is playing right now.")
        player.user_paused = True
        await self.guarded(ctx, lambda p: p.pause(True), what="pause")
        await self.persist(self.player_of(ctx))
        await reply(ctx, "⏸️ Paused.")

    @commands.hybrid_command(name="resume", aliases=["unpause"], description="Resume playback")
    async def resume(self, ctx: Ctx) -> None:
        player = await self.ensure_player(ctx, connect=False)
        if player.current is None:
            raise UserFacingError("Nothing is playing right now.")
        player.user_paused = False
        if player.auto_pause_reasons:
            await self.persist(player)
            await reply(ctx, f"⏸️ I'll resume automatically once {'I am unmuted' if 'muted' in player.auto_pause_reasons else 'someone is listening'}.")
            return
        await self.guarded(ctx, lambda p: p.pause(False), what="resume")
        await self.persist(self.player_of(ctx))
        await reply(ctx, "▶️ Resumed.")

    @commands.hybrid_command(name="queue", aliases=["q"], description="Show the queue")
    async def queue(self, ctx: Ctx) -> None:
        player = self.player_of(ctx)
        upcoming = list(player.queue)
        if player.current is None and not upcoming:
            await reply(ctx, "The queue is empty.")
            return
        embed = discord.Embed(title="Queue", color=EMBED_COLOR)
        if player.current is not None:
            embed.add_field(name="Now playing", value=track_line(player.current), inline=False)
        if upcoming:
            lines = [f"`{index}.` {track_line(track)} `{fmt_ms(track.length)}`" for index, track in enumerate(upcoming[:10], 1)]
            if len(upcoming) > 10:
                lines.append(f"…and **{len(upcoming) - 10}** more")
            embed.add_field(name="Up next", value="\n".join(lines)[:1024], inline=False)
            total = sum(track.length for track in upcoming if not track.is_stream)
            embed.set_footer(text=f"{len(upcoming)} tracks • {fmt_ms(total)} • Loop: {player.loop_mode}")
        await reply(ctx, embed=embed)

    @commands.hybrid_command(name="nowplaying", aliases=["np", "current"], description="Show the current track")
    async def nowplaying(self, ctx: Ctx) -> None:
        player = self.player_of(ctx)
        track = player.current
        if track is None:
            raise UserFacingError("Nothing is playing right now.")
        embed = self.now_playing_embed(player, track)
        if not track.is_stream and track.length:
            position = min(int(player.position), track.length)
            filled = int(14 * position / track.length)
            bar = "▬" * filled + "🔘" + "▬" * (14 - filled)
            embed.add_field(name="Progress", value=f"`{fmt_ms(position)}` {bar} `{fmt_ms(track.length)}`", inline=False)
        await reply(ctx, embed=embed)

    @commands.hybrid_command(name="volume", aliases=["vol"], description="Show or set the volume (0-100)")
    async def volume(self, ctx: Ctx, level: Optional[int] = None) -> None:
        player = await self.ensure_player(ctx, connect=False)
        if level is None:
            await reply(ctx, f"🔉 Volume is **{player.volume_pref}%**.")
            return
        if not 0 <= level <= 100:
            raise UserFacingError("Volume must be between 0 and 100.")
        player.volume_pref = level
        if player.current is not None:
            await self.guarded(ctx, lambda p: p.set_volume(level), what="volume")
        await self.persist(self.player_of(ctx))
        await reply(ctx, f"🔊 Volume set to **{level}%**.")

    @commands.hybrid_command(name="loop", aliases=["repeat"], description="Loop the track, the queue, or turn looping off")
    async def loop(self, ctx: Ctx, mode: LoopMode) -> None:
        player = await self.ensure_player(ctx, connect=False)
        player.loop_mode = mode
        await self.persist(player)
        labels = {"track": "🔂 Looping the current track.", "queue": "🔁 Looping the whole queue.", "off": "➡️ Loop disabled."}
        await reply(ctx, labels[mode])

    @commands.hybrid_command(name="shuffle", description="Shuffle the queue")
    async def shuffle(self, ctx: Ctx) -> None:
        player = await self.ensure_player(ctx, connect=False)
        if player.queue.count < 2:
            raise UserFacingError("I need at least two queued tracks to shuffle.")
        player.queue.shuffle()
        await self.persist(player)
        await reply(ctx, f"🔀 Shuffled **{player.queue.count}** tracks.")

    @commands.hybrid_command(name="seek", description="Seek to a position in seconds")
    async def seek(self, ctx: Ctx, seconds: int) -> None:
        player = await self.ensure_player(ctx, connect=False)
        track = player.current
        if track is None:
            raise UserFacingError("Nothing is playing right now.")
        if not track.is_seekable:
            raise UserFacingError("This track can't be seeked.")
        if seconds < 0 or seconds * 1000 >= track.length:
            raise UserFacingError(f"Pick a position between 0 and {track.length // 1000 - 1} seconds.")
        await self.guarded(ctx, lambda p: p.seek(seconds * 1000), what="seek")
        await self.persist(self.player_of(ctx))
        await reply(ctx, f"⏩ Jumped to **{fmt_ms(seconds * 1000)}**.")

    @commands.hybrid_command(name="lyrics", description="Fetch lyrics for the current track")
    async def lyrics(self, ctx: Ctx) -> None:
        player = self.player_of(ctx)
        track = player.current
        if track is None:
            raise UserFacingError("Nothing is playing right now.")
        await ctx.defer()
        text = await self.fetch_lyrics(track.title, track.author, track.length)
        if not text:
            await reply(ctx, "⚠️ I couldn't find lyrics for this track.", ephemeral=True)
            return
        chunks: list[str] = []
        current_chunk = ""
        for line in text.splitlines():
            if len(current_chunk) + len(line) + 1 > 4000:
                chunks.append(current_chunk)
                current_chunk = ""
            current_chunk += line + "\n"
        if current_chunk.strip():
            chunks.append(current_chunk)
        for index, chunk in enumerate(chunks[:3]):
            embed = discord.Embed(
                title=f"Lyrics: {track.title[:200]}" if index == 0 else None,
                description=chunk.strip(),
                color=EMBED_COLOR,
            )
            if index == 2 and len(chunks) > 3:
                embed.set_footer(text="Lyrics truncated")
            await reply(ctx, embed=embed)

    @commands.hybrid_command(name="filter", aliases=["fx"], description="Apply an audio filter")
    async def filter(self, ctx: Ctx, name: FilterName) -> None:
        player = await self.ensure_player(ctx, connect=False)
        await self.guarded(ctx, lambda p: p.set_filters(build_filters(name), seek=True), what="filter")
        player = self.player_of(ctx)
        player.filter_name = name
        await self.persist(player)
        await reply(ctx, "🎛️ Filters cleared." if name == "off" else f"🎛️ Filter **{name}** applied.")

    @commands.hybrid_command(name="247", description="Stay in the voice channel permanently")
    async def twentyfour_seven(self, ctx: Ctx, mode: Switch) -> None:
        player = await self.ensure_player(ctx, connect=(mode == "on"))
        player.stay_247 = mode == "on"
        if player.stay_247:
            self.cancel_task(self.idle_tasks, ctx.guild.id)
            self.cancel_task(self.empty_tasks, ctx.guild.id)
        elif player.current is None:
            self.schedule_idle(player)
        await self.persist(player)
        await reply(ctx, "🕒 24/7 mode **enabled**. I'll stay in the channel." if player.stay_247 else "🕒 24/7 mode **disabled**.")

    @commands.hybrid_command(name="autoplay", description="Automatically queue similar songs when the queue ends")
    async def autoplay(self, ctx: Ctx, mode: Switch) -> None:
        player = await self.ensure_player(ctx, connect=False)
        player.autoplay_enabled = mode == "on"
        await self.persist(player)
        await reply(ctx, "♾️ Autoplay **enabled**." if player.autoplay_enabled else "♾️ Autoplay **disabled**.")

    # ---- playlists ---------------------------------------------------------------------------------- #
    @staticmethod
    def playlist_key(name: str) -> str:
        key = " ".join(name.strip().lower().split())[:32]
        if not key:
            raise UserFacingError("Please provide a playlist name.")
        return key

    def user_playlists(self, user_id: int) -> dict[str, list[dict[str, Any]]]:
        return self.bot.store.data["playlists"].setdefault(str(user_id), {})

    @commands.hybrid_group(
        name="playlist", aliases=["pl"], invoke_without_command=True, description="Manage your saved playlists"
    )
    async def playlist(self, ctx: Ctx) -> None:
        p = ctx.clean_prefix
        await reply(ctx, f"Usage: `{p}playlist save|load|list|delete <name>`", ephemeral=True)

    @playlist.command(name="save", description="Save the current track and queue as a playlist")
    async def playlist_save(self, ctx: Ctx, *, name: str) -> None:
        key = self.playlist_key(name)
        player = self.player_of(ctx)
        tracks = ([player.current] if player.current else []) + list(player.queue)
        raws = [raw for raw in (track_to_raw(t) for t in tracks[:MAX_PLAYLIST_TRACKS]) if raw]
        if not raws:
            raise UserFacingError("There's nothing playing or queued to save.")
        playlists = self.user_playlists(ctx.author.id)
        if key not in playlists and len(playlists) >= MAX_PLAYLISTS_PER_USER:
            raise UserFacingError(f"You can store up to {MAX_PLAYLISTS_PER_USER} playlists. Delete one first.")
        playlists[key] = raws
        await self.bot.store.flush()
        await reply(ctx, f"💾 Saved **{len(raws)}** tracks as playlist **{discord.utils.escape_markdown(key)}**.")

    @playlist.command(name="load", description="Queue one of your saved playlists")
    async def playlist_load(self, ctx: Ctx, *, name: str) -> None:
        key = self.playlist_key(name)
        raws = self.user_playlists(ctx.author.id).get(key)
        if not raws:
            raise UserFacingError(f"You don't have a playlist named **{discord.utils.escape_markdown(key)}**.")
        await ctx.defer()
        tracks = [t for t in (track_from_raw(raw) for raw in raws) if t]
        if not tracks:
            raise UserFacingError("That playlist's saved data is unreadable. Please save it again.")
        player = await self.ensure_player(ctx, connect=True)
        tracks = tracks[: MAX_QUEUE - player.queue.count]
        was_idle = player.current is None
        for track in tracks:
            player.queue.put(track)
        if was_idle:
            await self.guarded(ctx, self.play_next, what="playlist load")
        self.cancel_task(self.idle_tasks, ctx.guild.id)
        await self.persist(self.player_of(ctx))
        await reply(ctx, f"📃 Loaded **{len(tracks)}** tracks from **{discord.utils.escape_markdown(key)}**.")

    @playlist.command(name="list", description="List your saved playlists")
    async def playlist_list(self, ctx: Ctx) -> None:
        playlists = self.user_playlists(ctx.author.id)
        if not playlists:
            await reply(ctx, "You have no saved playlists yet.", ephemeral=True)
            return
        lines = [f"• **{discord.utils.escape_markdown(name)}** — {len(items)} tracks" for name, items in sorted(playlists.items())]
        embed = discord.Embed(title="Your playlists", description="\n".join(lines)[:4000], color=EMBED_COLOR)
        await reply(ctx, embed=embed, ephemeral=True)

    @playlist.command(name="delete", description="Delete a saved playlist")
    async def playlist_delete(self, ctx: Ctx, *, name: str) -> None:
        key = self.playlist_key(name)
        playlists = self.user_playlists(ctx.author.id)
        if key not in playlists:
            raise UserFacingError(f"You don't have a playlist named **{discord.utils.escape_markdown(key)}**.")
        del playlists[key]
        await self.bot.store.flush()
        await reply(ctx, f"🗑️ Deleted playlist **{discord.utils.escape_markdown(key)}**.")


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #
def main() -> None:
    configure_logging()
    missing = [name for name, value in (("DISCORD_TOKEN", DISCORD_TOKEN), ("LAVALINK_PASSWORD", LAVALINK_PASSWORD)) if not value]
    if missing:
        log.error("missing required environment variables: %s", ", ".join(missing))
        sys.exit(1)
    bot = MusicBot()
    bot.run(DISCORD_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
