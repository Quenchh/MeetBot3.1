// ──────────────────────────────────────────────────────────────
//  app.js — Frontend WebSocket İstemcisi + UI Yönetimi
// ──────────────────────────────────────────────────────────────

(() => {
    "use strict";

    console.log("🚀 Uygulama başlatılıyor...");

    // ── Global Error Handler ──────────────────────────────────
    window.onerror = function (msg, url, line, col, error) {
        console.error("Global Error:", msg, error);
        showToast(`HATA: ${msg}`, "error");
        return false;
    };

    window.addEventListener("unhandledrejection", function (event) {
        console.error("Unhandled Rejection:", event.reason);
        showToast(`HATA: ${event.reason}`, "error");
    });

    // ── State ─────────────────────────────────────────────────
    let ws = null;
    let username = localStorage.getItem("meetbot_username") || "";
    console.log("Storage Kullanıcı Adı:", username);

    let state = {
        queue: [],
        current_song: null,
        playback_state: "idle",
        loop: false,
        music_volume: 80,
        mic_volume: 80,
        mic_muted: false,
        bot_status: "disconnected",
        meet_link: null,
    };

    let isAdmin = false;

    // ── DOM Referansları ───────────────────────────────────────
    const $ = (sel) => document.querySelector(sel);
    const loginOverlay = $("#login-overlay");
    const loginInput = $("#login-username");
    const loginBtn = $("#login-btn");
    const loginError = $("#login-error");
    const appEl = $("#app");

    // Admin Modal
    const adminToggleBtn = $("#admin-toggle-btn");
    const adminModalOverlay = $("#admin-modal-overlay");
    const adminPasswordInput = $("#admin-password");
    const adminLoginBtn = $("#admin-login-btn");
    const adminCancelBtn = $("#admin-cancel-btn");
    const adminError = $("#admin-error");
    // const adminPanel = $("#admin-panel"); // Removed: Element does not exist

    const meetInput = $("#meet-link-input");
    const meetJoinBtn = $("#meet-join-btn");

    const statusDot = $("#status-dot");
    const statusText = $("#status-text");
    const userBadgeName = $("#user-badge-name");

    const ytInput = $("#yt-link-input");
    const addSongBtn = $("#add-song-btn");

    const npTitle = $("#np-title");
    const npStatus = $("#np-status");
    const npTime = $("#np-time");

    const btnResume = $("#btn-resume");
    const btnPause = $("#btn-pause");
    const btnStop = $("#btn-stop");
    const btnSkip = $("#btn-skip");
    const btnLoop = $("#btn-loop");

    const musicSlider = $("#music-volume-slider");
    const musicValue = $("#music-volume-value");
    const micSlider = $("#mic-volume-slider");
    const micValue = $("#mic-volume-value");

    const btnToggleMic = $("#btn-toggle-mic");

    const queueList = $("#queue-list");
    const queueCount = $("#queue-count");

    const toastContainer = $("#toast-container");


    // ── Login Mantığı ─────────────────────────────────────────

    // Sayfa yüklendiğinde hemen kontrol et
    if (username) {
        console.log("✅ Otomatik giriş yapılıyor: " + username);
        appEl.classList.remove("hidden");
        appEl.classList.add("visible");
        loginOverlay.classList.add("hidden");
        userBadgeName.textContent = username;
        connectWS();
    } else {
        console.log("ℹ️ Giriş bekleniyor...");
        loginOverlay.classList.remove("hidden");
        appEl.classList.add("hidden");
    }

    loginBtn.addEventListener("click", doLogin);
    loginInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doLogin();
    });

    function doLogin() {
        const val = loginInput.value.trim();
        if (!val) {
            loginError.textContent = "Kullanıcı adı boş olamaz!";
            return;
        }
        username = val;
        localStorage.setItem("meetbot_username", username);
        console.log("💾 Kullanıcı kaydedildi:", username);

        loginOverlay.classList.add("hidden");
        appEl.classList.remove("hidden");
        appEl.classList.add("visible");
        userBadgeName.textContent = username;
        connectWS();
    }


    // ── Admin Login ───────────────────────────────────────────
    adminToggleBtn.addEventListener("click", () => {
        adminModalOverlay.classList.remove("hidden");
        adminPasswordInput.value = "";
        adminError.textContent = "";
        adminPasswordInput.focus();
    });

    adminCancelBtn.addEventListener("click", () => {
        adminModalOverlay.classList.add("hidden");
    });

    adminLoginBtn.addEventListener("click", doAdminLogin);
    adminPasswordInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") doAdminLogin();
    });

    function doAdminLogin() {
        const password = adminPasswordInput.value.trim();
        if (password === "xasimaymun123") {
            isAdmin = true;
            // adminPanel.classList.remove("hidden"); // Removed
            adminModalOverlay.classList.add("hidden");

            // Update Admin Button Style (Tailwind)
            adminToggleBtn.classList.remove("text-gray-400");
            adminToggleBtn.classList.add("text-indigo-500", "font-bold");
            adminToggleBtn.querySelector("div").classList.add("bg-indigo-500/10", "border-indigo-500/50");
            adminToggleBtn.querySelector("i").classList.add("text-indigo-500");

            showToast("Admin girişi başarılı", "success");
        } else {
            adminError.textContent = "Hatalı şifre!";
        }
    }


    // ── WebSocket ─────────────────────────────────────────────
    function connectWS() {
        if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
            return;
        }

        const protocol = location.protocol === "https:" ? "wss:" : "ws:";
        ws = new WebSocket(`${protocol}//${location.host}/ws`);

        ws.onopen = () => {
            console.log("[WS] Bağlantı kuruldu");
            showToast("Sunucuya bağlandı ✅", "success");
        };

        ws.onclose = () => {
            console.log("[WS] Bağlantı koptu, yeniden deneniyor...");
            showToast("Sunucu bağlantısı koptu", "error");
            setTimeout(connectWS, 3000);
        };

        ws.onerror = (err) => {
            console.error("[WS] Hata:", err);
        };

        ws.onmessage = (event) => {
            try {
                const msg = JSON.parse(event.data);
                handleMessage(msg);
            } catch (e) {
                console.error("[WS] Parse hatası:", e);
            }
        };
    }

    function send(msg) {
        if (ws && ws.readyState === WebSocket.OPEN) {
            ws.send(JSON.stringify(msg));
        } else {
            showToast("Sunucu bağlantısı yok!", "error");
        }
    }


    // ── Mesaj Yönetimi ────────────────────────────────────────
    function handleMessage(msg) {
        console.log("[WS] Mesaj alındı:", msg.type);
        switch (msg.type) {
            case "state_sync":
                state = { ...state, ...msg };
                renderAll();
                break;

            case "queue_update":
                state.queue = msg.queue || [];
                renderQueue();
                renderControls();
                break;

            case "playback_update":
                state.current_song = msg.current_song;
                state.playback_state = msg.playback_state;
                state.loop = msg.loop;
                renderNowPlaying();
                renderControls();
                break;

            case "volume_update":
                state.music_volume = msg.music_volume;
                state.mic_volume = msg.mic_volume;
                renderVolume();
                break;

            case "mic_status":
                state.mic_muted = msg.muted;
                renderMicButton();
                break;

            case "bot_status":
                state.bot_status = msg.status;
                if (msg.meet_link) state.meet_link = msg.meet_link;
                renderBotStatus();
                break;

            case "song_added":
                showToast(`🎵 "${msg.song.title}" kuyruğa eklendi`, "success");
                break;

            case "progress_update":
                if (msg.total > 0) {
                    npTime.textContent = `${formatTime(msg.current)} / ${formatTime(msg.total)}`;
                }
                break;

            case "error":
                showToast(`❌ ${msg.message}`, "error");
                break;
        }
    }


    // ── Render Fonksiyonları ───────────────────────────────────
    function renderAll() {
        renderQueue();
        renderNowPlaying();
        renderControls();
        renderVolume();
        renderBotStatus();
        renderMicButton();
    }

    // setInterval(renderAll, 2000); // UI Sync Watchdog İPTAL (Flicker yapıyor)

    // Drag & Drop Değişkenleri
    let dragSrcEl = null;

    function renderQueue() {
        if (!state.queue || !Array.isArray(state.queue)) {
            state.queue = [];
        }
        queueCount.textContent = state.queue.length;

        if (state.queue.length === 0) {
            queueList.innerHTML = `
                <div class="flex-1 flex flex-col items-center justify-center text-gray-500 h-full mt-12">
                     <div class="mb-4 text-[#1a1a1a]">
                        <i class="fa-solid fa-music text-7xl text-[#181818]"></i>
                     </div>
                     <p class="text-gray-500 text-md font-medium">Kuyruk boş <span class="text-gray-700 mx-2">—</span> YouTube linki ekleyin</p>
                </div>`;
            return;
        }

        queueList.innerHTML = "";
        state.queue.forEach((song, i) => {
            const item = document.createElement("div");
            item.className = "queue-item bg-[#121212] border border-[#2a2a2a] p-3 rounded-lg flex items-center justify-between group hover:border-[#444] hover:bg-[#1a1a1a] transition-all cursor-move mb-2";
            item.draggable = true;
            item.dataset.id = song.id;

            item.innerHTML = `
                <div class="flex items-center gap-3 overflow-hidden">
                    <div class="text-[#444] font-mono text-xs w-6 text-center select-none">${i + 1}</div>
                    <div class="flex-1 min-w-0">
                        <div class="text-gray-300 text-sm font-medium truncate" title="${escapeHtml(song.title)}">${escapeHtml(song.title)}</div>
                        <div class="text-[11px] text-[#666] flex items-center gap-2 mt-0.5">
                            <span><i class="fa-regular fa-clock text-[10px]"></i> ${song.duration_str}</span>
                            <span class="w-1 h-1 rounded-full bg-[#333]"></span>
                            <span><i class="fa-regular fa-user text-[10px]"></i> ${escapeHtml(song.added_by)}</span>
                        </div>
                    </div>
                </div>
                <button class="queue-remove text-[#444] hover:text-red-500 opacity-0 group-hover:opacity-100 transition-all p-2 bg-[#1a1a1a] rounded-lg border border-[#333] hover:border-red-900/50" title="Kaldır">
                    <i class="fa-solid fa-trash-can text-xs"></i>
                </button>
            `;

            item.querySelector(".queue-remove").addEventListener("click", (e) => {
                e.stopPropagation();
                send({ type: "remove_song", id: song.id });
            });

            addDragEvents(item);
            queueList.appendChild(item);
        });
    }

    function addDragEvents(item) {
        item.addEventListener("dragstart", handleDragStart);
        item.addEventListener("dragenter", handleDragEnter);
        item.addEventListener("dragover", handleDragOver);
        item.addEventListener("dragleave", handleDragLeave);
        item.addEventListener("drop", handleDrop);
        item.addEventListener("dragend", handleDragEnd);
    }

    function handleDragStart(e) {
        dragSrcEl = this;
        e.dataTransfer.effectAllowed = "move";
        this.classList.add("dragging");
    }

    function handleDragEnter(e) {
        if (this !== dragSrcEl) {
            this.classList.add("drag-over");
        }
    }

    function handleDragOver(e) {
        if (e.preventDefault) e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        return false;
    }

    function handleDragLeave(e) {
        this.classList.remove("drag-over");
    }

    function handleDrop(e) {
        if (e.stopPropagation) e.stopPropagation();

        if (dragSrcEl !== this) {
            const allItems = [...queueList.querySelectorAll(".queue-item")];
            const srcIndex = allItems.indexOf(dragSrcEl);
            const targetIndex = allItems.indexOf(this);

            if (srcIndex < targetIndex) this.after(dragSrcEl);
            else this.before(dragSrcEl);

            sendNewOrder();
        }
        return false;
    }

    function handleDragEnd(e) {
        this.classList.remove("dragging");
        [...queueList.querySelectorAll(".queue-item")].forEach(item => {
            item.classList.remove("drag-over");
        });
    }

    function sendNewOrder() {
        const newIds = [...queueList.querySelectorAll(".queue-item")].map(item => parseInt(item.dataset.id));
        send({ type: "reorder_queue", new_ids: newIds });
    }

    function renderNowPlaying() {
        if (state.current_song) {
            npTitle.textContent = state.current_song.title;
            // npTitle.classList.remove("np-idle"); // Tailwind'de gerek yok

            const icon = state.playback_state === "playing" ? '<i class="fa-solid fa-play"></i>' : '<i class="fa-solid fa-pause"></i>';
            const text = state.playback_state === "playing" ? "OYNATILIYOR" : "DURAKLATILDI";

            npStatus.innerHTML = `${icon} ${text}`;
            npStatus.classList.remove("opacity-0");
        } else {
            npTitle.textContent = "Şarkı çalmıyor";
            // npTitle.classList.add("np-idle"); 
            npStatus.textContent = "...";
            npStatus.classList.add("opacity-0");
            npTime.textContent = "--:-- / --:--";
        }
    }

    function renderControls() {
        btnLoop.classList.toggle("active", state.loop);

        const isPlaying = state.playback_state === "playing";
        const isPaused = state.playback_state === "paused";
        const hasSong = !!state.current_song;
        const hasQueue = state.queue.length > 0;

        if (state.playback_state === "playing") {
            btnResume.classList.add("hidden");
            btnPause.classList.remove("hidden");
        } else {
            btnResume.classList.remove("hidden");
            btnPause.classList.add("hidden");
        }

        btnResume.disabled = !((isPaused) || (state.playback_state === "idle" && hasQueue));
        btnPause.disabled = !isPlaying;
        btnStop.disabled = !hasSong;
        btnSkip.disabled = !hasSong;

        // Loop butonu rengi
        if (state.loop) btnLoop.classList.add("text-indigo-400");
        else btnLoop.classList.remove("text-indigo-400");
    }

    function renderVolume() {
        // Optimistik update için değerleri sadece sunucudan gelince değil, input anında da değiştiriyoruz.
        // Ancak bu fonksiyon sunucudan gelen değer için çalışıyorsa, input focus değilse güncelle.
        if (document.activeElement !== musicSlider) {
            musicSlider.value = state.music_volume;
            musicValue.textContent = state.music_volume + "%";
        }
        if (document.activeElement !== micSlider) {
            micSlider.value = state.mic_volume;
            micValue.textContent = state.mic_volume + "%";
        }
    }

    function renderBotStatus() {
        // statusDot.className = "status-indicator " + state.bot_status; // Eski CSS

        // Tailwind renkleri
        statusDot.className = "w-1.5 h-1.5 rounded-full " +
            (state.bot_status === "connected" ? "bg-green-500 shadow-[0_0_8px_rgba(34,197,94,0.6)]" :
                state.bot_status === "connecting" ? "bg-yellow-500 animate-pulse" :
                    "bg-red-500");

        const labels = {
            disconnected: "Bağlı Değil",
            connecting: "Bağlanıyor...",
            connected: "Meet'te",
        };
        statusText.textContent = labels[state.bot_status] || state.bot_status;

        const meetLeaveBtn = $("#meet-leave-btn");

        if (state.bot_status === "connected") {
            // Bağlıyken: Link inputu AÇIK (Değiştirmek için), Buton "Değiştir", Ayrıl butonu GÖRÜNÜR
            meetInput.value = state.meet_link || meetInput.value;
            meetInput.disabled = false;

            meetJoinBtn.disabled = false;
            meetJoinBtn.textContent = "Değiştir";
            // Neutral Style for "Change"
            meetJoinBtn.className = "bg-[#222] hover:bg-[#333] text-gray-200 text-sm font-medium px-5 py-2 rounded-lg transition-colors border border-[#333] disabled:opacity-50 disabled:cursor-not-allowed";
            meetJoinBtn.classList.remove("hidden");

            meetLeaveBtn.classList.remove("hidden");

        } else if (state.bot_status === "connecting") {
            meetInput.disabled = true;
            meetJoinBtn.disabled = true;
            meetJoinBtn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i>';
            meetLeaveBtn.classList.add("hidden");
        } else {
            // Disconnected
            meetInput.disabled = false;
            meetJoinBtn.disabled = false;
            meetJoinBtn.textContent = "Katıl";
            // Neutral Style for "Join"
            meetJoinBtn.className = "bg-[#222] hover:bg-[#333] text-gray-200 text-sm font-medium px-5 py-2 rounded-lg transition-colors border border-[#333] disabled:opacity-50 disabled:cursor-not-allowed";
            meetLeaveBtn.classList.add("hidden");
        }
    }

    function renderMicButton() {
        if (state.mic_muted) {
            btnToggleMic.className = "w-full bg-red-900/10 hover:bg-red-900/20 text-red-500 text-sm font-medium py-3 rounded-lg flex items-center justify-center gap-2 transition-colors border border-red-900/30";
            btnToggleMic.innerHTML = '<i class="fa-solid fa-microphone-slash"></i> Mikrofon KAPALI';
        } else {
            btnToggleMic.className = "w-full bg-[#2a2a35] hover:bg-[#323240] text-indigo-200 text-sm font-medium py-3 rounded-lg flex items-center justify-center gap-2 transition-colors border border-[#353545]";
            btnToggleMic.innerHTML = '<i class="fa-solid fa-microphone"></i> Mikrofon AÇIK';
        }
    }


    // ── Event Handlers ────────────────────────────────────────

    // Meet Katıl / Değiştir Butonu
    meetJoinBtn.addEventListener("click", () => {
        if (!isAdmin) {
            return showToast("Bu işlem için yönetici yetkisi gerekli!", "error");
        }

        const link = meetInput.value.trim();
        if (!link) return showToast("Meet linki girin!", "error");
        if (!link.includes("meet.google.com")) return showToast("Geçersiz Meet linki!", "error");

        // Eğer zaten bağlıysa ve farklı bir link girildiyse "join_meet" yine çalışır, bot.py yeni linke gider.
        send({ type: "join_meet", link });
    });

    // Meet Ayrıl Butonu (Dinamik eklendiği için event delegation veya direct seçim)
    // Yukarıda tanımladık ama element static HTML'de var artık.
    const meetLeaveBtn = $("#meet-leave-btn");
    meetLeaveBtn.addEventListener("click", () => {
        if (!isAdmin) {
            return showToast("Bu işlem için yönetici yetkisi gerekli!", "error");
        }
        if (confirm("Meet görüşmesinden ayrılmak istediğinize emin misiniz?")) {
            send({ type: "leave_meet" });
        }
    });

    meetInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") meetJoinBtn.click();
    });

    addSongBtn.addEventListener("click", addSong);
    ytInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter") addSong();
    });

    function addSong() {
        const url = ytInput.value.trim();
        if (!url) return showToast("YouTube linki girin!", "error");
        if (!url.includes("youtube.com") && !url.includes("youtu.be")) {
            return showToast("Geçersiz YouTube linki!", "error");
        }
        send({ type: "add_song", url, added_by: username });
        ytInput.value = "";
        addSongBtn.disabled = true;
        addSongBtn.innerHTML = '<i class="fa-solid fa-circle-notch fa-spin"></i>';
        setTimeout(() => {
            addSongBtn.disabled = false;
            addSongBtn.textContent = "Ekle";
        }, 3000);
    }

    btnSkip.addEventListener("click", () => send({ type: "skip" }));
    btnStop.addEventListener("click", () => send({ type: "stop" }));
    btnPause.addEventListener("click", () => send({ type: "pause" }));
    btnResume.addEventListener("click", () => send({ type: "resume" }));
    btnLoop.addEventListener("click", () => send({ type: "loop" }));

    btnToggleMic.addEventListener("click", () => {
        console.log("Mikrofon toggle istendi");
        send({ type: "toggle_mic" });
    });

    musicSlider.addEventListener("input", () => {
        const val = parseInt(musicSlider.value);
        musicValue.textContent = val + "%";
        // Debounce gerekebilir ama şimdilik doğrudan gönderelim
        send({ type: "set_volume", target: "music", value: val });
    });

    micSlider.addEventListener("input", () => {
        const val = parseInt(micSlider.value);
        micValue.textContent = val + "%";
        send({ type: "set_volume", target: "mic", value: val });
    });


    // ── Toast ─────────────────────────────────────────────────
    function showToast(message, type = "") {
        const toast = document.createElement("div");
        toast.className = `toast ${type}`;
        toast.textContent = message;
        toastContainer.appendChild(toast);
        setTimeout(() => toast.remove(), 3500);
    }


    // ── Yardımcı ──────────────────────────────────────────────
    function escapeHtml(str) {
        if (!str) return "";
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    function formatTime(seconds) {
        if (!seconds || isNaN(seconds)) return "0:00";
        const m = Math.floor(seconds / 60);
        const s = Math.floor(seconds % 60);
        return `${m}:${s < 10 ? "0" : ""}${s}`;
    }

})();
