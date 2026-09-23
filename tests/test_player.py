"""Player durum makinesi — SPEC §3 değişmezleri (I1–I7) + FakeBot."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from audio_manager import DownloadError, ResolveError
from bot import BotError, BotNotReady
from fake_bot import FakeBot, wav_duration
from fakes import MEET_LINK, FakeDownloader, Recorder, ScriptedBot, make_info, settle, wait_until, write_wav
from player import Player, PlayerError, Track, format_duration


def wire(player: Player, bot) -> None:
    bot.on_status = player.on_bot_status
    bot.on_track_ended = player.on_track_ended
    bot.on_progress = player.on_progress


async def make_harness(settings, connected: bool = True):
    bot = ScriptedBot(settings, connected=connected)
    downloader = FakeDownloader(settings.downloads_dir)
    recorder = Recorder()
    player = Player(settings, bot, downloader, recorder)
    wire(player, bot)
    return SimpleNamespace(player=player, bot=bot, dl=downloader, rec=recorder)


@pytest.fixture
async def h(settings):
    harness = await make_harness(settings, connected=True)
    yield harness
    await harness.player.shutdown()


@pytest.fixture
async def offline(settings):
    """Bot toplantıda DEĞİL."""
    harness = await make_harness(settings, connected=False)
    yield harness
    await harness.player.shutdown()


def queue_titles(player: Player) -> list[str]:
    return [t.video_id for t in player.queue]


async def playing(h, video_id: str) -> None:
    await wait_until(lambda: h.player.state == "playing" and h.player.current is not None
                     and h.player.current.video_id == video_id, message=f"{video_id} çalmıyor")


# ──────────────────────────────────────────────────────────────
#  Model
# ──────────────────────────────────────────────────────────────

@pytest.mark.parametrize("seconds,expected", [
    (None, "?"), (0, "0:00"), (59, "0:59"), (183, "3:03"), (3600, "1:00:00"), (3723, "1:02:03"),
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_track_public_never_exposes_private_fields():
    track = Track(id=1, video_id="abcdefghijk", title="T", duration=None, url="https://www.youtube.com/watch?v=abcdefghijk",
                  thumbnail="https://i.ytimg.com/vi/abcdefghijk/mqdefault.jpg", added_by="Vedat",
                  added_at="2026-09-23T10:15:00Z", status="error", file_path=r"C:\gizli\a.webm", error=r"C:\gizli hata")
    public = track.public()
    assert set(public) == {"id", "video_id", "title", "duration", "duration_str", "url", "thumbnail",
                           "added_by", "added_at", "status"}
    assert public["duration_str"] == "?"
    assert "gizli" not in repr(public)


async def test_snapshot_shape(h):
    snap = h.player.snapshot()
    assert snap == {
        "queue": [], "current": None,
        "playback": {"state": "idle", "position": 0, "duration": 0, "repeat": "off"},
        "volume": {"music": 80, "mic": 80}, "mic_muted": False,
        "bot": {"status": "connected", "meet_link": MEET_LINK, "detail": None},
        "history": [],
    }


# ──────────────────────────────────────────────────────────────
#  add + I4 (bot bağlı değilken)
# ──────────────────────────────────────────────────────────────

async def test_add_while_bot_offline_never_starts_playback(offline):
    tracks = await offline.player.add("song-a", "Vedat")
    await settle()
    assert [t.video_id for t in tracks] == ["song-a"]
    assert offline.player.state == "idle" and offline.player.current is None
    assert queue_titles(offline.player) == ["song-a"]
    assert offline.bot.play_calls == []
    added = tracks[0]
    assert added.added_by == "Vedat" and added.added_at.endswith("Z")
    notice = offline.rec.last("notice")
    assert notice["level"] == "success" and notice["message"] == "🎵 Vedat: Şarkı song-a kuyruğa eklendi"
    assert offline.rec.last("queue")["queue"][0]["id"] == added.id


async def test_resume_with_queue_while_offline_is_an_error(offline):
    await offline.player.add("song-a", "Vedat")
    with pytest.raises(PlayerError, match="Bot toplantıda değil"):
        await offline.player.resume()


async def test_connect_starts_next_queued_track(offline):
    await offline.player.add("song-a", "Vedat")
    await offline.player.add("song-b", "Vedat")
    await offline.bot.connect()
    await playing(offline, "song-a")
    assert queue_titles(offline.player) == ["song-b"]
    assert offline.rec.last("bot")["status"] == "connected"


async def test_add_autostarts_only_when_idle(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    await h.player.add("song-b", "Ali")
    await settle()
    assert h.player.current.video_id == "song-a"
    assert queue_titles(h.player) == ["song-b"]
    assert len(h.bot.play_calls) == 1


async def test_add_rejects_bad_queries_without_resolving(h):
    for query in ("", "   ", "x" * 501):
        with pytest.raises(PlayerError):
            await h.player.add(query, "Vedat")
    assert h.dl.resolve_calls == []


async def test_add_reports_resolve_errors(h):
    h.dl.resolve_error = ResolveError("Sadece YouTube bağlantıları destekleniyor")
    with pytest.raises(PlayerError, match="Sadece YouTube"):
        await h.player.add("https://evil.example/x", "Vedat")


# ──────────────────────────────────────────────────────────────
#  I7 — sınırlar
# ──────────────────────────────────────────────────────────────

async def test_max_queue(offline, settings):
    settings.max_queue = 2
    await offline.player.add("song-a", "A")
    await offline.player.add("song-b", "B")
    with pytest.raises(PlayerError, match="Kuyruk dolu"):
        await offline.player.add("song-c", "C")
    assert offline.dl.resolve_calls == ["song-a", "song-b"]  # dolu kuyruk YouTube'a gitmez


async def test_max_user_queue(offline, settings):
    settings.max_user_queue = 1
    await offline.player.add("song-a", "Vedat")
    with pytest.raises(PlayerError, match="en fazla 1 şarkın"):
        await offline.player.add("song-b", "Vedat")
    await offline.player.add("song-c", "Ali")
    assert queue_titles(offline.player) == ["song-a", "song-c"]


async def test_max_duration_skips_long_entries_and_reports_count(offline, settings):
    settings.max_duration = 600
    offline.dl.catalog["liste"] = [make_info("short-1", duration=100), make_info("long-01", duration=5000),
                                   make_info("unknwn1", duration=None), make_info("short-2", duration=600)]
    tracks = await offline.player.add("liste", "Vedat")
    assert [t.video_id for t in tracks] == ["short-1", "unknwn1", "short-2"]
    notice = offline.rec.last("notice")
    assert notice["level"] == "warning"
    assert "3 şarkı kuyruğa eklendi" in notice["message"] and "1 şarkı atlandı: 1 çok uzun" in notice["message"]


async def test_single_too_long_track_is_rejected(offline, settings):
    settings.max_duration = 60
    offline.dl.catalog["uzun"] = [make_info("long-01", duration=61)]
    with pytest.raises(PlayerError, match="Şarkı çok uzun .en fazla 1:00"):
        await offline.player.add("uzun", "Vedat")
    assert offline.player.queue == []


async def test_live_streams_are_rejected(offline):
    offline.dl.catalog["canli"] = [make_info("live-01", duration=None, is_live=True)]
    with pytest.raises(PlayerError, match="Canlı yayınlar eklenemez"):
        await offline.player.add("canli", "Vedat")
    offline.dl.catalog["karisik"] = [make_info("live-01", is_live=True), make_info("song-a")]
    tracks = await offline.player.add("karisik", "Vedat")
    assert [t.video_id for t in tracks] == ["song-a"]
    assert "1 canlı yayın" in offline.rec.last("notice")["message"]


async def test_playlist_limit_and_partial_fill(offline, settings):
    settings.playlist_limit = 3
    offline.dl.catalog["liste"] = [make_info(f"song-{i:02d}") for i in range(10)]
    tracks = await offline.player.add("liste", "Vedat")
    assert len(tracks) == 3
    settings.max_queue = 4
    tracks = await offline.player.add("liste", "Ali")
    assert len(tracks) == 1
    assert "2 kuyruk sınırı" in offline.rec.last("notice")["message"]


# ──────────────────────────────────────────────────────────────
#  I2 — "playing" ancak bot.play başarılı olunca
# ──────────────────────────────────────────────────────────────

async def test_state_is_loading_until_bot_play_succeeds(h):
    h.bot.play_gate = asyncio.Event()
    await h.player.add("song-a", "Vedat")
    await wait_until(lambda: len(h.bot.play_calls) == 1)
    assert h.player.state == "loading"
    assert all(m["state"] != "playing" for m in h.rec.of("playback"))
    h.bot.play_gate.set()
    await playing(h, "song-a")


async def test_bot_error_marks_track_and_advances(offline):
    await offline.player.add("song-a", "Vedat")
    await offline.player.add("song-b", "Vedat")
    failed = offline.player.queue[0]
    offline.bot.play_errors.append(BotError("Ses sayfaya yüklenemedi"))
    await offline.bot.connect()
    await playing(offline, "song-b")
    assert failed.status == "error" and failed.error == "Ses sayfaya yüklenemedi"
    assert list(offline.player.history) == []
    assert any("Şarkı song-a çalınamadı, atlandı" in n["message"] for n in offline.rec.of("notice"))
    assert not Path(offline.dl.path_for("song-a")).exists()  # hatalı parçanın dosyası da silinir


async def test_bot_errors_on_every_track_drain_queue_iteratively(offline):
    for i in range(40):
        await offline.player.add(f"song-{i:02d}", "Vedat")
    offline.bot.play_errors.extend(BotError("bozuk") for _ in range(40))
    await offline.bot.connect()
    await wait_until(lambda: len(offline.bot.play_calls) == 40 and offline.player.state == "idle", timeout=5)
    assert offline.player.queue == [] and offline.player.current is None
    assert len([n for n in offline.rec.of("notice") if "çalınamadı" in n["message"]]) == 40


async def test_bot_not_ready_keeps_current_paused_then_replays_on_connect(h):
    h.bot.play_errors.append(BotNotReady("Bot toplantıda değil"))
    await h.player.add("song-a", "Vedat")
    await wait_until(lambda: h.player.state == "paused")
    assert h.player.current.video_id == "song-a" and h.player.current.status == "ready"
    await h.bot.connect()
    await playing(h, "song-a")
    assert [call[2] for call in h.bot.play_calls] == [0.0, 0.0]


# ──────────────────────────────────────────────────────────────
#  I1 — tek olay = tek ilerleme; asla iki parça birden
# ──────────────────────────────────────────────────────────────

async def _three_queued(h):
    for vid in ("song-a", "song-b", "song-c"):
        await h.player.add(vid, "Vedat")
    await playing(h, "song-a")


async def test_skip_then_stale_ended_advances_once(h):
    await _three_queued(h)
    old_token = h.bot.token
    await h.player.skip()
    await playing(h, "song-b")
    await h.player.on_track_ended(old_token)  # botun eski parça için geç gelen "bitti" olayı
    await settle()
    assert h.player.current.video_id == "song-b"
    assert queue_titles(h.player) == ["song-c"]
    assert len(h.bot.play_calls) == 2
    assert h.bot.stop_calls == 1


async def test_concurrent_skip_ended_resume_plays_exactly_one_track(h):
    await _three_queued(h)
    token_a = h.bot.token
    await asyncio.gather(h.player.on_track_ended(token_a), h.player.skip(), h.player.resume(),
                         h.player.on_track_ended(token_a))
    await playing(h, "song-b")
    await settle()
    assert queue_titles(h.player) == ["song-c"]
    assert [Path(c[0]).stem for c in h.bot.play_calls] == ["song-a", "song-b"]
    assert h.bot.max_concurrent_plays == 1
    assert h.bot.token == h.player._active_token


async def test_stale_progress_is_ignored(h):
    await _three_queued(h)
    old_token = h.bot.token
    await h.player.skip()
    await playing(h, "song-b")
    await h.player.on_progress(old_token, 99.0, 180.0, False)
    assert h.player.position == 0.0


async def test_progress_updates_position_and_is_throttled(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    h.player._last_progress = 0.0
    await h.bot.report_progress(10.0)
    await h.bot.report_progress(11.0)
    assert h.player.position == 11.0
    progress = h.rec.of("progress")
    assert len(progress) == 1 and progress[0]["position"] == 10.0 and progress[0]["state"] == "playing"


# ──────────────────────────────────────────────────────────────
#  I3 — indirme sürerken durdur/atla/temizle/kaldır
# ──────────────────────────────────────────────────────────────

async def test_stop_during_download_discards_result(h):
    gate = h.dl.download_gates["song-a"] = asyncio.Event()
    await h.player.add("song-a", "Vedat")
    assert h.player.state == "loading"
    await h.player.stop()
    assert h.player.state == "idle" and h.player.current is None
    gate.set()
    await settle(60)
    assert h.bot.play_calls == []
    assert not h.dl.path_for("song-a").exists()


async def test_skip_during_download_plays_next_and_never_the_stale_one(h):
    gate = h.dl.download_gates["song-a"] = asyncio.Event()
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-b", "Vedat")
    await h.player.skip()
    await playing(h, "song-b")
    gate.set()
    await settle(60)
    assert [Path(c[0]).stem for c in h.bot.play_calls] == ["song-b"]
    assert h.player.current.video_id == "song-b"


async def test_clear_during_prefetch_cancels_downloads_and_leaves_no_files(offline):
    gates = {vid: offline.dl.download_gates.setdefault(vid, asyncio.Event()) for vid in ("song-a", "song-b")}
    await offline.player.add("song-a", "Vedat")
    await offline.player.add("song-b", "Vedat")
    await wait_until(lambda: len(offline.dl.download_calls) == 2)
    await offline.player.clear()
    for gate in gates.values():
        gate.set()
    await settle(60)
    assert offline.player._downloads == {}
    assert offline.dl.completed == []
    assert list(Path(offline.dl.downloads_dir).iterdir()) == []


async def test_readded_video_never_waits_on_a_dying_download(h):
    """İptal edilen indirme hemen bitmez (gerçekte yt-dlp ağacı öldürülür); bu arada
    aynı video yeniden eklenirse ölmekte olan göreve bağlanıp sonsuza dek 'loading' kalmamalı."""
    started = asyncio.Event()
    original = h.dl.download

    async def slow_to_cancel(video_id):
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            for _ in range(5):
                await asyncio.sleep(0)
            raise

    h.dl.download = slow_to_cancel
    await h.player.add("song-a", "Vedat")
    await started.wait()
    await h.player.stop()  # indirme iptal edildi ama henüz bitmedi
    h.dl.download = original
    await h.player.add("song-a", "Ali")
    await playing(h, "song-a")
    assert h.player.current.added_by == "Ali"


async def test_download_finishing_after_removal_is_deleted(offline):
    gate, started = asyncio.Event(), asyncio.Event()

    async def uncancellable(video_id):
        started.set()
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await gate.wait()  # iptale rağmen tamamlanan indirme
        return str(write_wav(offline.dl.path_for(video_id)))

    offline.dl.download = uncancellable
    [track] = await offline.player.add("song-a", "Vedat")
    await started.wait()
    await offline.player.remove(track.id, force=True)
    gate.set()
    await wait_until(lambda: offline.dl.cleaned == [str(offline.dl.path_for("song-a"))])
    assert not offline.dl.path_for("song-a").exists()


# ──────────────────────────────────────────────────────────────
#  I4 — bağlantı kopması / geri gelmesi
# ──────────────────────────────────────────────────────────────

async def test_disconnect_pauses_and_reconnect_resumes_from_position(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    h.player._last_progress = 0.0
    await h.bot.report_progress(42.0)
    await h.bot.disconnect("Toplantıdan çıkarıldı")
    await wait_until(lambda: h.player.state == "paused")
    assert h.player.position == 42.0
    assert h.rec.last("bot") == {"type": "bot", "status": "disconnected", "meet_link": MEET_LINK,
                                 "detail": "Toplantıdan çıkarıldı"}
    await h.bot.connect()
    await playing(h, "song-a")
    assert h.bot.play_calls[-1][2] == 42.0


async def test_resume_after_disconnect_is_rejected_until_connected(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    await h.bot.disconnect()
    await wait_until(lambda: h.player.state == "paused")
    with pytest.raises(PlayerError, match="Bot toplantıda değil"):
        await h.player.resume()


async def test_bot_status_is_published_without_waiting_for_the_player_lock(h):
    h.bot.play_gate = asyncio.Event()
    await h.player.add("song-a", "Vedat")
    await wait_until(lambda: len(h.bot.play_calls) == 1)  # bot.play kilit altında sürüyor
    await h.bot.disconnect("Toplantı sona erdi")
    assert h.rec.last("bot") == {"type": "bot", "status": "disconnected", "meet_link": MEET_LINK,
                                 "detail": "Toplantı sona erdi"}
    assert h.player.snapshot()["bot"]["status"] == "disconnected"
    assert h.player.state == "loading"
    h.bot.play_gate.set()  # yükleme bağlantı koptuktan sonra biter → BotNotReady
    await wait_until(lambda: h.player.state == "paused")
    assert h.player.current.video_id == "song-a" and h.bot.token is None


async def test_stale_connected_event_does_not_start_playback(offline):
    await offline.player.add("song-a", "Vedat")
    await offline.player.on_bot_status("connected", None)  # kilit beklenirken bot yine koptu
    await settle(60)
    assert offline.bot.play_calls == [] and offline.player.state == "idle"
    assert queue_titles(offline.player) == ["song-a"]


async def test_user_pause_survives_reconnect_and_meeting_switch(h):
    """Bulgu: kullanıcı duraklattıktan sonra bot yeniden bağlanınca (ya da başka toplantıya
    geçince) müzik kendiliğinden çalmaya başlıyordu."""
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    h.player._last_progress = 0.0
    await h.bot.report_progress(12.0)
    await h.player.pause()
    assert h.player.state == "paused" and h.bot.paused
    await h.bot.disconnect("Meet sekmesi çöktü")
    await wait_until(lambda: h.player._active_token is None)
    await h.bot.connect()
    await settle(60)
    assert h.player.state == "paused" and len(h.bot.play_calls) == 1
    # Başka bir toplantıya geçiş (join_meet) de duraklatmayı bozmaz
    h.bot.request_join("https://meet.google.com/ddd-eeee-fff")
    await wait_until(lambda: h.bot.status == "connected")
    await settle(60)
    assert h.player.state == "paused" and len(h.bot.play_calls) == 1 and h.player.position == 12.0
    # Kullanıcı devam deyince kaldığı yerden yüklenir
    await h.player.resume()
    await playing(h, "song-a")
    assert h.bot.play_calls[-1][2] == 12.0


async def test_pause_while_auto_paused_by_disconnect_keeps_it_paused_after_reconnect(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    await h.bot.disconnect()
    await wait_until(lambda: h.player.state == "paused")
    await h.player.pause()  # bot yokken kullanıcı da "duraklat" dedi
    await h.bot.connect()
    await settle(60)
    assert h.player.state == "paused" and len(h.bot.play_calls) == 1


async def test_connecting_during_load_pauses_and_discards_inflight_play(h):
    gate = h.dl.download_gates["song-a"] = asyncio.Event()
    await h.player.add("song-a", "Vedat")
    h.bot.status = "connecting"
    await h.player.on_bot_status("connecting", "Başka toplantıya geçiliyor")
    await wait_until(lambda: h.player.state == "paused")
    gate.set()
    await settle(60)
    assert h.bot.play_calls == []
    await h.bot.connect()
    await playing(h, "song-a")


# ──────────────────────────────────────────────────────────────
#  I5 — tekrar modları
# ──────────────────────────────────────────────────────────────

async def test_repeat_one_replays_same_track(h):
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-b", "Vedat")
    await playing(h, "song-a")
    await h.player.set_repeat("one")
    first_token = await h.bot.finish()
    await wait_until(lambda: len(h.bot.play_calls) == 2 and h.player.state == "playing")
    assert h.player.current.video_id == "song-a"
    assert h.bot.token != first_token
    assert queue_titles(h.player) == ["song-b"] and list(h.player.history) == []
    assert h.rec.last("playback")["repeat"] == "one"


async def test_repeat_all_requeues_finished_track_with_new_id(h):
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-b", "Vedat")
    await playing(h, "song-a")
    track_a = h.player.current
    old_id = track_a.id
    await h.player.set_repeat("all")
    await h.bot.finish()
    await playing(h, "song-b")
    assert h.player.queue == [track_a] and track_a.id > old_id
    assert h.dl.path_for("song-a").exists()  # kuyrukta olduğu için dosyası duruyor
    await h.bot.finish()
    await playing(h, "song-a")
    assert list(h.player.history) == []


async def test_repeat_off_moves_finished_track_to_history(h, settings):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    await h.bot.finish()
    await wait_until(lambda: h.player.state == "idle")
    assert [t.video_id for t in h.player.history] == ["song-a"]
    assert h.rec.last("history")["history"][0]["video_id"] == "song-a"
    assert h.rec.last("playback")["current"] is None


async def test_invalid_repeat_mode(h):
    with pytest.raises(PlayerError):
        await h.player.set_repeat("forever")


# ──────────────────────────────────────────────────────────────
#  I6 — dosyalar, ön indirme, indirme hataları
# ──────────────────────────────────────────────────────────────

async def test_file_deleted_when_no_track_references_it(h):
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-a", "Ali")  # aynı video iki kez
    await playing(h, "song-a")
    path = h.dl.path_for("song-a")
    assert h.dl.download_calls == ["song-a"]  # paylaşılan tek indirme
    await h.bot.finish()
    await wait_until(lambda: len(h.bot.play_calls) == 2 and h.player.state == "playing")
    assert path.exists()  # ikinci kopya hâlâ kullanıyor
    await h.bot.finish()
    await wait_until(lambda: h.player.state == "idle")
    assert not path.exists()  # geçmiş dosya tutmaz
    assert len(h.player.history) == 2


async def test_prefetch_downloads_only_the_next_n(offline, settings):
    settings.prefetch = 2
    for i in range(5):
        await offline.player.add(f"song-{i}0", "Vedat")
    await wait_until(lambda: len(offline.dl.completed) == 2)
    await settle()
    assert offline.dl.download_calls == ["song-00", "song-10"]
    assert [t.status for t in offline.player.queue] == ["ready", "ready", "pending", "pending", "pending"]


async def test_current_track_download_starts_before_prefetches(h, settings):
    """Bulgu: listenin ilk şarkısının indirmesi, sonraki şarkıların ön-indirmelerinin ARKASINDA
    başlıyordu (iki indirme yuvası dolu → ilk ses saniyelerce gecikiyordu)."""
    settings.prefetch = 2
    h.dl.catalog["liste"] = [make_info("song-a"), make_info("song-b"), make_info("song-c")]
    await h.player.add("liste", "Vedat")
    await playing(h, "song-a")
    assert h.dl.download_calls[:3] == ["song-a", "song-b", "song-c"]
    assert h.dl.prioritized == ["song-a"]


async def test_play_now_prioritizes_a_track_outside_the_prefetch_window(h, settings):
    settings.prefetch = 2
    for vid in ("song-a", "song-b", "song-c", "song-d"):
        await h.player.add(vid, "Vedat")
    await playing(h, "song-a")
    await h.player.play_now(h.player.queue[2].id)  # song-d ön-indirme penceresinin dışında
    await playing(h, "song-d")
    assert h.dl.prioritized[-1] == "song-d"


async def test_downloader_without_prioritize_still_works(h):
    """prioritize isteğe bağlıdır: yalnızca resolve/download/cleanup sunan indirici de çalışır."""
    h.player.downloader = SimpleNamespace(resolve=h.dl.resolve, download=h.dl.download, cleanup=h.dl.cleanup)
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")


async def test_download_error_marks_track_and_it_is_skipped_with_notice(h):
    h.dl.download_errors["song-b"] = DownloadError("Video kullanılamıyor")
    gate = h.dl.download_gates["song-a"] = asyncio.Event()
    for vid in ("song-a", "song-b", "song-c"):
        await h.player.add(vid, "Vedat")
    await wait_until(lambda: h.player.queue[0].status == "error")
    assert h.player.queue[0].error == "Video kullanılamıyor"
    assert h.rec.last("queue")["queue"][0]["status"] == "error"
    gate.set()
    await playing(h, "song-a")
    await h.bot.finish()
    await playing(h, "song-c")
    assert any(n["message"] == "⚠️ Şarkı song-b indirilemedi, atlandı" for n in h.rec.of("notice"))


async def test_current_download_error_advances(h):
    h.dl.download_errors["song-a"] = DownloadError("İndirme zaman aşımına uğradı")
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-b", "Vedat")
    await playing(h, "song-b")
    assert any("Şarkı song-a indirilemedi" in n["message"] for n in h.rec.of("notice"))


async def test_unexpected_downloader_exception_is_contained(h):
    h.dl.download_errors["song-a"] = RuntimeError("beklenmedik")
    await h.player.add("song-a", "Vedat")
    await wait_until(lambda: h.player.state == "idle")
    assert any("indirilemedi" in n["message"] for n in h.rec.of("notice"))


# ──────────────────────────────────────────────────────────────
#  Kuyruk komutları
# ──────────────────────────────────────────────────────────────

async def test_move_clamps_index_and_rejects_unknown_ids(offline):
    for vid in ("song-a", "song-b", "song-c"):
        await offline.player.add(vid, "Vedat")
    c = offline.player.queue[2]
    await offline.player.move(c.id, -5)
    assert queue_titles(offline.player) == ["song-c", "song-a", "song-b"]
    await offline.player.move(c.id, 999)
    assert queue_titles(offline.player) == ["song-a", "song-b", "song-c"]
    with pytest.raises(PlayerError, match="bulunamadı"):
        await offline.player.move(12345, 0)


async def test_remove_requires_ownership_unless_forced(offline):
    [mine] = await offline.player.add("song-a", "Vedat")
    [other] = await offline.player.add("song-b", "Ali")
    with pytest.raises(PlayerError, match="kendi eklediğin"):
        await offline.player.remove(other.id, requested_by="Vedat")
    assert (await offline.player.remove(mine.id, requested_by="Vedat")) is mine
    await offline.player.remove(other.id, requested_by="Vedat", force=True)
    assert offline.player.queue == []


async def test_play_now_moves_to_front_and_skips_current(h):
    for vid in ("song-a", "song-b", "song-c", "song-d"):
        await h.player.add(vid, "Vedat")
    await playing(h, "song-a")
    target = h.player.queue[2]
    await h.player.play_now(target.id)
    await playing(h, "song-d")
    assert queue_titles(h.player) == ["song-b", "song-c"]
    assert [t.video_id for t in h.player.history] == ["song-a"]


async def test_play_now_requires_bot(offline):
    [track] = await offline.player.add("song-a", "Vedat")
    with pytest.raises(PlayerError, match="Bot toplantıda değil"):
        await offline.player.play_now(track.id)


async def test_shuffle_keeps_the_same_tracks(offline):
    for i in range(8):
        await offline.player.add(f"song-{i}0", "Vedat")
    before = {t.id for t in offline.player.queue}
    await offline.player.shuffle()
    assert {t.id for t in offline.player.queue} == before


async def test_skip_and_pause_when_idle_are_errors(h):
    with pytest.raises(PlayerError, match="çalan bir şarkı yok"):
        await h.player.skip()
    with pytest.raises(PlayerError, match="çalan bir şarkı yok"):
        await h.player.pause()
    with pytest.raises(PlayerError, match="Kuyruk boş"):
        await h.player.resume()
    await h.player.stop()  # boşta durdurmak zararsız


# ──────────────────────────────────────────────────────────────
#  Oynatma kontrolleri
# ──────────────────────────────────────────────────────────────

async def test_pause_resume_and_seek(h):
    await h.player.add("song-a", "Vedat")
    await playing(h, "song-a")
    await h.player.pause()
    assert h.player.state == "paused" and h.bot.paused
    await h.player.seek(0.5)
    assert h.bot.position == 0.5 and h.rec.last("progress")["position"] == 0.5
    await h.player.seek(10_000)
    assert h.player.position == h.player.duration  # süreye kısıtlanır
    await h.player.resume()
    assert h.player.state == "playing" and not h.bot.paused
    assert len(h.bot.play_calls) == 1
    for bad in (float("nan"), "5", True):
        with pytest.raises(PlayerError, match="Geçersiz konum"):
            await h.player.seek(bad)


async def test_stop_moves_played_track_to_history_and_goes_idle(h):
    await h.player.add("song-a", "Vedat")
    await h.player.add("song-b", "Vedat")
    await playing(h, "song-a")
    await h.player.stop()
    assert h.player.state == "idle" and h.player.current is None
    assert h.bot.token is None and h.bot.stop_calls == 1
    assert queue_titles(h.player) == ["song-b"]
    assert [t.video_id for t in h.player.history] == ["song-a"]


async def test_volume_validation_storage_and_reapply_after_join(offline):
    for bad in (-1, 101, 5.5, True, "50"):
        with pytest.raises(PlayerError):
            await offline.player.set_volume("music", bad)
    with pytest.raises(PlayerError):
        await offline.player.set_volume("bass", 50)
    await offline.player.set_volume("music", 20)
    await offline.player.set_volume("mic", 35)
    await offline.player.set_mic_muted(True)
    assert offline.rec.last("volume") == {"type": "volume", "music": 20, "mic": 35}
    assert offline.rec.last("mic") == {"type": "mic", "muted": True}
    # Yeni sayfa varsayılanlarla açılır; bağlanınca son değerler yeniden uygulanmalı
    offline.bot.music_volume, offline.bot.mic_volume, offline.bot.mic_muted = 80, 80, False
    await offline.bot.connect()
    await wait_until(lambda: offline.bot.music_volume == 20)
    assert offline.bot.mic_volume == 35 and offline.bot.mic_muted is True


async def test_reapply_after_join_continues_when_one_setting_fails(offline):
    await offline.player.set_volume("mic", 35)
    await offline.player.set_mic_muted(True)
    offline.bot.mic_volume, offline.bot.mic_muted = 80, False

    async def broken(value):
        raise BotError("Tarayıcı komutu başarısız oldu")

    offline.bot.set_music_volume = broken
    await offline.bot.connect()
    await wait_until(lambda: offline.bot.mic_muted is True)
    assert offline.bot.mic_volume == 35


async def test_mic_mute_uses_state_reported_by_bot(h):
    async def stuck_unmuted(muted):
        return False

    h.bot.set_mic_muted = stuck_unmuted
    await h.player.set_mic_muted(True)
    assert h.player.mic_muted is False
    with pytest.raises(PlayerError):
        await h.player.set_mic_muted("evet")


async def test_shutdown_cancels_pending_downloads(offline):
    offline.dl.download_gates["song-a"] = asyncio.Event()
    await offline.player.add("song-a", "Vedat")
    await wait_until(lambda: offline.dl.download_calls == ["song-a"])
    task = offline.player._downloads["song-a"]
    await offline.player.shutdown()
    assert task.cancelled()
    await offline.player.on_bot_status("connected", None)  # kapandıktan sonra olaylar yok sayılır
    assert offline.player._tasks == set()


# ──────────────────────────────────────────────────────────────
#  FakeBot (gerçek saatle, hızlandırılmış)
# ──────────────────────────────────────────────────────────────

def test_wav_duration(tmp_path):
    wav = write_wav(tmp_path / "a.wav", seconds=2.5)
    assert wav_duration(str(wav)) == pytest.approx(2.5)
    other = tmp_path / "a.webm"
    other.write_bytes(b"\x1aE\xdf\xa3 not a wav")
    assert wav_duration(str(other)) is None
    assert wav_duration(str(tmp_path / "missing.wav")) is None


async def test_fakebot_join_play_and_clock_through_whole_queue(settings):
    bot = FakeBot(settings, speed=100, tick=0.01, join_delay=0.01)
    downloader = FakeDownloader(settings.downloads_dir, wav_seconds=1.0)
    recorder = Recorder()
    player = Player(settings, bot, downloader, recorder)
    wire(player, bot)
    try:
        await player.add("song-a", "Vedat")
        await player.add("song-b", "Vedat")
        bot.request_join(MEET_LINK)
        await wait_until(lambda: [m["status"] for m in recorder.of("bot")] == ["connecting", "connected"])
        await wait_until(lambda: len(player.history) == 2 and player.state == "idle", timeout=5)
        assert [t.video_id for t in player.history] == ["song-b", "song-a"]
        assert list(Path(settings.downloads_dir).iterdir()) == []
        await bot.leave()
        await wait_until(lambda: recorder.last("bot")["status"] == "disconnected")
        assert recorder.last("bot")["detail"] == "Toplantıdan ayrıldı"
    finally:
        await player.shutdown()
        await bot.shutdown()


async def test_fakebot_join_and_leave_semantics_match_the_real_bot(settings):
    bot = FakeBot(settings, join_delay=0.05)
    events: list[tuple[str, str | None]] = []

    async def on_status(status, detail):
        events.append((status, detail))

    bot.on_status = on_status
    await bot.leave()  # zaten ayrı → olay yok
    assert events == []
    bot.request_join(MEET_LINK)
    assert (bot.status, bot.meet_link) == ("connecting", MEET_LINK)  # olay beklenmeden hemen
    await wait_until(lambda: events == [("connecting", "Toplantıya bağlanılıyor (sahte bot)")])
    join_task = bot._join_task
    bot.request_join(MEET_LINK)  # aynı toplantı → yeni katılım başlamaz
    assert bot._join_task is join_task
    await bot.leave()  # katılım sürerken → iptal
    assert events[-1] == ("disconnected", "Katılma iptal edildi")
    await asyncio.sleep(0.1)
    assert [status for status, _ in events] == ["connecting", "disconnected"]  # asla "connected"
    await bot.shutdown()


async def test_fakebot_join_arriving_during_leave_wins(settings):
    """Bulgu: leave() iptal edilen katılımı beklerken gelen request_join'in durumunu eziyordu →
    sonuç 'connected' ama meet_link=None, üstelik bayat bir 'disconnected' olayı."""
    other = "https://meet.google.com/ddd-eeee-fff"
    bot = FakeBot(settings, join_delay=0.05)
    events: list[tuple[str, str | None]] = []

    async def on_status(status, detail):
        events.append((status, bot.meet_link))

    bot.on_status = on_status
    try:
        bot.request_join(MEET_LINK)
        await wait_until(lambda: events == [("connecting", MEET_LINK)])
        leave = asyncio.ensure_future(bot.leave())
        await asyncio.sleep(0)       # leave, iptal edilen katılımın bitmesini bekliyor
        bot.request_join(other)      # tam o sırada başka bir yönetici yeni toplantıya gönderdi
        await leave
        await wait_until(lambda: bot.status == "connected")
        assert bot.meet_link == other
        assert [status for status, _ in events] == ["connecting", "connecting", "connected"]
        assert events[-1] == ("connected", other)
    finally:
        await bot.shutdown()
    assert bot.status == "disconnected" and bot._join_task is None


async def test_fakebot_contract(settings, tmp_path):
    bot = FakeBot(settings, speed=1, tick=0.01, join_delay=0)
    ended: list[int] = []

    async def on_ended(token):
        ended.append(token)

    bot.on_track_ended = on_ended
    wav = write_wav(tmp_path / "x.wav", seconds=0.02)
    with pytest.raises(BotNotReady):
        await bot.play(str(wav), 1)
    bot.request_join(MEET_LINK)
    await wait_until(lambda: bot.is_connected)
    with pytest.raises(BotError, match="bulunamadı"):
        await bot.play(str(tmp_path / "yok.wav"), 1)
    other = tmp_path / "y.bin"
    other.write_bytes(b"not audio")
    assert await bot.play(str(other), 2) == 30.0  # WAV değil → varsayılan süre
    assert await bot.play(str(wav), 3) == pytest.approx(0.02)  # 2 numaralı jeton geçersiz kalır
    await wait_until(lambda: ended == [3])
    await settle()
    assert ended == [3]
    assert await bot.set_mic_muted(True) is True
    await bot.shutdown()
    assert bot.status == "disconnected"
