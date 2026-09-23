# ──────────────────────────────────────────────────────────────
#  bot.py — MeetBot: Chrome (CDP) + Playwright ile Google Meet botu
#
#  • Chrome'u kendi profiliyle ve sahte medya cihazlarıyla başlatır
#    (CDP portunda zaten bir tarayıcı varsa uyararak ona bağlanır).
#  • Meet'e (TR/EN arayüz) katılır: ad yazar, kamerayı kapatır, kabul /
#    ret / zaman aşımını ayırt eder. Katılım iptal edilebilir.
#  • meetbot_inject.js ile Web Audio motorunu sekmeye enjekte eder; ses
#    dosyasını diskten okuyup CDP üzerinden sayfaya taşır ve çalar
#    (Meet sayfası 127.0.0.1'e erişemez — Local Network Access).
#  • Toplantıyı izler: ilerleme, şarkı bitti, toplantıdan düşme, çökme;
#    "Hâlâ orada mısınız?" sorusunu yanıtlar, bekleme odasına geri
#    gönderilince sekmeyi kapatmadan yeniden kabulü bekler.
#
#  Player ile sözleşme SPEC §2'dedir (on_status / on_track_ended / on_progress).
# ──────────────────────────────────────────────────────────────

from __future__ import annotations

import asyncio
import base64
import functools
import json
import logging
import math
import os
import platform
import re
import shutil
import signal
import socket
import subprocess
import urllib.request
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Locator,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    async_playwright,
)
from playwright_stealth import Stealth

from config import BASE_DIR, Settings
from create_silence import DEFAULT_PATH as DEFAULT_SILENCE_WAV, create_silence_wav

log = logging.getLogger("meetbot.bot")

# ──────────────────────────────────────────────────────────────
#  Sabitler
# ──────────────────────────────────────────────────────────────

MEET_ORIGIN = "https://meet.google.com"
INJECT_SCRIPT_PATH = BASE_DIR / "meetbot_inject.js"
SILENCE_WAV = DEFAULT_SILENCE_WAV   # Chrome'un sahte mikrofonu (yoksa otomatik oluşturulur)

UPLOAD_CHUNK_BYTES = 512 * 1024   # sayfaya taşınan her base64 parçasının ham boyutu
CALL_TIMEOUT = 15.0               # sn — tek bir sayfa komutu (pause, seek, ...)
PLAY_TIMEOUT = 45.0               # sn — play(): yükleme + konuma atlama (JS tarafı 30+10 sn)
PROBE_TIMEOUT = 10.0              # sn — izleme / katılım yoklaması
NAVIGATION_TIMEOUT_MS = 45_000
CHROME_START_TIMEOUT = 20.0       # sn — Chrome'un CDP portunu açması
TOGGLE_FIND_TIMEOUT = 2.0         # sn — mikrofon/kamera düğmesinin görünmesi
TOGGLE_CONFIRM_TIMEOUT = 3.0      # sn — mikrofon/kamera düğmesinin durum değiştirmesi
VIRTUAL_SCREEN = (1920, 1080)     # Linux sanal ekranının (Xvfb) boyutu
XVFB_START_TIMEOUT = 10.0         # sn
VIEW_TIMEOUT = 10.0               # sn — bot ekranı: ekran görüntüsü / giriş işlemi
VIEW_JPEG_QUALITY = 55
SIGNED_IN_CHECK_INTERVAL = 5.0    # sn — Google oturum çerezi denetimi
MAX_POPUPS = 5

AUDIO_MIME_TYPES = {
    ".webm": "audio/webm",
    ".weba": "audio/webm",
    ".m4a": "audio/mp4",
    ".mp4": "audio/mp4",
    ".opus": "audio/ogg",
    ".ogg": "audio/ogg",
    ".oga": "audio/ogg",
    ".mp3": "audio/mpeg",
    ".wav": "audio/wav",
    ".flac": "audio/flac",
    ".aac": "audio/aac",
}

# ──────────────────────────────────────────────────────────────
#  Hatalar
# ──────────────────────────────────────────────────────────────


class BotError(Exception):
    """Kullanıcıya gösterilebilecek (Türkçe) bot hatası."""


class BotNotReady(BotError):
    """Bot toplantıda değilken ses komutu geldi."""


class JoinDenied(BotError):
    """Toplantıya girilemedi (ret, zaman aşımı, geçersiz kod...). Tekrar denenmez."""


class PlaybackCancelled(BotError):
    """Çalma isteği, arkasından gelen daha yeni bir play()/stop() yüzünden iptal edildi."""


class _RetryableJoinError(Exception):
    """Sekme çöktü / kapandı / sayfa açılamadı: katılım yeniden denenebilir."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


INJECT_ERROR_MESSAGES = {
    "SUPERSEDED": "Çalma iptal edildi (yerine yeni bir istek geldi)",
    "LOAD_ERROR": "Ses dosyası çözümlenemedi (biçim desteklenmiyor olabilir)",
    "LOAD_TIMEOUT": "Ses dosyası zamanında yüklenemedi",
    "PLAY_ERROR": "Tarayıcı çalmayı başlatamadı",
    "NO_TRACK": "Yüklü bir şarkı yok",
    "EMPTY": "Ses dosyası boş",
    "MISSING": "Ses motoru sayfada bulunamadı",
}

# ──────────────────────────────────────────────────────────────
#  Meet arayüzü (TR + EN)
# ──────────────────────────────────────────────────────────────

# Toplantının İÇİNDE olduğumuzun tek göstergesi "Görüşmeden ayrıl" düğmesidir.
# Mikrofon / kamera / "Diğer seçenekler" düğmeleri katılma ekranında ve kabul
# beklerken de vardır; onlara bakmak kabulü, reddi ve zaman aşımını gizler.
LEAVE_BUTTON_SELECTOR = ", ".join((
    '[aria-label^="Leave call" i]',
    '[aria-label^="Görüşmeden ayrıl" i]',
    '[data-tooltip^="Leave call" i]',
    '[data-tooltip^="Görüşmeden ayrıl" i]',
))

NAME_INPUT_SELECTOR = ", ".join((
    'input[aria-label*="your name" i]',
    'input[placeholder*="your name" i]',
    'input[aria-label*="adınız" i]',
    'input[placeholder*="adınız" i]',
    'input[autocomplete="name"]',
))

# Birincil katılma düğmesi. Meet bu düğmenin adını ~30 varyanttan üretir ("Join now", "Ask to join",
# "Join the call now", "Join anyway", "Ask to join anyway", "Join as a viewer", "Join here too"…) ve
# cihaz sorununda aria-label'ı "… without microphone & camera" yapar. Bu yüzden tam liste yerine
# baştan eşleşme + dışlama: "Other ways to join", Companion / telefonla katılma ve oturumu bu sekmeye
# TAŞIYAN "Switch here / Buraya geç" asla eşleşmez. (Playwright ifadeyi JS'e taşır: \b yalnızca ASCII
# harflerin yanında kullanıldı; "add_to_queue" = "Join here too" düğmesinin simgesi.)
_NOT_JOIN_PATTERN = (
    r"other ways|other joining|companion|\bphone\b|switch here"
    r"|diğer|yöntem|yardımcı|telefon|buraya geç"
)
JOIN_BUTTON_RE = re.compile(
    rf"^(?!.*(?:{_NOT_JOIN_PATTERN}))\s*"
    r"(?:(?:add_to_queue\s*)?(?:ask to )?join(?!\s+(?:with|and|by|using|via|from)\b)(?:\s|$)"
    r"|(?:.*\s)?katıl(?:\s|$)|.*katılma isteğ)",
    re.IGNORECASE,
)
NOT_JOIN_RE = re.compile(_NOT_JOIN_PATTERN, re.IGNORECASE)
JOIN_WORD_RE = re.compile(r"join|katıl", re.IGNORECASE)   # yalnızca tanı iletisi için
ASK_TO_JOIN_RE = re.compile(r"ask to join|katılma isteği", re.IGNORECASE)
# Bot profilindeki hesap toplantıda zaten varsa Meet birincil düğmeyi "Switch here" yapar. Ona basmak
# o oturumu (ör. operatörün kendisini) toplantıdan bu sekmeye taşır: ASLA tıklanmaz. Onun yerine
# kapalı "Other ways to join" bölümü açılır ve oradaki "Join here too / Burada da katıl" kullanılır.
SWITCH_HERE_RE = re.compile(r"^\s*(?:switch here|buraya geç)", re.IGNORECASE)
OTHER_WAYS_RE = re.compile(r"other ways to join|other joining options|diğer katılma", re.IGNORECASE)
POPUP_BUTTON_RE = re.compile(
    r"^\s*(got it|dismiss|no thanks|close|anladım|hayır,? teşekkürler|kapat)\s*$", re.IGNORECASE
)
MORE_OPTIONS_RE = re.compile(r"^\s*(more options|diğer seçenekler)\s*$", re.IGNORECASE)
# Menü öğesi / sekme içindeki Material simgesi aria-hidden değilse Chrome adı boşluksuz
# birleştirir ("settingsSettings", "volume_upAudio"): baştaki küçük harfli simge adına izin
# verilir. (Playwright bu ifadeleri JS'e taşır; satır içi bayrak kullanılamadığından büyük/küçük
# harf elle yazıldı — "Other settings" gibi başka öğeler eşleşmez.)
SETTINGS_ITEM_RE = re.compile(r"^\s*(?:[a-z_]+\s*)?(?:[Ss]ettings|[Aa]yarlar)\s*$")
AUDIO_TAB_RE = re.compile(r"^\s*(?:[a-z_]+\s*)?(?:[Aa]udio(?: [Ss]ettings)?|[Ss]es(?: [Aa]yarları)?)\s*$")
GENERAL_TAB_RE = re.compile(r"^\s*(?:[a-z_]+\s*)?(?:[Gg]eneral|[Gg]enel)\s*$")
DIALOG_CLOSE_RE = re.compile(r"^\s*(close|close dialog|kapat|[iİ]letişim kutusunu kapat)\s*$", re.IGNORECASE)
# Meet'in konuşma dışı sesleri süzen filtresi müziği boğar. Adı değişiyor: eskiden "Gürültü giderme /
# Noise cancellation", 2026'da TR arayüzde "Stüdyo ses kalitesi" (açıklaması aynı: "…konuşma olmayan
# sesleri filtreler"). Etiket ya da açıklama (önek) eşleşirse o satırdaki anahtar kapatılır.
NOISE_CANCELLATION_LABELS = (
    "noise cancellation", "gürültü giderme", "gürültü engelleme", "gürültü önleme",
    "stüdyo ses", "studio sound", "studio audio", "studio-quality",
    "mikrofonunuza gelen konuşma olmayan", "konuşma olmayan sesleri", "filters out sound that isn",
    "filters out non-speech",
)
# Ayarlar → Genel → "Leave empty calls" (varsayılan AÇIK): bot görüşmede tek kalınca Meet birkaç dakika
# sonra "Hâlâ orada mısınız?" diye sorar, yanıt gelmezse botu çıkarır. (TR etiketleri önek olarak eşleşir.)
LEAVE_EMPTY_CALL_LABELS = ("leave empty call", "boş görüşme", "boş arama", "boş toplantı")

# Meet'in "Hâlâ orada mısınız?" / "görüşmede kal mı?" sorusu (JS'te de derlenir: yalnızca ortak sözdizimi).
# Güçlü etiketler her görünür iletişim kutusunda, zayıf olanlar ("Continue", "Yes") yalnızca metni bu
# soruya benzeyen kutuda tıklanır. Bot tıklayacağı düğmeyi data-meetbot-stay ile işaretler.
PROMPT_TEXT_PATTERN = (
    r"are you still there|still in (?:the|this) call\?|you(?:'re| are) the only one"
    r"|h[aâ]l[aâ] (?:orada|burada) m[ıi]s[ıi]n[ıi]z|görüşmede tek kişi|toplantıda tek kişi"
)
STAY_STRONG_PATTERN = (
    r"^\s*(?:stay\b|keep waiting|keep me in|i'?m (?:still )?here|i am (?:still )?here|still here"
    r"|(?:görüşmede |toplantıda )?kal\s*$|beklemeye devam|(?:h[aâ]l[aâ] |evet,? )?buradayım)"
)
STAY_WEAK_PATTERN = r"^\s*(?:continue|yes|ok|devam(?: et)?|evet|tamam)\s*$"
STAY_MARK_SELECTOR = "[data-meetbot-stay]"
PROMPT_TEXT_RE = re.compile(PROMPT_TEXT_PATTERN, re.IGNORECASE)
STAY_STRONG_RE = re.compile(STAY_STRONG_PATTERN, re.IGNORECASE)
STAY_WEAK_RE = re.compile(STAY_WEAK_PATTERN, re.IGNORECASE)


def _toggle_selector(*words: str) -> str:
    return ", ".join(f'{tag}[aria-label*="{word}" i]' for word in words for tag in ("button", '[role="button"]'))


TOGGLE_SELECTORS = {
    "mic": _toggle_selector("microphone", "mikrofon"),
    "camera": _toggle_selector("camera", "kamera"),
}
# Etiket "aç / turn on" diyorsa cihaz şu an KAPALI, "kapat / turn off" diyorsa AÇIK.
_TOGGLE_OFF_NOW_RE = re.compile(r"^\s*(turn on|unmute\b|\S+ aç\b)", re.IGNORECASE)
_TOGGLE_ON_NOW_RE = re.compile(r"^\s*(turn off|mute\b|\S+ kapat\b)", re.IGNORECASE)


def _fold(text: str) -> str:
    """Küçük harfe çevirir; Türkçe ı/İ farkını yok sayar (ÇIKARILDINIZ → çikarildiniz)."""
    return text.lower().replace("i\u0307", "i").replace("ı", "i")


LEFT_FROM_WINDOW_DETAIL = "Bot toplantıdan ayrıldı (Meet penceresinden)"
STILL_THERE_DETAIL = "Meet 'Hâlâ orada mısınız?' sorusu yanıtlanmadığı için botu çıkardı"

# Katılamama / toplantıdan düşme metinleri → kısa Türkçe açıklama (sıra önemli: özelden genele).
# Metinler Meet'in katılım hatası listesinden (EN); TR karşılıklarının bir kısmı tahminidir.
_MEET_FAILURE_TEXTS = (
    (r"denied your request|isteğinizi reddetti|isteğiniz reddedildi",
     "Toplantı sahibi katılma isteğini reddetti"),
    (r"no one responded to your request|isteğinize (?:kimse )?yanıt ver(?:il)?medi",
     "Katılma isteğine kimse yanıt vermedi"),
    (r"you've been removed|you have been removed|removed you from the (?:meeting|call)"
     r"|(?:toplantıdan|görüşmeden) çıkarıldınız|sizi (?:toplantıdan|görüşmeden) çıkardı",
     "Toplantıdan çıkarıldı"),
    (r"are you still there|h[aâ]l[aâ] orada m[ıi]s[ıi]n[ıi]z"
     r"|you left the (?:meeting|call) because|left (?:the|this) empty (?:meeting|call)"
     r"|boş (?:görüşmeden|toplantıdan) (?:otomatik olarak )?ayrıldınız",
     STILL_THERE_DETAIL),
    (r"you left the (?:meeting|call)|you left the video call|(?:toplantıdan|görüşmeden) ayrıldınız",
     LEFT_FROM_WINDOW_DETAIL),
    (r"(?:meeting|call) has (?:already )?ended|ended the (?:meeting|call) for everyone"
     r"|(?:toplantı|görüşme) (?:zaten )?sona erdi|herkes için sonlandırdı",
     "Toplantı sona erdi"),
    (r"check your meeting code|invalid video call name|meeting code you entered doesn't work"
     r"|toplantı kodunuzu kontrol edin|geçersiz görüntülü görüşme adı",
     "Toplantı bulunamadı (bağlantıyı kontrol edin)"),
    (r"meeting code has expired|toplantı kodunun süresi (?:doldu|dolmuş)",
     "Toplantı kodunun süresi dolmuş"),
    (r"you can't create a meeting yourself|kendiniz toplantı oluşturamazsınız",
     "Toplantı henüz başlamamış (bot toplantı başlatamaz)"),
    (r"this meeting is full|reached the maximum of \d+ participants|(?:toplantı|görüşme) dolu",
     "Toplantı dolu (katılımcı sınırına ulaşıldı)"),
    (r"restricted to an organization you don't belong to|ait olmadığınız bir kuruluşa",
     "Toplantı yalnızca bir kuruluşun üyelerine açık (bot profilinde yetkili bir hesapla oturum açın)"),
    (r"in a video call in another window|başka bir pencerede (?:bir )?(?:görüntülü )?görüşme",
     "Bot profili başka bir pencerede zaten bir görüşmede"),
    (r"you can't join this (?:video )?call|bu (?:video |görüntülü )?görüşme(?:ye|sine) katılamazsınız"
     r"|you aren't allowed to join|you're not allowed to join|katılmanıza izin verilmiyor",
     "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),
)
MEET_FAILURES = tuple((re.compile(_fold(pattern)), detail) for pattern, detail in _MEET_FAILURE_TEXTS)
CALL_LOST_DETAIL = "Toplantıdan çıkarıldı veya toplantı sona erdi"

# Toplantı sahibi botu toplantıdan bekleme odasına geri gönderdi (ya da bot yeniden kabul bekliyor):
# kurtarılabilir bir durum — sekme kapatılmaz, sahibin geri alması beklenir.
_WAITING_ROOM_TEXT = (
    r"sent (?:you )?(?:back )?to the waiting room|moved (?:you )?(?:back )?to the waiting room"
    r"|brings you into the call|when someone lets you in"
    r"|bekleme odasına (?:geri )?(?:gönderildiniz|alındınız|taşındınız)|sizi bekleme odasına"
    r"|sizi içeri aldığında|sizi görüşmeye al"
)
WAITING_ROOM_RE = re.compile(_fold(_WAITING_ROOM_TEXT))
WAITING_ROOM_DETAIL = "Bekleme odasına alındı, toplantı sahibinin geri alması bekleniyor…"
READMITTED_DETAIL = "Toplantıya yeniden kabul edildi"

# ──────────────────────────────────────────────────────────────
#  Sayfada çalışan küçük JS parçaları (veri HER ZAMAN argümanla geçer)
# ──────────────────────────────────────────────────────────────

_INVOKE_JS = """([method, args]) => {
    const api = window.__meetbot;
    if (!api) throw new Error("MEETBOT_MISSING: ses motoru sayfada yok");
    return api[method](...args);
}"""

_STOP_JS = "() => { if (window.__meetbot) window.__meetbot.stop(); }"

_APPLY_SETTINGS_JS = """(s) => {
    const api = window.__meetbot;
    if (!api) throw new Error("MEETBOT_MISSING: ses motoru sayfada yok");
    api.setMusicVolume(s.music);
    api.setMicVolume(s.mic);
    api.setNormalize(s.normalize);
}"""

# Tek yoklama: toplantıda mıyız + ses durumu (+ toplantı dışındaysak sayfa metni) + Meet'in
# "Hâlâ orada mısınız?" türü sorusu (varsa; yanıtlayacak düğme data-meetbot-stay ile işaretlenir).
_PROBE_JS = r"""(p) => {
    const visible = (el) => !!el && el.getClientRects().length > 0 && getComputedStyle(el).visibility !== "hidden";
    const norm = (s) => (s || "").replace(/’/g, "'").replace(/\s+/g, " ").trim();
    let inCall = Array.from(document.querySelectorAll(p.leave)).some(visible);
    if (!inCall) {
        // Dilden bağımsız yedek: "Görüşmeden ayrıl" düğmesinin call_end simgesi.
        inCall = Array.from(document.querySelectorAll("button i, [role='button'] i")).some(
            (icon) => icon.textContent.trim() === "call_end" && visible(icon.closest("button, [role='button']")));
    }

    // Soru: görünür iletişim kutuları; toplantı dışındayken (bekleme ekranı) sayfanın kendisi de.
    // Toplantıdayken sayfa gövdesine bakılmaz: sohbetteki bir mesaj soru sanılmasın.
    document.querySelectorAll(p.mark).forEach((el) => el.removeAttribute("data-meetbot-stay"));
    const promptRe = new RegExp(p.prompt, "i"), strongRe = new RegExp(p.strong, "i"), weakRe = new RegExp(p.weak, "i");
    const label = (el) => norm(el.getAttribute("aria-label") || el.innerText);
    const boxes = Array.from(document.querySelectorAll('[role="dialog"], [role="alertdialog"]')).filter(visible);
    if (!inCall && document.body) boxes.push(document.body);
    let prompt = null;
    for (const box of boxes) {
        const text = norm(box.innerText);
        const isBody = box === document.body;
        const asked = promptRe.test(text) && (isBody || text.length < 1500);
        if (isBody && !asked) continue;
        const buttons = Array.from(box.querySelectorAll("button, [role='button']")).filter(visible);
        let target = buttons.find((b) => strongRe.test(label(b)));
        if (!target && asked) target = buttons.find((b) => weakRe.test(label(b)));
        if (!target && !asked) continue;
        if (target) target.setAttribute("data-meetbot-stay", "");
        prompt = { text: text.slice(0, 300), stay: target ? label(target) : null };
        break;
    }

    const api = window.__meetbot;
    return {
        in_call: inCall,
        audio: api ? api.status() : null,
        text: inCall || !document.body ? "" : document.body.innerText.slice(0, 20000),
        prompt,
    };
}"""

# Katılma düğmesi bulunamayınca tanı için: görünen düğmelerin adları.
_VISIBLE_BUTTONS_JS = r"""() => Array.from(document.querySelectorAll("button, [role='button']"))
    .filter((el) => el.getClientRects().length > 0)
    .map((el) => (el.getAttribute("aria-label") || el.innerText || "").replace(/\s+/g, " ").trim())
    .filter(Boolean)
    .slice(0, 30)"""

_PROBE_ARGS = {
    "leave": LEAVE_BUTTON_SELECTOR,
    "mark": STAY_MARK_SELECTOR,
    "prompt": PROMPT_TEXT_PATTERN,
    "strong": STAY_STRONG_PATTERN,
    "weak": STAY_WEAK_PATTERN,
}

_TOGGLE_ATTRS_JS = "(els) => els.map((el) => [el.getAttribute('data-is-muted'), el.getAttribute('aria-label') || ''])"

# Ayarlar penceresindeki bir anahtarı (gürültü giderme, boş görüşmelerden ayrıl) ETİKETİNE göre
# bulur (etiketin satırındaki tek anahtar) ve açıksa kapatır. Belgedeki ilk anahtarı almak başka
# bir ayarı değiştirebilirdi.
_SWITCH_OFF_JS = r"""async (labels) => {
    const wanted = labels.map((label) => label.toLowerCase());
    const norm = (s) => (s || "").replace(/\s+/g, " ").trim().toLowerCase();
    const isLabel = (s) => {
        const text = norm(s);
        return text.length > 0 && wanted.some((w) => text === w || text.startsWith(w));
    };
    const visible = (el) => el.getClientRects().length > 0;
    const ownText = (el) => Array.from(el.childNodes)
        .filter((n) => n.nodeType === Node.TEXT_NODE).map((n) => n.textContent).join(" ");
    const labelledBy = (el) => (el.getAttribute("aria-labelledby") || "").split(/\s+/)
        .map((id) => id && document.getElementById(id)).filter(Boolean).map((n) => n.textContent).join(" ");

    const findSwitch = () => {
        const dialogs = Array.from(document.querySelectorAll('[role="dialog"]')).filter(visible);
        const root = dialogs.length ? dialogs[dialogs.length - 1] : document.body;
        const switches = Array.from(root.querySelectorAll('[role="switch"], [role="checkbox"], input[type="checkbox"]'))
            .filter(visible);
        const direct = switches.find((sw) => isLabel(sw.getAttribute("aria-label")) || isLabel(labelledBy(sw)));
        if (direct) return direct;
        const labels = Array.from(root.querySelectorAll("*")).filter((el) => visible(el) && isLabel(ownText(el)));
        for (const label of labels) {
            for (let row = label.parentElement; row && root.contains(row); row = row.parentElement) {
                const inRow = switches.filter((sw) => row.contains(sw));
                if (inRow.length === 1) return inRow[0];
                if (inRow.length > 1) break;
            }
        }
        return null;
    };

    let target = null;
    for (let i = 0; i < 10 && !target; i++) {
        target = findSwitch();
        if (!target) await new Promise((resolve) => setTimeout(resolve, 200));
    }
    if (!target) return "not_found";
    const isOn = () => target.getAttribute("aria-checked") === "true" || target.checked === true;
    if (!isOn()) return "already_off";
    target.click();
    await new Promise((resolve) => setTimeout(resolve, 400));
    return isOn() ? "still_on" : "turned_off";
}"""

# ──────────────────────────────────────────────────────────────
#  Yardımcılar
# ──────────────────────────────────────────────────────────────


@functools.lru_cache(maxsize=1)
def load_inject_script() -> str:
    return INJECT_SCRIPT_PATH.read_text(encoding="utf-8")


def audio_mime_type(path: str | Path) -> str:
    return AUDIO_MIME_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")


def clamp_volume(value: Any) -> int:
    try:
        number = int(round(float(value)))
    except (TypeError, ValueError, OverflowError) as exc:
        raise BotError("Geçersiz ses seviyesi") from exc
    return max(0, min(100, number))


def _normalize_meet_text(text: str) -> str:
    return " ".join(_fold((text or "").replace("\u2019", "'")).split())


def classify_meet_text(text: str) -> Optional[str]:
    """Meet sayfa metninde katılamama / düşme nedeni arar; bulursa Türkçe açıklama döndürür."""
    normalized = _normalize_meet_text(text)
    for pattern, detail in MEET_FAILURES:
        if pattern.search(normalized):
            return detail
    return None


def is_waiting_room_text(text: str) -> bool:
    """Sayfa, botun (toplantı sahibince) bekleme odasına alındığını mı söylüyor?"""
    return bool(WAITING_ROOM_RE.search(_normalize_meet_text(text)))


def parse_toggle_state(is_muted: Optional[str], label: str) -> Optional[bool]:
    """Mikrofon/kamera düğmesinden 'kapalı mı?' bilgisini çıkarır; anlaşılamazsa None."""
    if is_muted in ("true", "false"):
        return is_muted == "true"
    if _TOGGLE_OFF_NOW_RE.search(label):
        return True
    if _TOGGLE_ON_NOW_RE.search(label):
        return False
    return None


def inject_error_code(exc: BaseException) -> Optional[str]:
    match = re.search(r"MEETBOT_([A-Z_]+)", str(exc))
    return match.group(1) if match else None


def _short(exc: BaseException) -> str:
    """Playwright hata mesajlarının ilk satırı (çağrı günlükleri hariç)."""
    text = str(exc).strip().splitlines()
    return (text[0] if text else exc.__class__.__name__)[:300]


def _finite(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) else 0.0


async def invoke(page: Page, method: str, *args: Any, timeout: float = CALL_TIMEOUT) -> Any:
    """window.__meetbot.<method>(*args) — argümanlar JSON olarak geçer, JS'e gömülmez."""
    return await asyncio.wait_for(page.evaluate(_INVOKE_JS, [method, list(args)]), timeout)


async def upload_audio(page: Page, data: bytes, mime: str, token: int) -> str:
    """Dosya baytlarını base64 parçalar hâlinde sayfaya taşır; blob URL'sini döndürür."""
    await invoke(page, "uploadBegin", token, mime)
    view = memoryview(data)
    for offset in range(0, len(view), UPLOAD_CHUNK_BYTES):
        chunk = base64.b64encode(view[offset:offset + UPLOAD_CHUNK_BYTES]).decode("ascii")
        await invoke(page, "uploadChunk", token, chunk)
    return await invoke(page, "uploadEnd", token)


def ensure_silence_wav(path: Path) -> Path:
    if not path.is_file():
        create_silence_wav(path)
        log.info("🔇 Sahte mikrofon için %s oluşturuldu", path.name)
    return path


# ──────────────────────────────────────────────────────────────
#  Chrome: bulma, başlatma, CDP, süreç temizliği
# ──────────────────────────────────────────────────────────────


def find_chrome(configured: str = "") -> Optional[str]:
    """Chrome/Edge yolunu döndürür (ayar > bilinen konumlar > PATH); bulunamazsa None."""
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_file():
            return str(candidate)
        return shutil.which(configured)

    system = platform.system()
    if system == "Windows":
        # ProgramW6432: 32 bit Python'da ProgramFiles "Program Files (x86)"u gösterir.
        keys = ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LocalAppData")
        roots = list(dict.fromkeys(root for root in (os.environ.get(key) for key in keys) if root))
        relatives = (r"Google\Chrome\Application\chrome.exe", r"Microsoft\Edge\Application\msedge.exe")
        candidates = [str(Path(root) / rel) for rel in relatives for root in roots]
    elif system == "Darwin":
        apps = (
            "Google Chrome.app/Contents/MacOS/Google Chrome",
            "Chromium.app/Contents/MacOS/Chromium",
            "Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        )
        bases = ("/Applications", str(Path.home() / "Applications"))
        candidates = [str(Path(base) / app) for app in apps for base in bases]
    else:
        names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser",
                 "microsoft-edge", "microsoft-edge-stable")
        candidates = [found for found in (shutil.which(name) for name in names) if found]
    return next((c for c in candidates if Path(c).is_file()), None)


def _running_as_root_on_linux() -> bool:
    return platform.system() == "Linux" and hasattr(os, "geteuid") and os.geteuid() == 0


def build_chrome_args(chrome_path: str, settings: Settings, silence_wav: Path,
                      virtual_display: bool = False) -> list[str]:
    args = [
        chrome_path,
        f"--remote-debugging-port={settings.cdp_port}",
        f"--user-data-dir={settings.profile_dir}",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={silence_wav}",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-notifications",
        "--start-maximized",
    ]
    if platform.system() == "Linux":
        # Sunucu/Docker: küçük /dev/shm'de sekme çöker; anahtarlık (keyring) istemi ekransız ortamı kilitler
        args += ["--disable-dev-shm-usage", "--password-store=basic"]
    if virtual_display:
        # Pencere yöneticisi olmayan sanal ekranda --start-maximized işe yaramaz
        width, height = VIRTUAL_SCREEN
        args += [f"--window-size={width},{height}", "--window-position=0,0"]
    if _running_as_root_on_linux():
        args.append("--no-sandbox")  # Chrome root olarak sandbox'la açılmıyor
    args.append("about:blank")
    return args


def cdp_version(port: int, timeout: float = 1.0) -> Optional[dict]:
    """127.0.0.1:<port>/json/version yanıtı (CDP açıksa), değilse None. Proxy ayarlarını yok sayar."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://127.0.0.1:{port}/json/version", timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) and "webSocketDebuggerUrl" in data else None


def port_in_use(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1.0)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def kill_process_tree(process: subprocess.Popen, timeout: float = 10.0) -> None:
    """SADECE verilen süreci ve alt süreçlerini sonlandırır — asla isimle (/IM) değil."""
    if process.poll() is not None:
        return
    if os.name == "nt":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True, timeout=timeout, check=False,
            )
            failure = (result.stderr or result.stdout).decode("oem", errors="replace").strip()
            succeeded = result.returncode == 0
        except (OSError, subprocess.SubprocessError) as exc:
            failure, succeeded = str(exc), False
        if not succeeded and process.poll() is None:
            # Ağaç sonlandırılamadı: en azından kendi tarayıcı sürecimizi kapat (alt süreçler onunla gider).
            log.warning("⚠️ taskkill başarısız (PID %d): %s — süreç doğrudan sonlandırılıyor", process.pid, failure)
            process.kill()
    else:
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)  # start_new_session=True → grup kimliği = PID
            except ProcessLookupError:
                return  # süreç grubu zaten kapanmış
            try:
                process.wait(timeout=5)
                return
            except subprocess.TimeoutExpired:
                continue
    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("⚠️ Chrome süreci (PID %d) kapanmadı", process.pid)


# ──────────────────────────────────────────────────────────────
#  Linux: ekransız sunucu için sanal ekran (Xvfb)
# ──────────────────────────────────────────────────────────────

def needs_virtual_display() -> bool:
    """Linux'ta grafik oturum yoksa (SSH / sunucu / systemd) Chrome'a sanal ekran gerekir."""
    return (platform.system() == "Linux"
            and not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY"))


class VirtualDisplay:
    """Botun Chrome'u için Xvfb sanal ekranı: gerektiğinde açılır, bot kapanınca kapanır."""

    def __init__(self) -> None:
        self._process: Optional[subprocess.Popen] = None
        self.display: Optional[str] = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    async def start(self) -> str:
        if self.running and self.display:
            return self.display
        xvfb = shutil.which("Xvfb")
        if xvfb is None:
            raise BotError("Ekransız sunucuda Chrome için Xvfb gerekli (sudo apt install xvfb)")
        width, height = VIRTUAL_SCREEN
        # -displayfd: Xvfb boş bir ekran numarası seçip onu stdout'a yazar (yarışsız)
        try:
            process = subprocess.Popen(
                [xvfb, "-displayfd", "1", "-screen", "0", f"{width}x{height}x24", "-nolisten", "tcp"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except OSError as exc:
            raise BotError("Xvfb başlatılamadı") from exc
        try:
            line = await asyncio.wait_for(asyncio.to_thread(process.stdout.readline), XVFB_START_TIMEOUT)
            number = int(line.strip())
        except (asyncio.TimeoutError, ValueError) as exc:
            await asyncio.to_thread(kill_process_tree, process)
            raise BotError("Xvfb başlatılamadı (sanal ekran açılmadı)") from exc
        self._process, self.display = process, f":{number}"
        log.info("🖥️ Sanal ekran (Xvfb) açıldı: %s (%dx%d)", self.display, width, height)
        return self.display

    async def stop(self) -> None:
        process, self._process, self.display = self._process, None, None
        if process is None:
            return
        await asyncio.to_thread(kill_process_tree, process)
        if process.stdout is not None:
            process.stdout.close()
        log.info("🖥️ Sanal ekran kapatıldı")


# ──────────────────────────────────────────────────────────────
#  Bot ekranı: panelden botun tarayıcısını görme / kullanma
# ──────────────────────────────────────────────────────────────

VIEW_TARGETS = ("meet", "login")
# Panelden gönderilebilecek özel tuşlar (metin "type" ile yazılır)
VIEW_KEYS = frozenset({
    "Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "Space",
    "ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Control+a",
})
VIEW_MAX_TEXT = 500
VIEW_MAX_SCROLL = 3000
# Oturum açık bir Google hesabının çerezleri
GOOGLE_SESSION_COOKIES = frozenset({"SID", "__Secure-1PSID", "__Secure-3PSID"})
GOOGLE_COOKIE_URLS = ["https://accounts.google.com/", "https://www.google.com/", "https://meet.google.com/"]
# "Hesabı çıkar": silinecek Google / YouTube çerezleri (.google.com, .google.com.tr, .youtube.com …)
GOOGLE_COOKIE_DOMAIN_RE = re.compile(r"(^|\.)(google(\.[a-z]{2,3}){1,2}|youtube\.com)$")


# ──────────────────────────────────────────────────────────────
#  MeetBot
# ──────────────────────────────────────────────────────────────

StatusCallback = Callable[[str, Optional[str]], Awaitable[None]]
TrackEndedCallback = Callable[[int], Awaitable[None]]
ProgressCallback = Callable[[int, float, float, bool], Awaitable[None]]


class MeetBot:
    """Google Meet'e katılan ve müzik çalan tarayıcı botu (SPEC §2).

    Test dikişi: `_ensure_context()` bir BrowserContext döndürür. Gerçekte
    Chrome'u başlatıp CDP ile bağlanır; testler onu ezip kendi (sahte Meet
    sayfalarına yönlendirilmiş) Playwright bağlamını verir. `_join_on_page()`
    ve `_monitor()` doğrudan bir Page üzerinde çalışır.
    """

    MONITOR_INTERVAL = 1.0     # sn — toplantı/ses yoklama aralığı
    OUT_OF_CALL_LIMIT = 5      # art arda bu kadar "toplantıda değil" → bağlantı koptu
    PREJOIN_TIMEOUT = 60.0     # sn — katılma ekranının gelmesi ve düğmenin etkinleşmesi
    POLL_INTERVAL = 0.3        # sn — katılım sırasında arayüz yoklama aralığı
    PROMPT_MEMORY = 300.0      # sn — "Hâlâ orada mısınız?" sorusundan sonraki kopma bu soruya bağlanır
    MAX_JOIN_ATTEMPTS = 3      # ilk deneme + en fazla 2 tekrar (yalnızca çökme / gezinme hatası)
    JOIN_RETRY_DELAY = 2.0     # sn

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._status = "disconnected"
        self._status_detail: Optional[str] = None
        self._meet_link: Optional[str] = None

        # main.py atar; hepsi async ve isteğe bağlı.
        self.on_status: Optional[StatusCallback] = None
        self.on_track_ended: Optional[TrackEndedCallback] = None
        self.on_progress: Optional[ProgressCallback] = None

        # Son ayarlar: her (yeniden) enjeksiyondan / katılımdan sonra tekrar uygulanır.
        self._music_volume = 80
        self._mic_volume = 80
        self._mic_muted = False

        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._chrome_process: Optional[subprocess.Popen] = None  # yalnızca BİZİM başlattığımız
        self._page: Optional[Page] = None
        self._page_created = False        # sekmeyi biz açtık (başkasının Chrome'unda kapatmak için)
        self._prepared_page: Optional[Page] = None
        self._crashed = False
        self._in_call = False

        self._session_lock = asyncio.Lock()   # katılım / ayrılma adımlarını sıraya koyar
        self._join_requests = 0               # request_join sayacı: leave() sonradan gelen katılımı bozmasın
        self._join_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._callback_tasks: set[asyncio.Task] = set()

        self._token: Optional[int] = None  # ilerlemesi / bitişi raporlanan parça
        self._audio_seq = 0                # her play/stop'ta artar; eski play sonucu token yazmaz

        self._browser_lock = asyncio.Lock()   # Chrome başlatma / bağlanma tek seferde (katılım + bot ekranı)
        self._display = VirtualDisplay()      # Linux'ta ekran yoksa Xvfb

        # Bot ekranı (panelden uzaktan görüntü + Google girişi)
        self._view_lock = asyncio.Lock()
        self._view_target = "meet"
        self._view_context: Optional[BrowserContext] = None
        self._login_page: Optional[Page] = None
        self._signed_in: Optional[bool] = None
        self._signed_in_checked = 0.0

    # ── Durum ────────────────────────────────────────────────

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_detail(self) -> Optional[str]:
        return self._status_detail

    @property
    def meet_link(self) -> Optional[str]:
        return self._meet_link

    @property
    def is_connected(self) -> bool:
        return self._status == "connected"

    # ── Katılma / ayrılma ────────────────────────────────────

    def request_join(self, link: str) -> None:
        """Katılımı arka planda başlatır; süren katılımı iptal eder (bloklamaz)."""
        link = link.strip()
        if link == self._meet_link and self._status in ("connecting", "connected"):
            log.info("ℹ️ Bot zaten bu toplantıda / bu toplantıya bağlanıyor: %s", link)
            return
        previous = self._join_task
        if previous is not None and not previous.done():
            log.info("↪️ Süren katılım iptal ediliyor, yeni bağlantıya geçiliyor")
            previous.cancel()
        self._status = "connecting"  # ses komutları artık BotNotReady alır
        self._status_detail = "Toplantıya bağlanılıyor…"
        self._meet_link = link
        self._token = None  # eski toplantıdaki parçanın ilerlemesi / bitişi artık raporlanmaz
        self._join_requests += 1
        self._join_task = asyncio.create_task(self._join_worker(link), name="meetbot-join")

    async def leave(self) -> None:
        """Katılımı / izlemeyi iptal eder, 'Görüşmeden ayrıl'a basar, sekmeyi boşaltır.

        leave() başladıktan SONRA gelen bir request_join() kazanır: o katılım eski
        toplantıdan kendisi ayrılır; leave() yeni toplantıya dokunmaz ve bildirim yapmaz.
        """
        requests = self._join_requests
        previous = self._status
        in_waiting_room = previous == "connecting" and self._status_detail == WAITING_ROOM_DETAIL
        join_task, self._join_task = self._join_task, None
        self._status = "disconnected"  # yeni ses komutları hemen BotNotReady alsın
        self._token = None
        if join_task is not None and not join_task.done():
            join_task.cancel()
            await asyncio.gather(join_task, return_exceptions=True)
        await self._stop_monitor()
        async with self._session_lock:
            if self._join_requests == requests:
                await self._teardown_call()
            superseded = self._join_requests != requests
            if not superseded:
                self._meet_link = None
        if superseded:
            log.info("↪️ Ayrılırken yeni bir katılım istendi; eski toplantıyı o kapatır")
        elif previous != "disconnected":
            if in_waiting_room:
                detail = "Bekleme odasından ayrıldı"
            else:
                detail = "Katılma iptal edildi" if previous == "connecting" else "Toplantıdan ayrıldı"
            log.info("👋 %s", detail)
            await self._emit_status("disconnected", detail)

    async def shutdown(self) -> None:
        """Toplantıdan çıkar, Playwright'ı kapatır, yalnızca kendi Chrome'umuzu sonlandırır."""
        log.info("🛑 Bot kapatılıyor…")
        # Her adım ayrı: biri patlasa da (ör. Playwright sürücüsü önce öldüyse) Chrome ve Xvfb yine kapanır
        for name, step in (("toplantıdan çıkış", self.leave), ("tarayıcı", self._close_browser),
                           ("sanal ekran", self._display.stop)):
            try:
                await step()
            except Exception:
                log.exception("❌ Kapanışta %s adımı başarısız", name)
                if step == self._close_browser:
                    await self._disconnect_browser()
                    await self._stop_chrome()
        if self._playwright is not None:
            playwright, self._playwright = self._playwright, None
            try:
                await playwright.stop()
            except Exception as exc:  # sürücü zaten kapanmış olabilir
                log.warning("⚠️ Playwright kapatılamadı: %s", _short(exc))
        log.info("✅ Bot kapatıldı")

    async def _join_worker(self, link: str) -> None:
        try:
            async with self._session_lock:
                await self._stop_monitor()
                if self._in_call:
                    log.info("👋 Önceki toplantıdan ayrılınıyor…")
                    await self._teardown_call()
                self._meet_link = link
                await self._emit_status("connecting", "Toplantıya bağlanılıyor…")
                try:
                    page = await self._join_with_retries(link)
                except BotError as exc:
                    await self._abandon_join(str(exc))
                    return
                except Exception as exc:  # beklenmeyen: tam iz günlüğe, kısa mesaj kullanıcıya
                    log.exception("❌ Toplantıya katılırken beklenmeyen hata")
                    await self._abandon_join(f"Beklenmeyen hata: {exc.__class__.__name__}")
                    return
                self._in_call = True
                self._status = "connected"
                self._monitor_task = asyncio.create_task(self._monitor(page), name="meetbot-monitor")
            log.info("🎉 Bot toplantıya katıldı: %s", link)
            await self._emit_status("connected", "Toplantıya katıldı")
        except asyncio.CancelledError:
            log.info("⏹️ Katılım iptal edildi: %s", link)
            raise
        finally:
            if self._join_task is asyncio.current_task():
                self._join_task = None

    async def _abandon_join(self, detail: str) -> None:
        log.warning("❌ Toplantıya katılınamadı: %s", detail)
        self._meet_link = None
        await self._teardown_call()
        await self._emit_status("disconnected", detail)

    async def _join_with_retries(self, link: str) -> Page:
        attempt = 1
        while True:
            try:
                page = await self._acquire_page()
                await self._join_on_page(page, link)
                return page
            except _RetryableJoinError as exc:
                failure, detail = exc, exc.detail
            except PlaywrightError as exc:
                # Sekme açılırken tarayıcı kapandı / bağlantı koptu: sonraki denemede yeniden bağlanılır.
                log.warning("⚠️ Meet sekmesi açılamadı: %s", _short(exc))
                failure, detail = exc, "Meet sekmesi açılamadı (tarayıcı kapanmış olabilir)"
            if attempt >= self.MAX_JOIN_ATTEMPTS:
                raise BotError(detail) from failure
            log.warning("⚠️ Katılım denemesi %d/%d başarısız (%s), tekrar denenecek",
                        attempt, self.MAX_JOIN_ATTEMPTS, detail)
            attempt += 1
            await self._emit_status("connecting", f"Tekrar deneniyor ({attempt}/{self.MAX_JOIN_ATTEMPTS})…")
            await asyncio.sleep(self.JOIN_RETRY_DELAY)

    async def _join_on_page(self, page: Page, link: str) -> None:
        """Verilen sekmede Meet katılım akışının tamamı."""
        try:
            await self._prepare_page(page)
            await self._emit_status("connecting", "Meet açılıyor…")
            await self._open_meet(page, link)
            join_button = await self._wait_for_prejoin(page)
            if join_button is not None:
                await self._click_join(page, join_button)
                await self._wait_for_admission(page)
            await self._after_join(page)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            self._ensure_page_alive(page)
            log.warning("⚠️ Meet sayfasında beklenmeyen hata: %s", _short(exc))
            raise _RetryableJoinError("Meet sayfasında beklenmeyen hata") from exc

    # ── Katılım adımları ─────────────────────────────────────

    async def _prepare_page(self, page: Page) -> None:
        """Mikrofon/kamera iznini verir; stealth + enjeksiyon script'lerini sekmeye BİR kez ekler."""
        self._page = page
        if self._prepared_page is page:
            return
        self._crashed = False
        # İzin "sorulacak" durumda kalırsa Meet katılma ekranını "Kullanıcıların toplantıda sizi
        # görmesini ve duymasını istiyor musunuz?" penceresiyle kapatır (sahte UI bayrağına rağmen).
        try:
            await page.context.grant_permissions(["microphone", "camera"], origin=MEET_ORIGIN)
        except PlaywrightError as exc:
            log.warning("⚠️ Mikrofon/kamera izni verilemedi: %s", _short(exc))
        await Stealth().apply_stealth_async(page)
        await page.add_init_script(script=load_inject_script())
        page.on("crash", self._on_page_crash)
        page.on("close", self._on_page_close)
        self._prepared_page = page

    async def _open_meet(self, page: Page, link: str) -> None:
        log.info("🌐 Meet açılıyor: %s", link)
        try:
            await page.goto(link, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        except PlaywrightError as exc:
            self._ensure_page_alive(page)
            log.warning("⚠️ Meet sayfası açılamadı: %s", _short(exc))
            raise _RetryableJoinError("Meet sayfası açılamadı (internet bağlantısını kontrol edin)") from exc
        await self._inject(page)  # init script zaten çalıştı; idempotent güvenlik adımı
        await self._apply_audio_settings(page)

    async def _wait_for_prejoin(self, page: Page) -> Optional[Locator]:
        """Katılma ekranını bekler; açılır pencereleri kapatır, adı yazar.

        Etkinleşmiş katılma düğmesini döndürür; bot zaten görüşmedeyse None.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.PREJOIN_TIMEOUT
        join_buttons = page.get_by_role("button", name=JOIN_BUTTON_RE).filter(visible=True)
        switch_buttons = page.get_by_role("button", name=SWITCH_HERE_RE).filter(visible=True)
        other_ways = page.get_by_role("button", name=OTHER_WAYS_RE, expanded=False).filter(visible=True)
        name_inputs = page.locator(NAME_INPUT_SELECTOR).filter(visible=True)
        ui_seen = switch_seen = button_seen = False
        while True:
            self._ensure_page_alive(page)
            try:
                probe = await self._probe(page)
                if probe["in_call"]:
                    return None
                failure = classify_meet_text(probe["text"])
                if failure:
                    raise JoinDenied(failure)
                has_button = await join_buttons.count() > 0
                has_switch = not has_button and await switch_buttons.count() > 0
                has_name = await name_inputs.count() > 0
                if (has_button or has_name or has_switch) and not ui_seen:
                    ui_seen = True
                    log.info("🚪 Katılma ekranı açıldı")
                    await self._dismiss_popups(page)
                if has_switch:
                    # "Buraya geç" o hesabın toplantıdaki oturumunu (ör. operatörü) bu sekmeye taşırdı.
                    if not switch_seen:
                        switch_seen = True
                        log.info("👥 Bot profilindeki hesap toplantıda zaten var; 'Buraya geç' yerine "
                                 "'Diğer katılma yöntemleri' → 'Burada da katıl' kullanılacak")
                    if await other_ways.count():
                        await other_ways.first.click(timeout=2000)
                        log.info("🔽 'Diğer katılma yöntemleri' açıldı")
                if has_name:
                    name_input = name_inputs.first
                    if not (await name_input.input_value(timeout=1000)).strip():
                        await name_input.fill(self._settings.bot_name, timeout=2000)
                        log.info("✍️ Görünen ad yazıldı: %s", self._settings.bot_name)
                if has_button:
                    button_seen = True
                    if await join_buttons.first.is_enabled(timeout=1000):
                        return join_buttons.first
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                # Gezinme / yeniden çizim sırasında geçici hatalar olur; süre sınırı bizi korur.
                self._ensure_page_alive(page)
                log.debug("Katılma ekranı yoklanırken geçici hata: %s", _short(exc))
            if loop.time() >= deadline:
                raise BotError(await self._prejoin_failure(page, switch_seen, button_seen))
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _prejoin_failure(self, page: Page, switch_seen: bool, button_seen: bool) -> str:
        """Katılma düğmesi süresinde bulunamadı: görünen düğmeleri günlüğe yazar, nedeni açıklar."""
        try:
            labels = await asyncio.wait_for(page.evaluate(_VISIBLE_BUTTONS_JS), PROBE_TIMEOUT)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.debug("Görünen düğmeler okunamadı: %s", _short(exc))
            labels = []
        log.warning("⚠️ %.0f sn içinde katılma düğmesi bulunamadı; sayfadaki düğmeler: %s",
                    self.PREJOIN_TIMEOUT, " | ".join(labels) or "(yok)")
        if switch_seen:
            return ("Bot profilindeki Google hesabı toplantıda zaten var ve 'Burada da katıl' bulunamadı "
                    "(bot 'Buraya geç'e basmaz; profilde başka bir hesap kullanın)")
        if button_seen:
            return "Meet katılma düğmesi etkinleşmedi"
        unknown = next((label for label in labels if JOIN_WORD_RE.search(label) and not NOT_JOIN_RE.search(label)),
                       None)
        if unknown:
            return f"Meet katılma düğmesi tanınmadı ('{unknown[:60]}')"
        return "Meet katılma ekranı açılmadı (katılma düğmesi bulunamadı)"

    async def _click_join(self, page: Page, join_button: Locator) -> None:
        await self._dismiss_popups(page)
        await self._try_toggle(page, "camera", muted=True, where="katılma ekranında")
        await self._try_toggle(page, "mic", muted=self._mic_muted, where="katılma ekranında")
        label = " ".join((await join_button.inner_text(timeout=2000)).split())
        await join_button.click(timeout=10_000)
        if ASK_TO_JOIN_RE.search(label):
            log.info("🙋 Katılma isteği gönderildi, toplantı sahibinin onayı bekleniyor…")
            await self._emit_status("connecting", "Katılma isteği gönderildi, onay bekleniyor…")
        else:
            log.info("🚪 '%s' düğmesine tıklandı", label)
            await self._emit_status("connecting", "Toplantıya giriliyor…")

    async def _wait_for_admission(self, page: Page) -> None:
        loop = asyncio.get_running_loop()
        timeout = self._settings.join_timeout
        deadline = loop.time() + timeout
        prompt_reported = False
        while True:
            self._ensure_page_alive(page)
            try:
                probe = await self._probe(page)
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                self._ensure_page_alive(page)
                log.debug("Kabul beklenirken geçici hata: %s", _short(exc))
            else:
                if probe["in_call"]:
                    log.info("✅ Toplantıya kabul edildi")
                    return
                # Uzun beklemede Meet "Hâlâ orada mısınız?" diye sorar: yanıtlanabiliyorsa beklemeye devam.
                prompt = probe.get("prompt")
                answered = bool(prompt) and await self._answer_prompt(page, prompt, report=not prompt_reported)
                prompt_reported = bool(prompt)
                failure = None if answered else classify_meet_text(probe["text"])
                if failure:
                    raise JoinDenied(failure)
            if loop.time() >= deadline:
                raise JoinDenied(f"Toplantıya {timeout} sn içinde kabul edilmedi")
            await asyncio.sleep(self.POLL_INTERVAL)

    async def _after_join(self, page: Page) -> None:
        await self._inject(page)
        await self._apply_audio_settings(page)
        await self._dismiss_popups(page)  # "Diğer seçenekler"in önünü kapatmasınlar
        await self._try_toggle(page, "camera", muted=True, where="toplantıda")
        await self._adjust_meet_settings(page)
        await self._dismiss_popups(page)
        await self._try_toggle(page, "mic", muted=self._mic_muted, where="toplantıda")
        self._ensure_page_alive(page)

    async def _dismiss_popups(self, page: Page) -> None:
        buttons = page.get_by_role("button", name=POPUP_BUTTON_RE).filter(visible=True)
        for _ in range(MAX_POPUPS):
            try:
                if await buttons.count() == 0:
                    return
                button = buttons.first
                label = " ".join((await button.inner_text(timeout=1000)).split())
                await button.click(timeout=2000)
                log.info("🧹 Açılır pencere kapatıldı: %s", label or "?")
                await asyncio.sleep(0.3)
            except PlaywrightError as exc:
                log.warning("⚠️ Açılır pencere kapatılamadı: %s", _short(exc))
                return

    async def _adjust_meet_settings(self, page: Page) -> None:
        """Diğer seçenekler → Ayarlar: Ses → Gürültü giderme KAPALI (müziği bozmasın);
        Genel → Boş görüşmelerden ayrıl KAPALI (bot tek kalınca Meet onu çıkarmasın)."""
        try:
            more = page.get_by_role("button", name=MORE_OPTIONS_RE).filter(visible=True).first
            try:
                await more.click(timeout=3000)
            except PlaywrightTimeout:
                log.info("ℹ️ 'Diğer seçenekler' bulunamadı, Meet ayarları (gürültü giderme…) atlandı")
                return
            item = page.get_by_role("menuitem", name=SETTINGS_ITEM_RE).filter(visible=True).first
            try:
                await item.click(timeout=3000)
            except PlaywrightTimeout:
                log.info("ℹ️ 'Ayarlar' menüsü bulunamadı, Meet ayarları (gürültü giderme…) atlandı")
                await page.keyboard.press("Escape")
                return
            dialog = page.get_by_role("dialog").filter(visible=True).last
            await dialog.wait_for(state="visible", timeout=5000)
            await self._switch_off_setting(page, dialog, AUDIO_TAB_RE, NOISE_CANCELLATION_LABELS,
                                           "Stüdyo ses / gürültü giderme", tab_required=False)
            await self._switch_off_setting(page, dialog, GENERAL_TAB_RE, LEAVE_EMPTY_CALL_LABELS,
                                           "Boş görüşmelerden ayrılma", tab_required=True)
            await self._close_dialog(page, dialog)
        except PlaywrightError as exc:
            log.warning("⚠️ Meet ayarları değiştirilemedi: %s", _short(exc))
            await self._press_escape(page)

    async def _switch_off_setting(self, page: Page, dialog: Locator, tab_re: re.Pattern, labels: tuple[str, ...],
                                  name: str, tab_required: bool) -> None:
        """Ayarlar penceresinde sekmeyi seçer, etiketiyle bulunan anahtarı kapatır ve sonucu günlüğe yazar."""
        tab = dialog.get_by_role("tab", name=tab_re)
        if await tab.count():
            await tab.first.click(timeout=2000)
        elif tab_required:
            log.info("ℹ️ %s ayarının sekmesi yok (anonim katılımda olmayabilir)", name)
            return
        result = await page.evaluate(_SWITCH_OFF_JS, list(labels))
        if result == "turned_off":
            log.info("🔇 %s kapatıldı", name)
        elif result == "already_off":
            log.info("ℹ️ %s zaten kapalı", name)
        elif result == "not_found":
            log.info("ℹ️ %s ayarı bulunamadı (anonim katılımda olmayabilir)", name)
        else:
            log.warning("⚠️ %s kapatılamadı", name)

    async def _close_dialog(self, page: Page, dialog: Locator) -> None:
        close_button = dialog.get_by_role("button", name=DIALOG_CLOSE_RE)
        if await close_button.count():
            await close_button.first.click(timeout=2000)
        else:
            await page.keyboard.press("Escape")
        try:
            await dialog.wait_for(state="hidden", timeout=3000)
        except PlaywrightTimeout:
            log.warning("⚠️ Ayarlar penceresi kapanmadı, Escape deneniyor")
            await page.keyboard.press("Escape")

    async def _press_escape(self, page: Page) -> None:
        try:
            await page.keyboard.press("Escape")
        except PlaywrightError as exc:
            log.warning("⚠️ Escape gönderilemedi: %s", _short(exc))

    async def _answer_prompt(self, page: Page, prompt: dict, report: bool) -> bool:
        """Meet'in "Hâlâ orada mısınız?" türü sorusunu yoklamanın işaretlediği 'kal' düğmesiyle yanıtlar.

        Tıklandıysa True. `report` False ise başarısızlık tekrar tekrar günlüğe yazılmaz.
        """
        question = " ".join(str(prompt.get("text") or "").split())[:120] or "?"
        label = prompt.get("stay")
        if not label:
            if report:
                log.warning("⚠️ Meet bir soru gösteriyor ama 'kal' düğmesi tanınmadı: %s", question)
            return False
        try:
            await page.locator(STAY_MARK_SELECTOR).first.click(timeout=2000)
        except PlaywrightError as exc:
            if report:
                log.warning("⚠️ Meet'in sorusu yanıtlanamadı ('%s'): %s", label, _short(exc))
            return False
        log.info("🙋 Meet'in sorusu yanıtlandı ('%s'): %s", label, question)
        return True

    # ── Mikrofon / kamera düğmeleri ──────────────────────────

    async def _read_toggle(self, page: Page, kind: str) -> Optional[tuple[Locator, bool]]:
        """Görünür mikrofon/kamera düğmesini ve 'kapalı mı?' durumunu bulur.

        Meet'in kendi düğmesi data-is-muted taşır; etiketi benzeyen başka düğmeler
        (ör. bir katılımcının "Mute … microphone" düğmesi) ancak ondan sonra denenir.
        """
        candidates = page.locator(TOGGLE_SELECTORS[kind]).filter(visible=True)
        attributes = await candidates.evaluate_all(_TOGGLE_ATTRS_JS)
        order = sorted(range(len(attributes)), key=lambda index: attributes[index][0] is None)
        for index in order:
            muted = parse_toggle_state(*attributes[index])
            if muted is not None:
                return candidates.nth(index), muted
        return None

    async def _set_toggle(self, page: Page, kind: str, muted: bool) -> Optional[bool]:
        """Düğme istenen durumda değilse tıklar; sonuçta okunan durumu döndürür (yoksa None).

        Düğme henüz çizilmemiş olabilir (araç çubuğu yeniden çiziliyor): kısa bir süre beklenir.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + TOGGLE_FIND_TIMEOUT
        found = await self._read_toggle(page, kind)
        while found is None and loop.time() < deadline:
            await asyncio.sleep(0.2)
            found = await self._read_toggle(page, kind)
        if found is None:
            return None
        button, current = found
        if current == muted:
            return current
        await button.click(timeout=5000)
        deadline = loop.time() + TOGGLE_CONFIRM_TIMEOUT
        while loop.time() < deadline:
            await asyncio.sleep(0.15)
            found = await self._read_toggle(page, kind)
            if found is not None and found[1] == muted:
                return muted
        return found[1] if found is not None else None

    async def _try_toggle(self, page: Page, kind: str, muted: bool, where: str) -> None:
        """Katılım sırasında düğme ayarı: başarısızlık günlüğe yazılır, katılımı durdurmaz."""
        name = "Kamera" if kind == "camera" else "Mikrofon"
        try:
            state = await self._set_toggle(page, kind, muted)
        except PlaywrightError as exc:
            self._ensure_page_alive(page)
            log.warning("⚠️ %s düğmesine %s tıklanamadı: %s", name, where, _short(exc))
            return
        if state is None:
            log.warning("⚠️ %s düğmesi %s bulunamadı", name, where)
        elif state != muted:
            log.warning("⚠️ %s %s istenen duruma getirilemedi", name, where)
        else:
            log.info("%s %s %s", "📷" if kind == "camera" else "🎤", name, "kapalı" if state else "açık")

    # ── Sayfa / tarayıcı yönetimi ────────────────────────────

    def _ensure_page_alive(self, page: Page) -> None:
        if self._crashed and page is self._page:
            raise _RetryableJoinError("Meet sekmesi çöktü")
        if page.is_closed():
            raise _RetryableJoinError("Meet sekmesi kapandı")

    def _on_page_crash(self, page: Page) -> None:
        if page is self._page:
            self._crashed = True
            log.error("💥 Meet sekmesi çöktü (Aw, Snap!)")

    def _on_page_close(self, page: Page) -> None:
        if page is self._page and self._status != "disconnected":
            log.warning("🗙 Meet sekmesi kapandı")

    async def _probe(self, page: Page) -> dict:
        return await asyncio.wait_for(page.evaluate(_PROBE_JS, _PROBE_ARGS), PROBE_TIMEOUT)

    async def _inject(self, page: Page) -> None:
        await page.evaluate(load_inject_script())

    async def _apply_audio_settings(self, page: Page) -> None:
        await page.evaluate(_APPLY_SETTINGS_JS, {
            "music": self._music_volume,
            "mic": self._mic_volume,
            "normalize": self._settings.normalize,
        })

    async def _acquire_page(self) -> Page:
        """Kullanılabilir bot sekmesini döndürür; çökmüş/kapanmış sekmenin yerine yenisini açar."""
        page = self._page
        if page is not None and not page.is_closed() and not self._crashed:
            return page
        context = await self._ensure_context()
        fresh: Optional[Page] = None
        if self._chrome_process is not None:  # kendi Chrome'umuzun boş ilk sekmesi
            fresh = next((p for p in context.pages
                          if p is not page and not p.is_closed() and p.url == "about:blank"), None)
        created = fresh is None
        if fresh is None:
            fresh = await context.new_page()
        # Eski (çökmüş) sekmeyi yenisi açıldıktan SONRA kapat: son pencere kapanınca Chrome da kapanır.
        await self._discard_page()
        self._page, self._page_created = fresh, created
        return fresh

    async def _discard_page(self) -> None:
        page, self._page = self._page, None
        self._prepared_page = None
        self._crashed = False
        if page is None or page.is_closed():
            return
        try:
            await asyncio.wait_for(page.close(), 5)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.warning("⚠️ Eski sekme kapatılamadı: %s", _short(exc))

    def _browser_ready(self) -> bool:
        return self._browser is not None and self._browser.is_connected() and self._context is not None

    async def _ensure_context(self, announce: bool = True) -> BrowserContext:
        """Chrome'u gerekirse başlatır, CDP ile bağlanır ve varsayılan bağlamı döndürür.

        announce=False: durum bildirimi yapılmaz (bot ekranı tarayıcıyı toplantı dışında açar).
        """
        if self._browser_ready():
            return self._context
        async with self._browser_lock:
            if self._browser_ready():  # beklerken başka bir görev bağlandı
                return self._context
            return await self._connect_browser(announce)

    async def _connect_browser(self, announce: bool) -> BrowserContext:
        await self._disconnect_browser()
        if announce:
            await self._emit_status("connecting", "Tarayıcı hazırlanıyor…")
        if self._chrome_process is not None and self._chrome_process.poll() is not None:
            log.info("ℹ️ Önceki Chrome kapanmış, yeniden başlatılacak")
            self._chrome_process = None

        port = self._settings.cdp_port
        info = await asyncio.to_thread(cdp_version, port)
        if info is None:
            if self._chrome_process is not None:  # bizim Chrome açık ama CDP yanıt vermiyor
                await self._stop_chrome()
            await self._launch_chrome()
        elif self._chrome_process is None:
            log.warning("⚠️ CDP portu %d'de zaten bir tarayıcı açık (%s; önceki bir çalıştırmadan kalmış olabilir), "
                        "ona bağlanılıyor. Bu tarayıcıyı MeetBot kapatmaz; sahte mikrofon / otomatik oynatma "
                        "bayrakları eksikse ses gitmeyebilir.", port, info.get("Browser", "?"))

        if self._playwright is None:
            self._playwright = await async_playwright().start()
        try:
            self._browser = await self._playwright.chromium.connect_over_cdp(
                f"http://127.0.0.1:{port}", timeout=20_000)
        except PlaywrightError as exc:
            log.error("❌ Chrome'a CDP ile bağlanılamadı: %s", _short(exc))
            raise BotError("Chrome'a bağlanılamadı (CDP)") from exc
        contexts = self._browser.contexts
        self._context = contexts[0] if contexts else await self._browser.new_context()
        log.info("🔗 Chrome'a bağlanıldı (CDP %d)", port)
        if self._chrome_process is None:
            stale = sum(1 for page in self._context.pages if page.url.startswith(MEET_ORIGIN))
            if stale:
                log.warning("⚠️ Bu tarayıcıda %d açık Meet sekmesi var (MeetBot onlara dokunmaz). Biri hâlâ "
                            "toplantıdaysa bot orada ikinci kez görünebilir; gerekirse o sekmeyi kapatın.", stale)
        return self._context

    async def _launch_chrome(self) -> None:
        port = self._settings.cdp_port
        if await asyncio.to_thread(port_in_use, port):
            raise BotError(f"CDP portu {port} başka bir uygulama tarafından kullanılıyor (MEETBOT_CDP_PORT)")
        chrome = find_chrome(self._settings.chrome_path)
        if chrome is None:
            if self._settings.chrome_path:
                log.error("❌ MEETBOT_CHROME_PATH geçersiz: %s", self._settings.chrome_path)
            raise BotError("Chrome veya Edge bulunamadı (MEETBOT_CHROME_PATH ayarını kontrol edin)")
        try:
            self._settings.profile_dir.mkdir(parents=True, exist_ok=True)
            silence = ensure_silence_wav(SILENCE_WAV)
        except OSError as exc:
            log.error("❌ Chrome profili / silence.wav hazırlanamadı: %s", exc)
            raise BotError("Chrome profili hazırlanamadı") from exc

        env: Optional[dict[str, str]] = None
        virtual = needs_virtual_display() or self._display.running
        if virtual:
            env = {**os.environ, "DISPLAY": await self._display.start()}
        args = build_chrome_args(chrome, self._settings, silence, virtual_display=virtual)
        log.info("🚀 Chrome başlatılıyor: %s", chrome)
        popen_kwargs: dict[str, Any] = {} if os.name == "nt" else {"start_new_session": True}
        try:
            process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, env=env, **popen_kwargs)
        except OSError as exc:
            log.error("❌ Chrome başlatılamadı: %s", exc)
            raise BotError("Chrome başlatılamadı") from exc
        self._chrome_process = process

        loop = asyncio.get_running_loop()
        deadline = loop.time() + CHROME_START_TIMEOUT
        while loop.time() < deadline:
            if await asyncio.to_thread(cdp_version, port) is not None:
                log.info("✅ Chrome hazır (PID %d, CDP %d)", process.pid, port)
                return
            if process.poll() is not None:
                self._chrome_process = None
                raise BotError("Chrome açılır açılmaz kapandı; bot profili başka bir Chrome penceresinde açık olabilir")
            await asyncio.sleep(0.25)
        await self._stop_chrome()
        raise BotError("Chrome başladı ama CDP bağlantısı açılmadı")

    async def _disconnect_browser(self) -> None:
        """Playwright'ın tarayıcı bağlantısını bırakır (CDP'de Chrome'u KAPATMAZ)."""
        browser = self._browser
        self._browser = None
        self._context = None
        self._page = None
        self._prepared_page = None
        self._crashed = False
        self._view_context = None
        self._login_page = None
        self._signed_in = None
        if browser is None:
            return
        try:
            await browser.close()
        except Exception as exc:  # PlaywrightError ya da sürücü kapanmış ("Connection closed …")
            log.debug("Tarayıcı bağlantısı zaten kopmuş: %s", _short(exc))

    async def _close_browser(self) -> None:
        """Bağlantıyı kapatır; kendi Chrome'umuzu düzgünce, gerekirse zorla kapatır."""
        browser, process = self._browser, self._chrome_process
        if browser is not None and browser.is_connected():
            if process is not None:
                try:
                    session = await browser.new_browser_cdp_session()
                    await session.send("Browser.close")
                except Exception as exc:  # tarayıcı yanıt vermeden kapandı / Playwright sürücüsü öldü
                    log.debug("Browser.close: %s", _short(exc))
            else:
                # Başkasının Chrome'unda yalnızca kendi açtığımız sekmeleri kapat
                own = [self._login_page]
                if self._page_created:
                    own.append(self._page)
                for page in own:
                    if page is None or page.is_closed():
                        continue
                    try:
                        await page.close()
                    except Exception as exc:  # PlaywrightError ya da sürücü kapanmış
                        log.warning("⚠️ Bot sekmesi kapatılamadı: %s", _short(exc))
        await self._disconnect_browser()
        if process is not None:
            try:
                await asyncio.to_thread(process.wait, 5)
            except subprocess.TimeoutExpired:
                log.info("ℹ️ Chrome kendiliğinden kapanmadı, süreç ağacı sonlandırılıyor")
            await self._stop_chrome()

    async def _stop_chrome(self) -> None:
        process, self._chrome_process = self._chrome_process, None
        if process is None:
            return
        try:
            await asyncio.to_thread(kill_process_tree, process)
        except (OSError, subprocess.SubprocessError) as exc:
            log.error("❌ Chrome süreci (PID %d) sonlandırılamadı: %s", process.pid, exc)

    async def _teardown_call(self) -> None:
        """Sesi durdurur, 'Görüşmeden ayrıl'a basar ve sekmeyi about:blank'e götürür."""
        self._in_call = False
        self._token = None
        page = self._page
        if page is None or page.is_closed() or self._crashed or page.url == "about:blank":
            return
        try:
            await asyncio.wait_for(page.evaluate(_STOP_JS), 5)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.warning("⚠️ Ses durdurulamadı: %s", _short(exc))
        try:
            leave_button = page.locator(LEAVE_BUTTON_SELECTOR).filter(visible=True)
            if await leave_button.count():
                await leave_button.first.click(timeout=3000)
                log.info("👋 'Görüşmeden ayrıl' düğmesine tıklandı")
        except PlaywrightError as exc:
            log.warning("⚠️ 'Görüşmeden ayrıl' tıklanamadı: %s", _short(exc))
        try:
            await page.goto("about:blank", wait_until="commit", timeout=10_000)
        except PlaywrightError as exc:
            log.warning("⚠️ Sekme temizlenemedi: %s", _short(exc))

    # ── İzleme ───────────────────────────────────────────────

    async def _stop_monitor(self) -> None:
        task, self._monitor_task = self._monitor_task, None
        if task is None or task.done() or task is asyncio.current_task():
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def _monitor(self, page: Page) -> None:
        """Her MONITOR_INTERVAL'de tek yoklama: ilerleme, şarkı bitti, toplantıda mıyız.

        Meet'in "Hâlâ orada mısınız?" sorusunu yanıtlar. Toplantı sahibi botu bekleme odasına
        gönderirse sekme kapatılmaz: durum "connecting" olur ve en çok join_timeout sn boyunca
        geri alınması beklenir (kabul → "connected"; ret / çıkarma / süre → "disconnected").
        """
        loop = asyncio.get_running_loop()
        generation = self._join_requests      # sonraki bir request_join bu izlemeyi geçersiz kılar
        misses = 0
        failures = 0
        prompt_at: Optional[float] = None      # son "Hâlâ orada mısınız?" sorusunun görüldüğü an
        prompt_reported = False
        waiting_since: Optional[float] = None  # bekleme odasına alındığı an

        def lost_detail(failure: Optional[str]) -> str:
            # Soru yanıtlanamadıysa Meet botu birkaç dakika içinde "ayrılmış" sayar: asıl neden odur.
            recent_prompt = prompt_at is not None and loop.time() - prompt_at <= self.PROMPT_MEMORY
            if recent_prompt and failure in (None, LEFT_FROM_WINDOW_DETAIL):
                return STILL_THERE_DETAIL
            return failure or CALL_LOST_DETAIL

        while True:
            await asyncio.sleep(self.MONITOR_INTERVAL)
            if self._crashed or page.is_closed():
                await self._call_lost("Meet sekmesi çöktü" if self._crashed else "Meet sekmesi veya tarayıcı kapatıldı",
                                      generation)
                return
            try:
                probe = await self._probe(page)
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                failures += 1
                log.debug("İzleme yoklaması başarısız (%d): %s", failures, _short(exc))
                if failures >= self.OUT_OF_CALL_LIMIT:
                    await self._call_lost("Meet sekmesi yanıt vermiyor", generation)
                    return
                continue
            failures = 0
            try:
                if probe["audio"] is None:
                    log.warning("⚠️ Ses motoru sayfada yok, yeniden enjekte ediliyor")
                    await self._inject(page)
                    await self._apply_audio_settings(page)
                else:
                    await self._handle_audio(probe["audio"])
            except PlaywrightError as exc:
                log.warning("⚠️ Ses motoru yeniden yüklenemedi: %s", _short(exc))

            prompt = probe.get("prompt")
            answered = False
            if prompt:
                prompt_at = loop.time()
                answered = await self._answer_prompt(page, prompt, report=not prompt_reported)
            prompt_reported = bool(prompt)

            if probe["in_call"]:
                misses = 0
                if waiting_since is not None:
                    waiting_since = None
                    await self._readmitted(page, generation)
                continue
            misses += 1
            text = probe.get("text") or ""
            failure = None if answered else classify_meet_text(text)

            if waiting_since is not None:
                # Bekleme odasında: sahibin geri alması, bir son ekranı ya da süre dolması beklenir.
                if is_waiting_room_text(text):
                    misses = 0
                timeout = self._settings.join_timeout
                if failure:
                    await self._call_lost(lost_detail(failure), generation)
                    return
                if loop.time() - waiting_since >= timeout:
                    await self._call_lost(f"Bekleme odasından {timeout} sn içinde geri alınmadı", generation)
                    return
                if misses >= self.OUT_OF_CALL_LIMIT:
                    await self._call_lost(lost_detail(None), generation)
                    return
                continue

            if misses < self.OUT_OF_CALL_LIMIT:
                continue
            if failure is None and is_waiting_room_text(text):
                if not await self._enter_waiting_room(page, generation):
                    return
                waiting_since, misses = loop.time(), 0
                continue
            await self._call_lost(lost_detail(failure), generation)
            return

    async def _enter_waiting_room(self, page: Page, generation: int) -> bool:
        """Toplantı sahibi botu bekleme odasına gönderdi: sekme kapatılmaz, geri alınması beklenir.

        Sayfadaki ses durdurulur; Player "connecting" ile konumu saklar ve kabulde kaldığı yerden çalar.
        leave() / yeni bir katılım durumu çoktan devraldıysa False.
        """
        if self._status != "connected" or self._join_requests != generation:
            return False
        log.warning("⏸️ Toplantı sahibi botu bekleme odasına aldı; geri alınması bekleniyor (en çok %d sn)",
                    self._settings.join_timeout)
        self._token = None
        await self._emit_status("connecting", WAITING_ROOM_DETAIL)
        try:
            await asyncio.wait_for(page.evaluate(_STOP_JS), 5)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.debug("Bekleme odasında ses durdurulamadı: %s", _short(exc))
        return True

    async def _readmitted(self, page: Page, generation: int) -> None:
        """Bekleme odasındaki bot toplantıya geri alındı: ses motoru ve kamera yeniden hazırlanır."""
        if self._status != "connecting" or self._join_requests != generation:
            return  # leave() / yeni katılım durumu devraldı
        log.info("✅ Bot bekleme odasından toplantıya geri alındı")
        try:
            await self._inject(page)
            await self._apply_audio_settings(page)
            await self._try_toggle(page, "camera", muted=True, where="toplantıda")
        except (PlaywrightError, _RetryableJoinError) as exc:
            log.warning("⚠️ Toplantıya dönüşte ses motoru / kamera hazırlanamadı: %s", _short(exc))
        if self._status == "connecting" and self._join_requests == generation:
            await self._emit_status("connected", READMITTED_DETAIL)

    async def _handle_audio(self, audio: dict) -> None:
        token = self._token
        if token is None or audio.get("token") != token:
            return  # durdurulmuş / eskimiş parça: raporlanmaz
        if audio.get("ended"):
            self._token = None  # her token için tam bir kez
            if audio.get("error"):
                log.warning("⚠️ Şarkı çalınırken hata oluştu, bitti sayılıyor: %s", audio["error"])
            await self._fire("on_track_ended", self.on_track_ended, token)
            return
        if audio.get("loading"):
            return
        await self._fire("on_progress", self.on_progress, token,
                         _finite(audio.get("position")), _finite(audio.get("duration")), bool(audio.get("paused")))

    async def _call_lost(self, detail: str, generation: Optional[int] = None) -> None:
        """İzleme toplantının koptuğunu gördü: önce Player'a bildirir, sonra sekmeyi temizler.

        Bildirim temizlikten ÖNCE yapılır: temizlik sırasında gelen leave() izlemeyi iptal
        etse bile "disconnected" kaybolmaz ve leave() ikinci bir bildirim göndermez.
        `generation` verilirse izlemenin kendi koyduğu "connecting" (bekleme odası) da geçerlidir.
        """
        waiting_room = (generation is not None and generation == self._join_requests
                        and self._status == "connecting")
        if self._status != "connected" and not waiting_room:
            # leave() ya da yeni bir request_join() durumu çoktan değiştirdi; bildirim ve temizlik onlarda.
            log.debug("Toplantı koptu (%s) ama durum zaten '%s'", detail, self._status)
            return
        log.warning("📴 Toplantı bağlantısı koptu: %s", detail)
        self._meet_link = None
        self._token = None
        await self._emit_status("disconnected", detail)
        await self._teardown_call()

    # ── Geri çağrılar ────────────────────────────────────────

    async def _emit_status(self, status: str, detail: Optional[str]) -> None:
        self._status = status
        self._status_detail = detail
        await self._fire("on_status", self.on_status, status, detail)

    async def _fire(self, name: str, callback: Optional[Callable[..., Awaitable[None]]], *args: Any) -> None:
        """Geri çağrıyı ayrı bir görevde çalıştırır ve bekler.

        shield: katılım/izleme görevi iptal edilse bile Player'ın işleyicisi yarıda kesilmez.
        Hatalar günlüğe yazılır; izleme döngüsünü asla öldürmez.
        """
        if callback is None:
            return
        task = asyncio.ensure_future(self._run_callback(name, callback, args))
        self._callback_tasks.add(task)
        task.add_done_callback(self._callback_tasks.discard)
        await asyncio.shield(task)

    @staticmethod
    async def _run_callback(name: str, callback: Callable[..., Awaitable[None]], args: tuple) -> None:
        try:
            await callback(*args)
        except Exception:
            log.exception("❌ %s geri çağrısı hata verdi", name)

    # ── Ses komutları ────────────────────────────────────────

    def _require_page(self) -> Page:
        page = self._page
        if self._status != "connected" or page is None or page.is_closed() or self._crashed:
            raise BotNotReady("Bot toplantıda değil")
        return page

    def _command_error(self, action: str, exc: BaseException, page: Page) -> BotError:
        """Sayfa hatasını kullanıcıya gösterilecek BotError'a çevirir (ve günlüğe yazar)."""
        if self._status != "connected" or page is not self._page or page.is_closed() or self._crashed:
            log.warning("⚠️ '%s' komutu bot toplantıdan çıkarken başarısız oldu: %s", action, _short(exc))
            return BotNotReady("Bot toplantıda değil")
        if isinstance(exc, asyncio.TimeoutError):
            log.error("❌ '%s' komutu zaman aşımına uğradı", action)
            return BotError("Tarayıcı zamanında yanıt vermedi")
        code = inject_error_code(exc)
        message = INJECT_ERROR_MESSAGES.get(code or "")
        if code == "SUPERSEDED":
            log.info("⏭️ '%s' iptal edildi: yerine yeni bir istek geldi", action)
            return PlaybackCancelled(message)
        if message is None:
            log.error("❌ '%s' komutu başarısız: %s", action, exc)
            return BotError("Tarayıcı komutu başarısız oldu")
        log.warning("⚠️ '%s' komutu başarısız: %s", action, _short(exc))
        return BotError(message)

    async def _command(self, method: str, *args: Any, timeout: float = CALL_TIMEOUT) -> Any:
        page = self._require_page()
        try:
            return await invoke(page, method, *args, timeout=timeout)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            raise self._command_error(method, exc, page) from exc

    async def play(self, file_path: str, token: int, start_at: float = 0.0) -> float:
        """Dosyayı sayfaya yükler ve çalmaya başlar; süreyi (sn) döndürür (bilinmiyorsa 0)."""
        page = self._require_page()
        path = Path(file_path)
        self._audio_seq += 1
        seq = self._audio_seq
        self._token = None  # yükleme sürerken eski parçanın ilerlemesi raporlanmasın
        start = max(0.0, _finite(start_at))
        try:
            # Eski parça hemen susar: yenisi yüklenemese bile sayfada sahipsiz ses kalmaz.
            await invoke(page, "stop")
            data = await asyncio.to_thread(path.read_bytes)
            if not data:
                log.error("❌ Ses dosyası boş: %s", path.name)
                raise BotError("Ses dosyası boş")
            blob_url = await upload_audio(page, data, audio_mime_type(path), token)
            result = await invoke(page, "play", token, blob_url, start, timeout=PLAY_TIMEOUT)
        except OSError as exc:
            log.error("❌ Ses dosyası okunamadı (%s): %s", path.name, exc)
            raise BotError("Ses dosyası okunamadı") from exc
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            raise self._command_error("play", exc, page) from exc
        if self._status != "connected" or page is not self._page:
            # Çalma başlarken bot toplantıdan çıktı / sekme değişti: ses sayfayla birlikte gitti.
            log.warning("⚠️ 'play' tamamlandı ama bot artık toplantıda değil (token %d)", token)
            raise BotNotReady("Bot toplantıda değil")
        duration = _finite(result.get("duration"))
        if seq == self._audio_seq:
            self._token = token
        log.info("▶️ Çalınıyor: %s (token %d, %.0f sn%s)", path.name, token, duration,
                 f", {start:.0f}. sn'den" if start else "")
        return duration

    async def pause(self) -> None:
        await self._command("pause")

    async def resume(self) -> None:
        await self._command("resume")

    async def stop(self) -> None:
        """Çalmayı durdurur, ses elementini ve blob'u serbest bırakır."""
        self._audio_seq += 1
        self._token = None
        if not self.is_connected:
            return  # toplantıda değilken sayfada çalan bir şey yok
        await self._command("stop")

    async def seek(self, position: float) -> None:
        await self._command("seek", max(0.0, _finite(position)))

    async def set_music_volume(self, value: int) -> None:
        self._music_volume = clamp_volume(value)
        if self.is_connected:
            await self._command("setMusicVolume", self._music_volume)

    async def set_mic_volume(self, value: int) -> None:
        self._mic_volume = clamp_volume(value)
        if self.is_connected:
            await self._command("setMicVolume", self._mic_volume)

    async def set_mic_muted(self, muted: bool) -> bool:
        """Meet'in mikrofon düğmesini istenen duruma getirir; Meet'ten okunan durumu döndürür."""
        self._mic_muted = bool(muted)
        if not self.is_connected:
            return self._mic_muted  # katılınca uygulanacak
        page = self._require_page()
        try:
            state = await self._set_toggle(page, "mic", self._mic_muted)
        except PlaywrightError as exc:
            raise self._command_error("mikrofon", exc, page) from exc
        if state is None:
            log.warning("⚠️ Meet'te mikrofon düğmesi bulunamadı")
            raise BotError("Meet'te mikrofon düğmesi bulunamadı")
        if state != self._mic_muted:
            log.warning("⚠️ Mikrofon istenen duruma getirilemedi (şu an %s)", "kapalı" if state else "açık")
        else:
            log.info("🎤 Mikrofon %s", "kapatıldı" if state else "açıldı")
        return state

    # ── Bot ekranı: panelden görüntü + fare/klavye (Google girişi için) ─
    #
    # Ekransız bir sunucuda botun Chrome profilinde Google hesabıyla oturum açmanın yolu:
    # yönetici paneldeki "Bot ekranı" penceresinden ayrı, temiz bir giriş sekmesi açılır
    # (stealth / ses enjeksiyonu YOK). Görüntü ekran görüntüleriyle gelir; tıklama ve
    # klavye olayları CDP ile sekmeye iletilir. Yazılan metin asla günlüğe yazılmaz.
    #
    # Güvenlik: girdi YALNIZCA oturum açılmamışken giriş sekmesine gider. Oturum açıldığı an
    # (her karede ve her girdiden önce çerezler taze okunur) giriş sekmesi kapanır; hesap
    # bağlıyken giriş sekmesi açılamaz. Meet sekmesi yalnızca izlenir. Böylece panelden
    # botun Google hesabının ayarlarına (profil, güvenlik…) erişilemez.

    GOOGLE_LOGIN_URL = "https://accounts.google.com/"

    def _view_page(self) -> Optional[Page]:
        """Bot ekranında gösterilen sekme: giriş sekmesi ya da botun Meet sekmesi."""
        login = self._login_page
        if self._view_target == "login" and login is not None and not login.is_closed():
            return login
        page = self._page
        if page is not None and not page.is_closed():
            return page
        context = self._view_context
        if context is None:
            return None
        return next((p for p in context.pages if not p.is_closed() and p is not login), None)

    def _require_view_page(self) -> Page:
        page = self._view_page()
        if page is None:
            raise BotError("Botun tarayıcısı açık değil (bot ekranını yeniden açın)")
        return page

    @staticmethod
    async def _bring_to_front(page: Page) -> None:
        try:
            await asyncio.wait_for(page.bring_to_front(), VIEW_TIMEOUT)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.debug("Sekme öne getirilemedi: %s", _short(exc))

    async def view_open(self, target: str) -> None:
        """Bot ekranını açar: gerekirse Chrome'u (durum bildirmeden) başlatır, hedef sekmeyi öne getirir.

        target="login": ayrı bir Google giriş sekmesi (yoksa açılır, varsa aynısı kullanılır).
        target="meet": botun Meet sekmesi; açık giriş sekmesi kapatılır.
        """
        if target not in VIEW_TARGETS:
            raise BotError("Geçersiz bot ekranı hedefi")
        async with self._view_lock:
            context = await self._ensure_context(announce=False)
            self._view_context = context
            if target == "login" and await self.google_signed_in(fresh=True):
                await self._close_login_page()
                raise BotError("Google hesabı zaten bağlı (başka bir hesap için önce hesabı çıkarın)")
            if target == "login":
                page = self._login_page
                if page is None or page.is_closed():
                    page = await context.new_page()
                    self._login_page = page
                    log.info("🔑 Google giriş sekmesi açıldı (bot ekranı)")
                    try:
                        await page.goto(self.GOOGLE_LOGIN_URL, wait_until="domcontentloaded",
                                        timeout=NAVIGATION_TIMEOUT_MS)
                    except PlaywrightError as exc:
                        log.warning("⚠️ Google giriş sayfası açılamadı: %s", _short(exc))
                self._view_target = "login"
            else:
                self._view_target = "meet"
                await self._close_login_page()
                page = self._view_page()
                if page is None:
                    page = await context.new_page()  # boş sekme; katılımda bot bunu kullanır
            self._signed_in_checked = 0.0
            await self._bring_to_front(page)

    async def _close_login_page(self) -> None:
        page, self._login_page = self._login_page, None
        if page is None or page.is_closed():
            return
        others = [p for p in page.context.pages if p is not page and not p.is_closed()]
        try:
            if others:
                await page.close()
            else:  # son sekme kapanırsa Chrome da kapanır: boşaltıp bot sekmesi olarak bırak
                await page.goto("about:blank", wait_until="commit", timeout=10_000)
        except PlaywrightError as exc:
            log.warning("⚠️ Google giriş sekmesi kapatılamadı: %s", _short(exc))
        log.info("🔑 Google giriş sekmesi kapatıldı")

    async def view_release(self) -> None:
        """Paneldeki izleyici kalmadı: toplantıdaysa Meet sekmesi yeniden öne gelsin."""
        async with self._view_lock:
            page = self._page
            if self._in_call and page is not None and not page.is_closed():
                await self._bring_to_front(page)

    async def google_signed_in(self, fresh: bool = False) -> Optional[bool]:
        """Botun profilinde Google oturumu açık mı? (bilinmiyorsa None; fresh=True → önbelleği atla)"""
        context = self._view_context or self._context
        if context is None:
            return None
        loop = asyncio.get_running_loop()
        recent = loop.time() - self._signed_in_checked < SIGNED_IN_CHECK_INTERVAL
        if not fresh and self._signed_in is not None and recent:
            return self._signed_in
        try:
            cookies = await asyncio.wait_for(context.cookies(GOOGLE_COOKIE_URLS), VIEW_TIMEOUT)
        except (PlaywrightError, asyncio.TimeoutError) as exc:
            log.debug("Google çerezleri okunamadı: %s", _short(exc))
            return self._signed_in
        signed_in = any(c.get("name") in GOOGLE_SESSION_COOKIES for c in cookies)
        if signed_in != self._signed_in and self._signed_in is not None:
            log.info("🔑 Google oturumu %s", "açıldı" if signed_in else "kapandı")
        self._signed_in, self._signed_in_checked = signed_in, loop.time()
        return signed_in

    async def _screenshot(self, page: Page) -> bytes:
        return await page.screenshot(type="jpeg", quality=VIEW_JPEG_QUALITY,
                                     timeout=VIEW_TIMEOUT * 1000, animations="allow")

    def _login_page_open(self) -> bool:
        page = self._login_page
        return page is not None and not page.is_closed()

    async def _close_login_if_signed_in(self) -> bool:
        """Giriş sekmesi açıkken Google oturumu açıldıysa sekmeyi hemen kapatır (hesap ayarlarına erişilemesin)."""
        if not self._login_page_open() or not await self.google_signed_in(fresh=True):
            return False
        await self._close_login_page()
        self._view_target = "meet"
        log.info("🔑 Google hesabı bağlandı; giriş sekmesi güvenlik için kapatıldı")
        return True

    async def view_frame(self) -> dict:
        """Gösterilen sekmenin JPEG ekran görüntüsü + adres, başlık ve Google oturum durumu."""
        async with self._view_lock:
            await self._close_login_if_signed_in()
            page = self._require_view_page()
            try:
                try:
                    image = await self._screenshot(page)
                except PlaywrightTimeout:
                    # Arka planda kalan sekme çizilmez: öne getirip bir kez daha dene
                    await self._bring_to_front(page)
                    image = await self._screenshot(page)
                width, height = await asyncio.wait_for(
                    page.evaluate("() => [window.innerWidth, window.innerHeight]"), VIEW_TIMEOUT)
                title = await asyncio.wait_for(page.title(), VIEW_TIMEOUT)
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                log.warning("⚠️ Bot ekranı alınamadı: %s", _short(exc))
                raise BotError("Bot ekranı alınamadı") from exc
            url = page.url
            target = "login" if page is self._login_page else "meet"
        return {
            "target": target,
            "interactive": target == "login",   # girdi yalnızca giriş sekmesinde
            "image": "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii"),
            "width": int(width),
            "height": int(height),
            "url": url,
            "title": title,
            "signed_in": await self.google_signed_in(),
        }

    async def _view_action(self, name: str, action: Callable[[Page], Awaitable[Any]]) -> None:
        async with self._view_lock:
            if self._view_target != "login" or not self._login_page_open():
                raise BotError("Meet sekmesi yalnızca izlenebilir; tıklama ve yazma yalnızca Google girişi sekmesinde")
            if await self._close_login_if_signed_in():
                raise BotError("Google hesabı bağlandı; giriş sekmesi güvenlik için kapatıldı")
            page = self._login_page
            try:
                await asyncio.wait_for(action(page), VIEW_TIMEOUT)
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                log.warning("⚠️ Bot ekranı: '%s' uygulanamadı: %s", name, _short(exc))
                raise BotError("Tarayıcı bu işlemi yapamadı") from exc

    async def view_click(self, x: float, y: float) -> None:
        """(x, y): görüntüye göre 0–1 arası oranlar."""
        if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
            raise BotError("Geçersiz tıklama konumu")

        async def click(page: Page) -> None:
            width, height = await page.evaluate("() => [window.innerWidth, window.innerHeight]")
            await page.mouse.click(x * width, y * height)
        await self._view_action("tıklama", click)

    async def view_type(self, text: str) -> None:
        if not text or len(text) > VIEW_MAX_TEXT:
            raise BotError(f"Metin 1–{VIEW_MAX_TEXT} karakter olmalı")
        await self._view_action("yazma", lambda page: page.keyboard.type(text))

    async def view_key(self, key: str) -> None:
        if key not in VIEW_KEYS:
            raise BotError("Bu tuş desteklenmiyor")
        await self._view_action(key, lambda page: page.keyboard.press(key))

    async def view_scroll(self, dy: float) -> None:
        amount = max(-VIEW_MAX_SCROLL, min(VIEW_MAX_SCROLL, _finite(dy)))
        await self._view_action("kaydırma", lambda page: page.mouse.wheel(0, amount))

    async def view_back(self) -> None:
        await self._view_action("geri", lambda page: page.go_back(wait_until="commit"))

    async def view_reload(self) -> None:
        await self._view_action("yenile", lambda page: page.reload(wait_until="commit"))

    async def google_sign_out(self) -> None:
        """Botun Google oturumunu kapatır (Google / YouTube çerezlerini siler); başka hesapla girmek için."""
        if self._status != "disconnected":
            raise BotError("Önce botu toplantıdan çıkarın")
        async with self._view_lock:
            context = await self._ensure_context(announce=False)
            self._view_context = context
            await self._close_login_page()
            self._view_target = "meet"
            try:
                await asyncio.wait_for(context.clear_cookies(domain=GOOGLE_COOKIE_DOMAIN_RE), VIEW_TIMEOUT)
            except (PlaywrightError, asyncio.TimeoutError) as exc:
                log.error("❌ Google çerezleri silinemedi: %s", _short(exc))
                raise BotError("Google hesabı çıkarılamadı") from exc
            self._signed_in, self._signed_in_checked = False, asyncio.get_running_loop().time()
        log.info("🔑 Google hesabı bottan çıkarıldı (çerezler silindi)")
