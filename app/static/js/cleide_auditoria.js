(function () {
  'use strict';

  var API_STATUS = '/api/cleide-auditoria/documents/status';
  var API_UPLOAD = '/api/cleide-auditoria/documents/upload';
  var API_CLEAR = '/api/cleide-auditoria/documents/clear';
  var API_CHAT = '/api/cleide-auditoria/chat';
  var API_TEMP_TABLE_SAVE = '/api/cleide-auditoria/temp-table/save';
  var API_COVERAGE_UPLOAD = '/api/cleide-auditoria/coverage/upload';
  var API_AUDIT_TEMPLATE = '/api/cleide-auditoria/audit-template';
  var API_AUDIT_UPLOAD = '/api/cleide-auditoria/audit/upload';
  var API_AUDIT_RUN = '/api/cleide-auditoria/audit/run';
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
  var currentCalculationBases = [];
  var lastTempTableCardButton = null;
  var tempTableEditMode = false;
  var tempTableEditSnapshot = null;
  var tempTableSaveInFlight = false;
  var tempTableValidationErrors = [];
  var openFreightTableKeys = new Set();
  var hasUserTouchedFreightTableOpenState = false;
  var tempTableModalActiveTab = 'freight';
  var coverageStepActive = false;
  var coveragePromptAnswered = false;
  var coveragePromptAccepted = false;
  var coverageUploadInFlight = false;
  var activeCoverageUploadPrefix = 'cleideAuditCoverage';
  var coverageSaveInFlight = false;
  var auditFileStepActive = false;
  var auditUploadInFlight = false;
  var auditRunInFlight = false;

  var COVERAGE_TABLE_HEADERS = [
    { key: 'destination_uf', label: 'UF destino' },
    { key: 'destination_city', label: 'Cidade destino' },
    { key: 'freight_region', label: 'Região de frete' },
    { key: 'notes', label: 'Observações' }
  ];

  function deepCloneValue(value) {
    if (value === null || typeof value !== 'object') return value;
    if (Array.isArray(value)) {
      return value.map(function (item) { return deepCloneValue(item); });
    }
    var out = {};
    Object.keys(value).forEach(function (key) {
      out[key] = deepCloneValue(value[key]);
    });
    return out;
  }

  function deepCloneTempTable(tempTable) {
    if (!tempTable) return null;
    return deepCloneValue(tempTable);
  }

  function normalizeCalculationUnit(value) {
    var text = normalizeTextKey(value).replace(/\s+/g, '');
    if (text === '%' || text === 'percent' || text === 'percentual' || text === 'porcentagem') return '%';
    if (text === 'r$' || text === 'rs' || text === 'brl' || text === 'real' || text === 'reais') return 'R$';
    if (text === 'kg' || text === 'quilo' || text === 'quilos') return 'kg';
    return text;
  }

  function getCalculationBaseById(baseId) {
    var wanted = String(baseId || '').trim();
    if (!wanted) return null;
    for (var i = 0; i < currentCalculationBases.length; i += 1) {
      var base = currentCalculationBases[i];
      if (base && String(base.id || '').trim() === wanted) return base;
    }
    return null;
  }

  function setTempTableModalError(message) {
    var el = byId('cleideAuditTempTableModalError');
    if (!el) return;
    if (!message) {
      el.hidden = true;
      el.textContent = '';
      return;
    }
    el.hidden = false;
    el.textContent = message;
  }

  function clearTempTableValidationErrors() {
    tempTableValidationErrors = [];
  }

  function hasCoverageRows(tempTable) {
    if (!tempTable || !tempTable.coverage_table) return false;
    var rows = tempTable.coverage_table.rows;
    return Array.isArray(rows) && rows.length > 0;
  }

  function hasLoadedCoverageTable(tempTable) {
    return !!(tempTable && tempTable.coverage_table && typeof tempTable.coverage_table === 'object');
  }

  function shouldShowCoverageTab(tempTable) {
    if (coverageStepActive) return true;
    if (hasCoverageRows(tempTable)) return true;
    if (coveragePromptAccepted) return true;
    if (hasLoadedCoverageTable(tempTable)) return true;
    return false;
  }

  function canEditCoverageTable(tempTable) {
    return hasCoverageRows(tempTable);
  }

  function resetCoveragePromptState() {
    coverageStepActive = false;
    coveragePromptAnswered = false;
    coveragePromptAccepted = false;
    tempTableModalActiveTab = 'freight';
  }

  function resetAuditFileStepState() {
    auditFileStepActive = false;
    auditUploadInFlight = false;
    auditRunInFlight = false;
    if (tempTableModalActiveTab === 'audit') {
      tempTableModalActiveTab = 'freight';
    }
  }

  function hasAuditBatch(tempTable) {
    if (!tempTable || !tempTable.audit_batch) return false;
    var status = String(tempTable.audit_batch.status || '').toLowerCase();
    return status === 'uploaded' || status === 'processed';
  }

  function shouldShowAuditTab(tempTable) {
    if (auditFileStepActive) return true;
    return hasAuditBatch(tempTable);
  }

  function appendCoveragePromptCTA() {
    var container = byId('cleideAuditoriaMessages');
    if (!container) return;

    var msg = document.createElement('div');
    msg.className = 'cleide-auditoria-chat-msg cleide-auditoria-chat-msg-bot cleide-audit-coverage-prompt-msg';
    msg.setAttribute('data-chat-role', 'assistant');

    var inner = document.createElement('div');
    inner.className = 'cleide-auditoria-chat-msg-inner cleide-audit-coverage-prompt-panel';

    var card = document.createElement('div');
    card.className = 'cleide-audit-coverage-prompt-card';

    var title = document.createElement('span');
    title.className = 'cleide-audit-coverage-prompt-title';
    title.textContent = 'Cidades atendidas';
    card.appendChild(title);

    var description = document.createElement('p');
    description.className = 'cleide-audit-coverage-prompt-description';
    description.textContent = 'Deseja informar a relação de cidades atendidas?';
    card.appendChild(description);

    var support = document.createElement('p');
    support.className = 'cleide-audit-coverage-prompt-support';
    support.textContent = 'Use essa etapa quando a tabela de frete trabalhar com regiões, praças, rotas ou itinerários.';
    card.appendChild(support);

    var actions = document.createElement('div');
    actions.className = 'cleide-audit-coverage-prompt-actions';

    var yesBtn = document.createElement('button');
    yesBtn.type = 'button';
    yesBtn.className = 'cleide-audit-coverage-prompt-yes cleide-audit-coverage-prompt-btn cleide-audit-coverage-prompt-btn-primary';
    yesBtn.textContent = 'Sim, enviar planilha';
    yesBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(true);
    });

    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'cleide-audit-coverage-prompt-no cleide-audit-coverage-prompt-btn cleide-audit-coverage-prompt-btn-secondary';
    noBtn.textContent = 'Agora não';
    noBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(false);
    });

    actions.appendChild(yesBtn);
    actions.appendChild(noBtn);
    card.appendChild(actions);
    inner.appendChild(card);
    msg.appendChild(inner);
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;

    chatHistory.push({ role: 'assistant', content: 'Deseja informar a relação de cidades atendidas?' });
    chatHistory = trimChatHistory(chatHistory);
  }

  function renderCoverageUploadCard(container, prefix) {
    if (!container) return;
    var idPrefix = prefix || 'cleideAuditCoverage';
    var fileInputId = idPrefix + 'FileInput';
    activeCoverageUploadPrefix = idPrefix;
    var card = document.createElement('div');
    card.className = 'cleide-audit-coverage-upload-card';

    var header = document.createElement('div');
    header.className = 'cleide-audit-coverage-upload-header';

    var title = document.createElement('span');
    title.className = 'cleide-audit-coverage-upload-title';
    title.textContent = 'Cidades atendidas';

    var badge = document.createElement('span');
    badge.className = 'cleide-audit-coverage-upload-badge';
    badge.textContent = 'CSV ou XLSX';

    header.appendChild(title);
    header.appendChild(badge);
    card.appendChild(header);

    var description = document.createElement('p');
    description.className = 'cleide-audit-coverage-upload-description';
    description.textContent = 'Envie uma planilha com UF, cidade e região de frete.';
    card.appendChild(description);

    var actions = document.createElement('div');
    actions.className = 'cleide-audit-coverage-upload-actions';

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv';
    fileInput.className = 'visually-hidden cleide-audit-coverage-upload-input';
    fileInput.id = fileInputId;

    var selectBtn = document.createElement('label');
    selectBtn.className = 'cleide-audit-coverage-upload-button';
    selectBtn.setAttribute('for', fileInputId);
    selectBtn.textContent = 'Selecionar arquivo';

    var fileName = document.createElement('span');
    fileName.className = 'cleide-audit-coverage-upload-file-name';
    fileName.id = idPrefix + 'UploadFileName';
    fileName.textContent = 'Nenhum arquivo selecionado';

    actions.appendChild(fileInput);
    actions.appendChild(selectBtn);
    actions.appendChild(fileName);
    card.appendChild(actions);

    var help = document.createElement('p');
    help.className = 'cleide-audit-coverage-upload-help';
    help.textContent = 'Formatos aceitos: CSV ou XLSX.';
    card.appendChild(help);

    var status = document.createElement('p');
    status.className = 'cleide-audit-coverage-upload-status';
    status.id = idPrefix + 'UploadStatus';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    card.appendChild(status);

    fileInput.addEventListener('change', function () {
      activeCoverageUploadPrefix = idPrefix;
      var file = fileInput.files && fileInput.files[0];
      if (file) {
        setCoverageUploadFileName(file.name);
      } else {
        setCoverageUploadFileName('');
      }
      fileInput.value = '';
      if (!file) return;
      uploadCoverageFile(file);
    });

    container.appendChild(card);
  }

  function showCoverageUploadArea() {
    var container = byId('cleideAuditoriaMessages');
    if (!container) return;
    if (byId('cleideAuditCoverageUploadPanel')) return;

    var msg = document.createElement('div');
    msg.className = 'cleide-auditoria-chat-msg cleide-auditoria-chat-msg-bot cleide-audit-coverage-upload-msg';
    msg.id = 'cleideAuditCoverageUploadPanel';

    var inner = document.createElement('div');
    inner.className = 'cleide-auditoria-chat-msg-inner cleide-audit-coverage-upload-panel';

    renderCoverageUploadCard(inner, 'cleideAuditCoverage');
    msg.appendChild(inner);
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function setCoverageUploadFileName(name) {
    var el = byId(activeCoverageUploadPrefix + 'UploadFileName') || byId('cleideAuditCoverageModalUploadFileName') || byId('cleideAuditCoverageUploadFileName');
    if (el) el.textContent = name || 'Nenhum arquivo selecionado';
  }

  function setCoverageUploadStatus(message, state) {
    var el = byId(activeCoverageUploadPrefix + 'UploadStatus') || byId('cleideAuditCoverageModalUploadStatus') || byId('cleideAuditCoverageUploadStatus');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'cleide-audit-coverage-upload-status';
    if (state === 'loading') {
      el.classList.add('is-loading');
    } else if (state === 'success') {
      el.classList.add('is-success');
    } else if (state === 'error') {
      el.classList.add('is-error');
    }
  }

  function handleCoveragePromptAnswer(accepted) {
    if (coveragePromptAnswered) return;
    coveragePromptAnswered = true;
    coveragePromptAccepted = !!accepted;
    if (coverageStepActive) {
      tempTableModalActiveTab = 'coverage';
      setTempTableModalError('');
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
      return;
    }
    if (accepted) {
      appendOperationalMessage('Certo. Você pode enviar o arquivo complementar com as cidades atendidas.');
      showCoverageUploadArea();
    } else {
      appendOperationalMessage('Sem problemas. Você pode continuar o fluxo normalmente.');
    }
  }

  function uploadCoverageFile(file) {
    if (!file || coverageUploadInFlight) return;
    coverageUploadInFlight = true;
    setCoverageUploadStatus('Enviando arquivo...', 'loading');

    var formData = new FormData();
    formData.append('file', file);

    fetch(API_COVERAGE_UPLOAD, {
      method: 'POST',
      credentials: 'same-origin',
      body: formData
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setCoverageUploadStatus(
            (res.data && res.data.message) || 'Não foi possível carregar o arquivo. Verifique o formato.',
            'error'
          );
          return;
        }
        if (res.data.temp_table) {
          currentTempTable = res.data.temp_table;
        }
        if (hasCoverageRows(currentTempTable)) {
          coverageStepActive = true;
          coveragePromptAnswered = true;
          coveragePromptAccepted = true;
          tempTableModalActiveTab = 'coverage';
          setCoverageUploadStatus('Arquivo carregado. Revise a aba Cidades atendidas.', 'success');
          if (!isTempTableModalOpen()) {
            appendOperationalMessage('Relação de cidades atendidas carregada. Revise na aba Cidades atendidas.');
          }
          fetchDocuments();
          if (isTempTableModalOpen()) {
            renderTempTableModalContent(currentTempTable);
            updateTempTableModalFooter();
          } else {
            openTempTableModal();
          }
        } else {
          setCoverageUploadStatus('Nenhuma cidade foi identificada no arquivo. Verifique o formato e tente novamente.', 'error');
          if (!isTempTableModalOpen()) {
            appendOperationalMessage('Nenhuma cidade foi identificada no arquivo complementar. Envie um CSV ou XLSX com UF, cidade e região de frete.');
          }
        }
      })
      .catch(function () {
        setCoverageUploadStatus('Não foi possível enviar o arquivo. Verifique sua conexão e tente novamente.', 'error');
      })
      .finally(function () {
        coverageUploadInFlight = false;
      });
  }

  function collectCoverageSavePayload() {
    if (!currentTempTable || !currentTempTable.temp_table_id) return null;
    var coverage = currentTempTable.coverage_table || { rows: [] };
    return {
      temp_table_id: currentTempTable.temp_table_id,
      edit_target: {
        coverage_table: {
          rows: deepCloneValue(coverage.rows || [])
        }
      },
      review_action: 'save_and_advance'
    };
  }

  function saveCoverageTableEdit() {
    if (!currentTempTable || coverageSaveInFlight) return;
    var payload = collectCoverageSavePayload();
    if (!payload) {
      setTempTableModalError('Nenhuma tabela de cobertura disponível para salvar.');
      return;
    }
    coverageSaveInFlight = true;
    tempTableSaveInFlight = true;
    updateTempTableModalFooter();
    setTempTableModalError('');

    fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          var errMsg = (res.data && res.data.message) || 'Não foi possível salvar a cobertura temporária.';
          setTempTableModalError(errMsg);
          return;
        }
        if (res.data.temp_table) {
          currentTempTable = res.data.temp_table;
        }
        tempTableEditMode = false;
        tempTableEditSnapshot = null;
        renderTempTableModalContent(currentTempTable);
        appendOperationalMessage('Relação de cidades atendidas salva temporariamente.');
        fetchDocuments();
      })
      .catch(function () {
        setTempTableModalError('Não foi possível salvar a cobertura. Verifique sua conexão e tente novamente.');
      })
      .finally(function () {
        coverageSaveInFlight = false;
        tempTableSaveInFlight = false;
        updateTempTableModalFooter();
      });
  }

  function updateTempTableModalFooter() {
    var editBtn = byId('cleideAuditTempTableModalEdit');
    var cancelBtn = byId('cleideAuditTempTableModalCancelEdit');
    var saveBtn = byId('cleideAuditTempTableModalSave');
    var startAuditBtn = byId('cleideAuditTempTableModalStartAudit');
    var banner = byId('cleideAuditTempTableModalEditBanner');
    var onCoverageTab = tempTableModalActiveTab === 'coverage' && shouldShowCoverageTab(currentTempTable);
    var onAuditTab = tempTableModalActiveTab === 'audit' && shouldShowAuditTab(currentTempTable);
    var coverageHasRows = hasCoverageRows(currentTempTable);
    var canStartAudit = coverageHasRows || (coveragePromptAnswered && !coveragePromptAccepted);
    var hideEditOnEmptyCoverage = onCoverageTab && !coverageHasRows;
    var hideEditOnAuditTab = onAuditTab;
    if (editBtn) {
      editBtn.hidden = !!tempTableEditMode || hideEditOnEmptyCoverage || hideEditOnAuditTab;
      editBtn.disabled = hideEditOnEmptyCoverage || hideEditOnAuditTab;
    }
    if (startAuditBtn) {
      startAuditBtn.hidden = !!tempTableEditMode || !onCoverageTab || !canStartAudit || onAuditTab || hasAuditBatch(currentTempTable);
    }
    if (cancelBtn) cancelBtn.hidden = !tempTableEditMode;
    if (banner) banner.hidden = !tempTableEditMode;
    document.body.classList.toggle('cleide-audit-temp-table-modal-editing', !!tempTableEditMode);
    if (saveBtn) {
      saveBtn.disabled = !!(tempTableSaveInFlight || coverageSaveInFlight);
      saveBtn.setAttribute('aria-busy', (tempTableSaveInFlight || coverageSaveInFlight) ? 'true' : 'false');
      if (onCoverageTab) {
        saveBtn.textContent = 'Salvar';
        saveBtn.hidden = !tempTableEditMode;
      } else if (onAuditTab) {
        saveBtn.hidden = true;
      } else {
        saveBtn.textContent = 'Salvar e Avançar';
        saveBtn.hidden = false;
      }
    }
  }

  function canEditFreightTables(tempTable) {
    return hasUsefulFreightTables(tempTable);
  }

  function canEditFreightRoutes(tempTable) {
    if (!tempTable || canEditFreightTables(tempTable)) return false;
    var routes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    return routes.length > 0;
  }

  function canEditAccessorialFees(tempTable) {
    if (!tempTable) return false;
    return getGeneralAccessorialFees(tempTable.accessorial_fees).length > 0;
  }

  function enterTempTableEditMode() {
    if (!currentTempTable || tempTableEditMode) return;
    tempTableEditSnapshot = deepCloneTempTable(currentTempTable);
    tempTableEditMode = true;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    var cancelBtn = byId('cleideAuditTempTableModalCancelEdit');
    if (cancelBtn) cancelBtn.focus();
  }

  function cancelTempTableEdit() {
    if (!tempTableEditMode) return;
    if (tempTableEditSnapshot) {
      currentTempTable = deepCloneTempTable(tempTableEditSnapshot);
    }
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    var editBtn = byId('cleideAuditTempTableModalEdit');
    if (editBtn) editBtn.focus();
  }

  function collectTempTableSavePayload() {
    if (!currentTempTable || !currentTempTable.temp_table_id) return null;
    var payload = {
      temp_table_id: currentTempTable.temp_table_id,
      edit_target: {
        freight_tables: [],
        freight_routes: [],
        accessorial_fees: []
      },
      review_action: 'save_and_advance'
    };
    if (tempTableEditMode) {
      if (canEditFreightTables(currentTempTable)) {
        payload.edit_target.freight_tables = deepCloneTempTable(currentTempTable.freight_tables) || [];
      } else if (canEditFreightRoutes(currentTempTable)) {
        payload.edit_target.freight_routes = deepCloneTempTable(currentTempTable.freight_routes) || [];
      }
      payload.edit_target.accessorial_fees = deepCloneTempTable(currentTempTable.accessorial_fees) || [];
    }
    return payload;
  }

  function accessorialFeeHasRequiredValue(fee) {
    var operation = String((fee && fee.operation) || '');
    var value = String((fee && fee.value) || '').trim();
    if (!operation) return false;
    if (operation === 'percentage_of_variable') return /%|[0-9]/.test(value) && /\d/.test(value);
    if (operation === 'fixed_amount' || operation === 'ceil_fraction' || operation === 'multiply_by_variable') {
      return /[0-9]/.test(value);
    }
    return false;
  }

  function accessorialFeeOperationIsComplete(fee) {
    var operation = String((fee && fee.operation) || '');
    if (
      operation !== 'fixed_amount'
      && operation !== 'percentage_of_variable'
      && operation !== 'multiply_by_variable'
      && operation !== 'ceil_fraction'
    ) {
      return false;
    }
    if (
      operation === 'percentage_of_variable'
      || operation === 'multiply_by_variable'
      || operation === 'ceil_fraction'
    ) {
      if (!String((fee && fee.audit_variable) || '').trim()) return false;
    }
    if (operation !== 'ceil_fraction') return true;
    var params = fee && fee.operation_parameters;
    var fractionSize = params && params.fraction_size;
    if (fractionSize === null || fractionSize === undefined || String(fractionSize).trim() === '') return false;
    var parsed = parseFloat(String(fractionSize).replace(',', '.'));
    return isFinite(parsed) && parsed > 0;
  }

  function accessorialFeeUnitMatchesBase(fee, base) {
    if (!base || !base.unit) return true;
    return normalizeCalculationUnit(fee && fee.unit) === normalizeCalculationUnit(base.unit);
  }

  function accessorialFeeMissingCalculationBase(fee) {
    var baseId = String((fee && fee.calculation_base_id) || '').trim();
    var base = getCalculationBaseById(baseId);
    var basis = normalizeTextKey(fee && fee.calculation_basis);
    return !baseId || !base || basis === normalizeTextKey('não mapeado / revisar');
  }

  function accessorialCalculationBaseErrorMessage() {
    return 'Selecione uma base de cálculo ou exclua a linha.';
  }

  function accessorialValueErrorMessage() {
    return 'Preencha um valor válido para esta taxa ou exclua a linha.';
  }

  function accessorialUnitErrorMessage() {
    return 'A unidade não é compatível com a base selecionada.';
  }

  function accessorialOperationErrorMessage() {
    return 'Revise a operação da base de cálculo selecionada.';
  }

  function accessorialFieldErrorMessage(error) {
    if (!error) return '';
    if (error.reason_code === 'incompatible_accessorial_unit') {
      return 'Ajuste a unidade para a base selecionada.';
    }
    return error.message || '';
  }

  function accessorialAdvanceValidationCountMessage(count) {
    if (count === 1) {
      return '1 item precisa de revisão. Corrija os campos destacados ou exclua a linha.';
    }
    return count + ' itens precisam de revisão. Corrija os campos destacados ou exclua as linhas.';
  }

  function collectTempTableAdvanceValidationErrors() {
    var fees = currentTempTable && Array.isArray(currentTempTable.accessorial_fees)
      ? currentTempTable.accessorial_fees
      : [];
    var errors = [];
    fees.forEach(function (fee, feeIndex) {
      if (!fee || typeof fee !== 'object' || isPrimaryFreightAccessorialFee(fee)) return;
      var error = null;
      if (accessorialFeeMissingCalculationBase(fee)) {
        error = {
          section: 'accessorial_fees',
          index: feeIndex,
          name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
          field: 'calculation_base_id',
          reason_code: 'missing_calculation_base',
          message: accessorialCalculationBaseErrorMessage()
        };
      } else if (!accessorialFeeOperationIsComplete(fee)) {
        error = {
          section: 'accessorial_fees',
          index: feeIndex,
          name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
          field: 'calculation_base_id',
          reason_code: 'unsupported_or_incomplete_operation',
          message: accessorialOperationErrorMessage()
        };
      } else if (!accessorialFeeHasRequiredValue(fee)) {
        error = {
          section: 'accessorial_fees',
          index: feeIndex,
          name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
          field: 'value',
          reason_code: 'invalid_accessorial_value',
          message: accessorialValueErrorMessage()
        };
      } else {
        var base = getCalculationBaseById(String(fee.calculation_base_id || '').trim());
        if (!accessorialFeeUnitMatchesBase(fee, base)) {
          error = {
            section: 'accessorial_fees',
            index: feeIndex,
            name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
            field: 'unit',
            reason_code: 'incompatible_accessorial_unit',
            message: accessorialUnitErrorMessage()
          };
        }
      }
      if (error) errors.push(error);
    });
    return errors;
  }

  function getAccessorialFeeValidationError(feeIndex, field) {
    for (var i = 0; i < tempTableValidationErrors.length; i += 1) {
      var error = tempTableValidationErrors[i];
      if (
        error
        && error.section === 'accessorial_fees'
        && Number(error.index) === feeIndex
        && (!field || error.field === field)
      ) {
        return error;
      }
    }
    return null;
  }

  function accessorialFeeHasValidationError(feeIndex) {
    return !!getAccessorialFeeValidationError(feeIndex);
  }

  function setTempTableValidationErrors(errors) {
    tempTableValidationErrors = Array.isArray(errors) ? errors.slice() : [];
    if (tempTableValidationErrors.length) {
      setTempTableModalError(accessorialAdvanceValidationCountMessage(tempTableValidationErrors.length));
    } else {
      setTempTableModalError('');
    }
  }

  function refreshTempTableValidationErrorsAfterAccessorialEdit() {
    if (!tempTableValidationErrors.length) return;
    setTempTableValidationErrors(collectTempTableAdvanceValidationErrors());
  }

  function focusFirstTempTableValidationError() {
    if (!tempTableValidationErrors.length) return;
    var first = tempTableValidationErrors[0];
    if (!first || first.section !== 'accessorial_fees') return;
    window.setTimeout(function () {
      var row = document.querySelector('[data-accessorial-fee-index="' + first.index + '"]');
      if (!row) return;
      if (typeof row.scrollIntoView === 'function') {
        row.scrollIntoView({ block: 'center', behavior: 'smooth' });
      }
      var field = row.querySelector('[data-field="' + (first.field || 'calculation_base_id') + '"]');
      if (field && typeof field.focus === 'function') field.focus();
    }, 0);
  }

  function ensureTempTableEditModeForValidation() {
    if (!currentTempTable || tempTableEditMode) return;
    tempTableEditSnapshot = deepCloneTempTable(currentTempTable);
    tempTableEditMode = true;
    tempTableModalActiveTab = 'freight';
    updateTempTableModalFooter();
  }

  function validateTempTableBeforeAdvance() {
    var errors = collectTempTableAdvanceValidationErrors();
    setTempTableValidationErrors(errors);
    if (errors.length) {
      ensureTempTableEditModeForValidation();
      renderTempTableModalContent(currentTempTable);
      focusFirstTempTableValidationError();
      return false;
    }
    return true;
  }

  function handleBackendTempTableValidationErrors(data) {
    if (!data || !Array.isArray(data.errors) || !data.errors.length) return false;
    setTempTableValidationErrors(data.errors);
    ensureTempTableEditModeForValidation();
    renderTempTableModalContent(currentTempTable);
    focusFirstTempTableValidationError();
    return true;
  }

  function saveTempTableAndAdvance() {
    if (!currentTempTable || tempTableSaveInFlight) return;
    if (!validateTempTableBeforeAdvance()) return;
    var payload = collectTempTableSavePayload();
    if (!payload) {
      setTempTableModalError('Nenhuma tabela temporária disponível para salvar.');
      return;
    }
    tempTableSaveInFlight = true;
    updateTempTableModalFooter();
    setTempTableModalError('');

    fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          if (handleBackendTempTableValidationErrors(res.data)) return;
          var errMsg = (res.data && res.data.message) || 'Não foi possível salvar a revisão da tabela temporária.';
          setTempTableModalError(errMsg);
          return;
        }
        if (res.data.temp_table) {
          currentTempTable = res.data.temp_table;
        }
        clearTempTableValidationErrors();
        tempTableEditMode = false;
        tempTableEditSnapshot = null;
        coverageStepActive = true;
        tempTableModalActiveTab = 'coverage';
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
        fetchDocuments();
      })
      .catch(function () {
        setTempTableModalError('Não foi possível salvar a revisão. Verifique sua conexão e tente novamente.');
      })
      .finally(function () {
        tempTableSaveInFlight = false;
        updateTempTableModalFooter();
      });
  }

  function byId(id) {
    return document.getElementById(id);
  }

  function handleStartAudit() {
    if (!currentTempTable) return;
    auditFileStepActive = true;
    tempTableModalActiveTab = 'audit';
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
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
    currentCalculationBases = Array.isArray(data.calculation_bases) ? data.calculation_bases : [];
    var tempTable = data.temp_table || null;
    var previousTempTableId = currentTempTable && currentTempTable.temp_table_id;
    var nextTempTableId = tempTable && tempTable.temp_table_id;
    if (previousTempTableId && nextTempTableId && previousTempTableId !== nextTempTableId) {
      resetCoveragePromptState();
      resetAuditFileStepState();
    } else if (previousTempTableId && !nextTempTableId) {
      resetCoveragePromptState();
      resetAuditFileStepState();
    }
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
      return 'Tabela temporária pronta e aguardando validação.';
    }
    if (status === 'needs_review') {
      return 'Tabela temporária disponível para revisão e edição.';
    }
    if (status === 'failed') return 'Não foi possível estruturar a tabela temporária.';
    if (status === 'expired') return 'Tabela temporária expirada.';
    if (status === 'discarded') return 'Tabela temporária invalidada.';
    return 'Tabela temporária extraída para revisão.';
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
          resetCoveragePromptState();
          resetAuditFileStepState();
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
              temp_table: res.data.temp_table,
              calculation_bases: res.data.calculation_bases || []
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
        resetCoveragePromptState();
        resetAuditFileStepState();
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

  var FREIGHT_WEIGHT_LIMITS = [30, 50, 70, 100];
  var NOT_IDENTIFIED_LABEL = 'não identificado';

  var PRIMARY_FREIGHT_FEE_PATTERNS = [
    /^taxa\s+embarque(\s+kg)?$/i,
    /^frete\s+valor(\s+%)?$/i,
    /^frete\s+peso(\s+kg)?$/i
  ];

  var COMMERCIAL_INFO_FIELDS = [
    { key: 'validity', label: 'Validade', altKeys: ['validade'] },
    { key: 'icms', label: 'ICMS' },
    { key: 'billing', label: 'Faturamento', altKeys: ['faturamento'] },
    { key: 'adjustment', label: 'Reajuste', altKeys: ['reajuste'] },
    { key: 'general_conditions', label: 'Condições gerais', altKeys: ['condicoes_gerais'] },
    { key: 'excluded_items', label: 'Itens não inclusos', altKeys: ['itens_nao_inclusos'] },
    { key: 'termination', label: 'Rescisão', altKeys: ['rescisao'] },
    { key: 'commercial_notes', label: 'Observações comerciais', altKeys: ['observacoes_comerciais'] }
  ];

  var FREIGHT_ROUTE_TABLE_HEADERS = [
    'Origem',
    'Destino',
    'Tipo',
    'Até 30 kg',
    'Até 50 kg',
    'Até 70 kg',
    'Até 100 kg',
    'Taxa Embarque Kg',
    'Frete Valor %',
    'Frete Peso Kg',
    'Observações'
  ];

  var FREIGHT_ROUTE_EDIT_FIELDS = [
    { key: 'origin', alt: null },
    { key: 'destination', alt: null },
    { key: 'freight_type', alt: 'type' },
    { key: 'weight_30', alt: 'weight_30kg' },
    { key: 'weight_50', alt: 'weight_50kg' },
    { key: 'weight_70', alt: 'weight_70kg' },
    { key: 'weight_100', alt: 'weight_100kg' },
    { key: 'boarding_fee', alt: 'taxa_embarque_kg' },
    { key: 'freight_value_pct', alt: 'frete_valor_pct' },
    { key: 'freight_weight_kg', alt: 'frete_peso_kg' },
    { key: 'notes', alt: 'observations' }
  ];

  function normalizeTextKey(value) {
    return String(value || '')
      .toLowerCase()
      .normalize('NFD')
      .replace(/[\u0300-\u036f]/g, '')
      .trim();
  }

  function isPrimaryFreightAccessorialFee(fee) {
    if (!fee || typeof fee !== 'object') return false;
    var name = normalizeTextKey(fee.name);
    if (!name) return false;
    var i;
    for (i = 0; i < PRIMARY_FREIGHT_FEE_PATTERNS.length; i += 1) {
      if (PRIMARY_FREIGHT_FEE_PATTERNS[i].test(name)) return true;
    }
    return false;
  }

  function formatDisplayValue(value, unit) {
    if (!hasFieldValue(value)) {
      if (hasFieldValue(unit)) return displayFieldValue(null);
      return '';
    }
    var text = String(value);
    if (hasFieldValue(unit) && text.indexOf(String(unit)) === -1) {
      return text + ' ' + String(unit);
    }
    return text;
  }

  function tableCellText(value, allowEmpty) {
    if (!hasFieldValue(value)) {
      return allowEmpty ? '' : displayFieldValue(null);
    }
    return String(value);
  }

  function extractWeightLimitFromText(text) {
    var match = String(text || '').match(/(\d{1,3})\s*kg/i);
    if (!match) return null;
    var n = parseInt(match[1], 10);
    if (FREIGHT_WEIGHT_LIMITS.indexOf(n) !== -1) return n;
    return null;
  }

  function getWeightColumnValues(tempTable) {
    var cols = { 30: '', 50: '', 70: '', 100: '' };
    var notes = [];
    var freightValues = Array.isArray(tempTable.freight_values) ? tempTable.freight_values : [];
    var weightRanges = Array.isArray(tempTable.weight_ranges) ? tempTable.weight_ranges : [];

    freightValues.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      var limit = extractWeightLimitFromText(item.label);
      if (limit && !hasFieldValue(cols[limit])) {
        cols[limit] = formatDisplayValue(item.value, item.unit);
      } else if (!limit && hasFieldValue(item.label)) {
        notes.push(String(item.label) + (hasFieldValue(item.value) ? ': ' + formatDisplayValue(item.value, item.unit) : ''));
      }
      if (hasFieldValue(item.notes)) notes.push(String(item.notes));
    });

    weightRanges.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      var limit = null;
      if (typeof item.max_weight === 'number' && FREIGHT_WEIGHT_LIMITS.indexOf(item.max_weight) !== -1) {
        limit = item.max_weight;
      }
      if (!limit) limit = extractWeightLimitFromText(item.label);
      if (limit && !hasFieldValue(cols[limit])) {
        var rangeVal = hasFieldValue(item.notes) ? String(item.notes) : '';
        if (!rangeVal && (hasFieldValue(item.min_weight) || hasFieldValue(item.max_weight))) {
          rangeVal = [
            hasFieldValue(item.min_weight) ? String(item.min_weight) : '',
            hasFieldValue(item.max_weight) ? String(item.max_weight) : ''
          ].filter(Boolean).join('-') + (item.unit ? ' ' + item.unit : '');
        }
        if (rangeVal) cols[limit] = rangeVal;
      } else if (hasFieldValue(item.label)) {
        notes.push(String(item.label));
      }
    });

    return { cols: cols, notes: notes };
  }

  function getPrimaryFreightFees(accessorialFees) {
    var fees = Array.isArray(accessorialFees) ? accessorialFees : [];
    var boarding = '';
    var freightValue = '';
    var freightWeight = '';
    fees.forEach(function (fee) {
      if (!fee || typeof fee !== 'object') return;
      var name = normalizeTextKey(fee.name);
      var formatted = formatDisplayValue(fee.value, fee.unit);
      if (/^taxa\s+embarque/.test(name)) boarding = formatted || boarding;
      else if (/^frete\s+valor/.test(name)) freightValue = formatted || freightValue;
      else if (/^frete\s+peso/.test(name)) freightWeight = formatted || freightWeight;
    });
    return {
      boarding_fee: boarding,
      freight_value_pct: freightValue,
      freight_weight_kg: freightWeight
    };
  }

  function hasFreightRouteSourceData(tempTable) {
    var freightRoutes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    if (freightRoutes.length) return true;
    var freightValues = Array.isArray(tempTable.freight_values) ? tempTable.freight_values : [];
    var weightRanges = Array.isArray(tempTable.weight_ranges) ? tempTable.weight_ranges : [];
    if (freightValues.length || weightRanges.length) return true;
    var fees = Array.isArray(tempTable.accessorial_fees) ? tempTable.accessorial_fees : [];
    return fees.some(function (fee) { return isPrimaryFreightAccessorialFee(fee); });
  }

  function buildStructuredFreightRows(freightRoutes) {
    return freightRoutes.map(function (route) {
      if (!route || typeof route !== 'object') return null;
      var routeType = route.freight_type || route.type;
      function routeField(primary, alias) {
        var value = route[primary];
        if (!hasFieldValue(value) && alias) value = route[alias];
        return hasFieldValue(value) ? String(value) : '';
      }
      return {
        origin: hasFieldValue(route.origin) ? String(route.origin) : NOT_IDENTIFIED_LABEL,
        destination: hasFieldValue(route.destination) ? String(route.destination) : NOT_IDENTIFIED_LABEL,
        type: hasFieldValue(routeType) ? String(routeType) : NOT_IDENTIFIED_LABEL,
        weight_30: routeField('weight_30', 'weight_30kg'),
        weight_50: routeField('weight_50', 'weight_50kg'),
        weight_70: routeField('weight_70', 'weight_70kg'),
        weight_100: routeField('weight_100', 'weight_100kg'),
        boarding_fee: routeField('boarding_fee', 'taxa_embarque_kg'),
        freight_value_pct: routeField('freight_value_pct', 'frete_valor_pct'),
        freight_weight_kg: routeField('freight_weight_kg', 'frete_peso_kg'),
        notes: routeField('notes') || routeField('observations') || routeField('observacoes')
      };
    }).filter(Boolean);
  }

  function buildPartialFreightRows(tempTable) {
    var weightData = getWeightColumnValues(tempTable);
    var primaryFees = getPrimaryFreightFees(tempTable.accessorial_fees);
    var textualNotes = weightData.notes.slice();
    var cols = weightData.cols;
    var hasAnyWeight = FREIGHT_WEIGHT_LIMITS.some(function (limit) { return hasFieldValue(cols[limit]); });
    var hasAnyFee = hasFieldValue(primaryFees.boarding_fee)
      || hasFieldValue(primaryFees.freight_value_pct)
      || hasFieldValue(primaryFees.freight_weight_kg);

    if (!hasAnyWeight && !hasAnyFee && !textualNotes.length) return [];

    return [{
      origin: NOT_IDENTIFIED_LABEL,
      destination: NOT_IDENTIFIED_LABEL,
      type: NOT_IDENTIFIED_LABEL,
      weight_30: cols[30],
      weight_50: cols[50],
      weight_70: cols[70],
      weight_100: cols[100],
      boarding_fee: primaryFees.boarding_fee,
      freight_value_pct: primaryFees.freight_value_pct,
      freight_weight_kg: primaryFees.freight_weight_kg,
      notes: textualNotes.join('; ')
    }];
  }

  function resolveFreightRouteRows(tempTable) {
    var freightRoutes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    if (freightRoutes.length) {
      return { rows: buildStructuredFreightRows(freightRoutes), isPartial: false };
    }
    return { rows: buildPartialFreightRows(tempTable), isPartial: true };
  }

  function appendTableCell(tr, text, isHeader, allowEmpty) {
    var el = document.createElement(isHeader ? 'th' : 'td');
    el.textContent = isHeader ? String(text) : tableCellText(text, allowEmpty);
    if (isHeader) el.scope = 'col';
    tr.appendChild(el);
  }

  function readFreightRouteField(route, field) {
    var value = route[field.key];
    if (!hasFieldValue(value) && field.alt) value = route[field.alt];
    return hasFieldValue(value) ? String(value) : '';
  }

  function writeFreightRouteField(route, field, newValue) {
    route[field.key] = newValue;
    if (field.alt && field.alt in route) {
      delete route[field.alt];
    }
  }

  function renderEditableFreightRoutesTable(container, tempTable) {
    var routes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    FREIGHT_ROUTE_TABLE_HEADERS.forEach(function (heading) {
      appendTableCell(headerRow, heading, true, false);
    });
    var actionsHeader = document.createElement('th');
    actionsHeader.scope = 'col';
    actionsHeader.className = 'cleide-audit-temp-table-modal-actions-col';
    actionsHeader.textContent = 'Ações';
    headerRow.appendChild(actionsHeader);
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    routes.forEach(function (route, rowIndex) {
      if (!route || typeof route !== 'object') return;
      var tr = document.createElement('tr');
      FREIGHT_ROUTE_EDIT_FIELDS.forEach(function (field) {
        appendEditableCell(tr, readFreightRouteField(route, field), function (newValue) {
          if (!currentTempTable.freight_routes[rowIndex]) return;
          writeFreightRouteField(currentTempTable.freight_routes[rowIndex], field, newValue);
        });
      });
      appendRowDeleteCell(tr, function () {
        if (!Array.isArray(currentTempTable.freight_routes)) return;
        currentTempTable.freight_routes.splice(rowIndex, 1);
        renderTempTableModalContent(currentTempTable);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);
  }

  function renderFreightRoutesSection(container, tempTable) {
    appendSectionTitle(container, 'Frete por rota');
    var section = document.createElement('div');
    section.className = 'cleide-audit-temp-table-modal-section cleide-audit-temp-table-modal-freight-section';

    if (tempTableEditMode && canEditFreightRoutes(tempTable)) {
      if (!tempTable.freight_routes.length) {
        var emptyEdit = document.createElement('p');
        emptyEdit.className = 'cleide-audit-temp-table-modal-empty';
        emptyEdit.textContent = 'Nenhuma rota de frete identificada nesta extração.';
        section.appendChild(emptyEdit);
        container.appendChild(section);
        return;
      }
      renderEditableFreightRoutesTable(section, tempTable);
      container.appendChild(section);
      return;
    }

    var resolved = resolveFreightRouteRows(tempTable);
    var rows = resolved.rows;

    if (resolved.isPartial) {
      var badgeRow = document.createElement('div');
      badgeRow.className = 'cleide-audit-temp-table-modal-partial-badge-row';
      var badge = document.createElement('span');
      badge.className = 'cleide-audit-temp-table-modal-partial-badge';
      badge.textContent = 'extração parcial';
      badgeRow.appendChild(badge);
      section.appendChild(badgeRow);
      var helper = document.createElement('p');
      helper.className = 'cleide-audit-temp-table-modal-partial-helper';
      helper.textContent = 'Alguns vínculos de origem, destino ou tipo de frete ainda precisam de validação humana.';
      section.appendChild(helper);
    }

    if (!hasFreightRouteSourceData(tempTable) || !rows.length) {
      var empty = document.createElement('p');
      empty.className = 'cleide-audit-temp-table-modal-empty';
      empty.textContent = 'Nenhuma rota de frete identificada nesta extração.';
      section.appendChild(empty);
      container.appendChild(section);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    FREIGHT_ROUTE_TABLE_HEADERS.forEach(function (heading) {
      appendTableCell(headerRow, heading, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (row) {
      var tr = document.createElement('tr');
      appendTableCell(tr, row.origin, false, false);
      appendTableCell(tr, row.destination, false, false);
      appendTableCell(tr, row.type, false, false);
      appendTableCell(tr, row.weight_30, false, false);
      appendTableCell(tr, row.weight_50, false, false);
      appendTableCell(tr, row.weight_70, false, false);
      appendTableCell(tr, row.weight_100, false, false);
      appendTableCell(tr, row.boarding_fee, false, false);
      appendTableCell(tr, row.freight_value_pct, false, false);
      appendTableCell(tr, row.freight_weight_kg, false, false);
      appendTableCell(tr, row.notes, false, true);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    section.appendChild(scrollWrap);
    container.appendChild(section);
  }

  var FREIGHT_TABLE_CONTEXT_FIELDS = [
    { key: 'route_label', label: 'Rota / tabela' },
    { key: 'origin', label: 'Origem' },
    { key: 'destination', label: 'Destino' },
    { key: 'customer', label: 'Cliente' },
    { key: 'supplier', label: 'Fornecedor' },
    { key: 'valid_from', label: 'Válido de' },
    { key: 'valid_to', label: 'Válido até' },
    { key: 'delivery_deadline', label: 'Prazo de entrega' }
  ];

  function hasUsefulFreightTables(tempTable) {
    var tables = Array.isArray(tempTable.freight_tables) ? tempTable.freight_tables : [];
    return tables.length > 0;
  }

  function getFreightTableKey(table, index) {
    if (!table || typeof table !== 'object') return 'index:' + index;
    var parts = [];
    if (hasFieldValue(table.evidence_ref)) parts.push(String(table.evidence_ref));
    if (hasFieldValue(table.table_title)) parts.push(String(table.table_title));
    if (hasFieldValue(table.table_type)) parts.push(String(table.table_type));
    var context = table.context;
    if (context && typeof context === 'object' && hasFieldValue(context.route_label)) {
      parts.push(String(context.route_label));
    }
    if (!parts.length) return 'index:' + index;
    return parts.join('::');
  }

  function resetFreightTableOpenState() {
    openFreightTableKeys.clear();
    hasUserTouchedFreightTableOpenState = false;
  }

  function renderFreightTableContext(container, context) {
    if (!context || typeof context !== 'object') return;
    var ctxWrap = document.createElement('div');
    ctxWrap.className = 'cleide-audit-temp-table-modal-freight-table-context';
    var hasAny = false;
    FREIGHT_TABLE_CONTEXT_FIELDS.forEach(function (field) {
      var val = context[field.key];
      if (!hasFieldValue(val)) return;
      hasAny = true;
      appendDetailRow(ctxWrap, field.label, String(val));
    });
    if (hasAny) container.appendChild(ctxWrap);
  }

  function appendEditableCell(tr, value, onChange) {
    var td = document.createElement('td');
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'cleide-audit-temp-table-modal-cell-input';
    input.value = hasFieldValue(value) ? String(value) : '';
    input.addEventListener('input', function () {
      if (typeof onChange === 'function') onChange(input.value);
    });
    td.appendChild(input);
    tr.appendChild(td);
  }

  function appendRowDeleteCell(tr, onDelete) {
    var td = document.createElement('td');
    td.className = 'cleide-audit-temp-table-modal-actions-col';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'cleide-audit-temp-table-modal-row-delete-btn';
    btn.textContent = 'Excluir';
    btn.setAttribute('aria-label', 'Excluir linha');
    btn.addEventListener('click', function () {
      if (typeof onDelete === 'function') onDelete();
    });
    td.appendChild(btn);
    tr.appendChild(td);
  }

  function renderDynamicFreightTable(container, freightTable, tableIndex) {
    var columns = Array.isArray(freightTable.columns) ? freightTable.columns : [];
    var rows = Array.isArray(freightTable.rows) ? freightTable.rows : [];
    var editMode = !!tempTableEditMode;

    if (!columns.length && !rows.length) {
      var empty = document.createElement('p');
      empty.className = 'cleide-audit-temp-table-modal-empty';
      empty.textContent = 'Nenhuma linha identificada nesta tabela.';
      container.appendChild(empty);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table';

    if (columns.length) {
      var thead = document.createElement('thead');
      var headerRow = document.createElement('tr');
      columns.forEach(function (col, colIndex) {
        if (editMode) {
          var th = document.createElement('th');
          th.scope = 'col';
          var headerWrap = document.createElement('div');
          var label = document.createElement('span');
          label.textContent = String(col);
          headerWrap.appendChild(label);
          var colBtn = document.createElement('button');
          colBtn.type = 'button';
          colBtn.className = 'cleide-audit-temp-table-modal-col-delete-btn';
          colBtn.textContent = '×';
          colBtn.setAttribute('aria-label', 'Excluir coluna ' + col);
          colBtn.addEventListener('click', function () {
            if (!window.confirm('Excluir a coluna "' + col + '"? Esta ação não pode ser desfeita sem cancelar a edição.')) {
              return;
            }
            var tableRef = currentTempTable.freight_tables[tableIndex];
            if (!tableRef || !Array.isArray(tableRef.columns)) return;
            var removedCol = tableRef.columns[colIndex];
            tableRef.columns.splice(colIndex, 1);
            (tableRef.rows || []).forEach(function (row) {
              if (row && typeof row === 'object' && removedCol in row) {
                delete row[removedCol];
              }
            });
            renderTempTableModalContent(currentTempTable);
          });
          headerWrap.appendChild(colBtn);
          th.appendChild(headerWrap);
          headerRow.appendChild(th);
        } else {
          appendTableCell(headerRow, col, true, false);
        }
      });
      if (editMode) {
        var actionsHeader = document.createElement('th');
        actionsHeader.scope = 'col';
        actionsHeader.className = 'cleide-audit-temp-table-modal-actions-col';
        actionsHeader.textContent = 'Ações';
        headerRow.appendChild(actionsHeader);
      }
      thead.appendChild(headerRow);
      table.appendChild(thead);
    }

    var tbody = document.createElement('tbody');
    rows.forEach(function (row, rowIndex) {
      if (!row || typeof row !== 'object') return;
      var tr = document.createElement('tr');
      if (columns.length) {
        columns.forEach(function (col) {
          if (editMode) {
            appendEditableCell(tr, row[col], function (newValue) {
              if (!currentTempTable.freight_tables[tableIndex]) return;
              if (!Array.isArray(currentTempTable.freight_tables[tableIndex].rows)) {
                currentTempTable.freight_tables[tableIndex].rows = [];
              }
              if (!currentTempTable.freight_tables[tableIndex].rows[rowIndex]) {
                currentTempTable.freight_tables[tableIndex].rows[rowIndex] = {};
              }
              currentTempTable.freight_tables[tableIndex].rows[rowIndex][col] = newValue;
            });
          } else {
            appendTableCell(tr, row[col], false, false);
          }
        });
      } else if (!editMode) {
        Object.keys(row).forEach(function (key) {
          appendTableCell(tr, row[key], false, false);
        });
      }
      if (editMode) {
        appendRowDeleteCell(tr, function () {
          var tableRef = currentTempTable.freight_tables[tableIndex];
          if (!tableRef || !Array.isArray(tableRef.rows)) return;
          tableRef.rows.splice(rowIndex, 1);
          renderTempTableModalContent(currentTempTable);
        });
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);
  }

  function renderFreightTableCard(container, freightTable, index) {
    var card = document.createElement('details');
    card.className = 'cleide-audit-temp-table-modal-freight-table-card';
    var tableKey = getFreightTableKey(freightTable, index);
    if (hasUserTouchedFreightTableOpenState) {
      card.open = openFreightTableKeys.has(tableKey);
    } else {
      card.open = index === 0;
    }
    card.addEventListener('toggle', function () {
      hasUserTouchedFreightTableOpenState = true;
      if (card.open) {
        openFreightTableKeys.add(tableKey);
      } else {
        openFreightTableKeys.delete(tableKey);
      }
    });

    var summary = document.createElement('summary');
    summary.className = 'cleide-audit-temp-table-modal-freight-table-summary';
    var titleText = hasFieldValue(freightTable.table_title)
      ? String(freightTable.table_title)
      : 'Tabela ' + (index + 1);
    summary.textContent = titleText;
    card.appendChild(summary);

    var cardBody = document.createElement('div');
    cardBody.className = 'cleide-audit-temp-table-modal-freight-table-body';

    if (hasFieldValue(freightTable.table_type)) {
      appendDetailRow(cardBody, 'Tipo', String(freightTable.table_type));
    }
    renderFreightTableContext(cardBody, freightTable.context);
    renderDynamicFreightTable(cardBody, freightTable, index);
    if (hasFieldValue(freightTable.notes)) {
      appendDetailRow(cardBody, 'Observações', String(freightTable.notes));
    }
    if (hasFieldValue(freightTable.evidence_ref)) {
      appendDetailRow(cardBody, 'Evidência', String(freightTable.evidence_ref));
    }

    card.appendChild(cardBody);
    container.appendChild(card);
  }

  function renderFreightTablesSection(container, tempTable) {
    appendSectionTitle(container, 'Tabelas de frete identificadas');
    var section = document.createElement('div');
    section.className = 'cleide-audit-temp-table-modal-section cleide-audit-temp-table-modal-freight-tables-section';

    var tables = Array.isArray(tempTable.freight_tables) ? tempTable.freight_tables : [];
    if (!tables.length) {
      var empty = document.createElement('p');
      empty.className = 'cleide-audit-temp-table-modal-empty';
      empty.textContent = 'Nenhuma tabela tarifária identificada nesta extração.';
      section.appendChild(empty);
      container.appendChild(section);
      return;
    }

    tables.forEach(function (freightTable, index) {
      if (!freightTable || typeof freightTable !== 'object') return;
      renderFreightTableCard(section, freightTable, index);
    });
    container.appendChild(section);
  }

  function renderMainFreightSection(container, tempTable) {
    if (hasUsefulFreightTables(tempTable)) {
      renderFreightTablesSection(container, tempTable);
      return;
    }
    var freightRoutes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    if (freightRoutes.length) {
      renderFreightRoutesSection(container, tempTable);
      return;
    }
    renderFreightRoutesSection(container, tempTable);
  }

  function getGeneralAccessorialFees(accessorialFees) {
    return (Array.isArray(accessorialFees) ? accessorialFees : []).filter(function (fee) {
      return !isPrimaryFreightAccessorialFee(fee);
    });
  }

  function renderAccessorialFeesSection(container, accessorialFees) {
    appendSectionTitle(container, 'Generalidades e serviços adicionais');
    var list = getGeneralAccessorialFees(accessorialFees);
    if (!list.length) {
      appendEmptySectionMessage(container);
      return;
    }

    if (tempTableEditMode && canEditAccessorialFees(currentTempTable)) {
      renderEditableAccessorialFeesSection(container, list);
      return;
    }

    var showScope = list.some(function (item) {
      return item && hasFieldValue(item.scope);
    });

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-data-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    var headers = ['Nome', 'Valor', 'Unidade', 'Base de cálculo', 'Observações'];
    if (showScope) headers.push('Escopo');
    headers.forEach(function (heading) {
      appendTableCell(headerRow, heading, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    list.forEach(function (item) {
      if (!item || typeof item !== 'object') return;
      var tr = document.createElement('tr');
      appendTableCell(tr, displayFieldValue(item.name), false, false);
      appendTableCell(tr, displayFieldValue(item.value), false, false);
      appendTableCell(tr, displayFieldValue(item.unit), false, false);
      appendReadonlyCalculationBasisCell(tr, item);
      appendTableCell(tr, hasFieldValue(item.notes) ? String(item.notes) : displayFieldValue(null), false, false);
      if (showScope) {
        appendTableCell(tr, hasFieldValue(item.scope) ? String(item.scope) : displayFieldValue(null), false, false);
      }
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function appendAccessorialFieldCell(tr, value, onChange, placeholder, options) {
    options = options || {};
    var td = document.createElement('td');
    if (options.validationError) td.className = 'accessorial-field-cell accessorial-field-cell--invalid';
    var input = document.createElement('input');
    input.type = 'text';
    input.className = 'cleide-audit-temp-table-modal-cell-input';
    if (options.field) input.setAttribute('data-field', options.field);
    input.value = hasFieldValue(value) ? String(value) : '';
    if (placeholder) input.placeholder = placeholder;
    if (options.validationError) {
      input.className += ' field-invalid';
      input.setAttribute('aria-invalid', 'true');
    }
    input.addEventListener('input', function () {
      if (typeof onChange === 'function') onChange(input.value);
    });
    td.appendChild(input);
    if (options.validationError) {
      var icon = document.createElement('span');
      icon.className = 'accessorial-field-error-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '⚠';
      td.appendChild(icon);
      var hint = document.createElement('div');
      hint.className = 'accessorial-field-error';
      hint.setAttribute('role', 'note');
      hint.textContent = accessorialFieldErrorMessage(options.validationError);
      td.appendChild(hint);
    }
    tr.appendChild(td);
  }

  function appendReadonlyCalculationBasisCell(tr, item) {
    var td = document.createElement('td');
    var baseId = String((item && item.calculation_base_id) || '').trim();
    var resolvedBase = baseId ? getCalculationBaseById(baseId) : null;
    if (resolvedBase) {
      td.textContent = hasFieldValue(item.calculation_base_label)
        ? String(item.calculation_base_label)
        : String(resolvedBase.label || '');
    } else {
      var extracted = hasFieldValue(item.raw_calculation_basis)
        ? String(item.raw_calculation_basis)
        : (hasFieldValue(item.calculation_basis) ? String(item.calculation_basis) : '');
      td.textContent = 'não mapeado / revisar';
      if (extracted && normalizeTextKey(extracted) !== normalizeTextKey('não mapeado / revisar')) {
        var extra = document.createElement('div');
        extra.className = 'accessorial-basis-extracted-text';
        extra.textContent = 'texto extraído: ' + extracted;
        td.appendChild(extra);
      }
    }
    tr.appendChild(td);
  }

  function calculationBaseOptionLabel(base) {
    var label = String((base && base.label) || '');
    var unit = String((base && base.unit) || '').trim();
    return unit ? label + ' (' + unit + ')' : label;
  }

  function markAccessorialFeeAsUnmapped(fee) {
    if (!fee) return;
    fee.calculation_base_id = null;
    fee.calculation_base_label = null;
    fee.calculation_basis = 'não mapeado / revisar';
    fee.calculation_type = 'unknown';
    fee.audit_variable = null;
    fee.operation = null;
    fee.operation_parameters = {};
    fee.classification_source = 'manual_unmapped_calculation_base';
  }

  function applyCalculationBaseToAccessorialFee(fee, base) {
    if (!fee || !base) return;
    fee.calculation_base_id = base.id || null;
    fee.calculation_base_label = base.label || '';
    fee.calculation_basis = base.label || '';
    fee.calculation_type = base.calculation_type || 'unknown';
    fee.audit_variable = base.audit_variable || null;
    fee.operation = base.operation || null;
    fee.operation_parameters = deepCloneValue(base.parameters || {});
    fee.classification_source = 'manual_configured_calculation_base';
    if (base.unit) fee.unit = base.unit;
  }

  function appendCalculationBaseSelectCell(tr, item, onChange, validationError) {
    var td = document.createElement('td');
    td.className = validationError ? 'calculation-base-cell calculation-base-cell--invalid' : 'calculation-base-cell';
    var select = document.createElement('select');
    select.className = 'cleide-audit-temp-table-modal-cell-input';
    select.setAttribute('data-field', 'calculation_base_id');
    if (validationError) {
      select.className += ' field-invalid';
      select.setAttribute('aria-invalid', 'true');
    }
    var placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'não mapeado / revisar';
    select.appendChild(placeholder);
    currentCalculationBases.forEach(function (base) {
      if (!base || !base.id) return;
      var option = document.createElement('option');
      option.value = String(base.id);
      option.textContent = calculationBaseOptionLabel(base);
      select.appendChild(option);
    });
    select.value = item && item.calculation_base_id ? String(item.calculation_base_id) : '';
    select.addEventListener('change', function () {
      if (typeof onChange === 'function') onChange(select.value);
    });
    td.appendChild(select);
    if (validationError) {
      var icon = document.createElement('span');
      icon.className = 'accessorial-field-error-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '⚠';
      td.appendChild(icon);
      var hint = document.createElement('div');
      hint.className = 'accessorial-field-error';
      hint.setAttribute('role', 'note');
      hint.textContent = accessorialFieldErrorMessage(validationError);
      td.appendChild(hint);
    }
    tr.appendChild(td);
  }

  function renderEditableAccessorialFeesSection(container, list) {
    var section = document.createElement('div');
    section.className = 'cleide-audit-temp-table-modal-section cleide-audit-temp-table-modal-accessorial-section';

    var helper = document.createElement('p');
    helper.className = 'cleide-audit-temp-table-modal-partial-helper';
    helper.textContent = 'Edite generalidades e serviços adicionais com mais espaço e salve tudo junto na revisão.';
    section.appendChild(helper);

    var actions = document.createElement('div');
    actions.className = 'cleide-audit-temp-table-modal-toolbar';
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'cleide-audit-temp-table-modal-add-btn';
    addBtn.textContent = 'Adicionar item';
    addBtn.addEventListener('click', function () {
      if (!Array.isArray(currentTempTable.accessorial_fees)) currentTempTable.accessorial_fees = [];
      currentTempTable.accessorial_fees.push({ name: '', value: '', unit: '', calculation_basis: 'não mapeado / revisar', calculation_base_id: null, calculation_base_label: null, raw_calculation_basis: '', notes: '', scope: 'general' });
      renderTempTableModalContent(currentTempTable);
    });
    actions.appendChild(addBtn);
    section.appendChild(actions);

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';
    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-data-table cleide-audit-temp-table-modal-data-table-editable';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['Nome', 'Valor', 'Unidade', 'Base de cálculo', 'Observações', 'Escopo', 'Ações'].forEach(function (heading) {
      appendTableCell(headerRow, heading, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    (currentTempTable.accessorial_fees || []).forEach(function (item, feeIndex) {
      if (!item || typeof item !== 'object' || isPrimaryFreightAccessorialFee(item)) return;
      var tr = document.createElement('tr');
      tr.setAttribute('data-accessorial-fee-index', String(feeIndex));
      var validationError = getAccessorialFeeValidationError(feeIndex);
      if (accessorialFeeHasValidationError(feeIndex)) tr.className = 'accessorial-row--invalid';
      appendAccessorialFieldCell(tr, item.name, function (newValue) {
        if (currentTempTable.accessorial_fees[feeIndex]) currentTempTable.accessorial_fees[feeIndex].name = newValue;
      }, 'Ex.: Pedágio geral');
      appendAccessorialFieldCell(tr, item.value, function (newValue) {
        if (currentTempTable.accessorial_fees[feeIndex]) currentTempTable.accessorial_fees[feeIndex].value = newValue;
        refreshTempTableValidationErrorsAfterAccessorialEdit();
      }, 'Ex.: conforme tabela', {
        field: 'value',
        validationError: getAccessorialFeeValidationError(feeIndex, 'value')
      });
      appendAccessorialFieldCell(tr, item.unit, function (newValue) {
        if (currentTempTable.accessorial_fees[feeIndex]) currentTempTable.accessorial_fees[feeIndex].unit = newValue;
        refreshTempTableValidationErrorsAfterAccessorialEdit();
      }, 'R$, %, texto', {
        field: 'unit',
        validationError: getAccessorialFeeValidationError(feeIndex, 'unit')
      });
      appendCalculationBaseSelectCell(tr, item, function (baseId) {
        var fee = currentTempTable.accessorial_fees[feeIndex];
        if (!fee) return;
        var base = getCalculationBaseById(baseId);
        if (base) {
          applyCalculationBaseToAccessorialFee(fee, base);
        } else {
          markAccessorialFeeAsUnmapped(fee);
        }
        refreshTempTableValidationErrorsAfterAccessorialEdit();
        renderTempTableModalContent(currentTempTable);
      }, getAccessorialFeeValidationError(feeIndex, 'calculation_base_id'));
      appendAccessorialFieldCell(tr, item.notes, function (newValue) {
        if (currentTempTable.accessorial_fees[feeIndex]) currentTempTable.accessorial_fees[feeIndex].notes = newValue;
      }, 'Observações');
      appendAccessorialFieldCell(tr, item.scope, function (newValue) {
        if (currentTempTable.accessorial_fees[feeIndex]) currentTempTable.accessorial_fees[feeIndex].scope = newValue;
      }, 'general');
      appendRowDeleteCell(tr, function () {
        if (!Array.isArray(currentTempTable.accessorial_fees)) return;
        currentTempTable.accessorial_fees.splice(feeIndex, 1);
        refreshTempTableValidationErrorsAfterAccessorialEdit();
        renderTempTableModalContent(currentTempTable);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    section.appendChild(scrollWrap);
    container.appendChild(section);
  }

  function collectCommercialInfo(tempTable) {
    var entries = [];
    var seen = {};
    var commercial = tempTable.commercial_info;

    function pushEntry(label, value) {
      if (!hasFieldValue(value) || seen[label]) return;
      seen[label] = true;
      entries.push({ label: label, value: String(value) });
    }

    function readField(source, field) {
      if (!source || typeof source !== 'object') return;
      var val = source[field.key];
      if (!hasFieldValue(val) && field.altKeys) {
        field.altKeys.forEach(function (altKey) {
          if (!hasFieldValue(val)) val = source[altKey];
        });
      }
      pushEntry(field.label, val);
    }

    if (commercial && typeof commercial === 'object') {
      COMMERCIAL_INFO_FIELDS.forEach(function (field) {
        readField(commercial, field);
      });
    }
    COMMERCIAL_INFO_FIELDS.forEach(function (field) {
      readField(tempTable, field);
    });
    return entries;
  }

  function renderAdditionalInfoSection(container, tempTable) {
    appendSectionTitle(container, 'Informações adicionais');
    var entries = collectCommercialInfo(tempTable);
    if (!entries.length) {
      var empty = document.createElement('p');
      empty.className = 'cleide-audit-temp-table-modal-empty';
      empty.textContent = 'Informações adicionais não identificadas no artefato atual.';
      container.appendChild(empty);
      return;
    }
    entries.forEach(function (entry) {
      appendDetailRow(container, entry.label, entry.value);
    });
  }

  function renderTempTableModalTabs(container, tempTable) {
    var tabs = document.createElement('div');
    tabs.className = 'cleide-audit-temp-table-modal-tabs';
    tabs.setAttribute('role', 'tablist');

    function makeTab(id, label, active) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'cleide-audit-temp-table-modal-tab' + (active ? ' is-active' : '');
      btn.id = id;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.textContent = label;
      return btn;
    }

    var freightTab = makeTab('cleideAuditTempTableTabFreight', 'Tabela de frete', tempTableModalActiveTab === 'freight');
    freightTab.addEventListener('click', function () {
      tempTableModalActiveTab = 'freight';
      renderTempTableModalContent(tempTable);
      updateTempTableModalFooter();
    });

    var coverageTab = makeTab('cleideAuditTempTableTabCoverage', 'Cidades atendidas', tempTableModalActiveTab === 'coverage');
    coverageTab.addEventListener('click', function () {
      tempTableModalActiveTab = 'coverage';
      renderTempTableModalContent(tempTable);
      updateTempTableModalFooter();
    });

    tabs.appendChild(freightTab);
    if (shouldShowCoverageTab(tempTable)) {
      tabs.appendChild(coverageTab);
    }
    if (shouldShowAuditTab(tempTable)) {
      var auditTab = makeTab('cleideAuditTempTableTabAudit', 'Arquivo para auditoria', tempTableModalActiveTab === 'audit');
      auditTab.addEventListener('click', function () {
        tempTableModalActiveTab = 'audit';
        renderTempTableModalContent(tempTable);
        updateTempTableModalFooter();
      });
      tabs.appendChild(auditTab);
    }
    container.appendChild(tabs);
  }

  function renderAuditFileTabContent(container, tempTable) {
    appendSectionTitle(container, 'Arquivo para auditoria');

    var section = document.createElement('div');
    section.className = 'cleide-audit-temp-table-modal-section cleide-audit-audit-file-section';

    var card = document.createElement('div');
    card.className = 'cleide-audit-audit-file-card';

    var intro = document.createElement('p');
    intro.className = 'cleide-audit-audit-file-description';
    intro.textContent = 'Baixe o modelo, preencha com os fretes cobrados e envie o arquivo para auditoria.';
    card.appendChild(intro);

    var actions = document.createElement('div');
    actions.className = 'cleide-audit-audit-file-actions';

    var downloadBtn = document.createElement('a');
    downloadBtn.className = 'cleide-audit-audit-file-download-btn';
    downloadBtn.href = API_AUDIT_TEMPLATE;
    downloadBtn.setAttribute('download', '');
    downloadBtn.textContent = 'Baixar modelo';
    actions.appendChild(downloadBtn);

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.id = 'cleideAuditAuditFileInput';
    fileInput.className = 'visually-hidden';
    fileInput.accept = '.csv,.xlsx';
    fileInput.setAttribute('tabindex', '-1');
    fileInput.setAttribute('aria-hidden', 'true');

    var uploadLabel = document.createElement('label');
    uploadLabel.className = 'cleide-audit-audit-file-upload-btn';
    uploadLabel.setAttribute('for', 'cleideAuditAuditFileInput');
    uploadLabel.textContent = 'Enviar arquivo preenchido';
    actions.appendChild(uploadLabel);
    actions.appendChild(fileInput);
    card.appendChild(actions);

    var fileName = document.createElement('span');
    fileName.className = 'cleide-audit-audit-file-name';
    fileName.id = 'cleideAuditAuditUploadFileName';
    fileName.textContent = 'Nenhum arquivo selecionado';
    card.appendChild(fileName);

    var status = document.createElement('p');
    status.className = 'cleide-audit-audit-file-status';
    status.id = 'cleideAuditAuditUploadStatus';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    card.appendChild(status);

    if (hasAuditBatch(tempTable)) {
      var summary = document.createElement('div');
      summary.className = 'cleide-audit-audit-file-summary';
      var batch = tempTable.audit_batch;
      var summaryTitle = document.createElement('p');
      summaryTitle.className = 'cleide-audit-audit-file-summary-title';
      summaryTitle.textContent = 'Arquivo recebido para auditoria';
      summary.appendChild(summaryTitle);
      appendDetailRow(summary, 'Arquivo', batch.source_file_name || '—');
      appendDetailRow(summary, 'Linhas', batch.row_count != null ? String(batch.row_count) : '—');
      appendDetailRow(summary, 'Limite configurado', batch.max_rows != null ? String(batch.max_rows) : '—');
      appendDetailRow(summary, 'Status', 'Arquivo recebido para auditoria');
      card.appendChild(summary);

      var runActions = document.createElement('div');
      runActions.className = 'cleide-audit-run-actions';
      var runBtn = document.createElement('button');
      runBtn.type = 'button';
      runBtn.className = 'cleide-audit-run-btn';
      runBtn.id = 'cleideAuditRunButton';
      runBtn.textContent = batch.summary ? 'Processar auditoria novamente' : 'Processar auditoria';
      runBtn.disabled = auditRunInFlight;
      runBtn.addEventListener('click', function () {
        runAuditProcessing();
      });
      runActions.appendChild(runBtn);
      card.appendChild(runActions);

      var runStatus = document.createElement('p');
      runStatus.className = 'cleide-audit-run-status';
      runStatus.id = 'cleideAuditRunStatus';
      runStatus.setAttribute('role', 'status');
      runStatus.setAttribute('aria-live', 'polite');
      card.appendChild(runStatus);

      renderAuditRunSummary(card, batch.summary);
      renderAuditRunResults(card, batch.results);
    }

    fileInput.addEventListener('change', function () {
      var file = fileInput.files && fileInput.files[0];
      if (file) {
        setAuditUploadFileName(file.name);
      } else {
        setAuditUploadFileName('');
      }
      fileInput.value = '';
      if (!file) return;
      uploadAuditFile(file);
    });

    section.appendChild(card);
    container.appendChild(section);
  }

  function setAuditUploadFileName(name) {
    var el = byId('cleideAuditAuditUploadFileName');
    if (el) el.textContent = name || 'Nenhum arquivo selecionado';
  }

  function setAuditUploadStatus(message, state) {
    var el = byId('cleideAuditAuditUploadStatus');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'cleide-audit-audit-file-status';
    if (state === 'loading') {
      el.classList.add('is-loading');
    } else if (state === 'success') {
      el.classList.add('is-success');
    } else if (state === 'error') {
      el.classList.add('is-error');
    }
  }

  function setAuditRunStatus(message, state) {
    var el = byId('cleideAuditRunStatus');
    if (!el) return;
    el.textContent = message || '';
    el.className = 'cleide-audit-run-status';
    if (state === 'loading') {
      el.classList.add('is-loading');
    } else if (state === 'success') {
      el.classList.add('is-success');
    } else if (state === 'error') {
      el.classList.add('is-error');
    }
  }

  function uploadAuditFile(file) {
    if (!file || auditUploadInFlight) return;
    auditUploadInFlight = true;
    setAuditUploadStatus('Enviando arquivo...', 'loading');

    var formData = new FormData();
    formData.append('file', file);

    fetch(API_AUDIT_UPLOAD, {
      method: 'POST',
      credentials: 'same-origin',
      body: formData
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setAuditUploadStatus(
            (res.data && res.data.message) || 'Não foi possível enviar o arquivo. Verifique o formato.',
            'error'
          );
          return;
        }
        if (res.data.temp_table) {
          currentTempTable = res.data.temp_table;
        }
        if (hasAuditBatch(currentTempTable)) {
          auditFileStepActive = true;
          tempTableModalActiveTab = 'audit';
          setAuditUploadStatus('Arquivo recebido para auditoria.', 'success');
          renderTempTableModalContent(currentTempTable);
          updateTempTableModalFooter();
        } else {
          setAuditUploadStatus('Não foi possível registrar o arquivo auditado.', 'error');
        }
      })
      .catch(function () {
        setAuditUploadStatus('Não foi possível enviar o arquivo. Verifique sua conexão e tente novamente.', 'error');
      })
      .finally(function () {
        auditUploadInFlight = false;
      });
  }

  function ensureCoverageTableShell(tempTable) {
    if (!tempTable.coverage_table || typeof tempTable.coverage_table !== 'object') {
      tempTable.coverage_table = {
        status: 'needs_review',
        columns: ['UF destino', 'Cidade destino', 'Região de frete'],
        rows: [],
        validation_warnings: [],
        notes: ''
      };
    }
    if (!Array.isArray(tempTable.coverage_table.rows)) {
      tempTable.coverage_table.rows = [];
    }
  }

  function formatAuditMoney(value) {
    if (!hasFieldValue(value)) return '—';
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function auditStatusLabel(status) {
    var key = String(status || '');
    var labels = {
      ok: 'ok',
      divergent: 'divergente',
      missing_coverage_mapping: 'sem mapeamento',
      ambiguous_coverage_mapping: 'mapeamento ambíguo',
      missing_freight_rule: 'sem regra',
      invalid_weight: 'peso inválido',
      invalid_charged_freight: 'frete cobrado inválido',
      unsupported_pricing_model: 'modelo não suportado'
    };
    return labels[key] || key || '—';
  }

  function appendAuditSummaryItem(container, label, value) {
    var item = document.createElement('div');
    item.className = 'cleide-audit-run-summary-item';
    var labelEl = document.createElement('span');
    labelEl.className = 'cleide-audit-run-summary-label';
    labelEl.textContent = label;
    var valueEl = document.createElement('strong');
    valueEl.className = 'cleide-audit-run-summary-value';
    valueEl.textContent = String(value == null ? 0 : value);
    item.appendChild(labelEl);
    item.appendChild(valueEl);
    container.appendChild(item);
  }

  function renderAuditRunSummary(container, summary) {
    if (!summary || typeof summary !== 'object') return;
    var block = document.createElement('div');
    block.className = 'cleide-audit-run-summary';
    var title = document.createElement('p');
    title.className = 'cleide-audit-run-summary-title';
    title.textContent = 'Resumo da auditoria';
    block.appendChild(title);
    appendAuditSummaryItem(block, 'Total de linhas', summary.total_rows);
    appendAuditSummaryItem(block, 'Ok', summary.ok);
    appendAuditSummaryItem(block, 'Divergentes', summary.divergent);
    appendAuditSummaryItem(block, 'Sem mapeamento', (summary.missing_coverage_mapping || 0) + (summary.ambiguous_coverage_mapping || 0));
    appendAuditSummaryItem(block, 'Sem regra', summary.missing_freight_rule);
    appendAuditSummaryItem(block, 'Inválidas', summary.invalid_rows);
    container.appendChild(block);
  }

  function renderAuditRunResults(container, results) {
    var rows = Array.isArray(results) ? results : [];
    if (!rows.length) return;
    var section = document.createElement('div');
    section.className = 'cleide-audit-run-results';
    var title = document.createElement('p');
    title.className = 'cleide-audit-run-results-title';
    title.textContent = 'Resultados por linha';
    section.appendChild(title);

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll cleide-audit-run-results-scroll';
    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table cleide-audit-run-results-table';
    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['Linha', 'Documento', 'UF', 'Cidade', 'Região', 'Peso', 'Cobrado', 'Esperado', 'Diferença', 'Status'].forEach(function (label) {
      appendTableCell(headerRow, label, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.slice(0, 200).forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      var tr = document.createElement('tr');
      appendTableCell(tr, row.row_index, false, false);
      appendTableCell(tr, row.numero_documento, false, true);
      appendTableCell(tr, row.destination_uf, false, true);
      appendTableCell(tr, row.destination_city, false, true);
      appendTableCell(tr, row.freight_region, false, true);
      appendTableCell(tr, row.audited_weight, false, true);
      appendTableCell(tr, formatAuditMoney(row.charged_freight), false, true);
      appendTableCell(tr, formatAuditMoney(row.expected_freight), false, true);
      appendTableCell(tr, formatAuditMoney(row.divergence_value), false, true);
      appendTableCell(tr, auditStatusLabel(row.status), false, false);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    section.appendChild(scrollWrap);
    container.appendChild(section);
  }

  function runAuditProcessing() {
    if (!hasAuditBatch(currentTempTable) || auditRunInFlight) return;
    auditRunInFlight = true;
    setAuditRunStatus('Processando auditoria...', 'loading');
    updateTempTableModalFooter();

    fetch(API_AUDIT_RUN, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setAuditRunStatus(
            (res.data && res.data.message) || 'Não foi possível processar a auditoria.',
            'error'
          );
          return;
        }
        if (res.data.temp_table) {
          currentTempTable = res.data.temp_table;
        }
        auditFileStepActive = true;
        tempTableModalActiveTab = 'audit';
        setAuditRunStatus('Auditoria processada.', 'success');
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
      })
      .catch(function () {
        setAuditRunStatus('Não foi possível processar a auditoria. Verifique sua conexão e tente novamente.', 'error');
      })
      .finally(function () {
        auditRunInFlight = false;
        updateTempTableModalFooter();
      });
  }

  function renderCoverageUploadHint(container) {
    var hint = document.createElement('p');
    hint.className = 'cleide-audit-temp-table-modal-empty';
    hint.textContent = 'Faça upload do arquivo complementar CSV ou XLSX para carregar UF, cidade e região de frete.';
    container.appendChild(hint);
  }

  function renderCoverageDecisionCard(container) {
    var card = document.createElement('div');
    card.className = 'cleide-audit-coverage-prompt-card';

    var title = document.createElement('span');
    title.className = 'cleide-audit-coverage-prompt-title';
    title.textContent = 'Cidades atendidas';
    card.appendChild(title);

    var description = document.createElement('p');
    description.className = 'cleide-audit-coverage-prompt-description';
    description.textContent = 'Deseja informar a relação de cidades atendidas?';
    card.appendChild(description);

    var support = document.createElement('p');
    support.className = 'cleide-audit-coverage-prompt-support';
    support.textContent = 'Use essa etapa quando a tabela de frete trabalhar com regiões, praças, rotas ou itinerários.';
    card.appendChild(support);

    var actions = document.createElement('div');
    actions.className = 'cleide-audit-coverage-prompt-actions';

    var yesBtn = document.createElement('button');
    yesBtn.type = 'button';
    yesBtn.className = 'cleide-audit-coverage-prompt-yes cleide-audit-coverage-prompt-btn cleide-audit-coverage-prompt-btn-primary';
    yesBtn.textContent = 'Sim, enviar planilha';
    yesBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(true);
    });

    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'cleide-audit-coverage-prompt-no cleide-audit-coverage-prompt-btn cleide-audit-coverage-prompt-btn-secondary';
    noBtn.textContent = 'Agora não';
    noBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(false);
    });

    actions.appendChild(yesBtn);
    actions.appendChild(noBtn);
    card.appendChild(actions);
    container.appendChild(card);
  }

  function renderCoverageSkippedState(container) {
    var info = document.createElement('p');
    info.className = 'cleide-audit-temp-table-modal-empty cleide-audit-coverage-skipped-state';
    info.textContent = 'Etapa ignorada. Você poderá iniciar a auditoria, mas linhas que dependam de regiões sem cidade podem ficar sem mapeamento.';
    container.appendChild(info);
  }

  function renderEditableCoverageTable(container, tempTable) {
    ensureCoverageTableShell(tempTable);
    var rows = tempTable.coverage_table.rows;

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    COVERAGE_TABLE_HEADERS.forEach(function (field) {
      appendTableCell(headerRow, field.label, true, false);
    });
    var actionsHeader = document.createElement('th');
    actionsHeader.scope = 'col';
    actionsHeader.className = 'cleide-audit-temp-table-modal-actions-col';
    actionsHeader.textContent = 'Ações';
    headerRow.appendChild(actionsHeader);
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (row, rowIndex) {
      if (!row || typeof row !== 'object') return;
      var tr = document.createElement('tr');
      COVERAGE_TABLE_HEADERS.forEach(function (field) {
        appendEditableCell(tr, row[field.key], function (newValue) {
          if (!currentTempTable.coverage_table.rows[rowIndex]) return;
          currentTempTable.coverage_table.rows[rowIndex][field.key] = newValue;
        });
      });
      appendRowDeleteCell(tr, function () {
        if (!currentTempTable.coverage_table || !Array.isArray(currentTempTable.coverage_table.rows)) return;
        currentTempTable.coverage_table.rows.splice(rowIndex, 1);
        renderTempTableModalContent(currentTempTable);
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);

    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-sm cleide-audit-temp-table-modal-add-btn';
    addBtn.textContent = 'Adicionar linha';
    addBtn.addEventListener('click', function () {
      ensureCoverageTableShell(currentTempTable);
      currentTempTable.coverage_table.rows.push({
        destination_uf: '',
        destination_city: '',
        freight_region: '',
        notes: ''
      });
      renderTempTableModalContent(currentTempTable);
    });
    container.appendChild(addBtn);
  }

  function renderReadonlyCoverageTable(container, tempTable) {
    ensureCoverageTableShell(tempTable);
    var rows = tempTable.coverage_table.rows;
    if (!rows.length) {
      renderCoverageUploadHint(container);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'cleide-audit-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'cleide-audit-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    COVERAGE_TABLE_HEADERS.forEach(function (field) {
      appendTableCell(headerRow, field.label, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (row) {
      if (!row || typeof row !== 'object') return;
      var tr = document.createElement('tr');
      COVERAGE_TABLE_HEADERS.forEach(function (field) {
        appendTableCell(tr, row[field.key], false, field.key === 'notes');
      });
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);

    var warnings = tempTable.coverage_table.validation_warnings;
    if (Array.isArray(warnings) && warnings.length) {
      appendSimpleListSection(container, 'Avisos de validação', warnings);
    }
  }

  function renderCoverageTabContent(container, tempTable) {
    appendSectionTitle(container, 'Cidades atendidas');
    var section = document.createElement('div');
    section.className = 'cleide-audit-temp-table-modal-section cleide-audit-temp-table-modal-coverage-section';

    if (!hasCoverageRows(tempTable) && !coveragePromptAnswered) {
      renderCoverageDecisionCard(section);
    } else if (!hasCoverageRows(tempTable) && coveragePromptAccepted) {
      renderCoverageUploadCard(section, 'cleideAuditCoverageModal');
    } else if (!hasCoverageRows(tempTable) && coveragePromptAnswered && !coveragePromptAccepted) {
      renderCoverageSkippedState(section);
    } else if (tempTableEditMode && canEditCoverageTable(tempTable)) {
      renderEditableCoverageTable(section, tempTable);
    } else {
      renderReadonlyCoverageTable(section, tempTable);
    }
    container.appendChild(section);
  }

  function renderFreightTabContent(container, tempTable) {
    var meta = document.createElement('div');
    meta.className = 'cleide-audit-temp-table-modal-meta';
    appendMetaRow(meta, 'Status', tempTableStatusLabel(tempTable.status));
    var sourceDocs = Array.isArray(tempTable.source_documents) ? tempTable.source_documents : [];
    appendMetaRow(meta, 'Documento(s) de origem', sourceDocs.length ? sourceDocs.join(', ') : null);
    appendMetaRow(meta, 'Criado em', formatDateTime(tempTable.created_at));
    appendMetaRow(meta, 'Atualizado em', formatDateTime(tempTable.updated_at));
    appendMetaRow(meta, 'Expira em', formatDateTime(tempTable.expires_at));
    container.appendChild(meta);

    renderMainFreightSection(container, tempTable);
    renderAccessorialFeesSection(container, tempTable.accessorial_fees);
    renderAdditionalInfoSection(container, tempTable);
    appendSimpleListSection(container, 'Alertas de leitura', tempTable.reading_alerts);
    appendSimpleListSection(container, 'Evidências/referências', tempTable.evidence_refs);
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

    var showTabs = shouldShowCoverageTab(tempTable) || shouldShowAuditTab(tempTable);
    if (showTabs) {
      renderTempTableModalTabs(body, tempTable);
      var panel = document.createElement('div');
      panel.className = 'cleide-audit-temp-table-modal-tab-panel';
      if (tempTableModalActiveTab === 'audit') {
        renderAuditFileTabContent(panel, tempTable);
      } else if (tempTableModalActiveTab === 'coverage') {
        renderCoverageTabContent(panel, tempTable);
      } else {
        renderFreightTabContent(panel, tempTable);
      }
      body.appendChild(panel);
      return;
    }

    renderFreightTabContent(body, tempTable);
  }

  function isTempTableModalOpen() {
    var modal = byId('cleideAuditTempTableModal');
    return !!(modal && !modal.hidden);
  }

  function openTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    if (!modal) return;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    modal.hidden = false;
    document.body.classList.add('cleide-audit-temp-table-modal-open');
    var saveBtn = byId('cleideAuditTempTableModalSave');
    if (saveBtn) saveBtn.focus();
  }

  function closeTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    if (!modal || modal.hidden) return;
    modal.hidden = true;
    document.body.classList.remove('cleide-audit-temp-table-modal-open');
    resetFreightTableOpenState();
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    updateTempTableModalFooter();
    if (lastTempTableCardButton && typeof lastTempTableCardButton.focus === 'function') {
      lastTempTableCardButton.focus();
    }
  }

  function initTempTableModal() {
    var modal = byId('cleideAuditTempTableModal');
    var saveBtn = byId('cleideAuditTempTableModalSave');
    var editBtn = byId('cleideAuditTempTableModalEdit');
    var startAuditBtn = byId('cleideAuditTempTableModalStartAudit');
    var closeBtn = byId('cleideAuditTempTableModalClose');
    var cancelEditBtn = byId('cleideAuditTempTableModalCancelEdit');
    var backdrop = byId('cleideAuditTempTableModalBackdrop');
    if (!modal) return;

    if (saveBtn) {
      saveBtn.addEventListener('click', function () {
        if (tempTableModalActiveTab === 'coverage' && shouldShowCoverageTab(currentTempTable)) {
          saveCoverageTableEdit();
          return;
        }
        saveTempTableAndAdvance();
      });
    }
    if (editBtn) {
      editBtn.addEventListener('click', function () {
        enterTempTableEditMode();
      });
    }
    if (startAuditBtn) {
      startAuditBtn.addEventListener('click', function () {
        handleStartAudit();
      });
    }
    if (cancelEditBtn) {
      cancelEditBtn.addEventListener('click', function () {
        cancelTempTableEdit();
      });
    }
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
    updateTempTableModalFooter();
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
