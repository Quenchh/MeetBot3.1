# ──────────────────────────────────────────────────────────────
#  config.py — Ayarlar (.env dosyası + ortam değişkenleri)
#
#  Öncelik: ortam değişkeni > .env dosyası > varsayılan değer.
#  Tüm anahtarlar MEETBOT_ önekiyle başlar (bkz. .env.example).
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import logging
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, Optional

VERSION = "3.2.0"

BASE_DIR = Path(__file__).resolve().parent

log = logging.getLogger("meetbot.config")

_TRUE = {"1", "true", "yes", "on", "evet", "e"}
_FALSE = {"0", "false", "no", "off", "hayir", "hayır", "h"}


def _parse_dotenv(path: Path) -> dict[str, str]:
    """Basit .env ayrıştırıcı (bağımlılıksız). KEY=VALUE, # yorum, tırnaklar."""
    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        key, sep, value = line.partition("=")
        if not sep:
            continue
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        else:
            # Tırnaksız değerlerde satır sonu yorumlarını at: KEY=abc  # yorum
            hash_pos = value.find(" #")
            if hash_pos != -1:
                value = value[:hash_pos].rstrip()
        values[key] = value
    return values


class _Source:
    def __init__(self, env: Mapping[str, str], dotenv: Mapping[str, str]):
        self.env = env
        self.dotenv = dotenv

    def raw(self, key: str) -> Optional[str]:
        if key in self.env:
            return self.env[key]
        return self.dotenv.get(key)

    def str(self, key: str, default: str) -> str:
        value = self.raw(key)
        return default if value is None else value.strip()

    def int(self, key: str, default: int, minimum: Optional[int] = None, maximum: Optional[int] = None) -> int:
        value = self.raw(key)
        if value is None or value.strip() == "":
            result = default
        else:
            try:
                result = int(value.strip())
            except ValueError:
                log.warning("⚠️  %s için geçersiz sayı (%r), varsayılan kullanılıyor: %s", key, value, default)
                result = default
        if minimum is not None and result < minimum:
            result = minimum
        if maximum is not None and result > maximum:
            result = maximum
        return result

    def bool(self, key: str, default: bool) -> bool:
        value = self.raw(key)
        if value is None or value.strip() == "":
            return default
        norm = value.strip().lower()
        if norm in _TRUE:
            return True
        if norm in _FALSE:
            return False
        log.warning("⚠️  %s için geçersiz evet/hayır değeri (%r), varsayılan kullanılıyor: %s", key, value, default)
        return default

    def list(self, key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = self.raw(key)
        if value is None or value.strip() == "":
            return default
        return tuple(item.strip().lower() for item in value.split(",") if item.strip())


def _resolve_dir(value: str) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = BASE_DIR / path
    return path


@dataclass
class Settings:
    # ── Sunucu ────────────────────────────────────────────────
    host: str = "0.0.0.0"
    port: int = 8000
    log_level: str = "INFO"
    # Panele IP adresi, localhost / bilgisayar adı ya da .local/.lan gibi yerel adlar DIŞINDA bir
    # alan adıyla (ör. ters vekil) erişiliyorsa o adlar. Diğer Host başlıkları DNS rebinding'e
    # karşı reddedilir. "*" denetimi kapatır.
    trusted_hostnames: tuple[str, ...] = ()

    # ── Yetkilendirme ─────────────────────────────────────────
    admin_password: str = ""
    admin_password_generated: bool = False  # True ise şifre otomatik üretildi (başlangıçta ekrana basılır)
    guest_controls: bool = True             # Misafirler oynatmayı kontrol edebilsin mi (duraklat/geç/sırala/ses...)
    # Bot ekranı (yöneticinin panelden botun tarayıcısını görüp kullanması, ör. Google girişi):
    # "local" → yalnızca bu bilgisayardan / SSH tüneliyle, "on" → her yerden, "off" → kapalı
    remote_view: str = "local"

    # ── Kuyruk sınırları ──────────────────────────────────────
    max_queue: int = 100          # Kuyruktaki en fazla şarkı
    max_user_queue: int = 0       # Kişi başı kuyruktaki en fazla şarkı (0 = sınırsız)
    max_duration: int = 0         # Saniye cinsinden en uzun şarkı (0 = sınırsız)
    playlist_limit: int = 25      # Bir oynatma listesinden eklenecek en fazla şarkı
    prefetch: int = 2             # Önceden indirilecek sıradaki şarkı sayısı
    history_size: int = 20        # "Son çalınanlar" listesinin uzunluğu

    # ── Kaynaklar ─────────────────────────────────────────────
    allowed_hosts: tuple[str, ...] = ("youtube.com", "youtu.be", "music.youtube.com")
    ytdlp_js_runtime: str = "auto"  # "auto" (node varsa kullan), "node", "deno", "none"
    metadata_timeout: int = 45     # yt-dlp bilgi çekme zaman aşımı (sn)
    download_timeout: int = 900    # yt-dlp indirme zaman aşımı (sn; süre sınırı yokken uzun videolar için geniş)

    # ── Tarayıcı / Bot ────────────────────────────────────────
    bot_name: str = "MeetBot"      # Google hesabı yoksa Meet'te görünecek isim
    chrome_path: str = ""          # Boşsa otomatik bulunur
    cdp_port: int = 9222
    profile_dir: Path = field(default_factory=lambda: BASE_DIR / "chrome_profil")
    downloads_dir: Path = field(default_factory=lambda: BASE_DIR / "downloads")
    join_timeout: int = 180        # Toplantıya kabul edilmeyi bekleme süresi (sn)
    normalize: bool = True         # Web Audio kompresörü ile ses seviyesini dengele

    @property
    def public_config(self) -> dict:
        """İstemcilere gönderilebilecek (gizli olmayan) sınırlar."""
        return {
            "max_queue": self.max_queue,
            "max_user_queue": self.max_user_queue,
            "max_duration": self.max_duration,
            "playlist_limit": self.playlist_limit,
            "guest_controls": self.guest_controls,
            "remote_view": self.remote_view,
        }


def load_settings(env: Optional[Mapping[str, str]] = None, dotenv_path: Optional[Path] = None) -> Settings:
    """Ayarları ortam değişkenleri ve .env dosyasından yükler.

    Testlerde `env={...}` ve `dotenv_path=Path("yok")` vererek izole kullanılabilir.
    """
    env = os.environ if env is None else env
    dotenv = _parse_dotenv(dotenv_path if dotenv_path is not None else BASE_DIR / ".env")
    src = _Source(env, dotenv)

    password = src.str("MEETBOT_ADMIN_PASSWORD", "")
    generated = False
    if not password:
        password = secrets.token_urlsafe(9)
        generated = True

    log_level = src.str("MEETBOT_LOG_LEVEL", "INFO").upper()
    if log_level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
        log_level = "INFO"

    remote_view = src.str("MEETBOT_REMOTE_VIEW", "local").lower()
    if remote_view not in {"local", "on", "off"}:
        log.warning("⚠️  MEETBOT_REMOTE_VIEW için geçersiz değer (%r), 'local' kullanılıyor", remote_view)
        remote_view = "local"

    js_runtime = src.str("MEETBOT_YTDLP_JS_RUNTIME", "auto").lower()
    if js_runtime not in {"auto", "node", "deno", "bun", "none"}:
        js_runtime = "auto"

    return Settings(
        host=src.str("MEETBOT_HOST", "0.0.0.0") or "0.0.0.0",
        port=src.int("MEETBOT_PORT", 8000, 1, 65535),
        log_level=log_level,
        trusted_hostnames=src.list("MEETBOT_TRUSTED_HOSTNAMES", ()),
        admin_password=password,
        admin_password_generated=generated,
        guest_controls=src.bool("MEETBOT_GUEST_CONTROLS", True),
        remote_view=remote_view,
        max_queue=src.int("MEETBOT_MAX_QUEUE", 100, 1),
        max_user_queue=src.int("MEETBOT_MAX_USER_QUEUE", 0, 0),
        max_duration=src.int("MEETBOT_MAX_DURATION", 0, 0),
        playlist_limit=src.int("MEETBOT_PLAYLIST_LIMIT", 25, 1, 200),
        prefetch=src.int("MEETBOT_PREFETCH", 2, 0, 10),
        history_size=src.int("MEETBOT_HISTORY_SIZE", 20, 0, 200),
        allowed_hosts=src.list("MEETBOT_ALLOWED_HOSTS", ("youtube.com", "youtu.be", "music.youtube.com")),
        ytdlp_js_runtime=js_runtime,
        metadata_timeout=src.int("MEETBOT_METADATA_TIMEOUT", 45, 5),
        download_timeout=src.int("MEETBOT_DOWNLOAD_TIMEOUT", 900, 10),
        bot_name=(src.str("MEETBOT_BOT_NAME", "MeetBot") or "MeetBot")[:60],
        chrome_path=src.str("MEETBOT_CHROME_PATH", ""),
        cdp_port=src.int("MEETBOT_CDP_PORT", 9222, 1, 65535),
        profile_dir=_resolve_dir(src.str("MEETBOT_PROFILE_DIR", "chrome_profil") or "chrome_profil"),
        downloads_dir=_resolve_dir(src.str("MEETBOT_DOWNLOADS_DIR", "downloads") or "downloads"),
        join_timeout=src.int("MEETBOT_JOIN_TIMEOUT", 180, 10),
        normalize=src.bool("MEETBOT_NORMALIZE", True),
    )
