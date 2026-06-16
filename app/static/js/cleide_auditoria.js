(function () {
  'use strict';

  var API_STATUS = '/api/cleide-auditoria/documents/status';
  var API_UPLOAD = '/api/cleide-auditoria/documents/upload';
  var API_CLEAR = '/api/cleide-auditoria/documents/clear';
  var API_CHAT = '/api/cleide-auditoria/chat';
  var uploadInFlight = false;
  var chatInFlight = false;
  var chatHistory = [];
  var MAX_CHAT_HISTORY = 10;
  var CHAT_LOADING_ID = 'cleideAuditoriaChatLoading';

  var ERROR_MESSAGES = {
    cleiton_doc_file_too_large: 'Arquivo acima do limite configurado para este tipo.',
    cleiton_doc_invalid_size: 'Tamanho de arquivo inválido.',
    cleiton_doc_session_bytes: 'Você atingiu o limite total de documentos desta sessão.',
    cleiton_doc_max_files: 'Você atingiu o limite de documentos desta sessão.',
    cleiton_doc_invalid_extension: 'Tipo de arquivo não suportado nesta fase.',
    cleiton_doc_invalid_mime: 'Tipo de arquivo não suportado nesta fase.',
    cleiton_doc_disabled_type: 'Este tipo de arquivo está desabilitado no momento.',
    cleiton_doc_upload_disabled: 'Upload documental desabilitado no momento.',
    cleiton_doc_empty_file: 'O arquivo enviado está vazio.',
    cleiton_doc_corrupted_file: 'Não consegui preparar este arquivo com segurança.',
    cleiton_doc_conversion_failed: 'Não consegui preparar este arquivo com segurança.',
    cleiton_doc_unsupported_type: 'Tipo de arquivo não suportado nesta fase.',
    cleiton_doc_unsafe_filename: 'Nome de arquivo inválido.',
    cleiton_doc_too_deep_xml: 'Não consegui preparar este arquivo com segurança.',
    cleiton_doc_too_many_nodes: 'Não consegui preparar este arquivo com segurança.',
    cleiton_doc_missing_file: 'Nenhum arquivo selecionado.',
    cleiton_doc_upload_failed: 'Não foi possível enviar o documento. Tente novamente.',
    auth_required: 'É necessário estar logado para anexar documentos.',
    franquia_blocked: 'Operação indisponível para este usuário no momento.'
  };

  var CHAT_FIXED_ERRORS = {
    login: 'É necessário estar logado para conversar com a Cleide.',
    network: 'Não foi possível obter resposta. Verifique sua conexão e tente novamente.',
    service: 'O serviço está indisponível no momento. Tente novamente em instantes.'
  };

  var TEMP_TABLE_OPERATIONAL_MESSAGES = {
    processing: 'Recebi os anexos e iniciei a estruturação da tabela temporária de frete.',
    awaiting_validation: 'A tabela temporária foi estruturada e está aguardando sua validação.',
    needs_review: 'A tabela temporária foi gerada. Revise os dados antes de continuar.',
    failed: 'Não foi possível estruturar a tabela temporária a partir dos anexos enviados.',
    expired: 'A tabela temporária desta sessão expirou.',
    discarded: 'Os documentos de origem foram alterados ou removidos, então a tabela temporária anterior foi invalidada.'
  };

  var lastAnnouncedTempTableStatus = null;
  var tempTablePollTimer = null;
  var TEMP_TABLE_POLL_MS = 2500;
  var currentTempTable = null;
  var lastTempTableCardButton = null;

  function byId(id) {
    return document.getElementById(id);
  }

  function appendOperationalMessage(text) {
    if (!text) return;
    appendChatBubble('assistant', text);
    chatHistory.push({ role: 'assistant', content: text });
    chatHistory = trimChatHistory(chatHistory);
  }

  function announceTempTableStatusIfNeeded(tempTable) {
    if (!tempTable || !tempTable.status) return;
    var status = String(tempTable.status).toLowerCase();
    if (status === lastAnnouncedTempTableStatus) return;
    var message = TEMP_TABLE_OPERATIONAL_MESSAGES[status];
    if (!message) return;
    lastAnnouncedTempTableStatus = status;
    appendOperationalMessage(message);
  }

  function stopTempTablePolling() {
    if (tempTablePollTimer) {
      window.clearInterval(tempTablePollTimer);
      tempTablePollTimer = null;
    }
  }

  function startTempTablePollingIfNeeded(tempTable) {
    stopTempTablePolling();
    if (!tempTable || String(tempTable.status || '').toLowerCase() !== 'processing') return;
    tempTablePollTimer = window.setInterval(function () {
      fetchDocuments().then(function (data) {
        if (!data || !data.temp_table) {
          stopTempTablePolling();
          return;
        }
        var status = String(data.temp_table.status || '').toLowerCase();
        if (status !== 'processing') {
          stopTempTablePolling();
        }
      });
    }, TEMP_TABLE_POLL_MS);
  }

  function handleTempTableFromStatus(data) {
    if (!data) return;
    var tempTable = data.temp_table || null;
    if (tempTable && tempTable.temp_table_id) {
      currentTempTable = tempTable;
    } else {
      currentTempTable = null;
    }
    renderDocuments(data.documents || [], tempTable);
    announceTempTableStatusIfNeeded(tempTable);
    startTempTablePollingIfNeeded(tempTable);
  }

  function formatBytes(bytes) {
    var n = Number(bytes) || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / (1024 * 1024)).toFixed(1) + ' MB';
  }

  function formatExpiry(iso) {
    if (!iso) return 'expira em breve';
    var expires = new Date(iso);
    if (isNaN(expires.getTime())) return 'expira em breve';
    var diffMs = expires.getTime() - Date.now();
    if (diffMs <= 0) return 'expirado';
    var mins = Math.round(diffMs / 60000);
    if (mins < 60) return 'expira em ~' + mins + ' min';
    var hours = Math.round(mins / 60);
    if (hours < 48) return 'expira em ~' + hours + ' h';
    var days = Math.round(hours / 24);
    return 'expira em ~' + days + ' dia(s)';
  }

  function friendlyError(data) {
    if (!data) return 'Não foi possível concluir a operação documental.';
    if (data.message && typeof data.message === 'string') return data.message;
    if (data.error_code && ERROR_MESSAGES[data.error_code]) {
      return ERROR_MESSAGES[data.error_code];
    }
    return 'Não foi possível concluir a operação documental.';
  }

  function setError(message) {
    var el = byId('cleideAuditDocumentsError');
    if (!el) return;
    if (!message) {
      el.style.display = 'none';
      el.textContent = '';
      return;
    }
    el.style.display = 'block';
    el.textContent = message;
  }

  function setStatus(message) {
    var el = byId('cleideAuditUploadStatus');
    if (!el) return;
    el.textContent = message || '';
  }

  function setUploadLoading(on) {
    uploadInFlight = !!on;
    var attachBtn = byId('cleideAuditoriaAttachBtn');
    var fileInput = byId('cleideAuditFileInput');
    if (attachBtn) {
      attachBtn.setAttribute('aria-busy', on ? 'true' : 'false');
    }
    if (fileInput && on) fileInput.value = '';
    setStatus(on ? 'Enviando documento...' : '');
  }

  function docTypeLabel(doc) {
    var ext = (doc.extension || '').replace(/^\./, '').toUpperCase();
    if (ext) return ext;
    var t = (doc.doc_type || '').toUpperCase();
    return t || 'DOC';
  }

  function statusLabel(doc) {
    var status = (doc.status || 'active').toLowerCase();
    if (status === 'active') return 'ativo';
    if (status === 'pending') return 'pendente';
    if (status === 'error') return 'erro';
    return status;
  }

  function documentContextNote(doc) {
    var label = docTypeLabel(doc);
    var status = (doc.status || 'active').toLowerCase();
    if (status === 'error') {
      return 'Não foi possível preparar este ' + label + ' para leitura. Tente novamente ou use outro arquivo.';
    }
    if (status === 'pending') {
      return label + ' anexado. Preparando leitura pela IA...';
    }
    return label + ' disponível como contexto da conversa.';
  }

  function documentBadgeClass(doc) {
    var status = (doc.status || '').toLowerCase();
    if (status === 'error') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-error';
    }
    if (status === 'pending') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-preparing';
    }
    return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-ready';
  }

  function tempTableStatusLabel(status) {
    var normalized = (status || '').toLowerCase();
    if (normalized === 'processing') return 'processando';
    if (normalized === 'awaiting_validation') return 'aguardando validação';
    if (normalized === 'needs_review') return 'revisão necessária';
    if (normalized === 'failed') return 'falha';
    if (normalized === 'expired') return 'expirada';
    if (normalized === 'discarded') return 'inválida';
    if (normalized === 'validated') return 'validada';
    return normalized || 'indisponível';
  }

  function tempTableBadgeClass(status) {
    var normalized = (status || '').toLowerCase();
    if (normalized === 'processing') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-preparing';
    }
    if (normalized === 'awaiting_validation' || normalized === 'validated') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-ready';
    }
    if (normalized === 'needs_review') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-preparing';
    }
    if (normalized === 'failed' || normalized === 'expired' || normalized === 'discarded') {
      return 'cleide-audit-doc-item-badge cleide-audit-doc-item-badge-error';
    }
    return 'cleide-audit-doc-item-badge';
  }

  function tempTableContextNote(tempTable) {
    var status = (tempTable.status || '').toLowerCase();
    if (status === 'processing') return 'Estruturando tabela temporária a partir dos anexos...';
    if (status === 'awaiting_validation') {
      return 'Tabela temporária pronta e aguardando validação (somente leitura).';
    }
    if (status === 'needs_review') {
      return 'Tabela temporária disponível para revisão (somente leitura).';
    }
    if (status === 'failed') return 'Não foi possível estruturar a tabela temporária.';
    if (status === 'expired') return 'Tabela temporária expirada.';
    if (status === 'discarded') return 'Tabela temporária invalidada.';
    return 'Tabela temporária extraída (somente leitura).';
  }

  function renderTempTableItem(tempTable) {
    var li = document.createElement('li');
    li.className = 'cleide-audit-doc-item cleide-audit-temp-table-item';
    li.setAttribute('data-temp-table-id', tempTable.temp_table_id || '');

    var openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'cleide-audit-temp-table-open-btn';
    openBtn.setAttribute('aria-label', 'Abrir dados da tabela temporária');
    openBtn.addEventListener('click', function () {
      lastTempTableCardButton = openBtn;
      openTempTableModal();
    });

    var ui = tempTable.ui_visibility || {};
    var name = document.createElement('div');
    name.className = 'cleide-audit-doc-item-name';
    name.textContent = ui.display_name || 'Tabela temporária extraída';

    var meta = document.createElement('div');
    meta.className = 'cleide-audit-doc-item-meta';
    var sourceCount = Array.isArray(tempTable.source_documents) ? tempTable.source_documents.length : 0;
    var parts = [
      'ARTEFATO',
      tempTableStatusLabel(tempTable.status),
      formatExpiry(tempTable.expires_at)
    ];
    if (sourceCount > 0) parts.push(sourceCount + ' doc(s) origem');
    meta.textContent = parts.join(' · ');

    var badge = document.createElement('span');
    badge.className = tempTableBadgeClass(tempTable.status);
    badge.textContent = tempTableContextNote(tempTable);

    openBtn.appendChild(name);
    openBtn.appendChild(meta);
    openBtn.appendChild(badge);
    li.appendChild(openBtn);
    return li;
  }

function renderDocumentItem(doc) {
    var li = document.createElement('li');
    li.className = 'cleide-audit-doc-item';
    li.setAttribute('data-doc-id', doc.doc_id || '');

    var main = document.createElement('div');
    main.className = 'cleide-audit-doc-item-main';

    var name = document.createElement('div');
    name.className = 'cleide-audit-doc-item-name';
    name.textContent = doc.display_name || doc.safe_name || 'Documento';

    var meta = document.createElement('div');
    meta.className = 'cleide-audit-doc-item-meta';
    var parts = [
      docTypeLabel(doc),
      formatBytes(doc.size_bytes),
      statusLabel(doc),
      formatExpiry(doc.expires_at)
    ];
    if (doc.truncated) parts.push('conteúdo truncado');
    meta.textContent = parts.join(' · ');

    var badge = document.createElement('span');
    badge.className = documentBadgeClass(doc);
    badge.textContent = documentContextNote(doc);

    main.appendChild(name);
    main.appendChild(meta);
    main.appendChild(badge);

    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'cleide-audit-doc-item-remove';
    removeBtn.setAttribute('aria-label', 'Remover documento ' + (doc.display_name || 'anexado'));
    removeBtn.innerHTML = '<i class="bi bi-x-lg" aria-hidden="true"></i>';
    removeBtn.addEventListener('click', function () {
      removeDocument(doc.doc_id);
    });

    li.appendChild(main);
    li.appendChild(removeBtn);
    return li;
  }

  function updateClearButton(count) {
    var btn = byId('cleideAuditClearDocuments');
    var toolbar = byId('cleideAuditDocumentsToolbar');
    var panel = byId('cleideAuditDocumentsPanel');
    if (btn) {
      btn.style.display = count > 0 ? 'inline-flex' : 'none';
    }
    if (panel) {
      if (count > 0) {
        panel.classList.remove('cleide-audit-documents-area-empty');
      } else {
        panel.classList.add('cleide-audit-documents-area-empty');
      }
    }
  }

  function renderDocuments(documents, tempTable) {
    var list = byId('cleideAuditDocumentsList');
    if (!list) return;
    list.innerHTML = '';
    var items = Array.isArray(documents) ? documents : [];
    items.forEach(function (doc) {
      if (!doc || !doc.doc_id) return;
      list.appendChild(renderDocumentItem(doc));
    });
    if (tempTable && tempTable.temp_table_id) {
      list.appendChild(renderTempTableItem(tempTable));
    }
    updateClearButton(items.length + (tempTable && tempTable.temp_table_id ? 1 : 0));
  }

  function fetchDocuments() {
    return fetch(API_STATUS, { method: 'GET', credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 401 || res.status === 403) {
          var errData = res.data || {};
          if (errData.error_code === 'franquia_blocked') {
            setError(errData.message || friendlyError(errData));
          } else {
            setError(friendlyError(errData));
          }
          currentTempTable = null;
          renderDocuments([], null);
          return null;
        }
        if (!res.data || res.data.ok !== true) {
          setError(friendlyError(res.data));
          return null;
        }
        setError('');
        handleTempTableFromStatus(res.data);
        return res.data;
      })
      .catch(function () {
        setError('Não foi possível carregar os documentos da sessão.');
        return null;
      });
  }

  function refreshAttachmentsAfterChat() {
    fetchDocuments();
  }

  function uploadDocument(file) {
    if (!file || uploadInFlight) return Promise.resolve();
    setError('');
    setUploadLoading(true);

    var formData = new FormData();
    formData.append('file', file, file.name);

    return fetch(API_UPLOAD, {
      method: 'POST',
      body: formData,
      credentials: 'same-origin'
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setError(friendlyError(res.data));
          return null;
        }
        setError('');
        return fetchDocuments().then(function (statusData) {
          if (statusData) return statusData;
          if (res.data.temp_table) {
            handleTempTableFromStatus({
              documents: [],
              temp_table: res.data.temp_table
            });
          }
          return res.data;
        });
      })
      .catch(function () {
        setError('Não foi possível enviar o documento. Tente novamente.');
        return null;
      })
      .finally(function () {
        setUploadLoading(false);
      });
  }

  function removeDocument(docId) {
    if (!docId) return;
    setError('');
    fetch('/api/cleide-auditoria/documents/' + encodeURIComponent(docId), {
      method: 'DELETE',
      credentials: 'same-origin'
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setError(friendlyError(res.data));
          return;
        }
        return fetchDocuments();
      })
      .catch(function () {
        setError('Não foi possível remover o documento.');
      });
  }

  function clearAllDocuments() {
    setError('');
    fetch(API_CLEAR, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin'
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setError(friendlyError(res.data));
          return;
        }
        renderDocuments([], null);
        currentTempTable = null;
        lastAnnouncedTempTableStatus = null;
        stopTempTablePolling();
        setStatus('');
      })
      .catch(function () {
        setError('Não foi possível limpar os documentos.');
      });
  }

  function positionActionsMenu() {
    var attachBtn = byId('cleideAuditoriaAttachBtn');
    var menu = byId('cleideAuditoriaActionsMenu');
    if (!attachBtn || !menu || menu.hidden) return;

    var rect = attachBtn.getBoundingClientRect();
    var gap = 8;
    var menuWidth = menu.offsetWidth || 260;
    var menuHeight = menu.offsetHeight || 220;
    var left = Math.min(Math.max(8, rect.left), window.innerWidth - menuWidth - 8);
    var top = rect.top - menuHeight - gap;

    if (top < 8) {
      top = rect.bottom + gap;
    }

    menu.style.left = left + 'px';
    menu.style.top = top + 'px';
  }

  var actionsMenuHomeParent = null;

  function setActionsMenuOpen(open) {
    var attachBtn = byId('cleideAuditoriaAttachBtn');
    var menu = byId('cleideAuditoriaActionsMenu');
    var composerWrap = byId('cleideAuditoriaComposerWrap');
    if (!attachBtn || !menu) return;
    var isOpen = !!open;

    if (isOpen) {
      menu.hidden = false;
      if (composerWrap && menu.parentNode !== document.body) {
        actionsMenuHomeParent = composerWrap;
        document.body.appendChild(menu);
      }
      positionActionsMenu();
    } else {
      menu.hidden = true;
      if (actionsMenuHomeParent && menu.parentNode === document.body) {
        actionsMenuHomeParent.appendChild(menu);
        actionsMenuHomeParent = null;
      }
      menu.style.left = '';
      menu.style.top = '';
    }

    attachBtn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
    attachBtn.setAttribute('aria-label', isOpen ? 'Fechar menu de ações' : 'Abrir menu de ações');
    attachBtn.title = isOpen ? 'Fechar menu de ações' : 'Abrir menu de ações';
    attachBtn.classList.toggle('is-open', isOpen);
  }

  function closeActionsMenu() {
    setActionsMenuOpen(false);
  }

  function toggleActionsMenu() {
    var menu = byId('cleideAuditoriaActionsMenu');
    if (!menu) return;
    setActionsMenuOpen(menu.hidden);
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function runWelcomeTypewriter() {
    var welcome = byId('cleideAuditoriaWelcome');
    if (!welcome) return;

    var text = welcome.getAttribute('data-typewriter-text') || '';
    if (!text) return;

    if (prefersReducedMotion()) {
      welcome.textContent = text;
      return;
    }

    welcome.textContent = '';
    var index = 0;
    var delayMs = 36;

    function typeNextChar() {
      if (index >= text.length) {
        welcome.textContent = text;
        return;
      }
      welcome.textContent += text.charAt(index);
      index += 1;
      window.setTimeout(typeNextChar, delayMs);
    }

    typeNextChar();
  }

  function generateRequestId() {
    if (window.crypto && typeof window.crypto.randomUUID === 'function') {
      return window.crypto.randomUUID().split('-').join('');
    }
    var out = '';
    var i;
    for (i = 0; i < 32; i += 1) {
      out += Math.floor(Math.random() * 16).toString(16);
    }
    return out;
  }

  function trimChatHistory(history) {
    if (!Array.isArray(history) || history.length <= MAX_CHAT_HISTORY) {
      return history || [];
    }
    return history.slice(history.length - MAX_CHAT_HISTORY);
  }

  function setChatInputEnabled(enabled) {
    var input = byId('cleideAuditoriaInput');
    var sendBtn = byId('cleideAuditoriaSend');
    if (input) input.disabled = !enabled;
    if (sendBtn) sendBtn.disabled = !enabled;
  }

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function renderCleideMarkdown(text) {
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

  function appendChatBubble(role, text) {
    var container = byId('cleideAuditoriaMessages');
    if (!container || !text) return;

    var isUser = role === 'user';
    var msg = document.createElement('div');
    msg.className = 'cleide-auditoria-chat-msg cleide-auditoria-chat-msg-' + (isUser ? 'user' : 'bot');
    msg.setAttribute('data-chat-role', isUser ? 'user' : 'assistant');

    var inner = document.createElement('div');
    inner.className = 'cleide-auditoria-chat-msg-inner';
    if (isUser) {
      inner.textContent = text;
    } else {
      inner.innerHTML = renderCleideMarkdown(text);
    }
    msg.appendChild(inner);

    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function setChatLoading(on) {
    var container = byId('cleideAuditoriaMessages');
    if (!container) return;

    if (on) {
      var el = document.createElement('div');
      el.id = CHAT_LOADING_ID;
      el.className = 'cleide-auditoria-chat-msg cleide-auditoria-chat-msg-bot';
      el.innerHTML = '<div class="cleide-auditoria-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> Cleide está analisando...</div>';
      container.appendChild(el);
      container.scrollTop = container.scrollHeight;
      return;
    }

    var loading = byId(CHAT_LOADING_ID);
    if (loading) loading.remove();
  }

  function chatErrorMessage(data, status) {
    if (status === 401) {
      return (data && data.message) || CHAT_FIXED_ERRORS.login;
    }
    if (status === 403) {
      return (data && data.message) || friendlyError(data);
    }
    if (data && data.message) return data.message;
    return CHAT_FIXED_ERRORS.service;
  }

  function sendChatMessage() {
    var input = byId('cleideAuditoriaInput');
    if (!input || chatInFlight) return;

    var text = (input.value || '').trim();
    if (!text) return;

    input.value = '';
    appendChatBubble('user', text);

    var historyForApi = trimChatHistory(chatHistory.slice());
    var requestId = generateRequestId();

    chatInFlight = true;
    setChatInputEnabled(false);
    setChatLoading(true);

    fetch(API_CHAT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        message: text,
        history: historyForApi,
        request_id: requestId
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        setChatLoading(false);
        if (res.status === 401 || res.status === 403) {
          appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          return;
        }
        if (!res.data || res.data.ok !== true) {
          appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          return;
        }

        var answer = typeof res.data.answer === 'string' ? res.data.answer : '';
        appendChatBubble('assistant', answer || CHAT_FIXED_ERRORS.service);
        chatHistory.push({ role: 'user', content: text });
        chatHistory.push({ role: 'assistant', content: answer });
        chatHistory = trimChatHistory(chatHistory);
        refreshAttachmentsAfterChat();
      })
      .catch(function () {
        setChatLoading(false);
        appendChatBubble('assistant', CHAT_FIXED_ERRORS.network);
      })
      .finally(function () {
        chatInFlight = false;
        setChatInputEnabled(true);
        if (input) input.focus();
      });
  }

  function initChat() {
    var form = byId('cleideAuditoriaForm');
    var input = byId('cleideAuditoriaInput');
    var sendBtn = byId('cleideAuditoriaSend');
    if (!form || !input || !sendBtn) return;

    function handleSend(e) {
      if (e) e.preventDefault();
      sendChatMessage();
    }

    sendBtn.addEventListener('click', handleSend);
    form.addEventListener('submit', handleSend);
    input.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendChatMessage();
      }
    });
  }

  function displayFieldValue(value) {
    if (value === null || value === undefined || value === '') return 'não informado';
    return String(value);
  }

  function hasFieldValue(value) {
    return value !== null && value !== undefined && value !== '';
  }

  function formatDateTime(iso) {
    if (!iso) return 'não informado';
    var d = new Date(iso);
    if (isNaN(d.getTime())) return 'não informado';
    try {
      return d.toLocaleString('pt-BR');
    } catch (e) {
      return String(iso);
    }
  }

  function appendMetaRow(container, label, value) {
    var row = document.createElement('div');
    row.className = 'cleide-audit-temp-table-modal-meta-row';
    var labelEl = document.createElement('span');
    labelEl.className = 'cleide-audit-temp-table-modal-meta-label';
    labelEl.textContent = label + ':';
    var valueEl = document.createElement('span');
    valueEl.className = 'cleide-audit-temp-table-modal-meta-value';
    valueEl.textContent = displayFieldValue(value);
    row.appendChild(labelEl);
    row.appendChild(valueEl);
    container.appendChild(row);
  }

  function appendDetailRow(container, label, value) {
    if (!hasFieldValue(value)) return;
    var row = document.createElement('div');
    row.className = 'cleide-audit-temp-table-modal-detail-row';
    var labelEl = document.createElement('span');
    labelEl.className = 'cleide-audit-temp-table-modal-detail-label';
    labelEl.textContent = label + ':';
    var valueEl = document.createElement('span');
    valueEl.className = 'cleide-audit-temp-table-modal-detail-value';
    valueEl.textContent = String(value);
    row.appendChild(labelEl);
    row.appendChild(valueEl);
    container.appendChild(row);
  }

  function appendSectionTitle(container, title) {
    var heading = document.createElement('h3');
    heading.className = 'cleide-audit-temp-table-modal-section-title';
    heading.textContent = title;
    container.appendChild(heading);
  }

  function appendEmptySectionMessage(container) {
    var empty = document.createElement('p');
    empty.className = 'cleide-audit-temp-table-modal-empty';
    empty.textContent = 'Nenhum item identificado nesta seção.';
    container.appendChild(empty);
  }

  function appendSimpleListSection(container, title, items) {
    appendSectionTitle(container, title);
    var list = Array.isArray(items) ? items : [];
    if (!list.length) {
      appendEmptySectionMessage(container);
      return;
    }
    var ul = document.createElement('ul');
    ul.className = 'cleide-audit-temp-table-modal-list';
    list.forEach(function (item) {
      if (!hasFieldValue(item)) return;
      var li = document.createElement('li');
      li.textContent = String(item);
      ul.appendChild(li);
    });
    if (!ul.childNodes.length) {
      appendEmptySectionMessage(container);
      return;
    }
    container.appendChild(ul);
  }

  function appendItemCard(container, rows) {
    var card = document.createElement('div');
    card.className = 'cleide-audit-temp-table-modal-item';
    rows.forEach(function (row) {
      appendDetailRow(card, row.label, row.value);
    });
    container.appendChild(card);
  }

  function renderFreightValuesSection(container, items) {
    appendSectionTitle(container, 'Valores de frete');
    var list = Array.isArray(items) ? items : [];
    if (!list.length) {
      appendEmptySectionMessage(container);
      return;
    }
    list.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      appendItemCard(container, [
        { label: 'Rótulo', value: displayFieldValue(item.label) },
        { label: 'Valor', value: displayFieldValue(item.value) },
        { label: 'Unidade', value: displayFieldValue(item.unit) },
        { label: 'Observações', value: item.notes }
      ]);
    });
  }

  function renderAccessorialFeesSection(container, items) {
    appendSectionTitle(container, 'Taxas e serviços adicionais');
    var list = Array.isArray(items) ? items : [];
    if (!list.length) {
      appendEmptySectionMessage(container);
      return;
    }
    list.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      appendItemCard(container, [
        { label: 'Nome', value: displayFieldValue(item.name) },
        { label: 'Valor', value: displayFieldValue(item.value) },
        { label: 'Unidade', value: displayFieldValue(item.unit) },
        { label: 'Base de cálculo', value: displayFieldValue(item.calculation_basis) },
        { label: 'Observações', value: item.notes }
      ]);
    });
  }

  function renderWeightRangesSection(container, items) {
    appendSectionTitle(container, 'Faixas de peso');
    var list = Array.isArray(items) ? items : [];
    if (!list.length) {
      appendEmptySectionMessage(container);
      return;
    }
    list.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      appendItemCard(container, [
        { label: 'Rótulo', value: displayFieldValue(item.label) },
        { label: 'Peso mínimo', value: displayFieldValue(item.min_weight) },
        { label: 'Peso máximo', value: displayFieldValue(item.max_weight) },
        { label: 'Unidade', value: displayFieldValue(item.unit) },
        { label: 'Observações', value: item.notes }
      ]);
    });
  }

  function renderTempTableModalContent(tempTable) {
    var body = byId('cleideAuditTempTableModalBody');
    if (!body) return;
    body.innerHTML = '';

    if (!tempTable) {
      var noData = document.createElement('p');
      noData.className = 'cleide-audit-temp-table-modal-empty';
      noData.textContent = 'Nenhuma tabela temporária disponível.';
      body.appendChild(noData);
      return;
    }

    var status = String(tempTable.status || '').toLowerCase();
    if (status === 'processing') {
      var processingMsg = document.createElement('p');
      processingMsg.className = 'cleide-audit-temp-table-modal-processing';
      processingMsg.textContent = 'Processamento em andamento. Os dados aparecerão aqui quando a extração terminar.';
      body.appendChild(processingMsg);
      return;
    }

    var meta = document.createElement('div');
    meta.className = 'cleide-audit-temp-table-modal-meta';
    appendMetaRow(meta, 'Status', tempTableStatusLabel(tempTable.status));
    var sourceDocs = Array.isArray(tempTable.source_documents) ? tempTable.source_documents : [];
    appendMetaRow(meta, 'Documento(s) de origem', sourceDocs.length ? sourceDocs.join(', ') : null);
    appendMetaRow(meta, 'Criado em', formatDateTime(tempTable.created_at));
    appendMetaRow(meta, 'Atualizado em', formatDateTime(tempTable.updated_at));
    appendMetaRow(meta, 'Expira em', formatDateTime(tempTable.expires_at));
    body.appendChild(meta);

    renderFreightValuesSection(body, tempTable.freight_values);
    renderAccessorialFeesSection(body, tempTable.accessorial_fees);
    renderWeightRangesSection(body, tempTable.weight_ranges);
    appendSimpleListSection(body, 'Alertas de leitura', tempTable.reading_alerts);
    appendSimpleListSection(body, 'Evidências/referências', tempTable.evidence_refs);
  }

  function isTempTableModalOpen() {
    var modal = byId('cleideAuditTempTableModal');
    return !!(modal && !modal.hidden);
  }

  function openTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    if (!modal) return;
    renderTempTableModalContent(currentTempTable);
    modal.hidden = false;
    document.body.classList.add('cleide-audit-temp-table-modal-open');
    var closeBtn = byId('cleideAuditTempTableModalClose');
    if (closeBtn) closeBtn.focus();
  }

  function closeTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove('cleide-audit-temp-table-modal-open');
    if (lastTempTableCardButton && typeof lastTempTableCardButton.focus === 'function') {
      lastTempTableCardButton.focus();
    }
  }

  function initTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    var closeBtn = byId('cleideAuditTempTableModalClose');
    var backdrop = byId('cleideAuditTempTableModalBackdrop');
    if (!modal) return;

    if (closeBtn) {
      closeBtn.addEventListener('click', function () {
        closeTempTableModal();
      });
    }
    if (backdrop) {
      backdrop.addEventListener('click', function () {
        closeTempTableModal();
      });
    }
    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && isTempTableModalOpen()) {
        e.stopPropagation();
        closeTempTableModal();
      }
    });
  }

  function initDocuments() {
    var fileInput = byId('cleideAuditFileInput');
    var uploadItem = byId('cleideAuditoriaUploadItem');
    var clearBtn = byId('cleideAuditClearDocuments');
    if (!fileInput) return;

    if (uploadItem) {
      uploadItem.addEventListener('click', function (e) {
        e.preventDefault();
        if (uploadInFlight) return;
        closeActionsMenu();
        fileInput.click();
      });
    }

    fileInput.addEventListener('change', function () {
      var file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      uploadDocument(file);
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        clearAllDocuments();
      });
    }

    fetchDocuments();
  }

  function init() {
    runWelcomeTypewriter();

    var attachBtn = byId('cleideAuditoriaAttachBtn');
    if (!attachBtn) return;

    attachBtn.addEventListener('click', function (e) {
      e.preventDefault();
      toggleActionsMenu();
    });

    document.addEventListener('click', function (e) {
      var menu = byId('cleideAuditoriaActionsMenu');
      var btn = byId('cleideAuditoriaAttachBtn');
      if (!menu || menu.hidden) return;
      if (menu.contains(e.target) || (btn && btn.contains(e.target))) return;
      closeActionsMenu();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape') closeActionsMenu();
    });

    window.addEventListener('resize', function () {
      positionActionsMenu();
    });

    initDocuments();
    initTempTableModal();
    initChat();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
