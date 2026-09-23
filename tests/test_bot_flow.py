# ──────────────────────────────────────────────────────────────
#  bot.py — katılım akışı, izleme, ses komutları ve Chrome yardımcıları
#
#  Tarayıcı testleri gerçek Chrome'u CDP ile BAŞLATMAZ: MeetBot'un
#  _ensure_context() dikişi, https://meet.google.com/** isteklerini
#  tests/mock_meet/meet.html'e yönlendiren bir Playwright bağlamı döndürür.
# ──────────────────────────────────────────────────────────────

import asyncio
import http.server
import inspect
import json
import logging
import os
import socket
import subprocess
import sys
import threading
import wave
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

import pytest
from playwright.async_api import Error as PlaywrightError

import bot
from bot import (
    BotError,
    BotNotReady,
    JoinDenied,
    MeetBot,
    PlaybackCancelled,
    audio_mime_type,
    build_chrome_args,
    cdp_version,
    clamp_volume,
    classify_meet_text,
    find_chrome,
    kill_process_tree,
    parse_toggle_state,
)
from create_silence import create_silence_wav

MOCK_HTML = (Path(__file__).parent / "mock_meet" / "meet.html").read_text(encoding="utf-8")
CODE = "abc-defg-hij"
OTHER_CODE = "xyz-wxyz-xyz"
LINK = f"https://meet.google.com/{CODE}"
OTHER_LINK = f"https://meet.google.com/{OTHER_CODE}"

browser = pytest.mark.browser


# ──────────────────────────────────────────────────────────────
#  Yardımcılar
# ──────────────────────────────────────────────────────────────

class MockMeet:
    """https://meet.google.com/<kod> → meet.html + senaryo; gezinme ve sayfa olaylarını kaydeder."""

    def __init__(self):
        self.scenarios = {}
        self.navigations = []
        self.events = []
        self.fail_next = 0  # sıradaki N gezinmeyi ağ hatasıyla düşür

    async def handle(self, route):
        request = route.request
        if request.resource_type != "document":
            await route.fulfill(status=404, body="")
            return
        code = urlparse(request.url).path.strip("/")
        self.navigations.append(code)
        if self.fail_next:
            self.fail_next -= 1
            await route.abort("connectionfailed")
            return
        html = MOCK_HTML.replace("__SCENARIO__", json.dumps(self.scenarios.get(code, {})))
        await route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

    def record(self, name, data):
        self.events.append((name, data))

    def names(self):
        return [name for name, _ in self.events]

    def first(self, name):
        return next(data for event, data in self.events if event == name)


class Recorder:
    def __init__(self, meet_bot):
        self.statuses = []
        self.ended = []
        self.progress = []
        meet_bot.on_status = self.on_status
        meet_bot.on_track_ended = self.on_track_ended
        meet_bot.on_progress = self.on_progress

    async def on_status(self, status, detail):
        self.statuses.append((status, detail))

    async def on_track_ended(self, token):
        self.ended.append(token)

    async def on_progress(self, token, position, duration, paused):
        self.progress.append((token, position, duration, paused))

    def last(self):
        return self.statuses[-1] if self.statuses else (None, None)


class PageBot(MeetBot):
    """Chrome/CDP yerine verilen Playwright bağlamını kullanan, hızlandırılmış MeetBot."""

    MONITOR_INTERVAL = 0.1
    OUT_OF_CALL_LIMIT = 3
    PREJOIN_TIMEOUT = 10.0
    POLL_INTERVAL = 0.05
    JOIN_RETRY_DELAY = 0.05

    def __init__(self, settings, context):
        super().__init__(settings)
        self.test_context = context

    async def _ensure_context(self, announce=True):
        return self.test_context

    @property
    def page(self):
        return self._page


async def eventually(predicate, timeout=10.0, interval=0.05, what="koşul"):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        result = predicate()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        if loop.time() >= deadline:
            raise AssertionError(f"{what} {timeout} sn içinde sağlanmadı")
        await asyncio.sleep(interval)


async def wait_status(rec, status, timeout=15.0):
    await eventually(lambda: rec.last()[0] == status, timeout, what=f"durum={status} (son: {rec.last()})")
    return rec.last()[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def meet(chromium, settings):
    context = await chromium.new_context()
    mock = MockMeet()
    await context.expose_function("__mockEvent", mock.record)
    await context.route("https://meet.google.com/**", mock.handle)
    meet_bot = PageBot(settings, context)
    rec = Recorder(meet_bot)
    yield SimpleNamespace(bot=meet_bot, mock=mock, rec=rec, context=context, settings=settings)
    await meet_bot.shutdown()
    await context.close()


async def join(meet, scenario=None, link=LINK):
    meet.mock.scenarios[urlparse(link).path.strip("/")] = {"admission": "direct", "popup": False, **(scenario or {})}
    meet.bot.request_join(link)
    detail = await wait_status(meet.rec, "connected")
    assert detail == "Toplantıya katıldı"
    return meet.bot.page


async def mock_state(page, expression):
    return await page.evaluate(f"() => ({expression})")


# ──────────────────────────────────────────────────────────────
#  Katılım akışı
# ──────────────────────────────────────────────────────────────

@browser
async def test_anonymous_join_fills_name_turns_camera_off_and_waits_for_the_host(meet):
    # Katılma ekranı 1.5 sn geç çizilir; kabul, sahibi onaylayana kadar bekler.
    meet.mock.scenarios[CODE] = {"prejoinDelayMs": 1500, "admission": "manual"}
    meet.bot.request_join(LINK)
    assert meet.bot.status == "connecting" and meet.bot.meet_link == LINK

    await eventually(lambda: "join_click" in meet.mock.names(), what="katılma tıklaması")
    assert meet.mock.first("join_click") == {"name": "MeetBot", "camMuted": True, "micMuted": False}

    # Bekleme ekranında mikrofon/kamera/"More options" görünür; bot yine de BAĞLANDI DEMEMELİ.
    await asyncio.sleep(1.0)
    assert meet.bot.status == "connecting"
    assert meet.rec.last() == ("connecting", "Katılma isteği gönderildi, onay bekleniyor…")

    await meet.bot.page.evaluate("() => mock.admit()")
    await wait_status(meet.rec, "connected")
    state = await mock_state(meet.bot.page, """{
        nc: mock.noiseCancellation, adaptive: mock.adaptiveAudio,
        popup: !!document.getElementById("popup"), dialog: !!document.getElementById("settings"),
        cam: mock.cam.muted, mic: mock.mic.muted,
        callTrack: mock.callStream.getAudioTracks()[0].readyState,
        previewTrack: mock.previewStream.getAudioTracks()[0].readyState }""")
    assert state == {
        "nc": False, "adaptive": True,            # doğru anahtar kapatıldı, komşusu değil
        "popup": False, "dialog": False,          # açılır pencere ve ayarlar kapatıldı
        "cam": True, "mic": False,
        "callTrack": "live", "previewTrack": "ended",  # Meet önizlemeyi durdurdu, bot susmadı
    }
    assert [status for status, _ in meet.rec.statuses].count("connected") == 1
    assert "disconnected" not in [status for status, _ in meet.rec.statuses]
    assert meet.bot.is_connected and meet.bot.status_detail == "Toplantıya katıldı"
    assert "permission_dialog" not in meet.mock.names()


@browser
async def test_meet_permission_dialog_is_avoided_by_granting_media_access(chromium, meet):
    # Kontrol: izin verilmemiş bir bağlamda sahte Meet de gerçeği gibi izin penceresi açar...
    control = await chromium.new_context()
    try:
        await control.route("https://meet.google.com/**", meet.mock.handle)
        page = await control.new_page()
        await page.goto(LINK)
        await eventually(lambda: mock_state(page, "mock.phase === 'permission'"), what="izin penceresi")
    finally:
        await control.close()
    # ...bot ise izni önceden verdiği için pencere hiç çıkmaz.
    await join(meet)
    assert await meet.bot.page.evaluate(
        "async () => (await navigator.permissions.query({ name: 'microphone' })).state") == "granted"


@browser
@pytest.mark.parametrize("lang", ["tr", "en"])
async def test_studio_sound_filter_is_switched_off(meet, lang):
    # 2026 Meet (oturum açık hesap): "Ses Ayarları" sekmesinde "Stüdyo ses kalitesi" — konuşma olmayan
    # sesleri (müziği!) süzer. Eski ad "Gürültü giderme"ydi; bot yenisini de kapatmalı, komşusuna dokunmamalı.
    page = await join(meet, {"lang": lang, "anonymous": False, "studioNaming": True})
    state = await mock_state(page, "{ nc: mock.noiseCancellation, ptt: !!mock.pushToTalk }")
    assert state == {"nc": False, "ptt": False}
    assert meet.mock.first("nc") == {"on": False}


@browser
async def test_turkish_ui_with_label_only_toggles(meet):
    page = await join(meet, {"lang": "tr", "anonymous": False, "toggleAttrs": False, "popup": True})
    assert ("connecting", "Toplantıya giriliyor…") in meet.rec.statuses
    assert meet.mock.first("join_click") == {"name": None, "camMuted": True, "micMuted": False}
    state = await mock_state(page, """{ nc: mock.noiseCancellation, adaptive: mock.adaptiveAudio,
                                       popup: !!document.getElementById("popup"), cam: mock.cam.muted }""")
    assert state == {"nc": False, "adaptive": True, "popup": False, "cam": True}

    assert await meet.bot.set_mic_muted(True) is True
    assert await page.evaluate("() => mock.micLabel()") == "Mikrofonu aç (ctrl + d)"
    assert await meet.bot.set_mic_muted(False) is False
    assert await page.evaluate("() => mock.micLabel()") == "Mikrofonu kapat (ctrl + d)"


@browser
@pytest.mark.parametrize("lang", ["en", "tr"])
async def test_denied_request_is_reported_and_not_retried(meet, lang):
    meet.mock.scenarios[CODE] = {"lang": lang, "admission": "deny", "admitAfterMs": 200}
    meet.bot.request_join(LINK)
    detail = await wait_status(meet.rec, "disconnected")
    assert detail == "Toplantı sahibi katılma isteğini reddetti"
    assert meet.mock.navigations == [CODE]  # ret tekrar denenmez
    assert "connected" not in [status for status, _ in meet.rec.statuses]
    assert meet.bot.meet_link is None and meet.bot.status_detail == detail
    assert meet.bot.page.url == "about:blank"


@browser
@pytest.mark.parametrize("scenario, expected", [
    ({"admission": "noresponse"}, "Katılma isteğine kimse yanıt vermedi"),
    ({"invalidCode": True}, "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),
    ({"lang": "tr", "invalidCode": True}, "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),
])
async def test_meet_error_screens_end_the_join(meet, scenario, expected):
    meet.mock.scenarios[CODE] = {"admitAfterMs": 200, **scenario}
    meet.bot.request_join(LINK)
    assert await wait_status(meet.rec, "disconnected") == expected
    assert meet.mock.navigations == [CODE]


@browser
async def test_admission_timeout(meet):
    meet.settings.join_timeout = 1
    meet.mock.scenarios[CODE] = {"admission": "manual"}
    meet.bot.request_join(LINK)
    assert await wait_status(meet.rec, "disconnected") == "Toplantıya 1 sn içinde kabul edilmedi"
    assert meet.bot.page.url == "about:blank"  # bekleme odasında kalmadı


@browser
async def test_join_flow_can_run_directly_on_a_page(meet):
    meet.mock.scenarios[CODE] = {"admission": "deny", "admitAfterMs": 100}
    page = await meet.context.new_page()
    with pytest.raises(JoinDenied, match="reddetti"):
        await meet.bot._join_on_page(page, LINK)
    assert meet.bot.status == "connecting"  # durum geçişleri çağırana (katılım görevine) ait


@browser
async def test_navigation_error_is_retried(meet):
    meet.mock.fail_next = 1
    await join(meet)
    assert meet.mock.navigations == [CODE, CODE]
    assert ("connecting", "Tekrar deneniyor (2/3)…") in meet.rec.statuses


@browser
async def test_navigation_errors_give_up_after_three_attempts(meet):
    meet.mock.fail_next = 5
    meet.bot.request_join(LINK)
    detail = await wait_status(meet.rec, "disconnected")
    assert detail == "Meet sayfası açılamadı (internet bağlantısını kontrol edin)"
    assert meet.mock.navigations == [CODE] * 3


@browser
async def test_browser_failure_while_opening_the_tab_is_retried(meet):
    # Tarayıcı tam sekme açılırken kapandı: sonraki deneme yeniden bağlanır (beklenmeyen hata değil).
    real_ensure_context = meet.bot._ensure_context
    failures = [PlaywrightError("Target page, context or browser has been closed")]

    async def flaky_ensure_context():
        if failures:
            raise failures.pop()
        return await real_ensure_context()

    meet.bot._ensure_context = flaky_ensure_context
    await join(meet)
    assert ("connecting", "Tekrar deneniyor (2/3)…") in meet.rec.statuses
    assert meet.mock.navigations == [CODE]


@browser
async def test_prejoin_screen_that_never_appears_is_not_retried(meet):
    meet.bot.PREJOIN_TIMEOUT = 1.0
    meet.mock.scenarios[CODE] = {"prejoinDelayMs": 60_000}
    meet.bot.request_join(LINK)
    assert await wait_status(meet.rec, "disconnected") == "Meet katılma ekranı açılmadı (katılma düğmesi bulunamadı)"
    assert meet.mock.navigations == [CODE]
    assert meet.bot.page.url == "about:blank"


@browser
@pytest.mark.parametrize("scenario", [
    {"joinLabel": "Join the call now"},                          # bekleme odalı toplantı
    {"joinLabel": "Join anyway"},
    {"joinLabel": "Ask to join anyway"},
    {"joinLabel": "Join as a viewer"},
    {"joinLabel": "Ask to join", "joinAriaLabel": "Ask to join without microphone"},   # cihaz sorunu
    {"joinLabel": "Join now", "joinAriaLabel": "Join now without camera", "anonymous": False},
    {"lang": "tr", "joinLabel": "Yine de katılma isteği gönder"},
])
async def test_meets_other_join_button_labels_are_recognized(meet, scenario):
    # Meet birincil düğmenin adını ~30 varyanttan üretir; eskiden yalnızca 6 tam etiket eşleşiyordu.
    meet.bot.PREJOIN_TIMEOUT = 3.0
    await join(meet, scenario)
    names = meet.mock.names()
    assert names.count("join_click") == 1
    assert not {"companion", "phone_audio", "other_ways"} & set(names)


@browser
@pytest.mark.parametrize("lang", ["en", "tr"])
async def test_switch_here_is_never_clicked_join_here_too_is_used_instead(meet, lang):
    # Bot profilindeki hesap (ör. operatörün kendisi) toplantıda: "Switch here" onun oturumunu taşırdı.
    await join(meet, {"lang": lang, "anonymous": False, "switchHere": True})
    names = meet.mock.names()
    assert "switch_here" not in names
    assert meet.mock.first("other_ways") == {"expanded": True}      # kapalı bölüm açıldı
    assert names.count("join_click") == 1                            # "Join here too / Burada da katıl"
    assert not {"companion", "phone_audio"} & set(names)
    assert ("connecting", "Toplantıya giriliyor…") in meet.rec.statuses


@browser
async def test_switch_here_without_join_here_too_names_the_real_cause(meet, caplog):
    meet.bot.PREJOIN_TIMEOUT = 1.0
    meet.mock.scenarios[CODE] = {"anonymous": False, "switchHere": True, "otherWays": False}
    with caplog.at_level(logging.WARNING, logger="meetbot.bot"):
        meet.bot.request_join(LINK)
        detail = await wait_status(meet.rec, "disconnected")
    assert "toplantıda zaten var" in detail and "Buraya geç" in detail
    assert "switch_here" not in meet.mock.names()
    assert "sayfadaki düğmeler" in caplog.text and "Switch here" in caplog.text
    assert meet.mock.navigations == [CODE]


@browser
async def test_unrecognized_join_button_is_named_in_the_error(meet, caplog):
    meet.bot.PREJOIN_TIMEOUT = 1.0
    meet.mock.scenarios[CODE] = {"anonymous": False, "joinLabel": "Rejoindre la réunion"}
    with caplog.at_level(logging.WARNING, logger="meetbot.bot"):
        meet.bot.request_join(LINK)
        detail = await wait_status(meet.rec, "disconnected")
    assert detail == "Meet katılma düğmesi tanınmadı ('Rejoindre la réunion')"
    assert "sayfadaki düğmeler: " in caplog.text and "| Rejoindre la réunion |" in caplog.text
    assert "join_click" not in meet.mock.names()


# ── Ayrılma / iptal / bağlantı değiştirme ─────────────────────

@browser
async def test_leave_during_connecting_cancels_without_rejoin(meet):
    meet.mock.scenarios[CODE] = {"admission": "manual"}
    meet.bot.request_join(LINK)
    await eventually(lambda: "join_click" in meet.mock.names(), what="katılma tıklaması")

    await meet.bot.leave()
    assert meet.rec.last() == ("disconnected", "Katılma iptal edildi")
    assert meet.bot.status == "disconnected" and meet.bot.meet_link is None
    assert meet.bot.page.url == "about:blank"

    statuses, navigations = list(meet.rec.statuses), list(meet.mock.navigations)
    await asyncio.sleep(1.0)
    assert meet.rec.statuses == statuses      # yeniden katılma yok
    assert meet.mock.navigations == navigations == [CODE]


@browser
async def test_leave_clicks_leave_call_and_blanks_the_tab(meet, tone_wav):
    await join(meet)
    await meet.bot.leave()
    assert "left" in meet.mock.names()
    assert meet.rec.last() == ("disconnected", "Toplantıdan ayrıldı")
    assert meet.bot.page.url == "about:blank"
    with pytest.raises(BotNotReady):
        await meet.bot.play(str(tone_wav(0.5)), token=1)
    statuses = list(meet.rec.statuses)
    await meet.bot.leave()  # ikinci kez: yeni olay yok
    assert meet.rec.statuses == statuses


@browser
async def test_joining_another_link_leaves_the_current_call_first(meet):
    await join(meet)
    meet.mock.scenarios[OTHER_CODE] = {"admission": "direct", "popup": False}
    meet.bot.request_join(OTHER_LINK)
    await eventually(lambda: meet.bot.meet_link == OTHER_LINK and meet.bot.is_connected, what="ikinci toplantı")
    names = meet.mock.names()
    assert names.index("left") < len(names) - 1 - names[::-1].index("join_click")  # önce ayrıldı, sonra katıldı
    assert meet.mock.navigations == [CODE, OTHER_CODE]
    assert [s for s, _ in meet.rec.statuses].count("connected") == 2


@browser
async def test_new_join_request_cancels_the_one_in_flight(meet):
    meet.mock.scenarios[CODE] = {"admission": "manual"}
    meet.bot.request_join(LINK)
    await eventually(lambda: "join_click" in meet.mock.names(), what="ilk katılma tıklaması")
    meet.mock.scenarios[OTHER_CODE] = {"admission": "direct", "popup": False}
    meet.bot.request_join(OTHER_LINK)
    assert meet.bot.meet_link == OTHER_LINK and meet.bot.status == "connecting"
    await wait_status(meet.rec, "connected")
    assert meet.bot.meet_link == OTHER_LINK
    assert meet.mock.navigations == [CODE, OTHER_CODE]
    assert "disconnected" not in [status for status, _ in meet.rec.statuses]


@browser
async def test_request_join_for_the_same_link_is_a_noop(meet):
    await join(meet)
    statuses = list(meet.rec.statuses)
    meet.bot.request_join(LINK + "  ")
    await asyncio.sleep(0.3)
    assert meet.rec.statuses == statuses and meet.mock.navigations == [CODE]


@browser
async def test_join_requested_while_leaving_wins(meet):
    await join(meet)
    connected_at = len(meet.rec.statuses)
    leaving = asyncio.create_task(meet.bot.leave())
    await asyncio.sleep(0)  # leave() başladı (izleme durduruluyor)
    meet.mock.scenarios[OTHER_CODE] = {"admission": "direct", "popup": False}
    meet.bot.request_join(OTHER_LINK)
    await leaving
    await wait_status(meet.rec, "connected")
    assert meet.bot.meet_link == OTHER_LINK
    later = [status for status, _ in meet.rec.statuses[connected_at:]]
    assert "disconnected" not in later  # ayrılma, yerine gelen katılımı bildirmez / bozmaz
    assert meet.mock.navigations == [CODE, OTHER_CODE] and "left" in meet.mock.names()


@browser
async def test_call_lost_while_a_new_join_waits_does_not_clobber_it(meet):
    page = await join(meet)
    connected_at = len(meet.rec.statuses)
    meet.mock.scenarios[OTHER_CODE] = {"admission": "direct", "popup": False}
    async with meet.bot._session_lock:  # yeni katılım görevi kilidi beklesin; izleme çalışmaya devam eder
        meet.bot.request_join(OTHER_LINK)
        await page.evaluate("() => mock.remove()")
        monitor = meet.bot._monitor_task
        await eventually(monitor.done, what="izleme kopmayı gördü ve durdu")  # 3 × 0.1 sn
        assert monitor.exception() is None
        assert (meet.bot.status, meet.bot.meet_link) == ("connecting", OTHER_LINK)
        assert meet.rec.statuses[connected_at:] == []
    await wait_status(meet.rec, "connected")
    assert meet.bot.meet_link == OTHER_LINK
    assert "disconnected" not in [status for status, _ in meet.rec.statuses[connected_at:]]


# ── İzleme ────────────────────────────────────────────────────

@browser
async def test_removal_after_joining_emits_disconnected(meet):
    page = await join(meet)
    await page.evaluate("() => mock.remove()")
    assert await wait_status(meet.rec, "disconnected") == "Toplantıdan çıkarıldı"
    assert meet.bot.meet_link is None and page.url == "about:blank"
    statuses = list(meet.rec.statuses)
    await asyncio.sleep(0.5)
    assert meet.rec.statuses == statuses


@browser
async def test_tab_crash_is_detected_and_the_next_join_uses_a_fresh_tab(meet):
    crashed = await join(meet)
    with pytest.raises(Exception):
        await crashed.goto("chrome://crash")
    assert await wait_status(meet.rec, "disconnected") == "Meet sekmesi çöktü"

    meet.bot.request_join(LINK)
    await wait_status(meet.rec, "connected")
    assert meet.bot.page is not crashed and crashed.is_closed()


@browser
async def test_closed_tab_is_detected(meet):
    page = await join(meet)
    await page.close()
    assert await wait_status(meet.rec, "disconnected") == "Meet sekmesi veya tarayıcı kapatıldı"


# ── Boş görüşme: "Leave empty calls" / "Are you still there?" ──

@browser
@pytest.mark.parametrize("scenario", [
    {"lang": "en", "anonymous": False},
    {"lang": "tr", "anonymous": False, "iconNames": True},   # sekme adı "settingsGenel"
])
async def test_leave_empty_calls_is_switched_off_on_the_general_tab(meet, scenario):
    page = await join(meet, scenario)
    state = await mock_state(page, """{ leaveEmpty: mock.leaveEmptyCalls, shortcuts: mock.shortcuts,
        nc: mock.noiseCancellation, adaptive: mock.adaptiveAudio, dialog: !!document.getElementById("settings") }""")
    assert state == {"leaveEmpty": False, "shortcuts": True,   # doğru anahtar, komşusu değil
                     "nc": False, "adaptive": True, "dialog": False}


@browser
async def test_settings_without_a_general_tab_still_turn_noise_cancellation_off(meet):
    page = await join(meet, {"leaveEmptyCalls": None})
    assert await mock_state(page, "{ nc: mock.noiseCancellation, dialog: !!document.getElementById('settings') }") \
        == {"nc": False, "dialog": False}


@browser
@pytest.mark.parametrize("lang", ["en", "tr"])
async def test_still_there_prompt_in_the_call_is_answered(meet, lang):
    page = await join(meet, {"lang": lang, "promptTimeoutMs": 3000})
    await page.evaluate("() => mock.stillThere()")
    await eventually(lambda: "stayed" in meet.mock.names(), what="'kal' düğmesine tıklandı")
    await asyncio.sleep(0.5)
    names = meet.mock.names()
    assert "prompt_left" not in names and "left" not in names and "prompt_timeout" not in names
    assert meet.bot.is_connected
    assert "disconnected" not in [status for status, _ in meet.rec.statuses]


@browser
async def test_unanswerable_still_there_prompt_is_reported_as_the_reason(meet):
    # "Kal" düğmesi tanınmadı → Meet botu çıkarır; neden "Meet penceresinden ayrıldı" DEĞİL.
    page = await join(meet, {"stayLabel": "Bleiben", "promptTimeoutMs": 400})
    await page.evaluate("() => mock.stillThere()")
    assert await wait_status(meet.rec, "disconnected") == bot.STILL_THERE_DETAIL
    assert "prompt_timeout" in meet.mock.names() and "prompt_left" not in meet.mock.names()


@browser
async def test_still_there_prompt_while_waiting_for_the_host_is_answered(meet):
    meet.mock.scenarios[CODE] = {"admission": "manual", "promptTimeoutMs": 3000}
    meet.bot.request_join(LINK)
    await eventually(lambda: "join_click" in meet.mock.names(), what="katılma tıklaması")
    await meet.bot.page.evaluate("() => mock.stillThere()")
    await eventually(lambda: "stayed" in meet.mock.names(), what="'Keep waiting' tıklandı")
    assert meet.mock.first("stayed") == {"inCall": False}
    assert meet.bot.status == "connecting"
    await meet.bot.page.evaluate("() => mock.admit()")
    await wait_status(meet.rec, "connected")


@browser
async def test_unanswered_lobby_prompt_ends_the_join_with_its_reason(meet):
    meet.mock.scenarios[CODE] = {"admission": "manual", "stayLabel": "Bleiben", "promptTimeoutMs": 200}
    meet.bot.request_join(LINK)
    await eventually(lambda: "join_click" in meet.mock.names(), what="katılma tıklaması")
    await meet.bot.page.evaluate("() => mock.stillThere()")
    assert await wait_status(meet.rec, "disconnected") == bot.STILL_THERE_DETAIL  # join_timeout (180 sn) beklenmez
    assert meet.mock.navigations == [CODE]


# ── Toplantı sahibi botu bekleme odasına geri gönderdi ─────────

@browser
async def test_sent_to_the_waiting_room_waits_for_readmission_instead_of_leaving(meet, tone_wav):
    page = await join(meet)
    await meet.bot.play(str(tone_wav(5.0)), token=1)
    await page.evaluate("() => mock.sendToWaitingRoom()")
    assert await wait_status(meet.rec, "connecting") == bot.WAITING_ROOM_DETAIL
    assert page.url == LINK and "left" not in meet.mock.names()   # sekme bekleme odasında kaldı
    assert await page.evaluate("() => window.__meetbot.status().token") is None  # ses durduruldu
    with pytest.raises(BotNotReady):
        await meet.bot.pause()
    meet.bot.request_join(LINK)  # aynı bağlantı: bekleme odasından çıkarmaz
    await asyncio.sleep(0.5)
    assert meet.bot.status == "connecting" and meet.mock.navigations == [CODE]

    await page.evaluate("() => mock.admit()")
    assert await wait_status(meet.rec, "connected") == bot.READMITTED_DETAIL
    assert meet.bot.is_connected and meet.bot.meet_link == LINK
    assert "readmitted" in meet.mock.names() and await mock_state(page, "mock.cam.muted") is True
    await meet.bot.play(str(tone_wav(2.0)), token=2)
    assert await page.evaluate("() => mock.level()") > 0.02  # müzik yine Meet'in mikrofon izinde
    assert "disconnected" not in [status for status, _ in meet.rec.statuses]


@browser
async def test_waiting_room_gives_up_after_the_join_timeout(meet):
    meet.settings.join_timeout = 1
    page = await join(meet)
    await page.evaluate("() => mock.sendToWaitingRoom()")
    await wait_status(meet.rec, "connecting")
    assert await wait_status(meet.rec, "disconnected") == "Bekleme odasından 1 sn içinde geri alınmadı"
    assert page.url == "about:blank" and meet.bot.meet_link is None


@browser
async def test_removal_or_leave_while_in_the_waiting_room(meet):
    page = await join(meet)
    await page.evaluate("() => mock.sendToWaitingRoom()")
    await wait_status(meet.rec, "connecting")
    await page.evaluate("() => mock.remove()")
    assert await wait_status(meet.rec, "disconnected") == "Toplantıdan çıkarıldı"
    assert page.url == "about:blank"

    page = await join(meet)
    await page.evaluate("() => mock.sendToWaitingRoom()")
    await wait_status(meet.rec, "connecting")
    await meet.bot.leave()
    assert meet.rec.last() == ("disconnected", "Bekleme odasından ayrıldı")
    assert page.url == "about:blank" and meet.bot.meet_link is None


@browser
async def test_joining_another_link_from_the_waiting_room(meet):
    page = await join(meet)
    connected_at = len(meet.rec.statuses)
    await page.evaluate("() => mock.sendToWaitingRoom()")
    await wait_status(meet.rec, "connecting")
    meet.mock.scenarios[OTHER_CODE] = {"admission": "direct", "popup": False}
    meet.bot.request_join(OTHER_LINK)
    await eventually(lambda: meet.bot.meet_link == OTHER_LINK and meet.bot.is_connected, what="ikinci toplantı")
    await asyncio.sleep(0.5)  # eski izleme bayat bir "disconnected" / "connected" göndermemeli
    assert "disconnected" not in [status for status, _ in meet.rec.statuses[connected_at:]]
    assert meet.bot.meet_link == OTHER_LINK and meet.bot.is_connected


# ── Ses komutları (toplantı içinde) ───────────────────────────

@browser
async def test_play_reports_progress_and_ended_exactly_once(meet, tone_wav):
    page = await join(meet)
    duration = await meet.bot.play(str(tone_wav(1.2)), token=7)
    assert duration == pytest.approx(1.2, abs=0.05)
    assert await page.evaluate("() => mock.level()") > 0.02  # müzik Meet'in mikrofon izinde

    await eventually(lambda: meet.rec.ended, what="şarkı bitti")
    await asyncio.sleep(0.5)  # birkaç yoklama daha: tekrar raporlanmamalı
    assert meet.rec.ended == [7]
    positions = [p for token, p, d, paused in meet.rec.progress]
    assert {token for token, *_ in meet.rec.progress} == {7}
    assert positions == sorted(positions) and len(positions) >= 3
    assert all(d == pytest.approx(1.2, abs=0.05) for _, _, d, _ in meet.rec.progress)

    # stop(): o token için artık ne ilerleme ne bitiş
    await meet.bot.play(str(tone_wav(3.0)), token=8)
    await eventually(lambda: any(token == 8 for token, *_ in meet.rec.progress), what="token 8 ilerlemesi")
    await meet.bot.stop()
    count = len(meet.rec.progress)
    await asyncio.sleep(0.5)
    assert len(meet.rec.progress) == count and meet.rec.ended == [7]
    assert await page.evaluate("() => window.__meetbot.status().token") is None


@browser
async def test_start_offset_pause_seek_and_resume(meet, tone_wav):
    await join(meet)
    await meet.bot.play(str(tone_wav(4.0)), token=3, start_at=2.5)
    await eventually(lambda: meet.rec.progress, what="ilerleme")
    assert meet.rec.progress[0][1] >= 2.5

    await meet.bot.pause()
    await eventually(lambda: meet.rec.progress[-1][3] is True, what="duraklatıldı")
    await meet.bot.seek(1.0)
    await eventually(lambda: meet.rec.progress[-1][1] == pytest.approx(1.0, abs=0.05), what="konum 1.0")
    assert meet.rec.progress[-1][3] is True  # duraklatılmışken de ilerleme raporlanır

    await meet.bot.resume()
    await eventually(lambda: meet.rec.progress[-1][3] is False and meet.rec.progress[-1][1] > 1.2,
                     what="devam ediyor")


@browser
async def test_play_errors_are_bot_errors(meet, tmp_path, tone_wav):
    page = await join(meet)
    await meet.bot.play(str(tone_wav(3.0)), token=1)
    with pytest.raises(BotError, match="okunamadı"):
        await meet.bot.play(str(tmp_path / "yok.webm"), token=2)
    assert await page.evaluate("() => window.__meetbot.status().token") is None  # eski şarkı sahipsiz kalmadı
    (tmp_path / "bos.webm").write_bytes(b"")
    with pytest.raises(BotError, match="boş"):
        await meet.bot.play(str(tmp_path / "bos.webm"), token=2)
    (tmp_path / "bozuk.webm").write_bytes(b"bu bir ses dosyasi degil" * 100)
    assert meet.rec.ended == []  # hatalı play'ler "bitti" olayı üretmez
    with pytest.raises(BotError, match="çözümlenemedi") as error:
        await meet.bot.play(str(tmp_path / "bozuk.webm"), token=3)
    assert not isinstance(error.value, BotNotReady)
    with pytest.raises(BotError, match="Yüklü bir şarkı yok"):
        await meet.bot.pause()


@browser
async def test_overlapping_plays_never_mix(meet, tone_wav, monkeypatch):
    monkeypatch.setattr(bot, "UPLOAD_CHUNK_BYTES", 16 * 1024)  # aktarım birçok parçaya bölünsün
    first_is_uploading = asyncio.Event()
    original_invoke = bot.invoke

    async def spying_invoke(page, method, *args, **kwargs):
        if method == "uploadChunk" and args[0] == 4:
            first_is_uploading.set()
        return await original_invoke(page, method, *args, **kwargs)

    monkeypatch.setattr(bot, "invoke", spying_invoke)
    page = await join(meet)
    first = asyncio.create_task(meet.bot.play(str(tone_wav(3.0)), token=4))
    await first_is_uploading.wait()
    second = asyncio.create_task(meet.bot.play(str(tone_wav(3.0, freq=660)), token=5))
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(results[0], PlaybackCancelled)  # yenisi, eskisinin aktarımını yarıda kesti
    assert results[1] == pytest.approx(3.0, abs=0.05)
    await eventually(lambda: meet.rec.progress and meet.rec.progress[-1][0] == 5, what="token 5 ilerlemesi")
    assert {token for token, *_ in meet.rec.progress} == {5}
    assert await page.evaluate("() => window.__meetbot.status().token") == 5


@browser
async def test_play_that_finishes_after_leaving_reports_not_ready(meet, tone_wav, monkeypatch):
    original_invoke = bot.invoke

    async def leave_right_after_play(page, method, *args, **kwargs):
        result = await original_invoke(page, method, *args, **kwargs)
        if method == "play":
            await meet.bot.leave()  # sayfadaki çalma başladı ama bot tam o anda ayrıldı
        return result

    monkeypatch.setattr(bot, "invoke", leave_right_after_play)
    await join(meet)
    with pytest.raises(BotNotReady):
        await meet.bot.play(str(tone_wav(3.0)), token=9)
    await asyncio.sleep(0.3)
    assert meet.rec.progress == [] and meet.rec.ended == []
    assert meet.rec.last() == ("disconnected", "Toplantıdan ayrıldı")


@browser
async def test_settings_made_before_joining_are_applied(meet):
    await meet.bot.set_music_volume(35)
    await meet.bot.set_mic_volume(60)
    assert await meet.bot.set_mic_muted(True) is True  # henüz toplantıda değil: saklanır
    page = await join(meet)
    await asyncio.sleep(0.2)  # kazanç rampası
    gains = await page.evaluate(
        "() => [window.__meetbot.engine.musicGain.gain.value, window.__meetbot.engine.micGain.gain.value]")
    assert gains == [pytest.approx(0.35, abs=0.01), pytest.approx(0.6, abs=0.01)]
    assert meet.mock.first("join_click")["micMuted"] is True
    assert await mock_state(page, "mock.mic.muted") is True


@browser
async def test_settings_menu_and_audio_tab_with_icon_text_in_their_names(meet):
    # Simge aria-hidden değilse ad "settingsSettings" / "volume_upSes" olur; ayar yine bulunmalı.
    for lang, code in (("en", CODE), ("tr", OTHER_CODE)):
        page = await join(meet, {"lang": lang, "iconNames": True}, link=f"https://meet.google.com/{code}")
        state = await mock_state(page, "{ nc: mock.noiseCancellation, adaptive: mock.adaptiveAudio }")
        assert state == {"nc": False, "adaptive": True}, lang
        await meet.bot.leave()


@browser
async def test_mic_toggle_prefers_meets_own_button_over_look_alikes(meet):
    page = await join(meet, {"participantMute": True})
    assert await meet.bot.set_mic_muted(True) is True
    assert await mock_state(page, "mock.mic.muted") is True
    assert "participant_muted" not in meet.mock.names()


@browser
async def test_mic_toggle_waits_for_a_late_button_and_errors_when_there_is_none(meet, monkeypatch):
    page = await join(meet)
    # Araç çubuğu yeniden çiziliyor: mikrofon düğmesi 0.5 sn görünmez.
    await page.evaluate("""() => {
        const mic = document.querySelector("[data-kind=mic]");
        mic.style.display = "none";
        setTimeout(() => { mic.style.display = ""; }, 500);
    }""")
    assert await meet.bot.set_mic_muted(True) is True
    assert await mock_state(page, "mock.mic.muted") is True

    monkeypatch.setattr(bot, "TOGGLE_FIND_TIMEOUT", 0.3)
    await page.evaluate('() => document.querySelector("[data-kind=mic]").remove()')
    with pytest.raises(BotError, match="mikrofon düğmesi bulunamadı") as error:
        await meet.bot.set_mic_muted(False)
    assert not isinstance(error.value, BotNotReady)


@browser
async def test_mic_mute_reads_and_sets_data_is_muted(meet):
    # Meet botu toplantıya sessizde alır; bot istenen durumu (açık) geri getirir.
    page = await join(meet, {"startMicMuted": True, "toggleDelayMs": 150})
    assert await mock_state(page, 'document.querySelector("[data-kind=mic]").dataset.isMuted') == "false"

    assert await meet.bot.set_mic_muted(True) is True
    assert await mock_state(page, 'document.querySelector("[data-kind=mic]").dataset.isMuted') == "true"
    assert await meet.bot.set_mic_muted(True) is True  # zaten kapalı: tıklanmaz
    assert meet.mock.names().count("mic_toggled") == 2
    assert await meet.bot.set_mic_muted(False) is False
    assert await mock_state(page, 'document.querySelector("[data-kind=mic]").dataset.isMuted') == "false"


@browser
async def test_callback_errors_do_not_kill_the_monitor(meet, tone_wav, caplog):
    async def broken_progress(*_):
        raise RuntimeError("Player hatası")

    meet.bot.on_progress = broken_progress
    await join(meet)
    with caplog.at_level(logging.ERROR, logger="meetbot.bot"):
        await meet.bot.play(str(tone_wav(0.6)), token=11)
        await eventually(lambda: meet.rec.ended == [11], what="şarkı bitti")
    assert "on_progress geri çağrısı hata verdi" in caplog.text
    assert meet.bot.is_connected


# ──────────────────────────────────────────────────────────────
#  Chrome: kendi sürecimiz / başkasının tarayıcısı (Playwright Chromium ile)
# ──────────────────────────────────────────────────────────────

@browser
async def test_own_chrome_is_launched_and_closed_by_pid(chromium, settings, monkeypatch, tmp_path):
    original = bot.build_chrome_args

    def headless_args(*args, **kwargs):
        return original(*args, **kwargs)[:-1] + ["--headless=new", "about:blank"]

    monkeypatch.setattr(bot, "build_chrome_args", headless_args)
    monkeypatch.setattr(bot, "SILENCE_WAV", tmp_path / "silence.wav")
    settings.chrome_path = chromium.browser_type.executable_path
    settings.cdp_port = free_port()
    meet_bot = MeetBot(settings)
    rec = Recorder(meet_bot)
    process = None
    try:
        await meet_bot._ensure_context()
        process = meet_bot._chrome_process
        assert process is not None and process.poll() is None
        assert (tmp_path / "silence.wav").is_file()
        page = await meet_bot._acquire_page()
        assert page.url == "about:blank" and meet_bot._page_created is False  # ilk boş sekme kullanıldı
        assert ("connecting", "Tarayıcı hazırlanıyor…") in rec.statuses
        await meet_bot.shutdown()
        assert process.poll() is not None
        assert cdp_version(settings.cdp_port) is None
    finally:
        if process is not None and process.poll() is None:
            kill_process_tree(process)


@browser
async def test_foreign_browser_on_the_cdp_port_is_reused_but_never_killed(chromium, settings, caplog):
    settings.cdp_port = free_port()
    foreign = await chromium.browser_type.launch(headless=True, args=[f"--remote-debugging-port={settings.cdp_port}"])
    try:
        user_page = await foreign.new_page()
        # Önceki bir çalıştırmadan kalmış (belki hâlâ toplantıda olan) Meet sekmesi
        await user_page.route("https://meet.google.com/**", lambda route: route.fulfill(body="eski toplantı"))
        await user_page.goto(LINK)
        meet_bot = MeetBot(settings)
        with caplog.at_level(logging.WARNING, logger="meetbot.bot"):
            await meet_bot._ensure_context()
        assert "zaten bir tarayıcı açık" in caplog.text
        assert "1 açık Meet sekmesi var" in caplog.text
        assert meet_bot._chrome_process is None
        page = await meet_bot._acquire_page()
        assert meet_bot._page_created is True  # kullanıcının sekmesine dokunulmadı
        await meet_bot.shutdown()
        assert page.is_closed()
        assert foreign.is_connected() and not user_page.is_closed()
    finally:
        await foreign.close()


# ──────────────────────────────────────────────────────────────
#  Tarayıcısız birim testleri
# ──────────────────────────────────────────────────────────────

async def test_commands_before_joining(settings, tmp_path):
    meet_bot = MeetBot(settings)
    rec = Recorder(meet_bot)
    assert (meet_bot.status, meet_bot.meet_link, meet_bot.is_connected) == ("disconnected", None, False)
    with pytest.raises(BotNotReady, match="toplantıda değil"):
        await meet_bot.play(str(tmp_path / "x.wav"), token=1)
    for command in (meet_bot.pause(), meet_bot.resume(), meet_bot.seek(3)):
        with pytest.raises(BotNotReady):
            await command
    await meet_bot.stop()  # çalan bir şey yok: sessizce geçer
    await meet_bot.set_music_volume(150)
    await meet_bot.set_mic_volume(-5)
    assert (meet_bot._music_volume, meet_bot._mic_volume) == (100, 0)
    with pytest.raises(BotError, match="Geçersiz ses seviyesi"):
        await meet_bot.set_music_volume("yüksek")
    assert await meet_bot.set_mic_muted(True) is True
    await meet_bot.leave()
    await meet_bot.shutdown()
    assert rec.statuses == []


class IdlePage:
    """İzleme birim testleri için sayfa yerine geçer (yoklama taklit edilir, sekme boş)."""

    url = "about:blank"

    def is_closed(self):
        return False


def audio_state(token, position=0.0, duration=3.0, paused=False, ended=False, error=None, loading=False):
    return {"token": token, "position": position, "duration": duration, "paused": paused,
            "ended": ended, "error": error, "loading": loading}


def connected_bot(settings, probes):
    """Toplantıda sayılan, yoklamaları sırayla `probes`tan gelen (istisna ise fırlatılan) MeetBot."""
    meet_bot = MeetBot(settings)
    meet_bot.MONITOR_INTERVAL = 0.01
    meet_bot._status, meet_bot._meet_link = "connected", LINK
    queue = iter(probes)

    async def fake_probe(page):
        probe = next(queue)
        if isinstance(probe, BaseException):
            raise probe
        return probe

    meet_bot._probe = fake_probe
    return meet_bot, Recorder(meet_bot)


REMOVED_PROBE = {"in_call": False, "audio": audio_state(None), "text": "You’ve been removed from the meeting"}


async def test_monitor_reports_progress_and_ended_once_for_the_current_token_only(settings):
    meet_bot, rec = connected_bot(settings, [
        {"in_call": True, "audio": audio_state(4, position=9.0), "text": ""},          # eski token
        {"in_call": True, "audio": audio_state(5, loading=True), "text": ""},          # hâlâ yükleniyor
        {"in_call": True, "audio": audio_state(5, position=1.5, paused=True), "text": ""},
        {"in_call": True, "audio": audio_state(5, position=None, duration=None), "text": ""},
        {"in_call": True, "audio": audio_state(5, ended=True, error="DECODE"), "text": ""},  # bozuk dosya = bitti
        {"in_call": True, "audio": audio_state(5, ended=True), "text": ""},            # tekrar raporlanmaz
        *[REMOVED_PROBE] * MeetBot.OUT_OF_CALL_LIMIT,
    ])
    meet_bot._token = 5
    await asyncio.wait_for(meet_bot._monitor(IdlePage()), 5)
    assert rec.progress == [(5, 1.5, 3.0, True), (5, 0.0, 0.0, False)]
    assert rec.ended == [5]
    assert rec.statuses == [("disconnected", "Toplantıdan çıkarıldı")]
    assert (meet_bot.status, meet_bot.meet_link, meet_bot.status_detail) == ("disconnected", None, "Toplantıdan çıkarıldı")


async def test_monitor_gives_up_after_consecutive_probe_failures_only(settings):
    in_call = {"in_call": True, "audio": audio_state(None), "text": ""}
    limit = MeetBot.OUT_OF_CALL_LIMIT
    failures = [PlaywrightError("Target crashed"), asyncio.TimeoutError()]
    meet_bot, rec = connected_bot(settings, [*failures, in_call, *(failures * limit)[:limit]])
    await asyncio.wait_for(meet_bot._monitor(IdlePage()), 5)
    assert rec.statuses == [("disconnected", "Meet sekmesi yanıt vermiyor")]


async def test_monitor_reinjects_a_missing_audio_engine(settings):
    meet_bot, rec = connected_bot(settings, [
        {"in_call": True, "audio": None, "text": ""},
        *[REMOVED_PROBE] * MeetBot.OUT_OF_CALL_LIMIT,
    ])
    calls = []

    async def record(name):
        calls.append(name)

    meet_bot._inject = lambda page: record("inject")
    meet_bot._apply_audio_settings = lambda page: record("settings")
    await asyncio.wait_for(meet_bot._monitor(IdlePage()), 5)
    assert calls == ["inject", "settings"]
    assert rec.last() == ("disconnected", "Toplantıdan çıkarıldı")


async def test_leave_during_a_lost_call_cleanup_reports_disconnected_once(settings):
    meet_bot, rec = connected_bot(settings, [REMOVED_PROBE] * MeetBot.OUT_OF_CALL_LIMIT)
    release, teardowns = asyncio.Event(), []

    async def slow_teardown():  # ör. Meet "Görüşmeden ayrıl"a geç yanıt veriyor
        teardowns.append(asyncio.current_task())
        await release.wait()

    meet_bot._teardown_call = slow_teardown
    meet_bot._monitor_task = asyncio.create_task(meet_bot._monitor(IdlePage()))
    await eventually(lambda: teardowns, what="izleme temizliğe başladı")
    assert rec.statuses == [("disconnected", "Toplantıdan çıkarıldı")]  # temizlikten ÖNCE bildirildi

    leaving = asyncio.create_task(meet_bot.leave())
    await eventually(lambda: len(teardowns) == 2, what="leave() temizliği")
    assert teardowns[0].cancelled()
    release.set()
    await leaving
    assert rec.statuses == [("disconnected", "Toplantıdan çıkarıldı")]  # ikinci bildirim yok


async def test_join_fails_cleanly_when_chrome_is_missing(settings, tmp_path):
    settings.chrome_path = str(tmp_path / "chrome-yok.exe")
    settings.cdp_port = free_port()
    meet_bot = MeetBot(settings)
    rec = Recorder(meet_bot)
    meet_bot.request_join(LINK)
    detail = await wait_status(rec, "disconnected")
    assert detail == "Chrome veya Edge bulunamadı (MEETBOT_CHROME_PATH ayarını kontrol edin)"
    assert rec.statuses[:2] == [("connecting", "Toplantıya bağlanılıyor…"), ("connecting", "Tarayıcı hazırlanıyor…")]
    assert meet_bot.meet_link is None
    await meet_bot.shutdown()


async def test_join_refuses_a_non_cdp_service_on_the_port(settings):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        settings.cdp_port = listener.getsockname()[1]
        meet_bot = MeetBot(settings)
        rec = Recorder(meet_bot)
        meet_bot.request_join(LINK)
        detail = await wait_status(rec, "disconnected")
    assert "başka bir uygulama tarafından kullanılıyor" in detail
    assert meet_bot._chrome_process is None
    await meet_bot.shutdown()


def test_find_chrome(monkeypatch, tmp_path):
    exe = tmp_path / "my-chrome.exe"
    exe.write_bytes(b"")
    assert find_chrome(str(exe)) == str(exe)
    assert find_chrome(str(tmp_path / "missing.exe")) is None  # yanlış ayar sessizce başka tarayıcıya düşmez

    monkeypatch.setattr(bot.platform, "system", lambda: "Windows")
    for key, sub in (("ProgramFiles", "pf86"), ("ProgramW6432", "pf"), ("ProgramFiles(x86)", "pf86"),
                     ("LocalAppData", "local")):
        monkeypatch.setenv(key, str(tmp_path / sub))
    edge = tmp_path / "pf86" / "Microsoft" / "Edge" / "Application" / "msedge.exe"
    edge.parent.mkdir(parents=True)
    edge.write_bytes(b"")
    assert Path(find_chrome()) == edge
    chrome = tmp_path / "local" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome.parent.mkdir(parents=True)
    chrome.write_bytes(b"")
    assert Path(find_chrome()) == chrome  # Chrome, Edge'den önce gelir
    # 32 bit Python: ProgramFiles = "(x86)"; 64 bit Chrome yine de ProgramW6432 altında bulunur.
    chrome64 = tmp_path / "pf" / "Google" / "Chrome" / "Application" / "chrome.exe"
    chrome64.parent.mkdir(parents=True)
    chrome64.write_bytes(b"")
    assert Path(find_chrome()) == chrome64

    monkeypatch.setattr(bot.platform, "system", lambda: "Linux")
    monkeypatch.setattr(bot.shutil, "which", lambda name: str(exe) if name == "chromium" else None)
    assert find_chrome() == str(exe)


def test_build_chrome_args(settings, monkeypatch, tmp_path):
    silence = tmp_path / "silence.wav"
    monkeypatch.setattr(bot, "_running_as_root_on_linux", lambda: False)
    args = build_chrome_args("chrome.exe", settings, silence)
    assert args[0] == "chrome.exe" and args[-1] == "about:blank"
    for flag in (
        f"--remote-debugging-port={settings.cdp_port}",
        f"--user-data-dir={settings.profile_dir}",
        "--use-fake-ui-for-media-stream",
        "--use-fake-device-for-media-stream",
        f"--use-file-for-fake-audio-capture={silence}",
        "--autoplay-policy=no-user-gesture-required",
        "--disable-background-timer-throttling",
        "--disable-renderer-backgrounding",
        "--disable-backgrounding-occluded-windows",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-notifications",
        "--start-maximized",
    ):
        assert flag in args
    assert "--no-sandbox" not in args
    assert not any(a.startswith("--allow-file-access-from-files") for a in args)

    monkeypatch.setattr(bot, "_running_as_root_on_linux", lambda: True)
    assert "--no-sandbox" in build_chrome_args("chrome", settings, silence)


def _pid_alive(pid):
    if os.name == "nt":
        output = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                                capture_output=True, text=True, errors="replace").stdout
        return str(pid) in output.split()
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_kill_process_tree_kills_the_whole_tree_by_pid():
    code = ("import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
            "print(child.pid, flush=True); time.sleep(60)")
    kwargs = {} if os.name == "nt" else {"start_new_session": True}
    parent = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True, **kwargs)
    child_pid = None
    try:
        child_pid = int(parent.stdout.readline())
        assert _pid_alive(child_pid)
        kill_process_tree(parent)
        assert parent.poll() is not None
        for _ in range(50):
            if not _pid_alive(child_pid):
                break
            threading.Event().wait(0.1)
        assert not _pid_alive(child_pid)
    finally:
        parent.stdout.close()
        if parent.poll() is None:
            parent.kill()
            parent.wait()


@pytest.mark.skipif(os.name != "nt", reason="Windows'a özgü")
def test_kill_process_tree_uses_taskkill_by_pid_never_by_image_name(monkeypatch):
    calls = []

    class FakeProcess:
        pid = 4242
        alive = True

        def poll(self):
            return None if self.alive else 1

        def wait(self, timeout=None):
            return 1

    process = FakeProcess()

    def fake_run(args, **kwargs):
        calls.append(args)
        process.alive = False
        return subprocess.CompletedProcess(args, 0, b"", b"")

    monkeypatch.setattr(bot.subprocess, "run", fake_run)
    kill_process_tree(process)
    assert calls == [["taskkill", "/PID", "4242", "/T", "/F"]]
    source = Path(bot.__file__).read_text(encoding="utf-8")
    assert '"/IM"' not in source and "/IM chrome" not in source and "/IM msedge" not in source


@pytest.mark.skipif(os.name != "nt", reason="Windows'a özgü")
@pytest.mark.parametrize("failure", ["missing", "error"])
def test_kill_process_tree_falls_back_to_our_own_process_when_taskkill_fails(monkeypatch, failure):
    killed = []

    class FakeProcess:
        pid = 4343

        def poll(self):
            return 1 if killed else None

        def kill(self):
            killed.append(self.pid)

        def wait(self, timeout=None):
            return 1

    def fake_run(args, **kwargs):
        if failure == "missing":
            raise FileNotFoundError("taskkill")
        return subprocess.CompletedProcess(args, 128, b"", "HATA: işlem bulunamadı".encode("cp857"))

    monkeypatch.setattr(bot.subprocess, "run", fake_run)
    kill_process_tree(FakeProcess())
    assert killed == [4343]


def test_cdp_version(settings):
    assert cdp_version(free_port()) is None

    class Handler(http.server.BaseHTTPRequestHandler):
        payload = {"Browser": "Chrome/153.0", "webSocketDebuggerUrl": "ws://127.0.0.1/devtools/browser/x"}

        def do_GET(self):
            body = json.dumps(self.payload if self.path == "/json/version" else {}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        assert cdp_version(port)["Browser"] == "Chrome/153.0"
        Handler.payload = {"hello": "world"}  # CDP olmayan bir HTTP servisi
        assert cdp_version(port) is None
    finally:
        server.shutdown()
        server.server_close()


DENIED = "Toplantı sahibi katılma isteğini reddetti"
REMOVED = "Toplantıdan çıkarıldı"
NOT_FOUND = "Toplantı bulunamadı (bağlantıyı kontrol edin)"


@pytest.mark.parametrize("text, expected", [
    ("You can’t join this call\nSomeone in the call denied your request to join", DENIED),
    ("Bu görüşmeye katılamazsınız. Görüşmedeki biri katılma isteğinizi reddetti", DENIED),
    ("No one responded to your request to join the call", "Katılma isteğine kimse yanıt vermedi"),
    ("Katılma isteğinize yanıt verilmedi", "Katılma isteğine kimse yanıt vermedi"),
    ("You’ve been removed from the meeting\nReturn to home screen", REMOVED),
    ("TOPLANTIDAN ÇIKARILDINIZ", REMOVED),
    ("You left the meeting", "Bot toplantıdan ayrıldı (Meet penceresinden)"),
    ("Your host ended the meeting for everyone", "Toplantı sona erdi"),
    ("Check your meeting code. Make sure you entered the correct meeting code", NOT_FOUND),
    ("Geçersiz görüntülü görüşme adı.", NOT_FOUND),
    ("You can't create a meeting yourself", "Toplantı henüz başlamamış (bot toplantı başlatamaz)"),
    ("You can't join this video call\nReturn to home screen", "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),  # gerçek Meet
    ("Bu video görüşmesine katılamazsınız\nAna ekrana dön", "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),  # gerçek Meet (TR)
    ("What’s your name?\nAsk to join\nOther ways to join", None),
    ("Katılma isteği gönderiliyor…\nBiri sizi içeri aldığında görüşmeye katılacaksınız", None),
    ("", None),
    # Meet'in katılım hatası listesinden (EN) — eskiden hiçbiri tanınmıyordu
    ("Are you still there?\nReturn to home screen", bot.STILL_THERE_DETAIL),
    ("This meeting is full. Try again later.", "Toplantı dolu (katılımcı sınırına ulaşıldı)"),
    ("This call has reached the maximum of 100 participants. Try again later.",
     "Toplantı dolu (katılımcı sınırına ulaşıldı)"),
    ("Your meeting code has expired", "Toplantı kodunun süresi dolmuş"),
    ("This meeting has already ended", "Toplantı sona erdi"),
    ("The meeting code you entered doesn’t work", NOT_FOUND),
    ("This video call is restricted to an organization you don't belong to.",
     "Toplantı yalnızca bir kuruluşun üyelerine açık (bot profilinde yetkili bir hesapla oturum açın)"),
    ("You're currently in a video call in another window. You must exit that video call before you can join "
     "this one.", "Bot profili başka bir pencerede zaten bir görüşmede"),
    ("You aren't allowed to join this video call", "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),
    ("You’re not allowed to join this call. An app is collecting audio and/or video from the call.",
     "Bu toplantıya katılınamıyor (Meet oturum açmamış katılımcıları kabul etmiyor olabilir: botun Chrome profilinde bir Google hesabıyla oturum açın)"),
    # Kurtarılabilir / bilgi metinleri: katılamama DEĞİL
    ("You’ve been sent to the waiting room\nPlease wait until a meeting host brings you into the call", None),
    ("This call has a waiting room\nJoin the call now\nOther ways to join", None),
    ("No one else is here\nJoin now", None),
])
def test_classify_meet_text(text, expected):
    assert classify_meet_text(text) == expected


@pytest.mark.parametrize("text, expected", [
    ("You’ve been sent to the waiting room\nPlease wait until a meeting host brings you into the call", True),
    ("Asking to be let in…\nYou’ll join the call when someone lets you in", True),
    ("Bekleme odasına gönderildiniz", True),
    ("Katılma isteği gönderiliyor…\nBiri sizi içeri aldığında görüşmeye katılacaksınız", True),
    ("This call has a waiting room\nJoin the call now", False),   # katılma ekranındaki bilgi notu
    ("You’ve been removed from the meeting", False),
    ("You left the meeting", False),
    ("", False),
])
def test_is_waiting_room_text(text, expected):
    assert bot.is_waiting_room_text(text) is expected


# Meet'in birincil katılma düğmesi için ürettiği etiketler (Meet JS, uiy) + aria-label varyantları.
MEET_JOIN_LABELS = [
    "Ask to join anyway", "Ask to join anyway without microphone & camera", "Ask to join anyway without camera",
    "Ask to join anyway without microphone", "Ask to join", "Ask to join without microphone & camera",
    "Ask to join without camera", "Ask to join without microphone",
    "Join the waiting room as a moderated contributor", "Join the waiting room anyway", "Join the call now",
    "Join as a viewer", "Join as a viewer without microphone & camera", "Join as a viewer without camera",
    "Join as a viewer without microphone", "Join anyway", "Join anyway without microphone & camera",
    "Join anyway without camera", "Join anyway without microphone", "Join now", "Join now without microphone & camera",
    "Join now without camera", "Join now without microphone", "Join", "Join here too", "add_to_queueJoin here too",
    "Katılma isteği gönder", "Hemen katıl", " Şimdi katıl ", "Katıl", "Burada da katıl", "Yine de katıl",
    "Yine de katılma isteği gönder", "Katılma isteğinde bulun", "Mikrofon olmadan hemen katıl",
]
NOT_JOIN_LABELS = [
    "Other ways to join", "Diğer katılma yöntemleri", "Join with Companion mode", "Join and use a phone for audio",
    "Switch here", "Switch here without microphone & camera", "Buraya geç", "Yardımcı moduyla katıl",
    "Katıl ve ses için telefon kullan", "Rejoin", "Return to home screen", "Joining…", "Use Companion mode",
    "Present", "Cancel", "Turn off microphone (ctrl + d)",
]


@pytest.mark.parametrize("pattern, matches, rejects", [
    (bot.SETTINGS_ITEM_RE, ["Settings", "settingsSettings", "Ayarlar", "settings Ayarlar"],
     ["Other settings", "Report a problem", "Görüntü ayarları"]),
    (bot.AUDIO_TAB_RE, ["Audio", "volume_upAudio", "Ses", "graphic_eq Ses"], ["Video", "videocamVideo", "Uzamsal ses"]),
    (bot.GENERAL_TAB_RE, ["General", "settingsGeneral", "Genel", "settings Genel"],
     ["General settings", "Genel bakış", "Audio"]),
    (bot.JOIN_BUTTON_RE, MEET_JOIN_LABELS, NOT_JOIN_LABELS),
    (bot.SWITCH_HERE_RE, ["Switch here", "Switch here without camera", "Buraya geç"],
     ["Join here too", "Burada da katıl", "Join now"]),
    (bot.OTHER_WAYS_RE, ["Other ways to join", "expand_moreOther ways to join", "Diğer katılma yöntemleri"],
     ["Join now", "Join here too", "More options"]),
    (bot.STAY_STRONG_RE, ["Stay in the call", "Keep waiting", "I'm still here", "Görüşmede kal", "Beklemeye devam et",
                          "Buradayım"], ["Leave call", "Leave now", "Continue", "Kalite", "Got it"]),
    (bot.PROMPT_TEXT_RE, ["Are you still there?", "You're the only one here", "Hâlâ orada mısınız?"],
     ["Others may see your video differently", "No one else is here", "Leave empty calls"]),
], ids=["settings", "audio_tab", "general_tab", "join", "switch_here", "other_ways", "stay", "prompt"])
def test_meet_ui_name_patterns(pattern, matches, rejects):
    assert [name for name in matches if not pattern.search(name)] == []
    assert [name for name in rejects if pattern.search(name)] == []


@pytest.mark.parametrize("is_muted, label, expected", [
    ("true", "Turn off microphone", True),        # data-is-muted etiketten önce gelir
    ("false", "", False),
    (None, "Turn on microphone (ctrl + d)", True),
    (None, "Turn off camera (ctrl + e)", False),
    (None, "Mikrofonu aç (ctrl + d)", True),
    (None, "Kamerayı kapat (ctrl + e)", False),
    (None, "Microphone: Fake Default Audio Input", None),
    (None, "Mikrofon ayarları", None),
])
def test_parse_toggle_state(is_muted, label, expected):
    assert parse_toggle_state(is_muted, label) == expected


def test_small_helpers(tmp_path):
    assert audio_mime_type("x/abc.WEBM") == "audio/webm"
    assert audio_mime_type("a.m4a") == audio_mime_type("a.mp4") == "audio/mp4"
    assert audio_mime_type("a.opus") == audio_mime_type("a.ogg") == "audio/ogg"
    assert audio_mime_type("a.mp3") == "audio/mpeg"
    assert audio_mime_type("a.wav") == "audio/wav"
    assert audio_mime_type("a.bin") == "application/octet-stream"
    assert [clamp_volume(v) for v in (-3, 42.6, "77", 250)] == [0, 43, 77, 100]
    for bad in (None, "abc", float("nan")):
        with pytest.raises(BotError):
            clamp_volume(bad)

    path = create_silence_wav(tmp_path / "sub" / "silence.wav")
    with wave.open(str(path)) as wav_file:
        assert (wav_file.getnchannels(), wav_file.getsampwidth(), wav_file.getframerate()) == (1, 2, 48000)
        assert wav_file.getnframes() == 48000
        assert set(wav_file.readframes(wav_file.getnframes())) == {0}
