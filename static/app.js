// ──────────────────────────────────────────────────────────────
//  app.js — MeetBot 3.2 web arayüzü
//
//  • WebSocket protokolü v2: hello/welcome el sıkışması, "rid" + "ack"
//    ile istek/yanıt eşleştirme, üstel geri çekilmeli yeniden bağlanma.
//  • Yetkiye göre arayüz: sunucunun gönderdiği "permissions" listesi
//    hangi kontrollerin açık olacağını belirler (asıl denetim sunucuda).
//  • Güvenlik: sunucudan gelen hiçbir veri HTML dizesi olarak yorumlanmaz; tüm
//    öğeler DOM API'leriyle (textContent / setAttribute) oluşturulur.
// ──────────────────────────────────────────────────────────────

(() => {
    "use strict";

    // ── Sabitler ──────────────────────────────────────────────
    const STORAGE_NAME = "meetbot_username";
    const STORAGE_TOKEN = "meetbot_admin_token";
    const NAME_MAX = 32;
    const QUERY_MAX = 500;
    // Sunucu bir bağlantının komutlarını GELİŞ SIRASIYLA yürütür: hafif bir komut (duraklat, taşı…) bile
    // botun toplantıdan ayrılmasının (~18 sn: ses durdur + "Ayrıl" + sekmeyi boşalt) ya da şarkı yüklenirken
    // oynatıcı kilidinin (bot.play, en fazla 45 sn) arkasında sıra bekleyebilir. Bu yüzden yanıt uzun
    // süre beklenir; SLOW_NOTICE_MS'yi aşınca hata değil "sırada" bilgisi gösterilir.
    const REQUEST_TIMEOUT_MS = 60000;
    const SLOW_NOTICE_MS = 15000;
    const HELLO_TIMEOUT_MS = 15000;   // el sıkışmada önde bekleyen iş yok: yanıt gelmezse sunucu takılmıştır
    const ADD_TIMEOUT_MS = 120000;
    // Doğası gereği uzun sürer ve bot durumu zaten görünür: gecikince "sırada" bildirimi gösterilmez
    const QUIET_SLOW_TYPES = new Set(["join_meet", "leave_meet"]);
    const RECONNECT_BASE_MS = 1000;
    const RECONNECT_MAX_MS = 30000;
    const PING_INTERVAL_MS = 25000;
    const PONG_TIMEOUT_MS = 10000;
    const VOLUME_THROTTLE_MS = 400;   // sunucu ~30 mesaj / 10 sn sınırı uygular (ping dahil)
    const MOVE_THROTTLE_MS = 500;     // basılı tutulan Alt+↓ / art arda tıklamalar tek hedefte birleşir
    const SEEK_DEBOUNCE_MS = 300;     // basılı tutulan ok tuşu tek bir "seek" mesajı üretsin
    const TOUCH_DRAG_PX = 10;         // dokunmatikte kaydırıcı ancak bu kadar yatay sürüklenince değer gönderir
    const TOAST_MAX = 4;
    const DRAG_MIME = "application/x-meetbot-track";
    const VIEW_FLUSH_MS = 400;        // bot ekranında basılan harfler toplu gönderilir (hız sınırı ~30 / 10 sn)
    const VIEW_SCROLL_MS = 300;
    const VIEW_TEXT_MAX = 500;
    const VIEW_IMAGE_RE = /^data:image\/(jpeg|png);base64,[A-Za-z0-9+/=]+$/;
    // Bot ekranı seçiliyken tarayıcıya doğrudan iletilen özel tuşlar (sunucudaki VIEW_KEYS ile aynı)
    const VIEW_KEYS = new Set(["Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown",
        "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown"]);
    const DEFAULT_TITLE = "MeetBot 3.2 — Vaporwave Dreamscape";
    const MEET_LINK_RE = /https:\/\/meet\.google\.com\/(lookup\/[A-Za-z0-9_-]+|[a-z]{3}-[a-z]{4}-[a-z]{3})/i;
    const MEET_CODE_RE = /^[a-z]{3}-[a-z]{4}-[a-z]{3}$/i;

    const REPEAT_NEXT = { off: "one", one: "all", all: "off" };
    const REPEAT_LABELS = { off: "Kapalı", one: "Şarkı", all: "Liste" };
    const BOT_LABELS = { disconnected: "Bağlı Değil", connecting: "Bağlanıyor…", connected: "Meet'te" };
    const BOT_BADGE_BASE = "flex items-center gap-2 px-3 py-1 text-sm font-bold uppercase tracking-tighter shadow-[4px_4px_0_#000]";
    const BOT_BADGE_VARIANTS = {
        disconnected: "bg-red-600 text-white",
        connecting: "bg-sunset text-black",
        connected: "bg-teal text-black",
    };
    const NOTICE_LEVELS = new Set(["info", "success", "warning", "error"]);


    // ── Durum ─────────────────────────────────────────────────
    const state = {
        queue: [],
        current: null,
        playback: { state: "idle", position: 0, duration: 0, repeat: "off" },
        volume: { music: 80, mic: 80 },
        micMuted: false,
        bot: { status: "disconnected", meet_link: null, detail: null },
        history: [],
        listeners: [],
        listenerCount: 0,
    };

    const session = {
        name: "",
        isAdmin: false,
        token: null,                  // bu bağlantının kullandığı yönetici jetonu
        permissions: new Set(),
        version: "",
        limits: {},
    };

    // phase: idle → connecting → handshake (hello gönderildi) → ready (welcome alındı)
    //        waiting (yeniden deneme zamanlayıcısı) | stopped (kullanıcı adı reddedildi)
    const conn = {
        ws: null,
        phase: "idle",
        attempt: 0,
        retryTimer: 0,
        retryAt: 0,
        countdownTimer: 0,
        pingTimer: 0,
        pongTimer: 0,
        hadOutage: false,
        sentToken: null,              // hello ile gönderilen jeton (yoksa null)
    };

    const pending = new Map();        // rid → { resolve, timer }
    let ridSeq = 0;

    const ui = {
        addPending: false,
        joinPending: false,
        adminPending: false,
        meetDirty: false,
        seek: { dragging: false, timer: 0, seq: 0 },   // konum kaydırıcısıyla etkileşim
        dragId: null,
        queueRenderDeferred: false,
        progressAt: performance.now(),
        lastEtaRender: 0,
        readdPending: new Set(),      // geçmişten yeniden eklenen şarkıların kimlikleri
        volumeBusy: { music: false, mic: false },
    };

    // Bot ekranı (yönetici): açık mı, hangi sekme, gönderilmeyi bekleyen yazı / kaydırma
    const view = {
        open: false,
        target: "meet",
        typed: "",
        flushTimer: 0,
        scroll: 0,
        scrollTimer: 0,
        returnFocus: null,
        backdropDown: false,
        interactive: false,           // girdi yalnızca (oturum açılmamış) Google girişi sekmesinde
        signedIn: null,
    };


    // ── DOM referansları ──────────────────────────────────────
    const $ = (id) => document.getElementById(id);
    const el = {
        app: $("app"),
        statue: $("deco-statue"),
        connBanner: $("conn-banner"),
        connBannerStatus: $("conn-banner-status"),
        connBannerCountdown: $("conn-banner-countdown"),
        connRetryBtn: $("conn-retry-btn"),

        loginOverlay: $("login-overlay"),
        loginForm: $("login-form"),
        loginInput: $("login-username"),
        loginError: $("login-error"),
        loginCancelBtn: $("login-cancel-btn"),

        adminOverlay: $("admin-modal-overlay"),
        adminForm: $("admin-form"),
        adminPassword: $("admin-password"),
        adminError: $("admin-error"),
        adminLoginBtn: $("admin-login-btn"),
        adminCancelBtn: $("admin-cancel-btn"),
        adminToggleBtn: $("admin-toggle-btn"),
        adminToggleIcon: $("admin-toggle-icon"),
        adminToggleText: $("admin-toggle-text"),
        userBadge: $("user-badge"),
        userBadgeName: $("user-badge-name"),

        viewBtn: $("view-btn"),
        viewOverlay: $("view-overlay"),
        viewDialog: $("view-dialog"),
        viewGoogle: $("view-google"),
        viewSignoutBtn: $("view-signout-btn"),
        viewReadonly: $("view-readonly"),
        viewCloseBtn: $("view-close-btn"),
        viewTabMeet: $("view-tab-meet"),
        viewTabLogin: $("view-tab-login"),
        viewBackBtn: $("view-back-btn"),
        viewReloadBtn: $("view-reload-btn"),
        viewUrl: $("view-url"),
        viewScreen: $("view-screen"),
        viewImg: $("view-img"),
        viewPlaceholder: $("view-placeholder"),
        viewStatus: $("view-status"),
        viewTypeForm: $("view-type-form"),
        viewText: $("view-text"),

        botBadge: $("bot-badge"),
        statusDot: $("status-dot"),
        statusText: $("status-text"),
        meetForm: $("meet-form"),
        meetInput: $("meet-link-input"),
        meetJoinBtn: $("meet-join-btn"),
        meetCancelBtn: $("meet-cancel-btn"),
        meetLeaveBtn: $("meet-leave-btn"),
        botDetail: $("bot-detail"),

        addForm: $("add-form"),
        addInput: $("yt-link-input"),
        addBtn: $("add-song-btn"),
        addSpinner: $("add-song-spinner"),
        addLabel: $("add-song-label"),
        addHint: $("add-hint"),

        npThumb: $("np-thumb"),
        npArtIcon: $("np-art-icon"),
        npTitle: $("np-title"),
        npRequester: $("np-requester"),
        npSeek: $("np-seek"),
        npStatus: $("np-status"),
        npTime: $("np-time"),
        npHint: $("np-hint"),

        btnRepeat: $("btn-repeat"),
        btnRepeatIcon: $("btn-repeat-icon"),
        repeatLabel: $("repeat-label"),
        btnStop: $("btn-stop"),
        btnPlayPause: $("btn-playpause"),
        btnPlayPauseIcon: $("btn-playpause-icon"),
        btnSkip: $("btn-skip"),

        queueHeading: $("queue-heading"),
        queueCount: $("queue-count"),
        queueEta: $("queue-eta"),
        queueList: $("queue-list"),
        queueEmpty: $("queue-empty"),
        btnShuffle: $("btn-shuffle"),
        btnClear: $("btn-clear"),

        btnMic: $("btn-toggle-mic"),
        micIcon: $("mic-icon"),
        micText: $("mic-text"),
        musicSlider: $("music-volume-slider"),
        musicValue: $("music-volume-value"),
        micSlider: $("mic-volume-slider"),
        micValue: $("mic-volume-value"),

        listenersCount: $("listeners-count"),
        listenersList: $("listeners-list"),
        historyHeading: $("history-heading"),
        historyList: $("history-list"),
        historyEmpty: $("history-empty"),

        appVersion: $("app-version"),
        toastContainer: $("toast-container"),
    };

    const volumeControls = {
        music: { slider: el.musicSlider, output: el.musicValue },
        mic: { slider: el.micSlider, output: el.micValue },
    };


    // ── Yardımcılar ───────────────────────────────────────────

    /** Güvenli öğe oluşturucu: metin daima textContent, öznitelikler setAttribute ile yazılır. */
    function h(tag, props = {}, ...children) {
        const node = document.createElement(tag);
        for (const [key, value] of Object.entries(props)) {
            if (value === null || value === undefined || value === false) continue;
            if (key === "class") node.className = value;
            else if (key === "text") node.textContent = String(value);
            else if (key === "dataset") Object.assign(node.dataset, value);
            else if (value === true) node.setAttribute(key, "");
            else node.setAttribute(key, String(value));
        }
        for (const child of children.flat()) {
            if (child === null || child === undefined || child === false) continue;
            node.append(child instanceof Node ? child : String(child));
        }
        return node;
    }

    function icon(name, extraClass = "") {
        return h("span", {
            class: `material-symbols-outlined ${extraClass}`.trim(),
            "aria-hidden": "true",
            text: name,
        });
    }

    /** Yalnızca https:// adreslerine izin ver (javascript:, data: vb. asla). */
    function httpsUrl(value) {
        if (typeof value !== "string" || !value) return null;
        try {
            const url = new URL(value);
            return url.protocol === "https:" ? url.href : null;
        } catch {
            return null;
        }
    }

    function asArray(value) {
        return Array.isArray(value) ? value : [];
    }

    function asObject(value) {
        return value && typeof value === "object" && !Array.isArray(value) ? value : {};
    }

    function asText(value) {
        return typeof value === "string" ? value : "";
    }

    function asNumber(value, fallback = 0) {
        const num = Number(value);
        return Number.isFinite(num) ? num : fallback;
    }

    function clampInt(value, min, max, fallback) {
        const num = Math.round(asNumber(value, fallback));
        return Math.min(max, Math.max(min, num));
    }

    function formatTime(seconds) {
        if (typeof seconds !== "number" || !Number.isFinite(seconds) || seconds < 0) return "--:--";
        const total = Math.floor(seconds);
        const hrs = Math.floor(total / 3600);
        const mins = Math.floor((total % 3600) / 60);
        const secs = String(total % 60).padStart(2, "0");
        return hrs > 0 ? `${hrs}:${String(mins).padStart(2, "0")}:${secs}` : `${mins}:${secs}`;
    }

    function trackDuration(track) {
        const text = asText(track.duration_str);
        if (text) return text;
        return typeof track.duration === "number" ? formatTime(track.duration) : "?";
    }

    function charCount(text) {
        return [...text].length;
    }

    /** Sunucu mesajı emoji ile başlamıyorsa başına işaret ekle. */
    function withEmoji(message, emoji) {
        return /^[\p{L}\p{N}]/u.test(message) ? `${emoji} ${message}` : message;
    }

    /** Kullanıcının yazdığını kanonik Meet linkine çevirir; geçersizse null. */
    function normalizeMeetLink(raw) {
        let text = asText(raw).trim();
        if (MEET_CODE_RE.test(text)) text = `https://meet.google.com/${text}`;
        else if (/^meet\.google\.com\//i.test(text)) text = `https://${text}`;
        const match = MEET_LINK_RE.exec(text);
        if (!match) return null;
        const path = match[1];
        const canonical = /^lookup\//i.test(path) ? `lookup/${path.slice("lookup/".length)}` : path.toLowerCase();
        return `https://meet.google.com/${canonical}`;
    }

    const storage = {
        get(key) {
            try {
                return localStorage.getItem(key);
            } catch (err) {
                console.warn("[storage] okunamadı:", key, err);
                return null;
            }
        },
        set(key, value) {
            try {
                localStorage.setItem(key, value);
            } catch (err) {
                console.warn("[storage] yazılamadı:", key, err);
            }
        },
        remove(key) {
            try {
                localStorage.removeItem(key);
            } catch (err) {
                console.warn("[storage] silinemedi:", key, err);
            }
        },
    };

    function isReady() {
        return conn.phase === "ready";
    }

    function can(type) {
        return session.permissions.has(type);
    }

    /** Bağlantı hazır VE oturumun bu mesaj türüne yetkisi var. */
    function canUse(type) {
        return isReady() && can(type);
    }

    /** Kendi eklediği şarkıyı herkes, diğerlerini yönetici ya da misafir kontrolü açıkken kaldırabilir. */
    function mayRemove(track) {
        if (!can("remove")) return false;
        if (track.added_by === session.name) return true;
        return session.isAdmin || asObject(session.limits).guest_controls === true;
    }

    function botInMeeting() {
        return state.bot.status === "connected";
    }

    /**
     * Kapalı kontrole nedenini açıklayan ipucu ekler: önce yetki, sonra verilen ek neden
     * (ör. "Bot toplantıda değil"). Bağlantı yokken ipucu gösterilmez; bant zaten görünür.
     */
    function setControlHint(control, type, reason = "") {
        let hint = "";
        if (isReady()) hint = can(type) ? reason : "Bu işlem için yönetici yetkisi gerekli";
        if (hint) control.title = hint;
        else control.removeAttribute("title");
    }

    function safeRender(...renderers) {
        for (const render of renderers) {
            try {
                render();
            } catch (err) {
                console.error(`[UI] ${render.name || "render"} çizilemedi:`, err);
            }
        }
    }


    // ── Bildirimler (toast) ───────────────────────────────────
    const toastTimers = new Map();

    function toast(message, level = "info") {
        const text = asText(message).trim();
        if (!text) return;
        const kind = NOTICE_LEVELS.has(level) ? level : "info";

        // Aynı mesaj zaten görünüyorsa yenisini açma, sayacı artır
        const existing = [...el.toastContainer.children].find((node) => node.dataset.message === text);
        if (existing) {
            const counter = existing.querySelector(".toast-count");
            const count = Number(existing.dataset.count || "1") + 1;
            existing.dataset.count = String(count);
            counter.textContent = `×${count}`;
            counter.hidden = false;
            scheduleToastRemoval(existing, text);
            return;
        }

        while (el.toastContainer.children.length >= TOAST_MAX) {
            removeToast(el.toastContainer.firstElementChild);
        }

        const node = h("div", { class: `toast toast-${kind}`, dataset: { message: text, count: "1" } },
            h("span", { class: "toast-text", text }),
            h("span", { class: "toast-count", hidden: true }),
        );
        const close = h("button", { type: "button", class: "toast-close", "aria-label": "Bildirimi kapat" }, icon("close"));
        close.addEventListener("click", () => removeToast(node));
        node.append(close);
        el.toastContainer.append(node);
        scheduleToastRemoval(node, text);
    }

    function scheduleToastRemoval(node, text) {
        clearTimeout(toastTimers.get(node));
        const lifetime = Math.min(9000, 3500 + text.length * 40);
        toastTimers.set(node, setTimeout(() => removeToast(node), lifetime));
    }

    function removeToast(node) {
        if (!node) return;
        clearTimeout(toastTimers.get(node));
        toastTimers.delete(node);
        node.remove();
    }


    // ── WebSocket bağlantısı ──────────────────────────────────

    function connect() {
        clearTimeout(conn.retryTimer);
        clearInterval(conn.countdownTimer);
        conn.retryTimer = 0;
        conn.countdownTimer = 0;
        if (!session.name) return;

        const scheme = location.protocol === "https:" ? "wss:" : "ws:";
        let ws;
        try {
            ws = new WebSocket(`${scheme}//${location.host}/ws`);
        } catch (err) {
            console.error("[WS] Bağlantı oluşturulamadı:", err);
            scheduleReconnect();
            return;
        }
        conn.ws = ws;
        setPhase("connecting");

        ws.addEventListener("open", () => {
            if (ws !== conn.ws) return;
            setPhase("handshake");
            sendHello();
        });
        ws.addEventListener("message", (event) => {
            if (ws === conn.ws) onSocketMessage(event.data);
        });
        // "error" olayını ayrıca dinlemeye gerek yok: tarayıcı ardından her zaman "close" gönderir
        ws.addEventListener("close", (event) => {
            if (ws === conn.ws) onSocketLost(`kod ${event.code}`);
        });
    }

    /** Mevcut soketi bırakır (olaylarını yok sayar) ve kapatır. */
    function detachSocket(code = 1000, reason = "") {
        const ws = conn.ws;
        conn.ws = null;
        stopHeartbeat();
        if (ws && ws.readyState <= WebSocket.OPEN) {
            try {
                ws.close(code, reason);
            } catch (err) {
                console.warn("[WS] Soket kapatılamadı:", err);
            }
        }
    }

    function onSocketLost(reason) {
        console.warn(`[WS] Bağlantı kapandı (${reason})`);
        if (isReady()) {
            // Bağlantı yokken ilerleme çubuğu tahminle ilerlemesin: son bilinen konumda dondur
            state.playback.position = livePosition();
            ui.progressAt = performance.now();
        }
        // Devre dışı kalan kaydırıcı "change" olayı üretmez: yarım kalan sürüklemeleri bitir,
        // yeniden bağlanınca sunucunun değerleri görünsün
        endSeekInteraction();
        ui.volumeBusy.music = false;
        ui.volumeBusy.mic = false;
        cancelPlannedMoves();   // bayat sıraya göre hesaplanmış taşımalar yeniden bağlanınca gönderilmesin
        if (view.open) {
            discardViewInput();
            setText(el.viewStatus, "⚠️ Sunucu bağlantısı koptu, yeniden bağlanınca bot ekranı sürecek…");
        }
        detachSocket();
        failPending("Sunucu bağlantısı koptu", true);
        if (conn.phase === "stopped") {
            safeRender(renderConnection);
            return;
        }
        conn.hadOutage = true;
        scheduleReconnect();
    }

    function scheduleReconnect() {
        clearTimeout(conn.retryTimer);
        const base = Math.min(RECONNECT_MAX_MS, RECONNECT_BASE_MS * 2 ** conn.attempt);
        const delay = Math.round(base * (0.8 + Math.random() * 0.4));   // ±%20 sapma
        conn.attempt += 1;
        conn.retryAt = Date.now() + delay;
        conn.retryTimer = setTimeout(connect, delay);
        clearInterval(conn.countdownTimer);
        conn.countdownTimer = setInterval(() => safeRender(renderConnection), 1000);
        setPhase("waiting");
    }

    /** Bekleme süresini atlayıp hemen bağlan ("Şimdi dene" butonu). */
    function retryNow() {
        if (conn.phase === "waiting") connect();
    }

    /** Ağ geri gelince / sekmeye dönülünce: bekliyorsak hemen bağlan, bağlıysak bağlantıyı yokla. */
    function checkConnection() {
        if (conn.phase === "waiting") retryNow();
        else sendPing();
    }

    /** Kullanıcı adı değişince: soketi kapatıp sıfırdan bağlan. */
    function restartConnection() {
        detachSocket(1000, "yeniden bağlanılıyor");
        failPending("Bağlantı yenilendi", true);
        conn.attempt = 0;
        connect();
    }

    function setPhase(phase) {
        conn.phase = phase;
        safeRender(renderConnection, renderAllControls);
    }

    function rawSend(message) {
        const ws = conn.ws;
        if (!ws || ws.readyState !== WebSocket.OPEN) return false;
        ws.send(JSON.stringify(message));
        return true;
    }

    /**
     * rid'li istek gönderir; sunucunun "ack" yanıtıyla çözülür.
     * Asla reddetmez: { ok, message, data, offline?, timedOut? } döner.
     */
    function request(type, payload = {}, { timeout = REQUEST_TIMEOUT_MS } = {}) {
        return new Promise((resolve) => {
            const allowed = type === "hello" ? conn.phase === "handshake" : isReady();
            if (!allowed) {
                resolve({ ok: false, message: "Sunucu bağlantısı yok", data: null, offline: true });
                return;
            }
            ridSeq += 1;
            const rid = `r${ridSeq}`;
            const timer = setTimeout(() => {
                settle(rid, { ok: false, message: "Sunucu zamanında yanıt vermedi", data: null, timedOut: true });
            }, timeout);
            pending.set(rid, { resolve, timer });
            if (!rawSend({ ...payload, type, rid })) {
                settle(rid, { ok: false, message: "Sunucu bağlantısı yok", data: null, offline: true });
            }
        });
    }

    function settle(rid, result) {
        const entry = pending.get(rid);
        if (!entry) return false;
        pending.delete(rid);
        clearTimeout(entry.timer);
        entry.resolve(result);
        return true;
    }

    function failPending(message, offline = false) {
        for (const rid of [...pending.keys()]) {
            settle(rid, { ok: false, message, data: null, offline });
        }
    }

    /**
     * Kullanıcı eylemi: hata olursa bildirim gösterir (bağlantı yoksa bant zaten görünür).
     * announce=false → başarı mesajı gösterilmez (ör. "add": sunucu herkese notice yayınlar).
     */
    async function command(type, payload = {}, { announce = true, ...options } = {}) {
        // Yanıt gecikirse (sunucu önceki işlemleri bitiriyor) kullanıcı bir şey olmadı sanmasın;
        // kendi bekleme göstergesi olan işlemler (ekleme: özel zaman aşımı, katıl/ayrıl) hariç
        const noticeTimer = options.timeout === undefined && !QUIET_SLOW_TYPES.has(type)
            ? setTimeout(() => toast("⏳ Sunucu önceki işlemleri bitiriyor, isteğin sırada…", "info"), SLOW_NOTICE_MS)
            : 0;
        const result = await request(type, payload, options);
        clearTimeout(noticeTimer);
        if (!result.ok) {
            if (!result.offline) toast(withEmoji(result.message || "İşlem başarısız oldu", "❌"), "error");
        } else if (result.message && announce) {
            toast(withEmoji(result.message, "ℹ️"), "info");
        }
        return result;
    }

    function sendHello() {
        const token = storage.get(STORAGE_TOKEN) || null;
        conn.sentToken = token;
        const payload = { name: session.name };
        if (token) payload.token = token;
        const ws = conn.ws;
        request("hello", payload, { timeout: HELLO_TIMEOUT_MS }).then((result) => {
            if (result.ok || result.offline || conn.phase !== "handshake" || ws !== conn.ws) return;
            // Yanıt gelmediyse sunucu takılmıştır: isim reddi değil, bağlantı sorunu gibi yeniden dene
            if (result.timedOut) onSocketLost("hello yanıtı gelmedi");
            else onHelloRejected(result.message);
        });
    }

    /** Saklanan jetonu yalnızca hâlâ bu oturumun jetonuysa sil (başka sekme yeni giriş yapmış olabilir). */
    function forgetToken(token) {
        if (token && storage.get(STORAGE_TOKEN) === token) storage.remove(STORAGE_TOKEN);
    }

    function onHelloRejected(message) {
        console.warn("[WS] hello reddedildi:", message);
        conn.phase = "stopped";
        detachSocket(1000, "hello reddedildi");
        failPending("Bağlantı kapatıldı", true);
        showLogin({ error: message || "Kullanıcı adı kabul edilmedi" });
        safeRender(renderConnection, renderAllControls);
    }

    function startHeartbeat() {
        stopHeartbeat();
        conn.pingTimer = setInterval(sendPing, PING_INTERVAL_MS);
    }

    function stopHeartbeat() {
        clearInterval(conn.pingTimer);
        clearTimeout(conn.pongTimer);
        conn.pingTimer = 0;
        conn.pongTimer = 0;
    }

    /** Uykudan dönen dizüstü / değişen Wi-Fi gibi "yarı açık" bağlantıları yakalar. */
    function sendPing() {
        if (!isReady() || conn.pongTimer) return;
        if (!rawSend({ type: "ping" })) return;
        conn.pongTimer = setTimeout(() => {
            conn.pongTimer = 0;
            onSocketLost("pong yanıtı gelmedi");
        }, PONG_TIMEOUT_MS);
    }

    /** Sunucudan gelen her çerçeve bağlantının canlı olduğunu kanıtlar. */
    function markAlive() {
        clearTimeout(conn.pongTimer);
        conn.pongTimer = 0;
    }


    // ── Sunucu mesajları ──────────────────────────────────────

    function onSocketMessage(raw) {
        // Yalnızca "pong" beklenmez: hız sınırına takılan ping "pong" yerine "error" alır ve
        // sağlam bir bağlantı "pong gelmedi" diye koparılırdı
        markAlive();
        let msg;
        try {
            msg = JSON.parse(raw);
        } catch (err) {
            console.error("[WS] Geçersiz JSON alındı:", err);
            return;
        }
        if (!msg || typeof msg !== "object" || typeof msg.type !== "string") {
            console.warn("[WS] Türü olmayan mesaj yok sayıldı:", msg);
            return;
        }
        if (!Object.hasOwn(handlers, msg.type)) {
            console.debug("[WS] Bilinmeyen mesaj türü yok sayıldı:", msg.type);
            return;
        }
        try {
            handlers[msg.type](msg);
        } catch (err) {
            console.error(`[WS] "${msg.type}" mesajı işlenemedi:`, err);
        }
    }

    const handlers = {
        welcome(msg) {
            if (typeof msg.name === "string" && msg.name) session.name = msg.name;
            session.version = asText(msg.version);
            session.limits = asObject(msg.limits);
            setSessionRights(msg.is_admin, msg.permissions);
            session.token = session.isAdmin ? conn.sentToken : null;
            if (conn.sentToken && !session.isAdmin) {
                forgetToken(conn.sentToken);
                toast("🔒 Yönetici oturumunun süresi dolmuş, lütfen tekrar giriş yapın", "warning");
            }

            const reconnected = conn.hadOutage;
            conn.hadOutage = false;
            conn.attempt = 0;
            conn.phase = "ready";
            startHeartbeat();
            // Uygulama zaten görünür (init / submitLogin); açık olabilecek "isim değiştir" penceresi kapatılmaz
            applySnapshot(asObject(msg.state));
            safeRender(renderConnection, renderVersion, renderLimits);
            if (reconnected) toast("✅ Sunucu bağlantısı yeniden kuruldu", "success");
            if (view.open) {
                if (canUse("view_start")) startView(view.target);
                else closeView();
            }
        },

        session(msg) {
            setSessionRights(msg.is_admin, msg.permissions);
            if (!session.isAdmin) {
                forgetToken(session.token);
                session.token = null;
            }
            renderAll();
        },

        state(msg) {
            // Anlık görüntü doğrudan mesajda ya da "state" alanında olabilir
            applySnapshot(msg.state && typeof msg.state === "object" ? msg.state : msg);
        },

        queue(msg) {
            state.queue = asArray(msg.queue);
            safeRender(renderQueue, renderQueueMeta, renderTransport, renderNowPlaying);
        },

        playback(msg) {
            setCurrent(msg.current);
            applyPlayback(msg);
            safeRender(renderNowPlaying, renderTransport, renderQueueMeta);
        },

        progress(msg) {
            if (!state.current) return;   // durdurulmuş şarkının geç gelen ilerlemesi
            const previous = state.playback.state;
            applyPlayback(msg);
            if (state.playback.state !== previous) safeRender(renderNowPlaying, renderTransport);
            else safeRender(renderProgress);
        },

        volume(msg) {
            state.volume.music = clampInt(msg.music, 0, 100, state.volume.music);
            state.volume.mic = clampInt(msg.mic, 0, 100, state.volume.mic);
            safeRender(renderVolume);
        },

        mic(msg) {
            state.micMuted = msg.muted === true;
            safeRender(renderMic);
        },

        bot(msg) {
            applyBot(msg);
            // Kuyruktaki "Şimdi çal" butonları da botun toplantıda olup olmadığına bağlı
            safeRender(renderBot, renderNowPlaying, renderTransport, renderQueue);
        },

        history(msg) {
            state.history = asArray(msg.history);
            safeRender(renderHistory);
        },

        listeners(msg) {
            applyListeners(msg.listeners, msg.count);
            safeRender(renderListeners);
        },

        notice(msg) {
            toast(msg.message, msg.level);
        },

        view_frame(msg) {
            applyViewFrame(msg);
        },

        view_error(msg) {
            if (view.open) setText(el.viewStatus, `⚠️ ${asText(msg.message) || "Bot ekranı alınamadı"}`);
        },

        view_closed(msg) {
            if (!view.open) return;
            setText(el.viewStatus, `⚠️ ${asText(msg.message) || "Bot ekranı kapandı"} — bir sekme seçerek yeniden açabilirsin`);
        },

        ack(msg) {
            const result = {
                ok: msg.ok === true,
                message: typeof msg.message === "string" && msg.message ? msg.message : null,
                data: msg.data ?? null,
            };
            if (!settle(String(msg.rid), result)) console.debug("[WS] Beklenmeyen ack:", msg.rid);
        },

        error(msg) {
            const message = asText(msg.message) || "Sunucu hatası";
            if (conn.phase === "handshake") onHelloRejected(message);
            else toast(withEmoji(message, "❌"), "error");
        },

        pong() {
            markAlive();   // onSocketMessage zaten yaptı; açıklık için
        },
    };

    function setSessionRights(isAdmin, permissions) {
        session.isAdmin = isAdmin === true;
        session.permissions = new Set(asArray(permissions).filter((item) => typeof item === "string"));
    }

    function setCurrent(track) {
        const previousId = state.current ? state.current.id : null;
        state.current = track && typeof track === "object" ? track : null;
        // Şarkı değiştiyse önceki şarkı için yarım kalan konum seçimi yeni şarkıya uygulanmasın
        if ((state.current ? state.current.id : null) !== previousId) endSeekInteraction();
    }

    function applySnapshot(snap) {
        state.queue = asArray(snap.queue);
        setCurrent(snap.current);
        applyPlayback(asObject(snap.playback));
        const volume = asObject(snap.volume);
        state.volume.music = clampInt(volume.music, 0, 100, state.volume.music);
        state.volume.mic = clampInt(volume.mic, 0, 100, state.volume.mic);
        state.micMuted = snap.mic_muted === true;
        applyBot(asObject(snap.bot));
        state.history = asArray(snap.history);
        applyListeners(snap.listeners);
        renderAll();
    }

    function applyPlayback(msg) {
        const pb = state.playback;
        if (["idle", "loading", "playing", "paused"].includes(msg.state)) pb.state = msg.state;
        if (["off", "one", "all"].includes(msg.repeat)) pb.repeat = msg.repeat;
        pb.position = Math.max(0, asNumber(msg.position, pb.position));
        pb.duration = Math.max(0, asNumber(msg.duration, pb.duration));
        ui.progressAt = performance.now();
    }

    function applyBot(msg) {
        const previousLink = state.bot.meet_link;
        state.bot = {
            status: Object.hasOwn(BOT_LABELS, msg.status) ? msg.status : "disconnected",
            meet_link: asText(msg.meet_link) || null,
            detail: asText(msg.detail) || null,
        };
        // Toplantı değiştiyse yeni linki göster; ama kullanıcının o an yazmakta olduğu metni ezme
        if (state.bot.meet_link !== previousLink && document.activeElement !== el.meetInput) ui.meetDirty = false;
    }

    function applyListeners(list, count) {
        state.listeners = asArray(list).filter((name) => typeof name === "string");
        state.listenerCount = Number.isInteger(count) ? count : state.listeners.length;
    }


    // ── Çizim (render) ────────────────────────────────────────

    function renderAll() {
        safeRender(renderConnection, renderSession, renderVersion, renderLimits, renderBot, renderAddForm,
            renderNowPlaying, renderTransport, renderQueue, renderQueueMeta, renderVolume, renderMic,
            renderHistory, renderListeners);
    }

    /** Bağlantı / yetki değişince yalnızca kontrollerin açık-kapalı durumu. */
    function renderAllControls() {
        safeRender(renderSession, renderBot, renderAddForm, renderTransport, renderProgress, renderQueue,
            renderQueueMeta, renderVolume, renderMic, renderHistory);
    }

    function renderConnection() {
        const waiting = conn.phase === "waiting";
        const reconnecting = (conn.phase === "connecting" || conn.phase === "handshake") && conn.attempt > 0;
        const visible = waiting || reconnecting;
        el.connBanner.hidden = !visible;
        document.body.classList.toggle("is-offline", visible);
        el.connRetryBtn.hidden = !waiting;
        if (waiting) {
            const seconds = Math.max(1, Math.ceil((conn.retryAt - Date.now()) / 1000));
            setText(el.connBannerStatus, "Sunucu bağlantısı koptu");
            el.connBannerCountdown.textContent = ` — ${seconds} sn içinde yeniden denenecek…`;
        } else if (reconnecting) {
            setText(el.connBannerStatus, "Sunucuya yeniden bağlanılıyor…");
            el.connBannerCountdown.textContent = "";
        }
    }

    /** Canlı bölgelerde aynı metni yeniden yazıp gereksiz duyuru tetikleme. */
    function setText(node, text) {
        if (node.textContent !== text) node.textContent = text;
    }

    function renderSession() {
        el.userBadgeName.textContent = session.name || "--";
        el.adminToggleBtn.disabled = !isReady();
        if (session.isAdmin) {
            el.adminToggleIcon.textContent = "lock_open";
            el.adminToggleText.textContent = "Yönetici";
            el.adminToggleBtn.classList.add("text-fuchsia");
            el.adminToggleBtn.setAttribute("aria-label", "Yönetici oturumu açık — çıkış yap");
            el.adminToggleBtn.removeAttribute("aria-haspopup");
        } else {
            el.adminToggleIcon.textContent = "lock";
            el.adminToggleText.textContent = "Admin";
            el.adminToggleBtn.classList.remove("text-fuchsia");
            el.adminToggleBtn.setAttribute("aria-label", "Yönetici girişi");
            el.adminToggleBtn.setAttribute("aria-haspopup", "dialog");
        }
        renderViewButton();
    }

    /** Bot ekranı düğmesi yalnızca yöneticiye görünür; yetki yoksa nedeni ipucunda yazar. */
    function renderViewButton() {
        el.viewBtn.hidden = !session.isAdmin;
        const allowed = canUse("view_start");
        el.viewBtn.disabled = !allowed;
        if (allowed || !isReady()) {
            el.viewBtn.title = "Botun tarayıcısını gör ve kullan (ör. Google girişi)";
        } else if (asObject(session.limits).remote_view === "off") {
            el.viewBtn.title = "Bot ekranı kapalı (MEETBOT_REMOTE_VIEW=off)";
        } else {
            el.viewBtn.title = "Güvenlik için yalnızca sunucunun kendisinden ya da SSH tüneliyle açılır: "
                + "ssh -L 8000:127.0.0.1:8000 kullanıcı@sunucu → http://localhost:8000";
        }
        // Yetki gittiyse (çıkış yapıldı / başka sekmede oturum kapandı) açık pencere kapansın
        if (view.open && isReady() && !can("view_start")) closeView();
    }

    function renderVersion() {
        el.appVersion.textContent = session.version ? `v${session.version}` : "v3.2";
    }

    function renderLimits() {
        const limits = asObject(session.limits);
        const parts = [];
        if (limits.playlist_limit > 0) parts.push(`Liste: en fazla ${limits.playlist_limit} şarkı`);
        if (limits.max_duration > 0) parts.push(`Süre: en fazla ${formatLimitDuration(limits.max_duration)}`);
        if (limits.max_user_queue > 0) parts.push(`Kişi başı: ${limits.max_user_queue} şarkı`);
        el.addHint.textContent = parts.join(" · ");
        el.addHint.hidden = parts.length === 0;
    }

    /**
     * MEETBOT_MAX_DURATION saniyedir ve tam dakika olmak zorunda değildir: yuvarlanırsa ipucu sunucunun
     * reddiyle çelişir ("en fazla 2 dk" derken 100 sn'lik şarkı reddedilir, 20 sn için "0 dk").
     */
    function formatLimitDuration(seconds) {
        return Number.isInteger(seconds) && seconds % 60 === 0 ? `${seconds / 60} dk` : formatTime(seconds);
    }

    function renderBot() {
        const { status, meet_link: link, detail } = state.bot;
        el.botBadge.className = `${BOT_BADGE_BASE} ${BOT_BADGE_VARIANTS[status]}`;
        el.statusDot.classList.toggle("animate-pulse", status === "connecting");
        setText(el.statusText, BOT_LABELS[status]);   // canlı bölge: yalnızca değişince yaz
        el.botBadge.title = detail ? `Bot: ${BOT_LABELS[status]} — ${detail}` : `Bot: ${BOT_LABELS[status]}`;

        const canJoin = canUse("join_meet");
        const canLeave = canUse("leave_meet");
        const connecting = status === "connecting";
        el.meetInput.readOnly = !canJoin || connecting || ui.joinPending;
        el.meetInput.placeholder = canJoin ? "https://meet.google.com/abc-defg-hij" : "Bot bir toplantıda değil";
        if (!ui.meetDirty) el.meetInput.value = link || "";

        el.meetJoinBtn.hidden = !can("join_meet") || connecting;
        el.meetJoinBtn.disabled = !canJoin || ui.joinPending;
        el.meetJoinBtn.textContent = status === "connected" ? "Değiştir" : "Katıl";
        el.meetCancelBtn.hidden = !can("leave_meet") || !connecting;
        el.meetCancelBtn.disabled = !canLeave;
        el.meetLeaveBtn.hidden = !can("leave_meet") || status !== "connected";
        el.meetLeaveBtn.disabled = !canLeave;

        el.botDetail.textContent = detail || "";
        el.botDetail.hidden = !detail;
    }

    function renderAddForm() {
        const allowed = canUse("add");
        el.addBtn.disabled = ui.addPending || !allowed;
        el.addSpinner.hidden = !ui.addPending;
        el.addLabel.textContent = ui.addPending ? "Ekleniyor…" : "Ekle";
        el.addForm.setAttribute("aria-busy", ui.addPending ? "true" : "false");
        setControlHint(el.addBtn, "add");
    }

    function renderNowPlaying() {
        const track = state.current;
        const status = state.playback.state;
        const botReady = botInMeeting();

        el.npTitle.classList.toggle("animate-pulse", status === "loading");
        if (!track) {
            el.npTitle.textContent = status === "loading" ? "Yükleniyor…" : "Şarkı çalmıyor";
            el.npTitle.removeAttribute("title");
            el.npRequester.hidden = true;
            el.npThumb.hidden = true;
            el.npThumb.removeAttribute("src");
            delete el.npThumb.dataset.failed;
            el.npArtIcon.hidden = false;
            el.npStatus.hidden = true;
            el.npHint.textContent = state.queue.length && !botReady
                ? "🤖 Bot toplantıya katılınca kuyruk çalmaya başlayacak"
                : "";
            el.npHint.hidden = !el.npHint.textContent;
            document.title = DEFAULT_TITLE;
            renderProgress();
            return;
        }

        const title = asText(track.title) || "Bilinmeyen şarkı";
        el.npTitle.textContent = title;
        el.npTitle.title = title;
        const requester = asText(track.added_by);
        el.npRequester.textContent = requester ? `İsteyen: ${requester}` : "";
        el.npRequester.hidden = !requester;

        const thumb = httpsUrl(track.thumbnail);
        if (thumb && el.npThumb.getAttribute("src") !== thumb) {
            delete el.npThumb.dataset.failed;
            el.npThumb.src = thumb;
        }
        // Küçük resim yüklenemediyse (bkz. bindEvents) boş kutu yerine albüm ikonu kalır
        const showThumb = Boolean(thumb) && !el.npThumb.dataset.failed;
        el.npThumb.hidden = !showThumb;
        el.npArtIcon.hidden = showThumb;

        const statusView = {
            playing: ["play_arrow", "Oynatılıyor", ""],
            paused: ["pause", "Duraklatıldı", ""],
            loading: ["progress_activity", "Yükleniyor", "animate-spin"],
            idle: ["stop", "Beklemede", ""],
        }[status];
        el.npStatus.replaceChildren(icon(statusView[0], `text-base ${statusView[2]}`.trim()), statusView[1]);
        el.npStatus.hidden = false;

        el.npHint.textContent = (status === "paused" || status === "loading") && !botReady
            ? "🤖 Bot toplantıya katılınca kaldığı yerden devam edecek"
            : "";
        el.npHint.hidden = !el.npHint.textContent;

        const titleIcon = { playing: "▶", loading: "⏳" }[status] || "❚❚";
        document.title = `${titleIcon} ${title} — MeetBot 3.2`;
        renderProgress();
    }

    function livePosition() {
        const pb = state.playback;
        if (pb.state !== "playing" || !isReady()) return pb.position;
        const elapsed = (performance.now() - ui.progressAt) / 1000;
        const pos = pb.position + elapsed;
        return pb.duration > 0 ? Math.min(pb.duration, pos) : pos;
    }

    function currentDuration() {
        if (state.playback.duration > 0) return state.playback.duration;
        return state.current && typeof state.current.duration === "number" ? state.current.duration : 0;
    }

    function renderProgress() {
        const seek = el.npSeek;
        if (!state.current) {
            seek.max = "0";
            seek.value = "0";
            seek.disabled = true;
            seek.style.setProperty("--fill", "0%");
            seek.removeAttribute("aria-valuetext");
            el.npTime.textContent = "--:-- / --:--";
            return;
        }
        const duration = currentDuration();
        const status = state.playback.state;
        seek.max = String(Math.max(1, Math.round(duration)));
        if (!ui.seek.dragging) seek.value = String(Math.round(livePosition()));
        const shown = Number(seek.value);
        seek.style.setProperty("--fill", duration > 0 ? `${Math.min(100, (shown / duration) * 100)}%` : "0%");
        const text = `${formatTime(shown)} / ${duration > 0 ? formatTime(duration) : "--:--"}`;
        el.npTime.textContent = text;
        seek.setAttribute("aria-valuetext", text);
        seek.disabled = !canUse("seek") || !(duration > 0) || !(status === "playing" || status === "paused");
    }

    function renderTransport() {
        const status = state.playback.state;
        const hasCurrent = Boolean(state.current);
        const playing = status === "playing";
        const loading = status === "loading";

        const playPause = el.btnPlayPause;
        if (playing) {
            el.btnPlayPauseIcon.textContent = "pause";
            playPause.setAttribute("aria-label", "Duraklat");
            playPause.disabled = !canUse("pause");
            setControlHint(playPause, "pause");
        } else {
            const resumable = status === "paused" || (status === "idle" && (hasCurrent || state.queue.length > 0));
            const botReady = botInMeeting();
            el.btnPlayPauseIcon.textContent = loading ? "progress_activity" : "play_arrow";
            playPause.setAttribute("aria-label", loading ? "Yükleniyor" : "Oynat");
            // Sunucu bot toplantıda değilken "resume"u reddeder; bot katılınca kaldığı yerden kendisi devam eder
            playPause.disabled = loading || !resumable || !botReady || !canUse("resume");
            setControlHint(playPause, "resume", resumable && !botReady ? "Bot toplantıda değil" : "");
        }
        el.btnPlayPauseIcon.classList.toggle("animate-spin", loading);

        // Yükleme sırasında da durdurulabilir / geçilebilir (takılan indirme iptal edilebilsin)
        el.btnStop.disabled = !canUse("stop") || (status === "idle" && !hasCurrent);
        setControlHint(el.btnStop, "stop");
        el.btnSkip.disabled = !canUse("skip") || (!hasCurrent && !loading);
        setControlHint(el.btnSkip, "skip");

        const mode = state.playback.repeat;
        el.btnRepeat.dataset.mode = mode;
        el.btnRepeatIcon.textContent = mode === "one" ? "repeat_one" : "repeat";
        el.btnRepeat.setAttribute("aria-label", `Tekrar modu: ${REPEAT_LABELS[mode]} (değiştir)`);
        el.btnRepeat.disabled = !canUse("repeat");
        setControlHint(el.btnRepeat, "repeat");
        el.repeatLabel.textContent = `Tekrar: ${REPEAT_LABELS[mode]}`;
    }

    function renderQueueMeta() {
        const queue = state.queue;
        el.queueCount.textContent = String(queue.length);
        const maxQueue = asObject(session.limits).max_queue;
        el.queueCount.title = maxQueue > 0 ? `Kuyrukta ${queue.length} / ${maxQueue} şarkı` : `Kuyrukta ${queue.length} şarkı`;

        el.btnShuffle.disabled = !canUse("shuffle") || queue.length < 2;
        setControlHint(el.btnShuffle, "shuffle");
        el.btnClear.hidden = !can("clear");
        el.btnClear.disabled = !canUse("clear") || queue.length === 0;

        if (!queue.length && !state.current) {
            el.queueEta.textContent = "Kuyruk boş";
            return;
        }
        let total = 0;
        let unknown = 0;
        for (const track of queue) {
            if (track.status === "error") continue;   // sırası gelince atlanır, süreye eklenmez
            if (typeof track.duration === "number" && track.duration > 0) total += track.duration;
            else unknown += 1;
        }
        const duration = currentDuration();
        if (state.current && duration > 0) total += Math.max(0, duration - livePosition());
        const endsAt = new Date(Date.now() + total * 1000).toLocaleTimeString("tr-TR", { hour: "2-digit", minute: "2-digit" });
        let text = `⏱ Kalan ${formatTime(total)} · bitiş ≈ ${endsAt}`;
        if (unknown) text += ` (+${unknown} süresi bilinmeyen)`;
        el.queueEta.textContent = text;
    }

    function renderQueue() {
        if (ui.dragId !== null) {
            // Sürükleme sürerken listeyi yeniden kurma; bırakınca çizilecek
            ui.queueRenderDeferred = true;
            return;
        }
        const list = el.queueList;
        const focus = captureFocus(list);
        const queue = state.queue;
        list.replaceChildren(...queue.map((track, index) => buildQueueItem(track, index, queue.length)));
        list.hidden = queue.length === 0;
        el.queueEmpty.hidden = queue.length > 0;
        restoreFocus(list, focus, el.queueHeading);
    }

    function buildQueueItem(track, index, total) {
        const title = asText(track.title) || "Bilinmeyen şarkı";
        // Butonlar yetkiye göre görünür; bağlantı yokken yerinde kalır ama devre dışıdır
        const offline = !isReady();
        const actions = [];
        if (can("play_now")) {
            // Sunucu bot toplantıda değilken "play_now"ı reddeder
            const botReady = botInMeeting();
            actions.push(actionButton("play_now", track.id, "play_arrow", `Şimdi çal: ${title}`,
                botReady ? "Şimdi çal" : "Şimdi çal (bot toplantıda değil)", offline || !botReady));
        }
        if (can("move")) {
            actions.push(actionButton("up", track.id, "arrow_upward", `Yukarı taşı: ${title}`, "Yukarı taşı (Alt+↑)",
                offline || index === 0));
            actions.push(actionButton("down", track.id, "arrow_downward", `Aşağı taşı: ${title}`, "Aşağı taşı (Alt+↓)",
                offline || index === total - 1));
        }
        if (mayRemove(track)) {
            actions.push(actionButton("remove", track.id, "delete", `Kuyruktan kaldır: ${title}`, "Kaldır", offline, "qi-remove"));
        }

        return h("li", {
            class: actions.length ? "queue-item" : "queue-item queue-item--static",
            dataset: { id: String(track.id) },
            draggable: canUse("move") ? "true" : null,
        },
            h("span", { class: "qi-handle", "aria-hidden": "true", title: "Sürükleyerek taşı" }, icon("drag_indicator")),
            h("span", { class: "qi-index text-teal font-vt text-2xl w-7 text-center select-none", text: String(index + 1) }),
            buildThumb(track, "qi-thumb w-16 sm:w-20"),
            h("div", { class: "qi-body" },
                h("p", { class: "font-display text-lg font-bold leading-tight line-clamp-2 break-words tracking-wide", title, text: title }),
                h("p", { class: "text-sunset font-mono text-sm flex flex-wrap items-center gap-x-3 gap-y-0.5 opacity-90" },
                    h("span", { class: "flex items-center gap-1" }, icon("schedule", "text-sm"), trackDuration(track)),
                    h("span", { class: "flex items-center gap-1 min-w-0" }, icon("person", "text-sm"),
                        h("span", { class: "truncate max-w-[10rem]", text: asText(track.added_by) || "?" })),
                    trackStatusBadge(track.status),
                ),
            ),
            actions.length ? h("div", { class: "qi-actions" }, actions) : null,
        );
    }

    function actionButton(action, id, iconName, label, tooltip, disabled = false, extraClass = "") {
        return h("button", {
            type: "button",
            class: `icon-btn ${extraClass}`.trim(),
            dataset: { action, id: String(id) },
            "aria-label": label,
            title: tooltip,
            disabled,
        }, icon(iconName, "text-2xl"));
    }

    function trackStatusBadge(status) {
        if (status === "downloading") {
            return h("span", { class: "flex items-center gap-1 text-teal", title: "İndiriliyor" },
                icon("download", "text-sm animate-pulse"), "İNDİRİLİYOR");
        }
        if (status === "ready") {
            return h("span", { class: "flex items-center text-teal/80", title: "İndirildi, çalmaya hazır" },
                icon("check_circle", "text-sm"), h("span", { class: "sr-only", text: "Hazır" }));
        }
        if (status === "error") {
            return h("span", { class: "flex items-center gap-1 text-red-400", title: "İndirilemedi, sırası gelince atlanacak" },
                icon("error", "text-sm"), "HATA");
        }
        return null;
    }

    function buildThumb(track, className) {
        const box = h("span", { class: `${className} relative block aspect-video overflow-hidden border border-teal/50 bg-black/60 shrink-0` },
            icon("music_note", "absolute inset-0 flex items-center justify-center text-teal/60"));
        const src = httpsUrl(track.thumbnail);
        if (src) {
            const img = h("img", {
                src,
                alt: "",
                loading: "lazy",
                decoding: "async",
                referrerpolicy: "no-referrer",
                class: "absolute inset-0 w-full h-full object-cover",
            });
            img.addEventListener("error", () => img.remove(), { once: true });
            box.append(img);
        }
        return box;
    }

    /** Liste yeniden kurulurken klavye odağını aynı şarkının aynı butonunda tut. */
    function captureFocus(container) {
        const active = document.activeElement;
        if (!active || !container.contains(active) || !active.dataset.action) return null;
        const row = active.closest("li");
        return {
            action: active.dataset.action,
            id: active.dataset.id,
            index: Math.max(0, [...container.children].indexOf(row)),
        };
    }

    function enabledActions(scope) {
        return [...scope.querySelectorAll("button[data-action]")].filter((btn) => !btn.disabled);
    }

    /**
     * Odak aynı şarkının butonuna döner. Şarkı listeden çıktıysa (kaldırıldı, "Şimdi çal", çalmaya
     * başladı) aynı sıradaki satıra, o yoksa en yakın üst satıra geçilir; liste boşaldıysa başlığa.
     * Yoksa eski düğümle birlikte odak <body>'ye düşer ve klavye kullanıcısı yerini kaybeder.
     */
    function restoreFocus(container, focus, fallback) {
        if (!focus) return;
        let buttons = enabledActions(container).filter((btn) => btn.dataset.id === focus.id);
        if (!buttons.length) {
            const rows = [...container.children];
            const start = Math.min(focus.index, rows.length - 1);
            const nearest = [...rows.slice(start), ...rows.slice(0, Math.max(0, start)).reverse()];
            buttons = nearest.map(enabledActions).find((list) => list.length) || [];
        }
        // Aynı buton artık kapalıysa (ör. en üste taşındı) aynı satırdaki başka butona geç
        const target = buttons.find((btn) => btn.dataset.action === focus.action) || buttons[0] || fallback;
        if (target) target.focus();
    }

    function renderVolume() {
        for (const [target, { slider, output }] of Object.entries(volumeControls)) {
            slider.disabled = !canUse("volume");
            setControlHint(slider, "volume");
            if (ui.volumeBusy[target]) continue;   // kullanıcı sürüklerken sunucu değeriyle ezme
            const value = state.volume[target];
            slider.value = String(value);
            output.textContent = `${value}%`;
            paintRange(slider);
        }
    }

    function paintRange(slider) {
        const max = Number(slider.max) || 100;
        slider.style.setProperty("--fill", `${(Number(slider.value) / max) * 100}%`);
    }

    function renderMic() {
        const muted = state.micMuted;
        el.btnMic.setAttribute("aria-pressed", muted ? "true" : "false");
        el.micIcon.textContent = muted ? "mic_off" : "mic";
        el.micText.textContent = muted ? "Mikrofon KAPALI" : "Mikrofon AÇIK";
        el.micText.classList.toggle("glitch-text", muted);
        el.btnMic.disabled = !canUse("mic");
        setControlHint(el.btnMic, "mic");
    }

    function renderHistory() {
        const list = el.historyList;
        const focus = captureFocus(list);
        const allowed = can("add");
        const offline = !isReady();
        list.replaceChildren(...state.history.map((track) => {
            const title = asText(track.title) || "Bilinmeyen şarkı";
            const id = String(track.id);
            const busy = ui.readdPending.has(id);
            return h("li", { class: "flex items-center gap-3 p-2 bg-black/60 border border-teal/30" },
                buildThumb(track, "w-14"),
                h("div", { class: "min-w-0 flex-1" },
                    h("p", { class: "font-display font-bold truncate", title, text: title }),
                    h("p", { class: "text-sunset text-sm font-mono truncate", text: `${trackDuration(track)} · ${asText(track.added_by) || "?"}` }),
                ),
                // Beklerken "disabled" yerine aria-disabled: klavye odağı butonda kalsın
                // (tekrarlanan tıklamaları readdFromHistory yok sayar)
                allowed ? h("button", {
                    type: "button",
                    class: "icon-btn w-10 h-10 border border-teal/40 bg-black/30 shrink-0",
                    dataset: { action: "readd", id },
                    "aria-label": `Kuyruğa tekrar ekle: ${title}`,
                    "aria-disabled": busy ? "true" : null,
                    "aria-busy": busy ? "true" : null,
                    title: busy ? "Ekleniyor…" : "Kuyruğa tekrar ekle",
                    disabled: offline,
                }, icon(busy ? "progress_activity" : "playlist_add", busy ? "animate-spin" : "")) : null,
            );
        }));
        list.hidden = state.history.length === 0;
        el.historyEmpty.hidden = state.history.length > 0;
        restoreFocus(list, focus, el.historyHeading);
    }

    function renderListeners() {
        el.listenersCount.textContent = String(state.listenerCount);
        el.listenersList.replaceChildren(...state.listeners.map((name) => {
            const isMe = name === session.name;
            return h("li", {
                class: isMe
                    ? "px-2 py-0.5 border border-fuchsia bg-black/60 text-fuchsia font-display text-lg max-w-full truncate"
                    : "px-2 py-0.5 border border-teal/60 bg-black/60 text-teal font-display text-lg max-w-full truncate",
                title: name,
            }, name, isMe ? " (sen)" : "");
        }));
        if (!state.listeners.length) {
            el.listenersList.append(h("li", { class: "opacity-70 text-lg", text: "Şu an kimse yok" }));
        }
    }


    // ── Kullanıcı adı girişi ──────────────────────────────────

    function showApp() {
        el.app.hidden = false;
        el.loginOverlay.hidden = true;
    }

    function showLogin({ error = "", changing = false } = {}) {
        el.loginOverlay.hidden = false;
        el.loginInput.value = session.name;
        el.loginError.textContent = error;
        el.loginCancelBtn.hidden = !changing;
        el.loginInput.focus();
        el.loginInput.select();
    }

    function submitLogin(event) {
        event.preventDefault();
        const name = el.loginInput.value.trim();
        if (!name) {
            el.loginError.textContent = "Kullanıcı adı boş olamaz!";
            return;
        }
        if (charCount(name) > NAME_MAX) {
            el.loginError.textContent = `Kullanıcı adı en fazla ${NAME_MAX} karakter olabilir`;
            return;
        }
        const changed = name !== session.name;
        session.name = name;
        storage.set(STORAGE_NAME, name);
        el.loginError.textContent = "";
        showApp();
        safeRender(renderSession);
        if (changed || !conn.ws) restartConnection();
    }


    // ── Yönetici girişi ───────────────────────────────────────
    let adminReturnFocus = null;
    let adminBackdropDown = false;

    function openAdminModal() {
        adminReturnFocus = document.activeElement;
        el.adminPassword.value = "";
        el.adminError.textContent = "";
        el.adminOverlay.hidden = false;
        el.adminPassword.focus();
    }

    function closeAdminModal() {
        el.adminOverlay.hidden = true;
        if (adminReturnFocus && typeof adminReturnFocus.focus === "function") adminReturnFocus.focus();
        adminReturnFocus = null;
    }

    async function submitAdmin(event) {
        event.preventDefault();
        if (ui.adminPending) return;
        const password = el.adminPassword.value.trim();
        if (!password) {
            el.adminError.textContent = "Şifre boş olamaz!";
            return;
        }
        ui.adminPending = true;
        el.adminLoginBtn.disabled = true;
        const result = await request("auth", { password });
        ui.adminPending = false;
        el.adminLoginBtn.disabled = false;
        if (!result.ok) {
            el.adminError.textContent = result.message || "Hatalı şifre!";
            el.adminPassword.select();
            return;
        }
        const token = asObject(result.data).token;
        if (typeof token === "string" && token) {
            session.token = token;
            storage.set(STORAGE_TOKEN, token);
        }
        closeAdminModal();
        toast("🔓 Yönetici girişi başarılı", "success");
    }

    async function logout() {
        if (!confirm("Yönetici oturumu kapatılsın mı?")) return;
        const token = session.token;
        const result = await command("logout", {}, { announce: false });
        if (result.ok) {
            forgetToken(token);   // sunucu "session" mesajıyla da bildirir; ikisi de zararsız
            toast("🔒 Yönetici oturumu kapatıldı", "info");
        }
    }

    /** Diyalog içinde Tab ile dolaşmayı hapseder, Esc ile kapatır. */
    function trapDialogKeys(event, dialog, onEscape) {
        if (event.key === "Escape" && onEscape) {
            event.preventDefault();
            onEscape();
            return;
        }
        if (event.key !== "Tab") return;
        const focusables = [...dialog.querySelectorAll("button, input")]
            .filter((node) => !node.disabled && node.offsetParent !== null);
        if (!focusables.length) return;
        const first = focusables[0];
        const last = focusables[focusables.length - 1];
        if (event.shiftKey && document.activeElement === first) {
            event.preventDefault();
            last.focus();
        } else if (!event.shiftKey && document.activeElement === last) {
            event.preventDefault();
            first.focus();
        }
    }


    // ── Bot ekranı (yönetici) ─────────────────────────────────
    //
    // Sunucu saniyede ~2 kare (JPEG) yollar; tıklamalar görüntüye göre 0–1 oranı olarak,
    // basılan harfler VIEW_FLUSH_MS'de bir toplu "type" olarak gider. Yazılanlar (şifre
    // olabilir) hiçbir yere kaydedilmez ve konsola yazılmaz.

    function openView() {
        if (view.open || !canUse("view_start")) return;
        view.open = true;
        view.returnFocus = document.activeElement;
        el.viewImg.hidden = true;
        el.viewImg.removeAttribute("src");
        el.viewPlaceholder.hidden = false;
        setText(el.viewPlaceholder, "Bot ekranı yükleniyor… (tarayıcı kapalıysa açılması birkaç saniye sürer)");
        setText(el.viewStatus, "");
        setText(el.viewUrl, "");
        renderViewGoogle(null);
        renderViewInteractive(false);
        el.viewOverlay.hidden = false;
        el.viewScreen.focus();
        startView(view.target);
    }

    async function startView(target) {
        view.target = target;
        renderViewTabs();
        if (target !== "login") renderViewInteractive(false);
        const result = await command("view_start", { target }, { announce: false });
        if (!view.open) return;
        if (result.ok) {
            setText(el.viewStatus, "");
        } else if (!result.offline) {
            setText(el.viewStatus, `⚠️ ${result.message || "Bot ekranı açılamadı"}`);
        }
    }

    /** Girdi yalnızca giriş sekmesinde: Meet sekmesinde metin kutusu / tuşlar gizlenir, tıklamalar gitmez. */
    function renderViewInteractive(interactive) {
        view.interactive = interactive;
        if (!interactive) discardViewInput();
        el.viewTypeForm.hidden = !interactive;
        el.viewReadonly.hidden = interactive;
        el.viewBackBtn.disabled = !interactive;
        el.viewReloadBtn.disabled = !interactive;
        el.viewScreen.dataset.interactive = String(interactive);
    }

    async function signOutGoogle() {
        if (!confirm("Bot Google hesabından çıkış yapsın mı? Tekrar kullanmak için yeniden giriş yapman gerekir.")) return;
        const result = await command("google_logout", {}, { announce: false });
        if (result.ok && view.open) setText(el.viewStatus, "🔓 Botun Google hesabından çıkış yapıldı");
    }

    function closeView() {
        if (!view.open) return;
        view.open = false;
        discardViewInput();
        el.viewOverlay.hidden = true;
        el.viewImg.removeAttribute("src");   // son kareyi bellekte tutma
        el.viewText.value = "";
        if (isReady()) request("view_stop");  // yanıtı beklenmez; bağlantı yoksa sunucu zaten unuttu
        const focus = view.returnFocus;
        view.returnFocus = null;
        if (focus && typeof focus.focus === "function" && document.contains(focus)) focus.focus();
    }

    function renderViewTabs() {
        for (const [button, target] of [[el.viewTabMeet, "meet"], [el.viewTabLogin, "login"]]) {
            button.setAttribute("aria-pressed", String(view.target === target));
        }
    }

    function renderViewGoogle(signedIn) {
        const labels = { yes: "Google: oturum açık ✓", no: "Google: oturum kapalı", unknown: "Google: ?" };
        const key = signedIn === true ? "yes" : signedIn === false ? "no" : "unknown";
        view.signedIn = signedIn;
        el.viewGoogle.dataset.state = key;
        setText(el.viewGoogle, labels[key]);
        // Hesap bağlıyken giriş sekmesi açılamaz (hesap ayarlarına erişilmesin); çıkış düğmesi görünür
        el.viewTabLogin.disabled = signedIn === true;
        el.viewTabLogin.title = signedIn === true
            ? "Google hesabı bağlı. Başka bir hesap için önce 'Hesabı çıkar'."
            : "Botun Google hesabına giriş";
        el.viewSignoutBtn.hidden = signedIn !== true;
    }

    function applyViewFrame(msg) {
        if (!view.open) return;
        const image = typeof msg.image === "string" ? msg.image : "";
        if (!VIEW_IMAGE_RE.test(image)) {
            console.warn("[Bot ekranı] Geçersiz kare yok sayıldı");
            return;
        }
        el.viewImg.src = image;
        el.viewImg.hidden = false;
        el.viewPlaceholder.hidden = true;
        const url = asText(msg.url);
        setText(el.viewUrl, url);
        el.viewUrl.title = asText(msg.title) || url;
        const wasLogin = view.target === "login";
        if (msg.target === "meet" || msg.target === "login") {
            view.target = msg.target;
            renderViewTabs();
        }
        const signedIn = msg.signed_in === true ? true : msg.signed_in === false ? false : null;
        renderViewGoogle(signedIn);
        renderViewInteractive(msg.interactive === true && view.target === "login");
        if (el.viewStatus.textContent.startsWith("⚠️")) setText(el.viewStatus, "");
        if (wasLogin && view.target === "meet" && signedIn === true) {
            setText(el.viewStatus, "✅ Google hesabı bağlandı; giriş sekmesi güvenlik için kapatıldı. Artık Meet'e katılabilirsin.");
        }
    }

    function sendViewInput(payload) {
        if (!view.open || !isReady() || !view.interactive) return;
        command("view_input", payload, { announce: false });
    }

    function discardViewInput() {
        clearTimeout(view.flushTimer);
        clearTimeout(view.scrollTimer);
        view.typed = "";
        view.scroll = 0;
        view.flushTimer = 0;
        view.scrollTimer = 0;
    }

    function flushViewTyping() {
        clearTimeout(view.flushTimer);
        view.flushTimer = 0;
        while (view.typed) {
            const chunk = [...view.typed].slice(0, VIEW_TEXT_MAX).join("");
            view.typed = view.typed.slice(chunk.length);
            sendViewInput({ action: "type", text: chunk });
        }
    }

    function queueViewText(text) {
        if (!text || !view.interactive) return;
        view.typed += text;
        if (!view.flushTimer) view.flushTimer = setTimeout(flushViewTyping, VIEW_FLUSH_MS);
    }

    function sendViewKey(key) {
        flushViewTyping();   // önce yazılanlar gitsin: "şifre" + Enter sırası korunur
        sendViewInput({ action: "key", key });
    }

    function onViewClick(event) {
        const rect = el.viewImg.getBoundingClientRect();
        if (el.viewImg.hidden || !rect.width || !rect.height) return;
        const x = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
        const y = Math.min(1, Math.max(0, (event.clientY - rect.top) / rect.height));
        flushViewTyping();
        sendViewInput({ action: "click", x, y });
        el.viewScreen.focus();
    }

    function onViewWheel(event) {
        if (el.viewImg.hidden) return;
        event.preventDefault();
        view.scroll += event.deltaMode === 1 ? event.deltaY * 40 : event.deltaY;
        if (view.scrollTimer) return;
        view.scrollTimer = setTimeout(() => {
            const dy = Math.round(view.scroll);
            view.scroll = 0;
            view.scrollTimer = 0;
            if (dy) sendViewInput({ action: "scroll", dy });
        }, VIEW_SCROLL_MS);
    }

    function onViewKeydown(event) {
        if (event.isComposing || !view.interactive) return;
        const ctrl = event.ctrlKey || event.metaKey;
        if (ctrl && event.key.toLowerCase() === "a") {
            event.preventDefault();
            sendViewKey("Control+a");
        } else if (event.key === "Tab" && event.shiftKey && !ctrl && !event.altKey) {
            event.preventDefault();
            sendViewKey("Shift+Tab");
        } else if (VIEW_KEYS.has(event.key) && !ctrl && !event.altKey) {
            event.preventDefault();
            event.stopPropagation();   // Esc bu pencereyi değil, bottaki sayfayı etkilesin
            sendViewKey(event.key);
        } else if ([...event.key].length === 1 && !ctrl && !event.altKey) {
            event.preventDefault();
            queueViewText(event.key);
        }
    }

    function onViewPaste(event) {
        if (!view.interactive) return;
        const text = event.clipboardData ? event.clipboardData.getData("text") : "";
        if (!text) return;
        event.preventDefault();
        queueViewText(text);
        flushViewTyping();
    }

    function submitViewText(event) {
        event.preventDefault();
        const text = el.viewText.value;
        if (!text) return;
        el.viewText.value = "";
        queueViewText(text);
        flushViewTyping();
        el.viewText.focus();
    }


    // ── Meet kontrolleri ──────────────────────────────────────

    async function submitMeet(event) {
        event.preventDefault();
        if (!canUse("join_meet") || ui.joinPending || state.bot.status === "connecting") return;
        const link = normalizeMeetLink(el.meetInput.value);
        if (!link) {
            toast("⚠️ Geçerli bir Google Meet linki girin (ör. https://meet.google.com/abc-defg-hij)", "warning");
            return;
        }
        const connected = state.bot.status === "connected";
        if (connected && link === normalizeMeetLink(state.bot.meet_link)) {
            ui.meetDirty = false;
            safeRender(renderBot);
            toast("ℹ️ Bot zaten bu toplantıda", "info");
            return;
        }
        if (connected && !confirm("Bot mevcut toplantıdan ayrılıp yeni toplantıya katılacak. Devam edilsin mi?")) return;

        ui.joinPending = true;
        safeRender(renderBot);
        const result = await command("join_meet", { link });
        ui.joinPending = false;
        if (result.ok) {
            el.meetInput.value = link;   // kullanıcının yazdığını kanonik linkle değiştir
            ui.meetDirty = false;
        }
        safeRender(renderBot);
    }

    async function leaveMeet({ confirmFirst }) {
        if (confirmFirst && !confirm("Bot Meet görüşmesinden ayrılsın mı?")) return;
        await command("leave_meet");
    }


    // ── Şarkı ekleme ──────────────────────────────────────────

    async function submitAdd(event) {
        event.preventDefault();
        if (ui.addPending || !canUse("add")) return;
        const query = el.addInput.value.trim();
        if (!query) {
            toast("⚠️ Bir YouTube linki ya da şarkı adı yazın", "warning");
            el.addInput.focus();
            return;
        }
        if (charCount(query) > QUERY_MAX) {
            toast(`⚠️ Arama en fazla ${QUERY_MAX} karakter olabilir`, "warning");
            return;
        }
        ui.addPending = true;
        safeRender(renderAddForm);
        const result = await command("add", { query }, { timeout: ADD_TIMEOUT_MS, announce: false });
        ui.addPending = false;
        // Yalnızca başarıda temizle; hata olursa kullanıcı linki yeniden yapıştırmak zorunda kalmasın
        if (result.ok && el.addInput.value.trim() === query) el.addInput.value = "";
        safeRender(renderAddForm);
    }

    /** Geçmiş satırları kimlikle eşlenir: bekleme sırasında geçmiş kayarsa yanlış satır dönmesin. */
    async function readdFromHistory(id) {
        const track = state.history.find((item) => String(item.id) === id);
        const query = track && httpsUrl(track.url);
        if (!query || ui.readdPending.has(id)) return;
        ui.readdPending.add(id);
        safeRender(renderHistory);
        await command("add", { query }, { timeout: ADD_TIMEOUT_MS, announce: false });
        ui.readdPending.delete(id);
        safeRender(renderHistory);
    }


    // ── Kuyruk eylemleri (butonlar, klavye, sürükle-bırak) ────

    function queueIndexOf(id) {
        return state.queue.findIndex((track) => String(track.id) === String(id));
    }

    // Taşımalar sıraya girer: yolda en fazla bir "move" olur ve MOVE_THROTTLE_MS'de birden fazla gitmez.
    // Beklerken gelen basışlar (basılı tutulan Alt+↓, art arda tıklamalar) şarkı başına tek hedefte
    // birleşir. Yoksa sunucunun hız sınırı (30 mesaj / 10 sn) dolar; kalp atışı ping'i de reddedilir.
    const moves = {
        planned: new Map(),   // şarkı kimliği (metin) → istenen sıra (henüz gönderilmedi)
        sending: null,        // yoldaki istek: { key, index }
        timer: 0,
        lastSentAt: -Infinity,
    };

    /**
     * Şarkının kullanıcının istediği son konumu. Sunucu yeni sırayı yayınlamadan (ack'ten önce gelir)
     * yerel kuyruk eskidir; göreli taşımalar (↑/↓) gönderilen / planlanan hedeften devam eder.
     */
    function plannedIndex(id) {
        const key = String(id);
        if (moves.planned.has(key)) return moves.planned.get(key);
        if (moves.sending && moves.sending.key === key) return moves.sending.index;
        return queueIndexOf(id);
    }

    function moveTrack(id, index) {
        if (queueIndexOf(id) < 0) return;
        const target = Math.min(state.queue.length - 1, Math.max(0, index));
        if (target === plannedIndex(id)) return;
        moves.planned.set(String(id), target);
        pumpMoves();
    }

    function pumpMoves() {
        if (!canUse("move")) {
            cancelPlannedMoves();
            return;
        }
        if (moves.sending || moves.timer || !moves.planned.size) return;
        const wait = MOVE_THROTTLE_MS - (performance.now() - moves.lastSentAt);
        if (wait > 0) {
            moves.timer = setTimeout(() => {
                moves.timer = 0;
                pumpMoves();
            }, wait);
            return;
        }
        for (const [key, index] of moves.planned) {
            moves.planned.delete(key);
            const from = queueIndexOf(key);
            if (from < 0 || from === index) continue;   // şarkı kuyruktan çıktı ya da zaten orada
            moves.sending = { key, index };
            moves.lastSentAt = performance.now();
            command("move", { id: state.queue[from].id, index }).then(() => {
                moves.sending = null;
                pumpMoves();
            });
            return;
        }
    }

    function cancelPlannedMoves() {
        clearTimeout(moves.timer);
        moves.timer = 0;
        moves.planned.clear();
    }

    function onQueueClick(event) {
        const button = event.target.closest("button[data-action]");
        if (!button || button.disabled) return;
        const { action, id } = button.dataset;
        const from = queueIndexOf(id);
        if (from < 0) return;
        const trackId = state.queue[from].id;
        if (action === "play_now") command("play_now", { id: trackId });
        else if (action === "up") moveTrack(id, plannedIndex(id) - 1);
        else if (action === "down") moveTrack(id, plannedIndex(id) + 1);
        else if (action === "remove") command("remove", { id: trackId });
    }

    function onQueueKeydown(event) {
        if (!event.altKey || (event.key !== "ArrowUp" && event.key !== "ArrowDown") || !canUse("move")) return;
        const item = event.target.closest(".queue-item");
        if (!item) return;
        event.preventDefault();
        const id = item.dataset.id;
        moveTrack(id, plannedIndex(id) + (event.key === "ArrowUp" ? -1 : 1));
    }

    function clearDropMarkers() {
        for (const item of el.queueList.querySelectorAll("[data-drop]")) delete item.dataset.drop;
    }

    function isOwnDrag(event) {
        return ui.dragId !== null && event.dataTransfer && [...event.dataTransfer.types].includes(DRAG_MIME);
    }

    function onDragStart(event) {
        const item = event.target.closest && event.target.closest(".queue-item");
        if (!item || !canUse("move")) {
            event.preventDefault();
            return;
        }
        ui.dragId = item.dataset.id;
        event.dataTransfer.effectAllowed = "move";
        event.dataTransfer.setData(DRAG_MIME, item.dataset.id);
        item.classList.add("is-dragging");
    }

    function onDragOver(event) {
        if (!isOwnDrag(event)) return;   // dışarıdan gelen link / metin bırakılamaz
        const item = event.target.closest(".queue-item");
        if (!item) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = "move";
        const rect = item.getBoundingClientRect();
        const position = event.clientY > rect.top + rect.height / 2 ? "after" : "before";
        if (item.dataset.drop !== position) {
            clearDropMarkers();
            item.dataset.drop = position;
        }
    }

    function onDragLeave(event) {
        if (!el.queueList.contains(event.relatedTarget)) clearDropMarkers();
    }

    function onDrop(event) {
        if (!isOwnDrag(event)) return;
        event.preventDefault();
        const item = event.target.closest(".queue-item");
        const sourceId = ui.dragId;
        clearDropMarkers();
        if (!item) return;
        const from = queueIndexOf(sourceId);
        const target = queueIndexOf(item.dataset.id);
        if (from < 0 || target < 0) return;
        const rect = item.getBoundingClientRect();
        const after = event.clientY > rect.top + rect.height / 2;
        let index = target + (after ? 1 : 0);
        if (from < index) index -= 1;   // sunucu önce çıkarıp sonra araya ekler
        moveTrack(sourceId, index);
    }

    function onDragEnd() {
        ui.dragId = null;
        clearDropMarkers();
        for (const item of el.queueList.querySelectorAll(".is-dragging")) item.classList.remove("is-dragging");
        if (ui.queueRenderDeferred) {
            ui.queueRenderDeferred = false;
            safeRender(renderQueue);
        }
    }


    // ── Kaydırıcılarda işaretçi: kaydırma mı, sürükleme mi? ───

    /**
     * Dokunmatik ekranda kaydırıcının üstünden başlayan dikey kaydırmayı tarayıcı devralır
     * ("pointercancel"; .vapor-range touch-action: pan-y). Ama Blink parmağın değdiği yere atlayıp
     * "input", parmak kalkınca da "change" gönderir: sayfayı kaydıran kullanıcı toplantıdaki herkes
     * için şarkıyı sardırır ya da sesi değiştirirdi. Bu yüzden:
     *   • dokunma/kalem yatay sürüklemeye dönüşene ya da bırakılana kadar "kararsızdır" (undecided);
     *     o sürede değer gönderilmez (fare ile basış hemen kesinleşir),
     *   • iptal edilen hareketten sonra gelen "input"/"change" yok sayılır (stale).
     * onRelease(changed): değer basıştakiyle aynıysa tarayıcı "change" GÖNDERMEZ; etkileşim yine bitmeli.
     */
    function watchRangeGesture(slider, { onStart = () => {}, onCommit = () => {}, onRelease, onCancel }) {
        const gesture = {
            id: null, x: 0, y: 0, dx: 0, dy: 0, startValue: slider.value, committed: true, cancelled: false,
        };

        const cancel = () => {
            gesture.id = null;
            gesture.committed = true;
            gesture.cancelled = true;
            onCancel(gesture.startValue);
        };

        slider.addEventListener("pointerdown", (event) => {
            if (!event.isPrimary) return;
            gesture.id = event.pointerId;
            gesture.x = event.clientX;
            gesture.y = event.clientY;
            gesture.dx = 0;
            gesture.dy = 0;
            gesture.startValue = slider.value;
            gesture.committed = event.pointerType === "mouse";
            gesture.cancelled = false;
            onStart();
        });
        slider.addEventListener("pointermove", (event) => {
            if (event.pointerId !== gesture.id || gesture.committed) return;
            gesture.dx = Math.max(gesture.dx, Math.abs(event.clientX - gesture.x));
            gesture.dy = Math.max(gesture.dy, Math.abs(event.clientY - gesture.y));
            if (gesture.dx >= TOUCH_DRAG_PX && gesture.dx > gesture.dy) {
                gesture.committed = true;
                onCommit();
            }
        });
        // Fare kaydırıcının dışında bırakılabilir: pencere düzeyinde (yakalama aşamasında) dinle
        window.addEventListener("pointerup", (event) => {
            if (event.pointerId !== gesture.id) return;
            // Kararsız dokunma dikey kaydıysa bu bir kaydırma denemesiydi (kaydırılacak yer olmasa da)
            if (!gesture.committed && gesture.dy >= TOUCH_DRAG_PX) {
                cancel();
                return;
            }
            gesture.id = null;
            gesture.committed = true;
            onRelease(slider.value !== gesture.startValue);
        }, true);
        window.addEventListener("pointercancel", (event) => {
            if (event.pointerId === gesture.id) cancel();
        }, true);
        // İptal edilen dokunmanın "change"i parmak kalkınca (touchend'in varsayılan işleminde) gelir;
        // ondan sonra bayrak iner ki sonraki etkileşimler (ör. ekran okuyucu) yok sayılmasın
        const settle = () => {
            if (gesture.cancelled) setTimeout(() => { gesture.cancelled = false; }, 0);
        };
        slider.addEventListener("touchend", settle, { passive: true });
        slider.addEventListener("touchcancel", settle, { passive: true });
        slider.addEventListener("keydown", () => {
            gesture.cancelled = false;
        });

        return {
            /** Dokunma sürüyor ama kaydırma mı sürükleme mi henüz belli değil. */
            get undecided() {
                return gesture.id !== null && !gesture.committed;
            },
            /** Olay, tarayıcının kaydırma için devraldığı hareketin artığı. */
            get stale() {
                return gesture.cancelled;
            },
            get active() {
                return gesture.id !== null;
            },
            get startValue() {
                return gesture.startValue;
            },
        };
    }


    // ── Ses kaydırıcıları (kısıtlı gönderim) ──────────────────

    function setupVolumeSlider(target) {
        const { slider, output } = volumeControls[target];
        let lastSentAt = -Infinity;
        let lastSentValue = null;
        let lastRequest = Promise.resolve();
        let inputSeq = 0;
        let finalPending = false;
        let timer = 0;
        let sentCount = 0;
        let sentAtGestureStart = 0;

        const showValue = () => {
            output.textContent = `${slider.value}%`;
            paintRange(slider);
        };

        const gesture = watchRangeGesture(slider, {
            onStart: () => {
                sentAtGestureStart = sentCount;
            },
            // Dokunma yatay sürüklemeye dönüştü: bekletilen değer artık gönderilebilir
            onCommit: () => {
                if (ui.volumeBusy[target]) schedule(false);
            },
            // "change" gelmeyebilir (aynı değere geri dönüldü): son değer yine gönderilir, kaydırıcı serbest kalır
            onRelease: () => {
                if (ui.volumeBusy[target]) schedule(true);
            },
            // Tarayıcı hareketi kaydırmaya çevirdi: kaydırıcı basıştan önceki değere döner
            onCancel: (startValue) => {
                if (!ui.volumeBusy[target]) return;
                slider.value = startValue;
                showValue();
                // Bu hareket sırasında bir değer gittiyse ya da önceki bırakmanın son değeri sıradaysa onu gönder
                if (timer || sentCount !== sentAtGestureStart) {
                    schedule(true);
                } else {
                    ui.volumeBusy[target] = false;
                    safeRender(renderVolume);
                }
            },
        });

        const send = async (final) => {
            // Kararsız dokunmada parmağın değdiği değer değil, basıştan önceki değer geçerlidir
            // (önceki bırakmanın kısıtlamaya takılan son değeri bu sırada gönderilebilir)
            const value = Number(gesture.undecided ? gesture.startValue : slider.value);
            if (value !== lastSentValue) {
                lastSentValue = value;
                lastSentAt = performance.now();
                sentCount += 1;
                lastRequest = command("volume", { target, value });
            }
            if (!final) return;
            // Sunucu "volume" yayınını ack'ten ÖNCE gönderir: son istek yanıtlanınca state.volume
            // sunucudaki gerçek değerdir (istek reddedildiyse eskisi). Kaydırıcı artık onu gösterir.
            const seq = inputSeq;
            await lastRequest;
            // Beklerken yeni sürükleme başladı ya da parmak hâlâ kaydırıcıda: onun bırakılması bitirecek
            if (seq !== inputSeq || gesture.active) return;
            ui.volumeBusy[target] = false;
            safeRender(renderVolume);
        };

        // Sürükleme ya da basılı tutulan ok tuşu en fazla VOLUME_THROTTLE_MS'de bir mesaj üretir;
        // bırakınca ("change") son değer yine kısıtlamaya uyarak mutlaka gönderilir.
        const schedule = (final) => {
            finalPending = finalPending || final;
            if (timer) return;
            const run = () => {
                timer = 0;
                const isFinal = finalPending;
                finalPending = false;
                send(isFinal);
            };
            const wait = VOLUME_THROTTLE_MS - (performance.now() - lastSentAt);
            if (wait <= 0) run();
            else timer = setTimeout(run, wait);
        };

        slider.addEventListener("input", () => {
            if (gesture.stale) {
                // Kaydırmaya dönüşen dokunmanın artığı: kaydırıcı sunucudaki (ya da geri alınan) değere döner
                if (ui.volumeBusy[target]) {
                    slider.value = gesture.startValue;
                    showValue();
                } else {
                    safeRender(renderVolume);
                }
                return;
            }
            // Yeni etkileşim sunucudaki değerden başlar: aynı değere geri dönülürse mesaj gitmez
            if (!ui.volumeBusy[target]) lastSentValue = state.volume[target];
            ui.volumeBusy[target] = true;
            inputSeq += 1;
            showValue();
            if (!gesture.undecided) schedule(false);   // kararsız dokunma: kaydırma olabilir, bekle
        });
        slider.addEventListener("change", () => {
            if (!gesture.stale) schedule(true);
        });
        // "change" gelmeden odak kaybolursa etkileşimi yine düzgün bitir
        slider.addEventListener("blur", () => {
            if (ui.volumeBusy[target] && !gesture.active) schedule(true);
        });
    }


    // ── Şarkı konumu (seek) ───────────────────────────────────

    /** Kaydırıcıyla oynanırken yerel saat onun değerini ezmesin. */
    function onSeekInput() {
        if (seekGesture.stale) {
            safeRender(renderProgress);   // kaydırmaya dönüşen dokunmanın artığı: gerçek konuma dön
            return;
        }
        ui.seek.dragging = true;
        ui.seek.seq += 1;
        safeRender(renderProgress);
    }

    /** Bir seçim dizisi (sürükleme, basılı tutulan ok tuşu) tek bir "seek" mesajı üretir. */
    function onSeekChange() {
        // Etkileşim zaten bittiyse (şarkı değişti, dokunma kaydırmaya dönüştü) seçim geçersizdir
        if (seekGesture.stale || !ui.seek.dragging) return;
        clearTimeout(ui.seek.timer);
        ui.seek.timer = setTimeout(sendSeek, SEEK_DEBOUNCE_MS);
    }

    /**
     * Değer değişmeden biten sürükleme (ör. ileri çekip aynı saniyeye geri bırakmak): tarayıcı "change"
     * göndermez. Bitirilmezse kaydırıcı şarkı değişene kadar donuk kalırdı.
     */
    function releaseSeek() {
        if (!ui.seek.dragging || ui.seek.timer) return;   // gönderilmeyi bekleyen seçim (ok tuşu) kendisi bitirir
        ui.seek.dragging = false;
        safeRender(renderProgress);
    }

    let seekGesture = null;   // setupSeekSlider kurar

    function setupSeekSlider() {
        seekGesture = watchRangeGesture(el.npSeek, {
            // Değer değiştiyse "change" ile aynı: tarayıcı göndermese bile seçim kaybolmasın (tekrarı zararsız)
            onRelease: (changed) => {
                if (changed) onSeekChange();
                else releaseSeek();
            },
            // Tarayıcı dokunmayı sayfa kaydırmaya çevirdi: hiçbir konum seçilmedi
            onCancel: (startValue) => {
                if (ui.seek.timer) {
                    // Önceki seçim (ör. ok tuşu) gönderilmeyi bekliyor: dokunmanın atladığı değer onu bozmasın
                    el.npSeek.value = startValue;
                    safeRender(renderProgress);
                    return;
                }
                endSeekInteraction();
                safeRender(renderProgress);
            },
        });
        el.npSeek.addEventListener("input", onSeekInput);
        el.npSeek.addEventListener("change", onSeekChange);
    }

    async function sendSeek() {
        ui.seek.timer = 0;
        const seq = ui.seek.seq;
        const position = Number(el.npSeek.value);
        const result = await command("seek", { position });
        // Beklerken şarkı değiştiyse (endSeekInteraction) ya da kullanıcı yeniden oynamaya başladıysa dokunma
        if (seq !== ui.seek.seq || ui.seek.timer) return;
        if (result.ok) {
            // Sunucu yeni konumu "progress" ile de yayınlar; yerel saat hedef konumdan devam etsin
            state.playback.position = position;
            ui.progressAt = performance.now();
        }
        // Başarısızsa kaydırıcı son bilinen gerçek konuma döner
        ui.seek.dragging = false;
        safeRender(renderProgress, renderQueueMeta);
    }

    /** Yarım kalan konum seçimini iptal eder (şarkı değişti / bağlantı koptu). */
    function endSeekInteraction() {
        clearTimeout(ui.seek.timer);
        ui.seek.timer = 0;
        ui.seek.dragging = false;
        ui.seek.seq += 1;
    }


    // ── Olay dinleyicileri ────────────────────────────────────

    function bindEvents() {
        el.loginForm.addEventListener("submit", submitLogin);
        el.loginCancelBtn.addEventListener("click", () => {
            el.loginOverlay.hidden = true;
            el.userBadge.focus();
        });
        el.loginOverlay.addEventListener("keydown", (event) => {
            const cancellable = !el.loginCancelBtn.hidden;
            trapDialogKeys(event, el.loginForm, cancellable ? () => el.loginCancelBtn.click() : null);
        });
        el.userBadge.addEventListener("click", () => showLogin({ changing: true }));

        el.adminToggleBtn.addEventListener("click", () => {
            if (session.isAdmin) logout();
            else openAdminModal();
        });
        el.adminForm.addEventListener("submit", submitAdmin);
        el.adminCancelBtn.addEventListener("click", closeAdminModal);
        el.adminOverlay.addEventListener("keydown", (event) => trapDialogKeys(event, el.adminForm, closeAdminModal));
        el.adminOverlay.addEventListener("pointerdown", (event) => {
            adminBackdropDown = event.target === el.adminOverlay;
        });
        el.adminOverlay.addEventListener("click", (event) => {
            if (adminBackdropDown && event.target === el.adminOverlay) closeAdminModal();
            adminBackdropDown = false;
        });

        el.viewBtn.addEventListener("click", openView);
        el.viewCloseBtn.addEventListener("click", closeView);
        el.viewOverlay.addEventListener("keydown", (event) => trapDialogKeys(event, el.viewDialog, closeView));
        el.viewOverlay.addEventListener("pointerdown", (event) => {
            view.backdropDown = event.target === el.viewOverlay;
        });
        el.viewOverlay.addEventListener("click", (event) => {
            if (view.backdropDown && event.target === el.viewOverlay) closeView();
            view.backdropDown = false;
        });
        for (const tab of [el.viewTabMeet, el.viewTabLogin]) {
            tab.addEventListener("click", () => startView(tab.dataset.target));
        }
        el.viewSignoutBtn.addEventListener("click", signOutGoogle);
        el.viewBackBtn.addEventListener("click", () => sendViewInput({ action: "back" }));
        el.viewReloadBtn.addEventListener("click", () => sendViewInput({ action: "reload" }));
        el.viewImg.addEventListener("click", onViewClick);
        el.viewScreen.addEventListener("wheel", onViewWheel, { passive: false });
        el.viewScreen.addEventListener("keydown", onViewKeydown);
        el.viewScreen.addEventListener("paste", onViewPaste);
        el.viewTypeForm.addEventListener("submit", submitViewText);
        el.viewTypeForm.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-key]");
            if (button) sendViewKey(button.dataset.key);
        });

        el.meetForm.addEventListener("submit", submitMeet);
        el.meetInput.addEventListener("input", () => {
            ui.meetDirty = true;
        });
        // Boş bırakılıp çıkılırsa yeniden botun bulunduğu toplantının linki görünsün
        el.meetInput.addEventListener("blur", () => {
            if (ui.meetDirty && !el.meetInput.value.trim()) {
                ui.meetDirty = false;
                safeRender(renderBot);
            }
        });
        el.meetCancelBtn.addEventListener("click", () => leaveMeet({ confirmFirst: false }));
        el.meetLeaveBtn.addEventListener("click", () => leaveMeet({ confirmFirst: true }));

        el.addForm.addEventListener("submit", submitAdd);

        el.btnPlayPause.addEventListener("click", () => {
            command(state.playback.state === "playing" ? "pause" : "resume");
        });
        el.btnStop.addEventListener("click", () => command("stop"));
        el.btnSkip.addEventListener("click", () => command("skip"));
        el.btnRepeat.addEventListener("click", () => {
            command("repeat", { mode: REPEAT_NEXT[state.playback.repeat] || "off" });
        });
        setupSeekSlider();
        el.npThumb.addEventListener("error", () => {
            if (!el.npThumb.getAttribute("src")) return;
            console.info("Kapak görseli yüklenemedi:", el.npThumb.src);
            el.npThumb.dataset.failed = "1";
            safeRender(renderNowPlaying);
        });

        el.btnShuffle.addEventListener("click", () => command("shuffle"));
        el.btnClear.addEventListener("click", () => {
            if (confirm("Kuyruktaki tüm şarkılar silinsin mi?")) command("clear");
        });
        el.queueList.addEventListener("click", onQueueClick);
        el.queueList.addEventListener("keydown", onQueueKeydown);
        el.queueList.addEventListener("dragstart", onDragStart);
        el.queueList.addEventListener("dragover", onDragOver);
        el.queueList.addEventListener("dragleave", onDragLeave);
        el.queueList.addEventListener("drop", onDrop);
        el.queueList.addEventListener("dragend", onDragEnd);

        el.historyList.addEventListener("click", (event) => {
            const button = event.target.closest("button[data-action='readd']");
            if (button && !button.disabled) readdFromHistory(button.dataset.id);
        });

        el.btnMic.addEventListener("click", () => command("mic", { muted: !state.micMuted }));
        setupVolumeSlider("music");
        setupVolumeSlider("mic");

        el.connRetryBtn.addEventListener("click", retryNow);
        window.addEventListener("online", checkConnection);
        document.addEventListener("visibilitychange", () => {
            if (document.visibilityState === "visible") checkConnection();
        });

        if (el.statue) {
            const hideStatue = () => {
                console.info("Dekoratif görsel yüklenemedi, gizlendi");
                el.statue.remove();
            };
            el.statue.addEventListener("error", hideStatue, { once: true });
            if (el.statue.complete && el.statue.naturalWidth === 0) hideStatue();
        }

        window.addEventListener("error", (event) => {
            console.error("[MeetBot] Beklenmeyen hata:", event.error || event.message);
            toast("⚠️ Beklenmeyen bir arayüz hatası oluştu. Sorun sürerse sayfayı yenileyin.", "error");
        });
        window.addEventListener("unhandledrejection", (event) => {
            console.error("[MeetBot] Yakalanmamış hata:", event.reason);
            toast("⚠️ Beklenmeyen bir arayüz hatası oluştu. Sorun sürerse sayfayı yenileyin.", "error");
        });
    }

    /** Çalan şarkının ilerleme çubuğu ve kuyruk bitiş tahmini için yerel saat (bağlantı yokken durur). */
    function startClock() {
        setInterval(() => {
            if (!isReady()) return;
            if (state.current && state.playback.state === "playing") safeRender(renderProgress);
            const now = Date.now();
            if (now - ui.lastEtaRender >= 1000) {
                ui.lastEtaRender = now;
                safeRender(renderQueueMeta);
            }
        }, 250);
    }


    // ── Başlatma ──────────────────────────────────────────────

    function init() {
        bindEvents();
        startClock();
        session.name = (storage.get(STORAGE_NAME) || "").trim();
        renderAll();
        if (session.name && charCount(session.name) <= NAME_MAX) {
            showApp();
            connect();
        } else {
            showLogin();
        }
    }

    init();
})();
