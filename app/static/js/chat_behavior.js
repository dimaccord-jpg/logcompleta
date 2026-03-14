/**
 * Comportamento do chat Júlia: foco/envio, contador freemium (X de N interações restantes),
 * bloqueio ao atingir limite e atualização a partir da resposta do backend.
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

  function updateCounterUI(restantes, limiteDia, limitReached) {
    var counterEl = byId('juliaChatCounter');
    var limitMsgEl = byId('juliaChatLimitMsg');
    var sendBtn = byId('juliaChatSend');
    var input = byId('juliaChatInput');
    if (!counterEl || !limitMsgEl || !sendBtn) return;
    if (chatLimits && limiteDia != null && restantes != null && !chatLimits.in_trial) {
      counterEl.style.display = 'block';
      counterEl.textContent = restantes + ' de ' + limiteDia + ' interações diárias restantes. A Júlia é uma IA e pode cometer erros.';
      if (limitReached || restantes <= 0) {
        limitMsgEl.style.display = 'block';
        limitMsgEl.textContent = 'Limite diário atingido. Volte amanhã ou assine um plano para continuar.';
        sendBtn.disabled = true;
        if (input) input.disabled = true;
      } else {
        limitMsgEl.style.display = 'none';
        sendBtn.disabled = false;
        if (input) input.disabled = false;
      }
    } else {
      counterEl.style.display = 'none';
      limitMsgEl.style.display = 'none';
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

  function appendMessage(role, text, container) {
    var welcome = byId('juliaChatWelcome');
    if (welcome) welcome.style.display = 'none';
    var msg = document.createElement('div');
    msg.className = 'julia-chat-msg julia-chat-msg-' + (role === 'user' ? 'user' : 'bot');
    var inner = document.createElement('div');
    inner.className = 'julia-chat-msg-inner';
    inner.textContent = text;
    msg.appendChild(inner);
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

  function sendMessage() {
    var input = byId('juliaChatInput');
    var form = byId('juliaChatForm');
    var messagesEl = byId('juliaChatMessages');
    if (!input || !form || !messagesEl) return;

    var text = (input.value || '').trim();
    if (!text) return;
    if (!isAuthenticated) {
      var loginUrl = (typeof window.JULIA_CHAT_LOGIN_URL !== 'undefined' && window.JULIA_CHAT_LOGIN_URL) ? window.JULIA_CHAT_LOGIN_URL : '/login';
      window.location.href = loginUrl;
      return;
    }
    if (chatLimits && !chatLimits.pode_usar_chat) return;

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

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: text, history: history })
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
        appendMessage('bot', data.reply || 'Sem resposta.', messagesEl);
        if (data.limit_reached !== undefined || data.chat_restantes !== undefined) {
          var rest = data.chat_restantes;
          var lim = data.chat_limite_dia;
          if (rest !== undefined && lim !== undefined) {
            chatLimits = chatLimits || {};
            chatLimits.restantes_hoje = rest;
            chatLimits.limite_dia = lim;
            chatLimits.pode_usar_chat = !data.limit_reached;
          }
          updateCounterUI(
            data.chat_restantes != null ? data.chat_restantes : (chatLimits && chatLimits.restantes_hoje),
            data.chat_limite_dia != null ? data.chat_limite_dia : (chatLimits && chatLimits.limite_dia),
            !!data.limit_reached
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

    if (chatLimits && chatLimits.limite_dia != null && !chatLimits.in_trial) {
      updateCounterUI(
        chatLimits.restantes_hoje != null ? chatLimits.restantes_hoje : chatLimits.limite_dia,
        chatLimits.limite_dia,
        !chatLimits.pode_usar_chat
      );
    }

    input.addEventListener('focus', function () { setChatActive(true); });
    input.addEventListener('blur', function () {
      if (!input.value.trim()) setChatActive(false);
    });

    form.addEventListener('submit', function (e) {
      e.preventDefault();
      sendMessage();
    });
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
