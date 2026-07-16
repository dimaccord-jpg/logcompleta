/**
 * Comportamento do chat na Home: Copilot de onboarding (discovery) ou Júlia operacional.
 */
(function () {
  'use strict';

  var HIDDEN_CLASS = 'julia-chat-content-hidden';
  var EXPANDED_CLASS = 'julia-chat-expanded';
  var DISCOVERY_MODE = (typeof window.ONBOARDING_DISCOVERY_MODE !== 'undefined' && window.ONBOARDING_DISCOVERY_MODE === true);
  var API_URL = DISCOVERY_MODE
    ? ((typeof window.ONBOARDING_DISCOVERY_API !== 'undefined' && window.ONBOARDING_DISCOVERY_API) || '/api/onboarding_discovery')
    : '/api/chat_julia';
  var DISCOVERY_PLACEHOLDERS = [
    'Pergunte sobre frete, custos, auditoria, planejamento ou estratégia logística...',
    'Ex.: Quero reduzir meu custo operacional',
    'Ex.: Minha transportadora está muito cara',
    'Ex.: Quero prever o comportamento do frete'
  ];

  function byId(id) { return document.getElementById(id); }
  function qsAll(sel) { return document.querySelectorAll(sel); }

  var TYPEWRITER_DONE_ATTR = 'data-typewriter-done';

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function runTypewriterOnElement(el, options) {
    if (!el || el.getAttribute('data-typewriter-enabled') !== 'true') return;
    if (el.getAttribute(TYPEWRITER_DONE_ATTR) === 'true') return;

    var text = el.getAttribute('data-typewriter-text') || '';
    if (!text) return;

    el.setAttribute(TYPEWRITER_DONE_ATTR, 'true');

    if (prefersReducedMotion()) {
      el.textContent = text;
      return;
    }

    el.textContent = '';
    var index = 0;
    var delayMs = (options && options.delayMs) || 36;

    function typeNextChar() {
      if (index >= text.length) {
        el.textContent = text;
        return;
      }
      el.textContent += text.charAt(index);
      index += 1;
      window.setTimeout(typeNextChar, delayMs);
    }

    typeNextChar();
  }

  function initOptInTypewriters(root) {
    var scope = root || document;
    var nodes = scope.querySelectorAll('[data-typewriter-enabled="true"][data-typewriter-text]');
    for (var i = 0; i < nodes.length; i++) {
      runTypewriterOnElement(nodes[i]);
    }
  }

  function resetOptInTypewriter(el) {
    if (!el || el.getAttribute('data-typewriter-enabled') !== 'true') return;
    el.removeAttribute(TYPEWRITER_DONE_ATTR);
    el.textContent = '';
    runTypewriterOnElement(el);
  }

  var chatLimits = (typeof window.JULIA_CHAT_LIMITS !== 'undefined' && window.JULIA_CHAT_LIMITS)
    ? window.JULIA_CHAT_LIMITS
    : null;
  var isAuthenticated = (typeof window.JULIA_CHAT_AUTHENTICATED !== 'undefined' && window.JULIA_CHAT_AUTHENTICATED === true);
  var discoveryState = (typeof window.ONBOARDING_DISCOVERY_STATE !== 'undefined' && window.ONBOARDING_DISCOVERY_STATE)
    ? window.ONBOARDING_DISCOVERY_STATE
    : { count: 0, limit: 5, limit_reached: false, has_active_session: false };
  var DISCOVERY_RESET_API = (typeof window.ONBOARDING_DISCOVERY_RESET_API !== 'undefined' && window.ONBOARDING_DISCOVERY_RESET_API)
    ? window.ONBOARDING_DISCOVERY_RESET_API
    : '/api/onboarding_discovery/reset';
  var activeCtaId = null;
  var discoveryPlaceholderIndex = 0;
  var discoveryPlaceholderTimer = null;
  var discoverySendInFlight = false;

  function isBlockedAuthorization(authz) {
    if (DISCOVERY_MODE) return false;
    if (!authz) return false;
    return authz.permitido === false || authz.modo_operacao === 'blocked';
  }

  function getBlockedMessage(authz) {
    if (!authz) return null;
    return authz.mensagem_usuario || null;
  }

  var PLAN_LIMIT_UPGRADE_LABEL = 'Faça o upgrade';
  var PLAN_LIMIT_UPGRADE_PATH = '/contrate-um-plano';

  function getUpgradeLimitPayload(source) {
    if (!source || typeof source !== 'object') return null;
    if (source.regularizacao_cta && typeof source.regularizacao_cta === 'object') {
      return source.regularizacao_cta;
    }
    if (source.upgrade_cta && typeof source.upgrade_cta === 'object') {
      return source.upgrade_cta;
    }
    if (
      (source.error_code === 'plan_limit_reached' || source.error_code === 'payment_renewal_failed')
      && source.upgrade_url
    ) {
      return source;
    }
    return null;
  }

  function resolvePlanLimitPayload(source) {
    var cta = getUpgradeLimitPayload(source);
    if (cta) return cta;
    if (source && source.authorization) {
      cta = getUpgradeLimitPayload(source.authorization);
      if (cta) return cta;
    }
    var plain = typeof source === 'string'
      ? source
      : (source && source.mensagem_usuario) || (source && source.reply) || '';
    if (!plain || plain.indexOf(PLAN_LIMIT_UPGRADE_LABEL) < 0) return null;
    var idx = plain.indexOf(PLAN_LIMIT_UPGRADE_LABEL);
    return {
      error_code: 'plan_limit_reached',
      message: plain.slice(0, idx),
      upgrade_url: PLAN_LIMIT_UPGRADE_PATH,
      upgrade_label: PLAN_LIMIT_UPGRADE_LABEL,
      message_suffix: plain.slice(idx + PLAN_LIMIT_UPGRADE_LABEL.length)
    };
  }

  function isJuliaPlanLimitResponse(data) {
    if (!data || typeof data !== 'object') return false;
    if (
      data.limit_reached === true
      || data.error_code === 'plan_limit_reached'
      || data.error_code === 'payment_renewal_failed'
    ) return true;
    if (data.authorization && isBlockedAuthorization(data.authorization)) {
      return !!resolvePlanLimitPayload(data.authorization);
    }
    return false;
  }

  function safeUpgradeHref(url) {
    var raw = String(url || '').trim();
    if (raw.indexOf('/') === 0 && raw.indexOf('//') !== 0) {
      return raw.split('?')[0] || PLAN_LIMIT_UPGRADE_PATH;
    }
    if (/^https?:\/\//i.test(raw)) return raw;
    return PLAN_LIMIT_UPGRADE_PATH;
  }

  function fillLimitMessageElement(el, payloadOrText) {
    if (!el) return;
    el.replaceChildren();
    var cta = resolvePlanLimitPayload(payloadOrText);
    if (cta) {
      el.appendChild(document.createTextNode(cta.message || ''));
      var link = document.createElement('a');
      link.href = safeUpgradeHref(cta.upgrade_url);
      link.textContent = cta.upgrade_label || PLAN_LIMIT_UPGRADE_LABEL;
      link.setAttribute('rel', 'noopener noreferrer');
      el.appendChild(link);
      el.appendChild(document.createTextNode(cta.message_suffix || ''));
      return;
    }
    var fallback = typeof payloadOrText === 'string'
      ? payloadOrText
      : (payloadOrText && payloadOrText.mensagem_usuario) || '';
    el.textContent = fallback || 'O chat está temporariamente indisponível para este usuário.';
  }

  function removePlanLimitBotMessages(messagesEl) {
    if (!messagesEl) return;
    var needle = 'Você atingiu o limite de uso do plano';
    var bots = messagesEl.querySelectorAll('.julia-chat-msg-bot');
    for (var i = 0; i < bots.length; i++) {
      var inner = bots[i].querySelector('.julia-chat-msg-inner');
      if (!inner) continue;
      var copy = (inner.textContent || '').trim();
      if (copy.indexOf(needle) >= 0) {
        bots[i].remove();
      }
    }
  }

  function showPlanLimitBanner(payloadOrText) {
    if (DISCOVERY_MODE) return;
    var limitMsgEl = byId('juliaChatLimitMsg');
    var sendBtn = byId('juliaChatSend');
    var input = byId('juliaChatInput');
    var messagesEl = byId('juliaChatMessages');
    if (!limitMsgEl || !sendBtn) return;
    limitMsgEl.style.display = 'block';
    fillLimitMessageElement(limitMsgEl, payloadOrText);
    sendBtn.disabled = true;
    if (input) input.disabled = true;
    removePlanLimitBotMessages(messagesEl);
  }

  function updateLimitUI(limitReached, payloadOrText) {
    if (DISCOVERY_MODE) return;
    var limitMsgEl = byId('juliaChatLimitMsg');
    var sendBtn = byId('juliaChatSend');
    var input = byId('juliaChatInput');
    if (!limitMsgEl || !sendBtn) return;
    if (limitReached) {
      showPlanLimitBanner(payloadOrText);
    } else {
      limitMsgEl.style.display = 'none';
      limitMsgEl.replaceChildren();
      sendBtn.disabled = false;
      if (input) input.disabled = false;
    }
  }

  function setDiscoverySuggestionsVisible(visible) {
    var suggestions = byId('juliaChatDiscoverySuggestions');
    if (!suggestions) return;
    suggestions.style.display = visible ? 'block' : 'none';
  }

  function updateDiscoveryGateUI(limitReached) {
    if (!DISCOVERY_MODE) return;
    var sendBtn = byId('juliaChatSend');
    var input = byId('juliaChatInput');
    if (sendBtn) sendBtn.disabled = !!limitReached;
    if (input) input.disabled = !!limitReached;
    if (limitReached) setDiscoverySuggestionsVisible(false);
  }

  function getDiscoverySessionMessage() {
    if (!discoveryState || !discoveryState.has_active_session) return '';
    var count = discoveryState.count || 0;
    var limit = discoveryState.limit || 5;
    if (discoveryState.limit_reached) {
      return 'Sua exploração gratuita desta sessão já atingiu o limite de '
        + limit + ' interações. Use "Nova conversa" para recomeçar ou faça login para continuar.';
    }
    if (count > 0) {
      return 'Interação ' + count + ' de ' + limit
        + ' nesta sessão. Para recomeçar do zero, use "Nova conversa".';
    }
    return 'Você já iniciou uma exploração nesta sessão. Para recomeçar do zero, use "Nova conversa".';
  }

  function applyDiscoveryStateFromPayload(data) {
    if (!DISCOVERY_MODE || !data) return;
    var count = typeof data.anonymous_interaction_count === 'number'
      ? data.anonymous_interaction_count
      : (discoveryState.count || 0);
    var limit = typeof data.anonymous_interaction_limit === 'number'
      ? data.anonymous_interaction_limit
      : (discoveryState.limit || 5);
    discoveryState = {
      count: count,
      limit: limit,
      limit_reached: data.limit_reached === true,
      has_active_session: count > 0 || data.limit_reached === true
    };
    updateDiscoveryGateUI(!!discoveryState.limit_reached);
    updateDiscoverySessionUI();
  }

  function updateDiscoverySessionUI() {
    if (!DISCOVERY_MODE) return;
    var stateWrap = byId('juliaChatSessionState');
    var stateCopy = byId('juliaChatSessionCopy');
    if (!stateWrap || !stateCopy) return;
    if (discoveryState && discoveryState.has_active_session) {
      stateCopy.textContent = getDiscoverySessionMessage();
      stateWrap.style.display = 'flex';
    } else {
      stateCopy.textContent = '';
      stateWrap.style.display = 'none';
    }
  }

  function clearDiscoveryVisualState() {
    var messagesEl = byId('juliaChatMessages');
    var welcome = byId('juliaChatWelcome');
    var input = byId('juliaChatInput');
    if (messagesEl) {
      var msgs = messagesEl.querySelectorAll('.julia-chat-msg');
      for (var i = 0; i < msgs.length; i++) msgs[i].remove();
    }
    if (welcome) {
      welcome.style.display = 'block';
      var typewriterEl = welcome.querySelector('[data-typewriter-enabled="true"]');
      if (typewriterEl) resetOptInTypewriter(typewriterEl);
    }
    if (input) input.value = '';
    setChatActive(false);
    setDiscoverySuggestionsVisible(true);
    updateDiscoveryGateUI(false);
  }

  function resetDiscoveryConversation() {
    if (!DISCOVERY_MODE) return Promise.resolve();
    return fetch(DISCOVERY_RESET_API, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status < 200 || res.status >= 300 || !res.data || res.data.ok !== true) {
          throw new Error('reset_failed');
        }
        discoveryState = {
          count: 0,
          limit: res.data.anonymous_interaction_limit || (discoveryState && discoveryState.limit) || 5,
          limit_reached: false,
          has_active_session: false
        };
        clearDiscoveryVisualState();
        updateDiscoverySessionUI();
        updateDiscoveryGateUI(false);
      });
  }

  function startDiscoveryPlaceholderRotation() {
    var input = byId('juliaChatInput');
    if (!DISCOVERY_MODE || !input) return;
    if (discoveryPlaceholderTimer) window.clearInterval(discoveryPlaceholderTimer);
    input.placeholder = DISCOVERY_PLACEHOLDERS[discoveryPlaceholderIndex];
    discoveryPlaceholderTimer = window.setInterval(function () {
      if (document.activeElement === input || (input.value || '').trim()) return;
      discoveryPlaceholderIndex = (discoveryPlaceholderIndex + 1) % DISCOVERY_PLACEHOLDERS.length;
      input.placeholder = DISCOVERY_PLACEHOLDERS[discoveryPlaceholderIndex];
    }, 3200);
  }

  function setChatActive(active) {
    var wrapper = byId('juliaChatWrapper');
    var contents = qsAll('.julia-home-content');
    if (!wrapper || !contents.length) return;
    if (active) {
      wrapper.classList.add(EXPANDED_CLASS);
      contents.forEach(function (el) { el.classList.add(HIDDEN_CLASS); });
    } else {
      wrapper.classList.remove(EXPANDED_CLASS);
      contents.forEach(function (el) { el.classList.remove(HIDDEN_CLASS); });
    }
  }

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderJuliaMarkdown(text) {
    function inlineFormat(raw) {
      var safe = escapeHtml(raw || '');
      safe = safe.replace(/\[([^\]\n]{1,120})\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, function (_, label, url) {
        return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
      });
      safe = safe.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, '<strong>$1</strong>');
      safe = safe.replace(/(^|[\s(])\*([^*\n][^*\n]*?)\*(?=[\s).,!?:;]|$)/g, '$1<em>$2</em>');
      return safe;
    }

    var lines = String(text || '').split(/\r?\n/);
    var htmlParts = [];
    var listItems = [];
    var i;
    for (i = 0; i < lines.length; i++) {
      var line = lines[i] || '';
      var listMatch = line.match(/^\s*\*\s+(.+)$/);
      if (listMatch) {
        listItems.push('<li>' + inlineFormat(listMatch[1]) + '</li>');
        continue;
      }
      if (listItems.length) {
        htmlParts.push('<ul>' + listItems.join('') + '</ul>');
        listItems = [];
      }
      if (!line.trim()) {
        htmlParts.push('<br>');
      } else {
        htmlParts.push(inlineFormat(line));
      }
    }
    if (listItems.length) {
      htmlParts.push('<ul>' + listItems.join('') + '</ul>');
    }
    return htmlParts.join('<br>');
  }

  function appendSuggestions(container, suggestions) {
    if (!Array.isArray(suggestions) || !suggestions.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'julia-chat-suggestions';
    suggestions.slice(0, 4).forEach(function (s) {
      if (!s) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'julia-chat-suggestion-btn';
      btn.setAttribute('data-julia-suggestion', String(s));
      btn.textContent = String(s);
      wrap.appendChild(btn);
    });
    if (wrap.childNodes.length) container.appendChild(wrap);
  }

  function classifyResponseActions(payload) {
    payload = payload || {};
    var actions = [];
    var limitReached = payload.limit_reached === true;
    var ctaLogin = payload.cta_login && typeof payload.cta_login === 'object' ? payload.cta_login : null;
    var handoffs = [];

    if (Array.isArray(payload.handoffs) && payload.handoffs.length) {
      handoffs = payload.handoffs.slice();
    } else if (payload.handoff && typeof payload.handoff === 'object') {
      handoffs = [payload.handoff];
    }

    if (limitReached && ctaLogin && ctaLogin.url) {
      actions.push({
        kind: 'limit',
        label: ctaLogin.label || 'Continuar gratuitamente',
        url: ctaLogin.url,
        requires_login: true
      });
    }

    handoffs.forEach(function (handoff) {
      if (!handoff) return;
      if (limitReached && handoff.requires_login === true) return;
      actions.push({
        kind: handoff.requires_login ? 'login_handoff' : 'suggestion',
        label: handoff.label || '',
        url: handoff.url || null,
        action: handoff.action || null,
        requires_login: handoff.requires_login === true
      });
    });

    return actions;
  }

  function appendResponseActions(container, actions) {
    if (!Array.isArray(actions) || !actions.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'julia-chat-response-actions';
    actions.forEach(function (actionItem) {
      if (!actionItem) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      if (actionItem.kind === 'limit') {
        btn.className = 'copilot-limit-btn';
        btn.setAttribute('data-handoff-url', actionItem.url);
        btn.setAttribute('data-handoff-login', '1');
        btn.textContent = actionItem.label || 'Continuar gratuitamente';
      } else if (actionItem.action === 'start_julia') {
        btn.className = actionItem.requires_login ? 'copilot-limit-btn' : 'copilot-suggestion-btn';
        // Preferir URL já normalizada pelo backend (/login?next=... ou canônica).
        if (actionItem.url) {
          btn.setAttribute('data-handoff-url', actionItem.url);
          btn.setAttribute('data-handoff-login', actionItem.requires_login ? '1' : '0');
        } else {
          btn.setAttribute('data-handoff-action', 'start_julia');
        }
        btn.textContent = actionItem.label || 'Continuar com Júlia gratuitamente';
      } else if (actionItem.url) {
        btn.className = actionItem.requires_login ? 'copilot-limit-btn' : 'copilot-suggestion-btn';
        btn.setAttribute('data-handoff-url', actionItem.url);
        btn.setAttribute('data-handoff-login', actionItem.requires_login ? '1' : '0');
        btn.textContent = actionItem.requires_login
          ? (actionItem.label || 'Continuar gratuitamente')
          : (actionItem.label ? ('Continuar: ' + actionItem.label) : 'Continuar');
      } else {
        return;
      }
      wrap.appendChild(btn);
    });
    if (wrap.childNodes.length) container.appendChild(wrap);
  }

  function enableJuliaOperationalMode() {
    DISCOVERY_MODE = false;
    API_URL = '/api/chat_julia';
    var nameEl = document.querySelector('.julia-chat-name');
    var roleEl = document.querySelector('.julia-chat-role');
    var welcome = byId('juliaChatWelcome');
    var input = byId('juliaChatInput');
    if (nameEl) nameEl.textContent = 'Júlia, Editora Virtual de AgenteFrete';
    if (roleEl) {
      roleEl.textContent = 'Consultoria em logística, supply chain, estratégia e planejamento. Atenção: a Júlia é uma IA e pode cometer erros.';
    }
    if (welcome) {
      welcome.innerHTML = '';
      var span = document.createElement('span');
      span.setAttribute('data-typewriter-enabled', 'true');
      span.setAttribute('data-typewriter-text', 'Faça uma pergunta sobre logística...');
      span.setAttribute('aria-live', 'polite');
      welcome.appendChild(span);
      runTypewriterOnElement(span);
      welcome.style.display = 'block';
    }
    if (input) input.placeholder = 'Mensagem para a Júlia...';
    setDiscoverySuggestionsVisible(false);
    updateLimitUI(isBlockedAuthorization(chatLimits), chatLimits);
  }

  function startJuliaOperationalHandoff() {
    var operationalUrl = '/chat_julia?mode=operational';
    if (!isAuthenticated) {
      var loginUrl = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL)
        ? window.JULIA_CHAT_LOGIN_URL
        : '/login';
      window.location.href = loginUrl + (loginUrl.indexOf('?') >= 0 ? '&' : '?') + 'next=' + encodeURIComponent(operationalUrl);
      return;
    }
    window.location.href = operationalUrl;
  }

  function navigateHandoff(handoffUrl) {
    if (!handoffUrl) return;
    window.location.href = handoffUrl;
  }

  function appendMessage(role, text, container, options) {
    options = options || {};
    var welcome = byId('juliaChatWelcome');
    if (welcome) welcome.style.display = 'none';
    setDiscoverySuggestionsVisible(false);
    var msg = document.createElement('div');
    msg.className = 'julia-chat-msg julia-chat-msg-' + (role === 'user' ? 'user' : 'bot');
    var inner = document.createElement('div');
    inner.className = 'julia-chat-msg-inner';
    if (role === 'user') {
      inner.textContent = text;
    } else {
      inner.innerHTML = renderJuliaMarkdown(text);
    }
    msg.appendChild(inner);
    if (role !== 'user') {
      appendSuggestions(msg, options.suggestions);
      appendResponseActions(msg, options.actions || []);
    }
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
    if (DISCOVERY_MODE) updateDiscoverySessionUI();
  }

  function setLoading(container, on) {
    var loadingId = 'juliaChatLoading';
    if (on) {
      var el = document.createElement('div');
      el.id = loadingId;
      el.className = 'julia-chat-msg julia-chat-msg-bot';
      el.innerHTML = '<div class="julia-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> '
        + (DISCOVERY_MODE ? 'Analisando seu cenário...' : 'Júlia está pensando...')
        + '</div>';
      container.appendChild(el);
      container.scrollTop = container.scrollHeight;
    } else {
      var loading = byId(loadingId);
      if (loading) loading.remove();
    }
  }

  function buildHistory(messagesEl) {
    var history = [];
    var msgs = messagesEl.querySelectorAll('.julia-chat-msg');
    for (var i = 0; i < msgs.length; i++) {
      var m = msgs[i];
      var isUser = m.classList.contains('julia-chat-msg-user');
      var content = m.querySelector('.julia-chat-msg-inner');
      if (content && !m.id) {
        history.push({ role: isUser ? 'user' : 'model', content: content.textContent.trim() });
      }
    }
    history.pop();
    var maxHistory = (typeof window.JULIA_CHAT_MAX_HISTORY !== 'undefined' && window.JULIA_CHAT_MAX_HISTORY > 0)
      ? window.JULIA_CHAT_MAX_HISTORY
      : 10;
    return history.slice(-maxHistory);
  }

  function sendMessage(forcedText, options) {
    options = options || {};
    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var messagesEl = byId('juliaChatMessages');
    var sendBtn = byId('juliaChatSend');
    if (!input || !form || !messagesEl) return;

    var text = (typeof forcedText === 'string' ? forcedText : (input.value || '')).trim();
    if (!text) return;

    if (DISCOVERY_MODE && discoverySendInFlight) return;
    if (DISCOVERY_MODE && discoveryState && discoveryState.limit_reached) {
      updateDiscoveryGateUI(true);
      return;
    }

    if (!DISCOVERY_MODE && !isAuthenticated) {
      var loginUrlEarly = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL)
        ? window.JULIA_CHAT_LOGIN_URL
        : '/login';
      window.location.href = loginUrlEarly;
      return;
    }
    if (isBlockedAuthorization(chatLimits)) {
      showPlanLimitBanner(chatLimits);
      return;
    }

    var ctaForRequest = options.cta_id !== undefined ? options.cta_id : activeCtaId;

    input.value = '';
    appendMessage('user', text, messagesEl);
    setChatActive(true);

    var history = buildHistory(messagesEl);
    setLoading(messagesEl, true);

    var payload = { message: text, history: history };
    if (DISCOVERY_MODE && ctaForRequest) {
      payload.cta_id = ctaForRequest;
    }
    if (!DISCOVERY_MODE && options.source === 'suggestion_chip') {
      payload.message = '[[JULIA_SUGGESTION::source=suggestion_chip;mode=execute_direct]] ' + text;
    }

    if (options.cta_id) {
      activeCtaId = null;
    }

    if (DISCOVERY_MODE) {
      discoverySendInFlight = true;
      if (sendBtn) sendBtn.disabled = true;
    }

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        var data = res.data;
        setLoading(messagesEl, false);
        if (res.status === 401 && !DISCOVERY_MODE) {
          appendMessage('bot', data.error || 'É necessário estar logado para conversar com a Júlia.', messagesEl);
          return;
        }
        if (!DISCOVERY_MODE && isJuliaPlanLimitResponse(data)) {
          if (data.authorization) {
            chatLimits = data.authorization;
          } else {
            chatLimits = chatLimits || {};
            chatLimits.permitido = false;
            chatLimits.modo_operacao = 'blocked';
          }
          showPlanLimitBanner(data.authorization || data);
          return;
        }
        if (DISCOVERY_MODE) {
          applyDiscoveryStateFromPayload(data);
        }
        if (!DISCOVERY_MODE && data.authorization) {
          chatLimits = data.authorization;
        }
        if (!DISCOVERY_MODE && data.limit_reached === true) {
          chatLimits = chatLimits || {};
          chatLimits.permitido = false;
          chatLimits.modo_operacao = 'blocked';
          showPlanLimitBanner(data.authorization || data);
          return;
        }
        var suggestions = DISCOVERY_MODE
          ? []
          : (data.refinement_options || data.suggestions || []);
        var actions = classifyResponseActions(data);
        appendMessage('bot', data.reply || 'Sem resposta.', messagesEl, {
          suggestions: suggestions,
          actions: actions
        });
      })
      .catch(function () {
        setLoading(messagesEl, false);
        appendMessage('bot', 'Não foi possível obter resposta. Tente novamente.', messagesEl);
      })
      .finally(function () {
        if (DISCOVERY_MODE) {
          discoverySendInFlight = false;
          if (sendBtn && !(discoveryState && discoveryState.limit_reached)) {
            sendBtn.disabled = false;
          }
        }
      });
  }

  function submitSuggestion(text, options) {
    var input = byId('juliaChatInput');
    if (!text || !text.trim()) return;
    activeCtaId = options && options.cta_id ? options.cta_id : null;
    if (input) input.value = text;
    sendMessage(text, options || {});
  }

  function initCtaButtons() {
    var buttons = qsAll('.onboarding-cta-btn');
    if (!buttons.length) return;
    buttons.forEach(function (btn) {
      btn.addEventListener('click', function () {
        var ctaId = btn.getAttribute('data-cta-id') || '';
        var ctaMessage = btn.getAttribute('data-cta-message') || btn.textContent.trim();
        submitSuggestion(ctaMessage, { cta_id: ctaId });
      });
    });
  }

  function init() {
    initOptInTypewriters();

    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var wrapper = byId('juliaChatWrapper');
    var resetBtn = byId('juliaChatResetBtn');
    if (!input || !form || !wrapper) return;

    updateLimitUI(
      isBlockedAuthorization(chatLimits),
      chatLimits
    );
    updateDiscoveryGateUI(!!(discoveryState && discoveryState.limit_reached));
    setDiscoverySuggestionsVisible(!(discoveryState && discoveryState.limit_reached));
    startDiscoveryPlaceholderRotation();
    updateDiscoverySessionUI();

    input.addEventListener('focus', function () { setChatActive(true); });
    input.addEventListener('blur', function () {
      if (!input.value.trim()) setChatActive(false);
    });
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessage();
      }
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      sendMessage();
    });

    if (resetBtn) {
      resetBtn.addEventListener('click', function () {
        resetBtn.disabled = true;
        resetDiscoveryConversation()
          .catch(function () {
            var messagesEl = byId('juliaChatMessages');
            if (messagesEl) {
              appendMessage('bot', 'Não foi possível iniciar uma nova conversa agora. Tente novamente.', messagesEl);
            }
          })
          .finally(function () {
            resetBtn.disabled = false;
          });
      });
    }

    var messagesEl = byId('juliaChatMessages');
    if (messagesEl) {
      messagesEl.addEventListener('click', function (e) {
        var handoffBtn = e.target && e.target.closest
          ? e.target.closest('.copilot-suggestion-btn, .copilot-limit-btn')
          : null;
        if (handoffBtn) {
          var url = handoffBtn.getAttribute('data-handoff-url') || '';
          if (url) {
            navigateHandoff(url);
            return;
          }
          // Compatibilidade mínima: handoff legado da Júlia sem URL no payload.
          var action = handoffBtn.getAttribute('data-handoff-action') || '';
          if (action === 'start_julia') {
            startJuliaOperationalHandoff();
            return;
          }
          return;
        }

        var suggestionBtn = e.target && e.target.closest ? e.target.closest('.julia-chat-suggestion-btn') : null;
        if (!suggestionBtn) return;
        var suggestion = suggestionBtn.getAttribute('data-julia-suggestion') || '';
        submitSuggestion(suggestion, { source: 'suggestion_chip' });
      });
    }

    initCtaButtons();

  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
