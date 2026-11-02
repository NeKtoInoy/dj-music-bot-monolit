import asyncio
import html
import json
import logging
import os
import random
import re
import shutil
import subprocess
import tempfile
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
INFO_TTL = 30 * 60               # сек: сколько живёт сохранённая инфа о треке (ссылки YouTube протухают)
PANEL_COLOR = 0x1D9E75
OPUS_BITRATE = 128              # кбит/с: ffmpeg сразу отдаёт сжатый звук, боту не надо его кодировать
STATE_PATH = os.getenv("STATE_PATH", os.path.expanduser("~/.cache/dj-bot/state.json"))
CONFIG_PATH = os.path.join(os.path.dirname(STATE_PATH), "config.json")  # какой канал на каком сервере — музыкальный
STATE_SAVE_EVERY = 10           # сек: как часто сохранять очередь (чтобы пережить перезапуск)

# эффекты: название → (фильтр ffmpeg, во сколько раз меняется скорость)
EFFECTS = {
    "none": ("", 1.0),
    "bassboost": ("bass=g=10:f=110:w=0.6", 1.0),
    "nightcore": ("asetrate=60000,aresample=48000", 1.25),
    "slowed": ("asetrate=38400,aresample=48000", 0.8),
    "8d": ("apulsator=hz=0.08", 1.0),
}
EFFECT_LABEL = {"none": "нет", "bassboost": "bass boost", "nightcore": "nightcore", "slowed": "slowed", "8d": "8D"}

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
    info_path: str | None = None   # сохранённый ответ yt-dlp, чтобы не искать трек второй раз
    info_time: float = 0.0
    start_at: float = 0.0          # с какой секунды начать (после перезапуска бота)

    def to_dict(self) -> dict:
        return {"title": self.title, "page_url": self.page_url, "duration": self.duration,
                "requester": self.requester, "thumbnail": self.thumbnail, "start_at": self.start_at}

    @classmethod
    def from_dict(cls, d: dict) -> "Track":
        return cls(title=d["title"], page_url=d["page_url"], duration=d.get("duration"),
                   requester=d.get("requester", "?"), thumbnail=d.get("thumbnail"),
                   start_at=float(d.get("start_at") or 0))

    def drop_info(self):
        if self.info_path:
            try:
                os.remove(self.info_path)
            except OSError:
                pass
            self.info_path = None

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


def save_info(data: dict) -> str:
    """Сохраняет ответ yt-dlp во временный файл."""
    fd, path = tempfile.mkstemp(prefix="ytinfo_", suffix=".json")
    with os.fdopen(fd, "w") as f:
        json.dump(ytdl.sanitize_info(data), f)
    return path


def open_audio(track: "Track", start: float = 0.0, af: str = "") -> tuple[subprocess.Popen, discord.FFmpegOpusAudio]:
    """yt-dlp качает звук кусками и отдаёт его ffmpeg через трубу.
    Прямую ссылку ffmpeg YouTube теперь часто режет ошибкой 403.
    Если инфа о треке уже есть (её достали в /play) — отдаём её yt-dlp, чтобы он не искал трек заново.
    ffmpeg сам накладывает громкость и эффекты (af) и сразу жмёт в opus — так меньше нагрузка на процессор."""
    fresh = track.info_path and os.path.exists(track.info_path) and time.time() - track.info_time < INFO_TTL
    src = ["--load-info-json", track.info_path] if fresh else [track.page_url]
    proc = subprocess.Popen(
        [sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
         "--downloader", "native", "--http-chunk-size", "10M",
         "-f", "bestaudio/best", "-o", "-", *src],
        stdout=subprocess.PIPE,
    )
    before = "-nostdin" + (f" -ss {start:.2f}" if start > 0.5 else "")
    options = "-vn" + (f' -af "{af}"' if af else "")
    audio = discord.FFmpegOpusAudio(proc.stdout, pipe=True, bitrate=OPUS_BITRATE,
                                    before_options=before, options=options)
    return proc, audio


class PrimedSource(discord.AudioSource):
    """Обёртка: сначала отдаёт заранее прочитанный кадр, потом читает дальше как обычно."""

    def __init__(self, inner: discord.AudioSource, first: bytes):
        self.inner, self.first = inner, first

    def read(self) -> bytes:
        if self.first is not None:
            frame, self.first = self.first, None
            return frame
        return self.inner.read()

    def is_opus(self) -> bool:
        return self.inner.is_opus()

    def cleanup(self):
        self.inner.cleanup()


@dataclass
class Prepared:
    """Трек, который уже начали грузить заранее."""
    track: Track
    proc: subprocess.Popen
    audio: discord.FFmpegOpusAudio

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
        self.last_action: str | None = None     # «Утрырыш добавил …» — строка внизу плеера
        self.restart_timer: asyncio.Task | None = None
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
        self.effect = "none"
        self.seg_offset = 0.0            # с какой секунды трека начался текущий отрезок
        self.cur_proc: subprocess.Popen | None = None  # yt-dlp, который сейчас качает звук
        self.swap_gen = 0                # номер последней подмены потока (старые подмены отменяются)
        self.started_at = 0.0
        self.paused_at: float | None = None
        self.paused_total = 0.0

    @property
    def vc(self) -> discord.VoiceClient | None:
        return self.guild.voice_client

    @property
    def is_paused(self) -> bool:
        return bool(self.vc and self.vc.is_paused())

    @property
    def speed(self) -> float:
        return EFFECTS[self.effect][1]

    def audio_filter(self) -> str:
        parts = [EFFECTS[self.effect][0]]
        if abs(self.volume - 1.0) > 0.01:
            parts.append(f"volume={self.volume:.2f}")
        return ",".join(p for p in parts if p)

    def elapsed(self) -> float:
        """Позиция в треке (в секундах самого трека, с учётом скорости эффекта)."""
        end = self.paused_at or time.monotonic()
        return max(0.0, self.seg_offset + (end - self.started_at - self.paused_total) * self.speed)

    def restart_at(self, pos: float | None = None) -> bool:
        """Перемотка, эффекты, громкость: готовит новый поток в фоне и подменяет им текущий без паузы."""
        if not self.current or not self.vc or not (self.vc.is_playing() or self.vc.is_paused()):
            return False
        pos = self.elapsed() if pos is None else max(0.0, pos)
        self.drop_prefetch()  # заготовка следующего трека сделана со старыми настройками
        self.swap_gen += 1
        asyncio.create_task(self._swap(self.current, pos, self.swap_gen))
        return True

    async def _swap(self, track: Track, pos: float, gen: int):
        try:
            proc, audio = open_audio(track, pos, self.audio_filter())
        except Exception as e:
            log.warning("Не смог подготовить новый поток: %s", e)
            return
        # ждём первый кадр нового потока — всё это время продолжает играть старый
        first = await asyncio.get_running_loop().run_in_executor(None, audio.read)
        vc = self.vc
        if (gen != self.swap_gen or self.current is not track or not first
                or not vc or not (vc.is_playing() or vc.is_paused())):
            audio.cleanup()  # трек сменился или пришла более свежая подмена — эта уже не нужна
            if proc.poll() is None:
                proc.kill()
            return
        old_source, old_proc = vc.source, self.cur_proc
        vc.source = PrimedSource(audio, first)
        self.cur_proc = proc
        self.seg_offset = pos
        self.started_at = time.monotonic()
        self.paused_total = 0.0
        self.paused_at = time.monotonic() if vc.is_paused() else None
        if old_proc and old_proc.poll() is None:
            old_proc.kill()
        if old_source:  # гасим старый ffmpeg в фоне, чтобы не подвешивать бота
            asyncio.get_running_loop().run_in_executor(None, old_source.cleanup)
        await self.refresh_panel()

    def snapshot(self) -> dict | None:
        """Состояние для сохранения на диск."""
        vc = self.vc
        if not vc or not vc.is_connected() or (not self.current and not self.queue):
            return None
        tracks = []
        if self.current:
            d = self.current.to_dict()
            d["start_at"] = round(self.elapsed(), 1)
            tracks.append(d)
        tracks += [t.to_dict() for t in list(self.queue)[:300]]
        return {"voice": vc.channel.id, "text": getattr(self.text_channel, "id", None), "tracks": tracks,
                "volume": self.volume, "loop": self.loop_mode, "autoplay": self.autoplay, "effect": self.effect}

    async def send(self, text: str):
        """Сообщение в чат. В музыкальном канале — исчезает через 15 сек, чтобы не мусорить."""
        home = self.home_channel_id
        channel = self.guild.get_channel(home) if home else self.text_channel
        if channel:
            try:
                await channel.send(text, delete_after=15 if home else None)
            except discord.HTTPException:
                pass

    def schedule_restart(self, delay: float = 1.2):
        """Перезапуск трека с задержкой — чтобы пять нажатий «громче» подряд дали один перезапуск."""
        if self.restart_timer and not self.restart_timer.done():
            self.restart_timer.cancel()

        async def later():
            await asyncio.sleep(delay)
            self.restart_at()
        self.restart_timer = asyncio.create_task(later())

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
        for t in self.queue:
            t.drop_info()
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
                            proc, audio = open_audio(nxt, 0.0, self.audio_filter())
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
            start, failed = track.start_at, False
            track.start_at = 0.0
            prep = self.take_prefetched(track) if start == 0 else None
            if prep:
                proc, audio = prep.proc, prep.audio
            else:
                try:
                    proc, audio = open_audio(track, start, self.audio_filter())
                except Exception as e:
                    log.warning("Не загрузился %s: %s", track.title, e)
                    await self.send(f"❌ Не смог загрузить {track.label()}: {e}")
                    failed = True

            if not failed:
                done = asyncio.Event()

                def after(err):
                    if err:
                        log.error("Ошибка воспроизведения: %s", err)
                    self.bot.loop.call_soon_threadsafe(done.set)

                self.skip_requested = self.back_requested = False
                self.seg_offset = start
                self.started_at = time.monotonic()
                self.paused_at = None
                self.paused_total = 0.0
                self.cur_proc = proc
                vc.play(audio, after=after)
                await self.bot.set_listening(track.title)
                await self.show_panel()
                asyncio.create_task(self._prefetch_near_end(track))
                await done.wait()

                # трек мог несколько раз подменяться (громкость/эффект/перемотка) — смотрим последний yt-dlp
                proc = self.cur_proc or proc
                rc = proc.poll()  # код выхода yt-dlp (None — ещё работает, например при пропуске)
                if rc is None:
                    proc.kill()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait)
                self.cur_proc = None
                self.swap_gen += 1  # отменить подмены, которые не успели к концу трека
            track.drop_info()  # инфа одноразовая: при повторе трека yt-dlp возьмёт свежую
            if failed:
                continue

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

    @property
    def home_channel_id(self) -> int | None:
        """Музыкальный канал этого сервера (задаётся /setup)."""
        return self.bot.config.get(str(self.guild.id), {}).get("channel")

    def is_home(self, channel_id) -> bool:
        return channel_id is not None and channel_id == self.home_channel_id

    def build_embed(self) -> discord.Embed:
        t = self.current
        if t:
            e = discord.Embed(title=t.title[:256], url=t.page_url, color=PANEL_COLOR)
            e.set_author(name=f"☢ Радио «Монолит» · {'пауза' if self.is_paused else 'сейчас играет'}")
            el = self.elapsed()
            e.description = f"`{fmt_time(el)}` {progress_bar(el, t.duration)} `{fmt_time(t.duration)}`"
            if t.thumbnail:
                e.set_thumbnail(url=t.thumbnail)
        else:
            e = discord.Embed(title="Тишина в эфире", color=PANEL_COLOR)
            e.set_author(name="☢ Радио «Монолит»")
            e.description = ("Нажми **➕ Добавить** или просто напиши название трека в этот канал."
                             if self.home_channel_id else "`/play`, чтобы включить музыку.")
        q = list(self.queue)
        if t or q:
            lines = [f"`{i}.` {x.title[:55]}" + (" 📻" if x.requester == AUTOPLAY_REQUESTER else "")
                     for i, x in enumerate(q[:8], 1)]
            if len(q) > 8:
                lines.append(f"…и ещё {len(q) - 8}")
            if not lines:
                lines = ["📻 подберу похожее" if self.autoplay else "пусто"]
            e.add_field(name=f"Очередь · {len(q)}", value="\n".join(lines)[:1024], inline=False)
        if self.history:
            recent = list(self.history)[::-1][:5]
            e.add_field(name="Недавно играло", value="\n".join(f"· {x.title[:60]}" for x in recent)[:1024], inline=False)
        info = [f"громкость {round(self.volume * 100)}%", f"повтор: {LOOP_LABEL[self.loop_mode]}",
                f"автоплей: {'вкл' if self.autoplay else 'выкл'}"]
        if self.effect != "none":
            info.append(f"эффект: {EFFECT_LABEL[self.effect]}")
        if t:
            info.insert(0, f"заказал {t.requester}")
        text = " · ".join(info)
        if self.last_action:
            text = f"{self.last_action}\n{text}"
        e.set_footer(text=text[:2048])
        return e

    async def _home_message(self) -> discord.Message | None:
        """Сообщение-плеер в музыкальном канале. Если его удалили — создаёт заново."""
        cfg = self.bot.config.get(str(self.guild.id))
        channel = self.guild.get_channel(cfg["channel"]) if cfg else None
        if channel is None:
            return None
        if self.panel and self.panel.id == cfg.get("message"):
            return self.panel
        msg = None
        if cfg.get("message"):
            try:
                msg = await channel.fetch_message(cfg["message"])
            except discord.HTTPException:
                msg = None
        if msg is None:
            msg = await channel.send(embed=self.build_embed(), view=PlayerView(self))
            cfg["message"] = msg.id
            self.bot.save_config()
            try:
                await msg.pin()
            except discord.HTTPException:
                pass
        self.panel = msg
        return msg

    async def show_panel(self):
        if self.home_channel_id:
            await self.refresh_panel()
        elif self.text_channel:
            # без музыкального канала — как раньше: новая панель под каждый трек, чтобы была внизу чата
            if self.panel:
                try:
                    await self.panel.delete()
                except discord.HTTPException:
                    pass
            try:
                self.panel = await self.text_channel.send(embed=self.build_embed(), view=PlayerView(self))
            except discord.HTTPException:
                self.panel = None
        if self.updater is None or self.updater.done():
            self.updater = asyncio.create_task(self._update_panel())

    async def refresh_panel(self):
        try:
            if self.home_channel_id:
                msg = await self._home_message()
                if msg:
                    await msg.edit(embed=self.build_embed(), view=PlayerView(self))
            elif self.panel and self.current:
                await self.panel.edit(embed=self.build_embed(), view=PlayerView(self))
        except discord.NotFound:
            self.panel = None
        except discord.HTTPException as e:
            log.warning("Не смог обновить панель: %s", e)

    async def _update_panel(self):
        while self.current:
            await asyncio.sleep(PANEL_REFRESH)
            await self.refresh_panel()

    async def finish_panel(self):
        if self.home_channel_id:
            await self.refresh_panel()  # плеер остаётся, просто показывает «тишину в эфире»
            return
        if self.panel:
            try:
                await self.panel.edit(
                    embed=discord.Embed(description="⏹️ Очередь закончилась. `/play`, чтобы продолжить.", color=PANEL_COLOR),
                    view=None,
                )
            except discord.HTTPException:
                pass
            self.panel = None


def same_voice(interaction: discord.Interaction) -> str | None:
    """Кнопками управляют только те, кто сидит с ботом в одном канале."""
    vc = interaction.guild.voice_client
    voice = interaction.user.voice
    if not vc:
        return "🔇 Я сейчас не в канале. Нажми **➕ Добавить**, чтобы включить музыку."
    if not voice or voice.channel != vc.channel:
        return "❌ Зайди в мой голосовой канал."
    return None


class PlayerView(discord.ui.View):
    """Кнопки и меню плеера. custom_id постоянные — кнопки работают и после перезапуска бота."""

    def __init__(self, player: "GuildPlayer | None"):
        super().__init__(timeout=None)
        self.add_item(QueueSelect(player))
        self.add_item(EffectSelect(player))
        if player:
            self.pause_btn.emoji = "▶️" if player.is_paused else "⏸️"
            self.loop_btn.emoji = "🔂" if player.loop_mode == "track" else "🔁"
            self.loop_btn.style = (discord.ButtonStyle.secondary if player.loop_mode == "off"
                                   else discord.ButtonStyle.success)
            self.autoplay_btn.style = (discord.ButtonStyle.success if player.autoplay
                                       else discord.ButtonStyle.secondary)

    @staticmethod
    async def guard(interaction: discord.Interaction) -> GuildPlayer | None:
        err = same_voice(interaction)
        if err:
            await interaction.response.send_message(err, ephemeral=True)
            return None
        return bot.player(interaction.guild)

    @staticmethod
    async def redraw(interaction: discord.Interaction, p: GuildPlayer):
        await interaction.response.edit_message(embed=p.build_embed(), view=PlayerView(p))

    @discord.ui.button(emoji="⏮️", style=discord.ButtonStyle.secondary, row=0, custom_id="dj:prev")
    async def prev_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        if p.previous():
            p.last_action = f"⏮️ {interaction.user.display_name} вернул прошлый трек"
            await interaction.response.defer()
        else:
            await interaction.response.send_message("📭 История пуста.", ephemeral=True)

    @discord.ui.button(emoji="⏸️", style=discord.ButtonStyle.primary, row=0, custom_id="dj:pause")
    async def pause_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        if p.resume():
            p.last_action = f"▶️ {interaction.user.display_name} снял с паузы"
        elif p.pause():
            p.last_action = f"⏸️ {interaction.user.display_name} поставил на паузу"
        await self.redraw(interaction, p)

    @discord.ui.button(emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="dj:skip")
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        if p.current and p.skip():
            p.last_action = f"⏭️ {interaction.user.display_name} пропустил «{p.current.title[:40]}»"
        await interaction.response.defer()

    @discord.ui.button(emoji="⏹️", style=discord.ButtonStyle.danger, row=0, custom_id="dj:stop")
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        p.stop()
        p.last_action = f"⏹️ {interaction.user.display_name} остановил музыку"
        await interaction.response.defer()

    @discord.ui.button(label="Добавить", emoji="➕", style=discord.ButtonStyle.success, row=0, custom_id="dj:add")
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(AddModal())

    @discord.ui.button(emoji="🔉", style=discord.ButtonStyle.secondary, row=1, custom_id="dj:voldown")
    async def voldown_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, -0.1)

    @discord.ui.button(emoji="🔊", style=discord.ButtonStyle.secondary, row=1, custom_id="dj:volup")
    async def volup_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.change_volume(interaction, +0.1)

    async def change_volume(self, interaction: discord.Interaction, delta: float):
        if not (p := await self.guard(interaction)):
            return
        p.volume = round(min(1.0, max(0.0, p.volume + delta)), 2)
        p.last_action = f"🔊 {interaction.user.display_name}: громкость {round(p.volume * 100)}%"
        p.schedule_restart()
        await self.redraw(interaction, p)

    @discord.ui.button(emoji="🔁", style=discord.ButtonStyle.secondary, row=1, custom_id="dj:loop")
    async def loop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        p.loop_mode = LOOP_NEXT[p.loop_mode]
        p.last_action = f"🔁 {interaction.user.display_name}: повтор — {LOOP_LABEL[p.loop_mode]}"
        await self.redraw(interaction, p)

    @discord.ui.button(emoji="🔀", style=discord.ButtonStyle.secondary, row=1, custom_id="dj:shuffle")
    async def shuffle_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        if p.shuffle():
            p.last_action = f"🔀 {interaction.user.display_name} перемешал очередь"
        await self.redraw(interaction, p)

    @discord.ui.button(emoji="📻", style=discord.ButtonStyle.secondary, row=1, custom_id="dj:autoplay")
    async def autoplay_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.guard(interaction)):
            return
        p.autoplay = not p.autoplay
        p.last_action = f"📻 {interaction.user.display_name}: автоплей {'включён' if p.autoplay else 'выключен'}"
        await self.redraw(interaction, p)


class QueueSelect(discord.ui.Select):
    """Меню очереди: выбрал трек — дальше «играть сейчас / следующим / убрать»."""

    def __init__(self, player: "GuildPlayer | None"):
        q = list(player.queue)[:25] if player else []
        options = [discord.SelectOption(label=f"{i}. {t.title}"[:100], value=str(i - 1),
                                        description=f"заказал {t.requester}"[:100])
                   for i, t in enumerate(q, 1)]
        super().__init__(
            custom_id="dj:queue", row=2,
            placeholder="Очередь: перейти к треку или убрать…" if options else "Очередь пуста",
            options=options or [discord.SelectOption(label="пусто", value="-1")],
            disabled=bool(player) and not options,
        )

    async def callback(self, interaction: discord.Interaction):
        p = bot.player(interaction.guild)
        idx = int(self.values[0])
        if not 0 <= idx < len(p.queue):
            return await interaction.response.send_message("Этого трека уже нет в очереди.", ephemeral=True)
        t = p.queue[idx]
        await interaction.response.send_message(f"🎵 {t.label()} — заказал {t.requester}", view=QueueItemView(t), ephemeral=True)


class QueueItemView(discord.ui.View):
    def __init__(self, track: Track):
        super().__init__(timeout=120)
        self.track = track

    async def take(self, interaction: discord.Interaction) -> GuildPlayer | None:
        """Достаёт трек из очереди (если он там ещё есть)."""
        err = same_voice(interaction)
        if err:
            await interaction.response.edit_message(content=err, view=None)
            return None
        p = bot.player(interaction.guild)
        for i, t in enumerate(p.queue):
            if t is self.track:
                del p.queue[i]
                return p
        await interaction.response.edit_message(content="Этого трека уже нет в очереди.", view=None)
        return None

    @discord.ui.button(label="Играть сейчас", emoji="▶️", style=discord.ButtonStyle.success)
    async def now_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.take(interaction)):
            return
        p.queue.appendleft(self.track)
        p.last_action = f"▶️ {interaction.user.display_name} включил «{self.track.title[:40]}»"
        if not p.skip():
            p.start()
        await interaction.response.edit_message(content=f"▶️ Включаю {self.track.label()}", view=None)

    @discord.ui.button(label="Следующим", emoji="⬆️", style=discord.ButtonStyle.secondary)
    async def next_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.take(interaction)):
            return
        p.queue.appendleft(self.track)
        p.last_action = f"⬆️ {interaction.user.display_name} поднял «{self.track.title[:40]}»"
        await interaction.response.edit_message(content=f"⬆️ {self.track.label()} будет следующим", view=None)
        await p.refresh_panel()

    @discord.ui.button(label="Убрать", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not (p := await self.take(interaction)):
            return
        self.track.drop_info()
        p.last_action = f"🗑️ {interaction.user.display_name} убрал «{self.track.title[:40]}»"
        await interaction.response.edit_message(content=f"🗑️ Убрал {self.track.label()}", view=None)
        await p.refresh_panel()


class EffectSelect(discord.ui.Select):
    def __init__(self, player: "GuildPlayer | None"):
        current = player.effect if player else "none"
        super().__init__(
            custom_id="dj:effect", row=3, placeholder="Эффект",
            options=[discord.SelectOption(label=f"Эффект: {label}", value=key, default=key == current)
                     for key, label in EFFECT_LABEL.items()],
        )

    async def callback(self, interaction: discord.Interaction):
        if not (p := await PlayerView.guard(interaction)):
            return
        pos = p.elapsed() if p.current else 0.0
        p.effect = self.values[0]
        p.restart_at(pos)
        p.last_action = f"🎛️ {interaction.user.display_name}: эффект — {EFFECT_LABEL[p.effect]}"
        await PlayerView.redraw(interaction, p)


class AddModal(discord.ui.Modal, title="Добавить трек"):
    query = discord.ui.TextInput(label="Название или ссылка", placeholder="Кино — Группа крови", max_length=300)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True, thinking=True)
        result = await add_from(interaction.guild, interaction.user, str(self.query.value), interaction.channel)
        await interaction.followup.send(result, ephemeral=True)


# ---------- бот ----------

class DJBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.players: dict[int, GuildPlayer] = {}
        self._restored = False
        self._last_state = ""
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                self.config: dict = json.load(f)
        except Exception:
            self.config = {}

    def save_config(self):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            tmp = CONFIG_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.config, f)
            os.replace(tmp, CONFIG_PATH)
        except Exception as e:
            log.warning("Не смог сохранить настройки: %s", e)

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
        self.add_view(PlayerView(None))  # кнопки старых плееров работают и после перезапуска
        synced = await self.tree.sync()
        log.info("Синхронизировано слэш-команд: %d", len(synced))
        if os.path.exists("/.dockerenv"):  # в Docker перезапуск после обновления сделает сам Docker
            asyncio.create_task(self.ytdlp_updater())
        asyncio.create_task(self.state_saver())

    async def state_saver(self):
        """Каждые несколько секунд сохраняет очереди на диск — чтобы пережить перезапуск."""
        await self.wait_until_ready()
        while True:
            await asyncio.sleep(STATE_SAVE_EVERY)
            if not self._restored:
                continue
            try:
                state = {str(gid): snap for gid, p in self.players.items() if (snap := p.snapshot())}
                text = json.dumps(state, ensure_ascii=False)
                if text != self._last_state:
                    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
                    tmp = STATE_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(text)
                    os.replace(tmp, STATE_PATH)
                    self._last_state = text
            except Exception as e:
                log.warning("Не смог сохранить очередь: %s", e)

    async def restore_state(self):
        """После перезапуска возвращается в каналы и продолжает играть с того же места."""
        try:
            with open(STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("Не смог прочитать сохранённую очередь: %s", e)
            return
        for gid, d in state.items():
            try:
                guild = self.get_guild(int(gid))
                channel = guild and guild.get_channel(d["voice"])
                if not isinstance(channel, discord.VoiceChannel) or not any(not m.bot for m in channel.members):
                    continue  # в канале никого — не врываемся
                if not guild.voice_client:
                    await channel.connect()
                p = self.player(guild)
                p.text_channel = guild.get_channel(d.get("text")) if d.get("text") else None
                p.volume = float(d.get("volume", 0.5))
                p.loop_mode = d.get("loop", "off") if d.get("loop") in LOOP_LABEL else "off"
                p.autoplay = bool(d.get("autoplay"))
                p.effect = d.get("effect", "none") if d.get("effect") in EFFECTS else "none"
                p.queue.extend(Track.from_dict(t) for t in d.get("tracks", []))
                if p.queue:
                    await p.send(f"♻️ Я перезапускался — продолжаю с того же места ({len(p.queue)} в очереди).")
                    p.start()
                log.info("Восстановил очередь на сервере %s: %d треков", guild.name, len(p.queue))
            except Exception as e:
                log.warning("Не смог восстановить сервер %s: %s", gid, e)

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
        if not self._restored:
            self._restored = True
            await self.restore_state()
            for gid in list(self.config):  # привести плееры в музыкальных каналах в актуальный вид
                guild = self.get_guild(int(gid))
                if guild:
                    await self.player(guild).refresh_panel()

    async def on_voice_state_update(self, member, before, after):
        guild = member.guild
        # бота выкинули из канала — гасим очередь и фоновые задачи
        if member.id == self.user.id and after.channel is None:
            p = self.players.get(guild.id)
            if p:
                p.cleanup()  # сам плеер оставляем: в нём история и настройки
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


# ---------- общая логика добавления треков ----------

async def connect_member(member: discord.Member) -> tuple[discord.VoiceClient | None, str | None]:
    """Подключает бота к каналу участника. Возвращает (голос, ошибка)."""
    voice = member.voice
    if not voice or not voice.channel:
        return None, "❌ Сначала зайди в голосовой канал."
    vc = member.guild.voice_client
    if vc is None:
        return await voice.channel.connect(), None
    if vc.channel != voice.channel:
        if vc.is_playing() or vc.is_paused():
            return None, f"❌ Я уже играю в {vc.channel.mention}."
        await vc.move_to(voice.channel)
    return vc, None


def friendly_error(query: str, e: Exception) -> str:
    log.warning("Не смог добавить %r: %s", query, e)
    if re.search(r"vk\.com|vk\.ru|^audio-?\d+_", query):
        return "❌ Музыку из ВК достать не могу — она доступна только с аккаунта. Напиши название трека, найду на YouTube."
    if "://" in query or re.match(r"^\S+\.[a-z]{2,}/\S*$", query):
        return ("❌ Не получилось открыть ссылку. Понимаю YouTube, SoundCloud, а также треки из Spotify и Яндекс Музыки. "
                "Можно просто написать название.")
    return f"❌ Не нашёл: {str(e).removeprefix('ERROR: ')[:200]}"


async def add_from(guild: discord.Guild, member: discord.Member, query: str, channel) -> str:
    """Добавляет трек/плейлист по запросу. Общая для /play, кнопки «➕» и сообщений в музыкальном канале."""
    query = query.strip()
    if not query:
        return "❌ Пустой запрос."
    vc, err = await connect_member(member)
    if err:
        return err
    p = bot.player(guild)
    if channel is not None:
        p.text_channel = channel
    who = member.display_name

    try:
        external = await resolve_external(query)
        if external:
            query = external
        if is_playlist(query):
            title, entries = await extract_playlist(query)
            tracks = [track_from_flat(e, who) for e in entries]
            if not tracks:
                return "❌ Плейлист пустой или закрытый."
            p.queue.extend(tracks)
            p.last_action = f"📃 {who} добавил плейлист «{title[:40]}» ({len(tracks)})"
            result = f"📃 Плейлист **{title}**: добавил {len(tracks)} треков"
        else:
            data = await extract(query)
            track = Track(
                title=data.get("title") or "Без названия",
                page_url=data.get("webpage_url") or data.get("original_url") or query,
                duration=data.get("duration"),
                requester=who,
                thumbnail=data.get("thumbnail"),
            )
            try:
                track.info_path, track.info_time = save_info(data), time.time()
            except Exception as e:
                log.warning("Не смог сохранить инфу о треке: %s", e)
            p.queue.append(track)
            p.last_action = f"➕ {who} добавил «{track.title[:50]}»"
            found = f" (нашёл по «{external}»)" if external else ""
            result = (f"🎵 Запускаю {track.label()}{found}" if p.current is None
                      else f"➕ В очереди #{len(p.queue)}: {track.label()}{found}")
    except Exception as e:
        return friendly_error(query, e)

    if p.current:
        await p.refresh_panel()
    p.start()
    return result


def quiet(interaction: discord.Interaction) -> bool:
    """В музыкальном канале ответы видит только тот, кто спросил — чтобы канал оставался чистым."""
    return bool(interaction.guild) and bot.player(interaction.guild).is_home(interaction.channel_id)


async def say(interaction: discord.Interaction, text: str, **kw):
    ephemeral = kw.pop("ephemeral", quiet(interaction))
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral, **kw)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral, **kw)


# ---------- музыкальный канал ----------

@bot.tree.command(name="setup", description="Сделать этот канал музыкальным: плеер + заказ треков сообщениями")
@app_commands.guild_only()
@app_commands.default_permissions(manage_channels=True)
async def setup_cmd(interaction: discord.Interaction):
    guild = interaction.guild
    await interaction.response.defer(ephemeral=True)
    old = bot.config.get(str(guild.id), {})
    if old.get("message") and old.get("channel") != interaction.channel_id:  # плеер переезжает — старый убираем
        try:
            ch = guild.get_channel(old["channel"])
            if ch:
                await (await ch.fetch_message(old["message"])).delete()
        except discord.HTTPException:
            pass
    bot.config[str(guild.id)] = {"channel": interaction.channel_id, "message": None}
    bot.save_config()
    p = bot.player(guild)
    p.panel = None
    await p.refresh_panel()

    notes = []
    perms = interaction.channel.permissions_for(guild.me)
    if bot.intents.message_content:
        notes.append("✍️ Можно просто писать название трека в этот канал — я добавлю его и уберу сообщение.")
        if not perms.manage_messages:
            notes.append("⚠️ Дай мне право **«Управлять сообщениями»** в этом канале, иначе не смогу убирать заказы из чата.")
    else:
        notes.append("ℹ️ Заказ треков сообщениями выключен: включи **Message Content Intent** в Developer Portal "
                     "и перезапусти меня. Пока добавляй через кнопку ➕.")
    await interaction.followup.send("✅ Плеер теперь живёт в этом канале.\n" + "\n".join(notes), ephemeral=True)


@bot.listen("on_message")
async def music_channel_requests(message: discord.Message):
    """В музыкальном канале любое сообщение — заказ трека."""
    if not message.guild or message.author.bot:
        return
    p = bot.player(message.guild)
    if not p.is_home(message.channel.id):
        return
    text = message.content.strip()
    try:
        await message.delete()
    except discord.HTTPException:
        pass
    if not text:
        return
    async with message.channel.typing():
        result = await add_from(message.guild, message.author, text, message.channel)
    if result.startswith("❌"):
        await message.channel.send(f"{message.author.mention} {result}", delete_after=10)


# ---------- команды ----------

@bot.tree.command(name="join", description="Подключиться к вашему голосовому каналу")
@app_commands.guild_only()
async def join(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=quiet(interaction))
    vc, err = await connect_member(interaction.user)
    if err:
        return await say(interaction, err)
    bot.player(interaction.guild).start()  # запустит таймер простоя, если играть нечего
    await say(interaction, f"✅ Я в {vc.channel.mention}")


@bot.tree.command(name="leave", description="Отключиться от голосового канала")
@app_commands.guild_only()
async def leave(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        return await say(interaction, "❌ Я не в голосовом канале.")
    p = bot.player(interaction.guild)
    p.last_action = f"👋 {interaction.user.display_name} выгнал меня из канала"
    await p.disconnect()
    await say(interaction, "👋 Покинул канал.")


@bot.tree.command(name="play", description="Трек или плейлист: ссылка (YouTube, Spotify, Яндекс Музыка) или название")
@app_commands.describe(query="Начни вводить название — появятся подсказки. Или вставь ссылку")
@app_commands.guild_only()
async def play(interaction: discord.Interaction, query: str):
    await interaction.response.defer(ephemeral=quiet(interaction))
    result = await add_from(interaction.guild, interaction.user, query, interaction.channel)
    await say(interaction, result)


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
    p = bot.player(interaction.guild)
    title = p.current.title if p.current else ""
    ok = p.skip()
    if ok:
        p.last_action = f"⏭️ {interaction.user.display_name} пропустил «{title[:40]}»"
    await say(interaction, "⏭️ Пропущено." if ok else "🔇 Ничего не играет.")


@bot.tree.command(name="previous", description="Вернуть предыдущий трек")
@app_commands.guild_only()
async def previous(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.previous()
    if ok:
        p.last_action = f"⏮️ {interaction.user.display_name} вернул прошлый трек"
    await say(interaction, "⏮️ Возвращаю." if ok else "📭 История пуста.")


@bot.tree.command(name="pause", description="Пауза")
@app_commands.guild_only()
async def pause(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.pause()
    if ok:
        p.last_action = f"⏸️ {interaction.user.display_name} поставил на паузу"
    await say(interaction, "⏸️ Пауза." if ok else "🔇 Ничего не играет.")
    await p.refresh_panel()


@bot.tree.command(name="resume", description="Продолжить воспроизведение")
@app_commands.guild_only()
async def resume(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.resume()
    if ok:
        p.last_action = f"▶️ {interaction.user.display_name} снял с паузы"
    await say(interaction, "▶️ Продолжаю." if ok else "❌ Нечего продолжать.")
    await p.refresh_panel()


@bot.tree.command(name="stop", description="Остановить музыку и очистить очередь")
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    p.stop()
    p.last_action = f"⏹️ {interaction.user.display_name} остановил музыку"
    await say(interaction, "⏹️ Остановлено, очередь очищена.")


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
    p.last_action = f"🔁 {interaction.user.display_name}: повтор — {LOOP_LABEL[mode.value]}"
    await say(interaction, f"🔁 Повтор: {LOOP_LABEL[mode.value]}")
    await p.refresh_panel()


@bot.tree.command(name="autoplay", description="Автоплей: когда очередь кончится, ставить похожие треки")
@app_commands.guild_only()
async def autoplay_cmd(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    p.autoplay = not p.autoplay
    p.last_action = f"📻 {interaction.user.display_name}: автоплей {'включён' if p.autoplay else 'выключен'}"
    await say(interaction, "📻 Автоплей включён." if p.autoplay else "📻 Автоплей выключен.")
    await p.refresh_panel()


def parse_time(text: str) -> float:
    """'90', '1:30', '1:02:03' → секунды."""
    sec = 0.0
    for x in text.strip().split(":"):
        sec = sec * 60 + float(x)
    return sec


@bot.tree.command(name="seek", description="Перемотка: 1:30 — на позицию, +15 / -15 — вперёд/назад")
@app_commands.describe(position="Например 1:30, 90, +15 или -10")
@app_commands.guild_only()
async def seek(interaction: discord.Interaction, position: str):
    p = bot.player(interaction.guild)
    if not p.current:
        return await say(interaction, "🔇 Ничего не играет.")
    if not p.current.duration:
        return await say(interaction, "❌ Это прямой эфир — его не перемотать.")
    try:
        text = position.strip()
        pos = (p.elapsed() + parse_time(text[1:]) * (1 if text[0] == "+" else -1)) if text[0] in "+-" else parse_time(text)
    except (ValueError, IndexError):
        return await say(interaction, "❌ Не понял время. Примеры: `1:30`, `90`, `+15`, `-10`.")
    pos = min(max(0.0, pos), p.current.duration - 1)
    if not p.restart_at(pos):
        return await say(interaction, "🔇 Ничего не играет.")
    p.last_action = f"⏩ {interaction.user.display_name} перемотал на {fmt_time(pos)}"
    await say(interaction, f"⏩ Перематываю на `{fmt_time(pos)}`…")


@bot.tree.command(name="effect", description="Эффект звука: bass boost, nightcore, slowed, 8D")
@app_commands.describe(name="Какой эффект")
@app_commands.choices(name=[app_commands.Choice(name=v, value=k) for k, v in EFFECT_LABEL.items()])
@app_commands.guild_only()
async def effect(interaction: discord.Interaction, name: app_commands.Choice[str]):
    p = bot.player(interaction.guild)
    pos = p.elapsed() if p.current else 0.0
    p.effect = name.value
    applied = p.restart_at(pos)
    p.last_action = f"🎛️ {interaction.user.display_name}: эффект — {EFFECT_LABEL[name.value]}"
    await say(interaction, f"🎛️ Эффект: **{EFFECT_LABEL[name.value]}**" + (" (применится через пару секунд)" if applied else ""))
    if not applied:
        await p.refresh_panel()


@bot.tree.command(name="queue", description="Показать очередь")
@app_commands.guild_only()
async def queue_cmd(interaction: discord.Interaction):
    await say(interaction, queue_text(bot.player(interaction.guild)))


@bot.tree.command(name="history", description="Что недавно играло")
@app_commands.guild_only()
async def history_cmd(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if not p.history:
        return await say(interaction, "📭 Пока ничего не играло.")
    items = list(p.history)[::-1][:10]
    lines = [f"`{i}.` [{t.title[:70]}]({t.page_url}) — {t.requester}" for i, t in enumerate(items, 1)]
    await say(interaction, "🕘 Недавно играло:\n" + "\n".join(lines), suppress_embeds=True)


@bot.tree.command(name="nowplaying", description="Показать панель плеера заново")
@app_commands.guild_only()
async def nowplaying(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    if p.home_channel_id:
        await say(interaction, f"👆 Плеер живёт в <#{p.home_channel_id}>.", ephemeral=True)
        return await p.refresh_panel()
    if not p.current:
        return await say(interaction, "🔇 Ничего не играет.")
    p.text_channel = interaction.channel
    await say(interaction, "👇", ephemeral=True)
    await p.show_panel()


@bot.tree.command(name="volume", description="Громкость 0–100")
@app_commands.describe(level="Громкость в процентах")
@app_commands.guild_only()
async def volume(interaction: discord.Interaction, level: app_commands.Range[int, 0, 100]):
    p = bot.player(interaction.guild)
    p.volume = level / 100
    p.last_action = f"🔊 {interaction.user.display_name}: громкость {level}%"
    applied = p.restart_at()  # ffmpeg применит громкость, перезапустив трек с того же места
    await say(interaction, f"🔊 Громкость: {level}%" + (" (применится через пару секунд)" if applied else ""))
    if not applied:
        await p.refresh_panel()


@bot.tree.command(name="shuffle", description="Перемешать очередь")
@app_commands.guild_only()
async def shuffle(interaction: discord.Interaction):
    p = bot.player(interaction.guild)
    ok = p.shuffle()
    if ok:
        p.last_action = f"🔀 {interaction.user.display_name} перемешал очередь"
    await say(interaction, "🔀 Очередь перемешана." if ok else "❌ Нечего перемешивать.")
    await p.refresh_panel()


@bot.tree.command(name="remove", description="Убрать трек из очереди по номеру")
@app_commands.describe(position="Номер в /queue")
@app_commands.guild_only()
async def remove(interaction: discord.Interaction, position: app_commands.Range[int, 1]):
    p = bot.player(interaction.guild)
    if position > len(p.queue):
        return await say(interaction, "❌ Нет такого номера.")
    track = p.queue[position - 1]
    del p.queue[position - 1]
    track.drop_info()
    p.last_action = f"🗑️ {interaction.user.display_name} убрал «{track.title[:40]}»"
    await say(interaction, f"🗑️ Убрал {track.label()}")
    await p.refresh_panel()


@bot.tree.command(name="stalker", description="Получить мудрость из Зоны")
async def stalker(interaction: discord.Interaction):
    await say(interaction, f"📜 *{random.choice(STALKER_QUOTES)}*")


@bot.tree.error
async def on_app_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    log.exception("Ошибка команды", exc_info=error)
    try:
        await say(interaction, f"❌ Ошибка: {error}", ephemeral=True)
    except discord.HTTPException:
        pass


def message_content_allowed() -> bool:
    """Включён ли у бота Message Content Intent в Developer Portal (иначе Discord не пустит с этим правом)."""
    import urllib.request
    try:
        req = urllib.request.Request("https://discord.com/api/v10/applications/@me",
                                     headers={"Authorization": f"Bot {TOKEN}", "User-Agent": "DiscordBot (dj-bot, 1.0)"})
        with urllib.request.urlopen(req, timeout=10) as r:
            flags = json.load(r).get("flags", 0)
        return bool(flags & ((1 << 18) | (1 << 19)))  # GATEWAY_MESSAGE_CONTENT (+ LIMITED)
    except Exception as e:
        log.warning("Не смог проверить Message Content Intent: %s", e)
        return False


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("[CRITICAL] Нет DISCORD_TOKEN в .env")
    allowed = message_content_allowed()
    bot._connection._intents.message_content = allowed
    log.info("Заказ треков сообщениями в музыкальном канале: %s", "включён" if allowed else
             "выключен (включи Message Content Intent в Developer Portal)")
    bot.run(TOKEN, log_handler=None)
