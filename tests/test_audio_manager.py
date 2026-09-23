"""audio_manager — sorgu sınıflandırma, yt-dlp argümanları, JSON ayrıştırma, indirme/önbellek/zaman aşımı.

Ağ gerektiren testler @pytest.mark.network ile işaretlidir (-m "not network" ile atlanır).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

import audio_manager
from audio_manager import (
    AUDIO_FORMAT,
    DownloadError,
    Downloader,
    ResolveError,
    Target,
    classify_query,
    entry_to_info,
    friendly_error,
    host_allowed,
    parse_ytdlp_json,
)

HOSTS = ("youtube.com", "youtu.be", "music.youtube.com")
VID = "jNQXAC9IVRw"
WATCH = f"https://www.youtube.com/watch?v={VID}"


# ──────────────────────────────────────────────────────────────
#  Sorgu sınıflandırma (SSRF / seçenek enjeksiyonu savunması)
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("query", [
    WATCH,
    f"https://youtube.com/watch?v={VID}&t=5s",
    f"https://m.youtube.com/watch?v={VID}",
    f"https://music.youtube.com/watch?v={VID}&list=RDAMVM{VID}",
    f"https://youtu.be/{VID}?si=AbCdEf123",
    f"youtu.be/{VID}",
    f"www.youtube.com/watch?v={VID}",
    f"https://www.youtube.com/shorts/{VID}",
    f"https://www.youtube.com/live/{VID}?feature=share",
    f"https://www.youtube.com/embed/{VID}",
    f"https://www.youtube.com/watch?v={VID}&list=RD{VID}&start_radio=1",
    f"HTTPS://WWW.YOUTUBE.COM/watch?v={VID}",
])
def test_video_urls_become_canonical_single_video(query):
    assert classify_query(query, HOSTS) == Target("video", WATCH)


@pytest.mark.parametrize("query,list_id", [
    (f"https://www.youtube.com/watch?v={VID}&list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI&index=3",
     "PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"),
    (f"https://music.youtube.com/watch?v={VID}&list=OLAK5uy_abc-123", "OLAK5uy_abc-123"),
    (f"https://youtu.be/{VID}?list=PLabc123&si=x", "PLabc123"),
])
def test_video_link_copied_from_a_playlist_adds_the_whole_playlist(query, list_id):
    # Listenin içinden kopyalanan link: kullanıcı listeyi istiyor (otomatik Mix/Radyo listeleri hariç)
    assert classify_query(query, HOSTS) == Target("playlist", f"https://www.youtube.com/playlist?list={list_id}")


@pytest.mark.parametrize("query,expected", [
    ("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI",
     "https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI"),
    ("https://music.youtube.com/playlist?list=OLAK5uy_abc-123&si=x",
     "https://www.youtube.com/playlist?list=OLAK5uy_abc-123"),
    ("https://www.youtube.com/watch?list=PLabc123", "https://www.youtube.com/playlist?list=PLabc123"),
])
def test_playlist_urls(query, expected):
    assert classify_query(query, HOSTS) == Target("playlist", expected)


def test_other_youtube_pages_are_resolved_flat_on_youtube_only():
    target = classify_query("https://www.youtube.com/@LofiGirl/live", HOSTS)
    assert target == Target("playlist", "https://www.youtube.com/@LofiGirl/live")


@pytest.mark.parametrize("query,expected", [
    # Kullanıcının yazdığı ana makine adı ASLA yt-dlp'ye gitmez: sabit bir YouTube adresi kullanılır
    ("https://m.youtube.com/@LofiGirl/videos", "https://www.youtube.com/@LofiGirl/videos"),
    ("https://YouTube.com./c/Kanal", "https://www.youtube.com/c/Kanal"),
    ("https://user:pw@www.youtube.com:8443/feed/trending#x", "https://www.youtube.com/feed/trending"),
    ("https://music.youtube.com/channel/UC123", "https://music.youtube.com/channel/UC123"),
    ("https://www.youtube.com/results?search_query=a%20b&sp=EgIQAQ%3D%3D",
     "https://www.youtube.com/results?search_query=a+b&sp=EgIQAQ%3D%3D"),
    ("https://www.youtube.com/@Lofi%20Girl/<x>'\"", "https://www.youtube.com/@Lofi%20Girl/%3Cx%3E'%22"),
])
def test_other_youtube_pages_are_rebuilt_on_a_fixed_youtube_host(query, expected):
    assert classify_query(query, HOSTS) == Target("playlist", expected)


@pytest.mark.parametrize("query", [
    # urlsplit bu karakterleri ana makine adında bırakır, urllib3 ayraç sayar → yt-dlp başka
    # bir sunucuya bağlanırdı (SSRF: "https://127.0.0.1\.youtube.com/x" → 127.0.0.1:443)
    "https://127.0.0.1\\.youtube.com/x",
    "https://evil.com\\@www.youtube.com/x",
    "https://10.0.0.1%2f.youtube.com/x",
    "https://evil.com%5c.youtube.com/@x",
    "https://www.yоutube.com/@x",                  # Kiril "о"
    "https://www.youtube.com。evil.com/@x",
    "https://www youtube.com/@x",
])
def test_hosts_with_parser_confusing_characters_are_rejected(query):
    with pytest.raises(ResolveError, match="Sadece YouTube bağlantıları destekleniyor"):
        classify_query(query, HOSTS)


def test_schemeless_confusing_host_is_only_a_search():
    target = classify_query("127.0.0.1\\.youtube.com/x", HOSTS)
    assert target.kind == "search"


@pytest.mark.parametrize("query", [
    "https://evil.example.com/watch?v=jNQXAC9IVRw",
    "https://youtube.com.evil.com/watch?v=jNQXAC9IVRw",
    "https://notyoutube.com/watch?v=jNQXAC9IVRw",
    "https://youtube.com@evil.com/watch?v=jNQXAC9IVRw",
    "http://127.0.0.1:9222/json/version",
    "http://192.168.1.1/",
    "file:///C:/Windows/win.ini",
    "ftp://youtube.com/x",
    "javascript://youtube.com/%0aalert(1)",
])
def test_non_youtube_urls_are_rejected(query):
    with pytest.raises(ResolveError, match="Sadece YouTube bağlantıları destekleniyor"):
        classify_query(query, HOSTS)


@pytest.mark.parametrize("query", [
    "https://www.youtube.com/watch?v=../../etc/passwd",
    "https://www.youtube.com/watch?v=",
    "https://youtu.be/a b",
    "https://www.youtube.com/playlist?list=<script>",
])
def test_malformed_youtube_urls_are_rejected(query):
    with pytest.raises(ResolveError):
        classify_query(query, HOSTS)


@pytest.mark.parametrize("query,expected", [
    ("rick astley never gonna give you up", "ytsearch1:rick astley never gonna give you up"),
    ("  tarkan   kuzu\tkuzu ", "ytsearch1:tarkan kuzu kuzu"),
    ("--exec calc.exe", "ytsearch1:--exec calc.exe"),
    ("-o /tmp/x", "ytsearch1:-o /tmp/x"),
    ("Mr.Brightside", "ytsearch1:Mr.Brightside"),
    ("müzik.com/şarkı", "ytsearch1:müzik.com/şarkı"),
])
def test_free_text_becomes_a_search(query, expected):
    assert classify_query(query, HOSTS) == Target("search", expected)


def test_empty_and_oversized_queries():
    with pytest.raises(ResolveError):
        classify_query("   ", HOSTS)
    with pytest.raises(ResolveError, match="500"):
        classify_query("a" * 501, HOSTS)


def test_host_allowed_matches_exact_and_subdomains_only():
    assert host_allowed("youtube.com", HOSTS)
    assert host_allowed("www.youtube.com", HOSTS)
    assert host_allowed("M.YouTube.com.", HOSTS)
    assert not host_allowed("evilyoutube.com", HOSTS)
    assert not host_allowed("youtube.com.evil.com", HOSTS)
    assert not host_allowed("127.0.0.1\\.youtube.com", HOSTS)
    assert not host_allowed("a@b.youtube.com", HOSTS)


# ──────────────────────────────────────────────────────────────
#  yt-dlp komut satırı
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def downloader(settings, monkeypatch):
    monkeypatch.setattr(audio_manager.shutil, "which", lambda name: "C:/node/node.exe" if name == "node" else None)
    monkeypatch.setattr(audio_manager, "js_runtime_version", lambda path: (24, 19, 0))
    return Downloader(settings)


def test_ytdlp_runs_from_this_python_environment(downloader):
    assert downloader.command == [sys.executable, "-m", "yt_dlp"]


@pytest.mark.parametrize("target", [
    Target("video", WATCH),
    Target("playlist", "https://www.youtube.com/playlist?list=PLabc"),
    Target("search", "ytsearch1:-x --exec calc"),
])
def test_resolve_args_end_with_double_dash_and_single_positional(downloader, target):
    args = downloader.resolve_args(target)
    assert args[-2:] == ["--", target.value]
    assert args.count("--") == 1
    assert "-J" in args and "--ignore-config" in args
    assert args[args.index("--encoding") + 1] == "utf-8"
    assert "--no-progress" in args
    # Genel (generic) çıkarıcı kapalı: eşleşmeyen bağlantı için rastgele sunucuya istek atılmaz
    assert args[args.index("--use-extractors") + 1] == "default,-generic"


def test_resolve_args_per_kind(downloader, settings):
    assert "--no-playlist" in downloader.resolve_args(Target("video", WATCH))
    playlist = downloader.resolve_args(Target("playlist", "https://www.youtube.com/playlist?list=PLabc"))
    assert "--flat-playlist" in playlist
    assert playlist[playlist.index("--playlist-end") + 1] == str(settings.playlist_limit)
    assert "--flat-playlist" in downloader.resolve_args(Target("search", "ytsearch1:x"))


def test_download_args_do_not_transcode(downloader, settings):
    args = downloader.download_args(VID)
    assert args[-2:] == ["--", WATCH]
    assert args[args.index("-f") + 1] == AUDIO_FORMAT
    assert args[args.index("-P") + 1] == str(Path(settings.downloads_dir).resolve())
    assert args[args.index("-o") + 1] == f"{VID}.%(ext)s"
    assert "--no-playlist" in args
    for forbidden in ("-x", "--extract-audio", "--audio-format", "--audio-quality", "--no-part"):
        assert forbidden not in args


def test_download_filter_rejects_live_and_overlong_videos(downloader, settings):
    settings.max_duration = 1200
    args = downloader.download_args(VID)
    assert args[args.index("--match-filter") + 1] == "!is_live & duration <=? 1200"
    settings.max_duration = 0  # sınırsız → sadece canlı yayın engeli
    assert downloader.download_filter() == "!is_live"


@pytest.mark.parametrize("mode,node,expected", [
    ("auto", True, ["--js-runtimes", "node"]),
    ("auto", False, []),
    ("none", True, []),
    ("deno", False, ["--js-runtimes", "deno"]),
    ("node", False, ["--js-runtimes", "node"]),
])
def test_js_runtime_args(settings, monkeypatch, mode, node, expected):
    monkeypatch.setattr(audio_manager.shutil, "which", lambda name: "node.exe" if node and name == "node" else None)
    monkeypatch.setattr(audio_manager, "js_runtime_version", lambda path: (22, 1, 0))
    settings.ytdlp_js_runtime = mode
    downloader = Downloader(settings)
    assert downloader.js_runtime_args == expected
    assert (downloader.base_args()[-2:] == expected) if expected else "--js-runtimes" not in downloader.base_args()


@pytest.mark.parametrize("version,expected", [
    ((20, 11, 1), []),                           # yt-dlp Node < 22'yi yok sayar → verme, uyar
    ((22, 0, 0), ["--js-runtimes", "node"]),
    (None, ["--js-runtimes", "node"]),           # sürüm okunamadı → karar yt-dlp'nin
])
def test_auto_js_runtime_requires_a_supported_node(settings, monkeypatch, caplog, version, expected):
    monkeypatch.setattr(audio_manager.shutil, "which", lambda name: "node.exe" if name == "node" else None)
    monkeypatch.setattr(audio_manager, "js_runtime_version", lambda path: version)
    with caplog.at_level(logging.WARNING, logger="meetbot.audio"):
        assert Downloader(settings).js_runtime_args == expected
    assert ("çok eski" in caplog.text) is (expected == [])


@pytest.mark.parametrize("text,expected", [
    ("v24.19.0", (24, 19, 0)), ("v22.1", (22, 1, 0)), ("deno 2.3.1 (stable, release)", (2, 3, 1)),
    ("1.2.11", (1, 2, 11)), ("", None), ("sürüm yok", None),
])
def test_parse_version(text, expected):
    assert audio_manager.parse_version(text) == expected


def test_js_runtime_minimums_match_ytdlp():
    from yt_dlp.utils import _jsruntime

    assert audio_manager.JS_RUNTIME_MIN_VERSIONS == {
        "node": _jsruntime.NodeJsRuntime.MIN_SUPPORTED_VERSION,
        "deno": _jsruntime.DenoJsRuntime.MIN_SUPPORTED_VERSION,
        "bun": _jsruntime.BunJsRuntime.MIN_SUPPORTED_VERSION,
    }


def test_js_runtime_version_reads_a_real_executable_and_survives_a_missing_one(tmp_path):
    audio_manager.js_runtime_version.cache_clear()
    assert audio_manager.js_runtime_version(str(tmp_path / "yok" / "node.exe")) is None
    assert audio_manager.js_runtime_version(sys.executable) == audio_manager.parse_version(sys.version)


# ──────────────────────────────────────────────────────────────
#  JSON ayrıştırma
# ──────────────────────────────────────────────────────────────

def test_entry_to_info_flat_search_entry():
    info = entry_to_info({"_type": "url", "ie_key": "Youtube", "id": "dQw4w9WgXcQ", "duration": 213.6,
                          "title": "  Rick   Astley - Never Gonna Give You Up ", "live_status": None})
    assert info.video_id == "dQw4w9WgXcQ"
    assert info.title == "Rick Astley - Never Gonna Give You Up"
    assert info.duration == 214
    assert info.url == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert info.thumbnail == "https://i.ytimg.com/vi/dQw4w9WgXcQ/mqdefault.jpg"
    assert info.is_live is False


@pytest.mark.parametrize("entry", [
    {"id": VID, "title": "x", "is_live": True},
    {"id": VID, "title": "x", "live_status": "is_live"},
    {"id": VID, "title": "x", "live_status": "is_upcoming"},
])
def test_entry_to_info_detects_live(entry):
    assert entry_to_info(entry).is_live is True


@pytest.mark.parametrize("entry", [
    None, "text", {"title": "no id"}, {"id": "../etc"}, {"id": "UCuAXFkgsw1L7xaCfnd5JJOw"},
    {"_type": "url", "ie_key": "YoutubeTab", "id": "abcdefghijk"},
    {"_type": "playlist", "id": "abcdefghijk"},
    {"_type": "url", "ie_key": "Youtube", "id": VID, "title": "[Private video]"},
    {"_type": "url", "ie_key": "Youtube", "id": VID, "title": "[Deleted video]"},
])
def test_entry_to_info_rejects_non_videos(entry):
    assert entry_to_info(entry) is None


def test_entry_to_info_fallbacks():
    info = entry_to_info({"id": VID, "duration": 0})
    assert info.title == VID and info.duration is None
    assert entry_to_info({"id": VID, "duration": "NA"}).duration is None
    assert entry_to_info({"id": VID, "duration": True}).duration is None


def test_parse_ytdlp_json_variants():
    single = json.dumps({"_type": "video", "id": VID, "title": "Me at the zoo", "duration": 19})
    assert [i.video_id for i in parse_ytdlp_json(single)] == [VID]
    playlist = json.dumps({"_type": "playlist", "entries": [
        {"_type": "url", "ie_key": "Youtube", "id": "aaaaaaaaaaa", "title": "A"},
        {"_type": "url", "ie_key": "Youtube", "id": "bbbbbbbbbbb", "title": "[Private video]"},
        {"_type": "url", "ie_key": "Youtube", "id": "ccccccccccc", "title": "C"},
    ]})
    assert [i.video_id for i in parse_ytdlp_json(playlist)] == ["aaaaaaaaaaa", "ccccccccccc"]
    assert parse_ytdlp_json(json.dumps({"entries": None})) == []
    for bad in ("", "WARNING: x", "[1, 2]", "null"):
        with pytest.raises(ResolveError):
            parse_ytdlp_json(bad)


def test_friendly_error_never_leaks_raw_stderr():
    assert friendly_error("ERROR: [youtube] x: Private video. Sign in", "d") == "Bu video gizli"
    assert friendly_error("ERROR: Sign in to confirm you’re not a bot", "d").startswith("YouTube bot doğrulaması")
    assert friendly_error("ERROR: [youtube] abc: Video unavailable", "d") == "Video kullanılamıyor"
    assert friendly_error(r"C:\Users\Quench\secret\trace", "Şarkı indirilemedi") == "Şarkı indirilemedi"
    no_extractor = "ERROR: No suitable extractor found for URL https://www.youtube.com/about/press"
    assert friendly_error(no_extractor, "d") == "Bu YouTube bağlantısı desteklenmiyor"


# ──────────────────────────────────────────────────────────────
#  resolve() — sahte yt-dlp süreci ile
# ──────────────────────────────────────────────────────────────

def fake_run(downloader, result=None, delay=0.0, raises=None, on_call=None):
    calls: list[list[str]] = []

    async def run(args, timeout):
        calls.append(args)
        if on_call:
            on_call(args)
        if delay:
            await asyncio.sleep(delay)
        if raises:
            raise raises
        return result

    downloader._run = run
    return calls


async def test_resolve_search_returns_first_hit(downloader):
    payload = {"_type": "playlist", "entries": [
        {"_type": "url", "ie_key": "Youtube", "id": "dQw4w9WgXcQ", "title": "Rick", "duration": 213},
        {"_type": "url", "ie_key": "Youtube", "id": "aaaaaaaaaaa", "title": "Other", "duration": 100},
    ]}
    calls = fake_run(downloader, (0, json.dumps(payload), ""))
    infos = await downloader.resolve("rick astley")
    assert [i.video_id for i in infos] == ["dQw4w9WgXcQ"]
    assert calls[0][-1] == "ytsearch1:rick astley"


async def test_resolve_playlist_is_capped(downloader, settings):
    settings.playlist_limit = 2
    entries = [{"_type": "url", "ie_key": "Youtube", "id": f"video{i:06d}", "title": str(i)} for i in range(5)]
    fake_run(downloader, (0, json.dumps({"_type": "playlist", "entries": entries}), ""))
    infos = await downloader.resolve("https://www.youtube.com/playlist?list=PLabc")
    assert [i.video_id for i in infos] == ["video000000", "video000001"]


async def test_resolve_errors_are_short_and_turkish(downloader):
    fake_run(downloader, (1, "", "ERROR: [youtube] jNQXAC9IVRw: Video unavailable\nTraceback C:\\Users\\x"))
    with pytest.raises(ResolveError) as exc_info:
        await downloader.resolve(WATCH)
    assert str(exc_info.value) == "Video kullanılamıyor"
    fake_run(downloader, (0, json.dumps({"_type": "playlist", "entries": []}), ""))
    with pytest.raises(ResolveError, match="Sonuç bulunamadı"):
        await downloader.resolve("çok garip bir arama")
    fake_run(downloader, raises=asyncio.TimeoutError())
    with pytest.raises(ResolveError, match="zamanında alınamadı"):
        await downloader.resolve("herhangi")


async def test_resolve_rejects_foreign_urls_without_running_ytdlp(downloader):
    calls = fake_run(downloader, (0, "{}", ""))
    with pytest.raises(ResolveError):
        await downloader.resolve("http://169.254.169.254/latest/meta-data")
    assert calls == []


# ──────────────────────────────────────────────────────────────
#  download() — önbellek, kilitler, eşzamanlılık, temizlik
# ──────────────────────────────────────────────────────────────

def writes_file(downloader, ext="webm"):
    def on_call(args):
        video_id = args[args.index("-o") + 1].split(".")[0]
        (downloader.downloads_dir / f"{video_id}.{ext}").write_bytes(b"audio")
    return on_call


async def test_download_success_returns_absolute_path_inside_downloads(downloader):
    calls = fake_run(downloader, (0, "", ""), on_call=writes_file(downloader, "m4a"))
    path = Path(await downloader.download(VID))
    assert path.is_absolute() and path.parent == downloader.downloads_dir
    assert path.name == f"{VID}.m4a"
    assert len(calls) == 1


LEFTOVERS = (
    f"{VID}.webm.part", f"{VID}.webm.ytdl", f"{VID}.webm.part-Frag1",  # yarım indirme / parçalar
    f"{VID}.temp.m4a", f"{VID}.f251.webm",                              # yt-dlp ara dosyaları
    f"{VID}.part",
)


async def test_download_cache_hit_skips_ytdlp_and_ignores_intermediate_files(downloader):
    downloader.downloads_dir.mkdir(parents=True, exist_ok=True)
    for name in LEFTOVERS:
        (downloader.downloads_dir / name).write_bytes(b"half")
    (downloader.downloads_dir / f"{VID}.m4a").write_bytes(b"")  # boş dosya da sayılmaz
    assert downloader.find_cached(VID) is None
    seen_before_run: list[str] = []

    def on_call(args):
        seen_before_run.extend(sorted(p.name for p in downloader.downloads_dir.iterdir()))
        writes_file(downloader, "webm")(args)

    calls = fake_run(downloader, (0, "", ""), on_call=on_call)
    await downloader.download(VID)
    assert len(calls) == 1
    # Önceki denemeden kalan çöp (boş <id>.m4a dahil) yt-dlp çalışmadan önce silinir;
    # yoksa yt-dlp boş dosyayı "zaten indirilmiş" sanıp atlardı.
    assert seen_before_run == []
    again = await downloader.download(VID)
    assert len(calls) == 1 and again.endswith(f"{VID}.webm")
    assert [p.name for p in downloader.downloads_dir.iterdir()] == [f"{VID}.webm"]


async def test_concurrent_downloads_of_same_video_share_one_process(downloader):
    calls = fake_run(downloader, (0, "", ""), delay=0.05, on_call=writes_file(downloader))
    paths = await asyncio.gather(*(downloader.download(VID) for _ in range(5)))
    assert len(calls) == 1 and len(set(paths)) == 1
    assert downloader._id_locks == {}


async def test_at_most_two_downloads_run_in_parallel(downloader):
    active = peak = 0

    async def run(args, timeout):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.03)
        writes_file(downloader)(args)
        active -= 1
        return 0, "", ""

    downloader._run = run
    await asyncio.gather(*(downloader.download(f"video{i:06d}") for i in range(6)))
    assert peak == 2


def gated_run(downloader):
    """Sahte yt-dlp: her video, testteki kapısı açılana kadar 'iner'; başlangıç sırası kaydedilir."""
    gates: dict[str, asyncio.Event] = {}
    started: list[str] = []

    async def run(args, timeout):
        video_id = args[args.index("-o") + 1].split(".")[0]
        started.append(video_id)
        await gates.setdefault(video_id, asyncio.Event()).wait()
        writes_file(downloader)(args)
        return 0, "", ""

    downloader._run = run
    return gates, started


async def test_prioritized_download_never_waits_behind_prefetches(downloader):
    """Bulgu: iki yavaş ön-indirme yuvaları doldurunca, çalınması beklenen parça (play_now,
    listenin ilk şarkısı) onlar bitene kadar hiç başlamıyordu."""
    gates, started = gated_run(downloader)
    prefetches = [asyncio.ensure_future(downloader.download(v)) for v in ("slowBBBBBBB", "slowCCCCCCC")]
    await wait_for(lambda: len(started) == 2)
    other = asyncio.ensure_future(downloader.download("laterDDDDDD"))   # sıradaki bir ön-indirme
    downloader.prioritize("fastAAAAAAA")
    current = asyncio.ensure_future(downloader.download("fastAAAAAAA"))
    await wait_for(lambda: "fastAAAAAAA" in started)                     # ayrılmış yuvada hemen başladı
    assert "laterDDDDDD" not in started                                 # normal bekleyen yine sırada
    gates.setdefault("fastAAAAAAA", asyncio.Event()).set()
    assert (await current).endswith("fastAAAAAAA.webm")
    for gate in ("slowBBBBBBB", "slowCCCCCCC", "laterDDDDDD"):
        gates.setdefault(gate, asyncio.Event()).set()
    await asyncio.gather(*prefetches, other)
    assert downloader._download_slots.active == 0


async def test_prioritizing_an_already_waiting_download_moves_it_first(downloader):
    gates, started = gated_run(downloader)
    running = [asyncio.ensure_future(downloader.download(v)) for v in ("slowBBBBBBB", "slowCCCCCCC")]
    await wait_for(lambda: len(started) == 2)
    waiting = [asyncio.ensure_future(downloader.download(v)) for v in ("queuedDDDDD", "queuedEEEEE")]
    await asyncio.sleep(0.01)
    assert started == ["slowBBBBBBB", "slowCCCCCCC"]
    downloader.prioritize("queuedEEEEE")  # ör. karıştırma + atla: sıradaki bekleyen şimdi çalınacak
    await wait_for(lambda: "queuedEEEEE" in started)
    assert "queuedDDDDD" not in started
    for gate in ("slowBBBBBBB", "slowCCCCCCC", "queuedDDDDD", "queuedEEEEE"):
        gates.setdefault(gate, asyncio.Event()).set()
    await asyncio.gather(*running, *waiting)
    assert downloader._download_slots.active == 0


async def test_cancelled_waiter_releases_nothing_and_slots_stay_consistent(downloader):
    gates, started = gated_run(downloader)
    running = [asyncio.ensure_future(downloader.download(v)) for v in ("slowBBBBBBB", "slowCCCCCCC")]
    await wait_for(lambda: len(started) == 2)
    waiter = asyncio.ensure_future(downloader.download("queuedDDDDD"))
    await asyncio.sleep(0.01)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    slots = downloader._download_slots
    assert slots.active == 2 and not slots._waiters
    for gate in ("slowBBBBBBB", "slowCCCCCCC"):
        gates[gate].set()
    await asyncio.gather(*running)
    assert slots.active == 0


async def wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "koşul sağlanmadı"
        await asyncio.sleep(0.005)


async def test_download_failure_cleans_partials_and_hides_details(downloader):
    def leave_partials(args):
        for name in LEFTOVERS:
            downloader.downloads_dir.joinpath(name).write_bytes(b"half")

    fake_run(downloader, (1, "", "ERROR: HTTP Error 403: Forbidden C:\\Users\\Quench"), on_call=leave_partials)
    with pytest.raises(DownloadError) as exc_info:
        await downloader.download(VID)
    assert str(exc_info.value) == "Şarkı indirilemedi"
    assert list(downloader.downloads_dir.iterdir()) == []


async def test_download_rejected_by_match_filter(downloader):
    stdout = "[download] Canlı Radyo does not pass filter (!is_live & duration <=? 1200), skipping ..\n"
    fake_run(downloader, (0, stdout, ""))
    with pytest.raises(DownloadError, match="Canlı yayınlar ve çok uzun şarkılar indirilemez"):
        await downloader.download(VID)
    assert list(downloader.downloads_dir.iterdir()) == []


async def test_missing_interpreter_maps_to_short_errors(downloader, caplog):
    downloader.command = [str(Path(downloader.downloads_dir) / "yok" / "python.exe"), "-m", "yt_dlp"]
    with caplog.at_level(logging.ERROR, logger="meetbot.audio"):
        with pytest.raises(ResolveError, match="yt-dlp çalıştırılamadı"):
            await downloader.resolve("herhangi bir şarkı")
        with pytest.raises(DownloadError, match="yt-dlp çalıştırılamadı"):
            await downloader.download(VID)
    assert "yt-dlp başlatılamadı" in caplog.text


async def test_download_timeout_and_missing_file(downloader):
    fake_run(downloader, raises=asyncio.TimeoutError())
    with pytest.raises(DownloadError, match="zaman aşımı"):
        await downloader.download(VID)
    fake_run(downloader, (0, "", ""))
    with pytest.raises(DownloadError, match="dosya bulunamadı"):
        await downloader.download(VID)


async def test_cancelled_download_removes_partials(downloader):
    def leave_partial(args):
        downloader.downloads_dir.joinpath(f"{VID}.m4a.part").write_bytes(b"half")

    fake_run(downloader, (0, "", ""), delay=10, on_call=leave_partial)
    task = asyncio.ensure_future(downloader.download(VID))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(downloader.downloads_dir.iterdir()) == []


@pytest.mark.parametrize("video_id", ["", "../../x", "a" * 30, "abc def", "*"])
async def test_download_validates_video_id(downloader, video_id):
    with pytest.raises(DownloadError, match="Geçersiz video"):
        await downloader.download(video_id)


# ──────────────────────────────────────────────────────────────
#  _run() — gerçek alt süreç (yt-dlp yerine küçük Python betikleri)
# ──────────────────────────────────────────────────────────────

def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes

        synchronize, still_active = 0x00100000 | 0x0400, 259  # SYNCHRONIZE | QUERY_INFORMATION
        handle = ctypes.windll.kernel32.OpenProcess(synchronize, False, pid)
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
            return code.value == still_active
        finally:
            ctypes.windll.kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_run_decodes_output_and_logs_ytdlp_warnings(downloader, caplog):
    script = "import sys; print('çıktı ✓'); print('WARNING: [youtube] uyarı', file=sys.stderr)"
    downloader.command = [sys.executable, "-X", "utf8", "-c", script]
    with caplog.at_level(logging.WARNING, logger="meetbot.audio"):
        code, out, err = await downloader._run([], timeout=30)
    assert code == 0 and out.strip() == "çıktı ✓"
    assert "yt-dlp: [youtube] uyarı" in caplog.text


async def test_run_timeout_kills_the_whole_process_tree(downloader, tmp_path):
    pid_file = tmp_path / "pids.txt"
    script = textwrap.dedent(f"""
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        with open({str(pid_file)!r}, "w") as f:
            f.write(f"{{os.getpid()}} {{child.pid}}")
        time.sleep(60)
    """)
    downloader.command = [sys.executable, "-c", script]
    started = time.monotonic()
    with pytest.raises(asyncio.TimeoutError):
        # Betik PID dosyasını yazana kadar bekleyecek kadar uzun, 60 sn'den çok kısa
        await downloader._run([], timeout=3)
    assert time.monotonic() - started < 20
    pids = [int(p) for p in pid_file.read_text().split()]
    assert len(pids) == 2
    for _ in range(50):
        if not any(_pid_alive(p) for p in pids):
            break
        await asyncio.sleep(0.1)
    assert not any(_pid_alive(p) for p in pids), "yt-dlp süreç ağacı öldürülmedi"


# ──────────────────────────────────────────────────────────────
#  cleanup / purge_all
# ──────────────────────────────────────────────────────────────

def test_cleanup_only_touches_files_inside_downloads(downloader, tmp_path, caplog):
    downloader.downloads_dir.mkdir(parents=True, exist_ok=True)
    inside = downloader.downloads_dir / f"{VID}.webm"
    inside.write_bytes(b"x")
    outside = tmp_path / "important.txt"
    outside.write_text("keep")
    downloader.cleanup(str(inside))
    assert not inside.exists()
    downloader.cleanup(str(inside))  # zaten yok → sessizce geç
    with caplog.at_level(logging.WARNING, logger="meetbot.audio"):
        downloader.cleanup(str(outside))
        downloader.cleanup(str(downloader.downloads_dir / ".." / "important.txt"))
    assert outside.read_text() == "keep"
    assert "dışındaki dosya silinmedi" in caplog.text


OWN_FILES = (f"{VID}.webm", f"{VID}.m4a", "dQw4w9WgXcQ.opus", f"{VID}.webm.part", f"{VID}.webm.part-Frag3",
             f"{VID}.webm.ytdl", f"{VID}.f251.webm", f"{VID}.f140.m4a.part", f"{VID}.temp.m4a", "-_aBc12345Z.mp4")
FOREIGN_FILES = ("Summer2020.mp3", "VID_20240101_123456.mp4", "holiday.webm", "notes.txt", "tatil.m4a",
                 f"{VID}.txt", f"{VID}.docx", f"Şarkı [{VID}].webm", f"{VID} (1).webm", "desktop.ini")


def test_purge_all_removes_only_the_bots_own_files(downloader):
    """Bulgu: MEETBOT_DOWNLOADS_DIR kişisel bir klasörü gösterirse içindeki HER dosya siliniyordu."""
    downloader.downloads_dir.mkdir(parents=True, exist_ok=True)
    for name in OWN_FILES + FOREIGN_FILES:
        (downloader.downloads_dir / name).write_bytes(b"x")
    (downloader.downloads_dir / "sub").mkdir()
    (downloader.downloads_dir / "sub" / f"{VID}.webm").write_bytes(b"x")  # alt klasöre inilmez
    assert downloader.purge_all() == len(OWN_FILES)
    remaining = sorted(p.name for p in downloader.downloads_dir.iterdir())
    assert remaining == sorted((*FOREIGN_FILES, "sub"))
    assert (downloader.downloads_dir / "sub" / f"{VID}.webm").exists()


# ──────────────────────────────────────────────────────────────
#  Gerçek ağ (YouTube)
# ──────────────────────────────────────────────────────────────

@pytest.fixture
def real_downloader(settings):
    return Downloader(settings)


@pytest.mark.network
async def test_network_resolve_single_video(real_downloader):
    [info] = await real_downloader.resolve(WATCH)
    assert info.video_id == VID
    assert info.title == "Me at the zoo"
    assert info.duration == 19 and not info.is_live


@pytest.mark.network
async def test_network_resolve_search(real_downloader):
    [info] = await real_downloader.resolve("rick astley never gonna give you up")
    assert "rick astley" in info.title.lower()
    assert info.duration and info.duration > 60
    assert info.url == f"https://www.youtube.com/watch?v={info.video_id}"


@pytest.mark.network
async def test_network_resolve_playlist(real_downloader, settings):
    settings.playlist_limit = 3
    infos = await real_downloader.resolve("https://www.youtube.com/playlist?list=PLFgquLnL59alCl_2TQvOiD5Vgm1hCaGSI")
    assert len(infos) == 3
    assert all(audio_manager.is_valid_video_id(i.video_id) and i.title for i in infos)


@pytest.mark.network
async def test_network_download_native_audio_without_ffmpeg(real_downloader, monkeypatch):
    # ffmpeg'i PATH'ten çıkar: dönüştürme yapılmadığı için indirme yine de başarılı olmalı
    path_without_ffmpeg = os.pathsep.join(
        p for p in os.environ.get("PATH", "").split(os.pathsep) if "ffmpeg" not in p.lower()
    )
    monkeypatch.setenv("PATH", path_without_ffmpeg)
    path = Path(await real_downloader.download(VID))
    assert path.suffix.lstrip(".") in {"webm", "opus", "m4a", "ogg", "mp4"}
    assert path.stat().st_size > 50_000
    assert path.parent == real_downloader.downloads_dir
    assert [p.name for p in real_downloader.downloads_dir.iterdir()] == [path.name]  # yarım dosya yok


@pytest.mark.network
async def test_network_download_filter_blocks_overlong_video(real_downloader, settings):
    settings.max_duration = 5  # "Me at the zoo" 19 sn
    with pytest.raises(DownloadError, match="çok uzun"):
        await real_downloader.download(VID)
    assert list(real_downloader.downloads_dir.iterdir()) == []


# ──────────────────────────────────────────────────────────────
#  İndirme: veri merkezi IP'lerindeki rastgele 403'e karşı başka istemciyle tekrar deneme
# ──────────────────────────────────────────────────────────────

class _ScriptedRuns:
    """Downloader._run yerine: her çağrıda sıradaki sonucu döndürür, başarıda dosyayı oluşturur."""

    def __init__(self, downloader, results):
        self.downloader = downloader
        self.results = list(results)
        self.calls = []

    async def __call__(self, args, timeout):
        self.calls.append(args)
        code, stdout, stderr = self.results.pop(0)
        if code == 0:
            (self.downloader.downloads_dir / f"{VID}.webm").write_bytes(b"ses")
        return code, stdout, stderr


def _client_of(args):
    return args[args.index("--extractor-args") + 1] if "--extractor-args" in args else None


async def test_download_retries_403_with_the_mweb_client(downloader, monkeypatch):
    runs = _ScriptedRuns(downloader, [
        (1, "", "ERROR: unable to download video data: HTTP Error 403: Forbidden"),
        (0, "", ""),
    ])
    monkeypatch.setattr(downloader, "_run", runs)
    path = await downloader.download(VID)
    assert path.endswith(f"{VID}.webm")
    assert [_client_of(a) for a in runs.calls] == [None, "youtube:player_client=mweb"]


async def test_download_gives_up_after_all_clients_fail(downloader, monkeypatch):
    forbidden = (1, "", "ERROR: unable to download video data: HTTP Error 403: Forbidden")
    runs = _ScriptedRuns(downloader, [forbidden] * 3)
    monkeypatch.setattr(downloader, "_run", runs)
    with pytest.raises(DownloadError, match="indirilemedi"):
        await downloader.download(VID)
    assert [_client_of(a) for a in runs.calls] == [None, "youtube:player_client=mweb", None]
    assert not list(downloader.downloads_dir.glob(f"{VID}.*"))


async def test_download_does_not_retry_permanent_errors(downloader, monkeypatch):
    runs = _ScriptedRuns(downloader, [(1, "", "ERROR: [youtube] abc: Private video. Sign in if you've been granted access")])
    monkeypatch.setattr(downloader, "_run", runs)
    with pytest.raises(DownloadError, match="gizli"):
        await downloader.download(VID)
    assert len(runs.calls) == 1
