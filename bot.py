import asyncio
import html
import logging
import os
import random
import re
import shutil
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass

import aiohttp
import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()  # DISCORD_TOKEN берётся из .env
TOKEN = os.getenv("DISCORD_TOKEN")
VK_TOKEN = os.getenv("VK_TOKEN")  # необязательно: токен Kate Mobile, чтобы понимать ссылки на музыку ВК

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(name)s: %(message)s")
if os.getenv("VOICE_DEBUG"):  # подробный лог голосового соединения: docker run -e VOICE_DEBUG=1 ...
    for name in ("discord.voice_state", "discord.gateway", "discord.player"):
        logging.getLogger(name).setLevel(logging.DEBUG)
log = logging.getLogger("dj-bot")

# ---------- настройки ----------

ALONE_TIMEOUT = 60              # сек: выйти, если в канале никого не осталось
IDLE_TIMEOUT = 300              # сек: выйти, если очередь кончилась и ничего не играет
PANEL_REFRESH = 15              # сек: как часто обновлять панель (чаще — упрёмся в лимиты Discord)
PREFETCH_BEFORE_END = 20        # сек: за сколько до конца трека начинать грузить следующий
MAX_PLAYLIST = 100              # сколько треков максимум брать из плейлиста
HISTORY_SIZE = 25               # сколько сыгранных треков помнить для ⏮️ и /history
AUTOPLAY_BATCH = 3              # сколько похожих треков добавлять автоплеем за раз
YTDLP_UPDATE_EVERY = 24 * 3600  # сек: как часто проверять обновления yt-dlp (только в Docker)
PANEL_COLOR = 0x1D9E75

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "source_address": "0.0.0.0",
    "extractor_retries": 3,
    "socket_timeout": 10,
}
# для плейлистов, автоплея и подсказок: только список треков, без загрузки каждого
YTDL_FLAT_OPTS = {**YTDL_OPTS, "noplaylist": False, "extract_flat": "in_playlist"}

ytdl = yt_dlp.YoutubeDL(YTDL_OPTS)
ytdl_flat = yt_dlp.YoutubeDL(YTDL_FLAT_OPTS)

LOOP_NEXT = {"off": "queue", "queue": "track", "track": "off"}
LOOP_LABEL = {"off": "выкл", "queue": "очередь", "track": "трек"}
AUTOPLAY_REQUESTER = "📻 автоплей"

STALKER_QUOTES = [
    "Зона не прощает ошибок. Только — шансов.",
    "Снова слышу звон... Кажется, 'Грави' где-то рядом.",
    "Не верь Клыку — он с Монолитом.",
    "В Зоне выживает не сильнейший, а тот, кто не лезет без нужды.",
    "Артефакт — не цель. Выживание — цель.",
    "Тишина в Зоне страшнее выстрелов.",
    "Если видишь аномалию — бросай болт. Если не видишь — бросай два.",
    "Монолит зовёт... но я не пойду.",
    "Карта — ложь. Глаза — правда.",
    "Смерть в Зоне приходит тихо. И всегда — сзади.",
]

VIDEO_ID_RE = re.compile(r"(?:v=|youtu\.be/|/shorts/|/live/)([\w-]{11})")
SPOTIFY_RE = re.compile(r"open\.spotify\.com/(?:intl-[\w-]+/)?(track|album|playlist|artist)/")
YANDEX_TRACK_RE = re.compile(r"music\.yandex\.\w+/(?:.*/)?track/(\d+)")
VK_LINK_RE = re.compile(r"vk\.com|vk\.ru|^audio-?\d+_")
VK_AUDIO_RE = re.compile(r"audio(-?\d+_\d+(?:_[0-9a-f]+)?)")
VK_UA = "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)"
HTTP_HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"}


# ---------- вспомогательное ----------

def fmt_time(seconds) -> str:
    if seconds is None:
        return "--:--"
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02}:{s:02}" if h else f"{m}:{s:02}"


def progress_bar(elapsed: float, duration, size: int = 16) -> str:
    if not duration:
        return "🔴 эфир"
    pos = min(int(elapsed / duration * (size - 1)), size - 1)
    return "▬" * pos + "🔘" + "▬" * (size - 1 - pos)


def yt_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


@dataclass
class Track:
    title: str
    page_url: str
    duration: int | None
    requester: str
    thumbnail: str | None = None

    @property
    def video_id(self) -> str | None:
        m = VIDEO_ID_RE.search(self.page_url)
        return m.group(1) if m else None

    def label(self) -> str:
        return f"**{self.title}** `{fmt_time(self.duration)}`"


def track_from_flat(e: dict, requester: str) -> Track:
    return Track(
        title=e.get("title") or "Без названия",
        page_url=e.get("url") if str(e.get("url", "")).startswith("http") else yt_url(e.get("id")),
        duration=e.get("duration"),
        requester=requester,
        thumbnail=(e.get("thumbnails") or [{}])[-1].get("url"),
    )


def open_audio(url: str) -> tuple[subprocess.Popen, discord.FFmpegPCMAudio]:
    """yt-dlp качает звук кусками и отдаёт его ffmpeg через трубу.
    Прямую ссылку ffmpeg YouTube теперь часто режет ошибкой 403."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
         "--downloader", "native", "--http-chunk-size", "10M",
         "-f", "bestaudio/best", "-o", "-", url],
        stdout=subprocess.PIPE,
    )
    audio = discord.FFmpegPCMAudio(proc.stdout, pipe=True, before_options="-nostdin", options="-vn")
    return proc, audio


@dataclass
class Prepared:
    """Трек, который уже начали грузить заранее."""
    track: Track
    proc: subprocess.Popen
    audio: discord.FFmpegPCMAudio

    def kill(self):
        try:
            self.audio.cleanup()
        except Exception:
            pass
        if self.proc.poll() is None:
            self.proc.kill()


def is_playlist(query: str) -> bool:
    return query.startswith("http") and ("list=" in query or "/playlist" in query or "/sets/" in query)


async def run_ytdl(ydl: yt_dlp.YoutubeDL, query: str) -> dict:
    """yt-dlp в отдельном потоке, чтобы не вешать бота."""
    loop = asyncio.get_running_loop()
    data = await loop.run_in_executor(None, lambda: ydl.extract_info(query, download=False))
    if not data:
        raise LookupError("ничего не найдено")
    return data


async def extract(query: str) -> dict:
    data = await run_ytdl(ytdl, query)
    if "entries" in data:
        entries = [e for e in data["entries"] if e]
        if not entries:
            raise LookupError("ничего не найдено")
        data = entries[0]
    return data


async def extract_playlist(query: str) -> tuple[str, list[dict]]:
    data = await run_ytdl(ytdl_flat, query)
    entries = [e for e in (data.get("entries") or []) if e][:MAX_PLAYLIST]
    return data.get("title") or "плейлист", entries


async def fetch(url: str, *, as_json: bool = False):
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(headers=HTTP_HEADERS, timeout=timeout) as s:
        async with s.get(url) as r:
            r.raise_for_status()
            return await r.json(content_type=None) if as_json else await r.text()


async def vk_api(method: str, **params) -> dict:
    timeout = aiohttp.ClientTimeout(total=10)
    data = {**params, "access_token": VK_TOKEN, "v": "5.131"}
    async with aiohttp.ClientSession(headers={"User-Agent": VK_UA}, timeout=timeout) as s:
        async with s.post(f"https://api.vk.com/method/{method}", data=data) as r:
            result = await r.json(content_type=None)
    if "error" in result:
        raise LookupError(f"ВК ответил: {result['error'].get('error_msg', 'ошибка')}")
    return result.get("response")


async def resolve_external(query: str) -> str | None:
    """Ссылку Spotify / Яндекс Музыки превращает в «Исполнитель - Трек» для поиска на YouTube."""
    if "open.spotify.com" in query:
        m = SPOTIFY_RE.search(query)
        if not m or m.group(1) != "track":
            raise LookupError("из Spotify пока понимаю только ссылки на отдельные треки")
        page = await fetch(query)
        t = re.search(r"<title>(.*?)</title>", page, re.S)
        if t:
            title = html.unescape(t.group(1)).strip()
            m2 = re.match(r"(.+?) - song(?: and lyrics)? by (.+?) \| Spotify", title)
            if m2:
                return f"{m2.group(2)} - {m2.group(1)}"
        og = re.search(r'<meta property="og:title" content="([^"]+)"', page)
        if og:
            return html.unescape(og.group(1))
        raise LookupError("не смог прочитать трек со страницы Spotify")

    if VK_LINK_RE.search(query):
        if not VK_TOKEN:
            raise LookupError("vk-no-token")
        m = VK_AUDIO_RE.search(query)
        if not m:
            raise LookupError("из ВК пока понимаю только ссылки на отдельные треки (vk.com/audio…)")
        items = await vk_api("audio.getById", audios=m.group(1)) or []
        if not items:
            raise LookupError("ВК не отдал этот трек — возможно, он удалён или закрыт")
        t = items[0]
        return f"{t.get('artist', '')} - {t.get('title', '')}".strip(" -")

    if "music.yandex." in query:
        m = YANDEX_TRACK_RE.search(query)
        if not m:
            raise LookupError("из Яндекс Музыки пока понимаю только ссылки на отдельные треки")
        data = await fetch(f"https://api.music.yandex.net/tracks/{m.group(1)}", as_json=True)
        t = (data.get("result") or [{}])[0]
        if not t.get("title"):
            raise LookupError("Яндекс Музыка не отдала данные трека")
        artists = ", ".join(a.get("name", "") for a in t.get("artists", []))
        return f"{artists} - {t['title']}" if artists else t["title"]

    return None


def queue_text(p: "GuildPlayer") -> str:
    if p.current is None and not p.queue:
        return "🔇 Очередь пуста."
    lines = []
    if p.current:
        lines.append(f"▶️ Сейчас: {p.current.label()}")
    for i, t in enumerate(list(p.queue)[:10], 1):
        lines.append(f"`{i}.` {t.label()} — {t.requester}")
    if len(p.queue) > 10:
        lines.append(f"…и ещё {len(p.queue) - 10}")
    return "\n".join(lines)


# ---------- плеер ----------

class GuildPlayer:
    """Очередь, воспроизведение и панель для одного сервера."""

    def __init__(self, bot: "DJBot", guild: discord.Guild):
        self.bot = bot
        self.guild = guild
        self.queue: deque[Track] = deque()
        self.history: deque[Track] = deque(maxlen=HISTORY_SIZE)
        self.current: Track | None = None
        self.last_played: Track | None = None   # от него отталкивается автоплей
        self.volume = 0.5
        self.loop_mode = "off"
        self.autoplay = False
        self.text_channel: discord.abc.Messageable | None = None
        self.task: asyncio.Task | None = None
        self.updater: asyncio.Task | None = None
        self.idle_task: asyncio.Task | None = None
        self.prefetched: Prepared | None = None
        self.panel: discord.Message | None = None
        self.view: PlayerView | None = None
        self.skip_requested = False
        self.stop_requested = False
        self.back_requested = False
        self.started_at = 0.0
        self.paused_at: float | None = None
        self.paused_total = 0.0

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def is_paused(self) -> bool:
        return bool(self.vc and self.vc.is_paused())

    def elapsed(self) -> float:
        end = self.paused_at or time.monotonic()
        return max(0.0, end - self.started_at - self.paused_total)

    async def send(self, text: str):
        if self.text_channel:
            try:
                await self.text_channel.send(text)
            except discord.HTTPException:
                pass

    # ---------- управление ----------

    def start(self):
        self.cancel_idle()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._loop())

    def pause(self) -> bool:
        if self.vc and self.vc.is_playing():
            self.vc.pause()
            self.paused_at = time.monotonic()
            return True
        return False

    def resume(self) -> bool:
        if self.vc and self.vc.is_paused():
            self.vc.resume()
            if self.paused_at:
                self.paused_total += time.monotonic() - self.paused_at
            self.paused_at = None
            return True
        return False

    def skip(self) -> bool:
        if self.vc and (self.vc.is_playing() or self.vc.is_paused()):
            self.skip_requested = True
            self.vc.stop()  # after() запустит следующий трек
            return True
        return False

    def previous(self) -> bool:
        """⏮️: вернуть прошлый трек, а текущий поставить следом."""
        if not self.history:
            return False
        prev = self.history.pop()
        if self.current:
            self.queue.appendleft(self.current)
        self.queue.appendleft(prev)
        self.back_requested = self.skip()
        if not self.back_requested:
            self.start()  # ничего не играло — просто запускаем
        return True

    def stop(self):
        self.queue.clear()
        self.last_played = None  # чтобы автоплей не включился сам после стопа
        self.drop_prefetch()
        if self.skip():
            self.stop_requested = True

    def shuffle(self) -> bool:
        if len(self.queue) < 2:
            return False
        items = list(self.queue)
        random.shuffle(items)
        self.queue = deque(items)
        return True

    async def disconnect(self):
        self.stop()
        self.cancel_idle()
        if self.vc:
            await self.vc.disconnect()

    def cleanup(self):
        """Бот вылетел из канала — гасим всё фоновое."""
        self.queue.clear()
        self.drop_prefetch()
        self.cancel_idle()

    # ---------- простой ----------

    def cancel_idle(self):
        if self.idle_task and not self.idle_task.done():
            self.idle_task.cancel()
        self.idle_task = None

    async def _idle_leave(self):
        await asyncio.sleep(IDLE_TIMEOUT)
        if self.current is None and not self.queue and self.vc:
            await self.send(f"💤 Тишина {IDLE_TIMEOUT // 60} мин — ухожу из канала. `/play`, чтобы вернуть.")
            await self.disconnect()

    # ---------- подгрузка следующего трека ----------

    def drop_prefetch(self):
        if self.prefetched:
            self.prefetched.kill()
            self.prefetched = None

    def take_prefetched(self, track: Track) -> Prepared | None:
        prep, self.prefetched = self.prefetched, None
        if prep and prep.track is track and prep.proc.poll() in (None, 0):
            return prep
        if prep:
            prep.kill()  # очередь успели поменять — заготовка не пригодилась
        return None

    async def _prefetch_near_end(self, track: Track):
        while self.current is track:
            if track.duration and self.queue and not self.is_paused:
                if track.duration - self.elapsed() <= PREFETCH_BEFORE_END:
                    nxt = self.queue[0]
                    if not (self.prefetched and self.prefetched.track is nxt):
                        self.drop_prefetch()
                        try:
                            proc, audio = open_audio(nxt.page_url)
                            self.prefetched = Prepared(nxt, proc, audio)
                            log.info("Заранее гружу: %s", nxt.title)
                        except Exception as e:
                            log.warning("Не смог заранее загрузить %s: %s", nxt.title, e)
                    return
            await asyncio.sleep(2)

    # ---------- автоплей ----------

    async def autoplay_fill(self) -> bool:
        """Добавляет в очередь похожие треки из YouTube-микса последнего трека."""
        seed = self.last_played
        vid = seed.video_id if seed else None
        if not vid:
            return False
        data = await run_ytdl(ytdl_flat, f"{yt_url(vid)}&list=RD{vid}")
        seen = {t.video_id for t in self.history} | {vid}
        added = 0
        for e in data.get("entries") or []:
            if not e or not e.get("id") or e["id"] in seen:
                continue
            self.queue.append(track_from_flat(e, AUTOPLAY_REQUESTER))
            seen.add(e["id"])
            added += 1
            if added >= AUTOPLAY_BATCH:
                break
        return added > 0

    # ---------- цикл воспроизведения ----------

    async def _loop(self):
        while True:
            if not self.queue and self.autoplay and self.last_played:
                try:
                    await self.autoplay_fill()
                except Exception as e:
                    log.warning("Автоплей не сработал: %s", e)
            if not self.queue:
                break
            vc = self.vc
            if vc is None or not vc.is_connected():
                break

            track = self.queue.popleft()
            self.current = track
            prep = self.take_prefetched(track)
            if prep:
                proc, audio = prep.proc, prep.audio
            else:
                try:
                    proc, audio = open_audio(track.page_url)
                except Exception as e:
                    log.warning("Не загрузился %s: %s", track.title, e)
                    await self.send(f"❌ Не смог загрузить {track.label()}: {e}")
                    continue
            source = discord.PCMVolumeTransformer(audio, volume=self.volume)

            done = asyncio.Event()

            def after(err):
                if err:
                    log.error("Ошибка воспроизведения: %s", err)
                self.bot.loop.call_soon_threadsafe(done.set)

            self.skip_requested = False
            self.back_requested = False
            self.started_at = time.monotonic()
            self.paused_at = None
            self.paused_total = 0.0
            vc.play(source, after=after)
            await self.bot.set_listening(track.title)
            await self.show_panel()
            asyncio.create_task(self._prefetch_near_end(track))
            await done.wait()

            rc = proc.poll()  # код выхода yt-dlp (None — ещё работает, например при пропуске)
            if rc is None:
                proc.kill()
            await asyncio.get_running_loop().run_in_executor(None, proc.wait)

            # yt-dlp упал, а трек никто не пропускал — значит YouTube не отдал звук
            if rc not in (None, 0) and not (self.skip_requested or self.stop_requested or self.back_requested):
                await self.send(f"❌ YouTube не отдал звук для {track.label()} (подробности в консоли бота).")
                continue

            if self.back_requested:  # ⏮️ — очередь уже переставлена, в историю не пишем
                self.back_requested = False
                continue
            if self.stop_requested:
                self.stop_requested = False
                continue

            self.last_played = track
            if self.loop_mode == "track" and not self.skip_requested:
                self.queue.appendleft(track)
            else:
                self.history.append(track)
                if self.loop_mode == "queue":
                    self.queue.append(track)

        self.current = None
        self.drop_prefetch()
        await self.bot.set_listening(None)
        await self.finish_panel()
        if self.vc and self.vc.is_connected():
            self.idle_task = asyncio.create_task(self._idle_leave())

    # ---------- панель ----------

    def build_embed(self) -> discord.Embed:
        t = self.current
        e = discord.Embed(title=t.title[:256], url=t.page_url, color=PANEL_COLOR)
        e.set_author(name="⏸️ Пауза" if self.is_paused else "▶️ Сейчас играет")
        el = self.elapsed()
        e.description = f"`{fmt_time(el)}` {progress_bar(el, t.duration)} `{fmt_time(t.duration)}`"
        if t.thumbnail:
            e.set_thumbnail(url=t.thumbnail)
        upcoming = list(self.queue)[:3]
        value = "\n".join(f"`{i}.` {x.title[:60]}" for i, x in enumerate(upcoming, 1))
        if not value:
            value = "📻 подберу похожее" if self.autoplay else "пусто"
        if len(self.queue) > 3:
            value += f"\n…и ещё {len(self.queue) - 3}"
        e.add_field(name="Далее", value=value, inline=False)
        e.set_footer(
            text=f"Заказал {t.requester} · громкость {round(self.volume * 100)}% · "
                 f"повтор: {LOOP_LABEL[self.loop_mode]} · автоплей: {'вкл' if self.autoplay else 'выкл'}"
        )
        return e

    async def show_panel(self):
        """Новая панель под каждый трек — чтобы она всегда была внизу чата."""
        if not self.text_channel:
            return
        if self.panel:
            try:
                await self.panel.delete()
            except discord.HTTPException:
                pass
        self.view = PlayerView(self)
        try:
            self.panel = await self.text_channel.send(embed=self.build_embed(), view=self.view)
        except discord.HTTPException:
            self.panel = None
        if self.updater is None or self.updater.done():
            self.updater = asyncio.create_task(self._update_panel())

    async def refresh_panel(self):
        if not (self.panel and self.current and self.view):
            return
        self.view.sync()
        try:
            await self.panel.edit(embed=self.build_embed(), view=self.view)
        except discord.NotFound:
            self.panel = None
        except discord.HTTPException:
            pass

    async def _update_panel(self):
        while self.current:
            await asyncio.sleep(PANEL_REFRESH)
            await self.refresh_panel()

    async def finish_panel(self):
        if self.panel:
            try:
                await self.panel.edit(
                    embed=discord.Embed(description="⏹️ Очередь закончилась. `/play`, чтобы продолжить.", color=PANEL_COLOR),
                    view=None,
                )
            except discord.HTTPException:
                pass
            self.panel = None


class PlayerView(discord.ui.View):
    """Кнопки под панелью."""

    def __init__(self, player: GuildPlayer):
        super().__init__(timeout=None)
        self.player = player
        self.sync()

    def sync(self):
        p = self.player
        self.pause_btn.emoji = "▶️" if p.is_paused else "⏸️"
        self.loop_btn.emoji = "🔂" if p.loop_mode == "track" else "🔁"
        self.loop_btn.style = discord.ButtonStyle.secondary if p.loop_mode == "off" else discord.ButtonStyle.success
        self.autoplay_btn.style = discord.ButtonStyle.success if p.autoplay else discord.ButtonStyle.secondary

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        vc = self.player.vc
        voice = interaction.user.voice
        if not vc or not voice or voice.channel != vc.channel:
            await interaction.response.send_message("❌ Зайди в мой голосовой канал.", ephemeral=True)
            return False
        return True

    async def redraw(self, interaction: discord.Interaction):
        self.sync()
        if self.player.current:
            await interaction.response.edit_message(embed=self.player.build_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0)
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.player.previous():
            await interaction.response.defer()  # новая панель появится сама
        else:
            await interaction.response.send_message("📭 История пуста.", ephemeral=True)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0)
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.player.resume():
            self.player.pause()
        await self.redraw(interaction)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.skip()
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.stop()
        await interaction.response.defer()

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1)
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.loop_mode = LOOP_NEXT[self.player.loop_mode]
        await self.redraw(interaction)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1)
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.shuffle()
        await self.redraw(interaction)

    @discord.ui.button(emoji="📻", style=discord.ButtonStyle.secondary, row=1)
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.player.autoplay = not self.player.autoplay
        await self.redraw(interaction)

    @discord.ui.button(emoji="📜", style=discord.ButtonStyle.secondary, row=1)
    async def queue_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(queue_text(self.player), ephemeral=True)


# ---------- бот ----------

class DJBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, GuildPlayer] = {}

    def player(self, guild: discord.Guild) -> GuildPlayer:
        if guild.id not in self.players:
            self.players[guild.id] = GuildPlayer(self, guild)
        return self.players[guild.id]

    async def set_listening(self, title: str | None):
        """Статус «Слушает: …» в профиле бота."""
        activity = discord.Activity(type=discord.ActivityType.listening, name=title[:128]) if title else None
        try:
            await self.change_presence(activity=activity)
        except Exception:
            pass

    async def setup_hook(self):
        synced = await self.tree.sync()
        log.info("Синхронизировано слэш-команд: %d", len(synced))
        if os.path.exists("/.dockerenv"):  # в Docker перезапуск после обновления сделает сам Docker
            asyncio.create_task(self.ytdlp_updater())

    async def ytdlp_updater(self):
        """Раз в сутки обновляет yt-dlp. Если вышла новая версия — дожидается тишины и перезапускает бота."""
        await asyncio.sleep(60)
        while True:
            try:
                old = yt_dlp.version.__version__
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-m", "pip", "install", "-q", "-U", "--no-cache-dir", "yt-dlp[default]",
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
                )
                _, err = await proc.communicate()
                if proc.returncode != 0:
                    log.warning("Не смог обновить yt-dlp: %s", err.decode(errors="ignore")[-300:])
                else:
                    ver = await asyncio.create_subprocess_exec(
                        sys.executable, "-m", "yt_dlp", "--version", stdout=asyncio.subprocess.PIPE)
                    out, _ = await ver.communicate()
                    new = out.decode().strip()
                    if new and new != old:
                        log.info("yt-dlp обновлён: %s → %s. Перезапущусь, когда музыка затихнет.", old, new)
                        while any(p.current or p.queue for p in self.players.values()):
                            await asyncio.sleep(60)
                        log.info("Перезапуск ради нового yt-dlp")
                        await self.close()  # Docker (--restart unless-stopped) поднимет бота заново
                        return
                    log.info("yt-dlp актуален: %s", old)
            except Exception as e:
                log.warning("Ошибка при обновлении yt-dlp: %s", e)
            await asyncio.sleep(YTDLP_UPDATE_EVERY)

    async def on_ready(self):
        if shutil.which("ffmpeg") is None:
            log.warning("ffmpeg не найден! Музыка работать не будет.")
        if not discord.opus.is_loaded():
            discord.opus._load_default()
        if not discord.opus.is_loaded():
            log.warning("Нет libopus! Звук работать не будет (apt install libopus0).")
        log.info("Бот %s запущен и готов к музыке!", self.user)

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        # бота выкинули из канала — гасим очередь и фоновые задачи
        if member.id == self.user.id and after.channel is None:
            p = self.players.pop(guild.id, None)
            if p:
                p.cleanup()
            return
        vc = guild.voice_client
        if vc is None:
            return
        if not any(not m.bot for m in vc.channel.members):
            await asyncio.sleep(ALONE_TIMEOUT)
            vc = guild.voice_client
            if vc and not any(not m.bot for m in vc.channel.members):
                p = self.player(guild)
                await p.send("👋 Все ушли — я тоже.")
                await p.disconnect()


bot = DJBot()


async def ensure_voice(interaction: discord.Interaction) -> discord.VoiceClient | None:
    """Подключает бота к каналу пользователя. Требует уже сделанного defer()."""
    voice = interaction.user.voice
    if not voice or not voice.channel:
        await interaction.followup.send("❌ Сначала зайди в голосовой канал.")
        return None
    vc = interaction.guild.voice_client
    if vc is None:
        return await voice.channel.connect()
    if vc.channel != voice.channel:
        if vc.is_playing() or vc.is_paused():
            await interaction.followup.send(f"❌ Я уже играю в {vc.channel.mention}.")
            return None
        await vc.move_to(voice.channel)
    return vc


# ---------- команды ----------

@bot.tree.command(name="join", description="Подключиться к вашему голосовому каналу")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if vc:
        bot.player(interaction.guild).start()  # запустит таймер простоя, если играть нечего
        await interaction.followup.send(f"✅ Я в {vc.channel.mention}")


@bot.tree.command(name="leave", description="Отключиться от голосового канала")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        return await interaction.response.send_message("❌ Я не в голосовом канале.")
    await bot.player(interaction.guild).disconnect()
    await interaction.response.send_message("👋 Покинул канал.")


@bot.tree.command(name="play", description="Трек или плейлист: ссылка (YouTube, Spotify, Яндекс Музыка) или название")
@app_commands.describe(query="Начни вводить название — появятся подсказки. Или вставь ссылку")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer()
    vc = await ensure_voice(interaction)
    if not vc:
        return
    p = bot.player(interaction.guild)
    p.text_channel = interaction.channel
    requester = interaction.user.display_name

    try:
        external = await resolve_external(query)
        if external:
            query = external
        if is_playlist(query):
            title, entries = await extract_playlist(query)
            tracks = [track_from_flat(e, requester) for e in entries]
            if not tracks:
                return await interaction.followup.send("❌ Плейлист пустой или закрытый.")
            p.queue.extend(tracks)
            await interaction.followup.send(f"📃 Плейлист **{title}**: добавил {len(tracks)} треков")
        else:
            data = await extract(query)
            track = Track(
                title=data.get("title") or "Без названия",
                page_url=data.get("webpage_url") or data.get("original_url") or query,
                duration=data.get("duration"),
                requester=requester,
                thumbnail=data.get("thumbnail"),
            )
            p.queue.append(track)
            found = f" (нашёл по «{external}»)" if external else ""
            if p.current is None:
                await interaction.followup.send(f"🎵 Запускаю {track.label()}{found}")
            else:
                await interaction.followup.send(f"➕ В очереди #{len(p.queue)}: {track.label()}{found}")
    except Exception as e:
        log.warning("/play %r: %s", query, e)
        if VK_LINK_RE.search(query):
            if str(e) == "vk-no-token":
                msg = "❌ Музыку из ВК достать не могу — она доступна только с аккаунта. Напиши название трека, найду на YouTube."
            else:
                msg = f"❌ ВК: {e}"
        elif "://" in query or re.match(r"^\S+\.[a-z]{2,}/\S*$", query):
            msg = ("❌ Не получилось открыть ссылку. Понимаю YouTube, SoundCloud, а также треки из Spotify и Яндекс Музыки. "
                   "Можно просто написать название.")
        else:
            msg = f"❌ Не нашёл: {str(e).removeprefix('ERROR: ')[:200]}"
        return await interaction.followup.send(msg)

    if p.current:
        await p.refresh_panel()
    p.start()


_suggest_cache: dict[str, list[tuple[str, str]]] = {}


@play.autocomplete("query")
async def play_autocomplete(interaction: discord.Interaction, current: str):
    q = current.strip()
    if len(q) < 3 or q.startswith("http"):
        return []
    if q not in _suggest_cache:
        try:
            data = await asyncio.wait_for(run_ytdl(ytdl_flat, f"ytsearch5:{q}"), timeout=2.5)
        except Exception:
            return []  # не успели за 3 сек, которые даёт Discord — просто без подсказок
        if len(_suggest_cache) > 300:
            _suggest_cache.clear()
        _suggest_cache[q] = [
            (f"{e.get('title') or '?'} · {fmt_time(e.get('duration'))}"[:100], yt_url(e["id"]))
            for e in (data.get("entries") or []) if e and e.get("id")
        ]
    return [app_commands.Choice(name=n, value=v) for n, v in _suggest_cache[q]]


@bot.tree.command(name="skip", description="Пропустить текущий трек")
@app_commands.guild_only()
async def skip(interaction: discord.Interaction):
    ok = bot.player(interaction.guild).skip()
    await interaction.response.send_message("⏭️ Пропущено." if ok else "🔇 Ничего не играет.")


@bot.tree.command(name="previous", description="Вернуть предыдущий трек")
@app_commands.guild_only()
async def previous(interaction: discord.Interaction):
    ok = bot.player(interaction.guild).previous()
    await interaction.response.send_message("⏮️ Возвращаю." if ok else "📭 История пуста.")


@bot.tree.command(name="pause", description="Пауза")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.pause()
    await interaction.response.send_message("⏸️ Пауза." if ok else "🔇 Ничего не играет.")
    await p.refresh_panel()


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.resume()
    await interaction.response.send_message("▶️ Продолжаю." if ok else "❌ Нечего продолжать.")
    await p.refresh_panel()


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    bot.player(interaction.guild).stop()
    await interaction.response.send_message("⏹️ Остановлено, очередь очищена.")


@bot.tree.command(name="loop", description="Режим повтора")
@app_commands.describe(mode="Что повторять")
@app_commands.choices(mode=[
    app_commands.Choice(name="выкл", value="off"),
    app_commands.Choice(name="текущий трек", value="track"),
    app_commands.Choice(name="всю очередь", value="queue"),
])
@app_commands.guild_only()
async def loop_cmd(interaction: discord.Interaction, mode: app_commands.Choice[str]):
    p = bot.player(interaction.guild)
    p.loop_mode = mode.value
    await interaction.response.send_message(f"🔁 Повтор: {LOOP_LABEL[mode.value]}")
    await p.refresh_panel()


@bot.tree.command(name="autoplay", description="Автоплей: когда очередь кончится, ставить похожие треки")
@app_commands.guild_only()
async def autoplay_cmd(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    p.autoplay = not p.autoplay
    await interaction.response.send_message("📻 Автоплей включён." if p.autoplay else "📻 Автоплей выключен.")
    await p.refresh_panel()


@bot.tree.command(name="queue", description="Показать очередь")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(queue_text(bot.player(interaction.guild)))


@bot.tree.command(name="history", description="Что недавно играло")
@app_commands.guild_only()
async def history_cmd(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if not p.history:
        return await interaction.response.send_message("📭 Пока ничего не играло.")
    items = list(p.history)[::-1][:10]
    lines = [f"`{i}.` [{t.title[:70]}]({t.page_url}) — {t.requester}" for i, t in enumerate(items, 1)]
    await interaction.response.send_message("🕘 Недавно играло:\n" + "\n".join(lines), suppress_embeds=True)


@bot.tree.command(name="nowplaying", description="Показать панель плеера заново")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if not p.current:
        return await interaction.response.send_message("🔇 Ничего не играет.")
    p.text_channel = interaction.channel
    await interaction.response.send_message("👇", ephemeral=True)
    await p.show_panel()


@bot.tree.command(name="volume", description="Громкость 0–100")
@app_commands.describe(level="Громкость в процентах")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    p = bot.player(interaction.guild)
    p.volume = level / 100
    vc = interaction.guild.voice_client
    if vc and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = p.volume
    await interaction.response.send_message(f"🔊 Громкость: {level}%")
    await p.refresh_panel()


@bot.tree.command(name="shuffle", description="Перемешать очередь")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.shuffle()
    await interaction.response.send_message("🔀 Очередь перемешана." if ok else "❌ Нечего перемешивать.")
    await p.refresh_panel()


@bot.tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер в /queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    p = bot.player(interaction.guild)
    if position > len(p.queue):
        return await interaction.response.send_message("❌ Нет такого номера.")
    track = p.queue[position - 1]
    del p.queue[position - 1]
    await interaction.response.send_message(f"🗑️ Убрал {track.label()}")
    await p.refresh_panel()


@bot.tree.command(name="stalker", description="Получить мудрость из Зоны")
async def stalker(interaction: discord.Interaction):
    await interaction.response.send_message(f"📜 *{random.choice(STALKER_QUOTES)}*")


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Ошибка команды", exc_info=error)
    msg = f"❌ Ошибка: {error}"
    if interaction.response.is_done():
        await interaction.followup.send(msg, ephemeral=True)
    else:
        await interaction.response.send_message(msg, ephemeral=True)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("[CRITICAL] Нет DISCORD_TOKEN в .env")
    bot.run(TOKEN, log_handler=None)
