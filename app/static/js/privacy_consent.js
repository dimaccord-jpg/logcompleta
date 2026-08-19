(() => {
    const root = document.getElementById("af-privacy-root");
    if (!root) {
        return;
    }

    const endpoint = root.getAttribute("data-endpoint") || "/api/privacy/marketing-consent";
    const panel = document.getElementById("af-privacy-panel");
    const errorEl = document.getElementById("af-privacy-error");
    const titleEl = document.getElementById("af-privacy-panel-title");

    function setError(message) {
        if (!errorEl) {
            return;
        }
        if (!message) {
            errorEl.hidden = true;
            errorEl.textContent = "";
            return;
        }
        errorEl.hidden = false;
        errorEl.textContent = message;
    }

    function decisionButtons() {
        return document.querySelectorAll("[data-af-privacy-decision]");
    }

    function setBusy(busy) {
        decisionButtons().forEach((button) => {
            button.disabled = busy;
        });
    }

    function panelOpeners() {
        return document.querySelectorAll("[data-af-privacy-open-panel]");
    }

    function setPanelOpen(open) {
        if (!panel) {
            return;
        }
        panel.hidden = !open;
        panel.setAttribute("aria-hidden", open ? "false" : "true");
        panelOpeners().forEach((button) => {
            button.setAttribute("aria-expanded", open ? "true" : "false");
        });
        if (open && titleEl) {
            titleEl.focus();
        }
    }

    function clearOwnMarketingStorage() {
        try {
            const storage = window.localStorage;
            if (!storage) {
                return;
            }
            const toRemove = [];
            for (let i = 0; i < storage.length; i += 1) {
                const key = storage.key(i);
                if (key && key.indexOf("fb_pixel_") === 0) {
                    toRemove.push(key);
                }
            }
            toRemove.forEach((key) => {
                try {
                    storage.removeItem(key);
                } catch (err) {
                    // Cleanup defensivo: não interrompe o fluxo.
                }
            });
        } catch (err) {
            // Storage indisponível ou bloqueado.
        }
    }

    function saveDecision(decision) {
        if (decision !== "accepted" && decision !== "rejected") {
            return;
        }
        setError("");
        setBusy(true);
        fetch(endpoint, {
            method: "POST",
            credentials: "same-origin",
            headers: {
                "Content-Type": "application/json",
                Accept: "application/json",
            },
            body: JSON.stringify({ decision: decision }),
        })
            .then((resp) => {
                if (!resp.ok) {
                    throw new Error("request_failed");
                }
                return resp.json();
            })
            .then((body) => {
                if (!body || body.ok !== true) {
                    throw new Error("request_failed");
                }
                if (decision === "rejected") {
                    clearOwnMarketingStorage();
                }
                window.location.reload();
            })
            .catch(() => {
                setBusy(false);
                setError("Não foi possível salvar sua preferência. Tente novamente.");
            });
    }

    document.addEventListener("click", (event) => {
        const target = event.target;
        if (!(target instanceof Element)) {
            return;
        }
        const decisionBtn = target.closest("[data-af-privacy-decision]");
        if (decisionBtn) {
            event.preventDefault();
            saveDecision(decisionBtn.getAttribute("data-af-privacy-decision") || "");
            return;
        }
        if (target.closest("[data-af-privacy-open-panel]")) {
            event.preventDefault();
            setPanelOpen(true);
            return;
        }
        if (target.closest("[data-af-privacy-close-panel]")) {
            event.preventDefault();
            setPanelOpen(false);
        }
    });

    document.addEventListener("keydown", (event) => {
        if (event.key === "Escape" && panel && !panel.hidden) {
            setPanelOpen(false);
        }
    });
})();
