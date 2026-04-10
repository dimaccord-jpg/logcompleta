(function () {
  'use strict';

  function byId(id) { return document.getElementById(id); }

  var API_URL = '/api/chat_roberto';
  var panel = byId('robertoChatPanel');
  var toggleBtn = byId('robertoChatToggle');
  var closeBtn = byId('robertoChatClose');
  var form = byId('robertoChatForm');
  var input = byId('robertoChatInput');
  var messages = byId('robertoChatMessages');
  var welcome = byId('robertoChatWelcome');
  var limitMsg = byId('robertoChatLimitMsg');
  var proactive = byId('robertoChatProactive');

  if (!panel || !toggleBtn || !form || !input || !messages) return;

  var chatLimits = window.ROBERTO_CHAT_LIMITS || null;
  var isAuthenticated = window.ROBERTO_CHAT_AUTHENTICATED === true;
  var maxHistory = (window.ROBERTO_CHAT_MAX_HISTORY && window.ROBERTO_CHAT_MAX_HISTORY > 0)
    ? window.ROBERTO_CHAT_MAX_HISTORY
    : 10;
  var proactiveMsgs = [
    'Quer continuidade da análise com foco em riscos e desvios?',
    'Posso sintetizar os principais sinais por período, UF e modal.',
    'Se quiser, monto um e-mail executivo com os achados atuais.'
  ];
  var proactiveIdx = 0;
  var proactiveTimer = null;

  function isBlocked(authz) {
    return !!(authz && (authz.permitido === false || authz.modo_operacao === 'blocked'));
  }

  function blockedMessage(authz) {
    return (authz && authz.mensagem_usuario) ? authz.mensagem_usuario : null;
  }

  function esc(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function render(text) {
    var safe = esc(text || '');
    safe = safe.replace(/\[([^\]\n]{1,140})\]\(((?:https?:\/\/|\/)[^\s)]+)\)/g, function (_, label, url) {
      return '<a href="' + url + '" target="_blank" rel="noopener noreferrer">' + label + '</a>';
    });
    var lines = safe.split(/\r?\n/);
    var out = [];
    for (var i = 0; i < lines.length; i++) {
      var line = lines[i] || '';
      if (!line.trim()) {
        out.push('<br>');
      } else {
        out.push(line);
      }
    }
    return out.join('<br>');
  }

  function updateLimitUI(on, msg) {
    var sendBtn = byId('robertoChatSend');
    if (!limitMsg || !sendBtn) return;
    if (on) {
      limitMsg.style.display = 'block';
      limitMsg.innerHTML = render(msg || 'Chat indisponível para este usuário no momento.');
      sendBtn.disabled = true;
      input.disabled = true;
    } else {
      limitMsg.style.display = 'none';
      limitMsg.innerHTML = '';
      sendBtn.disabled = false;
      input.disabled = false;
    }
  }

  function setOpen(open) {
    panel.style.display = open ? 'flex' : 'none';
    if (!open) {
      startProactive();
      return;
    }
    stopProactive();
    proactive.style.display = 'none';
    input.focus();
  }

  function appendSuggestions(container, suggestions) {
    if (!Array.isArray(suggestions) || !suggestions.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'roberto-chat-suggestions';
    suggestions.slice(0, 3).forEach(function (s) {
      if (!s) return;
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'roberto-chat-suggestion-btn';
      btn.setAttribute('data-roberto-suggestion', String(s));
      btn.textContent = String(s);
      wrap.appendChild(btn);
    });
    if (wrap.childNodes.length) container.appendChild(wrap);
  }

  function buildCopyAction() {
    var actions = document.createElement('div');
    actions.className = 'roberto-chat-actions';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'roberto-chat-copy-btn';
    btn.setAttribute('data-roberto-copy', '1');
    btn.setAttribute('aria-label', 'Copiar resposta do Roberto');
    btn.textContent = 'Copiar';
    actions.appendChild(btn);
    return actions;
  }

  function markCopied(button) {
    if (!button) return;
    var original = button.getAttribute('data-copy-label') || 'Copiar';
    button.setAttribute('data-copy-label', original);
    button.textContent = 'Copiado';
    button.classList.add('is-copied');
    window.setTimeout(function () {
      button.textContent = original;
      button.classList.remove('is-copied');
    }, 1200);
  }

  function copyTextToClipboard(text) {
    var value = String(text || '').trim();
    if (!value) return Promise.reject(new Error('empty'));
    if (navigator.clipboard && typeof navigator.clipboard.writeText === 'function') {
      return navigator.clipboard.writeText(value);
    }
    return new Promise(function (resolve, reject) {
      var area = document.createElement('textarea');
      area.value = value;
      area.setAttribute('readonly', 'readonly');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      area.style.pointerEvents = 'none';
      area.style.left = '-9999px';
      document.body.appendChild(area);
      area.focus();
      area.select();
      var ok = false;
      try {
        ok = document.execCommand('copy');
      } catch (_) {
        ok = false;
      }
      document.body.removeChild(area);
      if (ok) resolve();
      else reject(new Error('copy-failed'));
    });
  }

  function getMessageTextForCopy(msgNode) {
    if (!msgNode) return '';
    var inner = msgNode.querySelector('.roberto-chat-msg-inner');
    if (!inner) return '';
    return (inner.textContent || '').trim();
  }

  function decorateExistingBotMessages() {
    var nodes = messages.querySelectorAll('.roberto-chat-msg-bot');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.querySelector('.roberto-chat-copy-btn')) continue;
      n.appendChild(buildCopyAction());
    }
  }

  function appendMessage(role, text, options) {
    options = options || {};
    if (welcome) welcome.style.display = 'none';
    var msg = document.createElement('div');
    msg.className = 'roberto-chat-msg roberto-chat-msg-' + (role === 'user' ? 'user' : 'bot');
    var inner = document.createElement('div');
    inner.className = 'roberto-chat-msg-inner';
    if (role === 'user') {
      inner.textContent = text;
    } else {
      inner.innerHTML = render(text);
    }
    msg.appendChild(inner);
    if (role !== 'user') {
      msg.appendChild(buildCopyAction());
      appendSuggestions(msg, options.suggestions);
    }
    messages.appendChild(msg);
    messages.scrollTop = messages.scrollHeight;
  }

  function setLoading(on) {
    var loadingId = 'robertoChatLoading';
    if (on) {
      var el = document.createElement('div');
      el.id = loadingId;
      el.className = 'roberto-chat-msg roberto-chat-msg-bot';
      el.innerHTML = '<div class="roberto-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> Roberto está analisando...</div>';
      messages.appendChild(el);
      messages.scrollTop = messages.scrollHeight;
    } else {
      var old = byId(loadingId);
      if (old) old.remove();
    }
  }

  function readHistory() {
    var out = [];
    var nodes = messages.querySelectorAll('.roberto-chat-msg');
    for (var i = 0; i < nodes.length; i++) {
      var n = nodes[i];
      if (n.id === 'robertoChatLoading') continue;
      var content = n.querySelector('.roberto-chat-msg-inner');
      if (!content) continue;
      var isUser = n.classList.contains('roberto-chat-msg-user');
      out.push({ role: isUser ? 'user' : 'model', content: content.textContent.trim() });
    }
    // remove a última (mensagem atual do usuário) antes de enviar
    out.pop();
    return out.slice(-maxHistory);
  }

  function sendMessage(forcedText, options) {
    options = options || {};
    var text = (typeof forcedText === 'string' ? forcedText : (input.value || '')).trim();
    if (!text) return;
    if (!isAuthenticated) {
      window.location.href = '/login';
      return;
    }
    if (isBlocked(chatLimits)) {
      updateLimitUI(true, blockedMessage(chatLimits));
      return;
    }

    input.value = '';
    appendMessage('user', text);
    setOpen(true);
    setLoading(true);

    var payloadText = text;
    if (options.source === 'proactive_chip') {
      payloadText = '[[ROBERTO_SUGGESTION::source=proactive_chip;mode=execute_direct]] ' + text;
    }

    fetch(API_URL, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: payloadText, history: readHistory() })
    })
      .then(function (r) { return r.json().then(function (d) { return { status: r.status, data: d }; }); })
      .then(function (res) {
        setLoading(false);
        var data = res.data || {};
        if (res.status === 401) {
          appendMessage('bot', data.error || 'É necessário login para usar o chat.');
          return;
        }
        appendMessage('bot', data.reply || 'Sem resposta.', { suggestions: data.suggestions || [] });
        if (data.authorization) chatLimits = data.authorization;
        if (data.max_history && data.max_history > 0) maxHistory = data.max_history;
        if (data.limit_reached !== undefined) {
          chatLimits = chatLimits || {};
          chatLimits.permitido = !data.limit_reached;
          if (data.limit_reached) chatLimits.modo_operacao = 'blocked';
          updateLimitUI(!!data.limit_reached, blockedMessage(chatLimits) || data.reply);
        }
      })
      .catch(function () {
        setLoading(false);
        appendMessage('bot', 'Falha ao obter resposta. Tente novamente.');
      });
  }

  function setProactiveMessage(text) {
    proactive.innerHTML = esc(text);
    proactive.style.display = 'block';
  }

  function tickProactive() {
    if (panel.style.display !== 'none') return;
    setProactiveMessage(proactiveMsgs[proactiveIdx % proactiveMsgs.length]);
    proactiveIdx += 1;
  }

  function startProactive() {
    if (proactiveTimer) return;
    tickProactive();
    proactiveTimer = window.setInterval(tickProactive, 18000);
  }

  function stopProactive() {
    if (!proactiveTimer) return;
    window.clearInterval(proactiveTimer);
    proactiveTimer = null;
  }

  toggleBtn.addEventListener('click', function () {
    setOpen(panel.style.display === 'none');
  });
  if (closeBtn) {
    closeBtn.addEventListener('click', function () { setOpen(false); });
  }
  form.addEventListener('submit', function (e) {
    e.preventDefault();
    sendMessage();
  });
  input.addEventListener('keydown', function (e) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      sendMessage();
    }
  });
  proactive.addEventListener('click', function () {
    setOpen(true);
    input.value = proactiveMsgs[(proactiveIdx - 1 + proactiveMsgs.length) % proactiveMsgs.length];
    sendMessage(undefined, { source: 'proactive_chip' });
  });
  messages.addEventListener('click', function (e) {
    var target = e.target;
    if (!target) return;
    if (target.matches('.roberto-chat-copy-btn')) {
      e.preventDefault();
      var msgNode = target.closest('.roberto-chat-msg-bot');
      var text = getMessageTextForCopy(msgNode);
      copyTextToClipboard(text)
        .then(function () { markCopied(target); })
        .catch(function () {
          target.textContent = 'Falha';
          window.setTimeout(function () { target.textContent = 'Copiar'; }, 1200);
        });
      return;
    }
    if (target.matches('.roberto-chat-suggestion-btn')) {
      var suggestion = target.getAttribute('data-roberto-suggestion') || '';
      if (!suggestion.trim()) return;
      input.value = suggestion;
      sendMessage(undefined, { source: 'proactive_chip' });
    }
  });

  updateLimitUI(isBlocked(chatLimits), blockedMessage(chatLimits));
  decorateExistingBotMessages();
  setOpen(false);
})();
