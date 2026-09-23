# ──────────────────────────────────────────────────────────────
#  audio_manager.py — yt-dlp ile YouTube ses yönetimi
#
#  • resolve(query)  → bağlantı / oynatma listesi / serbest arama → TrackInfo listesi
#  • download(id)    → sesi dönüştürmeden (ffmpeg gerekmez) downloads/<id>.<ext> olarak indirir
#  • cleanup / purge_all → indirme klasörünün temizliği
#
#  yt-dlp her zaman bu Python ortamındaki paketle (sys.executable -m yt_dlp)
#  çalıştırılır; kullanıcı girdisi asla yt-dlp seçeneği olarak yorumlanamaz.
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit, urlunsplit

from config import Settings

log = logging.getLogger("meetbot.audio")

# ──────────────────────────────────────────────────────────────
#  Sabitler
# ──────────────────────────────────────────────────────────────

VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
PLAYLIST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{2,64}$")
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://", re.IGNORECASE)
_SINGLE_VIDEO_PATH_RE = re.compile(r"^/(?:shorts|live|embed|v|e)/([^/?#]+)")
# Katı alan adı: urlsplit'in ana makine adında bıraktığı ama başka URL ayrıştırıcılarının
# ayraç saydığı karakterler (\, %, @, boşluk...) asla kabul edilmez (SSRF).
_HOSTNAME_RE = re.compile(r"^[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?$")
_NETLOC_CONFUSING_RE = re.compile(r"[\\\s\x00-\x1f\x7f]")
# Yol/sorgu yeniden kodlanırken dokunulmayan karakterler (RFC 3986 "pchar", ':' ve '@' dahil)
_PATH_SAFE = "/:@!$&'()*+,;=-._~"

MAX_QUERY_LENGTH = 500

# Dönüştürme yok: Chrome opus/webm, m4a ve mp4'ü doğrudan çalabiliyor.
AUDIO_FORMAT = "bestaudio[acodec=opus]/bestaudio[ext=m4a]/bestaudio/best"
TEMP_SUFFIXES = (".part", ".ytdl", ".temp", ".tmp")
# --match-filter'a takılan video atlanır: yt-dlp 0 ile çıkar, stdout'a bunu yazar
FILTER_REJECTED = "does not pass filter"
# Başlangıç temizliği yalnızca botun KENDİ yazdığı adlara dokunur: <11 karakterlik YouTube
# kimliği>.<yt-dlp'nin dönüştürmeden yazdığı uzantı>, ara biçimler (<id>.f251.webm,
# <id>.temp.m4a) ve yarım kalan indirme ekleri (.part, .part-Frag3, .ytdl, .temp, .tmp).
# MEETBOT_DOWNLOADS_DIR yanlışlıkla kişisel bir klasörü gösterse de başka dosyalar silinmez.
_OWN_FILE_RE = re.compile(
    r"^[A-Za-z0-9_-]{11}"
    r"(?:\.f[0-9A-Za-z_-]+|\.temp)?"
    r"\.(?:webm|weba|m4a|mp4|opus|ogg|mka|3gp)"
    r"(?:\.(?:part(?:-Frag\d+)?|ytdl|temp|tmp))*$",
    re.IGNORECASE,
)

MAX_PARALLEL_DOWNLOADS = 2
MAX_PARALLEL_RESOLVES = 3
SOCKET_TIMEOUT = 20  # yt-dlp'nin tek bir ağ isteği için bekleme süresi (sn)
# Genel (generic) çıkarıcı kapalı: yt-dlp yalnızca kendi YouTube çıkarıcılarıyla çalışır ve
# eşleşmeyen bir bağlantı için rastgele bir sunucuya HTTP isteği atmaz (SSRF'e karşı ikinci hat).
EXTRACTORS = "default,-generic"

# yt-dlp'nin YouTube JS doğrulaması için kabul ettiği en eski sürümler
# (yt_dlp/utils/_jsruntime.py → *JsRuntime.MIN_SUPPORTED_VERSION). Daha eskisi sessizce yok sayılır.
JS_RUNTIME_MIN_VERSIONS: dict[str, tuple[int, int, int]] = {
    "node": (22, 0, 0),
    "deno": (2, 3, 0),
    "bun": (1, 2, 11),
}
JS_RUNTIME_PROBE_TIMEOUT = 15  # sn

# Oynatma listelerindeki erişilemeyen videoların yt-dlp'deki başlıkları
_UNAVAILABLE_TITLES = {"[Private video]", "[Deleted video]"}

# yt-dlp hata çıktısındaki ipuçları → kullanıcıya gösterilecek kısa mesaj
_ERROR_HINTS: tuple[tuple[str, str], ...] = (
    ("no module named yt_dlp", "yt-dlp kurulu değil (pip install -r requirements.txt)"),
    ("private video", "Bu video gizli"),
    ("confirm your age", "Yaş doğrulaması gerektiren video"),
    ("not a bot", "YouTube bot doğrulaması istedi, biraz sonra tekrar dene"),
    ("members-only", "Sadece kanal üyelerine açık video"),
    ("join this channel", "Sadece kanal üyelerine açık video"),
    ("this live event will begin", "Canlı yayın henüz başlamadı"),
    ("not available in your country", "Bu video bu ülkede kullanılamıyor"),
    ("video unavailable", "Video kullanılamıyor"),
    ("this video is unavailable", "Video kullanılamıyor"),
    ("does not exist", "Oynatma listesi veya video bulunamadı"),
    ("unsupported url", "Bu YouTube bağlantısı desteklenmiyor"),
    ("no suitable extractor", "Bu YouTube bağlantısı desteklenmiyor"),
    ("unable to download webpage", "YouTube'a ulaşılamadı"),
    ("getaddrinfo failed", "YouTube'a ulaşılamadı"),
    ("timed out", "YouTube'a ulaşılamadı"),
)


# ──────────────────────────────────────────────────────────────
#  Hatalar ve veri modeli
# ──────────────────────────────────────────────────────────────

class AudioError(Exception):
    """Kullanıcıya gösterilebilecek kısa Türkçe mesaj taşır (asla dosya yolu içermez)."""


class ResolveError(AudioError):
    """Sorgu çözümlenemedi (geçersiz bağlantı, sonuç yok, yt-dlp hatası...)."""


class DownloadError(AudioError):
    """Ses dosyası indirilemedi."""


@dataclass
class TrackInfo:
    video_id: str
    title: str
    duration: Optional[int]  # saniye; bilinmiyorsa None
    url: str                 # https://www.youtube.com/watch?v=<id>
    thumbnail: str           # https://i.ytimg.com/vi/<id>/mqdefault.jpg
    is_live: bool = False


def canonical_watch_url(video_id: str) -> str:
    return f"https://www.youtube.com/watch?v={video_id}"


def thumbnail_url(video_id: str) -> str:
    return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"


def is_valid_video_id(video_id: object) -> bool:
    return isinstance(video_id, str) and bool(VIDEO_ID_RE.match(video_id))


# ──────────────────────────────────────────────────────────────
#  Sorgu sınıflandırma
# ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Target:
    kind: str   # "video" | "playlist" | "search"
    value: str  # yt-dlp'ye verilecek tek konumsal argüman


def host_allowed(host: str, allowed_hosts: tuple[str, ...]) -> bool:
    host = host.lower()
    if not _HOSTNAME_RE.match(host):
        return False  # ör. "127.0.0.1\.youtube.com": urlsplit için tek ad, urllib3 için 127.0.0.1
    host = host.rstrip(".")
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _canonical_page_url(host: str, path: str, query: str) -> str:
    """Kanal, /@kanal/live, arama sonuç sayfası gibi diğer YouTube sayfalarının yt-dlp'ye
    verilecek adresi. Kullanıcının yazdığı ana makine adı yt-dlp'ye HİÇ gitmez: sabit bir
    YouTube adresi kullanılır, yol ve sorgu yeniden kodlanır, parça (#...) atılır."""
    music = host == "music.youtube.com" or host.endswith(".music.youtube.com")
    clean_path = quote(unquote(path), safe=_PATH_SAFE) or "/"
    clean_query = urlencode(parse_qsl(query, keep_blank_values=True))
    return urlunsplit(("https", "music.youtube.com" if music else "www.youtube.com", clean_path, clean_query, ""))


def classify_query(query: str, allowed_hosts: tuple[str, ...]) -> Target:
    """Kullanıcı girdisini güvenli bir yt-dlp hedefine çevirir.

    • http(s) bağlantısı → sadece izin verilen YouTube alan adları; kanonik URL yeniden kurulur
    • şemasız "youtu.be/xyz" gibi YouTube bağlantıları da bağlantı sayılır
    • diğer her şey → "ytsearch1:<metin>" (ilk arama sonucu)
    """
    text = " ".join(query.split())
    if not text:
        raise ResolveError("Şarkı adı veya bağlantı boş olamaz")
    if len(text) > MAX_QUERY_LENGTH:
        raise ResolveError(f"İstek en fazla {MAX_QUERY_LENGTH} karakter olabilir")

    if _SCHEME_RE.match(text):
        return _classify_url(text, allowed_hosts)
    if " " not in text:
        host = re.split(r"[/?#]", text, maxsplit=1)[0]
        if "." in host and host_allowed(host, allowed_hosts):
            return _classify_url("https://" + text, allowed_hosts)
    return Target("search", f"ytsearch1:{text}")


def _classify_url(url: str, allowed_hosts: tuple[str, ...]) -> Target:
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
    except ValueError:
        raise ResolveError("Geçersiz bağlantı") from None
    if (
        parts.scheme.lower() not in ("http", "https")
        or not host
        or not host_allowed(host, allowed_hosts)
        or _NETLOC_CONFUSING_RE.search(parts.netloc)  # ör. "evil.com\@www.youtube.com"
    ):
        raise ResolveError("Sadece YouTube bağlantıları destekleniyor")
    host = host.rstrip(".")

    query = parse_qs(parts.query)
    path = parts.path or "/"
    list_id = (query.get("list") or [None])[0]
    video_id: Optional[str] = None
    if host == "youtu.be" or host.endswith(".youtu.be"):
        video_id = path.strip("/").split("/")[0]
    elif path.rstrip("/") == "/watch":
        video_id = (query.get("v") or [None])[0]
        if video_id is None and list_id is None:
            raise ResolveError("Geçersiz YouTube bağlantısı")
    else:
        match = _SINGLE_VIDEO_PATH_RE.match(path)
        if match:
            video_id = match.group(1)

    if video_id is not None:
        # watch?v=X&list=Y → sadece X (liste yok sayılır)
        if not is_valid_video_id(video_id):
            raise ResolveError("Geçersiz YouTube bağlantısı")
        return Target("video", canonical_watch_url(video_id))

    if list_id is not None:
        if not PLAYLIST_ID_RE.match(list_id):
            raise ResolveError("Geçersiz oynatma listesi bağlantısı")
        return Target("playlist", f"https://www.youtube.com/playlist?list={list_id}")

    # Kanal, /@kanal/live, arama sonuç sayfası vb. — sabit YouTube adresinde,
    # liste gibi (düz, sınırlı) çözümlenir; canlı yayınlar Player'da reddedilir.
    return Target("playlist", _canonical_page_url(host, path, parts.query))


# ──────────────────────────────────────────────────────────────
#  yt-dlp JSON çıktısını ayrıştırma
# ──────────────────────────────────────────────────────────────

def _parse_duration(value: object) -> Optional[int]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    seconds = int(round(value))
    return seconds if seconds > 0 else None


def entry_to_info(entry: object) -> Optional[TrackInfo]:
    """Tek bir yt-dlp girdisini TrackInfo'ya çevirir; video olmayanları (kanal sekmesi,
    alt liste, gizli/silinmiş video) None döndürerek eler."""
    if not isinstance(entry, dict):
        return None
    if entry.get("_type") not in (None, "video", "url"):
        return None
    if entry.get("ie_key") not in (None, "Youtube"):
        return None
    video_id = entry.get("id")
    if not is_valid_video_id(video_id):
        return None
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        title = video_id
    title = " ".join(title.split())
    if title in _UNAVAILABLE_TITLES:
        return None
    is_live = bool(entry.get("is_live")) or entry.get("live_status") in ("is_live", "is_upcoming")
    return TrackInfo(
        video_id=video_id,
        title=title[:300],
        duration=_parse_duration(entry.get("duration")),
        url=canonical_watch_url(video_id),
        thumbnail=thumbnail_url(video_id),
        is_live=is_live,
    )


def parse_ytdlp_json(raw: str) -> list[TrackInfo]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise ResolveError("Şarkı bilgisi okunamadı") from None
    if not isinstance(data, dict):
        raise ResolveError("Şarkı bilgisi okunamadı")
    entries = data.get("entries") if "entries" in data else [data]
    if not isinstance(entries, list):
        return []
    return [info for info in map(entry_to_info, entries) if info is not None]


def friendly_error(stderr: str, default: str) -> str:
    lowered = stderr.lower()
    for needle, message in _ERROR_HINTS:
        if needle in lowered:
            return message
    return default


# ──────────────────────────────────────────────────────────────
#  Süreç yardımcıları
# ──────────────────────────────────────────────────────────────

def _popen_kwargs() -> dict:
    if os.name == "nt":
        return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
    return {"start_new_session": True}


async def kill_process_tree(proc: asyncio.subprocess.Process) -> None:
    """Süreci ve TÜM alt süreçlerini sonlandırır.

    Windows'ta venv'in python.exe'si asıl yorumlayıcıyı alt süreç olarak başlatır;
    sadece üst süreci öldürmek indirmeyi durdurmaz → taskkill /T.
    """
    if proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            killer = await asyncio.create_subprocess_exec(
                "taskkill", "/PID", str(proc.pid), "/T", "/F",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
                **_popen_kwargs(),
            )
            await asyncio.wait_for(killer.wait(), timeout=10)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, asyncio.TimeoutError, OSError) as exc:
        log.warning("⚠️  yt-dlp süreci ağaç olarak sonlandırılamadı (PID %s): %s", proc.pid, exc)
    if proc.returncode is None:
        try:
            proc.kill()
        except ProcessLookupError:
            pass  # taskkill ile arada zaten sonlandı
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        log.warning("⚠️  yt-dlp süreci kapanmadı (PID %s)", proc.pid)


def parse_version(text: str) -> Optional[tuple[int, int, int]]:
    """"v24.19.0" / "deno 2.3.1 (stable...)" / "1.2.11" → (24, 19, 0); bulunamazsa None."""
    match = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", text or "")
    if not match:
        return None
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch or 0)


def format_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


@functools.lru_cache(maxsize=16)
def js_runtime_version(path: str) -> Optional[tuple[int, int, int]]:
    """`<çalışma zamanı> --version` çıktısındaki sürüm (çalıştırılamazsa None). Sonuç önbelleklenir."""
    try:
        result = subprocess.run([path, "--version"], capture_output=True, text=True, encoding="utf-8",
                                errors="replace", stdin=subprocess.DEVNULL, timeout=JS_RUNTIME_PROBE_TIMEOUT,
                                **_popen_kwargs())
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.debug("JS çalışma zamanı sürümü okunamadı (%s): %s", path, exc)
        return None
    if result.returncode != 0:
        return None
    lines = (result.stdout or "").strip().splitlines()
    return parse_version(lines[0]) if lines else None


class _IdLock:
    """Aynı video için eşzamanlı indirmeleri tek sürece indirger (kullanıcı sayısıyla birlikte)."""

    __slots__ = ("lock", "users")

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.users = 0


class _DownloadSlots:
    """İndirme yuvaları: en fazla `size` ön-indirme + çalınmayı bekleyen parça için 1 ayrılmış yuva.

    Bekleyenler geliş sırasıyla başlar; ama öncelikli video (Player'ın şu an çalmak istediği)
    hepsinin önüne geçer ve normal yuvalar doluysa ayrılmış yuvayı kullanır. Böylece sıradaki
    şarkıların ön-indirmesi, kullanıcının beklediği parçayı asla bekletmez.
    """

    def __init__(self, size: int):
        self.size = size
        self.normal = 0            # kullanımdaki normal yuva sayısı
        self.reserved = False      # ayrılmış yuva kullanımda mı
        self.priority_id: Optional[str] = None
        self._waiters: deque[tuple[str, asyncio.Future]] = deque()

    @property
    def active(self) -> int:
        return self.normal + int(self.reserved)

    def _take(self, video_id: str) -> Optional[str]:
        if self.normal < self.size:
            self.normal += 1
            return "normal"
        if video_id == self.priority_id and not self.reserved:
            self.reserved = True
            return "reserved"
        return None

    async def acquire(self, video_id: str) -> str:
        if not self._waiters or video_id == self.priority_id:
            kind = self._take(video_id)
            if kind is not None:
                return kind
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        entry = (video_id, future)
        self._waiters.append(entry)
        try:
            return await future
        except asyncio.CancelledError:
            if future.done() and not future.cancelled():
                self.release(future.result())  # yuva verildi ama bekleyen iptal edildi → geri bırak
            else:
                try:
                    self._waiters.remove(entry)
                except ValueError:
                    pass
            raise

    def release(self, kind: str) -> None:
        if kind == "reserved":
            self.reserved = False
        else:
            self.normal -= 1
        self._wake()

    def prioritize(self, video_id: Optional[str]) -> None:
        self.priority_id = video_id
        self._wake()

    def _wake(self) -> None:
        # Önce öncelikli bekleyen, sonra geliş sırası (sorted kararlıdır)
        for entry in sorted(self._waiters, key=lambda item: item[0] != self.priority_id):
            video_id, future = entry
            if future.done():
                self._waiters.remove(entry)
                continue
            kind = self._take(video_id)
            if kind is not None:
                self._waiters.remove(entry)
                future.set_result(kind)


# ──────────────────────────────────────────────────────────────
#  Downloader
# ──────────────────────────────────────────────────────────────

class Downloader:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.downloads_dir = Path(settings.downloads_dir).resolve()
        # Test edilebilirlik için ayrı tutulur: yt-dlp HER ZAMAN bu ortamın paketi
        self.command: list[str] = [sys.executable, "-m", "yt_dlp"]
        self._id_locks: dict[str, _IdLock] = {}
        self._download_slots = _DownloadSlots(MAX_PARALLEL_DOWNLOADS)
        self._resolve_slots = asyncio.Semaphore(MAX_PARALLEL_RESOLVES)
        self.js_runtime_args = self._js_runtime_args()
        if self.js_runtime_args and importlib.util.find_spec("yt_dlp_ejs") is None:
            log.warning("⚠️  yt-dlp-ejs kurulu değil; YouTube JS doğrulaması çözülemeyebilir "
                        "(pip install -r requirements.txt)")

    # ── yt-dlp komut satırı ───────────────────────────────────

    def _js_runtime_args(self) -> list[str]:
        mode = self.settings.ytdlp_js_runtime
        if mode == "none":
            return []
        if mode == "auto":
            node = shutil.which("node")
            if node is None:
                log.warning("⚠️  Node.js bulunamadı; YouTube bazı formatları gizleyebilir "
                            "(Node.js kurun veya MEETBOT_YTDLP_JS_RUNTIME ayarlayın)")
                return []
            version = js_runtime_version(node)
            minimum = JS_RUNTIME_MIN_VERSIONS["node"]
            if version is not None and version < minimum:
                # yt-dlp eski Node'u zaten yok sayar; vermek yerine sebebini açıkça söyle
                log.warning("⚠️  Node.js %s çok eski (yt-dlp en az %s istiyor); YouTube bazı formatları "
                            "gizleyebilir — Node.js'i güncelleyin", format_version(version), format_version(minimum))
                return []
            return ["--js-runtimes", "node"]
        return ["--js-runtimes", mode]

    def base_args(self) -> list[str]:
        return [
            "--ignore-config",
            "--no-progress",
            "--encoding", "utf-8",
            "--socket-timeout", str(SOCKET_TIMEOUT),
            "--use-extractors", EXTRACTORS,
            *self.js_runtime_args,
        ]

    def resolve_args(self, target: Target) -> list[str]:
        args = self.base_args() + ["-J"]
        if target.kind == "video":
            args += ["--no-playlist"]
        elif target.kind == "playlist":
            args += ["--flat-playlist", "--playlist-end", str(self.settings.playlist_limit)]
        else:
            args += ["--flat-playlist"]
        # "--": bundan sonrası asla seçenek olarak yorumlanmaz
        return args + ["--", target.value]

    def download_filter(self) -> str:
        """İkinci savunma hattı: çözümlemede süresi bilinmeyen canlı / çok uzun videolar
        (ör. düz arama sonuçları) indirme sırasında yine de reddedilir."""
        rules = ["!is_live"]
        if self.settings.max_duration:
            rules.append(f"duration <=? {self.settings.max_duration}")  # "?": süre yoksa geçer
        return " & ".join(rules)

    def download_args(self, video_id: str) -> list[str]:
        return self.base_args() + [
            "-f", AUDIO_FORMAT,
            "--no-playlist",
            "--match-filter", self.download_filter(),
            "-P", str(self.downloads_dir),
            "-o", f"{video_id}.%(ext)s",
            "--", canonical_watch_url(video_id),
        ]

    async def _run(self, args: list[str], timeout: float) -> tuple[int, str, str]:
        """yt-dlp'yi çalıştırır; zaman aşımında veya iptalde tüm süreç ağacını öldürür."""
        proc = await asyncio.create_subprocess_exec(
            *self.command, *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **_popen_kwargs(),
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except BaseException:
            # TimeoutError, CancelledError (atla/durdur/kapanış) → yarım kalan süreç kalmasın
            await kill_process_tree(proc)
            raise
        err_text = stderr.decode("utf-8", errors="replace")
        for line in err_text.splitlines():
            if line.startswith("WARNING:"):
                log.warning("yt-dlp: %s", line[len("WARNING:"):].strip())
        return proc.returncode or 0, stdout.decode("utf-8", errors="replace"), err_text

    # ── Çözümleme ─────────────────────────────────────────────

    async def resolve(self, query: str) -> list[TrackInfo]:
        target = classify_query(query, self.settings.allowed_hosts)
        log.info("🔍  Çözümleniyor (%s): %s", target.kind, target.value)
        async with self._resolve_slots:
            try:
                code, stdout, stderr = await self._run(self.resolve_args(target), self.settings.metadata_timeout)
            except asyncio.TimeoutError:
                log.warning("⏱️  yt-dlp bilgi çekme zaman aşımı: %s", target.value)
                raise ResolveError("Şarkı bilgisi zamanında alınamadı") from None
            except OSError as exc:
                log.error("❌  yt-dlp başlatılamadı (%s): %s", self.command[0], exc)
                raise ResolveError("yt-dlp çalıştırılamadı") from None
        if code != 0:
            log.error("❌  yt-dlp bilgi hatası (%s): %s", target.value, stderr.strip()[-2000:])
            raise ResolveError(friendly_error(stderr, "Şarkı bilgisi alınamadı"))
        infos = parse_ytdlp_json(stdout)
        if not infos:
            raise ResolveError("Sonuç bulunamadı")
        if target.kind == "playlist":
            infos = infos[: self.settings.playlist_limit]
        elif target.kind == "search":
            infos = infos[:1]
        return infos

    # ── İndirme ───────────────────────────────────────────────

    @staticmethod
    def _is_final_name(path: Path, video_id: str) -> bool:
        """Sadece tam olarak <id>.<ext>: "<id>.webm.part", "<id>.webm.part-Frag1",
        "<id>.temp.m4a", "<id>.f251.webm" gibi yt-dlp ara dosyaları sayılmaz."""
        return path.stem == video_id and path.suffix.lower() not in TEMP_SUFFIXES

    def find_cached(self, video_id: str) -> Optional[Path]:
        """downloads/<id>.<ext> — ara/yarım/boş dosyalar hariç, en yenisi."""
        if not is_valid_video_id(video_id) or not self.downloads_dir.is_dir():
            return None
        candidates = [
            path for path in self.downloads_dir.glob(f"{video_id}.*")
            if self._is_final_name(path, video_id) and path.is_file() and path.stat().st_size > 0
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def prioritize(self, video_id: str) -> None:
        """Player bu videoyu ŞİMDİ çalmak istiyor: indirmesi (bekliyorsa ya da ileride
        başlarsa) diğer bekleyenlerin önüne geçer; normal yuvalar ön-indirmelerle doluysa
        ayrılmış yuvayı kullanır."""
        self._download_slots.prioritize(video_id)

    async def download(self, video_id: str) -> str:
        if not is_valid_video_id(video_id):
            raise DownloadError("Geçersiz video kimliği")
        entry = self._id_locks.setdefault(video_id, _IdLock())
        entry.users += 1
        try:
            async with entry.lock:
                cached = self.find_cached(video_id)
                if cached:
                    log.debug("💾  Önbellekten: %s", cached.name)
                    return str(cached)
                slot = await self._download_slots.acquire(video_id)
                try:
                    return str(await self._download_locked(video_id))
                finally:
                    self._download_slots.release(slot)
        finally:
            entry.users -= 1
            if entry.users == 0:
                self._id_locks.pop(video_id, None)

    async def _download_locked(self, video_id: str) -> Path:
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        # Geçerli önbellek yok → <id>.* ile başlayan her şey önceki denemeden kalan çöptür.
        # (Boş bir <id>.webm kalırsa yt-dlp "zaten indirilmiş" sanıp atlar.)
        self._remove_leftovers(video_id)
        log.info("⬇️  İndiriliyor: %s", video_id)
        completed = False
        try:
            code, stdout, stderr = await self._run(self.download_args(video_id), self.settings.download_timeout)
            if code != 0:
                log.error("❌  yt-dlp indirme hatası (%s): %s", video_id, stderr.strip()[-2000:])
                raise DownloadError(friendly_error(stderr, "Şarkı indirilemedi"))
            path = self.find_cached(video_id)
            if path is None:
                if FILTER_REJECTED in stdout:
                    log.warning("🚫  İndirilmedi (canlı yayın veya çok uzun): %s", video_id)
                    raise DownloadError("Canlı yayınlar ve çok uzun şarkılar indirilemez")
                log.error("❌  yt-dlp başarılı döndü ama dosya yok: %s", video_id)
                raise DownloadError("İndirilen dosya bulunamadı")
            completed = True
        except asyncio.TimeoutError:
            log.warning("⏱️  İndirme zaman aşımı (%s sn): %s", self.settings.download_timeout, video_id)
            raise DownloadError("İndirme zaman aşımına uğradı") from None
        except OSError as exc:
            log.error("❌  yt-dlp başlatılamadı (%s): %s", self.command[0], exc)
            raise DownloadError("yt-dlp çalıştırılamadı") from None
        finally:
            if not completed:
                self._remove_leftovers(video_id)  # hata/zaman aşımı/iptal → yarım dosya kalmasın
        log.info("✅  İndirildi: %s (%.1f MB)", path.name, path.stat().st_size / 1_048_576)
        return path

    def _remove_leftovers(self, video_id: str) -> None:
        """<id>.* dosyalarını siler; yalnızca bu video için geçerli önbellek YOKKEN çağrılır."""
        for path in self.downloads_dir.glob(f"{video_id}.*"):
            if not path.is_file():
                continue
            try:
                path.unlink()
            except OSError as exc:
                log.warning("⚠️  Yarım dosya silinemedi (%s): %s", path.name, exc)

    # ── Temizlik ──────────────────────────────────────────────

    def _inside_downloads(self, path: Path) -> bool:
        try:
            return path.resolve().parent == self.downloads_dir
        except OSError:
            return False

    def cleanup(self, path: str) -> None:
        """Tek bir indirilen dosyayı siler (sadece indirme klasörünün içindeyse)."""
        target = Path(path)
        if not self._inside_downloads(target):
            log.warning("⚠️  İndirme klasörü dışındaki dosya silinmedi: %s", path)
            return
        try:
            target.unlink()
            log.info("🗑️  Dosya silindi: %s", target.name)
        except FileNotFoundError:
            pass  # zaten silinmiş: istenen durum
        except OSError as exc:
            log.warning("⚠️  Dosya silinemedi (%s): %s", target.name, exc)

    @staticmethod
    def is_own_file(name: str) -> bool:
        """Dosya adı botun kendi indirmelerinden biri mi (<id>.<uzantı> ya da yt-dlp ara/yarım dosyası)?"""
        return bool(_OWN_FILE_RE.match(name))

    def purge_all(self) -> int:
        """Başlangıç temizliği: indirme klasöründe botun ÖNCEKİ çalışmadan kalan dosyalarını siler.

        Yalnızca botun yazdığı adlara uyan dosyalar silinir (bkz. _OWN_FILE_RE); başka dosyalara
        ve alt klasörlere asla dokunulmaz (klasör yanlışlıkla ör. "Müzik" klasörünü gösterebilir).
        """
        self.downloads_dir.mkdir(parents=True, exist_ok=True)
        removed = 0
        for path in self.downloads_dir.iterdir():
            if not self.is_own_file(path.name) or path.is_symlink() or not path.is_file():
                continue
            try:
                path.unlink()
                removed += 1
            except OSError as exc:
                log.warning("⚠️  Eski dosya silinemedi (%s): %s", path.name, exc)
        if removed:
            log.info("🧹  İndirme klasörü temizlendi (%d dosya)", removed)
        return removed
