// ──────────────────────────────────────────────────────────────
//  meetbot_inject.js — MeetBot ses motoru (Meet sekmesine enjekte edilir)
//
//  bot.py bu dosyayı sekmeye init script olarak ekler ve her gezinmeden
//  sonra güvenlik için bir kez daha çalıştırır; tekrar çalışması zararsızdır.
//
//  Ses zinciri (48 kHz):
//    <audio src="blob:…"> → musicGain → [DynamicsCompressor] → micGain → MediaStreamDestination
//
//  Meet mikrofon istediğinde (getUserMedia) hedef akışın ses izinin bir
//  KLONU verilir: Meet eski bir izi stop() etse bile botun sesi kesilmez.
//
//  Python tarafı yalnızca window.__meetbot API'sini kullanır:
//    uploadBegin(token, mime) · uploadChunk(token, b64) · uploadEnd(token) → blobUrl
//    play(token, url, startAt) → {duration, paused}
//    pause() · resume() · stop() · seek(t)
//    setMusicVolume(v) · setMicVolume(v) · setNormalize(on)
//    status() → {token, position, duration, paused, ended, error, loading}
//
//  Hatalar "MEETBOT_<KOD>: açıklama" biçimindedir; bot.py koda bakarak
//  kullanıcıya Türkçe mesaj üretir.
// ──────────────────────────────────────────────────────────────
(() => {
    "use strict";

    // iframe'ler kendi AudioContext'ini açmasın: sadece üst çerçeve.
    if (window.top !== window) return;
    if (window.__meetbot_injected) return;
    window.__meetbot_injected = true;

    const TAG = "[MeetBot]";
    const SAMPLE_RATE = 48000;
    const LOAD_TIMEOUT_MS = 30000;
    const SEEK_TIMEOUT_MS = 10000;
    const RAMP_SECONDS = 0.02;
    const END_MARGIN_SECONDS = 0.25;

    const fail = (code, message) => new Error(`MEETBOT_${code}: ${message}`);
    const finite = (value) => (Number.isFinite(value) ? value : 0);
    const clampVolume = (value) => Math.min(100, Math.max(0, Number(value) || 0));

    const state = {
        engine: null,     // {ctx, dest, musicGain, compressor, micGain} — ilk ihtiyaçta kurulur
        musicVolume: 80,
        micVolume: 80,
        normalize: true,
        current: null,    // yüklenen / çalan parça
        upload: null,     // devam eden dosya aktarımı
        pendingUrl: null, // aktarımı bitmiş ama henüz play() ile sahiplenilmemiş blob URL'si
        discarded: new Set(), // sahiplenilmeden bırakılan (iptal edilmiş) blob URL'leri
    };

    // ── Ses motoru ──────────────────────────────────────────────

    function engine() {
        if (state.engine) return state.engine;
        const AudioContextClass = window.AudioContext || window.webkitAudioContext;
        const ctx = new AudioContextClass({ sampleRate: SAMPLE_RATE });
        const musicGain = ctx.createGain();
        const compressor = ctx.createDynamicsCompressor();
        const micGain = ctx.createGain();
        const dest = ctx.createMediaStreamDestination();

        // Yumuşak bir kompresör: şarkılar arası seviye farkını azaltır, pompalamaz.
        compressor.threshold.value = -20;
        compressor.knee.value = 10;
        compressor.ratio.value = 4;
        compressor.attack.value = 0.005;
        compressor.release.value = 0.25;

        musicGain.gain.value = state.musicVolume / 100;
        micGain.gain.value = state.micVolume / 100;
        micGain.connect(dest);

        state.engine = { ctx, dest, musicGain, compressor, micGain };
        wire();
        console.log(TAG, `Ses motoru hazır (${ctx.sampleRate} Hz, durum: ${ctx.state}).`);
        return state.engine;
    }

    function wire() {
        const { musicGain, compressor, micGain } = state.engine;
        musicGain.disconnect();
        compressor.disconnect();
        if (state.normalize) {
            musicGain.connect(compressor);
            compressor.connect(micGain);
        } else {
            musicGain.connect(micGain);
        }
    }

    // resume() otomatik oynatma kuralına takılırsa sonsuza kadar bekleyebilir;
    // bu yüzden beklemeden tetiklenir (bot Chrome'u zaten
    // --autoplay-policy=no-user-gesture-required ile açar).
    function wake(ctx) {
        if (ctx.state === "running") return;
        ctx.resume().catch((err) => console.warn(TAG, "AudioContext başlatılamadı:", err));
    }

    function rampTo(param, value) {
        const { ctx } = state.engine;
        param.cancelScheduledValues(ctx.currentTime);
        param.setTargetAtTime(value, ctx.currentTime, RAMP_SECONDS);
    }

    // ── Parça yaşam döngüsü ─────────────────────────────────────

    function mediaError(audio) {
        const err = audio.error;
        if (!err) return "bilinmeyen medya hatası";
        const names = { 1: "ABORTED", 2: "NETWORK", 3: "DECODE", 4: "SRC_NOT_SUPPORTED" };
        return `${names[err.code] || err.code}${err.message ? ` (${err.message})` : ""}`;
    }

    // Parçayı tamamen bırakır: bekleyen yüklemeyi iptal eder, grafikten
    // koparır, elementi boşaltır ve blob URL'sini serbest bırakır.
    function release(track) {
        if (!track || track.released) return;
        track.released = true;
        track.controller.abort();
        track.audio.pause();
        if (track.source) track.source.disconnect();
        track.audio.removeAttribute("src");
        track.audio.load();
        if (track.url.startsWith("blob:")) URL.revokeObjectURL(track.url);
    }

    function waitForMedia(track, eventName, timeoutMs, what) {
        const { audio, controller } = track;
        return new Promise((resolve, reject) => {
            let timer = null;
            const finish = (err) => {
                clearTimeout(timer);
                audio.removeEventListener(eventName, onReady);
                audio.removeEventListener("error", onError);
                controller.signal.removeEventListener("abort", onAbort);
                if (err) reject(err);
                else resolve();
            };
            const onReady = () => finish(null);
            const onError = () => finish(fail("LOAD_ERROR", `${what}: ${mediaError(audio)}`));
            const onAbort = () => finish(fail("SUPERSEDED", "yerine yeni bir istek geldi"));
            if (controller.signal.aborted) {
                onAbort();
                return;
            }
            timer = setTimeout(
                () => finish(fail("LOAD_TIMEOUT", `${what} ${timeoutMs / 1000} sn içinde tamamlanmadı`)),
                timeoutMs,
            );
            audio.addEventListener(eventName, onReady);
            audio.addEventListener("error", onError);
            controller.signal.addEventListener("abort", onAbort);
        });
    }

    async function play(token, url, startAt) {
        url = String(url);
        // Arada gelen stop() / yeni aktarım bu dosyayı iptal etti: çalmaya kalkma.
        if (state.discarded.delete(url)) throw fail("SUPERSEDED", "yerine yeni bir istek geldi");
        if (state.pendingUrl === url) state.pendingUrl = null; // blob artık parçaya ait
        const { ctx, musicGain } = engine();

        // Yeni istek eskisini HEMEN geçersiz kılar: iki parça asla karışmaz.
        release(state.current);
        const track = {
            token,
            url,
            audio: new Audio(),
            source: null,
            controller: new AbortController(),
            ready: false,       // grafiğe bağlandı, çalmaya hazır
            ended: false,
            error: null,
            wantPaused: false,  // yüklenirken pause() geldiyse başlatma
            released: false,
        };
        state.current = track;
        const { audio } = track;
        audio.preload = "auto";
        audio.addEventListener("ended", () => {
            if (state.current === track) track.ended = true;
        });
        audio.addEventListener("error", () => {
            // Yükleme hataları waitForMedia'da ele alınır; bu yalnızca çalarken bozulan dosyalar için.
            if (state.current !== track || !track.ready || track.ended) return;
            track.error = mediaError(audio);
            track.ended = true;
            console.warn(TAG, "Parça çalınırken hata:", track.error);
        });
        audio.src = track.url;

        try {
            await waitForMedia(track, "loadedmetadata", LOAD_TIMEOUT_MS, "Ses dosyası");
            const target = Math.max(0, Number(startAt) || 0);
            if (target > 0) {
                const duration = audio.duration;
                audio.currentTime = Number.isFinite(duration)
                    ? Math.min(target, Math.max(0, duration - END_MARGIN_SECONDS))
                    : target;
                await waitForMedia(track, "seeked", SEEK_TIMEOUT_MS, "Konuma atlama");
            }
        } catch (err) {
            if (state.current === track) state.current = null;
            release(track);
            throw err;
        }

        track.source = ctx.createMediaElementSource(audio);
        track.source.connect(musicGain);
        track.ready = true;
        wake(ctx);

        if (!track.wantPaused) {
            try {
                await audio.play();
            } catch (err) {
                if (track.released) throw fail("SUPERSEDED", "yerine yeni bir istek geldi");
                if (state.current === track) state.current = null;
                release(track);
                throw fail("PLAY_ERROR", `${err.name}: ${err.message}`);
            }
        }
        // play() beklenirken stop()/yeni play() geldiyse bu parça artık geçersiz.
        if (track.released) throw fail("SUPERSEDED", "yerine yeni bir istek geldi");
        console.log(TAG, `▶️ Çalıyor (token ${token}, ${finite(audio.duration).toFixed(1)} sn).`);
        return { duration: finite(audio.duration), paused: audio.paused };
    }

    function currentTrack() {
        if (!state.current) throw fail("NO_TRACK", "yüklü bir parça yok");
        return state.current;
    }

    function pause() {
        const track = currentTrack();
        track.wantPaused = true;
        track.audio.pause();
    }

    async function resume() {
        const track = currentTrack();
        track.wantPaused = false;
        if (!track.ready || track.ended) return; // hâlâ yükleniyorsa play() başlatacak
        wake(state.engine.ctx);
        try {
            await track.audio.play();
        } catch (err) {
            if (track.released) throw fail("SUPERSEDED", "yerine yeni bir istek geldi");
            throw fail("PLAY_ERROR", `${err.name}: ${err.message}`);
        }
    }

    function seek(seconds) {
        const track = currentTrack();
        if (!track.ready) throw fail("NO_TRACK", "parça henüz yüklenmedi");
        const duration = track.audio.duration;
        let target = Math.max(0, Number(seconds) || 0);
        if (Number.isFinite(duration)) target = Math.min(target, Math.max(0, duration - END_MARGIN_SECONDS));
        track.audio.currentTime = target;
        return target;
    }

    function stop() {
        state.upload = null;
        discardPendingUrl();
        release(state.current);
        state.current = null;
    }

    function status() {
        const track = state.current;
        if (!track) {
            return { token: null, position: 0, duration: 0, paused: true, ended: false, error: null, loading: false };
        }
        return {
            token: track.token,
            position: finite(track.audio.currentTime),
            duration: finite(track.audio.duration),
            paused: track.audio.paused,
            ended: track.ended,
            error: track.error,
            loading: !track.ready,
        };
    }

    // ── Dosya aktarımı (CDP üzerinden base64 parçalar → Blob) ──
    // Meet sayfası http://127.0.0.1'e erişemez (Local Network Access), bu
    // yüzden dosya baytları sayfanın içine taşınıp blob URL'si olarak çalınır.

    // Aktarımı bitmiş ama çalınmamış blob'u bırakır (ör. Python tarafı play()'e gelmeden
    // iptal edildi); aksi hâlde dosya sekme kapanana kadar bellekte kalırdı.
    function discardPendingUrl() {
        if (!state.pendingUrl) return;
        URL.revokeObjectURL(state.pendingUrl);
        state.discarded.add(state.pendingUrl);
        state.pendingUrl = null;
    }

    function uploadBegin(token, mime) {
        discardPendingUrl();
        state.upload = { token, mime: String(mime || ""), parts: [], size: 0 };
    }

    function uploadChunk(token, b64) {
        const upload = state.upload;
        if (!upload || upload.token !== token) throw fail("SUPERSEDED", "dosya aktarımı iptal edildi");
        const binary = atob(b64);
        const bytes = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
        upload.parts.push(bytes);
        upload.size += bytes.length;
        return upload.size;
    }

    function uploadEnd(token) {
        const upload = state.upload;
        if (!upload || upload.token !== token) throw fail("SUPERSEDED", "dosya aktarımı iptal edildi");
        state.upload = null;
        if (!upload.size) throw fail("EMPTY", "dosya boş");
        state.pendingUrl = URL.createObjectURL(new Blob(upload.parts, { type: upload.mime }));
        return state.pendingUrl;
    }

    // ── getUserMedia yaması ─────────────────────────────────────
    // Ses istenirse müzik hattının bir klonu, görüntü de istenirse
    // Chrome'un (sahte) kamerası verilir. Her çağırana kendi izi.

    function patchGetUserMedia() {
        const proto = window.MediaDevices && window.MediaDevices.prototype;
        if (!proto || typeof proto.getUserMedia !== "function") {
            console.warn(TAG, "MediaDevices yok, getUserMedia yaması atlandı.");
            return;
        }
        const original = proto.getUserMedia;
        async function getUserMedia(constraints) {
            if (!constraints || !constraints.audio) return original.call(this, constraints);
            const { ctx, dest } = engine();
            wake(ctx);
            const stream = new MediaStream([dest.stream.getAudioTracks()[0].clone()]);
            if (constraints.video) {
                try {
                    const camera = await original.call(this, { video: constraints.video });
                    camera.getVideoTracks().forEach((videoTrack) => stream.addTrack(videoTrack));
                } catch (err) {
                    console.warn(TAG, "Kamera alınamadı, yalnızca ses veriliyor:", err);
                }
            }
            console.log(TAG, "🎤 Mikrofon isteği müzik hattına bağlandı.");
            return stream;
        }
        Object.defineProperty(proto, "getUserMedia", { value: getUserMedia, writable: true, configurable: true });
    }

    patchGetUserMedia();

    window.__meetbot = Object.freeze({
        uploadBegin,
        uploadChunk,
        uploadEnd,
        play,
        pause,
        resume,
        stop,
        seek,
        status,
        setMusicVolume(value) {
            state.musicVolume = clampVolume(value);
            if (state.engine) rampTo(state.engine.musicGain.gain, state.musicVolume / 100);
        },
        setMicVolume(value) {
            state.micVolume = clampVolume(value);
            if (state.engine) rampTo(state.engine.micGain.gain, state.micVolume / 100);
        },
        setNormalize(enabled) {
            state.normalize = Boolean(enabled);
            if (state.engine) wire();
        },
        // Teşhis ve testler için: Web Audio düğümleri (motor kurulmadıysa null).
        get engine() {
            return state.engine;
        },
    });
    console.log(TAG, "Enjeksiyon tamamlandı.");
})();
