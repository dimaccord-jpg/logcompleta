(() => {
    const ALLOWED_EVENTS = {
        page_view: true,
        signup_completed: true,
        checkout_started: true,
        first_relevant_task_completed: true,
    };

    // Nomes Meta standard (fbq track). first_relevant usa trackCustom fixo.
    const META_MAP = {
        page_view: "PageView",
        signup_completed: "CompleteRegistration",
        checkout_started: "InitiateCheckout",
    };

    const META_CUSTOM_MAP = {
        first_relevant_task_completed: "FirstRelevantTaskCompleted",
    };

    const GA4_MAP = {
        page_view: "page_view",
        signup_completed: "sign_up",
        checkout_started: "begin_checkout",
        first_relevant_task_completed: "first_relevant_task_completed",
    };

    // Espelha ALLOWED_GROWTH_PAGE_VIEW_PAGES do backend (endpoints Flask).
    const PAGE_ALLOW = {
        index: true,
        feed: true,
        login: true,
        request_password_reset: true,
        reset_password: true,
        complete_profile: true,
        chat_julia: true,
        fretes: true,
        controle_estoque: true,
        insights_frete: true,
        detalhe_noticia: true,
        newsletter_cancelar: true,
        admin_promocao_confirmar: true,
        admin_revogacao_confirmar: true,
        "user.perfil": true,
        "user.contrate_plano": true,
        "user.regularizar_pagamento": true,
        "agente_compara.agente_compara_page": true,
        "cleide.auditoria_frete": true,
        "cleide.cleide_auditoria": true,
        "multiuser_painel.gestao_multiuser": true,
        "multiuser_convite.visualizar_convite": true,
    };

    const SIGNUP_METHOD_ALLOW = {
        password: true,
        google: true,
    };

    const PLAN_ALLOW = {
        starter: true,
        pro: true,
        multiuser: true,
    };

    const sent = new Set();
    let enabled = true;

    function ownHas(obj, key) {
        return Object.prototype.hasOwnProperty.call(obj, key);
    }

    function disable() {
        enabled = false;
    }

    function enable() {
        enabled = true;
    }

    function isEnabled() {
        return enabled === true;
    }

    function sanitizePageViewParams(raw) {
        const out = {};
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
            return out;
        }
        if (!ownHas(raw, "page")) {
            return out;
        }
        const page = raw.page;
        if (typeof page !== "string") {
            return out;
        }
        const pageN = page.trim();
        if (!pageN || !ownHas(PAGE_ALLOW, pageN)) {
            return out;
        }
        out.page = pageN;
        return out;
    }

    function sanitizeSignupParams(raw) {
        const out = {};
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
            return out;
        }
        if (!ownHas(raw, "signup_method")) {
            return out;
        }
        const method = raw.signup_method;
        if (typeof method !== "string") {
            return out;
        }
        const methodN = method.trim().toLowerCase();
        if (!methodN || !ownHas(SIGNUP_METHOD_ALLOW, methodN)) {
            return out;
        }
        out.signup_method = methodN;
        return out;
    }

    function sanitizeCheckoutParams(raw) {
        const out = {};
        if (!raw || typeof raw !== "object" || Array.isArray(raw)) {
            return out;
        }
        if (!ownHas(raw, "plan")) {
            return out;
        }
        const plan = raw.plan;
        if (typeof plan !== "string") {
            return out;
        }
        const planN = plan.trim().toLowerCase();
        if (!planN || !ownHas(PLAN_ALLOW, planN)) {
            return out;
        }
        out.plan = planN;
        return out;
    }

    function sanitizeParams(eventName, raw) {
        if (eventName === "page_view") {
            return sanitizePageViewParams(raw);
        }
        if (eventName === "signup_completed") {
            return sanitizeSignupParams(raw);
        }
        if (eventName === "checkout_started") {
            return sanitizeCheckoutParams(raw);
        }
        if (eventName === "first_relevant_task_completed") {
            return {};
        }
        return {};
    }

    function dedupeKey(destination, eventName, token) {
        return destination + "|" + eventName + "|" + token;
    }

    function alreadySent(destination, eventName, token) {
        const key = dedupeKey(destination, eventName, token);
        if (sent.has(key)) {
            return true;
        }
        sent.add(key);
        return false;
    }

    function dispatchMeta(eventName, token, params) {
        if (typeof window.fbq !== "function") {
            return;
        }
        if (alreadySent("meta", eventName, token)) {
            return;
        }
        try {
            if (ownHas(META_CUSTOM_MAP, eventName)) {
                // Nome Meta fixo no cliente; nao vem do backend.
                window.fbq(
                    "trackCustom",
                    META_CUSTOM_MAP[eventName],
                    {},
                    { eventID: token }
                );
                return;
            }
            if (!ownHas(META_MAP, eventName)) {
                return;
            }
            const metaName = META_MAP[eventName];
            if (!metaName) {
                return;
            }
            window.fbq("track", metaName, params || {}, { eventID: token });
        } catch (_err) {
            // Falha Meta nao bloqueia Google nem Growth.
        }
    }

    function pageLocationSansQuery() {
        try {
            return window.location.origin + window.location.pathname;
        } catch (_err) {
            return undefined;
        }
    }

    function buildGa4Params(eventName, params) {
        const out = {};
        if (eventName === "page_view") {
            const loc = pageLocationSansQuery();
            if (loc) {
                out.page_location = loc;
            }
            if (params && typeof params.page === "string") {
                out.page_title = params.page;
            }
            return out;
        }
        if (eventName === "signup_completed" && params && typeof params.signup_method === "string") {
            out.method = params.signup_method;
            return out;
        }
        if (eventName === "checkout_started" && params && typeof params.plan === "string") {
            out.plan = params.plan;
            return out;
        }
        if (eventName === "first_relevant_task_completed") {
            return out;
        }
        return out;
    }

    function dispatchGoogle(eventName, token, params) {
        if (typeof window.gtag !== "function") {
            return;
        }
        if (alreadySent("ga4", eventName, token)) {
            return;
        }
        if (!ownHas(GA4_MAP, eventName)) {
            return;
        }
        const gaName = GA4_MAP[eventName];
        if (!gaName) {
            return;
        }
        try {
            // Token nao e enviado ao GA4 como mecanismo de dedupe.
            window.gtag("event", gaName, buildGa4Params(eventName, params));
        } catch (_err) {
            // Falha Google nao bloqueia Meta nem Growth.
        }
    }

    function dispatch(envelope) {
        try {
            if (!isEnabled()) {
                return false;
            }
            if (!envelope || typeof envelope !== "object") {
                return false;
            }
            const eventName = String(envelope.event || "").trim().toLowerCase();
            if (!eventName || !ownHas(ALLOWED_EVENTS, eventName)) {
                return false;
            }
            const token = String(envelope.token || "").trim();
            if (!token) {
                return false;
            }
            const params = sanitizeParams(eventName, envelope.params);
            try {
                dispatchMeta(eventName, token, params);
            } catch (_metaErr) {
                // isolado
            }
            try {
                dispatchGoogle(eventName, token, params);
            } catch (_gaErr) {
                // isolado
            }
            return true;
        } catch (_err) {
            return false;
        }
    }

    window.AFExternalTracking = {
        dispatch: dispatch,
        disable: disable,
        enable: enable,
        isEnabled: isEnabled,
    };
})();
