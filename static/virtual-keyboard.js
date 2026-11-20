/**
 * virtual-keyboard.js - Built-in Touch Virtual Keyboard untuk AKMv2 Kiosk (1024x600 Touchscreen)
 * Otomatis muncul saat menyentuh/klik form <input> atau <textarea>, mendukung:
 * - Layout Penuh QWERTY + Angka (1-0) + Simbol (@ . _ - / :)
 * - Layout Khusus Numerik untuk input angka/telepon/NIK/kalibrasi
 * - Kompatibel penuh dengan event jQuery .on('input') / Autocomplete Wilayah & Pasien
 */
(function () {
    "use strict";

    const NON_TEXT_TYPES = new Set([
        "button", "checkbox", "color", "date", "datetime-local",
        "file", "hidden", "image", "month", "radio", "range",
        "reset", "submit", "time", "week"
    ]);

    const LAYOUTS = {
        alpha: [
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "BKSP"],
            ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
            ["a", "s", "d", "f", "g", "h", "j", "k", "l", "@"],
            ["SHIFT", "z", "x", "c", "v", "b", "n", "m", ".", "_", "-"],
            ["SYM", "NUM", "SPACE", "/", ":", "NEXT", "DONE"]
        ],
        sym: [
            ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0", "BKSP"],
            ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")"],
            ["-", "_", "=", "+", "[", "]", "{", "}", "\\", "|"],
            [";", ":", "'", "\"", ",", ".", "<", ">", "/", "?"],
            ["ABC", "NUM", "SPACE", "~", "`", "NEXT", "DONE"]
        ],
        num: [
            ["7", "8", "9", "BKSP"],
            ["4", "5", "6", "-"],
            ["1", "2", "3", "."],
            ["ABC", "0", "CLR", "DONE"]
        ]
    };

    let activeEl = null;
    let currentMode = "alpha"; // 'alpha' | 'sym' | 'num'
    let isShift = false;
    let isCaps = false;
    let kbRoot = null;
    let keysContainer = null;
    let fieldLabelEl = null;
    let previewEl = null;
    let peekPassword = false;
    let bkspTimer = null;
    let bkspInterval = null;

    function isEditableInput(el) {
        if (!el || el.disabled || el.readOnly) return false;
        const tag = el.tagName;
        if (tag === "TEXTAREA") return true;
        if (tag === "INPUT") {
            const type = (el.getAttribute("type") || "text").toLowerCase();
            return !NON_TEXT_TYPES.has(type);
        }
        return false;
    }

    function isNumericPreferred(el) {
        if (!el || el.tagName !== "INPUT") return false;
        const type = (el.getAttribute("type") || "text").toLowerCase();
        const inputMode = (el.getAttribute("inputmode") || "").toLowerCase();
        return type === "number" || type === "tel" || inputMode === "numeric" || inputMode === "decimal";
    }

    function getFieldTitle(el) {
        if (!el) return "Input";
        const ph = el.getAttribute("placeholder");
        if (ph) return ph.length > 34 ? ph.slice(0, 34) + "..." : ph;
        if (el.id) {
            const lbl = document.querySelector(`label[for="${el.id}"]`);
            if (lbl) return lbl.textContent.trim();
        }
        const parentLbl = el.closest(".form-group, .calib-card");
        if (parentLbl) {
            const l = parentLbl.querySelector("label, .calib-param-name");
            if (l) return l.textContent.trim();
        }
        return el.name || "Input Form";
    }

    function updateHeaderPreview() {
        if (!activeEl || !previewEl || !fieldLabelEl) return;
        fieldLabelEl.textContent = getFieldTitle(activeEl);
        const rawVal = activeEl.value || "";
        const isPwd = (activeEl.getAttribute("type") || "").toLowerCase() === "password";
        if (!rawVal) {
            previewEl.textContent = "(kosong)";
            previewEl.classList.add("empty");
        } else {
            previewEl.classList.remove("empty");
            previewEl.textContent = (isPwd && !peekPassword) ? "•".repeat(Math.min(rawVal.length, 28)) : rawVal;
        }
        const peekBtn = kbRoot.querySelector(".vk-peek-btn");
        if (peekBtn) {
            peekBtn.style.display = isPwd ? "inline-flex" : "none";
            peekBtn.textContent = peekPassword ? "Sembunyikan" : "Lihat Sandi";
        }
    }

    function triggerInputEvents(el) {
        el.dispatchEvent(new Event("input", { bubbles: true }));
        el.dispatchEvent(new Event("change", { bubbles: true }));
        if (window.jQuery) {
            window.jQuery(el).trigger("input");
        }
        updateHeaderPreview();
    }

    function supportsSelection(el) {
        if (!el) return false;
        const type = (el.getAttribute("type") || "text").toLowerCase();
        return !["number", "email"].includes(type);
    }

    function insertText(char) {
        if (!activeEl) return;
        activeEl.focus({ preventScroll: true });

        if (supportsSelection(activeEl)) {
            try {
                const start = activeEl.selectionStart ?? activeEl.value.length;
                const end = activeEl.selectionEnd ?? activeEl.value.length;
                const val = activeEl.value || "";
                activeEl.value = val.slice(0, start) + char + val.slice(end);
                const pos = start + char.length;
                activeEl.setSelectionRange(pos, pos);
                triggerInputEvents(activeEl);
                return;
            } catch (_) {}
        }

        // Fallback untuk <input type="number"> atau <input type="email">
        let cur = activeEl.value || "";
        const type = (activeEl.getAttribute("type") || "").toLowerCase();
        if (type === "number") {
            if (char === "-") {
                cur = cur.startsWith("-") ? cur.slice(1) : "-" + cur;
            } else if (char === ".") {
                if (!cur.includes(".")) cur = (cur === "" || cur === "-") ? cur + "0." : cur + ".";
            } else if (/^[0-9]$/.test(char)) {
                cur = (cur === "0") ? char : cur + char;
            }
            activeEl.value = cur;
        } else {
            activeEl.value = cur + char;
        }
        triggerInputEvents(activeEl);
    }

    function doBackspace() {
        if (!activeEl) return;
        activeEl.focus({ preventScroll: true });

        if (supportsSelection(activeEl)) {
            try {
                const start = activeEl.selectionStart ?? activeEl.value.length;
                const end = activeEl.selectionEnd ?? activeEl.value.length;
                const val = activeEl.value || "";
                if (start !== end) {
                    activeEl.value = val.slice(0, start) + val.slice(end);
                    activeEl.setSelectionRange(start, start);
                } else if (start > 0) {
                    activeEl.value = val.slice(0, start - 1) + val.slice(start);
                    activeEl.setSelectionRange(start - 1, start - 1);
                }
                triggerInputEvents(activeEl);
                return;
            } catch (_) {}
        }

        const cur = activeEl.value || "";
        activeEl.value = cur.slice(0, -1);
        triggerInputEvents(activeEl);
    }

    function stopBkspRepeat() {
        if (bkspTimer) { clearTimeout(bkspTimer); bkspTimer = null; }
        if (bkspInterval) { clearInterval(bkspInterval); bkspInterval = null; }
    }

    function focusNextInput() {
        if (!activeEl) return;
        const all = Array.from(document.querySelectorAll("input, textarea")).filter(el => {
            if (!isEditableInput(el)) return false;
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        });
        const idx = all.indexOf(activeEl);
        if (idx >= 0 && idx + 1 < all.length) {
            const next = all[idx + 1];
            next.focus();
            openKeyboard(next);
        } else {
            closeKeyboard();
        }
    }

    function handleDone() {
        if (!activeEl) {
            closeKeyboard();
            return;
        }
        const form = activeEl.closest("form");
        // Pada halaman auth (login/signup), tombol Selesai di kolom terakhir langsung mempermudah submit atau tutup
        closeKeyboard();
        if (form && document.body.classList.contains("auth-page")) {
            const allInForm = Array.from(form.querySelectorAll("input")).filter(isEditableInput);
            if (allInForm.length > 0 && activeEl === allInForm[allInForm.length - 1]) {
                // Biarkan user melihat nilai atau langsung klik Masuk
            }
        }
    }

    function renderKeys() {
        if (!keysContainer) return;
        keysContainer.innerHTML = "";
        keysContainer.className = "vk-rows vk-mode-" + currentMode;

        const rows = LAYOUTS[currentMode] || LAYOUTS.alpha;
        rows.forEach(row => {
            const rowEl = document.createElement("div");
            rowEl.className = "vk-row";

            row.forEach(key => {
                const btn = document.createElement("button");
                btn.type = "button";
                btn.className = "vk-key";

                if (key === "BKSP") {
                    btn.classList.add("vk-key-fn", "vk-key-bksp");
                    btn.innerHTML = "&#9003;";
                    const startRepeat = (e) => {
                        e.preventDefault();
                        doBackspace();
                        stopBkspRepeat();
                        bkspTimer = setTimeout(() => {
                            bkspInterval = setInterval(doBackspace, 65);
                        }, 380);
                    };
                    btn.addEventListener("pointerdown", startRepeat);
                    btn.addEventListener("pointerup", stopBkspRepeat);
                    btn.addEventListener("pointerleave", stopBkspRepeat);
                    btn.addEventListener("pointercancel", stopBkspRepeat);
                } else if (key === "SHIFT") {
                    btn.classList.add("vk-key-fn", "vk-key-wide");
                    if (isShift || isCaps) btn.classList.add("vk-key-active");
                    btn.textContent = isCaps ? "CAPS" : "⇧ Shift";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        if (isShift && !isCaps) {
                            isCaps = true;
                            isShift = false;
                        } else if (isCaps) {
                            isCaps = false;
                            isShift = false;
                        } else {
                            isShift = true;
                        }
                        renderKeys();
                    });
                } else if (key === "SPACE") {
                    btn.classList.add("vk-key-space");
                    btn.textContent = "Spasi";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        insertText(" ");
                    });
                } else if (key === "SYM") {
                    btn.classList.add("vk-key-fn");
                    btn.textContent = "?#=";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        currentMode = "sym";
                        renderKeys();
                    });
                } else if (key === "NUM") {
                    btn.classList.add("vk-key-fn");
                    btn.textContent = "123";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        currentMode = "num";
                        renderKeys();
                    });
                } else if (key === "ABC") {
                    btn.classList.add("vk-key-fn");
                    btn.textContent = "ABC";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        currentMode = "alpha";
                        renderKeys();
                    });
                } else if (key === "CLR") {
                    btn.classList.add("vk-key-fn");
                    btn.textContent = "Hapus";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        if (activeEl) {
                            activeEl.value = "";
                            triggerInputEvents(activeEl);
                        }
                    });
                } else if (key === "NEXT") {
                    btn.classList.add("vk-key-fn", "vk-key-wide");
                    btn.textContent = "Lanjut ⇥";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        focusNextInput();
                    });
                } else if (key === "DONE") {
                    btn.classList.add("vk-key-done", "vk-key-wide");
                    btn.textContent = "Selesai ✓";
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        handleDone();
                    });
                } else {
                    const upper = (isShift || isCaps) && /^[a-z]$/.test(key);
                    const ch = upper ? key.toUpperCase() : key;
                    btn.textContent = ch;
                    btn.addEventListener("pointerdown", (e) => {
                        e.preventDefault();
                        insertText(ch);
                        if (isShift && !isCaps) {
                            isShift = false;
                            renderKeys();
                        }
                    });
                }

                rowEl.appendChild(btn);
            });

            keysContainer.appendChild(rowEl);
        });
    }

    function buildKeyboardDOM() {
        if (kbRoot) return;
        kbRoot = document.createElement("div");
        kbRoot.id = "akmVirtualKeyboard";
        kbRoot.className = "vk-Overlay";
        kbRoot.innerHTML = `
            <div class="vk-bar">
                <div class="vk-bar-left">
                    <span class="vk-field-badge" id="vkFieldLabel">Input</span>
                    <span class="vk-preview empty" id="vkPreview">(kosong)</span>
                </div>
                <div class="vk-bar-right">
                    <button type="button" class="vk-peek-btn" style="display:none;">Lihat Sandi</button>
                    <button type="button" class="vk-close-btn">Tutup ✕</button>
                </div>
            </div>
            <div id="vkKeysContainer" class="vk-rows"></div>
        `;

        // Cegah kehilangan fokus input saat menyentuh area keyboard
        kbRoot.addEventListener("mousedown", (e) => e.preventDefault());

        kbRoot.querySelector(".vk-peek-btn").addEventListener("pointerdown", (e) => {
            e.preventDefault();
            peekPassword = !peekPassword;
            updateHeaderPreview();
        });

        kbRoot.querySelector(".vk-close-btn").addEventListener("pointerdown", (e) => {
            e.preventDefault();
            closeKeyboard();
        });

        document.body.appendChild(kbRoot);
        keysContainer = document.getElementById("vkKeysContainer");
        fieldLabelEl = document.getElementById("vkFieldLabel");
        previewEl = document.getElementById("vkPreview");
    }

    function openKeyboard(el) {
        if (!isEditableInput(el)) return;
        buildKeyboardDOM();

        const switchedElement = (activeEl !== el);
        activeEl = el;
        if (switchedElement) {
            peekPassword = false;
            isShift = false;
            currentMode = isNumericPreferred(el) ? "num" : "alpha";
        }

        document.body.classList.add("vk-body-padded");
        kbRoot.classList.add("vk-visible");
        updateHeaderPreview();
        renderKeys();

        setTimeout(() => {
            if (activeEl) {
                try {
                    activeEl.scrollIntoView({ behavior: "smooth", block: "center" });
                } catch (_) {}
            }
        }, 120);
    }

    function closeKeyboard() {
        stopBkspRepeat();
        if (kbRoot) {
            kbRoot.classList.remove("vk-visible");
        }
        document.body.classList.remove("vk-body-padded");
        if (activeEl) {
            try { activeEl.blur(); } catch (_) {}
        }
        activeEl = null;
    }

    document.addEventListener("DOMContentLoaded", () => {
        buildKeyboardDOM();

        document.addEventListener("focusin", (e) => {
            if (isEditableInput(e.target)) {
                openKeyboard(e.target);
            }
        });

        document.addEventListener("click", (e) => {
            const target = e.target;
            if (isEditableInput(target)) {
                openKeyboard(target);
                return;
            }
            if (!kbRoot || !kbRoot.classList.contains("vk-visible")) return;
            if (kbRoot.contains(target)) return;
            // Jangan tutup jika klik item dropdown autocomplete
            if (target.closest(".ac-list-container, .autocomplete-items, .calib-step-btn")) {
                setTimeout(updateHeaderPreview, 50);
                return;
            }
            closeKeyboard();
        });

        document.addEventListener("input", (e) => {
            if (e.target === activeEl) {
                updateHeaderPreview();
            }
        });
    });
})();
