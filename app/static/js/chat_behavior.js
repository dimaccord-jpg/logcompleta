/**
 * Comportamento do chat Júlia: foco/envio e atualização de estado de bloqueio.
 */
(function () {
  'use strict';

  var HIDDEN_CLASS = 'julia-chat-content-hidden';
  var EXPANDED_CLASS = 'julia-chat-expanded';
  var API_URL = '/api/chat_julia';

  function byId(id) { return document.getElementById(id); }
  function qs(sel) { return document.querySelector(sel); }
  function qsAll(sel) { return document.querySelectorAll(sel); }

  var chatLimits = (typeof window.JULIA_CHAT_LIMITS !== 'undefined' && window.JULIA_CHAT_LIMITS)
    ? window.JULIA_CHAT_LIMITS
    : null;
  var isAuthenticated = (typeof window.JULIA_CHAT_AUTHENTICATED !== 'undefined' && window.JULIA_CHAT_AUTHENTICATED === true);

  function isBlockedAuthorization(authz) {
    if (!authz) return false;
    return authz.permitido === false || authz.modo_operacao === 'blocked';
  }

  function getBlockedMessage(authz) {
    if (!authz) return null;
    return authz.mensagem_usuario || null;
  }

  function updateLimitUI(limitReached, message) {
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
    suggestions.slice(0, 3).forEach(function (s) {
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

  function appendMessage(role, text, container, options) {
    options = options || {};
    var welcome = byId('juliaChatWelcome');
    if (welcome) welcome.style.display = 'none';
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
      el.innerHTML = '<div class="julia-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> Júlia está pensando...</div>';
      container.appendChild(el);
      container.scrollTop = container.scrollHeight;
    } else {
      var loading = byId(loadingId);
      if (loading) loading.remove();
    }
  }

  function sendMessage(forcedText, options) {
    options = options || {};
    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var messagesEl = byId('juliaChatMessages');
    if (!input || !form || !messagesEl) return;

    var text = (typeof forcedText === 'string' ? forcedText : (input.value || '')).trim();
    if (!text) return;
    if (!isAuthenticated) {
      var loginUrl = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL) ? window.JULIA_CHAT_LOGIN_URL : '/login';
      window.location.href = loginUrl;
      return;
    }
    if (isBlockedAuthorization(chatLimits)) {
      updateLimitUI(true, getBlockedMessage(chatLimits) || 'Você não pode usar o chat neste momento.');
      return;
    }

    input.value = '';
    appendMessage('user', text, messagesEl);
    setChatActive(true);

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
    history = history.slice(-maxHistory);

    setLoading(messagesEl, true);

    var transportText = text;
    if (options.source === 'suggestion_chip') {
      transportText = '[[JULIA_SUGGESTION::source=suggestion_chip;mode=execute_direct]] ' + text;
    }

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: transportText, history: history })
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        var data = res.data;
        setLoading(messagesEl, false);
        if (res.status === 401) {
          appendMessage('bot', data.error || 'É necessário estar logado para conversar com a Júlia.', messagesEl);
          return;
        }
        appendMessage('bot', data.reply || 'Sem resposta.', messagesEl, { suggestions: data.suggestions || [] });
        if (data.authorization) {
          chatLimits = data.authorization;
        }
        if (data.limit_reached !== undefined) {
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

  function init() {
    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var wrapper = byId('juliaChatWrapper');
    if (!input || !form || !wrapper) return;

    updateLimitUI(
      isBlockedAuthorization(chatLimits),
      getBlockedMessage(chatLimits)
    );

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
        var target = e.target;
        if (!target || !target.matches('.julia-chat-suggestion-btn')) return;
        var suggestion = target.getAttribute('data-julia-suggestion') || '';
        if (!suggestion.trim()) return;
        input.value = suggestion;
        sendMessage(undefined, { source: 'suggestion_chip' });
      });
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
