/**
 * Anexos documentais do chat operacional da Júlia (Fase 5).
 * Backend é autoridade; frontend não processa conteúdo dos arquivos.
 */
(function () {
  'use strict';

  if (typeof window.JULIA_DOCUMENTS_UI === 'undefined' || window.JULIA_DOCUMENTS_UI !== true) {
    return;
  }

  var API_LIST = '/api/julia/documents';
  var API_UPLOAD = '/api/julia/documents/upload';
  var API_CLEAR = '/api/julia/documents/clear';
  var CONTEXT_KIND_GEMINI_FILE = 'gemini_file';
  var uploadInFlight = false;

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
    cleiton_doc_gemini_file_upload_failed: 'Não foi possível preparar este PDF para leitura. Tente novamente ou use outro arquivo.',
    auth_required: 'É necessário estar logado para anexar documentos.',
    franquia_blocked: 'Operação indisponível para este usuário no momento.'
  };

  function byId(id) { return document.getElementById(id); }

  function escapeHtml(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
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
    var el = byId('juliaDocumentsError');
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
    var el = byId('juliaDocumentsStatus');
    if (!el) return;
    el.textContent = message || '';
  }

  function setUploadLoading(on) {
    uploadInFlight = !!on;
    var attachBtn = byId('juliaChatAttachBtn');
    var fileInput = byId('juliaChatFileInput');
    if (attachBtn) {
      attachBtn.disabled = !!on;
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

  function pdfPlaceholderNote(doc) {
    var isPdf = (doc.doc_type || '').toLowerCase() === 'pdf' || doc.context_kind === CONTEXT_KIND_GEMINI_FILE;
    if (!isPdf) return '';
    if ((doc.status || '').toLowerCase() === 'error') {
      return 'Não foi possível preparar este PDF para leitura. Tente novamente ou use outro arquivo.';
    }
    if (doc.pdf_context_ready === true) {
      return 'PDF disponível como contexto da conversa.';
    }
    return 'PDF anexado. Preparando leitura pela IA...';
  }

  function pdfBadgeClass(doc) {
    var status = (doc.status || '').toLowerCase();
    if (status === 'error') {
      return 'julia-doc-item-badge julia-doc-item-badge-error';
    }
    if (doc.pdf_context_ready === true) {
      return 'julia-doc-item-badge julia-doc-item-badge-ready';
    }
    return 'julia-doc-item-badge julia-doc-item-badge-preparing';
  }

  function renderDocumentItem(doc) {
    var li = document.createElement('li');
    li.className = 'julia-doc-item';
    li.setAttribute('data-doc-id', doc.doc_id || '');

    var main = document.createElement('div');
    main.className = 'julia-doc-item-main';

    var name = document.createElement('div');
    name.className = 'julia-doc-item-name';
    name.textContent = doc.display_name || doc.safe_name || 'Documento';

    var meta = document.createElement('div');
    meta.className = 'julia-doc-item-meta';
    var parts = [
      docTypeLabel(doc),
      formatBytes(doc.size_bytes),
      statusLabel(doc),
      formatExpiry(doc.expires_at)
    ];
    if (doc.truncated) parts.push('conteúdo truncado');
    meta.textContent = parts.join(' · ');

    main.appendChild(name);
    main.appendChild(meta);

    var pdfNote = pdfPlaceholderNote(doc);
    if (pdfNote) {
      var badge = document.createElement('span');
      badge.className = pdfBadgeClass(doc);
      badge.textContent = pdfNote;
      main.appendChild(badge);
    }

    var removeBtn = document.createElement('button');
    removeBtn.type = 'button';
    removeBtn.className = 'julia-doc-item-remove';
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
    var btn = byId('juliaDocumentsClearBtn');
    var toolbar = byId('juliaDocumentsToolbar');
    var area = byId('juliaDocumentsArea');
    if (btn) {
      btn.style.display = count > 0 ? 'inline-flex' : 'none';
    }
    if (toolbar) {
      toolbar.style.display = count > 0 ? 'flex' : 'none';
    }
    if (area) {
      if (count > 0) {
        area.classList.remove('julia-documents-area-empty');
      } else {
        area.classList.add('julia-documents-area-empty');
      }
    }
  }

  function positionActionsMenu() {
    var attachBtn = byId('juliaChatAttachBtn');
    var menu = byId('juliaChatActionsMenu');
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
    var attachBtn = byId('juliaChatAttachBtn');
    var menu = byId('juliaChatActionsMenu');
    var composerWrap = byId('juliaChatComposerWrap');
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
    var menu = byId('juliaChatActionsMenu');
    if (!menu) return;
    setActionsMenuOpen(menu.hidden);
  }

  function renderDocuments(documents) {
    var list = byId('juliaDocumentsList');
    if (!list) return;
    list.innerHTML = '';
    var items = Array.isArray(documents) ? documents : [];
    items.forEach(function (doc) {
      if (!doc || !doc.doc_id) return;
      list.appendChild(renderDocumentItem(doc));
    });
    updateClearButton(items.length);
  }

  function fetchDocuments() {
    return fetch(API_LIST, { method: 'GET', credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (res.status === 401 || res.status === 403) {
          var errData = res.data || {};
          if (errData.error_code === 'franquia_blocked') {
            setError('');
          } else {
            setError(friendlyError(errData));
          }
          renderDocuments([]);
          return null;
        }
        if (!res.data || res.data.ok !== true) {
          setError(friendlyError(res.data));
          return null;
        }
        setError('');
        renderDocuments(res.data.documents || []);
        return res.data;
      })
      .catch(function () {
        setError('Não foi possível carregar os documentos da sessão.');
        return null;
      });
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
        return fetchDocuments();
      })
      .catch(function () {
        setError('Não foi possível enviar o documento. Tente novamente.');
        return null;
      })
      .finally(function () {
        setUploadLoading(false);
        var input = byId('juliaChatInput');
        if (input) input.focus();
      });
  }

  function removeDocument(docId) {
    if (!docId) return;
    setError('');
    fetch(API_LIST.replace(/\/$/, '') + '/' + encodeURIComponent(docId), {
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
        renderDocuments([]);
        setStatus('');
      })
      .catch(function () {
        setError('Não foi possível limpar os documentos.');
      });
  }

  function init() {
    var attachBtn = byId('juliaChatAttachBtn');
    var fileInput = byId('juliaChatFileInput');
    var uploadItem = byId('juliaChatUploadItem');
    var actionsMenu = byId('juliaChatActionsMenu');
    var clearBtn = byId('juliaDocumentsClearBtn');
    if (!attachBtn || !fileInput) return;

    attachBtn.addEventListener('click', function (e) {
      e.stopPropagation();
      if (uploadInFlight) return;
      toggleActionsMenu();
    });

    attachBtn.addEventListener('keydown', function (e) {
      if (e.key === 'Enter' || e.key === ' ') {
        e.preventDefault();
        attachBtn.click();
      }
    });

    if (uploadItem) {
      uploadItem.addEventListener('click', function () {
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

    document.addEventListener('click', function (e) {
      if (!actionsMenu || actionsMenu.hidden) return;
      var target = e.target;
      if (attachBtn && attachBtn.contains(target)) return;
      if (actionsMenu.contains(target)) return;
      closeActionsMenu();
    });

    document.addEventListener('keydown', function (e) {
      if (e.key === 'Escape' && actionsMenu && !actionsMenu.hidden) {
        closeActionsMenu();
        attachBtn.focus();
      }
    });

    window.addEventListener('resize', function () {
      if (actionsMenu && !actionsMenu.hidden) positionActionsMenu();
    });

    window.addEventListener('scroll', function () {
      if (actionsMenu && !actionsMenu.hidden) positionActionsMenu();
    }, true);

    fetchDocuments();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
