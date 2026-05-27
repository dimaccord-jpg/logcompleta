/**
 * Comportamento do chat na Home: shell visual Júlia + Cleiton Discovery AI (onboarding).
 */
(function () {
  'use strict';

  var HIDDEN_CLASS = 'julia-chat-content-hidden';
  var EXPANDED_CLASS = 'julia-chat-expanded';
  var DISCOVERY_MODE = (typeof window.ONBOARDING_DISCOVERY_MODE !== 'undefined' && window.ONBOARDING_DISCOVERY_MODE === true);
  var API_URL = DISCOVERY_MODE
    ? ((typeof window.ONBOARDING_DISCOVERY_API !== 'undefined' && window.ONBOARDING_DISCOVERY_API) || '/api/onboarding_discovery')
    : '/api/chat_julia';
  var JULIA_PENDING_KEY = 'pending_julia_operational';
  var DISCOVERY_PLACEHOLDERS = [
    'Ex.: Quero reduzir meu custo operacional',
    'Ex.: Minha transportadora está muito cara',
    'Ex.: Quero prever o comportamento do frete',
    'Ex.: Preciso melhorar meus indicadores logísticos'
  ];

  function byId(id) { return document.getElementById(id); }
  function qsAll(sel) { return document.querySelectorAll(sel); }

  var chatLimits = (typeof window.JULIA_CHAT_LIMITS !== 'undefined' && window.JULIA_CHAT_LIMITS)
    ? window.JULIA_CHAT_LIMITS
    : null;
  var isAuthenticated = (typeof window.JULIA_CHAT_AUTHENTICATED !== 'undefined' && window.JULIA_CHAT_AUTHENTICATED === true);
  var activeCtaId = null;
  var discoveryPlaceholderIndex = 0;
  var discoveryPlaceholderTimer = null;

  function isBlockedAuthorization(authz) {
    if (DISCOVERY_MODE) return false;
    if (!authz) return false;
    return authz.permitido === false || authz.modo_operacao === 'blocked';
  }

  function getBlockedMessage(authz) {
    if (!authz) return null;
    return authz.mensagem_usuario || null;
  }

  function updateLimitUI(limitReached, message) {
    if (DISCOVERY_MODE) return;
    var limitMsgEl = byId('juliaChatLimitMsg');
    var sendBtn = byId('juliaChatSend');
    var input = byId('juliaChatInput');
    if (!limitMsgEl || !sendBtn) return;
    if (limitReached) {
      limitMsgEl.style.display = 'block';
      limitMsgEl.innerHTML = renderJuliaMarkdown(
        message || 'O chat está temporariamente indisponível para este usuário.'
      );
      sendBtn.disabled = true;
      if (input) input.disabled = true;
    } else {
      limitMsgEl.style.display = 'none';
      limitMsgEl.innerHTML = '';
      sendBtn.disabled = false;
      if (input) input.disabled = false;
    }
  }

  function setDiscoverySuggestionsVisible(visible) {
    var suggestions = byId('juliaChatDiscoverySuggestions');
    if (!suggestions) return;
    suggestions.style.display = visible ? 'block' : 'none';
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

  function appendHandoff(container, handoff) {
    if (!handoff) return;
    var wrap = document.createElement('div');
    wrap.className = 'julia-chat-handoff-wrap';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'julia-chat-handoff-btn';
    if (handoff.action === 'start_julia') {
      btn.setAttribute('data-handoff-action', 'start_julia');
      btn.textContent = handoff.label || 'Conversar com a Júlia';
    } else if (handoff.url) {
      btn.setAttribute('data-handoff-url', handoff.url);
      btn.setAttribute('data-handoff-login', '0');
      btn.textContent = handoff.label ? ('Continuar: ' + handoff.label) : 'Continuar';
    } else {
      return;
    }
    wrap.appendChild(btn);
    container.appendChild(wrap);
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
      welcome.textContent = 'Faça uma pergunta sobre logística, fretes, supply chain, estratégia ou planejamento.';
      welcome.style.display = 'block';
    }
    if (input) input.placeholder = 'Mensagem para a Júlia...';
    setDiscoverySuggestionsVisible(false);
    updateLimitUI(isBlockedAuthorization(chatLimits), getBlockedMessage(chatLimits));
  }

  function startJuliaOperationalHandoff() {
    if (!isAuthenticated) {
      try { sessionStorage.setItem(JULIA_PENDING_KEY, '1'); } catch (e) { /* ignore */ }
      var loginUrl = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL)
        ? window.JULIA_CHAT_LOGIN_URL
        : '/login';
      window.location.href = loginUrl + (loginUrl.indexOf('?') >= 0 ? '&' : '?') + 'next=' + encodeURIComponent('/');
      return;
    }
    enableJuliaOperationalMode();
    var input = byId('juliaChatInput');
    if (input) input.focus();
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
      appendHandoff(msg, options.handoff);
    }
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function setLoading(container, on) {
    var loadingId = 'juliaChatLoading';
    if (on) {
      var el = document.createElement('div');
      el.id = loadingId;
      el.className = 'julia-chat-msg julia-chat-msg-bot';
      el.innerHTML = '<div class="julia-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> '
        + (DISCOVERY_MODE ? 'Analisando sua intenção...' : 'Júlia está pensando...')
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
    if (!input || !form || !messagesEl) return;

    var text = (typeof forcedText === 'string' ? forcedText : (input.value || '')).trim();
    if (!text) return;

    if (!DISCOVERY_MODE && !isAuthenticated) {
      var loginUrlEarly = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL)
        ? window.JULIA_CHAT_LOGIN_URL
        : '/login';
      window.location.href = loginUrlEarly;
      return;
    }
    if (isBlockedAuthorization(chatLimits)) {
      updateLimitUI(true, getBlockedMessage(chatLimits) || 'Você não pode usar o chat neste momento.');
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
        var suggestions = data.refinement_options || data.suggestions || [];
        appendMessage('bot', data.reply || 'Sem resposta.', messagesEl, {
          suggestions: suggestions,
          handoff: data.handoff || null
        });
        if (!DISCOVERY_MODE && data.authorization) {
          chatLimits = data.authorization;
        }
        if (!DISCOVERY_MODE && data.limit_reached !== undefined) {
          chatLimits = chatLimits || {};
          chatLimits.permitido = !data.limit_reached;
          if (data.limit_reached) {
            chatLimits.modo_operacao = 'blocked';
          }
          updateLimitUI(
            !!data.limit_reached,
            getBlockedMessage(chatLimits) || data.reply
          );
        }
      })
      .catch(function () {
        setLoading(messagesEl, false);
        appendMessage('bot', 'Não foi possível obter resposta. Tente novamente.', messagesEl);
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
    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var wrapper = byId('juliaChatWrapper');
    if (!input || !form || !wrapper) return;

    updateLimitUI(
      isBlockedAuthorization(chatLimits),
      getBlockedMessage(chatLimits)
    );
    setDiscoverySuggestionsVisible(true);
    startDiscoveryPlaceholderRotation();

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

    var messagesEl = byId('juliaChatMessages');
    if (messagesEl) {
      messagesEl.addEventListener('click', function (e) {
        var handoffBtn = e.target && e.target.closest ? e.target.closest('.julia-chat-handoff-btn') : null;
        if (handoffBtn) {
          var action = handoffBtn.getAttribute('data-handoff-action') || '';
          if (action === 'start_julia') {
            startJuliaOperationalHandoff();
            return;
          }
          var url = handoffBtn.getAttribute('data-handoff-url') || '';
          navigateHandoff(url);
          return;
        }

        var suggestionBtn = e.target && e.target.closest ? e.target.closest('.julia-chat-suggestion-btn') : null;
        if (!suggestionBtn) return;
        var suggestion = suggestionBtn.getAttribute('data-julia-suggestion') || '';
        submitSuggestion(suggestion, { source: 'suggestion_chip' });
      });
    }

    initCtaButtons();

    if (DISCOVERY_MODE && isAuthenticated) {
      try {
        if (sessionStorage.getItem(JULIA_PENDING_KEY) === '1') {
          sessionStorage.removeItem(JULIA_PENDING_KEY);
          enableJuliaOperationalMode();
        }
      } catch (e) { /* ignore */ }
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
