(function () {
  'use strict';

  var API_STATUS = '/api/agente-compara/documents/status';
  var API_UPLOAD = '/api/agente-compara/documents/upload';
  var API_CLEAR = '/api/agente-compara/documents/clear';
  var API_CHAT = '/api/agente-compara/chat';
  var API_AUDIT_CHAT = '/api/agente-compara/audit-chat';
  var API_AUDIT_CHAT_UNLOCK = '/api/agente-compara/audit-chat/unlock';
  var API_TEMP_TABLE_SAVE = '/api/agente-compara/temp-table/save';
  var API_COVERAGE_UPLOAD = '/api/agente-compara/coverage/upload';
  var API_AUDIT_TEMPLATE = '/api/agente-compara/audit-template';
  var API_AUDIT_UPLOAD = '/api/agente-compara/audit/upload';
  var API_AUDIT_RUN = '/api/agente-compara/audit/run';
  var API_AUDIT_CORRECTION_PREVIEW = '/api/agente-compara/audit/correction/preview';
  var API_AUDIT_CORRECTION_APPLY = '/api/agente-compara/audit/correction/apply';
  var API_AUDIT_CORRECTION_UNDO = '/api/agente-compara/audit/correction/undo';
  var API_COMPARISON_PROCEED_TWO = '/api/agente-compara/comparison/proceed-two-tables';
  var API_COMPARISON_ADD_THIRD = '/api/agente-compara/comparison/add-third-table';
  var API_COMPARISON_SET_ACTIVE = '/api/agente-compara/comparison/set-active-table';
  var API_COMPARISON_TAXES = '/api/agente-compara/comparison/taxes';
  var API_COMPARISON_RESET = '/api/agente-compara/comparison/reset';
  var API_COMPARISON_START = '/api/agente-compara/comparison/start';
  var API_COMPARISON_CALCULATE = '/api/agente-compara/comparison/calculate';
  var API_COMPARISON_CALCULATION = '/api/agente-compara/comparison/calculation';
  var comparisonRequestGeneration = 0;
  var comparisonResetInFlight = false;
  var comparisonStartPromise = null;
  var comparisonCalculationInFlight = false;
  var comparisonCalculationState = {
    status: 'not_started',
    executionId: null,
    fingerprintShort: null,
    stale: false,
    result: null,
    error: null,
    billingStatus: null
  };
  var comparisonState = {
    comparisonId: null,
    currentStep: null,
    activeTableId: null,
    desiredTableCount: 2,
    primaryTempTableId: null,
    tables: []
  };
  // Estado visual local da revisão final (CONFIGURATION_READY). Não altera o workflow.
  var configurationReviewTab = null;
  var reviewTempTablesById = {};
  var reviewSharedTempTable = null;
  var reviewComparisonId = null;
  var reviewLoadToken = 0;
  var reviewLoadInFlightTableId = null;

  function invalidateComparisonDerivedState() {
    stopTempTablePolling();
    clearPendingFreightTableUpload();
    setCurrentTempTable(null);
    resetConfigurationReviewState();
    resetTaxStepState();
    resetCoveragePromptState();
    resetAuditFileStepState();
    lastAnnouncedTempTableStatus = null;
  }

  function syncComparisonStateFromPayload(comparison) {
    // A. comparison válido → sincronizar campos relevantes
    // B. comparison === null → limpar estado local
    // C. campo ausente/ inválido → não limpar automaticamente
    if (comparison === null) {
      if (comparisonState.comparisonId) {
        bumpComparisonRequestGeneration();
        invalidateComparisonDerivedState();
      }
      clearLocalComparisonState();
      reviewComparisonId = null;
      return;
    }
    if (!comparison || typeof comparison !== 'object') return;

    var nextComparisonId = comparison.comparison_id || null;
    var previousComparisonId = comparisonState.comparisonId || null;
    if (nextComparisonId && previousComparisonId && nextComparisonId !== previousComparisonId) {
      bumpComparisonRequestGeneration();
      invalidateComparisonDerivedState();
    } else if (nextComparisonId && !previousComparisonId) {
      bumpComparisonRequestGeneration();
    } else if (nextComparisonId && reviewComparisonId && nextComparisonId !== reviewComparisonId) {
      resetConfigurationReviewState();
    }

    if (nextComparisonId) reviewComparisonId = nextComparisonId;
    comparisonState.comparisonId = nextComparisonId;
    comparisonState.currentStep = comparison.current_step || null;
    comparisonState.activeTableId = comparison.active_table_id || null;
    comparisonState.desiredTableCount = comparison.desired_table_count || 2;
    comparisonState.tables = Array.isArray(comparison.tables) ? comparison.tables : [];
    if (Object.prototype.hasOwnProperty.call(comparison, 'primary_temp_table_id')) {
      comparisonState.primaryTempTableId =
        typeof comparison.primary_temp_table_id === 'string'
          ? comparison.primary_temp_table_id
          : null;
    }
    comparisonState.canAdvanceToCoverage = comparison.can_advance_to_coverage === true;
    if (comparison.tax_config && typeof comparison.tax_config === 'object') {
      restoreGlobalTaxConfig(comparison.tax_config);
    }
    if (Array.isArray(comparison.tax_table_ufs_preview)) {
      taxTableUfsPreview = {};
      comparison.tax_table_ufs_preview.forEach(function (item) {
        if (item && item.table_id) taxTableUfsPreview[item.table_id] = item;
      });
    }
  }

  function ensureComparisonStarted() {
    if (comparisonState.comparisonId && activeComparisonTable()) {
      return Promise.resolve({
        ok: true,
        comparison: comparisonState,
        alreadyStarted: true,
        comparisonStarted: false,
        idempotentReplay: true
      });
    }
    if (comparisonStartPromise) {
      return comparisonStartPromise;
    }
    var startGeneration = comparisonRequestGeneration;
    comparisonStartPromise = fetch(API_COMPARISON_START, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({})
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        }).catch(function () {
          return {
            status: r.status,
            data: {
              ok: false,
              message: 'Não foi possível iniciar uma nova comparação. Tente novamente.'
            }
          };
        });
      })
      .then(function (res) {
        if (startGeneration !== comparisonRequestGeneration) {
          return { ok: false, aborted: true, message: 'Comparação reiniciada. Tente novamente.' };
        }
        if (!res.data || res.data.ok !== true || !res.data.comparison) {
          return {
            ok: false,
            message: (res.data && res.data.message) ||
              'Não foi possível iniciar uma nova comparação. Tente novamente.',
            status: res.status
          };
        }
        syncComparisonStateFromPayload(res.data.comparison);
        if (!comparisonState.comparisonId || !activeComparisonTable()) {
          return {
            ok: false,
            message: 'Não foi possível iniciar uma nova comparação. Tente novamente.'
          };
        }
        return {
          ok: true,
          comparison: comparisonState,
          alreadyStarted: false,
          comparisonStarted: res.data.comparison_started === true,
          idempotentReplay: res.data.idempotent_replay === true
        };
      })
      .catch(function () {
        return {
          ok: false,
          message: 'Não foi possível iniciar uma nova comparação. Tente novamente.'
        };
      })
      .finally(function () {
        comparisonStartPromise = null;
      });
    return comparisonStartPromise;
  }

  function activeComparisonTable() {
    if (!comparisonState.tables || !comparisonState.tables.length) return null;
    var activeId = comparisonState.activeTableId;
    if (activeId) {
      var found = comparisonState.tables.find(function (t) { return t && t.table_id === activeId; });
      if (found) return found;
    }
    return comparisonState.tables[0] || null;
  }

  function comparisonIdentityQuery() {
    var active = activeComparisonTable();
    var params = [];
    if (comparisonState.comparisonId) params.push('comparison_id=' + encodeURIComponent(comparisonState.comparisonId));
    if (comparisonState.activeTableId) params.push('table_id=' + encodeURIComponent(comparisonState.activeTableId));
    else if (active && active.table_id) params.push('table_id=' + encodeURIComponent(active.table_id));
    if (active && active.slot_number) params.push('slot=' + encodeURIComponent(String(active.slot_number)));
    return params.length ? ('?' + params.join('&')) : '';
  }

  function tableCarrierDisplay(tableMeta) {
    if (!tableMeta || !tableMeta.carrier_name) return '';
    var name = String(tableMeta.carrier_name).trim();
    return name || '';
  }

  var pendingFreightTableUpload = null;
  var freightUploadPreparationInFlight = false;
  var carrierIdentifyInFlight = false;
  var carrierEditOnlyMode = false;
  var CARRIER_NAME_MAX_LENGTH = 120;

  function trimCarrierNameInput(value) {
    return String(value || '').trim();
  }

  function validateCarrierNameInput(value) {
    var name = trimCarrierNameInput(value);
    if (!name) {
      return { ok: false, message: 'Informe a transportadora.' };
    }
    if (name.length > CARRIER_NAME_MAX_LENGTH) {
      return {
        ok: false,
        message: 'O nome da transportadora deve ter no máximo ' + CARRIER_NAME_MAX_LENGTH + ' caracteres.'
      };
    }
    return { ok: true, name: name };
  }

  function isCarrierIdentificationOpen() {
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    return !!(panel && !panel.hidden);
  }

  function setCarrierIdentifyError(message) {
    var errorEl = byId('agenteComparaCarrierIdentifyError');
    if (!errorEl) return;
    if (message) {
      errorEl.textContent = String(message);
      errorEl.hidden = false;
    } else {
      errorEl.textContent = '';
      errorEl.hidden = true;
    }
  }

  function updateCarrierIdentifyContinueButton() {
    var input = byId('agenteComparaCarrierNameInput');
    var continueBtn = byId('agenteComparaCarrierIdentifyContinue');
    if (!continueBtn) return;
    var validation = validateCarrierNameInput(input ? input.value : '');
    continueBtn.disabled = !validation.ok || carrierIdentifyInFlight;
  }

  function clearPendingFreightTableUpload() {
    pendingFreightTableUpload = null;
    carrierEditOnlyMode = false;
  }

  function setUploadPreparationLoading(on) {
    var attachBtn = byId('agenteComparaAttachBtn');
    if (attachBtn) {
      attachBtn.setAttribute('aria-busy', on ? 'true' : 'false');
    }
    setStatus(on ? 'Preparando envio...' : '');
  }

  function resetFreightFileInput() {
    var fileInput = byId('agenteComparaFileInput');
    if (fileInput) fileInput.value = '';
  }

  function closeCarrierIdentificationPanel() {
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    if (panel) panel.hidden = true;
    if (body) body.hidden = false;
    if (footer) footer.hidden = false;
    setCarrierIdentifyError('');
    carrierIdentifyInFlight = false;
    updateCarrierIdentifyContinueButton();
    if (!uploadInFlight && !isTempTableModalOpen()) {
      var modal = byId('agenteComparaTempTableModal');
      if (modal && !currentTempTable) {
        modal.hidden = true;
        document.body.classList.remove('agente-compara-temp-table-modal-open');
      }
    }
  }

  function openCarrierIdentificationPanel(options) {
    options = options || {};
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    var input = byId('agenteComparaCarrierNameInput');
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    if (!panel || !input) return;
    carrierEditOnlyMode = !!options.editOnly;
    var preset = options.presetName || '';
    if (!preset && pendingFreightTableUpload && pendingFreightTableUpload.carrierName) {
      preset = pendingFreightTableUpload.carrierName;
    }
    input.value = preset;
    setCarrierIdentifyError('');
    if (!isTempTableModalOpen()) {
      var modal = byId('agenteComparaTempTableModal');
      if (modal) {
        modal.hidden = false;
        document.body.classList.add('agente-compara-temp-table-modal-open');
      }
    }
    if (body) body.hidden = true;
    if (footer) footer.hidden = true;
    panel.hidden = false;
    updateCarrierIdentifyContinueButton();
    window.setTimeout(function () {
      input.focus();
      if (typeof input.select === 'function') input.select();
    }, 0);
  }

  function pendingUploadContextStillValid(pending) {
    if (!pending) return false;
    if (pending.comparisonId !== comparisonState.comparisonId) return false;
    if (pending.tableId !== comparisonState.activeTableId) return false;
    if (pending.currentStep !== comparisonState.currentStep) return false;
    return true;
  }

  function beginPendingFreightTableUpload(file) {
    if (
      !file ||
      pendingFreightTableUpload ||
      freightUploadPreparationInFlight ||
      uploadInFlight ||
      carrierIdentifyInFlight
    ) {
      return;
    }
    if (comparisonStartPromise) return;

    freightUploadPreparationInFlight = true;
    setError('');
    setUploadPreparationLoading(true);

    ensureComparisonStarted()
      .then(function (result) {
        if (!result || result.ok !== true) {
          setError(
            (result && result.message) ||
              'Não foi possível iniciar uma nova comparação. Tente novamente.'
          );
          resetFreightFileInput();
          return;
        }
        var active = activeComparisonTable();
        if (!active || !active.table_id || !comparisonState.comparisonId) {
          setError('Não foi possível identificar a tabela ativa para upload.');
          resetFreightFileInput();
          return;
        }
        if (pendingFreightTableUpload || uploadInFlight || carrierIdentifyInFlight) {
          return;
        }
        pendingFreightTableUpload = {
          file: file,
          comparisonId: comparisonState.comparisonId,
          tableId: active.table_id,
          slot: active.slot_number,
          currentStep: comparisonState.currentStep,
          carrierName: tableCarrierDisplay(active)
        };
        carrierEditOnlyMode = false;
        openCarrierIdentificationPanel({ presetName: pendingFreightTableUpload.carrierName });
      })
      .catch(function () {
        setError('Não foi possível preparar o envio da tabela. Tente novamente.');
        resetFreightFileInput();
      })
      .finally(function () {
        freightUploadPreparationInFlight = false;
        setUploadPreparationLoading(false);
      });
  }

  function cancelCarrierIdentification() {
    closeCarrierIdentificationPanel();
    clearPendingFreightTableUpload();
    freightUploadPreparationInFlight = false;
    setUploadPreparationLoading(false);
    resetFreightFileInput();
  }

  function openCarrierNameEdit() {
    var active = activeComparisonTable();
    if (!active || active.confirmed) {
      setTempTableModalError('Não é possível alterar a transportadora desta tabela.');
      return;
    }
    carrierEditOnlyMode = true;
    openCarrierIdentificationPanel({ editOnly: true, presetName: tableCarrierDisplay(active) });
  }

  function renderModalCarrierEditLink(subtitleEl, tableMeta) {
    if (!subtitleEl || !tableMeta || tableMeta.confirmed) return;
    var existing = byId('agenteComparaModalCarrierEditBtn');
    if (existing) existing.remove();
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agente-compara-modal-carrier-edit-btn';
    btn.id = 'agenteComparaModalCarrierEditBtn';
    btn.textContent = 'Editar transportadora';
    btn.addEventListener('click', function (event) {
      event.preventDefault();
      openCarrierNameEdit();
    });
    subtitleEl.appendChild(document.createElement('br'));
    subtitleEl.appendChild(btn);
  }

  function updateCarrierNameOnly(name) {
    var active = activeComparisonTable();
    if (!active || !currentTempTable || !currentTempTable.temp_table_id) {
      return Promise.resolve(null);
    }
    return fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        temp_table_id: currentTempTable.temp_table_id,
        comparison_id: comparisonState.comparisonId,
        table_id: active.table_id,
        slot: active.slot_number,
        carrier_name: name,
        review_action: 'update_carrier_name',
        edit_target: {}
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setCarrierIdentifyError((res.data && res.data.message) || 'Não foi possível atualizar a transportadora.');
          return null;
        }
        if (res.data.comparison) syncComparisonStateFromPayload(res.data.comparison);
        else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        if (isComparisonWizardFlowActive()) {
          renderComparisonWizardModal();
        } else {
          renderTempTableModalContent(currentTempTable);
          updateTempTableModalFooter();
        }
        return res.data;
      })
      .catch(function () {
        setCarrierIdentifyError('Não foi possível atualizar a transportadora. Tente novamente.');
        return null;
      });
  }

  function confirmCarrierIdentification() {
    if (carrierIdentifyInFlight) return;
    var input = byId('agenteComparaCarrierNameInput');
    var validation = validateCarrierNameInput(input ? input.value : '');
    if (!validation.ok) {
      setCarrierIdentifyError(validation.message);
      updateCarrierIdentifyContinueButton();
      return;
    }
    setCarrierIdentifyError('');
    carrierIdentifyInFlight = true;
    updateCarrierIdentifyContinueButton();

    if (carrierEditOnlyMode) {
      updateCarrierNameOnly(validation.name)
        .finally(function () {
          carrierIdentifyInFlight = false;
          closeCarrierIdentificationPanel();
          carrierEditOnlyMode = false;
          updateCarrierIdentifyContinueButton();
        });
      return;
    }

    if (!pendingFreightTableUpload || !pendingFreightTableUpload.file) {
      carrierIdentifyInFlight = false;
      updateCarrierIdentifyContinueButton();
      cancelCarrierIdentification();
      return;
    }
    if (!pendingUploadContextStillValid(pendingFreightTableUpload)) {
      carrierIdentifyInFlight = false;
      updateCarrierIdentifyContinueButton();
      cancelCarrierIdentification();
      setError('A etapa da comparação foi alterada. Selecione o arquivo novamente.');
      return;
    }

    var pending = pendingFreightTableUpload;
    pending.carrierName = validation.name;
    var file = pending.file;
    clearPendingFreightTableUpload();
    closeCarrierIdentificationPanel();

    uploadDocument(file, validation.name, {
      comparisonId: pending.comparisonId,
      tableId: pending.tableId,
      slot: pending.slot
    }).finally(function () {
      carrierIdentifyInFlight = false;
      updateCarrierIdentifyContinueButton();
      resetFreightFileInput();
    });
  }

  function initCarrierIdentificationPanel() {
    var input = byId('agenteComparaCarrierNameInput');
    var cancelBtn = byId('agenteComparaCarrierIdentifyCancel');
    var continueBtn = byId('agenteComparaCarrierIdentifyContinue');
    if (input && !input.dataset.agenteComparaCarrierBound) {
      input.dataset.agenteComparaCarrierBound = '1';
      input.addEventListener('input', updateCarrierIdentifyContinueButton);
      input.addEventListener('keydown', function (event) {
        if (event.key === 'Enter') {
          event.preventDefault();
          confirmCarrierIdentification();
        }
      });
    }
    if (cancelBtn && !cancelBtn.dataset.agenteComparaCarrierBound) {
      cancelBtn.dataset.agenteComparaCarrierBound = '1';
      cancelBtn.addEventListener('click', function (event) {
        event.preventDefault();
        cancelCarrierIdentification();
      });
    }
    if (continueBtn && !continueBtn.dataset.agenteComparaCarrierBound) {
      continueBtn.dataset.agenteComparaCarrierBound = '1';
      continueBtn.addEventListener('click', function (event) {
        event.preventDefault();
        if (carrierIdentifyInFlight) return;
        confirmCarrierIdentification();
      });
    }
  }

  function formatTableStatusShort(status, confirmed) {
    if (confirmed) return 'Confirmada';
    var key = String(status || 'empty').toLowerCase();
    if (key === 'empty') return 'Aguardando envio';
    if (key === 'locked') return 'Bloqueada';
    if (key === 'processing') return 'Processando';
    if (key === 'needs_review') return 'Revisar';
    if (key === 'confirmed') return 'Confirmada';
    if (key === 'failed') return 'Erro';
    return 'Aguardando envio';
  }

  function formatTableStatusHint(status, confirmed) {
    if (confirmed) return 'Tabela confirmada.';
    var key = String(status || 'empty').toLowerCase();
    if (key === 'empty') return 'Aguardando envio do arquivo.';
    if (key === 'locked') return 'Bloqueada até concluir a tabela anterior.';
    if (key === 'processing') return 'Processando o arquivo enviado.';
    if (key === 'needs_review') return 'Pronta para revisão.';
    if (key === 'confirmed') return 'Tabela confirmada.';
    if (key === 'failed') return 'Erro no processamento.';
    return 'Aguardando envio do arquivo.';
  }

  function escapeTableTabAttr(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/"/g, '&quot;')
      .replace(/</g, '&lt;');
  }

  function escapeTableTabText(text) {
    return String(text || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  function isComparisonWizardStep(step) {
    step = step || comparisonState.currentStep || '';
    return step === 'PREPARE_TABLE_1' || step === 'PREPARE_TABLE_2' ||
      step === 'ASK_TABLE_3' || step === 'PREPARE_TABLE_3';
  }

  function isComparisonCommonParamsStep(step) {
    step = step || comparisonState.currentStep || '';
    return step === 'TAXES' || step === 'COVERAGE' ||
      step === 'CALCULATION_FILE' || step === 'CONFIGURATION_READY';
  }

  function isComparisonConfigurationReady() {
    return (comparisonState.currentStep || '') === 'CONFIGURATION_READY';
  }

  function isComparisonCalculationStep() {
    var step = comparisonState.currentStep || '';
    return (
      step === 'CALCULATION_RUNNING' ||
      step === 'CALCULATION_READY' ||
      step === 'CALCULATION_FAILED'
    );
  }

  function isComparisonPostConfigStep() {
    return isComparisonConfigurationReady() || isComparisonCalculationStep();
  }

  function isComparisonConfigurationFlow() {
    return isComparisonCommonParamsStep() || isComparisonCalculationStep();
  }

  function isComparisonWizardComplete() {
    return false;
  }

  function isComparisonWizardFlowActive() {
    return !!comparisonState.comparisonId && isComparisonWizardStep();
  }

  function isComparisonWizardEngaged() {
    return comparisonWizardEngaged;
  }

  function markComparisonWizardEngaged() {
    comparisonWizardEngaged = true;
  }

  function resolveComparisonWizardView() {
    var step = comparisonState.currentStep || '';
    if (step === 'ASK_TABLE_3') return 'ask_table_3';
    if (!isComparisonWizardStep(step)) return null;

    var active = activeComparisonTable();
    var tempTable = currentTempTable;
    if (tempTable && tempTable.temp_table_id && tempTableMatchesActiveSlot(tempTable)) {
      var tempStatus = String(tempTable.status || '').toLowerCase();
      if (tempStatus === 'processing') return 'processing';
      if (tempStatus === 'needs_review' && active && !active.confirmed) return 'review';
    }
    if (active) {
      var slotStatus = String(active.status || 'empty').toLowerCase();
      if (slotStatus === 'processing') return 'processing';
    }
    return 'upload';
  }

  function confirmedComparisonTables() {
    return (comparisonState.tables || []).filter(function (tableMeta) {
      return tableMeta && tableMeta.confirmed;
    });
  }

  function confirmedComparisonTablesForReview() {
    return confirmedComparisonTables().slice().sort(function (a, b) {
      return (Number(a.slot_number) || 0) - (Number(b.slot_number) || 0);
    });
  }

  function reviewCarrierLabel(tableMeta) {
    var carrier = tableCarrierDisplay(tableMeta);
    if (carrier) return carrier;
    var slot = Number(tableMeta && tableMeta.slot_number) || 0;
    return slot > 0 ? ('Transportadora ' + slot) : 'Transportadora';
  }

  function reviewTableTabId(tableId) {
    return 'table:' + String(tableId || '');
  }

  function parseReviewTableTabId(tabId) {
    var raw = String(tabId || '');
    if (raw.indexOf('table:') !== 0) return null;
    var tableId = raw.slice('table:'.length);
    return tableId || null;
  }

  function resetConfigurationReviewState() {
    configurationReviewTab = null;
    reviewTempTablesById = {};
    reviewSharedTempTable = null;
    reviewLoadToken += 1;
    reviewLoadInFlightTableId = null;
  }

  function bumpComparisonRequestGeneration() {
    comparisonRequestGeneration += 1;
    return comparisonRequestGeneration;
  }

  function isStaleComparisonRequest(generation, comparisonId) {
    if (generation !== comparisonRequestGeneration) return true;
    var activeId = comparisonState.comparisonId || null;
    if (!activeId) return true;
    if (comparisonId && comparisonId !== activeId) return true;
    return false;
  }

  function clearLocalComparisonState() {
    comparisonState.comparisonId = null;
    comparisonState.currentStep = null;
    comparisonState.activeTableId = null;
    comparisonState.desiredTableCount = 2;
    comparisonState.primaryTempTableId = null;
    comparisonState.tables = [];
    comparisonState.canAdvanceToCoverage = false;
  }

  function hideTempTableModalShell() {
    var modal = byId('agenteComparaTempTableModal');
    if (modal) modal.hidden = true;
    document.body.classList.remove('agente-compara-temp-table-modal-open');
    document.body.classList.remove('agente-compara-temp-table-modal-editing');
  }

  function resetAgenteComparaFrontendState() {
    bumpComparisonRequestGeneration();
    comparisonStartPromise = null;
    reviewLoadToken += 1;
    stopTempTablePolling();
    clearPendingFreightTableUpload();
    freightUploadPreparationInFlight = false;
    setUploadPreparationLoading(false);
    resetFreightFileInput();
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    tempTableSaveInFlight = false;
    tempTableModalActiveTab = 'freight';
    setCurrentTempTable(null);
    closeCarrierIdentificationPanel();
    hideTempTableModalShell();
    clearLocalComparisonState();
    resetConfigurationReviewState();
    resetTaxStepState();
    resetCoveragePromptState();
    resetAuditFileStepState();
    currentCalculationBases = [];
    lastAnnouncedTempTableStatus = null;
    comparisonWizardEngaged = false;
    comparisonWizardModalSuppressed = false;
    renderDocuments([], null);
    setStatus('');
    setError('');
    updateClearButton(0);
  }

  function cacheReviewTempTableIfOwned(tempTable) {
    if (!tempTable || !tempTable.table_id) return;
    var meta = findConfirmedReviewTable(tempTable.table_id);
    if (!meta) return;
    if (meta.temp_table_id && tempTable.temp_table_id &&
        meta.temp_table_id !== tempTable.temp_table_id) {
      return;
    }
    reviewTempTablesById[tempTable.table_id] = tempTable;
  }

  function captureReviewSharedTempTable(tempTable) {
    if (!tempTable || !tempTable.temp_table_id) return;
    cacheReviewTempTableIfOwned(tempTable);
    // coverage_table e audit_batch ficam no record do primary_temp_table_id (recurso global da comparação).
    var primaryId = comparisonState.primaryTempTableId;
    if (primaryId && tempTable.temp_table_id === primaryId) {
      reviewSharedTempTable = tempTable;
      return;
    }
    if (!reviewSharedTempTable) {
      reviewSharedTempTable = tempTable;
      return;
    }
    if (tempTable.coverage_table || tempTable.audit_batch) {
      reviewSharedTempTable = tempTable;
    }
  }

  function defaultConfigurationReviewTab() {
    var confirmed = confirmedComparisonTablesForReview();
    if (confirmed.length && confirmed[0].table_id) {
      return reviewTableTabId(confirmed[0].table_id);
    }
    // Estado inconsistente: sem transportadora confirmada.
    return 'comparison_file';
  }

  function ensureConfigurationReviewDefaults(options) {
    options = options || {};
    if (!isComparisonConfigurationReady()) return;
    captureReviewSharedTempTable(currentTempTable);
    // Entrada/reabertura/refresh completo: 1ª transportadora.
    // Atualização soft com modal aberto: preserva a aba visual local.
    if (options.forceFirstCarrier || !configurationReviewTab) {
      configurationReviewTab = defaultConfigurationReviewTab();
    }
    tempTableModalActiveTab = 'configuration_review';
  }

  function prepareConfigurationReviewRender() {
    if (!isComparisonConfigurationReady()) return;
    captureReviewSharedTempTable(currentTempTable);
    if (!configurationReviewTab) {
      configurationReviewTab = defaultConfigurationReviewTab();
    }
    tempTableModalActiveTab = 'configuration_review';
  }

  function tempTableMatchesActiveSlot(tempTable) {
    if (!tempTable || !tempTable.temp_table_id) return false;
    var active = activeComparisonTable();
    if (!active) return true;
    if (tempTable.table_id && active.table_id && tempTable.table_id !== active.table_id) return false;
    if (tempTable.slot_number != null && active.slot_number != null &&
        Number(tempTable.slot_number) !== Number(active.slot_number)) return false;
    return true;
  }

  function wizardUploadCopy(slotNumber) {
    if (slotNumber === 2) {
      return {
        title: 'Prepare a segunda tabela de frete',
        text: 'A primeira tabela foi confirmada. Envie agora a segunda tabela obrigatória.',
        button: 'Enviar segunda tabela'
      };
    }
    if (slotNumber === 3) {
      return {
        title: 'Prepare a terceira tabela de frete',
        text: 'Envie a terceira tabela de frete, opcional nesta comparação.',
        button: 'Enviar terceira tabela'
      };
    }
    return {
      title: 'Prepare a primeira tabela de frete',
      text: 'Envie a tabela de frete da primeira transportadora para iniciar a comparação.',
      button: 'Enviar tabela de frete'
    };
  }

  function setComparisonWizardModalHeader(view) {
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    if (!titleEl || !subtitleEl) return;
    var active = activeComparisonTable();
    var slotNumber = active ? Number(active.slot_number) || 1 : 1;
    if (view === 'upload') {
      var copy = wizardUploadCopy(slotNumber);
      titleEl.textContent = copy.title;
      subtitleEl.textContent = copy.text;
      return;
    }
    if (view === 'processing') {
      titleEl.textContent = 'Processando tabela de frete';
      var processingCarrier = tableCarrierDisplay(active);
      subtitleEl.textContent = processingCarrier
        ? ('Transportadora: ' + processingCarrier + '. Aguarde enquanto extraímos os dados da tabela enviada.')
        : 'Aguarde enquanto extraímos os dados da tabela enviada.';
      return;
    }
    if (view === 'review') {
      var slotReviewLabels = { 1: 'primeira', 2: 'segunda', 3: 'terceira' };
      var slotReviewLabel = slotReviewLabels[slotNumber] || 'primeira';
      titleEl.textContent = 'Revisão da ' + slotReviewLabel + ' tabela de frete';
      var reviewCarrier = tableCarrierDisplay(active);
      subtitleEl.textContent = '';
      if (reviewCarrier) {
        subtitleEl.appendChild(document.createTextNode('Transportadora: ' + reviewCarrier));
      } else {
        subtitleEl.textContent = 'Revise os dados extraídos antes de continuar.';
      }
      renderModalCarrierEditLink(subtitleEl, active);
      return;
    }
    if (view === 'ask_table_3') {
      titleEl.textContent = 'Tabelas obrigatórias preparadas';
      subtitleEl.textContent = 'Deseja adicionar uma terceira tabela de frete ou concluir a preparação com duas tabelas?';
    }
  }

  function setComparisonCommonParamsModalHeader() {
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    if (!titleEl || !subtitleEl) return;
    var step = comparisonState.currentStep || '';
    if (step === 'TAXES') {
      titleEl.textContent = 'Impostos do cenário';
      subtitleEl.textContent = 'Configure os impostos de cada transportadora. A origem e o ISS são comuns a todo o cenário.';
      return;
    }
    if (step === 'COVERAGE') {
      titleEl.textContent = 'Cidades atendidas';
      subtitleEl.textContent = 'Configure uma única relação de cidades atendidas para o cenário comparativo.';
      return;
    }
    if (step === 'CALCULATION_FILE') {
      titleEl.textContent = 'Arquivo para Comparação';
      subtitleEl.textContent = 'Envie o arquivo operacional com o volume real faturado.';
      return;
    }
    if (step === 'CONFIGURATION_READY') {
      titleEl.textContent = 'Configuração concluída';
      subtitleEl.textContent = 'As tabelas de frete, os parâmetros e o arquivo operacional foram preparados. O cálculo comparativo será realizado na próxima etapa.';
    }
  }

  function activateComparisonCommonParamsStep(step) {
    step = step || comparisonState.currentStep || '';
    if (step === 'TAXES') {
      taxStepActive = true;
      tempTableModalActiveTab = 'taxes';
      initGlobalTaxConfigFromState();
    } else if (step === 'COVERAGE') {
      coverageStepActive = true;
      tempTableModalActiveTab = 'coverage';
    } else if (step === 'CALCULATION_FILE') {
      auditFileStepActive = true;
      tempTableModalActiveTab = 'audit';
    } else if (step === 'CONFIGURATION_READY') {
      taxStepActive = true;
      coverageStepActive = true;
      auditFileStepActive = true;
      initGlobalTaxConfigFromState();
      // Na primeira entrada (aba ainda nula) força a 1ª transportadora; soft status preserva aba.
      ensureConfigurationReviewDefaults({
        forceFirstCarrier: !configurationReviewTab || !isTempTableModalOpen()
      });
      var initialTableId = parseReviewTableTabId(configurationReviewTab);
      if (initialTableId) {
        selectConfigurationReviewTab(configurationReviewTab, { preferCache: true });
        comparisonWizardModalSuppressed = false;
        setComparisonCommonParamsModalHeader();
        updateTempTableModalFooter();
        if (!isTempTableModalOpen()) {
          markComparisonWizardEngaged();
          var readyModal = byId('agenteComparaTempTableModal');
          if (readyModal) {
            readyModal.hidden = false;
            document.body.classList.add('agente-compara-temp-table-modal-open');
          }
        }
        return;
      }
      tempTableModalActiveTab = 'configuration_review';
    }
    comparisonWizardModalSuppressed = false;
    setComparisonCommonParamsModalHeader();
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    if (!isTempTableModalOpen()) {
      markComparisonWizardEngaged();
      var modal = byId('agenteComparaTempTableModal');
      if (modal) {
        modal.hidden = false;
        document.body.classList.add('agente-compara-temp-table-modal-open');
      }
    }
  }

  function appendWizardConfirmedSummary(container) {
    var confirmed = confirmedComparisonTables();
    if (!confirmed.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'agente-compara-wizard-summary';
    confirmed.forEach(function (tableMeta) {
      var row = document.createElement('div');
      row.className = 'agente-compara-wizard-summary-row';
      row.textContent = 'Tabela ' + tableMeta.slot_number + ' — Confirmada';
      wrap.appendChild(row);
      var carrier = tableCarrierDisplay(tableMeta);
      if (carrier) {
        var carrierEl = document.createElement('div');
        carrierEl.className = 'agente-compara-wizard-summary-carrier';
        carrierEl.textContent = carrier;
        wrap.appendChild(carrierEl);
      }
    });
    container.appendChild(wrap);
  }

  function bindWizardPrimaryButton(button, handler) {
    if (!button) return;
    button.addEventListener('click', function (event) {
      event.preventDefault();
      handler();
    });
  }

  function triggerComparisonWizardFileInput() {
    if (
      uploadInFlight ||
      freightUploadPreparationInFlight ||
      pendingFreightTableUpload ||
      isCarrierIdentificationOpen()
    ) {
      return;
    }
    if (comparisonStartPromise) return;
    // Wizard só opera com comparison já ativa; não chama start novamente.
    if (!comparisonState.comparisonId || !activeComparisonTable()) return;
    var fileInput = byId('agenteComparaFileInput');
    if (!fileInput) return;
    fileInput.click();
  }

  function renderComparisonWizardUploadBody(body) {
    var panel = document.createElement('div');
    panel.className = 'agente-compara-wizard-panel';
    appendWizardConfirmedSummary(panel);
    var active = activeComparisonTable();
    var slotNumber = active ? Number(active.slot_number) || 1 : 1;
    var copy = wizardUploadCopy(slotNumber);
    var uploadBtn = document.createElement('button');
    uploadBtn.type = 'button';
    uploadBtn.className = 'btn btn-sm btn-primary agente-compara-wizard-upload-btn';
    uploadBtn.id = 'agenteComparaWizardUploadBtn';
    uploadBtn.textContent = copy.button;
    bindWizardPrimaryButton(uploadBtn, triggerComparisonWizardFileInput);
    panel.appendChild(uploadBtn);
    body.appendChild(panel);
  }

  function renderComparisonWizardAskTable3Body(body) {
    var panel = document.createElement('div');
    panel.className = 'agente-compara-wizard-panel';
    appendWizardConfirmedSummary(panel);
    var actions = document.createElement('div');
    actions.className = 'agente-compara-wizard-actions';
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-sm btn-primary';
    addBtn.id = 'agenteComparaWizardAddThirdBtn';
    addBtn.textContent = 'Adicionar terceira tabela';
    bindWizardPrimaryButton(addBtn, chooseAddThirdTable);
    var proceedBtn = document.createElement('button');
    proceedBtn.type = 'button';
    proceedBtn.className = 'btn btn-sm btn-outline-primary';
    proceedBtn.id = 'agenteComparaWizardProceedTwoBtn';
    proceedBtn.textContent = 'Concluir preparação com duas tabelas';
    bindWizardPrimaryButton(proceedBtn, chooseProceedWithTwoTables);
    actions.appendChild(addBtn);
    actions.appendChild(proceedBtn);
    panel.appendChild(actions);
    body.appendChild(panel);
  }

  function renderComparisonWizardTablesReadyBody(body) {
    var panel = document.createElement('div');
    panel.className = 'agente-compara-wizard-panel';
    appendWizardConfirmedSummary(panel);
    var info = document.createElement('p');
    info.className = 'agente-compara-wizard-ready-text small mb-0';
    info.textContent = 'Preparação das tabelas concluída.';
    panel.appendChild(info);
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'btn btn-sm btn-outline-primary agente-compara-wizard-close-btn';
    closeBtn.textContent = 'Fechar';
    bindWizardPrimaryButton(closeBtn, closeTempTableModal);
    panel.appendChild(closeBtn);
    body.appendChild(panel);
  }

  function renderComparisonWizardModal() {
    if (!isComparisonWizardFlowActive()) {
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
      return;
    }
    var view = resolveComparisonWizardView();
    if (!view) {
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
      return;
    }
    setComparisonWizardModalHeader(view);
    if (view === 'review' || view === 'processing') {
      renderTempTableModalContent(currentTempTable);
    } else {
      var body = byId('agenteComparaTempTableModalBody');
      if (!body) return;
      body.innerHTML = '';
      if (view === 'upload') {
        renderComparisonWizardUploadBody(body);
      } else if (view === 'ask_table_3') {
        renderComparisonWizardAskTable3Body(body);
      } else if (view === 'tables_ready') {
        renderComparisonWizardTablesReadyBody(body);
      }
    }
    updateTempTableModalFooter();
  }

  function shouldAutoOpenComparisonWizard() {
    if (!comparisonWizardEngaged || comparisonWizardModalSuppressed) return false;
    if (!isComparisonWizardFlowActive()) return false;
    var step = comparisonState.currentStep || '';
    if (step !== 'PREPARE_TABLE_1') return false;
    var view = resolveComparisonWizardView();
    return view === 'review' || view === 'processing';
  }

  function maybeOpenComparisonWizardAfterStatus() {
    if (!isComparisonWizardFlowActive()) return;
    if (isTempTableModalOpen()) {
      if (isComparisonWizardEngaged()) {
        renderComparisonWizardModal();
      }
      return;
    }
    if (shouldAutoOpenComparisonWizard()) {
      openComparisonWizardModal();
    }
  }

  function openComparisonWizardModal() {
    if (!isComparisonWizardEngaged()) return;
    comparisonWizardModalSuppressed = false;
    var modal = byId('agenteComparaTempTableModal');
    if (!modal) return;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    tempTableModalActiveTab = 'freight';
    renderComparisonWizardModal();
    modal.hidden = false;
    document.body.classList.add('agente-compara-temp-table-modal-open');
  }

  function refreshComparisonWizardAfterTransition() {
    comparisonWizardModalSuppressed = false;
    return fetchDocuments().then(function () {
      renderComparisonWizardModal();
      if (!isTempTableModalOpen()) {
        openComparisonWizardModal();
      }
    });
  }

  function setActiveComparisonTable(tableId, slot) {
    if (!tableId) return;
    fetch(API_COMPARISON_SET_ACTIVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        comparison_id: comparisonState.comparisonId,
        table_id: tableId,
        slot: slot
      })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (!data || data.ok !== true) return;
        if (data.comparison) syncComparisonStateFromPayload(data.comparison);
        handleTempTableFromStatus(data);
        if (taxStepActive) {
          renderTempTableModalContent(currentTempTable);
          updateTempTableModalFooter();
        }
      })
      .catch(function () { /* noop */ });
  }

  function chooseProceedWithTwoTables() {
    fetch(API_COMPARISON_PROCEED_TWO, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ comparison_id: comparisonState.comparisonId })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.comparison) syncComparisonStateFromPayload(data.comparison);
        return fetchDocuments();
      })
      .then(function () {
        activateComparisonCommonParamsStep('TAXES');
      });
  }

  function chooseAddThirdTable() {
    fetch(API_COMPARISON_ADD_THIRD, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({ comparison_id: comparisonState.comparisonId })
    })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        if (data && data.comparison) syncComparisonStateFromPayload(data.comparison);
        return refreshComparisonWizardAfterTransition();
      });
  }

  function isComparisonPrepareStep() {
    var step = comparisonState.currentStep || '';
    return step === 'PREPARE_TABLE_1' || step === 'PREPARE_TABLE_2' || step === 'PREPARE_TABLE_3';
  }
  var uploadInFlight = false;
  var comparisonWizardEngaged = false;
  var comparisonWizardModalSuppressed = false;
  var chatInFlight = false;
  var chatUnlocked = false;
  var chatHistory = [];
  var MAX_CHAT_HISTORY = 10;
  var CHAT_LOADING_ID = 'agenteComparaChatLoading';
  var CHAT_BLOCKED_MESSAGE = 'Faça o upload da tabela de frete.';
  var CHAT_LOCKED_PLACEHOLDER = 'Faça o upload da tabela de frete para liberar o chat.';
  var CHAT_UNLOCKED_MESSAGE = 'O chat está liberado para consultas sobre a auditoria e os resultados.';

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
    login: 'É necessário estar logado para conversar com a Agente Compara.',
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
  var tempTableSaveExecutionId = null;
  var tempTableValidationErrors = [];
  var openFreightTableKeys = new Set();
  var hasUserTouchedFreightTableOpenState = false;
  var tempTableModalActiveTab = 'freight';
  var taxStepActive = false;
  var taxSaveInFlight = false;
  var taxContinueInFlight = false;
  var taxSelectedTableIds = new Set();
  var globalTaxConfig = null;
  var taxScenarioCommon = { origin_uf: '', origin_city: '', iss_rate: null };
  var taxConfigDirty = false;
  var taxTableUfsPreview = {};
  var coverageStepActive = false;
  var coveragePromptAnswered = false;
  var coveragePromptAccepted = false;
  var coverageUploadInFlight = false;
  var activeCoverageUploadPrefix = 'agenteComparaCoverage';
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

  var BRAZILIAN_UFS = ['AC', 'AL', 'AP', 'AM', 'BA', 'CE', 'DF', 'ES', 'GO', 'MA', 'MG', 'MS', 'MT', 'PA', 'PB', 'PE', 'PI', 'PR', 'RJ', 'RN', 'RO', 'RR', 'RS', 'SC', 'SE', 'SP', 'TO'];
  var ICMS_7_PERCENT_ORIGIN_UFS = ['PR', 'RS', 'SC', 'ES', 'MG', 'RJ', 'SP'];
  var ICMS_7_PERCENT_DESTINATION_UFS = ['AC', 'AP', 'AM', 'PA', 'RO', 'RR', 'TO', 'AL', 'BA', 'CE', 'MA', 'PB', 'PE', 'PI', 'RN', 'SE', 'DF', 'GO', 'MT', 'MS', 'ES'];
  var BR_STATE_NAME_TO_UF = {
    'ACRE': 'AC',
    'ALAGOAS': 'AL',
    'AMAPA': 'AP',
    'AMAZONAS': 'AM',
    'BAHIA': 'BA',
    'CEARA': 'CE',
    'DISTRITO FEDERAL': 'DF',
    'ESPIRITO SANTO': 'ES',
    'GOIAS': 'GO',
    'MARANHAO': 'MA',
    'MATO GROSSO': 'MT',
    'MATO GROSSO DO SUL': 'MS',
    'MINAS GERAIS': 'MG',
    'PARA': 'PA',
    'PARAIBA': 'PB',
    'PARANA': 'PR',
    'PERNAMBUCO': 'PE',
    'PIAUI': 'PI',
    'RIO DE JANEIRO': 'RJ',
    'RIO GRANDE DO NORTE': 'RN',
    'RIO GRANDE DO SUL': 'RS',
    'RONDONIA': 'RO',
    'RORAIMA': 'RR',
    'SANTA CATARINA': 'SC',
    'SAO PAULO': 'SP',
    'SERGIPE': 'SE',
    'TOCANTINS': 'TO'
  };
  var KNOWN_CITY_TO_UF = {
    'CAMPINAS': 'SP',
    'SAO PAULO': 'SP',
    'JOINVILLE': 'SC'
  };
  var TAX_DESTINATION_SOURCE_LABELS = {
    automatic: 'Automática',
    inferred_city: 'Inferida por cidade',
    inferred_state: 'Inferida por estado',
    manual: 'Manual'
  };
  var ICMS_INTERSTATE_SOURCE_NAME = 'Resolução Senado Federal nº 22/1989';
  var ICMS_INTERMUNICIPAL_SOURCE_NAME = 'Cadastro estadual/manual';

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

  function setTempTableModalError(messageOrPayload) {
    var el = byId('agenteComparaTempTableModalError');
    if (!el) return;
    if (!messageOrPayload) {
      el.hidden = true;
      el.replaceChildren();
      return;
    }
    el.hidden = false;
    if (typeof messageOrPayload === 'object') {
      if (resolvePlanLimitPayload(messageOrPayload)) {
        fillLimitMessageElement(el, messageOrPayload);
        return;
      }
      fillLimitMessageElement(el, messageOrPayload.message || friendlyError(messageOrPayload));
      return;
    }
    fillLimitMessageElement(el, messageOrPayload);
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

  function hasTaxConfig(tempTable) {
    return !!(tempTable && tempTable.tax_config && typeof tempTable.tax_config === 'object');
  }

  function shouldShowTaxTab(tempTable) {
    return !!(taxStepActive || hasTaxConfig(tempTable));
  }

  function canEditCoverageTable(tempTable) {
    return hasCoverageRows(tempTable);
  }

  function resetTaxStepState() {
    taxStepActive = false;
    taxSaveInFlight = false;
    taxContinueInFlight = false;
    taxSelectedTableIds = new Set();
    globalTaxConfig = null;
    taxScenarioCommon = { origin_uf: '', origin_city: '', iss_rate: null };
    taxConfigDirty = false;
    taxTableUfsPreview = {};
    if (tempTableModalActiveTab === 'taxes') {
      tempTableModalActiveTab = 'freight';
    }
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

  function calculationFileStatusLabel(status) {
    var key = String(status || '').toLowerCase();
    if (key === 'uploaded' || key === 'processed') {
      return 'Arquivo recebido para comparação';
    }
    return 'Arquivo recebido para comparação';
  }

  function getCalculationFileMetadata(tempTable) {
    if (!tempTable || !tempTable.audit_batch) return null;
    var batch = tempTable.audit_batch;
    var status = String(batch.status || '').toLowerCase();
    if (status !== 'uploaded' && status !== 'processed') return null;
    return {
      source_file_name: batch.source_file_name || '',
      row_count: batch.row_count,
      max_rows: batch.max_rows,
      status: status,
      visual_status: calculationFileStatusLabel(status)
    };
  }

  function shouldShowProcessCalculationsButton(tempTable) {
    if (!isComparisonPostConfigStep()) return false;
    if (!comparisonState.comparisonId) return false;
    if (auditUploadInFlight) return false;
    var confirmed = confirmedComparisonTablesForReview();
    if (!confirmed || confirmed.length < 2 || confirmed.length > 3) return false;
    var meta = getCalculationFileMetadata(tempTable);
    return !!(meta && (meta.status === 'uploaded' || meta.status === 'processed'));
  }

  function canEnableProcessCalculationsButton() {
    if (comparisonCalculationInFlight) return false;
    if (auditUploadInFlight) return false;
    if (!comparisonState.comparisonId) return false;
    var step = comparisonState.currentStep || '';
    var billing = comparisonCalculationState.billingStatus || '';
    if (step === 'CALCULATION_RUNNING') return false;
    // READY vigente só bloqueia novo cálculo quando billing já foi aplicado.
    if (
      step === 'CALCULATION_READY' &&
      !comparisonCalculationState.stale &&
      billing === 'applied'
    ) {
      return false;
    }
    if (step === 'CONFIGURATION_READY' || step === 'CALCULATION_FAILED') return true;
    if (step === 'CALCULATION_READY' && comparisonCalculationState.stale) return true;
    if (
      step === 'CALCULATION_READY' &&
      (billing === 'pending' || billing === 'failed' || billing === 'not_started')
    ) {
      return true;
    }
    return false;
  }

  function setProcessCalculationsButtonState(button) {
    if (!button) return;
    var enable = canEnableProcessCalculationsButton();
    var step = comparisonState.currentStep || '';
    var loading = comparisonCalculationInFlight || step === 'CALCULATION_RUNNING';

    button.disabled = !enable || loading;
    button.setAttribute('aria-disabled', button.disabled ? 'true' : 'false');
    button.setAttribute('aria-busy', loading ? 'true' : 'false');

    var billing = comparisonCalculationState.billingStatus || '';
    if (loading) {
      button.classList.add('is-loading');
      button.textContent = 'Processando cálculos...';
      button.setAttribute('title', 'Processando cálculos comparativos...');
    } else if (step === 'CALCULATION_READY' && comparisonCalculationState.stale) {
      button.classList.remove('is-loading');
      button.textContent = 'Reprocessar Cálculos';
      button.setAttribute('title', 'As configurações mudaram. Processe novamente.');
    } else if (
      step === 'CALCULATION_READY' &&
      !comparisonCalculationState.stale &&
      (billing === 'pending' || billing === 'failed')
    ) {
      button.classList.remove('is-loading');
      button.textContent = billing === 'pending' ? 'Finalizar processamento' : 'Regularizar processamento';
      button.setAttribute(
        'title',
        billing === 'pending'
          ? 'Cálculo concluído. Finalize o processamento.'
          : 'Cálculo concluído. Regularize o processamento sem recalcular.'
      );
    } else if (step === 'CALCULATION_READY' && !comparisonCalculationState.stale) {
      button.classList.remove('is-loading');
      button.textContent = 'Cálculos concluídos';
      button.setAttribute('title', 'Cálculos comparativos já concluídos para a configuração atual.');
    } else if (step === 'CALCULATION_FAILED') {
      button.classList.remove('is-loading');
      button.textContent = 'Tentar novamente';
      button.setAttribute('title', 'Tentar processar os cálculos novamente.');
    } else {
      button.classList.remove('is-loading');
      button.textContent = 'Processar Cálculos';
      button.setAttribute(
        'title',
        enable
          ? 'Processar cálculos comparativos das transportadoras confirmadas.'
          : 'Conclua a configuração para processar os cálculos.'
      );
    }

    bindProcessCalculationsButton(button);
  }

  function bindProcessCalculationsButton(button) {
    if (!button || button.dataset.processCalculationsBound === '1') return;
    button.dataset.processCalculationsBound = '1';
    button.addEventListener('click', onProcessCalculationsButtonClick);
  }

  function onProcessCalculationsButtonClick(event) {
    if (event) {
      event.preventDefault();
      event.stopPropagation();
    }
    processComparisonCalculations();
  }

  function formatComparisonMoney(value) {
    if (value === null || value === undefined || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return 'R$ ' + n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }

  function formatComparisonWeight(value) {
    if (value === null || value === undefined || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 0, maximumFractionDigits: 3 }) + ' kg';
  }

  function disambiguateCarrierColumnTitle(tables) {
    var counts = {};
    (tables || []).forEach(function (table) {
      var name = String((table && table.carrier_name) || 'Transportadora').trim() || 'Transportadora';
      counts[name] = (counts[name] || 0) + 1;
    });
    return (tables || []).map(function (table) {
      var name = String((table && table.carrier_name) || 'Transportadora').trim() || 'Transportadora';
      var title = 'Frete calculado — ' + name;
      if (counts[name] > 1) {
        title = name + ' — Tabela ' + String(table.slot_number || '');
      }
      return {
        table_id: table.table_id,
        slot_number: table.slot_number,
        carrier_name: name,
        title: title
      };
    });
  }

  function clearComparisonCalculationResults(container) {
    if (!container) return;
    var existing = container.querySelector('#agenteComparaComparisonCalculationResults');
    if (existing) existing.remove();
    var status = container.querySelector('#agenteComparaComparisonCalculationStatus');
    if (status) status.remove();
  }

  function renderComparisonCalculationStatus(container) {
    if (!container) return;
    var existing = container.querySelector('#agenteComparaComparisonCalculationStatus');
    if (existing) existing.remove();

    var status = document.createElement('p');
    status.className = 'agente-compara-run-status';
    status.id = 'agenteComparaComparisonCalculationStatus';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');

    var step = comparisonState.currentStep || '';
    var calcStatus = comparisonCalculationState.status || 'not_started';

    var billing = comparisonCalculationState.billingStatus || '';
    if (comparisonCalculationInFlight || step === 'CALCULATION_RUNNING' || calcStatus === 'CALCULATION_RUNNING') {
      status.classList.add('is-loading');
      status.textContent = 'Processando cálculos comparativos...';
    } else if (calcStatus === 'CALCULATION_READY' && comparisonCalculationState.stale) {
      status.classList.add('is-error');
      status.textContent = 'As configurações mudaram. O resultado anterior está desatualizado.';
    } else if (calcStatus === 'CALCULATION_READY' && billing === 'pending') {
      status.classList.add('is-loading');
      status.textContent = 'Finalizando processamento...';
    } else if (calcStatus === 'CALCULATION_READY' && billing === 'failed') {
      status.classList.add('is-error');
      status.textContent =
        (comparisonCalculationState.error && comparisonCalculationState.error.message) ||
        'Cálculo concluído, mas a regularização da execução falhou. Tente novamente.';
    } else if (calcStatus === 'CALCULATION_READY' && billing === 'applied') {
      status.classList.add('is-success');
      status.textContent = 'Cálculos concluídos';
    } else if (calcStatus === 'CALCULATION_READY') {
      status.classList.add('is-loading');
      status.textContent = 'Finalizando processamento...';
    } else if (calcStatus === 'CALCULATION_FAILED') {
      status.classList.add('is-error');
      status.textContent =
        (comparisonCalculationState.error && comparisonCalculationState.error.message) ||
        'Não foi possível concluir o cálculo comparativo.';
    } else {
      return;
    }
    container.appendChild(status);
  }

  function renderComparisonCalculationResults(container, result) {
    clearComparisonCalculationResults(container);
    renderComparisonCalculationStatus(container);
    if (!result || !Array.isArray(result.comparative_rows)) return;
    if (comparisonCalculationState.stale) return;

    var region = document.createElement('div');
    region.className = 'agente-compara-comparison-calculation-results';
    region.id = 'agenteComparaComparisonCalculationResults';
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', 'Resultados do cálculo comparativo');

    var title = document.createElement('h3');
    title.className = 'agente-compara-run-summary-title';
    title.textContent = 'Resultado comparativo';
    region.appendChild(title);

    var scroll = document.createElement('div');
    scroll.className = 'agente-compara-comparison-calculation-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-comparison-calculation-table';
    table.setAttribute('aria-label', 'Tabela comparativa de frete calculado');

    var columns = disambiguateCarrierColumnTitle(result.tables || []);
    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');
    [
      'Documento',
      'Cidade destino',
      'UF',
      'Peso',
      'Valor da NF'
    ].forEach(function (label) {
      var th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      headRow.appendChild(th);
    });
    columns.forEach(function (col) {
      var th = document.createElement('th');
      th.scope = 'col';
      th.textContent = col.title;
      th.setAttribute('data-table-id', col.table_id || '');
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    var fragment = document.createDocumentFragment();
    result.comparative_rows.forEach(function (row) {
      var tr = document.createElement('tr');
      var cells = [
        row.document_number,
        row.destination_city,
        row.destination_uf,
        formatComparisonWeight(row.weight),
        formatComparisonMoney(row.invoice_value)
      ];
      cells.forEach(function (value) {
        var td = document.createElement('td');
        td.textContent = hasFieldValue(value) ? String(value) : '—';
        tr.appendChild(td);
      });

      var tableResults = row.table_results || {};
      columns.forEach(function (col) {
        var td = document.createElement('td');
        td.setAttribute('data-table-id', col.table_id || '');
        var cell = tableResults[col.table_id];
        if (!cell && Array.isArray(tableResults)) {
          cell = tableResults.find(function (item) {
            return item && item.table_id === col.table_id;
          });
        }
        if (!cell) {
          td.textContent = 'Não calculado';
        } else if (cell.status && cell.status !== 'calculated') {
          td.textContent = 'Não calculado';
          var detail =
            (cell.error && (cell.error.message || cell.error.code)) ||
            cell.status ||
            'Erro de domínio';
          td.title = String(detail);
          td.setAttribute('aria-label', 'Não calculado: ' + String(detail));
        } else {
          td.textContent = formatComparisonMoney(cell.calculated_freight);
        }
        tr.appendChild(td);
      });
      fragment.appendChild(tr);
    });
    tbody.appendChild(fragment);
    table.appendChild(tbody);
    scroll.appendChild(table);
    region.appendChild(scroll);
    container.appendChild(region);
  }

  function applyComparisonCalculationPayload(payload) {
    if (!payload || typeof payload !== 'object') return;
    comparisonCalculationState.status = payload.status || 'not_started';
    comparisonCalculationState.executionId = payload.execution_id || null;
    comparisonCalculationState.fingerprintShort = payload.fingerprint_short || null;
    comparisonCalculationState.stale = payload.stale === true;
    comparisonCalculationState.billingStatus = payload.billing_status || null;
    if (payload.error && typeof payload.error === 'object') {
      comparisonCalculationState.error = payload.error;
    } else if (payload.error_code || payload.message) {
      comparisonCalculationState.error = {
        code: payload.error_code || null,
        message: payload.message || null
      };
    } else {
      comparisonCalculationState.error = null;
    }
    // Resultado só é liberado com billing applied; não usar path/storage key.
    if (
      payload.status === 'CALCULATION_READY' &&
      payload.result &&
      !payload.stale &&
      payload.billing_status === 'applied'
    ) {
      comparisonCalculationState.result = payload.result;
    } else if (payload.status === 'CALCULATION_FAILED') {
      comparisonCalculationState.result = null;
    } else if (
      payload.status === 'CALCULATION_READY' &&
      (payload.billing_status === 'pending' || payload.billing_status === 'failed')
    ) {
      comparisonCalculationState.result = null;
    } else if (payload.status === 'not_started' || payload.status === 'CALCULATION_RUNNING') {
      if (payload.status === 'CALCULATION_RUNNING') {
        comparisonCalculationState.result = null;
      }
    }
    if (payload.current_step) {
      comparisonState.currentStep = payload.current_step;
    }
  }

  function restoreComparisonCalculationFromStatus() {
    if (!comparisonState.comparisonId) return Promise.resolve(null);
    if (!isComparisonPostConfigStep() && comparisonState.currentStep !== 'CONFIGURATION_READY') {
      // Still try restore when step already CALCULATION_* from server.
      if (!isComparisonCalculationStep()) return Promise.resolve(null);
    }
    var url =
      API_COMPARISON_CALCULATION +
      '?comparison_id=' +
      encodeURIComponent(comparisonState.comparisonId || '');
    return fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        }).catch(function () {
          return { status: r.status, data: null };
        });
      })
      .then(function (res) {
        if (!res.data || !res.data.ok) return null;
        applyComparisonCalculationPayload(res.data);
        var summary = document.getElementById('agenteComparaCalculationFileSummary');
        if (summary && summary.parentNode) {
          renderComparisonCalculationResults(summary.parentNode, comparisonCalculationState.result);
          var btn = document.getElementById('agenteComparaProcessCalculationsButton');
          setProcessCalculationsButtonState(btn);
        }
        return res.data;
      })
      .catch(function () {
        return null;
      });
  }

  function processComparisonCalculations() {
    if (comparisonCalculationInFlight) return;
    comparisonCalculationInFlight = true;

    var button = document.getElementById('agenteComparaProcessCalculationsButton');
    setProcessCalculationsButtonState(button);

    var executionId = generateRequestId();
    var comparisonId = comparisonState.comparisonId;
    if (!comparisonId) {
      comparisonCalculationInFlight = false;
      setProcessCalculationsButtonState(button);
      return;
    }

    var summaryHost = document.getElementById('agenteComparaCalculationFileSummary');
    if (summaryHost && summaryHost.parentNode) {
      renderComparisonCalculationStatus(summaryHost.parentNode);
    }

    fetch(API_COMPARISON_CALCULATE, {
      method: 'POST',
      credentials: 'same-origin',
      headers: {
        'Content-Type': 'application/json',
        Accept: 'application/json',
        'X-Execution-ID': executionId
      },
      body: JSON.stringify({
        comparison_id: comparisonId,
        execution_id: executionId,
        schema_version: 1
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        }).catch(function () {
          return {
            status: r.status,
            data: {
              ok: false,
              message: 'Não foi possível processar os cálculos comparativos.'
            }
          };
        });
      })
      .then(function (res) {
        comparisonCalculationInFlight = false;
        var data = res.data || {};
        if (res.status === 409) {
          comparisonCalculationState.error = {
            code: data.error_code || 'conflict',
            message: data.message || 'Conflito ao processar os cálculos.'
          };
          if (data.status) applyComparisonCalculationPayload(data);
          else comparisonCalculationState.status = 'CALCULATION_FAILED';
        } else if (data.status === 'CALCULATION_READY') {
          // READY matemático: pode vir com billing pending/failed (ok=false) sem liberar result.
          applyComparisonCalculationPayload(data);
          comparisonState.currentStep = 'CALCULATION_READY';
        } else if (data.status === 'CALCULATION_FAILED') {
          applyComparisonCalculationPayload({
            status: 'CALCULATION_FAILED',
            execution_id: data.execution_id || executionId,
            error: {
              code: data.error_code || 'agente_compara_calculation_failed',
              message: data.message || 'Não foi possível concluir o cálculo comparativo.'
            },
            current_step: 'CALCULATION_FAILED'
          });
          comparisonState.currentStep = 'CALCULATION_FAILED';
        } else if (data.ok === false) {
          applyComparisonCalculationPayload({
            status: 'CALCULATION_FAILED',
            execution_id: data.execution_id || executionId,
            error: {
              code: data.error_code || 'agente_compara_calculation_failed',
              message: data.message || 'Não foi possível concluir o cálculo comparativo.'
            },
            current_step: 'CALCULATION_FAILED'
          });
          comparisonState.currentStep = 'CALCULATION_FAILED';
        } else if (data.status === 'CALCULATION_RUNNING') {
          applyComparisonCalculationPayload(data);
          comparisonState.currentStep = 'CALCULATION_RUNNING';
        }

        button = document.getElementById('agenteComparaProcessCalculationsButton');
        setProcessCalculationsButtonState(button);
        var host = document.getElementById('agenteComparaCalculationFileSummary');
        if (host && host.parentNode) {
          renderComparisonCalculationResults(host.parentNode, comparisonCalculationState.result);
        }
      })
      .catch(function () {
        comparisonCalculationInFlight = false;
        comparisonCalculationState.status = 'CALCULATION_FAILED';
        comparisonCalculationState.error = {
          code: 'agente_compara_calculation_failed',
          message: 'Não foi possível concluir o cálculo comparativo.'
        };
        comparisonState.currentStep = 'CALCULATION_FAILED';
        button = document.getElementById('agenteComparaProcessCalculationsButton');
        setProcessCalculationsButtonState(button);
        var host = document.getElementById('agenteComparaCalculationFileSummary');
        if (host && host.parentNode) {
          renderComparisonCalculationStatus(host.parentNode);
        }
      });
  }

  function clearCalculationFileSummary(container) {
    if (!container) return;
    var existing = container.querySelector('#agenteComparaCalculationFileSummary');
    if (existing) existing.remove();
    clearComparisonCalculationResults(container);
  }

  function appendCalculationFileDetailRow(container, label, value, valueId) {
    var row = document.createElement('div');
    row.className = 'agente-compara-temp-table-modal-detail-row';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-temp-table-modal-detail-label';
    labelEl.textContent = label + ':';
    var valueEl = document.createElement('strong');
    valueEl.className = 'agente-compara-temp-table-modal-detail-value';
    if (valueId) valueEl.id = valueId;
    valueEl.textContent = hasFieldValue(value) ? String(value) : '—';
    row.appendChild(labelEl);
    row.appendChild(valueEl);
    container.appendChild(row);
  }

  function renderCalculationFileSummary(container, tempTable, options) {
    options = options || {};
    clearCalculationFileSummary(container);
    var meta = getCalculationFileMetadata(tempTable);
    if (!meta) return null;

    var summary = document.createElement('div');
    summary.className = 'agente-compara-audit-file-summary';
    summary.id = 'agenteComparaCalculationFileSummary';
    summary.setAttribute('role', 'region');
    summary.setAttribute('aria-label', 'Arquivo recebido para comparação');

    var summaryTitle = document.createElement('h3');
    summaryTitle.className = 'agente-compara-audit-file-summary-title';
    summaryTitle.id = 'agenteComparaCalculationFileSummaryTitle';
    summaryTitle.textContent = 'Arquivo recebido para comparação';
    summary.appendChild(summaryTitle);

    appendCalculationFileDetailRow(
      summary,
      'Arquivo',
      meta.source_file_name || '—',
      'agenteComparaCalculationFileName'
    );
    appendCalculationFileDetailRow(
      summary,
      'Linhas',
      meta.row_count != null ? meta.row_count : '—',
      'agenteComparaCalculationFileRows'
    );
    appendCalculationFileDetailRow(
      summary,
      'Limite configurado',
      meta.max_rows != null ? meta.max_rows : '—',
      'agenteComparaCalculationFileLimit'
    );
    appendCalculationFileDetailRow(
      summary,
      'Status',
      meta.visual_status,
      'agenteComparaCalculationFileStatus'
    );

    if (options.showProcessButton !== false && shouldShowProcessCalculationsButton(tempTable)) {
      var runActions = document.createElement('div');
      runActions.className = 'agente-compara-run-actions';
      runActions.id = 'agenteComparaProcessCalculationsActions';

      var runBtn = document.createElement('button');
      runBtn.type = 'button';
      runBtn.className = 'agente-compara-run-btn';
      runBtn.id = 'agenteComparaProcessCalculationsButton';
      runBtn.textContent = 'Processar Cálculos';
      setProcessCalculationsButtonState(runBtn);
      runActions.appendChild(runBtn);
      summary.appendChild(runActions);

      var hint = document.createElement('p');
      hint.className = 'agente-compara-process-calculations-hint';
      hint.id = 'agenteComparaProcessCalculationsHint';
      if (
        comparisonCalculationState.status === 'CALCULATION_READY' &&
        !comparisonCalculationState.stale &&
        comparisonCalculationState.billingStatus === 'applied'
      ) {
        hint.textContent = 'Cálculos comparativos concluídos para a configuração atual.';
      } else if (
        comparisonCalculationState.status === 'CALCULATION_READY' &&
        comparisonCalculationState.billingStatus === 'pending'
      ) {
        hint.textContent = 'Cálculo concluído. Finalizando processamento...';
      } else if (
        comparisonCalculationState.status === 'CALCULATION_READY' &&
        comparisonCalculationState.billingStatus === 'failed'
      ) {
        hint.textContent = 'Cálculo concluído. Regularize o processamento para liberar o resultado.';
      } else if (comparisonCalculationState.stale) {
        hint.textContent = 'As configurações mudaram. Processe novamente para atualizar o resultado.';
      } else if (comparisonCalculationState.status === 'CALCULATION_FAILED') {
        hint.textContent = 'Houve uma falha no cálculo. Você pode tentar novamente.';
      } else {
        hint.textContent = 'Processa o frete calculado de cada transportadora confirmada.';
      }
      summary.appendChild(hint);
    }

    container.appendChild(summary);
    if (comparisonCalculationState.result || comparisonCalculationState.status === 'CALCULATION_FAILED' || comparisonCalculationState.status === 'CALCULATION_RUNNING') {
      renderComparisonCalculationResults(container, comparisonCalculationState.result);
    }
    return summary;
  }

  function shouldShowAuditTab(tempTable) {
    if (auditFileStepActive) return true;
    return hasAuditBatch(tempTable);
  }

  function appendCoveragePromptCTA() {
    var container = byId('agenteComparaMessages');
    if (!container) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-bot agente-compara-coverage-prompt-msg';
    msg.setAttribute('data-chat-role', 'assistant');

    var inner = document.createElement('div');
    inner.className = 'agente-compara-chat-msg-inner agente-compara-coverage-prompt-panel';

    var card = document.createElement('div');
    card.className = 'agente-compara-coverage-prompt-card';

    var title = document.createElement('span');
    title.className = 'agente-compara-coverage-prompt-title';
    title.textContent = 'Cidades atendidas';
    card.appendChild(title);

    var description = document.createElement('p');
    description.className = 'agente-compara-coverage-prompt-description';
    description.textContent = 'Deseja informar a relação de cidades atendidas?';
    card.appendChild(description);

    var support = document.createElement('p');
    support.className = 'agente-compara-coverage-prompt-support';
    support.textContent = 'Use essa etapa quando a tabela de frete trabalhar com regiões, praças, rotas ou itinerários.';
    card.appendChild(support);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-coverage-prompt-actions';

    var yesBtn = document.createElement('button');
    yesBtn.type = 'button';
    yesBtn.className = 'agente-compara-coverage-prompt-yes agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-primary';
    yesBtn.textContent = 'Sim, enviar planilha';
    yesBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(true);
    });

    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'agente-compara-coverage-prompt-no agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-secondary';
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
    var idPrefix = prefix || 'agenteComparaCoverage';
    var fileInputId = idPrefix + 'FileInput';
    activeCoverageUploadPrefix = idPrefix;
    var card = document.createElement('div');
    card.className = 'agente-compara-coverage-upload-card';

    var header = document.createElement('div');
    header.className = 'agente-compara-coverage-upload-header';

    var title = document.createElement('span');
    title.className = 'agente-compara-coverage-upload-title';
    title.textContent = 'Cidades atendidas';

    var badge = document.createElement('span');
    badge.className = 'agente-compara-coverage-upload-badge';
    badge.textContent = 'CSV ou XLSX';

    header.appendChild(title);
    header.appendChild(badge);
    card.appendChild(header);

    var description = document.createElement('p');
    description.className = 'agente-compara-coverage-upload-description';
    description.textContent = 'Envie uma planilha com UF, cidade e região de frete.';
    card.appendChild(description);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-coverage-upload-actions';

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.accept = '.csv,.xlsx,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,text/csv';
    fileInput.className = 'visually-hidden agente-compara-coverage-upload-input';
    fileInput.id = fileInputId;

    var selectBtn = document.createElement('label');
    selectBtn.className = 'agente-compara-coverage-upload-button';
    selectBtn.setAttribute('for', fileInputId);
    selectBtn.textContent = 'Selecionar arquivo';

    var fileName = document.createElement('span');
    fileName.className = 'agente-compara-coverage-upload-file-name';
    fileName.id = idPrefix + 'UploadFileName';
    fileName.textContent = 'Nenhum arquivo selecionado';

    actions.appendChild(fileInput);
    actions.appendChild(selectBtn);
    actions.appendChild(fileName);
    card.appendChild(actions);

    var help = document.createElement('p');
    help.className = 'agente-compara-coverage-upload-help';
    help.textContent = 'Formatos aceitos: CSV ou XLSX.';
    card.appendChild(help);

    var status = document.createElement('p');
    status.className = 'agente-compara-coverage-upload-status';
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
    var container = byId('agenteComparaMessages');
    if (!container) return;
    if (byId('agenteComparaCoverageUploadPanel')) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-bot agente-compara-coverage-upload-msg';
    msg.id = 'agenteComparaCoverageUploadPanel';

    var inner = document.createElement('div');
    inner.className = 'agente-compara-chat-msg-inner agente-compara-coverage-upload-panel';

    renderCoverageUploadCard(inner, 'agenteComparaCoverage');
    msg.appendChild(inner);
    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function setCoverageUploadFileName(name) {
    var el = byId(activeCoverageUploadPrefix + 'UploadFileName') || byId('agenteComparaCoverageModalUploadFileName') || byId('agenteComparaCoverageUploadFileName');
    if (el) el.textContent = name || 'Nenhum arquivo selecionado';
  }

  function setCoverageUploadStatus(messageOrPayload, state) {
    var el = byId(activeCoverageUploadPrefix + 'UploadStatus') || byId('agenteComparaCoverageModalUploadStatus') || byId('agenteComparaCoverageUploadStatus');
    if (!el) return;
    if (!messageOrPayload) {
      el.replaceChildren();
    } else if (typeof messageOrPayload === 'object') {
      if (resolvePlanLimitPayload(messageOrPayload)) {
        fillLimitMessageElement(el, messageOrPayload);
      } else {
        fillLimitMessageElement(el, messageOrPayload.message || friendlyError(messageOrPayload));
      }
    } else {
      fillLimitMessageElement(el, messageOrPayload);
    }
    el.className = 'agente-compara-coverage-upload-status';
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
            res.data || 'Não foi possível carregar o arquivo. Verifique o formato.',
            'error'
          );
          return;
        }
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        } else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        if (hasCoverageRows(currentTempTable)) {
          coverageStepActive = true;
          coveragePromptAnswered = true;
          coveragePromptAccepted = true;
          if ((comparisonState.currentStep || '') === 'CALCULATION_FILE') {
            activateComparisonCommonParamsStep('CALCULATION_FILE');
          } else {
            tempTableModalActiveTab = 'coverage';
          }
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
          setTempTableModalError(res.data || 'Não foi possível salvar a cobertura temporária.');
          return;
        }
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        } else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        tempTableEditMode = false;
        tempTableEditSnapshot = null;
        if (isComparisonCommonParamsStep('CALCULATION_FILE')) {
          activateComparisonCommonParamsStep('CALCULATION_FILE');
        } else {
          renderTempTableModalContent(currentTempTable);
        }
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
    var editBtn = byId('agenteComparaTempTableModalEdit');
    var cancelBtn = byId('agenteComparaTempTableModalCancelEdit');
    var saveBtn = byId('agenteComparaTempTableModalSave');
    var taxSaveBtn = byId('agenteComparaTempTableModalTaxSave');
    var startAuditBtn = byId('agenteComparaTempTableModalStartAudit');
    var clearSlotBtn = byId('agenteComparaTempTableModalClearSlot');
    var banner = byId('agenteComparaTempTableModalEditBanner');
    var wizardView = isComparisonWizardFlowActive() ? resolveComparisonWizardView() : null;
    var wizardNonReview = wizardView && wizardView !== 'review';
    var canClearSlot = isComparisonWizardStep() && !!comparisonState.comparisonId && !!comparisonState.activeTableId &&
      !isComparisonCommonParamsStep();
    if (clearSlotBtn) {
      clearSlotBtn.hidden = !canClearSlot || !!tempTableEditMode;
      clearSlotBtn.disabled = !!comparisonResetInFlight || !!tempTableSaveInFlight;
    }
    if (isComparisonConfigurationReady()) {
      if (editBtn) editBtn.hidden = true;
      if (cancelBtn) cancelBtn.hidden = true;
      if (banner) banner.hidden = true;
      if (startAuditBtn) startAuditBtn.hidden = true;
      if (saveBtn) saveBtn.hidden = true;
      if (taxSaveBtn) taxSaveBtn.hidden = true;
      if (clearSlotBtn) clearSlotBtn.hidden = true;
      document.body.classList.remove('agente-compara-temp-table-modal-editing');
      return;
    }
    if (wizardNonReview) {
      if (editBtn) editBtn.hidden = true;
      if (cancelBtn) cancelBtn.hidden = true;
      if (banner) banner.hidden = true;
      if (startAuditBtn) startAuditBtn.hidden = true;
      if (saveBtn) saveBtn.hidden = true;
      if (taxSaveBtn) taxSaveBtn.hidden = true;
      document.body.classList.remove('agente-compara-temp-table-modal-editing');
      return;
    }
    var onTaxTab = tempTableModalActiveTab === 'taxes' && shouldShowTaxTab(currentTempTable);
    var onCoverageTab = tempTableModalActiveTab === 'coverage' && shouldShowCoverageTab(currentTempTable);
    var onAuditTab = tempTableModalActiveTab === 'audit' && shouldShowAuditTab(currentTempTable);
    var coverageHasRows = hasCoverageRows(currentTempTable);
    var canStartAudit = coverageHasRows || (coveragePromptAnswered && !coveragePromptAccepted);
    var hideEditOnEmptyCoverage = onCoverageTab && !coverageHasRows;
    var hideEditOnTaxTab = onTaxTab;
    var hideEditOnAuditTab = onAuditTab;
    if (editBtn) {
      editBtn.hidden = !!tempTableEditMode || hideEditOnEmptyCoverage || hideEditOnTaxTab || hideEditOnAuditTab;
      editBtn.disabled = hideEditOnEmptyCoverage || hideEditOnTaxTab || hideEditOnAuditTab;
    }
    if (startAuditBtn) {
      startAuditBtn.hidden = !!tempTableEditMode || !onCoverageTab || !canStartAudit || onTaxTab || onAuditTab || hasAuditBatch(currentTempTable) ||
        isComparisonConfigurationFlow();
    }
    if (cancelBtn) cancelBtn.hidden = !tempTableEditMode;
    if (banner) banner.hidden = !tempTableEditMode;
    document.body.classList.toggle('agente-compara-temp-table-modal-editing', !!tempTableEditMode);
    if (saveBtn) {
      saveBtn.disabled = !!(tempTableSaveInFlight || taxSaveInFlight || taxContinueInFlight || coverageSaveInFlight);
      saveBtn.setAttribute('aria-busy', (tempTableSaveInFlight || taxSaveInFlight || taxContinueInFlight || coverageSaveInFlight) ? 'true' : 'false');
      if (onTaxTab) {
        saveBtn.textContent = (taxContinueInFlight || taxSaveInFlight || tempTableSaveInFlight)
          ? 'Salvando e continuando...'
          : 'Continuar para cidades';
        saveBtn.hidden = false;
      } else if (onCoverageTab) {
        saveBtn.textContent = 'Salvar';
        saveBtn.hidden = !tempTableEditMode;
      } else if (onAuditTab) {
        saveBtn.hidden = true;
      } else {
        saveBtn.textContent = 'Salvar e Avançar';
        saveBtn.hidden = false;
      }
    }
    if (taxSaveBtn) {
      taxSaveBtn.hidden = true;
      taxSaveBtn.disabled = !!(tempTableSaveInFlight || taxSaveInFlight || taxContinueInFlight || coverageSaveInFlight);
      taxSaveBtn.setAttribute('aria-busy', taxSaveInFlight ? 'true' : 'false');
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
    resetTempTableSaveExecutionId();
    tempTableEditSnapshot = deepCloneTempTable(currentTempTable);
    tempTableEditMode = true;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    var cancelBtn = byId('agenteComparaTempTableModalCancelEdit');
    if (cancelBtn) cancelBtn.focus();
  }

  function cancelTempTableEdit() {
    if (!tempTableEditMode) return;
    resetTempTableSaveExecutionId();
    if (tempTableEditSnapshot) {
      setCurrentTempTable(deepCloneTempTable(tempTableEditSnapshot), { refreshAuditBi: false });
    }
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    var editBtn = byId('agenteComparaTempTableModalEdit');
    if (editBtn) editBtn.focus();
  }

  function populateTempTableSaveEditTarget(editTarget, tempTable) {
    if (!editTarget || !tempTable) return;
    if (canEditFreightTables(tempTable)) {
      editTarget.freight_tables = deepCloneTempTable(tempTable.freight_tables) || [];
    } else if (canEditFreightRoutes(tempTable)) {
      editTarget.freight_routes = deepCloneTempTable(tempTable.freight_routes) || [];
    }
    editTarget.accessorial_fees = deepCloneTempTable(tempTable.accessorial_fees) || [];
    syncAccessorialMinimumAmountFields(editTarget.accessorial_fees);
  }

  function ensureTempTableSaveExecutionId() {
    if (!tempTableSaveExecutionId) {
      if (window.crypto && typeof window.crypto.randomUUID === 'function') {
        tempTableSaveExecutionId = window.crypto.randomUUID();
      } else {
        tempTableSaveExecutionId = 'save-' + Date.now() + '-' + Math.random().toString(36).slice(2);
      }
    }
    return tempTableSaveExecutionId;
  }

  function resetTempTableSaveExecutionId() {
    tempTableSaveExecutionId = null;
  }

  function collectTempTableSavePayload() {
    if (!currentTempTable || !currentTempTable.temp_table_id) return null;
    var payload = {
      temp_table_id: currentTempTable.temp_table_id,
      comparison_id: comparisonState.comparisonId,
      table_id: comparisonState.activeTableId,
      slot: activeComparisonTable() ? activeComparisonTable().slot_number : null,
      edit_version: typeof currentTempTable.edit_version === 'number' ? currentTempTable.edit_version : 0,
      edit_target: {
        freight_tables: [],
        freight_routes: [],
        accessorial_fees: []
      },
      review_action: 'save_and_advance',
      execution_id: ensureTempTableSaveExecutionId()
    };
    populateTempTableSaveEditTarget(payload.edit_target, currentTempTable);
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

  function accessorialStringHasDigit(text) {
    var source = String(text || '');
    for (var i = 0; i < source.length; i += 1) {
      var ch = source.charAt(i);
      if (ch >= '0' && ch <= '9') return true;
    }
    return false;
  }

  function accessorialParsePositiveDecimal(value) {
    var text = String(value || '').trim();
    if (!text || !accessorialStringHasDigit(text)) return null;
    var normalized = '';
    var i;
    for (i = 0; i < text.length; i += 1) {
      var ch = text.charAt(i);
      if ((ch >= '0' && ch <= '9') || ch === '.' || ch === ',') normalized += ch;
    }
    if (!normalized) return null;
    var commaIndex = normalized.indexOf(',');
    var dotIndex = normalized.indexOf('.');
    if (commaIndex !== -1) {
      normalized = normalized.slice(0, commaIndex).split('.').join('') + '.' + normalized.slice(commaIndex + 1).split('.').join('');
    } else if (dotIndex !== -1) {
      var afterDot = normalized.slice(dotIndex + 1);
      var hasSecondDot = normalized.indexOf('.', dotIndex + 1) !== -1;
      if (!hasSecondDot && afterDot.length > 0 && afterDot.length <= 4) {
        normalized = normalized.slice(0, dotIndex).split('.').join('') + '.' + afterDot.split('.').join('');
      } else {
        normalized = normalized.split('.').join('');
      }
    }
    var parsed = parseFloat(normalized);
    return isFinite(parsed) && parsed > 0 ? parsed : null;
  }

  function accessorialParseExplicitPercentValues(text) {
    var source = String(text || '');
    var values = [];
    var index = 0;
    while (index < source.length) {
      var percentIndex = source.indexOf('%', index);
      if (percentIndex < 0) break;
      var end = percentIndex - 1;
      while (end >= 0 && /\s/.test(source.charAt(end))) end -= 1;
      var start = end;
      while (start >= 0 && /[0-9.,]/.test(source.charAt(start))) start -= 1;
      var token = source.slice(start + 1, end + 1);
      var parsed = accessorialParsePositiveDecimal(token);
      if (parsed !== null && values.indexOf(parsed) === -1) values.push(parsed);
      index = percentIndex + 1;
    }
    return values;
  }

  function accessorialFormatPercent(value) {
    var num = Number(value);
    if (!isFinite(num)) return '';
    return num.toLocaleString('pt-BR', { minimumFractionDigits: 0, maximumFractionDigits: 6 });
  }

  function accessorialRateConflictError(fee, feeIndex) {
    if (normalizeCalculationUnit(fee && fee.unit) !== '%') return null;
    var structured = accessorialParsePositiveDecimal(fee && fee.value);
    if (structured === null) return null;
    var described = accessorialParseExplicitPercentValues(fee && fee.notes);
    if (described.length !== 1) return null;
    if (Math.abs(structured - described[0]) < 0.000001) return null;
    var name = hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1);
    return {
      code: 'accessorial_rate_conflict',
      section: 'accessorial_fees',
      index: feeIndex,
      name: name,
      field: 'value',
      related_fields: ['value', 'notes'],
      structured_percent: structured,
      described_percent: described[0],
      severity: 'blocking',
      reason_code: 'accessorial_rate_conflict',
      message: 'Há informações contraditórias na regra ' + name + '.\n\n'
        + 'Valor informado no campo: ' + accessorialFormatPercent(structured) + '%\n'
        + 'Valor descrito na observação: ' + accessorialFormatPercent(described[0]) + '%\n\n'
        + 'O sistema não pode decidir qual percentual utilizar. Corrija o valor, ajuste a observação ou exclua esta regra antes de continuar.'
    };
  }

  function syncAccessorialMinimumAmountFields(fees) {
    if (!Array.isArray(fees)) return;
    fees.forEach(function (fee) {
      if (!fee || typeof fee !== 'object' || !accessorialFeeIsMinimumAmount(fee)) return;
      var parsed = accessorialParsePositiveDecimal(fee.minimum_amount);
      if (parsed === null) parsed = accessorialParsePositiveDecimal(fee.value);
      if (parsed !== null) fee.minimum_amount = parsed;
    });
  }

  function accessorialFeeIsMinimumAmount(fee) {
    var modifier = String((fee && fee.modifier_type) || '').trim();
    var calcType = String((fee && fee.calculation_type) || '').trim();
    return modifier === 'minimum_amount' || calcType === 'minimum_amount';
  }

  function accessorialFeeLinkRefs(fee) {
    var refs = [];
    ['component_group', 'canonical_component', 'related_to'].forEach(function (field) {
      var value = String((fee && fee[field]) || '').trim();
      if (!value || value === 'generic_accessorial') return;
      if (refs.indexOf(value) === -1) refs.push(value);
    });
    return refs;
  }

  function accessorialFeeIsBaseForMinimumLink(fee) {
    if (!fee || typeof fee !== 'object') return false;
    var modifier = String(fee.modifier_type || '').trim();
    var calcType = String(fee.calculation_type || '').trim();
    if (modifier === 'minimum_amount' || calcType === 'minimum_amount') return false;
    if (modifier === 'maximum_amount' || calcType === 'maximum_amount') return false;
    return true;
  }

  function accessorialFeesShareLinkRef(feeA, feeB) {
    var refsA = accessorialFeeLinkRefs(feeA);
    var refsB = accessorialFeeLinkRefs(feeB);
    return refsA.some(function (ref) { return refsB.indexOf(ref) !== -1; });
  }

  function findLinkedAccessorialBaseFee(minimumFee, fees, minimumIndex) {
    var relatedTo = String((minimumFee && minimumFee.related_to) || '').trim();
    if (!relatedTo) return null;
    var matches = [];
    fees.forEach(function (fee, idx) {
      if (idx === minimumIndex || !fee || typeof fee !== 'object') return;
      if (!accessorialFeeIsBaseForMinimumLink(fee)) return;
      if (!accessorialFeesShareLinkRef(minimumFee, fee)) return;
      if (accessorialFeeLinkRefs(fee).indexOf(relatedTo) === -1) return;
      matches.push(fee);
    });
    return matches.length === 1 ? matches[0] : null;
  }

  function accessorialFeeHasValidMinimumAmount(fee) {
    var parsedAmount = accessorialParsePositiveDecimal(fee && fee.minimum_amount);
    if (parsedAmount !== null) return true;
    return accessorialParsePositiveDecimal(fee && fee.value) !== null;
  }

  function accessorialMinimumLinkErrorMessage() {
    return 'Esta regra mínima não possui uma taxa principal válida vinculada. Corrija ou exclua a regra antes de continuar.';
  }

  function accessorialFeeMissingCalculationBase(fee) {
    if (accessorialFeeIsMinimumAmount(fee)) return false;
    if (accessorialFeeIsExtractionHypothesis(fee)) return false;
    var baseId = String((fee && fee.calculation_base_id) || '').trim();
    var base = getCalculationBaseById(baseId);
    var basis = normalizeTextKey(fee && fee.calculation_basis);
    return !baseId || !base || basis === normalizeTextKey('não mapeado / revisar');
  }

  function accessorialFeeIsUserCommitted(fee) {
    var source = String((fee && fee.classification_source) || '').trim();
    return source.indexOf('manual_') === 0;
  }

  function accessorialFeeIsExtractionHypothesis(fee) {
    if (!fee || accessorialFeeIsUserCommitted(fee)) return false;
    var source = String(fee.classification_source || '').trim();
    var status = String(fee.status || '').trim();
    var baseId = String(fee.calculation_base_id || '').trim();
    var basis = normalizeTextKey(fee.calculation_basis);
    if (!baseId && basis === normalizeTextKey('não mapeado / revisar')) return true;
    if (
      (source === 'legacy_classifier' || source === 'unmapped_calculation_base')
      && (status === 'needs_review' || status === 'unknown' || status === 'unsupported' || !status)
      && !accessorialFeeHasRequiredValue(fee)
    ) {
      return true;
    }
    if (
      source === 'configured_calculation_base'
      && (status === 'needs_review' || status === 'unknown' || status === 'unsupported')
      && !accessorialFeeHasRequiredValue(fee)
    ) {
      return true;
    }
    return false;
  }

  function accessorialFeeShouldBlockAdvance(fee) {
    if (!fee || typeof fee !== 'object' || isPrimaryFreightAccessorialFee(fee)) return false;
    if (accessorialFeeIsExtractionHypothesis(fee)) return false;
    if (accessorialFeeIsMinimumAmount(fee)) return true;
    var baseId = String(fee.calculation_base_id || '').trim();
    var basis = normalizeTextKey(fee.calculation_basis);
    var source = String(fee.classification_source || '').trim();
    if (!baseId && basis !== normalizeTextKey('não mapeado / revisar') && source.indexOf('manual_') !== 0) {
      return false;
    }
    return true;
  }

  function validateLinkedMinimumAccessorialFee(fee, fees, feeIndex) {
    if (!accessorialFeeHasValidMinimumAmount(fee)) {
      return {
        section: 'accessorial_fees',
        index: feeIndex,
        name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
        field: 'value',
        reason_code: 'invalid_accessorial_value',
        message: accessorialValueErrorMessage()
      };
    }
    var relatedTo = String((fee && fee.related_to) || '').trim();
    if (!relatedTo || !findLinkedAccessorialBaseFee(fee, fees, feeIndex)) {
      return {
        section: 'accessorial_fees',
        index: feeIndex,
        name: hasFieldValue(fee.name) ? String(fee.name) : 'Item ' + (feeIndex + 1),
        field: 'related_to',
        reason_code: relatedTo ? 'invalid_minimum_base_link' : 'missing_minimum_base_link',
        message: accessorialMinimumLinkErrorMessage()
      };
    }
    return null;
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
    if (error.code === 'accessorial_rate_conflict' || error.reason_code === 'accessorial_rate_conflict') {
      return error.message || 'Há informações contraditórias nesta regra. Corrija o valor, a observação ou exclua a linha.';
    }
    if (error.reason_code === 'incompatible_accessorial_unit') {
      return 'Ajuste a unidade para a base selecionada.';
    }
    if (
      error.reason_code === 'missing_minimum_base_link'
      || error.reason_code === 'invalid_minimum_base_link'
    ) {
      return accessorialMinimumLinkErrorMessage();
    }
    return error.message || '';
  }

  function accessorialAdvanceValidationCountMessage(count) {
    if (count === 1) {
      return '1 item precisa de revisão. Corrija os campos destacados ou exclua a linha.';
    }
    return count + ' itens precisam de revisão. Corrija os campos destacados ou exclua as linhas.';
  }

  function accessorialValidationSummaryMessage(errors) {
    if (!errors || !errors.length) return '';
    if (errors.length === 1) {
      var err = errors[0];
      var detail = err.message || accessorialFieldErrorMessage(err);
      var name = err.name && String(err.name).trim();
      if (name && !/^Item \d+$/i.test(name)) {
        return 'Revise a generalidade "' + name + '": ' + detail;
      }
      return detail || 'Revise uma generalidade: informe o valor obrigatório.';
    }
    return accessorialAdvanceValidationCountMessage(errors.length);
  }

  function resolveTempTableSaveErrorMessage(data) {
    if (!data || typeof data !== 'object') return '';
    if (Array.isArray(data.errors) && data.errors.length) {
      return accessorialValidationSummaryMessage(data.errors);
    }
    if (data.error_code === 'invalid_accessorial_fees' && data.message) {
      return String(data.message);
    }
    if (data.message && typeof data.message === 'string') {
      return data.message;
    }
    return friendlyError(data);
  }

  function collectTempTableAdvanceValidationErrors() {
    var fees = currentTempTable && Array.isArray(currentTempTable.accessorial_fees)
      ? currentTempTable.accessorial_fees
      : [];
    syncAccessorialMinimumAmountFields(fees);
    var errors = [];
    fees.forEach(function (fee, feeIndex) {
      if (!fee || typeof fee !== 'object' || isPrimaryFreightAccessorialFee(fee)) return;
      if (!accessorialFeeShouldBlockAdvance(fee)) return;
      var error = null;
      if (accessorialFeeIsMinimumAmount(fee)) {
        error = validateLinkedMinimumAccessorialFee(fee, fees, feeIndex);
      } else if (accessorialFeeMissingCalculationBase(fee)) {
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
        } else {
          error = accessorialRateConflictError(fee, feeIndex);
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
        && (
          !field
          || error.field === field
          || (Array.isArray(error.related_fields) && error.related_fields.indexOf(field) !== -1)
        )
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
      setTempTableModalError(accessorialValidationSummaryMessage(tempTableValidationErrors));
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
    if (!data || typeof data !== 'object') return false;
    var errors = Array.isArray(data.errors) ? data.errors : [];
    if (data.error_code === 'invalid_accessorial_fees') {
      if (errors.length) {
        setTempTableValidationErrors(errors);
      } else if (data.message) {
        setTempTableModalError(String(data.message));
      } else {
        return false;
      }
      ensureTempTableEditModeForValidation();
      renderTempTableModalContent(currentTempTable);
      focusFirstTempTableValidationError();
      return true;
    }
    if (!errors.length) return false;
    setTempTableValidationErrors(errors);
    ensureTempTableEditModeForValidation();
    renderTempTableModalContent(currentTempTable);
    focusFirstTempTableValidationError();
    return true;
  }

  function saveTempTableAndAdvance() {
    if (!currentTempTable || tempTableSaveInFlight) return;
    tempTableSaveInFlight = true;
    updateTempTableModalFooter();
    if (!validateTempTableBeforeAdvance()) {
      tempTableSaveInFlight = false;
      updateTempTableModalFooter();
      return;
    }
    var payload = collectTempTableSavePayload();
    if (!payload) {
      tempTableSaveInFlight = false;
      updateTempTableModalFooter();
      setTempTableModalError('Nenhuma tabela temporária disponível para salvar.');
      return;
    }
    setTempTableModalError('');
    var saveSucceeded = false;

    fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-Execution-ID': payload.execution_id
      },
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
          if (res.status === 500) {
            setTempTableModalError('Não foi possível salvar a revisão da tabela temporária.');
            return;
          }
          var safeMessage = resolveTempTableSaveErrorMessage(res.data);
          if (safeMessage) {
            setTempTableModalError(safeMessage);
            if (res.data && res.data.error_code === 'invalid_accessorial_fees') {
              ensureTempTableEditModeForValidation();
              renderTempTableModalContent(currentTempTable);
            }
            return;
          }
          setTempTableModalError('Não foi possível salvar a revisão da tabela temporária.');
          return;
        }
        saveSucceeded = true;
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        } else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        clearTempTableValidationErrors();
        tempTableEditMode = false;
        tempTableEditSnapshot = null;
        resetTempTableSaveExecutionId();
        if (isComparisonPrepareStep()) {
          return refreshComparisonWizardAfterTransition();
        }
        if (isComparisonCommonParamsStep()) {
          activateComparisonCommonParamsStep(comparisonState.currentStep);
          return fetchDocuments();
        }
        taxStepActive = true;
        tempTableModalActiveTab = 'taxes';
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
    if ((comparisonState.currentStep || '') === 'COVERAGE') {
      skipComparisonCoverageAndAdvance();
      return;
    }
    auditFileStepActive = true;
    tempTableModalActiveTab = 'audit';
    clearTempTableValidationErrors();
    setTempTableModalError('');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
  }

  function skipComparisonCoverageAndAdvance() {
    if (!currentTempTable || !currentTempTable.temp_table_id || tempTableSaveInFlight) return;
    tempTableSaveInFlight = true;
    setTempTableModalError('');
    updateTempTableModalFooter();
    fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        temp_table_id: currentTempTable.temp_table_id,
        review_action: 'skip_coverage_and_advance'
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setTempTableModalError(res.data || 'Não foi possível avançar para o arquivo operacional.');
          return;
        }
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        } else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        coveragePromptAnswered = true;
        coveragePromptAccepted = false;
        activateComparisonCommonParamsStep('CALCULATION_FILE');
        return fetchDocuments();
      })
      .catch(function () {
        setTempTableModalError('Não foi possível avançar para o arquivo operacional. Verifique sua conexão e tente novamente.');
      })
      .finally(function () {
        tempTableSaveInFlight = false;
        updateTempTableModalFooter();
      });
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
    if (!comparisonState.comparisonId) return;
    var generation = comparisonRequestGeneration;
    var expectedComparisonId = comparisonState.comparisonId;
    tempTablePollTimer = window.setInterval(function () {
      if (tempTableEditMode || tempTableSaveInFlight) return;
      if (generation !== comparisonRequestGeneration || comparisonState.comparisonId !== expectedComparisonId) {
        stopTempTablePolling();
        return;
      }
      fetchDocuments().then(function (data) {
        if (generation !== comparisonRequestGeneration) {
          stopTempTablePolling();
          return;
        }
        if (!data || !data.temp_table) {
          stopTempTablePolling();
          return;
        }
        var status = String(data.temp_table.status || '').toLowerCase();
        if (status !== 'processing') {
          stopTempTablePolling();
          if (isTempTableModalOpen() && isComparisonWizardFlowActive()) {
            renderComparisonWizardModal();
          }
        }
      });
    }, TEMP_TABLE_POLL_MS);
  }

  function handleTempTableFromStatus(data) {
    if (!data) return;
    if (Object.prototype.hasOwnProperty.call(data, 'comparison')) {
      if (data.comparison) {
        syncComparisonStateFromPayload(data.comparison);
      } else {
        syncComparisonStateFromPayload(null);
        resetConfigurationReviewState();
        resetTaxStepState();
        resetCoveragePromptState();
        resetAuditFileStepState();
        comparisonWizardEngaged = false;
        comparisonWizardModalSuppressed = false;
        hideTempTableModalShell();
      }
    }
    currentCalculationBases = Array.isArray(data.calculation_bases) ? data.calculation_bases : [];
    var tempTable = data.temp_table || null;
    var previousTempTableId = currentTempTable && currentTempTable.temp_table_id;
    var nextTempTableId = tempTable && tempTable.temp_table_id;
    if (previousTempTableId && nextTempTableId && previousTempTableId !== nextTempTableId) {
      tempTableSaveInFlight = false;
      resetTaxStepState();
      resetCoveragePromptState();
      resetAuditFileStepState();
    } else if (previousTempTableId && !nextTempTableId) {
      tempTableSaveInFlight = false;
      resetTaxStepState();
      resetCoveragePromptState();
      resetAuditFileStepState();
    }
    if (tempTable && tempTable.temp_table_id) {
      if (!tempTableEditMode && !tempTableSaveInFlight) {
        setCurrentTempTable(tempTable);
      }
    } else if (!tempTableEditMode && !tempTableSaveInFlight) {
      setCurrentTempTable(null);
    }
    renderDocuments(data.documents || [], tempTable);
    announceTempTableStatusIfNeeded(tempTable);
    startTempTablePollingIfNeeded(tempTable);
    if (!comparisonState.comparisonId) {
      return;
    }
    if (isComparisonCommonParamsStep()) {
      activateComparisonCommonParamsStep(comparisonState.currentStep);
      return;
    }
    maybeOpenComparisonWizardAfterStatus();
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
      && (source.upgrade_url || source.upgrade_label || source.message_suffix !== undefined)
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
    return null;
  }

  function safeUpgradeHref(url) {
    var raw = String(url || '').trim();
    if (raw.indexOf('/') === 0 && raw.indexOf('//') !== 0) {
      var pathOnly = raw.split('?')[0].split('#')[0];
      if (pathOnly) return pathOnly;
    }
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
      : (payloadOrText && payloadOrText.message)
        || (payloadOrText && payloadOrText.mensagem_usuario)
        || '';
    el.textContent = fallback;
  }

  function setError(messageOrPayload) {
    var el = byId('agenteComparaDocumentsError');
    if (!el) return;
    if (!messageOrPayload) {
      el.style.display = 'none';
      el.replaceChildren();
      return;
    }
    el.style.display = 'block';
    if (typeof messageOrPayload === 'object') {
      if (resolvePlanLimitPayload(messageOrPayload)) {
        fillLimitMessageElement(el, messageOrPayload);
        return;
      }
      fillLimitMessageElement(el, messageOrPayload.message || friendlyError(messageOrPayload));
      return;
    }
    fillLimitMessageElement(el, messageOrPayload);
  }

  function setStatus(message) {
    var el = byId('agenteComparaUploadStatus');
    if (!el) return;
    el.textContent = message || '';
  }

  function setUploadLoading(on) {
    uploadInFlight = !!on;
    var attachBtn = byId('agenteComparaAttachBtn');
    var fileInput = byId('agenteComparaFileInput');
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
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-error';
    }
    if (status === 'pending') {
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-preparing';
    }
    return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-ready';
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
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-preparing';
    }
    if (normalized === 'awaiting_validation' || normalized === 'validated') {
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-ready';
    }
    if (normalized === 'needs_review') {
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-preparing';
    }
    if (normalized === 'failed' || normalized === 'expired' || normalized === 'discarded') {
      return 'agente-compara-doc-item-badge agente-compara-doc-item-badge-error';
    }
    return 'agente-compara-doc-item-badge';
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

  var AUDIT_BI_CHART_LABELS = {
    transportadora: 'Impacto Financeiro por Transportadora',
    uf_destino: 'Impacto Financeiro por UF Destino',
    temporal: 'Evolução do Impacto Financeiro no Período',
    pareto_transportadora: 'Pareto do Valor Cobrado a Mais'
  };

  var AUDIT_BI_CHART_KEYS = [
    'transportadora',
    'uf_destino',
    'temporal',
    'pareto_transportadora'
  ];

  var AUDIT_BI_FIELD_REQUIREMENTS = {
    transportadora: ['carrier', 'charged_freight', 'expected_freight'],
    uf_destino: ['destination_uf', 'charged_freight', 'expected_freight'],
    temporal: ['issue_date', 'charged_freight', 'expected_freight'],
    pareto_transportadora: ['carrier', 'charged_freight', 'expected_freight']
  };

  var AUDIT_BI_FIELD_UNAVAILABLE_MESSAGE = 'Campo indisponível no lote auditado atual.';
  var AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE = 'Divergência financeira indisponível no lote auditado atual.';
  var AUDIT_BI_CARRIER_UNAVAILABLE_MESSAGE = 'Transportadora indisponível no lote auditado atual.';
  var AUDIT_BI_DESTINATION_UF_UNAVAILABLE_MESSAGE = 'UF destino indisponível no lote auditado atual.';
  var AUDIT_BI_TOP_N = 10;

  var auditBiDashboardState = {
    initialized: false,
    sourceRows: [],
    fieldPresence: {},
    rowCount: 0,
    activeFilters: {
      carrier: null,
      origin_uf: null,
      destination_uf: null,
      issue_date: null
    },
    hiddenCharts: {},
    chartInstances: {}
  };

  function auditBiSafeText(value) {
    return String(value == null ? '' : value).trim();
  }

  function auditBiGetNumeric(value) {
    var num = Number(value);
    return Number.isFinite(num) ? num : 0;
  }

  function auditBiHasNumericValue(value) {
    if (value === null || value === undefined || value === '') return false;
    return Number.isFinite(Number(value));
  }

  function auditBiComputeDivergence(row) {
    if (!row || typeof row !== 'object') return null;
    if (auditBiHasNumericValue(row.charged_freight) && auditBiHasNumericValue(row.expected_freight)) {
      return auditBiGetNumeric(row.charged_freight) - auditBiGetNumeric(row.expected_freight);
    }
    if (auditBiHasNumericValue(row.divergence_value)) {
      return auditBiGetNumeric(row.divergence_value);
    }
    return null;
  }

  function auditBiHasFinancialBase(row) {
    return row && auditBiHasNumericValue(row.charged_freight) && auditBiHasNumericValue(row.expected_freight);
  }

  function auditBiNormalizeDate(value) {
    var text = auditBiSafeText(value);
    if (/^\d{4}-\d{2}-\d{2}$/.test(text)) return text;
    var parsed = new Date(text);
    if (isNaN(parsed.getTime())) return '';
    return parsed.toISOString().slice(0, 10);
  }

  function auditBiFormatNumber(value, fractionDigits) {
    var digits = typeof fractionDigits === 'number' ? fractionDigits : 2;
    return auditBiGetNumeric(value).toLocaleString('pt-BR', {
      minimumFractionDigits: digits,
      maximumFractionDigits: digits
    });
  }

  function auditBiFormatCurrency(value) {
    return 'R$ ' + auditBiFormatNumber(value, 2);
  }

  function auditBiFormatPercent(value) {
    return auditBiFormatNumber(value, 2) + '%';
  }

  function auditBiFormatAbsoluteCurrency(value) {
    return auditBiFormatCurrency(Math.abs(auditBiGetNumeric(value)));
  }

  function auditBiResolveDeviationPredominance(row) {
    if (!row || typeof row !== 'object') return 'Predominância do desvio: indisponível';
    var over = auditBiGetNumeric(row.cobrado_a_mais);
    var under = auditBiGetNumeric(row.cobrado_a_menor);
    if (over > under + 0.004) return 'Predominância do desvio: cobrado a mais';
    if (under > over + 0.004) return 'Predominância do desvio: cobrado a menor';
    return 'Predominância do desvio: equilibrada';
  }

  function auditBiChartRequirementsMet(chartKey) {
    var required = AUDIT_BI_FIELD_REQUIREMENTS[chartKey] || [];
    return required.every(function (fieldKey) {
      return auditBiDashboardState.fieldPresence[fieldKey] === true;
    });
  }

  function auditBiFilteredRows() {
    var rows = Array.isArray(auditBiDashboardState.sourceRows) ? auditBiDashboardState.sourceRows : [];
    var filters = auditBiDashboardState.activeFilters;
    return rows.filter(function (row) {
      if (!row || typeof row !== 'object') return false;
      if (filters.carrier && auditBiSafeText(row.carrier) !== filters.carrier) return false;
      if (filters.origin_uf && auditBiSafeText(row.origin_uf) !== filters.origin_uf) return false;
      if (filters.destination_uf && auditBiSafeText(row.destination_uf) !== filters.destination_uf) return false;
      if (filters.issue_date && auditBiNormalizeDate(row.issue_date) !== filters.issue_date) return false;
      return true;
    });
  }

  function auditBiAggregateByField(rows, fieldName) {
    var grouped = {};
    rows.forEach(function (row) {
      var key = auditBiSafeText(row[fieldName]);
      if (!key) return;
      if (!grouped[key]) {
        grouped[key] = {
          chave: key,
          quantidade: 0,
          linhas_financeiras: 0,
          linhas_divergentes: 0,
          valor_cobrado: 0,
          valor_esperado: 0,
          divergencia_liquida: 0,
          cobrado_a_mais: 0,
          cobrado_a_menor: 0,
          impacto_total: 0
        };
      }
      grouped[key].quantidade += 1;
      if (auditBiHasNumericValue(row.charged_freight)) {
        grouped[key].valor_cobrado += auditBiGetNumeric(row.charged_freight);
      }
      if (auditBiHasNumericValue(row.expected_freight)) {
        grouped[key].valor_esperado += auditBiGetNumeric(row.expected_freight);
      }
      var divergence = auditBiComputeDivergence(row);
      if (divergence === null) return;
      grouped[key].linhas_financeiras += 1;
      grouped[key].divergencia_liquida += divergence;
      if (Math.abs(divergence) > 0.004) grouped[key].linhas_divergentes += 1;
      if (divergence > 0) {
        grouped[key].cobrado_a_mais += divergence;
        grouped[key].impacto_total += divergence;
      } else if (divergence < 0) {
        grouped[key].cobrado_a_menor += Math.abs(divergence);
        grouped[key].impacto_total += Math.abs(divergence);
      }
    });
    return Object.keys(grouped).map(function (key) { return grouped[key]; });
  }

  function auditBiHasDivergenceValue(rows) {
    return rows.some(function (row) {
      return auditBiComputeDivergence(row) !== null;
    });
  }

  function auditBiHasCarrierValue(rows) {
    return rows.some(function (row) {
      return row && auditBiSafeText(row.carrier) !== '';
    });
  }

  function auditBiHasDestinationUfValue(rows) {
    return rows.some(function (row) {
      return row && auditBiSafeText(row.destination_uf) !== '';
    });
  }

  function auditBiAggregateCarrierDivergence(rows) {
    return auditBiSortRows(auditBiAggregateByField(rows, 'carrier'), 'impacto_total', 'desc');
  }

  function auditBiAggregateByDate(rows) {
    var grouped = {};
    rows.forEach(function (row) {
      var key = auditBiNormalizeDate(row.issue_date);
      if (!key) return;
      if (!grouped[key]) {
        grouped[key] = {
          data: key,
          quantidade: 0,
          linhas_financeiras: 0,
          linhas_divergentes: 0,
          divergencia_liquida: 0,
          cobrado_a_mais: 0,
          cobrado_a_menor: 0,
          impacto_total: 0
        };
      }
      grouped[key].quantidade += 1;
      var divergence = auditBiComputeDivergence(row);
      if (divergence === null) return;
      grouped[key].linhas_financeiras += 1;
      grouped[key].divergencia_liquida += divergence;
      if (Math.abs(divergence) > 0.004) grouped[key].linhas_divergentes += 1;
      if (divergence > 0) {
        grouped[key].cobrado_a_mais += divergence;
        grouped[key].impacto_total += divergence;
      } else if (divergence < 0) {
        grouped[key].cobrado_a_menor += Math.abs(divergence);
        grouped[key].impacto_total += Math.abs(divergence);
      }
    });
    return Object.keys(grouped).sort().map(function (key) { return grouped[key]; });
  }

  function auditBiBuildOverchargeParetoRows(rows, fieldName) {
    var grouped = auditBiAggregateByField(rows, fieldName).filter(function (row) {
      return auditBiGetNumeric(row.cobrado_a_mais) > 0;
    });
    grouped.sort(function (a, b) { return b.cobrado_a_mais - a.cobrado_a_mais; });
    var total = grouped.reduce(function (sum, row) { return sum + row.cobrado_a_mais; }, 0);
    var accumulated = 0;
    return grouped.slice(0, AUDIT_BI_TOP_N).map(function (row) {
      var value = auditBiGetNumeric(row.cobrado_a_mais);
      var percentual = total > 0 ? (value / total) * 100 : 0;
      accumulated += percentual;
      return {
        chave: row.chave,
        valor: value,
        percentual: percentual,
        percentual_acumulado: accumulated
      };
    });
  }

  function auditBiSortRows(rows, sortKey, order) {
    var direction = order === 'asc' ? 1 : -1;
    return rows.slice().sort(function (a, b) {
      var av = a[sortKey];
      var bv = b[sortKey];
      if (sortKey === 'chave' || sortKey === 'data') {
        var at = auditBiSafeText(av).toUpperCase();
        var bt = auditBiSafeText(bv).toUpperCase();
        if (at < bt) return -1 * direction;
        if (at > bt) return 1 * direction;
        return 0;
      }
      var an = auditBiGetNumeric(av);
      var bn = auditBiGetNumeric(bv);
      if (an === bn) return 0;
      return (an - bn) * direction;
    });
  }

  function auditBiTopRows(rows, sortKey, valueKey) {
    return auditBiSortRows(rows, sortKey, 'desc').slice(0, AUDIT_BI_TOP_N).map(function (row) {
      return {
        label: auditBiSafeText(row.chave || row.data) || '-',
        value: auditBiGetNumeric(row[valueKey])
      };
    });
  }

  function auditBiBuildFinancialMetrics(rows) {
    var totalRows = Array.isArray(rows) ? rows.length : 0;
    var metrics = {
      totalRows: totalRows,
      financialRows: 0,
      divergentRows: 0,
      chargedTotal: 0,
      expectedTotal: 0,
      overcharged: 0,
      undercharged: 0,
      netDivergence: 0,
      absoluteImpact: 0,
      confidenceRatio: 0,
      confidenceLabel: 'Indisponível',
      confidenceClass: 'unavailable'
    };
    rows.forEach(function (row) {
      if (auditBiHasNumericValue(row.charged_freight)) {
        metrics.chargedTotal += auditBiGetNumeric(row.charged_freight);
      }
      if (auditBiHasNumericValue(row.expected_freight)) {
        metrics.expectedTotal += auditBiGetNumeric(row.expected_freight);
      }
      var divergence = auditBiComputeDivergence(row);
      if (divergence === null) return;
      if (auditBiHasFinancialBase(row)) metrics.financialRows += 1;
      if (Math.abs(divergence) > 0.004) metrics.divergentRows += 1;
      metrics.netDivergence += divergence;
      if (divergence > 0) {
        metrics.overcharged += divergence;
        metrics.absoluteImpact += divergence;
      } else if (divergence < 0) {
        metrics.undercharged += Math.abs(divergence);
        metrics.absoluteImpact += Math.abs(divergence);
      }
    });
    metrics.confidenceRatio = totalRows > 0 ? (metrics.financialRows / totalRows) * 100 : 0;
    metrics.averageAbsoluteDivergencePerDocument = totalRows > 0
      ? metrics.absoluteImpact / totalRows
      : 0;
    if (metrics.confidenceRatio >= 95) {
      metrics.confidenceLabel = 'Alta';
      metrics.confidenceClass = 'high';
    } else if (metrics.confidenceRatio >= 75) {
      metrics.confidenceLabel = 'Média';
      metrics.confidenceClass = 'medium';
    } else if (metrics.confidenceRatio > 0) {
      metrics.confidenceLabel = 'Baixa';
      metrics.confidenceClass = 'low';
    }
    return metrics;
  }

  function auditBiRenderExecutiveSummary(rows) {
    var metrics = auditBiBuildFinancialMetrics(rows);
    var confidenceCard = byId('agenteComparaBiConfidenceCard');
    var confidenceValue = byId('agenteComparaBiConfidenceValue');
    var confidenceDetail = byId('agenteComparaBiConfidenceDetail');
    if (confidenceCard) {
      confidenceCard.className = 'agente-compara-bi-confidence-card is-' + metrics.confidenceClass;
    }
    if (confidenceValue) {
      confidenceValue.textContent = metrics.confidenceLabel + ' (' + auditBiFormatPercent(metrics.confidenceRatio) + ')';
    }
    if (confidenceDetail) {
      confidenceDetail.textContent = metrics.financialRows + ' de ' + metrics.totalRows +
        ' linhas filtradas têm frete cobrado e esperado para cálculo financeiro.';
    }

    var kpiGrid = byId('agenteComparaBiKpiGrid');
    if (!kpiGrid) return;
    var kpis = [
      {
        label: 'Cobrado a mais',
        value: auditBiFormatCurrency(metrics.overcharged),
        hint: 'Soma das divergências positivas',
        className: 'is-negative'
      },
      {
        label: 'Cobrado a menor',
        value: auditBiFormatCurrency(metrics.undercharged),
        hint: 'Soma absoluta das divergências negativas',
        className: 'is-warning'
      },
      {
        label: 'Impacto total',
        value: auditBiFormatCurrency(metrics.absoluteImpact),
        hint: 'Cobrado a mais + cobrado a menor',
        className: 'is-negative'
      },
      {
        label: 'Divergência média por documento',
        value: auditBiFormatCurrency(Math.abs(metrics.averageAbsoluteDivergencePerDocument)),
        hint: 'Impacto financeiro absoluto médio por documento analisado.',
        className: ''
      },
      {
        label: 'Linhas divergentes',
        value: String(metrics.divergentRows),
        hint: metrics.financialRows ? auditBiFormatPercent((metrics.divergentRows / metrics.financialRows) * 100) + ' das linhas calculáveis' : 'Sem linhas calculáveis',
        className: ''
      },
      {
        label: 'Linhas analisadas',
        value: String(metrics.totalRows),
        hint: 'Após filtros cruzados ativos',
        className: 'is-positive'
      }
    ];
    kpiGrid.innerHTML = '';
    kpis.forEach(function (kpi) {
      var card = document.createElement('article');
      card.className = 'agente-compara-bi-kpi-card ' + kpi.className;
      card.innerHTML =
        '<span class="agente-compara-bi-kpi-label"></span>' +
        '<strong class="agente-compara-bi-kpi-value"></strong>' +
        '<span class="agente-compara-bi-kpi-hint"></span>';
      card.querySelector('.agente-compara-bi-kpi-label').textContent = kpi.label;
      card.querySelector('.agente-compara-bi-kpi-value').textContent = kpi.value;
      card.querySelector('.agente-compara-bi-kpi-hint').textContent = kpi.hint;
      kpiGrid.appendChild(card);
    });
  }

  function auditBiDestroyChart(chartKey) {
    var instance = auditBiDashboardState.chartInstances[chartKey];
    if (instance && typeof instance.destroy === 'function') {
      instance.destroy();
    }
    delete auditBiDashboardState.chartInstances[chartKey];
  }

  function auditBiDestroyAllCharts() {
    AUDIT_BI_CHART_KEYS.forEach(auditBiDestroyChart);
  }

  function auditBiEnsureChartJs() {
    return typeof window.Chart === 'function';
  }

  function auditBiGetCanvas(chartKey) {
    var section = byId('agenteComparaBiSection');
    if (!section) return null;
    return section.querySelector('[data-audit-bi-chart-canvas="' + chartKey + '"]');
  }

  function auditBiSetCardEmpty(chartKey, message, showCanvas) {
    var section = byId('agenteComparaBiSection');
    if (!section) return;
    var emptyEl = section.querySelector('[data-audit-bi-chart-empty="' + chartKey + '"]');
    var noteEl = section.querySelector('[data-audit-bi-chart-note="' + chartKey + '"]');
    var wrapEl = emptyEl ? emptyEl.parentElement.querySelector('.agente-compara-bi-chart-wrap') : null;
    if (emptyEl) {
      emptyEl.textContent = message || '';
      emptyEl.hidden = !message;
    }
    if (noteEl) noteEl.textContent = '';
    if (wrapEl) wrapEl.style.display = showCanvas ? '' : 'none';
    if (!showCanvas) auditBiDestroyChart(chartKey);
  }

  function auditBiApplyFilterToggle(filterKey, value) {
    if (!value) return;
    auditBiDashboardState.activeFilters[filterKey] =
      auditBiDashboardState.activeFilters[filterKey] === value ? null : value;
    auditBiRenderDashboard();
  }

  function auditBiHandleChartClick(chartKey, label) {
    var selected = auditBiSafeText(label);
    if (!selected || selected === '-') return;
    if (chartKey === 'transportadora' || chartKey === 'pareto_transportadora') {
      auditBiApplyFilterToggle('carrier', selected);
      return;
    }
    if (chartKey === 'uf_destino') {
      auditBiApplyFilterToggle('destination_uf', selected);
      return;
    }
    if (chartKey === 'temporal') {
      var day = auditBiNormalizeDate(selected);
      if (!day) return;
      auditBiApplyFilterToggle('issue_date', day);
    }
  }

  function auditBiRenderSimpleChart(chartKey, labels, values, options) {
    if (!auditBiEnsureChartJs()) {
      auditBiSetCardEmpty(chartKey, 'Chart.js indisponível nesta página.', false);
      return false;
    }
    var canvas = auditBiGetCanvas(chartKey);
    if (!canvas) return false;
    auditBiDestroyChart(chartKey);
    var type = options.type || 'bar';
    var indexAxis = options.indexAxis || 'x';
    var datasetLabel = options.datasetLabel || 'Valor';
    var valueFormatter = options.valueFormatter || auditBiFormatNumber;
    var chartColor = options.chartColor || '#25b0ff';
    var areaColor = options.areaColor || 'rgba(37, 176, 255, 0.20)';
    var isHorizontal = indexAxis === 'y';
    var displayValues = values.map(function (value) {
      return Math.abs(auditBiGetNumeric(value));
    });
    var tooltipRows = Array.isArray(options.tooltipRows) ? options.tooltipRows : null;
    var valueAxisKey = isHorizontal ? 'x' : 'y';
    var scales = {
      x: {
        grid: { color: 'rgba(124, 148, 189, 0.14)' },
        ticks: { color: '#c6d7f2' }
      },
      y: {
        grid: { color: 'rgba(124, 148, 189, 0.14)' },
        ticks: { color: '#c6d7f2' }
      }
    };
    scales[valueAxisKey].beginAtZero = true;
    scales[valueAxisKey].min = 0;
    if (valueFormatter === auditBiFormatCurrency || options.nonNegativeCurrency) {
      scales[valueAxisKey].ticks.callback = function (v) { return auditBiFormatAbsoluteCurrency(v); };
    }
    var instance = new window.Chart(canvas, {
      type: type,
      data: {
        labels: labels,
        datasets: [{
          label: datasetLabel,
          data: displayValues,
          borderColor: chartColor,
          backgroundColor: areaColor,
          fill: type === 'line',
          borderWidth: 2,
          tension: type === 'line' ? 0.24 : 0,
          pointRadius: type === 'line' ? 2 : 0,
          maxBarThickness: 28
        }]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        indexAxis: indexAxis,
        plugins: {
          legend: { display: false },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var parsed = ctx.parsed || {};
                var raw = isHorizontal ? parsed.x : parsed.y;
                return datasetLabel + ': ' + valueFormatter(Math.abs(raw));
              },
              afterBody: function (items) {
                if (!tooltipRows || !items || !items.length) return [];
                var row = tooltipRows[items[0].dataIndex];
                if (!row) return [];
                return [
                  'Cobrado a mais: ' + auditBiFormatAbsoluteCurrency(row.cobrado_a_mais),
                  'Cobrado a menor: ' + auditBiFormatAbsoluteCurrency(row.cobrado_a_menor),
                  'Impacto total: ' + auditBiFormatAbsoluteCurrency(row.impacto_total),
                  auditBiResolveDeviationPredominance(row)
                ];
              }
            }
          }
        },
        scales: scales,
        onClick: function (_event, elements) {
          if (!elements || !elements.length) return;
          auditBiHandleChartClick(chartKey, labels[elements[0].index]);
        }
      }
    });
    auditBiDashboardState.chartInstances[chartKey] = instance;
    return true;
  }

  function auditBiRenderParetoChart(chartKey, rows) {
    if (!auditBiEnsureChartJs()) {
      auditBiSetCardEmpty(chartKey, 'Chart.js indisponível nesta página.', false);
      return false;
    }
    var canvas = auditBiGetCanvas(chartKey);
    if (!canvas) return false;
    auditBiDestroyChart(chartKey);
    var labels = rows.map(function (row) { return auditBiSafeText(row.chave) || '-'; });
    var values = rows.map(function (row) { return auditBiGetNumeric(row.valor); });
    var acumulado = rows.map(function (row) { return auditBiGetNumeric(row.percentual_acumulado); });
    var percentual = rows.map(function (row) { return auditBiGetNumeric(row.percentual); });
    var instance = new window.Chart(canvas, {
      data: {
        labels: labels,
        datasets: [
          {
            type: 'bar',
            label: 'Cobrado a mais',
            data: values,
            yAxisID: 'y',
            backgroundColor: 'rgba(255, 77, 106, 0.42)',
            borderColor: '#FF4D6A',
            borderWidth: 1,
            maxBarThickness: 28
          },
          {
            type: 'line',
            label: 'Pareto acumulado',
            data: acumulado,
            yAxisID: 'y1',
            borderColor: '#f4b400',
            backgroundColor: 'rgba(244, 180, 0, 0.2)',
            fill: false,
            borderWidth: 2,
            tension: 0.24,
            pointRadius: 2
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: true, labels: { color: '#c6d7f2' } },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                if (ctx.dataset && ctx.dataset.yAxisID === 'y1') {
                  return ctx.dataset.label + ': ' + auditBiFormatPercent(ctx.parsed.y || 0);
                }
                return ctx.dataset.label + ': ' + auditBiFormatCurrency(ctx.parsed.y || 0);
              },
              afterBody: function (items) {
                var idx = items[0] ? items[0].dataIndex : 0;
                return [
                  'Percentual: ' + auditBiFormatPercent(percentual[idx] || 0),
                  'Acumulado: ' + auditBiFormatPercent(acumulado[idx] || 0)
                ];
              }
            }
          }
        },
        scales: {
          x: { grid: { color: 'rgba(124, 148, 189, 0.14)' }, ticks: { color: '#c6d7f2' } },
          y: {
            beginAtZero: true,
            position: 'left',
            grid: { color: 'rgba(124, 148, 189, 0.14)' },
            ticks: { color: '#c6d7f2', callback: function (v) { return auditBiFormatCurrency(v); } }
          },
          y1: {
            beginAtZero: true,
            position: 'right',
            min: 0,
            max: 100,
            grid: { drawOnChartArea: false },
            ticks: { color: '#f4d27a', callback: function (v) { return auditBiFormatNumber(v, 0) + '%'; } }
          }
        },
        onClick: function (_event, elements) {
          if (!elements || !elements.length) return;
          auditBiHandleChartClick(chartKey, labels[elements[0].index]);
        }
      }
    });
    auditBiDashboardState.chartInstances[chartKey] = instance;
    return true;
  }

  function auditBiRenderFinancialImpactBarChart(chartKey, rows) {
    if (!auditBiEnsureChartJs()) {
      auditBiSetCardEmpty(chartKey, 'Chart.js indisponível nesta página.', false);
      return false;
    }
    var canvas = auditBiGetCanvas(chartKey);
    if (!canvas) return false;
    auditBiDestroyChart(chartKey);
    var labels = rows.map(function (row) { return auditBiSafeText(row.chave) || '-'; });
    var overcharged = rows.map(function (row) { return auditBiGetNumeric(row.cobrado_a_mais); });
    var undercharged = rows.map(function (row) { return auditBiGetNumeric(row.cobrado_a_menor); });
    var totalImpact = rows.map(function (row) { return auditBiGetNumeric(row.impacto_total); });
    var instance = new window.Chart(canvas, {
      type: 'bar',
      data: {
        labels: labels,
        datasets: [
          {
            label: 'Cobrado a mais',
            data: overcharged,
            borderColor: '#FF4D6A',
            backgroundColor: 'rgba(255, 77, 106, 0.42)',
            borderWidth: 1,
            maxBarThickness: 22
          },
          {
            label: 'Cobrado a menor',
            data: undercharged,
            borderColor: '#f4b400',
            backgroundColor: 'rgba(244, 180, 0, 0.42)',
            borderWidth: 1,
            maxBarThickness: 22
          },
          {
            label: 'Impacto total',
            data: totalImpact,
            borderColor: '#7cc4ff',
            backgroundColor: 'rgba(124, 196, 255, 0.22)',
            borderWidth: 1,
            maxBarThickness: 22
          }
        ]
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        indexAxis: 'y',
        plugins: {
          legend: { display: true, labels: { color: '#c6d7f2' } },
          tooltip: {
            callbacks: {
              label: function (ctx) {
                var parsed = ctx.parsed || {};
                return ctx.dataset.label + ': ' + auditBiFormatAbsoluteCurrency(parsed.x);
              },
              afterBody: function (items) {
                if (!items || !items.length) return [];
                var row = rows[items[0].dataIndex];
                if (!row) return [];
                return [auditBiResolveDeviationPredominance(row)];
              }
            }
          }
        },
        scales: {
          x: {
            beginAtZero: true,
            min: 0,
            grid: { color: 'rgba(124, 148, 189, 0.14)' },
            ticks: { color: '#c6d7f2', callback: function (v) { return auditBiFormatAbsoluteCurrency(v); } }
          },
          y: {
            grid: { color: 'rgba(124, 148, 189, 0.14)' },
            ticks: { color: '#c6d7f2' }
          }
        },
        onClick: function (_event, elements) {
          if (!elements || !elements.length) return;
          auditBiHandleChartClick(chartKey, labels[elements[0].index]);
        }
      }
    });
    auditBiDashboardState.chartInstances[chartKey] = instance;
    return true;
  }

  function auditBiRenderCarrierDivergenceChart(rows) {
    return auditBiRenderFinancialImpactBarChart('transportadora', rows);
  }

  function auditBiRenderFilterUi() {
    var filters = auditBiDashboardState.activeFilters;
    var activeEntries = [];
    if (filters.carrier) activeEntries.push({ key: 'carrier', label: 'Transportadora', value: filters.carrier });
    if (filters.origin_uf) activeEntries.push({ key: 'origin_uf', label: 'UF origem', value: filters.origin_uf });
    if (filters.destination_uf) activeEntries.push({ key: 'destination_uf', label: 'UF destino', value: filters.destination_uf });
    if (filters.issue_date) activeEntries.push({ key: 'issue_date', label: 'Data', value: filters.issue_date });

    var statusEl = byId('agenteComparaBiFilterStatus');
    if (statusEl) {
      statusEl.textContent = activeEntries.length
        ? 'Filtros ativos: ' + activeEntries.map(function (entry) { return entry.label + '=' + entry.value; }).join(' | ')
        : 'Filtros inativos. Exibindo visão geral do lote auditado.';
    }

    var chipsEl = byId('agenteComparaBiFilterChips');
    if (chipsEl) {
      chipsEl.innerHTML = '';
      if (!activeEntries.length) {
        chipsEl.innerHTML = '<span class="small">nenhum</span>';
      } else {
        activeEntries.forEach(function (entry) {
          var chip = document.createElement('span');
          chip.className = 'agente-compara-bi-filter-chip';
          chip.innerHTML = entry.label + ': ' + entry.value +
            ' <button type="button" data-audit-bi-remove-filter="' + entry.key + '" aria-label="Remover filtro ' + entry.label + '">×</button>';
          chipsEl.appendChild(chip);
        });
      }
    }

    var clearBtn = byId('agenteComparaBiClearFiltersBtn');
    if (clearBtn) clearBtn.disabled = activeEntries.length === 0;
  }

  function auditBiResizeVisibleCharts() {
    window.requestAnimationFrame(function () {
      AUDIT_BI_CHART_KEYS.forEach(function (chartKey) {
        if (auditBiDashboardState.hiddenCharts[chartKey]) return;
        var instance = auditBiDashboardState.chartInstances[chartKey];
        if (instance && typeof instance.resize === 'function') {
          instance.resize();
        }
      });
    });
  }

  function auditBiRenderHiddenChartsUi() {
    var hiddenKeys = AUDIT_BI_CHART_KEYS.filter(function (chartKey) {
      return auditBiDashboardState.hiddenCharts[chartKey] === true;
    });
    var listEl = byId('agenteComparaBiHiddenChartsList');
    var showAllBtn = byId('agenteComparaBiShowAllChartsBtn');
    var allHiddenMessage = byId('agenteComparaBiAllHiddenMessage');
    var gridEl = byId('agenteComparaBiChartsGrid');
    if (gridEl) {
      gridEl.classList.toggle('agente-compara-bi-charts-grid--has-hidden', hiddenKeys.length > 0);
    }
    if (listEl) {
      listEl.innerHTML = '';
      if (!hiddenKeys.length) {
        listEl.innerHTML = '<span class="small">Nenhum gráfico oculto</span>';
      } else {
        hiddenKeys.forEach(function (chartKey) {
          var btn = document.createElement('button');
          btn.type = 'button';
          btn.className = 'btn btn-sm agente-compara-bi-hidden-chart-chip';
          btn.setAttribute('data-audit-bi-show-chart', chartKey);
          btn.textContent = 'Mostrar ' + (AUDIT_BI_CHART_LABELS[chartKey] || chartKey);
          listEl.appendChild(btn);
        });
      }
    }
    if (showAllBtn) showAllBtn.disabled = hiddenKeys.length === 0;
    if (allHiddenMessage) allHiddenMessage.hidden = hiddenKeys.length !== AUDIT_BI_CHART_KEYS.length;
    AUDIT_BI_CHART_KEYS.forEach(function (chartKey) {
      var card = document.querySelector('[data-audit-bi-chart-card="' + chartKey + '"]');
      if (card) card.classList.toggle('is-hidden', auditBiDashboardState.hiddenCharts[chartKey] === true);
    });
    auditBiResizeVisibleCharts();
  }

  function auditBiHideChart(chartKey) {
    if (AUDIT_BI_CHART_KEYS.indexOf(chartKey) === -1) return;
    auditBiDashboardState.hiddenCharts[chartKey] = true;
    auditBiRenderHiddenChartsUi();
  }

  function auditBiShowChart(chartKey) {
    if (AUDIT_BI_CHART_KEYS.indexOf(chartKey) === -1) return;
    delete auditBiDashboardState.hiddenCharts[chartKey];
    auditBiRenderHiddenChartsUi();
    auditBiRenderChartCard(chartKey, auditBiFilteredRows());
  }

  function auditBiShowAllCharts() {
    auditBiDashboardState.hiddenCharts = {};
    auditBiRenderHiddenChartsUi();
    auditBiRenderDashboard();
  }

  function auditBiClearFilters() {
    auditBiDashboardState.activeFilters = {
      carrier: null,
      origin_uf: null,
      destination_uf: null,
      issue_date: null
    };
    auditBiRenderDashboard();
  }

  function auditBiRenderChartCard(chartKey, filteredRows) {
    if (auditBiDashboardState.hiddenCharts[chartKey]) return;
    if (!auditBiChartRequirementsMet(chartKey)) {
      auditBiSetCardEmpty(chartKey, AUDIT_BI_FIELD_UNAVAILABLE_MESSAGE, false);
      return;
    }
    var noteEl = document.querySelector('[data-audit-bi-chart-note="' + chartKey + '"]');
    var wrapEl = document.querySelector('[data-audit-bi-chart-card="' + chartKey + '"] .agente-compara-bi-chart-wrap');
    if (wrapEl) wrapEl.style.display = '';
    var emptyMessage = filteredRows.length ? 'Sem dados para os filtros atuais.' : 'Sem dados no lote auditado atual.';
    if (chartKey === 'transportadora') {
      if (auditBiDashboardState.fieldPresence.carrier === false ||
          (filteredRows.length > 0 && !auditBiHasCarrierValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_CARRIER_UNAVAILABLE_MESSAGE, false);
        return;
      }
      if (auditBiDashboardState.fieldPresence.expected_freight === false ||
          (filteredRows.length > 0 && !auditBiHasDivergenceValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE, false);
        return;
      }
      var carrierRows = auditBiAggregateCarrierDivergence(filteredRows).slice(0, AUDIT_BI_TOP_N);
      if (!carrierRows.length) {
        auditBiSetCardEmpty(chartKey, emptyMessage, false);
        return;
      }
      auditBiSetCardEmpty(chartKey, '', true);
      if (noteEl) noteEl.textContent = 'Top ' + carrierRows.length + ' transportadoras por impacto financeiro absoluto.';
      auditBiRenderCarrierDivergenceChart(carrierRows);
      return;
    }
    if (chartKey === 'uf_destino') {
      if (auditBiDashboardState.fieldPresence.destination_uf === false ||
          (filteredRows.length > 0 && !auditBiHasDestinationUfValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_DESTINATION_UF_UNAVAILABLE_MESSAGE, false);
        return;
      }
      if (auditBiDashboardState.fieldPresence.expected_freight === false ||
          (filteredRows.length > 0 && !auditBiHasDivergenceValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE, false);
        return;
      }
      var ufDestRows = auditBiSortRows(auditBiAggregateByField(filteredRows, 'destination_uf'), 'impacto_total', 'desc').slice(0, AUDIT_BI_TOP_N);
      if (!ufDestRows.length) {
        auditBiSetCardEmpty(chartKey, emptyMessage, false);
        return;
      }
      auditBiSetCardEmpty(chartKey, '', true);
      if (noteEl) noteEl.textContent = 'Top UFs por impacto financeiro absoluto da auditoria.';
      auditBiRenderFinancialImpactBarChart(chartKey, ufDestRows);
      return;
    }
    if (chartKey === 'temporal') {
      if (auditBiDashboardState.fieldPresence.issue_date === false) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_FIELD_UNAVAILABLE_MESSAGE, false);
        return;
      }
      if (auditBiDashboardState.fieldPresence.expected_freight === false ||
          (filteredRows.length > 0 && !auditBiHasDivergenceValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE, false);
        return;
      }
      var temporalAggregated = auditBiAggregateByDate(filteredRows);
      var temporalRows = temporalAggregated.map(function (row) {
        return {
          label: auditBiSafeText(row.data) || '-',
          value: auditBiGetNumeric(row.impacto_total),
          cobrado_a_mais: auditBiGetNumeric(row.cobrado_a_mais),
          cobrado_a_menor: auditBiGetNumeric(row.cobrado_a_menor),
          impacto_total: auditBiGetNumeric(row.impacto_total)
        };
      });
      if (!temporalRows.length) {
        auditBiSetCardEmpty(chartKey, emptyMessage, false);
        return;
      }
      auditBiSetCardEmpty(chartKey, '', true);
      if (noteEl) noteEl.textContent = 'Série diária do impacto financeiro absoluto no período auditado.';
      auditBiRenderSimpleChart(
        chartKey,
        temporalRows.map(function (row) { return row.label; }),
        temporalRows.map(function (row) { return row.value; }),
        {
          type: 'line',
          datasetLabel: 'Impacto total',
          valueFormatter: auditBiFormatAbsoluteCurrency,
          nonNegativeCurrency: true,
          chartColor: '#7cc4ff',
          areaColor: 'rgba(124, 196, 255, 0.18)',
          tooltipRows: temporalRows
        }
      );
      return;
    }
    if (chartKey === 'pareto_transportadora') {
      if (auditBiDashboardState.fieldPresence.carrier === false ||
          (filteredRows.length > 0 && !auditBiHasCarrierValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_CARRIER_UNAVAILABLE_MESSAGE, false);
        return;
      }
      if (auditBiDashboardState.fieldPresence.expected_freight === false ||
          (filteredRows.length > 0 && !auditBiHasDivergenceValue(filteredRows))) {
        auditBiSetCardEmpty(chartKey, AUDIT_BI_DIVERGENCE_UNAVAILABLE_MESSAGE, false);
        return;
      }
      var paretoCarrierRows = auditBiBuildOverchargeParetoRows(filteredRows, 'carrier');
      if (!paretoCarrierRows.length) {
        auditBiSetCardEmpty(chartKey, 'Sem valor cobrado a mais por transportadora nos filtros atuais.', false);
        return;
      }
      auditBiSetCardEmpty(chartKey, '', true);
      if (noteEl) noteEl.textContent = 'Concentração do valor cobrado a mais por transportadora.';
      auditBiRenderParetoChart(chartKey, paretoCarrierRows);
    }
  }

  function auditBiRenderDashboard() {
    var filteredRows = auditBiFilteredRows();
    auditBiRenderFilterUi();
    auditBiRenderHiddenChartsUi();
    auditBiRenderExecutiveSummary(filteredRows);
    AUDIT_BI_CHART_KEYS.forEach(function (chartKey) {
      auditBiRenderChartCard(chartKey, filteredRows);
    });
  }

  function auditBiBindDashboardEvents() {
    if (auditBiDashboardState.initialized) return;
    var section = byId('agenteComparaBiSection');
    if (!section) return;
    section.addEventListener('click', function (event) {
      var target = event.target;
      if (!target || !target.closest) return;
      var hideBtn = target.closest('[data-audit-bi-hide-chart]');
      if (hideBtn) {
        auditBiHideChart(hideBtn.getAttribute('data-audit-bi-hide-chart'));
        return;
      }
      var showBtn = target.closest('[data-audit-bi-show-chart]');
      if (showBtn) {
        auditBiShowChart(showBtn.getAttribute('data-audit-bi-show-chart'));
        return;
      }
      var removeFilterBtn = target.closest('[data-audit-bi-remove-filter]');
      if (removeFilterBtn) {
        var filterKey = removeFilterBtn.getAttribute('data-audit-bi-remove-filter');
        if (filterKey && Object.prototype.hasOwnProperty.call(auditBiDashboardState.activeFilters, filterKey)) {
          auditBiDashboardState.activeFilters[filterKey] = null;
          auditBiRenderDashboard();
        }
        return;
      }
      if (target.id === 'agenteComparaBiClearFiltersBtn' || target.closest('#agenteComparaBiClearFiltersBtn')) {
        auditBiClearFilters();
        return;
      }
      if (target.id === 'agenteComparaBiShowAllChartsBtn' || target.closest('#agenteComparaBiShowAllChartsBtn')) {
        auditBiShowAllCharts();
      }
    });
    auditBiDashboardState.initialized = true;
  }

  function initAuditBiDashboard(auditBi) {
    auditBiBindDashboardEvents();
    auditBiDestroyAllCharts();
    auditBiDashboardState.activeFilters = {
      carrier: null,
      origin_uf: null,
      destination_uf: null,
      issue_date: null
    };
    var unavailableEl = byId('agenteComparaBiUnavailable');
    var dashboardEl = byId('agenteComparaBiDashboard');
    var legacyContentEl = byId('agenteComparaBiContent');
    if (!auditBi || auditBi.ready !== true || !Array.isArray(auditBi.rows) || !auditBi.rows.length) {
      auditBiDestroyAllCharts();
      if (dashboardEl) dashboardEl.hidden = true;
      if (legacyContentEl) legacyContentEl.hidden = true;
      if (unavailableEl) {
        unavailableEl.hidden = false;
        unavailableEl.textContent = (auditBi && auditBi.message) || 'Gráficos indisponíveis até o envio do arquivo auditado.';
      }
      return false;
    }
    if (unavailableEl) unavailableEl.hidden = true;
    if (legacyContentEl) legacyContentEl.hidden = true;
    if (dashboardEl) dashboardEl.hidden = false;
    auditBiDashboardState.sourceRows = auditBi.rows.slice();
    auditBiDashboardState.fieldPresence = auditBi.field_presence || {};
    auditBiDashboardState.rowCount = Number(auditBi.row_count) || auditBi.rows.length;
    auditBiRenderDashboard();
    return true;
  }

  function refreshAuditBiDashboardFromCurrentTempTable() {
    var section = byId('agenteComparaBiSection');
    if (!section || section.hidden) return;
    var latestAuditBi = currentTempTable && currentTempTable.audit_bi ? currentTempTable.audit_bi : null;
    initAuditBiDashboard(latestAuditBi);
  }

  function setCurrentTempTable(tempTable, options) {
    options = options || {};
    currentTempTable = tempTable || null;
    if (options.refreshAuditBi !== false) {
      refreshAuditBiDashboardFromCurrentTempTable();
    }
  }

  function showAuditBiSection(auditBi) {
    var section = byId('agenteComparaBiSection');
    if (!section) return false;

    var resolvedAuditBi = (currentTempTable && currentTempTable.audit_bi) || auditBi;
    var dashboardReady = initAuditBiDashboard(resolvedAuditBi);
    if (!dashboardReady) {
      section.hidden = true;
      return false;
    }

    section.hidden = false;
    section.scrollIntoView({ behavior: 'smooth', block: 'start' });

    fetch(API_AUDIT_CHAT_UNLOCK, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({})
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (res.status === 200 && res.data && res.data.ok === true && res.data.unlocked === true) {
          unlockChat();
        }
      })
      .catch(function () {
        /* chat permanece bloqueado se a liberação backend falhar */
      });

    return true;
  }

  function renderTempTableItem(tempTable) {
    var li = document.createElement('li');
    li.className = 'agente-compara-doc-item agente-compara-temp-table-item';
    li.setAttribute('data-temp-table-id', tempTable.temp_table_id || '');

    var openBtn = document.createElement('button');
    openBtn.type = 'button';
    openBtn.className = 'agente-compara-temp-table-open-btn';
    openBtn.setAttribute('aria-label', 'Abrir dados da tabela temporária');
    openBtn.addEventListener('click', function () {
      lastTempTableCardButton = openBtn;
      comparisonWizardModalSuppressed = false;
      if (isComparisonWizardFlowActive() && !isComparisonWizardComplete()) {
        markComparisonWizardEngaged();
      }
      openTempTableModal();
    });

    var ui = tempTable.ui_visibility || {};
    var name = document.createElement('div');
    name.className = 'agente-compara-doc-item-name';
    name.textContent = ui.display_name || 'Tabela temporária extraída';

    var meta = document.createElement('div');
    meta.className = 'agente-compara-doc-item-meta';
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

    var auditBi = tempTable.audit_bi || null;
    if (auditBi) {
      var actions = document.createElement('div');
      actions.className = 'agente-compara-temp-table-item-actions';
      if (auditBi.ready === true && Number(auditBi.row_count) > 0) {
        var chartBtn = document.createElement('button');
        chartBtn.type = 'button';
        chartBtn.className = 'btn btn-sm agente-compara-bi-generate-btn';
        chartBtn.textContent = 'Gerar Gráficos';
        chartBtn.addEventListener('click', function (event) {
          event.preventDefault();
          event.stopPropagation();
          showAuditBiSection(auditBi);
        });
        actions.appendChild(chartBtn);
      } else if (auditBi.ready === false) {
        var unavailable = document.createElement('span');
        unavailable.className = 'agente-compara-bi-unavailable small';
        unavailable.textContent = auditBi.message || 'Gráficos indisponíveis até o envio do arquivo auditado.';
        actions.appendChild(unavailable);
      }
      if (actions.childNodes.length > 0) {
        li.appendChild(actions);
      }
    }
    return li;
  }

function renderDocumentItem(doc) {
    var li = document.createElement('li');
    li.className = 'agente-compara-doc-item';
    li.setAttribute('data-doc-id', doc.doc_id || '');

    var main = document.createElement('div');
    main.className = 'agente-compara-doc-item-main';

    var name = document.createElement('div');
    name.className = 'agente-compara-doc-item-name';
    name.textContent = doc.display_name || doc.safe_name || 'Documento';

    var meta = document.createElement('div');
    meta.className = 'agente-compara-doc-item-meta';
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
    removeBtn.className = 'agente-compara-doc-item-remove';
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
    var btn = byId('agenteComparaClearDocuments');
    var panel = byId('agenteComparaDocumentsPanel');
    var hasComparison = !!comparisonState.comparisonId;
    if (btn) {
      btn.textContent = 'Reiniciar comparação';
      btn.setAttribute('aria-label', 'Reiniciar comparação');
      btn.style.display = hasComparison ? 'inline-flex' : 'none';
      btn.disabled = !!comparisonResetInFlight;
    }
    if (panel) {
      if (count > 0) {
        panel.classList.remove('agente-compara-documents-area-empty');
      } else {
        panel.classList.add('agente-compara-documents-area-empty');
      }
    }
  }

  function renderDocuments(documents, tempTable) {
    var list = byId('agenteComparaDocumentsList');
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
    var generation = comparisonRequestGeneration;
    var expectedComparisonId = comparisonState.comparisonId || null;
    return fetch(API_STATUS + comparisonIdentityQuery(), { method: 'GET', credentials: 'same-origin' })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (generation !== comparisonRequestGeneration) {
          return null;
        }
        var responseComparisonId = res.data && res.data.comparison
          ? (res.data.comparison.comparison_id || null)
          : null;
        if (expectedComparisonId && responseComparisonId && responseComparisonId !== expectedComparisonId) {
          return null;
        }
        // Status vazio antigo após start local: preservar comparison recém-criada.
        if (
          !expectedComparisonId &&
          comparisonState.comparisonId &&
          res.data &&
          res.data.ok === true &&
          !res.data.comparison &&
          res.data.has_active_comparison === false
        ) {
          return null;
        }
        if (expectedComparisonId && res.data && res.data.ok === true && !res.data.comparison && res.data.has_active_comparison === false) {
          // Outra aba/reset removeu a comparação: limpar UI local.
          resetAgenteComparaFrontendState();
          return res.data;
        }
        if (res.status === 401 || res.status === 403) {
          var errData = res.data || {};
          if (errData.error_code === 'franquia_blocked') {
            setError(errData);
          } else {
            setError(errData.message ? errData : friendlyError(errData));
          }
          setCurrentTempTable(null);
          resetCoveragePromptState();
          resetAuditFileStepState();
          renderDocuments([], null);
          return null;
        }
        if (!res.data || res.data.ok !== true) {
          setError(res.data || friendlyError(res.data));
          return null;
        }
        setError('');
        handleTempTableFromStatus(res.data);
        return res.data;
      })
      .catch(function () {
        if (generation !== comparisonRequestGeneration) return null;
        setError('Não foi possível carregar os documentos da sessão.');
        return null;
      });
  }

  function refreshAttachmentsAfterChat() {
    fetchDocuments();
  }

  function uploadDocument(file, carrierName, identityOverride) {
    if (!file || uploadInFlight) return Promise.resolve();
    var validation = validateCarrierNameInput(carrierName);
    if (!validation.ok) {
      setError(validation.message);
      return Promise.resolve(null);
    }
    var comparisonId = identityOverride && identityOverride.comparisonId
      ? identityOverride.comparisonId
      : comparisonState.comparisonId;
    var tableId = identityOverride && identityOverride.tableId
      ? identityOverride.tableId
      : comparisonState.activeTableId;
    var slot = identityOverride && identityOverride.slot != null
      ? identityOverride.slot
      : (activeComparisonTable() ? activeComparisonTable().slot_number : null);
    if (!comparisonId || !tableId || slot == null || slot === '') {
      setError('Identidade da comparação inconsistente. Selecione o arquivo novamente.');
      return Promise.resolve(null);
    }
    setError('');
    setUploadLoading(true);

    var formData = new FormData();
    formData.append('file', file, file.name);
    formData.append('carrier_name', validation.name);
    formData.append('comparison_id', comparisonId);
    formData.append('table_id', tableId);
    formData.append('slot', String(slot));

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
          setError(res.data || friendlyError(res.data));
          return null;
        }
        setError('');
        comparisonWizardModalSuppressed = false;
        markComparisonWizardEngaged();
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        }
        return fetchDocuments().then(function (statusData) {
          if (statusData) return statusData;
          if (res.data.temp_table) {
            handleTempTableFromStatus({
              documents: [],
              temp_table: res.data.temp_table,
              calculation_bases: res.data.calculation_bases || [],
              comparison: res.data.comparison || undefined
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
    fetch('/api/agente-compara/documents/' + encodeURIComponent(docId), {
      method: 'DELETE',
      credentials: 'same-origin'
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setError(res.data || friendlyError(res.data));
          return;
        }
        return fetchDocuments();
      })
      .catch(function () {
        setError('Não foi possível remover o documento.');
      });
  }

  function setResetConfirmError(message) {
    var el = byId('agenteComparaResetConfirmError');
    if (!el) return;
    if (message) {
      el.textContent = String(message);
      el.hidden = false;
    } else {
      el.textContent = '';
      el.hidden = true;
    }
  }

  function setResetConfirmLoading(loading) {
    var submitBtn = byId('agenteComparaResetConfirmSubmit');
    var cancelBtn = byId('agenteComparaResetConfirmCancel');
    comparisonResetInFlight = !!loading;
    if (submitBtn) {
      submitBtn.disabled = !!loading;
      submitBtn.setAttribute('aria-busy', loading ? 'true' : 'false');
      submitBtn.textContent = loading ? 'Reiniciando...' : 'Reiniciar comparação';
    }
    if (cancelBtn) cancelBtn.disabled = !!loading;
    updateClearButton(
      (byId('agenteComparaDocumentsList') && byId('agenteComparaDocumentsList').children.length) || 0
    );
  }

  function closeResetConfirmModal() {
    var modal = byId('agenteComparaResetConfirmModal');
    if (modal) modal.hidden = true;
    document.body.classList.remove('agente-compara-reset-confirm-open');
    setResetConfirmError('');
    setResetConfirmLoading(false);
  }

  function openResetConfirmModal() {
    var modal = byId('agenteComparaResetConfirmModal');
    if (!modal || comparisonResetInFlight) return;
    setResetConfirmError('');
    setResetConfirmLoading(false);
    modal.hidden = false;
    document.body.classList.add('agente-compara-reset-confirm-open');
    var cancelBtn = byId('agenteComparaResetConfirmCancel');
    if (cancelBtn) cancelBtn.focus();
  }

  function executeComparisonReset() {
    if (comparisonResetInFlight) return Promise.resolve(null);
    setResetConfirmLoading(true);
    setResetConfirmError('');
    setError('');
    var previousComparisonId = comparisonState.comparisonId || null;
    var body = {};
    if (previousComparisonId) body.comparison_id = previousComparisonId;
    return fetch(API_COMPARISON_RESET, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true || res.data.comparison_reset !== true) {
          var message = (res.data && (res.data.message || res.data.error)) ||
            'Não foi possível reiniciar a comparação.';
          setResetConfirmError(message);
          setError(res.data || friendlyError(res.data) || message);
          setResetConfirmLoading(false);
          return null;
        }
        // Só limpa frontend após sucesso confirmado.
        resetAgenteComparaFrontendState();
        closeResetConfirmModal();
        return res.data;
      })
      .catch(function () {
        setResetConfirmError('Não foi possível reiniciar a comparação. Tente novamente.');
        setError('Não foi possível reiniciar a comparação. Tente novamente.');
        setResetConfirmLoading(false);
        return null;
      });
  }

  function requestRestartComparison() {
    if (!comparisonState.comparisonId || comparisonResetInFlight) return;
    openResetConfirmModal();
  }

  function clearActiveSlotDocuments() {
    if (!comparisonState.comparisonId || !comparisonState.activeTableId) return;
    setError('');
    var body = {
      comparison_id: comparisonState.comparisonId,
      table_id: comparisonState.activeTableId
    };
    var active = activeComparisonTable();
    if (active && active.slot_number) body.slot = active.slot_number;
    fetch(API_CLEAR, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(body)
    })
      .then(function (r) {
        return r.json().then(function (data) { return { status: r.status, data: data }; });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setError(res.data || friendlyError(res.data));
          return;
        }
        return fetchDocuments();
      })
      .catch(function () {
        setError('Não foi possível remover os arquivos desta tabela.');
      });
  }

  function clearAllDocuments() {
    // Compat: ação global agora reinicia a comparação com confirmação.
    requestRestartComparison();
  }

  function positionActionsMenu() {
    var attachBtn = byId('agenteComparaAttachBtn');
    var menu = byId('agenteComparaActionsMenu');
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
    var attachBtn = byId('agenteComparaAttachBtn');
    var menu = byId('agenteComparaActionsMenu');
    var composerWrap = byId('agenteComparaComposerWrap');
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
    var menu = byId('agenteComparaActionsMenu');
    if (!menu) return;
    setActionsMenuOpen(menu.hidden);
  }

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  }

  function runAgenteComparaTypewriter(targetEl, text, options) {
    options = options || {};
    if (!targetEl || !text) {
      if (options.onComplete) options.onComplete();
      return;
    }

    if (prefersReducedMotion()) {
      targetEl.textContent = text;
      if (options.onScroll) options.onScroll();
      if (options.onComplete) options.onComplete();
      return;
    }

    targetEl.textContent = '';
    var index = 0;
    var delayMs = typeof options.delayMs === 'number' ? options.delayMs : 36;

    function typeNextChar() {
      if (index >= text.length) {
        targetEl.textContent = text;
        if (options.onScroll) options.onScroll();
        if (options.onComplete) options.onComplete();
        return;
      }
      targetEl.textContent += text.charAt(index);
      index += 1;
      if (options.onScroll) options.onScroll();
      window.setTimeout(typeNextChar, delayMs);
    }

    typeNextChar();
  }

  function runWelcomeTypewriter() {
    var welcome = byId('agenteComparaWelcome');
    if (!welcome) return;

    var text = welcome.getAttribute('data-typewriter-text') || '';
    if (!text) return;

    runAgenteComparaTypewriter(welcome, text);
  }

  function updateChatLockedUi() {
    var input = byId('agenteComparaInput');
    var composer = byId('agenteComparaComposer');
    if (!input) return;

    if (chatUnlocked) {
      input.placeholder = 'Mensagem para a Agente Compara...';
      input.removeAttribute('aria-disabled');
      input.classList.remove('agente-compara-input--locked');
      if (composer) composer.classList.remove('agente-compara-composer--chat-locked');
      return;
    }

    input.placeholder = CHAT_LOCKED_PLACEHOLDER;
    input.setAttribute('aria-disabled', 'true');
    input.classList.add('agente-compara-input--locked');
    if (composer) composer.classList.add('agente-compara-composer--chat-locked');
  }

  function unlockChat() {
    if (chatUnlocked) return;
    chatUnlocked = true;
    updateChatLockedUi();
    appendChatBubble('assistant', CHAT_UNLOCKED_MESSAGE);
    chatHistory.push({ role: 'assistant', content: CHAT_UNLOCKED_MESSAGE });
    chatHistory = trimChatHistory(chatHistory);
  }

  function appendBlockedChatGuidance() {
    var container = byId('agenteComparaMessages');
    if (!container) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-bot';
    msg.setAttribute('data-chat-role', 'assistant');
    msg.setAttribute('data-chat-blocked-guidance', 'true');

    var inner = document.createElement('div');
    inner.className = 'agente-compara-chat-msg-inner';
    msg.appendChild(inner);
    container.appendChild(msg);

    function scrollChat() {
      container.scrollTop = container.scrollHeight;
    }

    scrollChat();
    runAgenteComparaTypewriter(inner, CHAT_BLOCKED_MESSAGE, { onScroll: scrollChat });
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
    var input = byId('agenteComparaInput');
    var sendBtn = byId('agenteComparaSend');
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

  function renderAgenteComparaMarkdown(text) {
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

  function buildAgenteComparaCopyAction() {
    var actions = document.createElement('div');
    actions.className = 'agente-compara-chat-actions';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agente-compara-chat-copy-btn';
    btn.setAttribute('data-agente-compara-copy', '1');
    btn.setAttribute('aria-label', 'Copiar resposta da Agente Compara');
    btn.textContent = 'Copiar';
    actions.appendChild(btn);
    return actions;
  }

  function markAgenteComparaCopied(button, label) {
    if (!button) return;
    var original = button.getAttribute('data-copy-label') || 'Copiar';
    button.setAttribute('data-copy-label', original);
    button.textContent = label;
    if (label === 'Copiado') {
      button.classList.add('is-copied');
    } else {
      button.classList.remove('is-copied');
    }
    window.setTimeout(function () {
      button.textContent = original;
      button.classList.remove('is-copied');
    }, 1200);
  }

  function copyAgenteComparaTextToClipboard(text) {
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

  function getAgenteComparaMessageTextForCopy(msgNode) {
    if (!msgNode) return '';
    var inner = msgNode.querySelector('.agente-compara-chat-msg-inner');
    if (!inner) return '';
    // innerText preserva melhor quebras de lista/e-mail do que textContent puro.
    var text = (typeof inner.innerText === 'string' ? inner.innerText : inner.textContent) || '';
    return text.trim();
  }

  function appendChatBubble(role, textOrPayload) {
    var container = byId('agenteComparaMessages');
    if (!container) return;

    var isUser = role === 'user';
    var limitPayload = !isUser ? resolvePlanLimitPayload(textOrPayload) : null;
    var text = typeof textOrPayload === 'string'
      ? textOrPayload
      : (textOrPayload && textOrPayload.message) || '';
    if (!limitPayload && !text) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-' + (isUser ? 'user' : 'bot');
    msg.setAttribute('data-chat-role', isUser ? 'user' : 'assistant');

    var inner = document.createElement('div');
    inner.className = 'agente-compara-chat-msg-inner';
    if (isUser) {
      inner.textContent = text;
    } else if (limitPayload) {
      fillLimitMessageElement(inner, textOrPayload);
    } else {
      inner.innerHTML = renderAgenteComparaMarkdown(text);
    }
    msg.appendChild(inner);
    if (!isUser) {
      msg.appendChild(buildAgenteComparaCopyAction());
    }

    container.appendChild(msg);
    container.scrollTop = container.scrollHeight;
  }

  function setChatLoading(on) {
    var container = byId('agenteComparaMessages');
    if (!container) return;

    if (on) {
      var el = document.createElement('div');
      el.id = CHAT_LOADING_ID;
      el.className = 'agente-compara-chat-msg agente-compara-chat-msg-bot';
      el.innerHTML = '<div class="agente-compara-chat-msg-inner"><span class="spinner-border spinner-border-sm me-1"></span> Agente Compara está analisando...</div>';
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
    var input = byId('agenteComparaInput');
    if (!input || chatInFlight) return;

    var text = (input.value || '').trim();
    if (!text) return;

    if (!chatUnlocked) {
      appendBlockedChatGuidance();
      return;
    }

    input.value = '';
    appendChatBubble('user', text);

    var historyForApi = trimChatHistory(chatHistory.slice());
    var requestId = generateRequestId();
    var visualFocus = null;
    if (auditBiDashboardState && auditBiDashboardState.activeFilters) {
      var filters = auditBiDashboardState.activeFilters;
      visualFocus = {};
      if (filters.carrier) visualFocus.carrier = filters.carrier;
      if (filters.origin_uf) visualFocus.origin_uf = filters.origin_uf;
      if (filters.destination_uf) visualFocus.destination_uf = filters.destination_uf;
      if (filters.issue_date) visualFocus.issue_date = filters.issue_date;
      if (!visualFocus.carrier && !visualFocus.origin_uf && !visualFocus.destination_uf && !visualFocus.issue_date) {
        visualFocus = null;
      }
    }

    chatInFlight = true;
    setChatInputEnabled(false);
    setChatLoading(true);

    fetch(API_AUDIT_CHAT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        message: text,
        history: historyForApi,
        request_id: requestId,
        visual_focus: visualFocus
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
          if (resolvePlanLimitPayload(res.data)) {
            appendChatBubble('assistant', res.data);
          } else {
            appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          }
          return;
        }
        if (!res.data || res.data.ok !== true) {
          if (resolvePlanLimitPayload(res.data)) {
            appendChatBubble('assistant', res.data);
          } else {
            appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          }
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
    var form = byId('agenteComparaForm');
    var input = byId('agenteComparaInput');
    var sendBtn = byId('agenteComparaSend');
    var messages = byId('agenteComparaMessages');
    if (!form || !input || !sendBtn) return;

    updateChatLockedUi();

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

    if (messages && !messages.getAttribute('data-agente_compara-copy-bound')) {
      messages.setAttribute('data-agente_compara-copy-bound', '1');
      messages.addEventListener('click', function (e) {
        var target = e.target && e.target.closest
          ? e.target.closest('.agente-compara-chat-copy-btn')
          : null;
        if (!target) return;
        e.preventDefault();
        e.stopPropagation();
        var msgNode = target.closest('.agente-compara-chat-msg-bot');
        if (!msgNode || msgNode.id === CHAT_LOADING_ID) return;
        var text = getAgenteComparaMessageTextForCopy(msgNode);
        copyAgenteComparaTextToClipboard(text)
          .then(function () { markAgenteComparaCopied(target, 'Copiado'); })
          .catch(function () { markAgenteComparaCopied(target, 'Falha ao copiar'); });
      });
    }
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
    row.className = 'agente-compara-temp-table-modal-meta-row';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-temp-table-modal-meta-label';
    labelEl.textContent = label + ':';
    var valueEl = document.createElement('span');
    valueEl.className = 'agente-compara-temp-table-modal-meta-value';
    valueEl.textContent = displayFieldValue(value);
    row.appendChild(labelEl);
    row.appendChild(valueEl);
    container.appendChild(row);
  }

  function appendDetailRow(container, label, value) {
    if (!hasFieldValue(value)) return;
    var row = document.createElement('div');
    row.className = 'agente-compara-temp-table-modal-detail-row';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-temp-table-modal-detail-label';
    labelEl.textContent = label + ':';
    var valueEl = document.createElement('span');
    valueEl.className = 'agente-compara-temp-table-modal-detail-value';
    valueEl.textContent = String(value);
    row.appendChild(labelEl);
    row.appendChild(valueEl);
    container.appendChild(row);
  }

  function appendSectionTitle(container, title) {
    var heading = document.createElement('h3');
    heading.className = 'agente-compara-temp-table-modal-section-title';
    heading.textContent = title;
    container.appendChild(heading);
  }

  function appendEmptySectionMessage(container) {
    var empty = document.createElement('p');
    empty.className = 'agente-compara-temp-table-modal-empty';
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
    ul.className = 'agente-compara-temp-table-modal-list';
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

  var FREIGHT_ROUTE_DEFAULT_LABELS = {
    origin: 'Origem',
    destination: 'Destino',
    freight_type: 'Tipo',
    weight_30: 'Até 30 kg',
    weight_50: 'Até 50 kg',
    weight_70: 'Até 70 kg',
    weight_100: 'Até 100 kg',
    boarding_fee: 'Taxa embarque',
    freight_value_pct: 'Frete Valor %',
    freight_weight_kg: 'Excedente por kg',
    pedagio: 'Pedágio',
    notes: 'Observações'
  };

  var FREIGHT_ROUTE_COLUMN_ORDER = [
    'origin',
    'destination',
    'freight_type',
    'weight_30',
    'weight_50',
    'weight_70',
    'weight_100',
    'boarding_fee',
    'freight_value_pct',
    'freight_weight_kg',
    'pedagio',
    'notes'
  ];

  var FREIGHT_ROUTE_RESERVED_KEYS = {
    origin: true,
    destination: true,
    freight_type: true,
    type: true,
    weight_30: true,
    weight_30kg: true,
    weight_50: true,
    weight_50kg: true,
    weight_70: true,
    weight_70kg: true,
    weight_100: true,
    weight_100kg: true,
    boarding_fee: true,
    taxa_embarque_kg: true,
    freight_value_pct: true,
    frete_valor_pct: true,
    freight_weight_kg: true,
    frete_peso_kg: true,
    pedagio: true,
    notes: true,
    observations: true,
    observacoes: true,
    evidence_ref: true,
    confidence: true,
    column_labels: true
  };

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
    { key: 'pedagio', alt: null },
    { key: 'notes', alt: 'observations' }
  ];

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
      var row = {
        origin: hasFieldValue(route.origin) ? String(route.origin) : NOT_IDENTIFIED_LABEL,
        destination: hasFieldValue(route.destination) ? String(route.destination) : NOT_IDENTIFIED_LABEL,
        freight_type: hasFieldValue(routeType) ? String(routeType) : NOT_IDENTIFIED_LABEL
      };
      FREIGHT_ROUTE_COLUMN_ORDER.forEach(function (key) {
        if (key === 'origin' || key === 'destination' || key === 'freight_type') return;
        var value = readFreightRouteColumnValue(route, key);
        if (hasFieldValue(value)) row[key] = value;
      });
      Object.keys(route).forEach(function (key) {
        if (FREIGHT_ROUTE_RESERVED_KEYS[key] || key in row) return;
        var extraValue = readFreightRouteColumnValue(route, key);
        if (hasFieldValue(extraValue)) row[key] = extraValue;
      });
      return row;
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

    var partialRow = {
      origin: NOT_IDENTIFIED_LABEL,
      destination: NOT_IDENTIFIED_LABEL,
      freight_type: NOT_IDENTIFIED_LABEL
    };
    FREIGHT_WEIGHT_LIMITS.forEach(function (limit) {
      if (hasFieldValue(cols[limit])) partialRow['weight_' + limit] = cols[limit];
    });
    if (hasFieldValue(primaryFees.boarding_fee)) partialRow.boarding_fee = primaryFees.boarding_fee;
    if (hasFieldValue(primaryFees.freight_value_pct)) partialRow.freight_value_pct = primaryFees.freight_value_pct;
    if (hasFieldValue(primaryFees.freight_weight_kg)) partialRow.freight_weight_kg = primaryFees.freight_weight_kg;
    if (textualNotes.length) partialRow.notes = textualNotes.join('; ');
    return [partialRow];
  }

  function resolveFreightRouteRows(tempTable) {
    var freightRoutes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    if (freightRoutes.length) {
      return {
        rows: buildStructuredFreightRows(freightRoutes),
        columns: resolveFreightRouteColumns(freightRoutes),
        isPartial: false
      };
    }
    var partialRows = buildPartialFreightRows(tempTable);
    return {
      rows: partialRows,
      columns: resolvePartialFreightRouteColumns(tempTable),
      isPartial: true
    };
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

  function mergeFreightRouteColumnLabels(routes) {
    var labels = {};
    (Array.isArray(routes) ? routes : []).forEach(function (route) {
      if (!route || typeof route !== 'object' || !route.column_labels || typeof route.column_labels !== 'object') {
        return;
      }
      Object.keys(route.column_labels).forEach(function (key) {
        if (hasFieldValue(route.column_labels[key])) {
          labels[key] = String(route.column_labels[key]);
        }
      });
    });
    return labels;
  }

  function routeFieldHasVisibleValue(route, key) {
    if (!route || typeof route !== 'object') return false;
    if (hasFieldValue(route[key])) return true;
    var field = FREIGHT_ROUTE_EDIT_FIELDS.find(function (item) { return item.key === key; });
    if (field && field.alt && hasFieldValue(route[field.alt])) return true;
    return false;
  }

  function readFreightRouteColumnValue(route, key) {
    if (!route || typeof route !== 'object') return '';
    if (hasFieldValue(route[key])) return String(route[key]);
    var field = FREIGHT_ROUTE_EDIT_FIELDS.find(function (item) { return item.key === key; });
    if (field && field.alt && hasFieldValue(route[field.alt])) return String(route[field.alt]);
    return '';
  }

  function resolveFreightRouteColumnLabel(key, mergedLabels) {
    if (mergedLabels && hasFieldValue(mergedLabels[key])) return String(mergedLabels[key]);
    if (FREIGHT_ROUTE_DEFAULT_LABELS[key]) return FREIGHT_ROUTE_DEFAULT_LABELS[key];
    return String(key);
  }

  function resolveFreightRouteColumns(routes) {
    var mergedLabels = mergeFreightRouteColumnLabels(routes);
    var specs = [];
    var seen = {};
    FREIGHT_ROUTE_COLUMN_ORDER.forEach(function (key) {
      var hasValue = (Array.isArray(routes) ? routes : []).some(function (route) {
        return routeFieldHasVisibleValue(route, key);
      });
      if (!hasValue) return;
      specs.push({
        key: key,
        label: resolveFreightRouteColumnLabel(key, mergedLabels)
      });
      seen[key] = true;
    });
    (Array.isArray(routes) ? routes : []).forEach(function (route) {
      if (!route || typeof route !== 'object') return;
      Object.keys(route).forEach(function (key) {
        if (seen[key] || FREIGHT_ROUTE_RESERVED_KEYS[key]) return;
        if (!routeFieldHasVisibleValue(route, key)) return;
        specs.push({
          key: key,
          label: resolveFreightRouteColumnLabel(key, mergedLabels)
        });
        seen[key] = true;
      });
    });
    return specs;
  }

  function resolvePartialFreightRouteColumns(tempTable) {
    var weightData = getWeightColumnValues(tempTable);
    var primaryFees = getPrimaryFreightFees(tempTable.accessorial_fees);
    var specs = [];
    FREIGHT_WEIGHT_LIMITS.forEach(function (limit) {
      if (hasFieldValue(weightData.cols[limit])) {
        specs.push({ key: 'weight_' + limit, label: 'Até ' + limit + ' kg' });
      }
    });
    if (hasFieldValue(primaryFees.boarding_fee)) {
      specs.push({ key: 'boarding_fee', label: FREIGHT_ROUTE_DEFAULT_LABELS.boarding_fee });
    }
    if (hasFieldValue(primaryFees.freight_value_pct)) {
      specs.push({ key: 'freight_value_pct', label: FREIGHT_ROUTE_DEFAULT_LABELS.freight_value_pct });
    }
    if (hasFieldValue(primaryFees.freight_weight_kg)) {
      specs.push({ key: 'freight_weight_kg', label: FREIGHT_ROUTE_DEFAULT_LABELS.freight_weight_kg });
    }
    if (specs.length || weightData.notes.length) {
      specs.unshift(
        { key: 'origin', label: FREIGHT_ROUTE_DEFAULT_LABELS.origin },
        { key: 'destination', label: FREIGHT_ROUTE_DEFAULT_LABELS.destination },
        { key: 'freight_type', label: FREIGHT_ROUTE_DEFAULT_LABELS.freight_type }
      );
      if (weightData.notes.length) {
        specs.push({ key: 'notes', label: FREIGHT_ROUTE_DEFAULT_LABELS.notes });
      }
    }
    return specs;
  }

  function renderEditableFreightRoutesTable(container, tempTable) {
    var routes = Array.isArray(tempTable.freight_routes) ? tempTable.freight_routes : [];
    var columns = resolveFreightRouteColumns(routes);
    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    columns.forEach(function (column) {
      appendTableCell(headerRow, column.label, true, false);
    });
    var actionsHeader = document.createElement('th');
    actionsHeader.scope = 'col';
    actionsHeader.className = 'agente-compara-temp-table-modal-actions-col';
    actionsHeader.textContent = 'Ações';
    headerRow.appendChild(actionsHeader);
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    routes.forEach(function (route, rowIndex) {
      if (!route || typeof route !== 'object') return;
      var tr = document.createElement('tr');
      columns.forEach(function (column) {
        var field = FREIGHT_ROUTE_EDIT_FIELDS.find(function (item) { return item.key === column.key; });
        if (field) {
          appendEditableCell(tr, readFreightRouteField(route, field), function (newValue) {
            if (!currentTempTable.freight_routes[rowIndex]) return;
            writeFreightRouteField(currentTempTable.freight_routes[rowIndex], field, newValue);
          });
        } else {
          appendEditableCell(tr, readFreightRouteColumnValue(route, column.key), function (newValue) {
            if (!currentTempTable.freight_routes[rowIndex]) return;
            currentTempTable.freight_routes[rowIndex][column.key] = newValue;
          });
        }
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
    section.className = 'agente-compara-temp-table-modal-section agente-compara-temp-table-modal-freight-section';

    if (tempTableEditMode && canEditFreightRoutes(tempTable)) {
      if (!tempTable.freight_routes.length) {
        var emptyEdit = document.createElement('p');
        emptyEdit.className = 'agente-compara-temp-table-modal-empty';
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
    var columns = resolved.columns;

    if (resolved.isPartial) {
      var badgeRow = document.createElement('div');
      badgeRow.className = 'agente-compara-temp-table-modal-partial-badge-row';
      var badge = document.createElement('span');
      badge.className = 'agente-compara-temp-table-modal-partial-badge';
      badge.textContent = 'extração parcial';
      badgeRow.appendChild(badge);
      section.appendChild(badgeRow);
      var helper = document.createElement('p');
      helper.className = 'agente-compara-temp-table-modal-partial-helper';
      helper.textContent = 'Alguns vínculos de origem, destino ou tipo de frete ainda precisam de validação humana.';
      section.appendChild(helper);
    }

    if (!hasFreightRouteSourceData(tempTable) || !rows.length) {
      var empty = document.createElement('p');
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Nenhuma rota de frete identificada nesta extração.';
      section.appendChild(empty);
      container.appendChild(section);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    columns.forEach(function (column) {
      appendTableCell(headerRow, column.label, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    rows.forEach(function (row) {
      var tr = document.createElement('tr');
      columns.forEach(function (column) {
        var cellValue = row[column.key];
        var allowEmpty = column.key !== 'origin' && column.key !== 'destination' && column.key !== 'freight_type';
        appendTableCell(tr, cellValue, false, allowEmpty);
      });
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
    ctxWrap.className = 'agente-compara-temp-table-modal-freight-table-context';
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
    input.className = 'agente-compara-temp-table-modal-cell-input';
    input.value = hasFieldValue(value) ? String(value) : '';
    input.addEventListener('input', function () {
      if (typeof onChange === 'function') onChange(input.value);
    });
    td.appendChild(input);
    tr.appendChild(td);
  }

  function appendRowDeleteCell(tr, onDelete) {
    var td = document.createElement('td');
    td.className = 'agente-compara-temp-table-modal-actions-col';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agente-compara-temp-table-modal-row-delete-btn';
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
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Nenhuma linha identificada nesta tabela.';
      container.appendChild(empty);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table';

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
          colBtn.className = 'agente-compara-temp-table-modal-col-delete-btn';
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
        actionsHeader.className = 'agente-compara-temp-table-modal-actions-col';
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
            appendTableCell(tr, row[col], false, true);
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
    card.className = 'agente-compara-temp-table-modal-freight-table-card';
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
    summary.className = 'agente-compara-temp-table-modal-freight-table-summary';
    var titleText = hasFieldValue(freightTable.table_title)
      ? String(freightTable.table_title)
      : 'Tabela ' + (index + 1);
    summary.textContent = titleText;
    card.appendChild(summary);

    var cardBody = document.createElement('div');
    cardBody.className = 'agente-compara-temp-table-modal-freight-table-body';

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
    section.className = 'agente-compara-temp-table-modal-section agente-compara-temp-table-modal-freight-tables-section';

    var tables = Array.isArray(tempTable.freight_tables) ? tempTable.freight_tables : [];
    if (!tables.length) {
      var empty = document.createElement('p');
      empty.className = 'agente-compara-temp-table-modal-empty';
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
    table.className = 'agente-compara-temp-table-modal-data-table';

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
    input.className = 'agente-compara-temp-table-modal-cell-input';
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
      icon.textContent = 'âš ';
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
    select.className = 'agente-compara-temp-table-modal-cell-input';
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
      icon.textContent = 'âš ';
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
    section.className = 'agente-compara-temp-table-modal-section agente-compara-temp-table-modal-accessorial-section';

    var helper = document.createElement('p');
    helper.className = 'agente-compara-temp-table-modal-partial-helper';
    helper.textContent = 'Edite generalidades e serviços adicionais com mais espaço e salve tudo junto na revisão.';
    section.appendChild(helper);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-temp-table-modal-toolbar';
    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'agente-compara-temp-table-modal-add-btn';
    addBtn.textContent = 'Adicionar item';
    addBtn.addEventListener('click', function () {
      if (!Array.isArray(currentTempTable.accessorial_fees)) currentTempTable.accessorial_fees = [];
      currentTempTable.accessorial_fees.push({ name: '', value: '', unit: '', calculation_basis: 'não mapeado / revisar', calculation_base_id: null, calculation_base_label: null, raw_calculation_basis: '', notes: '', scope: 'general' });
      renderTempTableModalContent(currentTempTable);
    });
    actions.appendChild(addBtn);
    section.appendChild(actions);

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';
    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-data-table agente-compara-temp-table-modal-data-table-editable';

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
      else if (accessorialFeeIsExtractionHypothesis(item)) tr.className = 'accessorial-row--pending';
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
        refreshTempTableValidationErrorsAfterAccessorialEdit();
      }, 'Observações', {
        field: 'notes',
        validationError: getAccessorialFeeValidationError(feeIndex, 'notes')
      });
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
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Informações adicionais não identificadas no artefato atual.';
      container.appendChild(empty);
      return;
    }
    entries.forEach(function (entry) {
      appendDetailRow(container, entry.label, entry.value);
    });
  }

  function findConfirmedReviewTable(tableId) {
    if (!tableId || !comparisonState.comparisonId) return null;
    var tables = comparisonState.tables || [];
    for (var i = 0; i < tables.length; i++) {
      var entry = tables[i];
      if (!entry || entry.table_id !== tableId) continue;
      if (!entry.confirmed) return null;
      return entry;
    }
    return null;
  }

  function getReviewDisplayTempTable() {
    var tableId = parseReviewTableTabId(configurationReviewTab);
    if (tableId && reviewTempTablesById[tableId]) {
      return reviewTempTablesById[tableId];
    }
    return currentTempTable;
  }

  function getReviewSharedTempTable() {
    // coverage e arquivo operacional são globais da comparação (record do primary_temp_table_id).
    return reviewSharedTempTable || currentTempTable;
  }

  function renderConfigurationReviewLoading(container, label) {
    var loading = document.createElement('p');
    loading.className = 'agente-compara-temp-table-modal-processing';
    loading.setAttribute('role', 'status');
    loading.setAttribute('aria-live', 'polite');
    loading.textContent = label || 'Carregando dados da transportadora...';
    container.appendChild(loading);
  }

  function renderConfigurationReviewTaxContent(container) {
    appendSectionTitle(container, 'Impostos');
    var section = document.createElement('div');
    section.className = 'agente-compara-temp-table-modal-section agente-compara-tax-section agente-compara-tax-review-section';
    initGlobalTaxConfigFromState();
    var taxConfig = ensureGlobalTaxConfigShell();

    appendDetailRow(section, 'Incluir impostos', taxConfig.include_taxes === true ? 'Sim' : (taxConfig.include_taxes === false ? 'Não' : '—'));
    appendDetailRow(section, 'UF origem', taxConfig.origin_uf || '—');
    appendDetailRow(section, 'Cidade origem', taxConfig.origin_city || '—');
    appendDetailRow(
      section,
      'ISS (%)',
      taxConfig.iss_rate === null || taxConfig.iss_rate === undefined || taxConfig.iss_rate === ''
        ? '—'
        : String(taxConfig.iss_rate)
    );
    appendDetailRow(section, 'Confirmado', taxConfig.confirmed ? 'Sim' : 'Não');

    var selectedIds = Array.isArray(taxConfig.selected_table_ids) ? taxConfig.selected_table_ids : [];
    var selectedLabels = selectedIds.map(function (tableId) {
      var meta = (comparisonState.tables || []).find(function (item) { return item && item.table_id === tableId; });
      return reviewCarrierLabel(meta || { slot_number: null, carrier_name: '' }) + ' (' + tableId + ')';
    });
    appendDetailRow(section, 'Transportadoras selecionadas', selectedLabels.length ? selectedLabels.join(', ') : '—');

    if (taxConfig.include_taxes === true) {
      var destSection = document.createElement('div');
      destSection.className = 'agente-compara-tax-destination-ufs';
      appendSectionTitle(destSection, 'UFs de destino');
      var destinations = Array.isArray(taxConfig.destination_ufs) ? taxConfig.destination_ufs : [];
      if (!destinations.length) {
        var emptyDest = document.createElement('p');
        emptyDest.className = 'agente-compara-temp-table-modal-empty';
        emptyDest.textContent = 'Nenhuma UF de destino configurada.';
        destSection.appendChild(emptyDest);
      } else {
        destinations.forEach(function (row) {
          if (!row || typeof row !== 'object') return;
          var label = (row.uf || '—') + ' · ' + taxDestinationSourceLabel(row.source);
          appendDetailRow(destSection, label, Array.isArray(row.evidence) && row.evidence.length ? row.evidence.join(', ') : '—');
        });
      }
      section.appendChild(destSection);

      var ratesSection = document.createElement('div');
      ratesSection.className = 'agente-compara-tax-icms-rates';
      appendSectionTitle(ratesSection, 'Matriz de ICMS');
      var rates = Array.isArray(taxConfig.icms_rates) ? taxConfig.icms_rates : [];
      if (!rates.length) {
        var emptyRates = document.createElement('p');
        emptyRates.className = 'agente-compara-temp-table-modal-empty';
        emptyRates.textContent = 'Nenhuma alíquota configurada.';
        ratesSection.appendChild(emptyRates);
      } else {
        rates.forEach(function (row) {
          if (!row || typeof row !== 'object') return;
          var activeLabel = row.is_active ? 'ativa' : 'inativa';
          var rateLabel = (row.destination_uf || '—') + ' (' + activeLabel + ')';
          var rateValue = row.applied_rate === null || row.applied_rate === undefined || row.applied_rate === ''
            ? '—'
            : (String(row.applied_rate) + '%');
          if (row.user_edited) rateValue += ' · ajuste manual';
          appendDetailRow(ratesSection, rateLabel, rateValue);
        });
      }
      section.appendChild(ratesSection);
    }

    container.appendChild(section);
  }

  function renderConfigurationReviewAuditContent(container, tempTable) {
    appendSectionTitle(container, 'Arquivo para Comparação');
    var section = document.createElement('div');
    section.className = 'agente-compara-temp-table-modal-section agente-compara-audit-file-section';
    var card = document.createElement('div');
    card.className = 'agente-compara-audit-file-card';

    var intro = document.createElement('p');
    intro.className = 'agente-compara-audit-file-description';
    intro.textContent = 'Baixe o modelo, preencha com o volume real faturado e envie o arquivo operacional para preparar a comparação entre as transportadoras.';
    card.appendChild(intro);

    if (hasAuditBatch(tempTable)) {
      renderCalculationFileSummary(card, tempTable, { showProcessButton: true });
    } else {
      var empty = document.createElement('p');
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Nenhum arquivo operacional disponível para revisão.';
      card.appendChild(empty);
    }

    section.appendChild(card);
    container.appendChild(section);
  }

  function renderConfigurationReviewCarrierContent(container, tableMeta, tempTable) {
    appendSectionTitle(container, reviewCarrierLabel(tableMeta));
    if (!tempTable) {
      renderConfigurationReviewLoading(container);
      return;
    }
    var meta = document.createElement('div');
    meta.className = 'agente-compara-temp-table-modal-meta';
    appendMetaRow(meta, 'Transportadora', reviewCarrierLabel(tableMeta));
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

  function renderConfigurationReviewContent(panel) {
    var tabId = configurationReviewTab || defaultConfigurationReviewTab();
    var tableId = parseReviewTableTabId(tabId);
    if (tableId) {
      var tableMeta = findConfirmedReviewTable(tableId);
      if (!tableMeta) {
        var invalid = document.createElement('p');
        invalid.className = 'agente-compara-temp-table-modal-empty';
        invalid.textContent = 'Tabela não disponível para revisão nesta comparação.';
        panel.appendChild(invalid);
        return;
      }
      if (reviewLoadInFlightTableId === tableId && !reviewTempTablesById[tableId]) {
        renderConfigurationReviewLoading(panel);
        return;
      }
      renderConfigurationReviewCarrierContent(panel, tableMeta, reviewTempTablesById[tableId] || null);
      return;
    }
    if (tabId === 'taxes') {
      renderConfigurationReviewTaxContent(panel);
      return;
    }
    if (tabId === 'coverage') {
      var sharedCoverage = getReviewSharedTempTable();
      appendSectionTitle(panel, 'Cidades Atendidas');
      var coverageSection = document.createElement('div');
      coverageSection.className = 'agente-compara-temp-table-modal-section agente-compara-temp-table-modal-coverage-section';
      if (sharedCoverage && hasCoverageRows(sharedCoverage)) {
        renderReadonlyCoverageTable(coverageSection, sharedCoverage);
      } else {
        var emptyCoverage = document.createElement('p');
        emptyCoverage.className = 'agente-compara-temp-table-modal-empty';
        emptyCoverage.textContent = 'Nenhuma cidade atendida disponível para revisão.';
        coverageSection.appendChild(emptyCoverage);
      }
      panel.appendChild(coverageSection);
      return;
    }
    renderConfigurationReviewAuditContent(panel, getReviewSharedTempTable());
  }

  function renderConfigurationReviewTabs(container) {
    var tabs = document.createElement('div');
    tabs.className = 'agente-compara-temp-table-modal-tabs';
    tabs.setAttribute('role', 'tablist');
    tabs.setAttribute('data-review-mode', 'configuration-ready');

    function makeReviewTab(id, label, active, disabled) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-temp-table-modal-tab' + (active ? ' is-active' : '');
      btn.id = id;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.textContent = label;
      if (disabled) {
        btn.disabled = true;
        btn.setAttribute('aria-busy', 'true');
      }
      return btn;
    }

    var activeTab = configurationReviewTab || defaultConfigurationReviewTab();
    var confirmed = confirmedComparisonTablesForReview();
    confirmed.forEach(function (tableMeta) {
      if (!tableMeta || !tableMeta.table_id) return;
      var tabKey = reviewTableTabId(tableMeta.table_id);
      var loadingThis = reviewLoadInFlightTableId === tableMeta.table_id && activeTab === tabKey;
      var tabBtn = makeReviewTab(
        'agenteComparaReviewTab_' + tableMeta.table_id,
        reviewCarrierLabel(tableMeta),
        activeTab === tabKey,
        loadingThis
      );
      tabBtn.setAttribute('data-review-tab', tabKey);
      tabBtn.setAttribute('data-comparison-id', comparisonState.comparisonId || '');
      tabBtn.setAttribute('data-table-id', tableMeta.table_id);
      tabBtn.setAttribute('data-slot-number', String(tableMeta.slot_number || ''));
      if (tableMeta.temp_table_id) {
        tabBtn.setAttribute('data-temp-table-id', tableMeta.temp_table_id);
      }
      tabBtn.setAttribute('data-carrier-name', tableCarrierDisplay(tableMeta));
      tabBtn.addEventListener('click', function () {
        selectConfigurationReviewTab(tabKey);
      });
      tabs.appendChild(tabBtn);
    });

    var taxesTab = makeReviewTab('agenteComparaReviewTabTaxes', 'Impostos', activeTab === 'taxes', false);
    taxesTab.setAttribute('data-review-tab', 'taxes');
    taxesTab.addEventListener('click', function () {
      selectConfigurationReviewTab('taxes');
    });
    tabs.appendChild(taxesTab);

    var coverageTab = makeReviewTab('agenteComparaReviewTabCoverage', 'Cidades Atendidas', activeTab === 'coverage', false);
    coverageTab.setAttribute('data-review-tab', 'coverage');
    coverageTab.addEventListener('click', function () {
      selectConfigurationReviewTab('coverage');
    });
    tabs.appendChild(coverageTab);

    var fileTab = makeReviewTab('agenteComparaReviewTabComparisonFile', 'Arquivo para Comparação', activeTab === 'comparison_file', false);
    fileTab.setAttribute('data-review-tab', 'comparison_file');
    fileTab.addEventListener('click', function () {
      selectConfigurationReviewTab('comparison_file');
    });
    tabs.appendChild(fileTab);

    container.appendChild(tabs);
  }

  function loadReviewTempTable(tableId) {
    var tableMeta = findConfirmedReviewTable(tableId);
    if (!tableMeta) {
      return Promise.resolve({ ok: false, reason: 'not_found' });
    }
    if (reviewTempTablesById[tableId]) {
      return Promise.resolve({ ok: true, temp_table: reviewTempTablesById[tableId], fromCache: true });
    }
    var token = ++reviewLoadToken;
    var generation = comparisonRequestGeneration;
    var expectedComparisonId = comparisonState.comparisonId || null;
    reviewLoadInFlightTableId = tableId;
    var params = [];
    if (comparisonState.comparisonId) {
      params.push('comparison_id=' + encodeURIComponent(comparisonState.comparisonId));
    }
    params.push('table_id=' + encodeURIComponent(tableId));
    return fetch(API_STATUS + '?' + params.join('&'), {
      method: 'GET',
      credentials: 'same-origin'
    })
      .then(function (r) { return r.json().then(function (data) { return { status: r.status, data: data }; }); })
      .then(function (res) {
        if (token !== reviewLoadToken || generation !== comparisonRequestGeneration) {
          return { ok: false, reason: 'stale' };
        }
        if (expectedComparisonId && comparisonState.comparisonId !== expectedComparisonId) {
          return { ok: false, reason: 'stale' };
        }
        if (!comparisonState.comparisonId) {
          return { ok: false, reason: 'stale' };
        }
        if (parseReviewTableTabId(configurationReviewTab) !== tableId) {
          return { ok: false, reason: 'tab_changed' };
        }
        if (!res.data || res.data.ok !== true || !res.data.temp_table) {
          return { ok: false, reason: 'load_failed', data: res.data };
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        }
        if (String(comparisonState.currentStep || '') !== 'CONFIGURATION_READY') {
          return { ok: false, reason: 'step_changed' };
        }
        var loadedMeta = findConfirmedReviewTable(tableId);
        if (!loadedMeta) {
          return { ok: false, reason: 'ownership' };
        }
        if (loadedMeta.temp_table_id && res.data.temp_table.temp_table_id &&
            loadedMeta.temp_table_id !== res.data.temp_table.temp_table_id) {
          return { ok: false, reason: 'temp_mismatch' };
        }
        reviewTempTablesById[tableId] = res.data.temp_table;
        if (reviewLoadInFlightTableId === tableId) {
          reviewLoadInFlightTableId = null;
        }
        return { ok: true, temp_table: res.data.temp_table, fromCache: false };
      })
      .catch(function () {
        if (token === reviewLoadToken && reviewLoadInFlightTableId === tableId) {
          reviewLoadInFlightTableId = null;
        }
        return { ok: false, reason: 'network' };
      });
  }

  function selectConfigurationReviewTab(tabId, options) {
    options = options || {};
    if (!isComparisonConfigurationReady()) return;
    var nextTab = tabId || defaultConfigurationReviewTab();
    configurationReviewTab = nextTab;
    tempTableModalActiveTab = 'configuration_review';
    setComparisonCommonParamsModalHeader();

    var tableId = parseReviewTableTabId(nextTab);
    if (!tableId) {
      reviewLoadInFlightTableId = null;
      renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
      updateTempTableModalFooter();
      return;
    }

    var tableMeta = findConfirmedReviewTable(tableId);
    if (!tableMeta) {
      configurationReviewTab = 'comparison_file';
      renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
      updateTempTableModalFooter();
      return;
    }

    if (options.preferCache && reviewTempTablesById[tableId]) {
      renderTempTableModalContent(reviewTempTablesById[tableId]);
      updateTempTableModalFooter();
      return;
    }

    if (reviewTempTablesById[tableId] && !options.forceReload) {
      renderTempTableModalContent(reviewTempTablesById[tableId]);
      updateTempTableModalFooter();
      return;
    }

    // Evita mostrar dados da aba anterior como se fossem da nova durante o lazy load.
    reviewLoadInFlightTableId = tableId;
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    loadReviewTempTable(tableId).then(function (result) {
      if (!result || !result.ok) {
        if (result && (result.reason === 'stale' || result.reason === 'tab_changed')) return;
        if (parseReviewTableTabId(configurationReviewTab) !== tableId) return;
        if (reviewLoadInFlightTableId === tableId) reviewLoadInFlightTableId = null;
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
        return;
      }
      if (parseReviewTableTabId(configurationReviewTab) !== tableId) return;
      renderTempTableModalContent(result.temp_table);
      updateTempTableModalFooter();
    });
  }

  function renderTempTableModalTabs(container, tempTable) {
    if (isComparisonConfigurationReady()) {
      renderConfigurationReviewTabs(container);
      return;
    }

    var tabs = document.createElement('div');
    tabs.className = 'agente-compara-temp-table-modal-tabs';
    tabs.setAttribute('role', 'tablist');

    function makeTab(id, label, active) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-temp-table-modal-tab' + (active ? ' is-active' : '');
      btn.id = id;
      btn.setAttribute('role', 'tab');
      btn.setAttribute('aria-selected', active ? 'true' : 'false');
      btn.textContent = label;
      return btn;
    }

    var freightTab = makeTab('agenteComparaTempTableTabFreight', 'Tabela de frete', tempTableModalActiveTab === 'freight');
    freightTab.addEventListener('click', function () {
      tempTableModalActiveTab = 'freight';
      renderTempTableModalContent(tempTable);
      updateTempTableModalFooter();
    });

    var taxTab = makeTab('agenteComparaTempTableTabTaxes', 'Impostos', tempTableModalActiveTab === 'taxes');
    taxTab.addEventListener('click', function () {
      tempTableModalActiveTab = 'taxes';
      renderTempTableModalContent(tempTable);
      updateTempTableModalFooter();
    });

    var coverageTab = makeTab('agenteComparaTempTableTabCoverage', 'Cidades atendidas', tempTableModalActiveTab === 'coverage');
    coverageTab.addEventListener('click', function () {
      tempTableModalActiveTab = 'coverage';
      renderTempTableModalContent(tempTable);
      updateTempTableModalFooter();
    });

    tabs.appendChild(freightTab);
    if (shouldShowTaxTab(tempTable)) {
      tabs.appendChild(taxTab);
    }
    if (shouldShowCoverageTab(tempTable)) {
      tabs.appendChild(coverageTab);
    }
    if (shouldShowAuditTab(tempTable)) {
      var auditLabel = 'Arquivo para Comparação';
      var auditTab = makeTab('agenteComparaTempTableTabAudit', auditLabel, tempTableModalActiveTab === 'audit');
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
    if (isComparisonConfigurationReady()) {
      renderConfigurationReviewAuditContent(container, tempTable || getReviewSharedTempTable());
      return;
    }

    appendSectionTitle(container, 'Arquivo para Comparação');

    var section = document.createElement('div');
    section.className = 'agente-compara-temp-table-modal-section agente-compara-audit-file-section';

    var card = document.createElement('div');
    card.className = 'agente-compara-audit-file-card';

    var intro = document.createElement('p');
    intro.className = 'agente-compara-audit-file-description';
    intro.textContent = isComparisonConfigurationFlow()
      ? 'Baixe o modelo, preencha com o volume real faturado e envie o arquivo operacional para preparar a comparação entre as transportadoras.'
      : 'Baixe o modelo, preencha com o volume real faturado e envie o arquivo operacional para comparação.';
    card.appendChild(intro);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-audit-file-actions';

    var downloadBtn = document.createElement('a');
    downloadBtn.className = 'agente-compara-audit-file-download-btn';
    downloadBtn.href = API_AUDIT_TEMPLATE;
    downloadBtn.setAttribute('download', '');
    downloadBtn.textContent = 'Baixar modelo';
    actions.appendChild(downloadBtn);

    var fileInput = document.createElement('input');
    fileInput.type = 'file';
    fileInput.id = 'agenteComparaAuditFileInput';
    fileInput.className = 'visually-hidden';
    fileInput.accept = '.csv,.xlsx';
    fileInput.setAttribute('tabindex', '-1');
    fileInput.setAttribute('aria-hidden', 'true');

    var uploadLabel = document.createElement('label');
    uploadLabel.className = 'agente-compara-audit-file-upload-btn';
    uploadLabel.setAttribute('for', 'agenteComparaAuditFileInput');
    uploadLabel.textContent = 'Enviar arquivo preenchido';
    actions.appendChild(uploadLabel);
    actions.appendChild(fileInput);
    card.appendChild(actions);

    var fileName = document.createElement('span');
    fileName.className = 'agente-compara-audit-file-name';
    fileName.id = 'agenteComparaAuditUploadFileName';
    fileName.textContent = 'Nenhum arquivo selecionado';
    card.appendChild(fileName);

    var status = document.createElement('p');
    status.className = 'agente-compara-audit-file-status';
    status.id = 'agenteComparaAuditUploadStatus';
    status.setAttribute('role', 'status');
    status.setAttribute('aria-live', 'polite');
    card.appendChild(status);

    if (hasAuditBatch(tempTable)) {
      var batch = tempTable.audit_batch;
      renderCalculationFileSummary(card, tempTable, {
        showProcessButton: isComparisonPostConfigStep()
      });

      if (!isComparisonConfigurationFlow()) {
        var runActions = document.createElement('div');
        runActions.className = 'agente-compara-run-actions';
        var runBtn = document.createElement('button');
        runBtn.type = 'button';
        runBtn.className = 'agente-compara-run-btn';
        runBtn.id = 'agenteComparaRunButton';
        runBtn.textContent = batch.summary ? 'Processar novamente' : 'Processar comparação';
        runBtn.disabled = auditRunInFlight;
        runBtn.addEventListener('click', function () {
          runAuditProcessing();
        });
        runActions.appendChild(runBtn);
        card.appendChild(runActions);

        var runStatus = document.createElement('p');
        runStatus.className = 'agente-compara-run-status';
        runStatus.id = 'agenteComparaRunStatus';
        runStatus.setAttribute('role', 'status');
        runStatus.setAttribute('aria-live', 'polite');
        card.appendChild(runStatus);

        renderAuditRunSummary(card, batch.summary);
        renderAuditGlobalErrorButton(card, batch.audit_diagnostics);
        renderAuditDiagnostics(card, batch.audit_diagnostics);
        renderLegacyAuditDiagnosticsNotice(card, batch);
        renderAuditRunResults(card, batch.results, batch.audit_diagnostics);
      }
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
    var el = byId('agenteComparaAuditUploadFileName');
    if (el) el.textContent = name || 'Nenhum arquivo selecionado';
  }

  function setAuditUploadStatus(messageOrPayload, state) {
    var el = byId('agenteComparaAuditUploadStatus');
    if (!el) return;
    if (!messageOrPayload) {
      el.replaceChildren();
    } else if (typeof messageOrPayload === 'object') {
      if (resolvePlanLimitPayload(messageOrPayload)) {
        fillLimitMessageElement(el, messageOrPayload);
      } else {
        fillLimitMessageElement(el, messageOrPayload.message || friendlyError(messageOrPayload));
      }
    } else {
      fillLimitMessageElement(el, messageOrPayload);
    }
    el.className = 'agente-compara-audit-file-status';
    if (state === 'loading') {
      el.classList.add('is-loading');
    } else if (state === 'success') {
      el.classList.add('is-success');
    } else if (state === 'error') {
      el.classList.add('is-error');
    }
  }

  function setAuditRunStatus(messageOrPayload, state) {
    var el = byId('agenteComparaRunStatus');
    if (!el) return;
    if (!messageOrPayload) {
      el.replaceChildren();
    } else if (typeof messageOrPayload === 'object') {
      if (resolvePlanLimitPayload(messageOrPayload)) {
        fillLimitMessageElement(el, messageOrPayload);
      } else {
        fillLimitMessageElement(el, messageOrPayload.message || friendlyError(messageOrPayload));
      }
    } else {
      fillLimitMessageElement(el, messageOrPayload);
    }
    el.className = 'agente-compara-run-status';
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
            res.data || 'Não foi possível enviar o arquivo. Verifique o formato.',
            'error'
          );
          return;
        }
        return fetchDocuments().then(function (statusData) {
          if (statusData) return statusData;
          if (res.data.temp_table) {
            setCurrentTempTable(res.data.temp_table);
          }
          return res.data;
        }).then(function () {
          if (res.data.comparison) {
            syncComparisonStateFromPayload(res.data.comparison);
          } else if (res.data.temp_table && res.data.temp_table.comparison) {
            syncComparisonStateFromPayload(res.data.temp_table.comparison);
          }
          if (isComparisonConfigurationReady()) {
            // Libera o flag antes do render para o botão visual aparecer (ainda disabled).
            auditUploadInFlight = false;
            activateComparisonCommonParamsStep('CONFIGURATION_READY');
            selectConfigurationReviewTab('comparison_file', { preferCache: true });
            return;
          }
          if (hasAuditBatch(currentTempTable)) {
            auditUploadInFlight = false;
            auditFileStepActive = true;
            tempTableModalActiveTab = 'audit';
            setAuditUploadStatus('Arquivo recebido para comparação.', 'success');
            renderTempTableModalContent(currentTempTable);
            updateTempTableModalFooter();
          } else {
            setAuditUploadStatus('Não foi possível registrar o arquivo operacional.', 'error');
          }
        });
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

  function formatAuditMoneyWithCurrency(value) {
    var formatted = formatAuditMoney(value);
    if (formatted === '—') return formatted;
    return 'R$ ' + formatted;
  }

  function auditMemoryDisplayText(value) {
    if (value === null || value === undefined) return '';
    if (typeof value === 'object') return '';
    var text = String(value).trim();
    if (!text || text === 'undefined' || text === 'null' || text === '[object Object]') return '';
    return text;
  }

  function auditRowBasisText(row) {
    var parts = [];
    var basis = auditMemoryDisplayText(row && row.calculation_basis);
    var details = auditMemoryDisplayText(row && row.calculation_details);
    if (basis) parts.push(basis);
    if (details && details !== basis) parts.push(details);
    return parts.join(' — ');
  }

  function auditComponentBasisText(component, row) {
    if (!component || typeof component !== 'object') return auditRowBasisText(row);
    var details = auditMemoryDisplayText(component.details);
    if (details) return details;
    var basis = auditMemoryDisplayText(component.basis);
    if (basis) return basis;
    var calcBasis = auditMemoryDisplayText(component.calculation_basis);
    if (calcBasis) return calcBasis;
    return auditRowBasisText(row);
  }

  function auditIgnoredReasonText(item) {
    if (!item || typeof item !== 'object') return '';
    var details = auditMemoryDisplayText(item.details);
    if (details) return 'Ignorado — ' + details;
    var reasonCode = auditMemoryDisplayText(item.reason_code);
    if (!reasonCode) return '';
    return 'Ignorado — ' + reasonCode.replace(/_/g, ' ');
  }

  function formatAuditPercent(value) {
    if (!hasFieldValue(value)) return '—';
    var n = Number(value);
    if (!isFinite(n)) return String(value);
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) + '%';
  }

  function auditDetailsWithoutTaxLines(details) {
    var text = auditMemoryDisplayText(details);
    if (!text) return '';
    return text.split(' | ').filter(function (part) {
      var trimmed = part.trim();
      if (!trimmed) return false;
      if (trimmed.indexOf('Subtotal antes dos impostos:') === 0) return false;
      if (trimmed.indexOf('ICMS:') === 0) return false;
      if (trimmed.indexOf('ICMS por dentro:') === 0) return false;
      if (trimmed.indexOf('ISS:') === 0) return false;
      if (trimmed.indexOf('ISS por dentro:') === 0) return false;
      if (trimmed.indexOf('Fonte:') === 0) return false;
      if (trimmed === 'Alíquota editada pelo usuário.') return false;
      if (trimmed.indexOf('Total esperado com impostos:') === 0) return false;
      if (trimmed.indexOf('ISS não aplicado nesta linha:') === 0) return false;
      if (trimmed.indexOf('Base parcial:') === 0) return false;
      return true;
    }).join(' — ');
  }

  function auditRowBasisTextWithoutTax(row) {
    if (!row || typeof row !== 'object') return '';
    var basis = auditMemoryDisplayText(row.calculation_basis);
    var details = auditDetailsWithoutTaxLines(row.calculation_details);
    var parts = [];
    if (basis) parts.push(basis);
    if (details && details !== basis) parts.push(details);
    return parts.join(' — ');
  }

  function hasAppliedTaxComponents(components) {
    if (!components || typeof components !== 'object') return false;
    if (!Array.isArray(components.tax_components)) return false;
    return components.tax_components.some(function (item) {
      if (!item || typeof item !== 'object' || item.applied !== true) return false;
      if (!hasFieldValue(item.amount)) return false;
      return Number(item.amount) > 0;
    });
  }

  function auditDiscreteIgnoredTaxNotes(row, components) {
    var notes = [];
    if (components && Array.isArray(components.tax_components)) {
      components.tax_components.forEach(function (item) {
        if (!item || typeof item !== 'object' || item.applied === true) return;
        var reason = auditMemoryDisplayText(item.ignored_reason);
        if (!reason) return;
        var taxType = auditMemoryDisplayText(item.tax_type) || 'Imposto';
        notes.push({
          component: taxType,
          basis: reason,
          amount: null,
          ignored: true
        });
      });
    }
    var details = auditMemoryDisplayText(row && row.calculation_details);
    if (details && details.indexOf('ISS não aplicado nesta linha:') >= 0) {
      var issNote = details.split(' | ').filter(function (part) {
        return part.trim().indexOf('ISS não aplicado nesta linha:') === 0;
      })[0];
      if (issNote) {
        var alreadyListed = notes.some(function (note) {
          return note.component === 'ISS' && note.basis === issNote.trim();
        });
        if (!alreadyListed) {
          notes.push({
            component: 'ISS',
            basis: issNote.trim(),
            amount: null,
            ignored: true
          });
        }
      }
    }
    return notes;
  }

  function auditTaxPartialBaseNote(row) {
    var details = auditMemoryDisplayText(row && row.calculation_details);
    if (!details) return '';
    var note = details.split(' | ').filter(function (part) {
      return part.trim().indexOf('Base parcial:') === 0;
    })[0];
    return note ? note.trim() : '';
  }

  function buildAuditTaxBasisText(taxItem) {
    if (!taxItem || typeof taxItem !== 'object') return '';
    if (taxItem.calculation_mode === 'inside') {
      var parts = [];
      if (hasFieldValue(taxItem.rate) && hasFieldValue(taxItem.base_amount)) {
        parts.push(
          formatAuditPercent(taxItem.rate) + ' por dentro sobre ' + formatAuditMoneyWithCurrency(taxItem.base_amount)
        );
      }
      if (hasFieldValue(taxItem.amount)) {
        parts.push('imposto ' + formatAuditMoneyWithCurrency(taxItem.amount));
      }
      var sourceName = auditMemoryDisplayText(taxItem.source_name);
      if (sourceName) parts.push('Fonte: ' + sourceName);
      if (taxItem.user_edited === true) parts.push('Alíquota editada pelo usuário.');
      return parts.join(' — ');
    }
    var parts = [];
    if (hasFieldValue(taxItem.rate) && hasFieldValue(taxItem.base_amount)) {
      parts.push(
        formatAuditPercent(taxItem.rate) + ' sobre ' + formatAuditMoneyWithCurrency(taxItem.base_amount)
      );
    }
    var sourceName = auditMemoryDisplayText(taxItem.source_name);
    if (sourceName) parts.push('Fonte: ' + sourceName);
    if (taxItem.user_edited === true) parts.push('Alíquota editada pelo usuário.');
    return parts.join(' — ');
  }

  function buildAuditTaxMemoryRows(row, components) {
    if (!hasAppliedTaxComponents(components)) return [];
    var memoryRows = [];
    var partialNote = auditTaxPartialBaseNote(row);
    if (hasFieldValue(components.subtotal_before_taxes)) {
      memoryRows.push({
        component: 'Subtotal antes dos impostos',
        basis: partialNote || 'Soma dos componentes antes dos impostos',
        amount: components.subtotal_before_taxes,
        ignored: false
      });
    }

    components.tax_components.forEach(function (item) {
      if (!item || typeof item !== 'object' || item.applied !== true) return;
      if (!hasFieldValue(item.amount) || Number(item.amount) <= 0) return;
      var taxType = auditMemoryDisplayText(item.tax_type) || 'Imposto';
      memoryRows.push({
        component: taxType,
        basis: buildAuditTaxBasisText(item),
        amount: item.amount,
        ignored: false
      });
    });

    return memoryRows;
  }

  function hasAuditCalculationMemoryDetail(row) {
    if (!row || typeof row !== 'object') return false;
    if (auditRowBasisText(row)) return true;
    var components = row.calculation_components;
    if (!components || typeof components !== 'object') return false;
    if (components.weight_freight && typeof components.weight_freight === 'object') return true;
    if (components.freight_value && typeof components.freight_value === 'object') return true;
    if (components.tariff_freight_value && typeof components.tariff_freight_value === 'object') return true;
    if (Array.isArray(components.accessorial_fees) && components.accessorial_fees.length) return true;
    if (hasAppliedTaxComponents(components)) return true;
    if (auditDiscreteIgnoredTaxNotes(row, components).length) return true;
    if (Array.isArray(components.ignored_accessorial_fees) && components.ignored_accessorial_fees.length) {
      return components.ignored_accessorial_fees.some(function (item) {
        return !!auditIgnoredReasonText(item);
      });
    }
    return false;
  }

  function buildAuditCalculationMemoryRows(row) {
    var memoryRows = [];
    if (!row || typeof row !== 'object') return memoryRows;
    var components = row.calculation_components;
    var hasComponents = components && typeof components === 'object';

    if (hasComponents && components.weight_freight && typeof components.weight_freight === 'object') {
      memoryRows.push({
        component: 'Frete base/peso',
        basis: auditComponentBasisText(components.weight_freight, row),
        amount: components.weight_freight.amount,
        ignored: false
      });
    } else if (hasFieldValue(row.weight_freight)) {
      memoryRows.push({
        component: 'Frete base/peso',
        basis: auditRowBasisText(row),
        amount: row.weight_freight,
        ignored: false
      });
    }

    var addedFreightValue = false;

    if (hasComponents) {
      var freightValue = components.freight_value || components.tariff_freight_value;
      if (freightValue && typeof freightValue === 'object' && hasFieldValue(freightValue.amount)) {
        var freightLabel = auditMemoryDisplayText(freightValue.source_column) || 'Frete valor/ad valorem';
        memoryRows.push({
          component: freightLabel,
          basis: auditComponentBasisText(freightValue, row),
          amount: freightValue.amount,
          ignored: false
        });
        addedFreightValue = true;
      }
    }
    if (!addedFreightValue && hasFieldValue(row.freight_value_amount)) {
      memoryRows.push({
        component: 'Frete valor/ad valorem',
        basis: '',
        amount: row.freight_value_amount,
        ignored: false
      });
    }

    if (hasComponents) {
      var routeToll = components.route_toll || components.tariff_route_toll;
      if (routeToll && typeof routeToll === 'object' && hasFieldValue(routeToll.amount)) {
        memoryRows.push({
          component: auditMemoryDisplayText(routeToll.source_column) || 'Pedágio',
          basis: auditComponentBasisText(routeToll, row),
          amount: routeToll.amount,
          ignored: false
        });
      } else if (hasFieldValue(row.route_toll_amount)) {
        memoryRows.push({
          component: 'Pedágio',
          basis: '',
          amount: row.route_toll_amount,
          ignored: false
        });
      }
    }

    if (hasComponents && Array.isArray(components.accessorial_fees)) {
      components.accessorial_fees.forEach(function (item) {
        if (!item || typeof item !== 'object') return;
        var amount = hasFieldValue(item.amount) ? item.amount : item.calculated_amount;
        var label = auditMemoryDisplayText(item.label) || 'Generalidade';
        if (!hasFieldValue(amount) && !auditMemoryDisplayText(item.label)) return;
        memoryRows.push({
          component: label,
          basis: auditComponentBasisText(item, row),
          amount: amount,
          ignored: false
        });
      });
    }

    if (hasComponents && Array.isArray(components.ignored_accessorial_fees)) {
      components.ignored_accessorial_fees.forEach(function (item) {
        var reason = auditIgnoredReasonText(item);
        if (!reason) return;
        memoryRows.push({
          component: auditMemoryDisplayText(item && item.label) || 'Generalidade',
          basis: reason,
          amount: null,
          ignored: true
        });
      });
    }

    if (hasComponents) {
      memoryRows = memoryRows.concat(buildAuditTaxMemoryRows(row, components));
      if (!hasAppliedTaxComponents(components)) {
        auditDiscreteIgnoredTaxNotes(row, components).forEach(function (note) {
          memoryRows.push(note);
        });
      }
    }

    return memoryRows;
  }

  var auditCalculationMemoryModalEl = null;
  var auditCalculationMemoryEscapeHandler = null;

  function ensureAuditCalculationMemoryModal() {
    if (auditCalculationMemoryModalEl) return auditCalculationMemoryModalEl;

    var modal = document.createElement('div');
    modal.className = 'agente-compara-temp-table-modal agente-compara-calculation-memory-modal';
    modal.id = 'agenteComparaCalculationMemoryModal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'agenteComparaCalculationMemoryModalTitle');
    modal.hidden = true;

    var backdrop = document.createElement('div');
    backdrop.className = 'agente-compara-temp-table-modal-backdrop';
    backdrop.id = 'agenteComparaCalculationMemoryModalBackdrop';

    var dialog = document.createElement('div');
    dialog.className = 'agente-compara-temp-table-modal-dialog';

    var header = document.createElement('div');
    header.className = 'agente-compara-temp-table-modal-header';

    var headerMain = document.createElement('div');
    headerMain.className = 'agente-compara-temp-table-modal-header-main';

    var title = document.createElement('h2');
    title.className = 'agente-compara-temp-table-modal-title';
    title.id = 'agenteComparaCalculationMemoryModalTitle';
    title.textContent = 'Memória de cálculo';

    var subtitle = document.createElement('p');
    subtitle.className = 'agente-compara-temp-table-modal-subtitle';
    subtitle.id = 'agenteComparaCalculationMemoryModalSubtitle';
    subtitle.textContent = 'Detalhamento do frete esperado da linha selecionada.';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'agente-compara-temp-table-modal-close-btn';
    closeBtn.id = 'agenteComparaCalculationMemoryModalClose';
    closeBtn.setAttribute('aria-label', 'Fechar memória de cálculo');
    closeBtn.innerHTML = '<span aria-hidden="true">&times;</span>';

    headerMain.appendChild(title);
    headerMain.appendChild(subtitle);
    header.appendChild(headerMain);
    header.appendChild(closeBtn);

    var body = document.createElement('div');
    body.className = 'agente-compara-temp-table-modal-body';
    body.id = 'agenteComparaCalculationMemoryModalBody';

    dialog.appendChild(header);
    dialog.appendChild(body);
    modal.appendChild(backdrop);
    modal.appendChild(dialog);
    document.body.appendChild(modal);

    closeBtn.addEventListener('click', closeAuditCalculationMemory);
    backdrop.addEventListener('click', closeAuditCalculationMemory);

    auditCalculationMemoryModalEl = modal;
    return modal;
  }

  function isAuditCalculationMemoryModalOpen() {
    return !!(auditCalculationMemoryModalEl && !auditCalculationMemoryModalEl.hidden);
  }

  function closeAuditCalculationMemory() {
    if (!auditCalculationMemoryModalEl || auditCalculationMemoryModalEl.hidden) return;
    auditCalculationMemoryModalEl.hidden = true;
    var body = byId('agenteComparaCalculationMemoryModalBody');
    if (body) body.textContent = '';
    if (auditCalculationMemoryEscapeHandler) {
      document.removeEventListener('keydown', auditCalculationMemoryEscapeHandler, true);
      auditCalculationMemoryEscapeHandler = null;
    }
  }

  function renderAuditCalculationMemoryContent(row) {
    var body = byId('agenteComparaCalculationMemoryModalBody');
    if (!body) return;

    var summary = document.createElement('div');
    summary.className = 'agente-compara-calculation-memory-summary';
    appendDetailRow(summary, 'Documento', auditMemoryDisplayText(row.numero_documento) || '—');
    var locationParts = [
      auditMemoryDisplayText(row.destination_uf),
      auditMemoryDisplayText(row.destination_city),
      auditMemoryDisplayText(row.freight_region)
    ].filter(function (part) { return !!part; });
    appendDetailRow(summary, 'Destino', locationParts.length ? locationParts.join(' / ') : '—');
    appendDetailRow(summary, 'Peso', hasFieldValue(row.audited_weight) ? String(row.audited_weight) : '—');
    appendDetailRow(summary, 'Cobrado', formatAuditMoneyWithCurrency(row.charged_freight));
    appendDetailRow(summary, 'Esperado', formatAuditMoneyWithCurrency(row.expected_freight));
    appendDetailRow(summary, 'Diferença', formatAuditMoneyWithCurrency(row.divergence_value));
    appendDetailRow(summary, 'Status', auditStatusLabel(row.status));
    body.appendChild(summary);

    var basisText = hasAppliedTaxComponents(row.calculation_components)
      ? auditRowBasisTextWithoutTax(row)
      : auditRowBasisText(row);
    if (basisText) {
      appendDetailRow(body, 'Regra/base', basisText);
    }

    var memoryRows = buildAuditCalculationMemoryRows(row);
    if (memoryRows.length) {
      var sectionTitle = document.createElement('h3');
      sectionTitle.className = 'agente-compara-temp-table-modal-section-title';
      sectionTitle.textContent = 'Componentes';
      body.appendChild(sectionTitle);

      var scrollWrap = document.createElement('div');
      scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll agente-compara-calculation-memory-table-scroll';
      var table = document.createElement('table');
      table.className = 'agente-compara-temp-table-modal-freight-table agente-compara-calculation-memory-table';
      var thead = document.createElement('thead');
      var headerRow = document.createElement('tr');
      ['Componente', 'Cálculo/Base', 'Valor'].forEach(function (label) {
        appendTableCell(headerRow, label, true, false);
      });
      thead.appendChild(headerRow);
      table.appendChild(thead);

      var tbody = document.createElement('tbody');
      memoryRows.forEach(function (memoryRow) {
        var tr = document.createElement('tr');
        appendTableCell(tr, memoryRow.component, false, true);
        appendTableCell(tr, memoryRow.basis || (memoryRow.ignored ? 'Não aplicado' : ''), false, true);
        var valueText = memoryRow.ignored
          ? 'Ignorado'
          : formatAuditMoneyWithCurrency(memoryRow.amount);
        appendTableCell(tr, valueText, false, true);
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      scrollWrap.appendChild(table);
      body.appendChild(scrollWrap);
    } else if (!hasAuditCalculationMemoryDetail(row)) {
      var empty = document.createElement('p');
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Memória de cálculo detalhada não disponível para esta linha.';
      body.appendChild(empty);
    }

    var totalRow = document.createElement('div');
    totalRow.className = 'agente-compara-calculation-memory-total';
    var totalLabel = document.createElement('span');
    totalLabel.className = 'agente-compara-calculation-memory-total-label';
    totalLabel.textContent = hasAppliedTaxComponents(row.calculation_components)
      ? 'Total esperado com impostos:'
      : 'Total esperado:';
    var totalValue = document.createElement('strong');
    totalValue.className = 'agente-compara-calculation-memory-total-value';
    totalValue.textContent = formatAuditMoneyWithCurrency(row.expected_freight);
    totalRow.appendChild(totalLabel);
    totalRow.appendChild(totalValue);
    body.appendChild(totalRow);
  }

  function openAuditCalculationMemory(row) {
    if (!row || !hasFieldValue(row.expected_freight)) return;
    var modal = ensureAuditCalculationMemoryModal();
    var subtitle = byId('agenteComparaCalculationMemoryModalSubtitle');
    if (subtitle) {
      var doc = auditMemoryDisplayText(row.numero_documento);
      subtitle.textContent = doc
        ? 'Linha ' + String(row.row_index == null ? '—' : row.row_index) + ' — documento ' + doc
        : 'Detalhamento do frete esperado da linha selecionada.';
    }
    renderAuditCalculationMemoryContent(row);
    modal.hidden = false;
    var closeBtn = byId('agenteComparaCalculationMemoryModalClose');
    if (closeBtn && typeof closeBtn.focus === 'function') closeBtn.focus();

    if (!auditCalculationMemoryEscapeHandler) {
      auditCalculationMemoryEscapeHandler = function (e) {
        if (e.key === 'Escape' && isAuditCalculationMemoryModalOpen()) {
          e.preventDefault();
          e.stopPropagation();
          closeAuditCalculationMemory();
        }
      };
      document.addEventListener('keydown', auditCalculationMemoryEscapeHandler, true);
    }
  }

  function appendExpectedFreightCell(tr, row) {
    var td = document.createElement('td');
    if (hasFieldValue(row.expected_freight)) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-run-expected-link';
      btn.textContent = formatAuditMoney(row.expected_freight);
      btn.setAttribute('aria-label', 'Ver memória de cálculo do frete esperado');
      btn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        openAuditCalculationMemory(row);
      });
      td.appendChild(btn);
    } else {
      td.textContent = '—';
    }
    tr.appendChild(td);
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

  function auditDiagnosticsHasErrors(diagnostics) {
    if (!diagnostics || typeof diagnostics !== 'object') return false;
    return diagnostics.has_errors === true && Number(diagnostics.total_errors || 0) > 0;
  }

  function auditRowHasFailure(row) {
    if (!row || typeof row !== 'object') return false;
    var status = String(row.status || '');
    return !!status && status !== 'ok' && status !== 'divergent';
  }

  function auditBatchHasFailureResults(batch) {
    var rows = batch && Array.isArray(batch.results) ? batch.results : [];
    return rows.some(auditRowHasFailure);
  }

  function findAuditDiagnosticGroupForRow(row, diagnostics) {
    var groups = diagnostics && Array.isArray(diagnostics.groups) ? diagnostics.groups : [];
    var diagnostic = row && row.diagnostic && typeof row.diagnostic === 'object' ? row.diagnostic : null;
    var code = diagnostic && diagnostic.diagnostic_group_code ? String(diagnostic.diagnostic_group_code) : '';
    if (!groups.length || !code) return null;
    return groups.find(function (group) {
      return group && String(group.code || '') === code;
    }) || null;
  }

  function auditDiagnosticListText(values) {
    return Array.isArray(values) && values.length ? values.join(', ') : '—';
  }

  function auditCorrectionSuggestionForGroup(group) {
    var batch = currentTempTable && currentTempTable.audit_batch ? currentTempTable.audit_batch : null;
    var diagnostics = batch && batch.audit_diagnostics ? batch.audit_diagnostics : null;
    var suggestions = diagnostics && Array.isArray(diagnostics.suggestions) ? diagnostics.suggestions : [];
    if (!group || !suggestions.length) return null;
    return suggestions.find(function (suggestion) {
      return suggestion && String(suggestion.diagnostic_code || '') === String(group.code || '');
    }) || null;
  }

  function auditPreviewErrorCount(previewPart) {
    var summary = previewPart && previewPart.summary ? previewPart.summary : {};
    return Number(summary.missing_coverage_mapping || 0)
      + Number(summary.ambiguous_coverage_mapping || 0)
      + Number(summary.missing_freight_rule || 0)
      + Number(summary.invalid_rows || 0)
      + Number(summary.unsupported_pricing_model || 0);
  }

  function requestAuditCorrectionUndo(applicationId, resultContainer, button) {
    if (!applicationId) return;
    button.disabled = true;
    button.textContent = 'Desfazendo...';
    fetch(API_AUDIT_CORRECTION_UNDO, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ application_id: applicationId })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true || !res.data.temp_table) {
          resultContainer.textContent = (res.data && res.data.message) || 'Não foi possível desfazer a correção.';
          return;
        }
        setCurrentTempTable(res.data.temp_table);
        resultContainer.textContent = 'Correção desfeita. A auditoria voltou ao estado anterior.';
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
      })
      .catch(function () {
        resultContainer.textContent = 'Não foi possível desfazer a correção. Verifique sua conexão e tente novamente.';
      })
      .finally(function () {
        button.disabled = false;
        button.textContent = 'Desfazer correção';
      });
  }

  function renderAuditCorrectionUndoAction(container, applicationId) {
    if (!applicationId) return;
    var undoWrap = document.createElement('div');
    undoWrap.className = 'agente-compara-correction-choice-actions';
    var undoBtn = document.createElement('button');
    undoBtn.type = 'button';
    undoBtn.className = 'agente-compara-correction-secondary-btn';
    undoBtn.textContent = 'Desfazer correção';
    var undoStatus = document.createElement('p');
    undoStatus.className = 'agente-compara-correction-help';
    undoBtn.addEventListener('click', function () {
      requestAuditCorrectionUndo(applicationId, undoStatus, undoBtn);
    });
    undoWrap.appendChild(undoBtn);
    container.appendChild(undoWrap);
    container.appendChild(undoStatus);
  }

  function requestAuditCorrectionApply(preview, resultContainer, button) {
    if (!preview || !preview.preview_id || !preview.suggestion_id) return;
    button.disabled = true;
    button.textContent = 'Aplicando...';
    fetch(API_AUDIT_CORRECTION_APPLY, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        preview_id: preview.preview_id,
        suggestion_id: preview.suggestion_id
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true || !res.data.temp_table) {
          resultContainer.textContent = (res.data && res.data.message) || 'Não foi possível aplicar a correção.';
          return;
        }
        setCurrentTempTable(res.data.temp_table);
        resultContainer.textContent = 'Correção aplicada e auditoria reprocessada.';
        renderAuditCorrectionUndoAction(resultContainer, res.data.application_id);
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
      })
      .catch(function () {
        resultContainer.textContent = 'Não foi possível aplicar a correção. Verifique sua conexão e tente novamente.';
      })
      .finally(function () {
        button.disabled = !preview.safe_to_apply;
        button.textContent = 'Aplicar correção';
      });
  }

  function renderAuditCorrectionPreviewResult(container, preview) {
    if (!preview || typeof preview !== 'object') return;
    var panel = document.createElement('div');
    panel.className = 'agente-compara-correction-choice-panel';
    var title = document.createElement('p');
    title.className = 'agente-compara-correction-choice-title';
    title.textContent = 'Resultado da simulação';
    panel.appendChild(title);

    var transformation = preview.transformation || {};
    var params = transformation.parameters || {};
    appendDetailRow(panel, 'Transformação simulada', transformation.type || '—');
    appendDetailRow(panel, 'Coluna atual', params.current_column || '—');
    appendDetailRow(panel, 'Coluna candidata', params.candidate_column || '—');
    appendDetailRow(panel, 'Linhas analisadas', preview.before && preview.before.summary ? preview.before.summary.total_rows : '—');
    appendDetailRow(panel, 'Erros antes', auditPreviewErrorCount(preview.before));
    appendDetailRow(panel, 'Erros depois', auditPreviewErrorCount(preview.after));
    appendDetailRow(panel, 'Erros resolvidos', preview.delta ? preview.delta.resolved_errors : 0);
    appendDetailRow(panel, 'Erros restantes', preview.delta ? preview.delta.remaining_errors : 0);
    appendDetailRow(panel, 'Novas linhas calculáveis', preview.delta ? Number(preview.delta.new_ok || 0) + Number(preview.delta.new_divergent || 0) : 0);
    appendDetailRow(panel, 'Regressões', Array.isArray(preview.regressions) ? preview.regressions.length : 0);
    appendDetailRow(panel, 'Confiança', preview.confidence || '—');

    var conclusion = document.createElement('p');
    conclusion.className = 'agente-compara-correction-warning';
    conclusion.textContent = preview.safe_to_apply
      ? 'A simulação não encontrou regressões.'
      : (Array.isArray(preview.regressions) && preview.regressions.length
        ? 'A simulação criou regressões e não poderá ser aplicada.'
        : 'A simulação ainda não atende aos critérios de segurança.');
    panel.appendChild(conclusion);

    if (Array.isArray(preview.sample_changes) && preview.sample_changes.length) {
      appendDetailRow(panel, 'Amostras antes/depois', preview.sample_changes.map(function (item) {
        var before = item.before || {};
        var after = item.after || {};
        return 'Linha ' + String(after.row_index || before.row_index || '—') + ': ' + String(before.status || '—') + ' → ' + String(after.status || '—');
      }).join('; '));
    }
    if (Array.isArray(preview.remaining_errors) && preview.remaining_errors.length) {
      appendDetailRow(panel, 'Erros restantes', preview.remaining_errors.map(function (item) {
        return 'Linha ' + String(item.row_index || '—') + ': ' + String(item.status || item.reason_code || 'erro');
      }).join('; '));
    }

    var applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'agente-compara-correction-primary-btn';
    applyBtn.textContent = 'Aplicar correção';
    applyBtn.disabled = !preview.safe_to_apply;
    panel.appendChild(applyBtn);
    var applyHelp = document.createElement('p');
    applyHelp.className = 'agente-compara-correction-help';
    applyHelp.textContent = preview.safe_to_apply
      ? 'A correção será aplicada somente na tabela temporária e poderá ser desfeita.'
      : 'A aplicação será habilitada após uma simulação segura.';
    panel.appendChild(applyHelp);
    var applyStatus = document.createElement('p');
    applyStatus.className = 'agente-compara-correction-help';
    panel.appendChild(applyStatus);
    applyBtn.addEventListener('click', function () {
      requestAuditCorrectionApply(preview, applyStatus, applyBtn);
    });
    container.appendChild(panel);
  }

  function requestAuditCorrectionPreview(group, resultContainer, button) {
    var suggestion = auditCorrectionSuggestionForGroup(group);
    if (!suggestion || !suggestion.suggestion_id) {
      resultContainer.textContent = 'Nenhuma sugestão de preview está disponível para o artefato atual.';
      return;
    }
    button.disabled = true;
    button.textContent = 'Simulando...';
    resultContainer.textContent = 'Simulando correção...';
    fetch(API_AUDIT_CORRECTION_PREVIEW, {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ suggestion_id: suggestion.suggestion_id })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        resultContainer.textContent = '';
        if (!res.data || res.data.ok !== true || !res.data.preview) {
          resultContainer.textContent = (res.data && res.data.message) || 'Não foi possível simular a correção.';
          return;
        }
        renderAuditCorrectionPreviewResult(resultContainer, res.data.preview);
      })
      .catch(function () {
        resultContainer.textContent = 'Não foi possível simular a correção. Verifique sua conexão e tente novamente.';
      })
      .finally(function () {
        button.disabled = false;
        button.textContent = 'Simular correção';
      });
  }

  function scrollToAuditDiagnostics() {
    var section = byId('agenteComparaDiagnostics');
    if (!section) return;
    section.hidden = false;
    section.classList.add('is-highlighted');
    try {
      section.scrollIntoView({ behavior: 'smooth', block: 'start' });
    } catch (e) {
      section.scrollIntoView();
    }
    window.setTimeout(function () {
      section.classList.remove('is-highlighted');
    }, 1400);
  }

  function renderAuditGlobalErrorButton(container, diagnostics) {
    if (!auditDiagnosticsHasErrors(diagnostics)) return;
    var wrap = document.createElement('div');
    wrap.className = 'agente-compara-error-global-actions';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agente-compara-error-global-btn';
    btn.textContent = 'Ver erros da auditoria';
    btn.addEventListener('click', function () {
      scrollToAuditDiagnostics();
    });
    wrap.appendChild(btn);
    container.appendChild(wrap);
  }

  function renderLegacyAuditDiagnosticsNotice(container, batch) {
    if (!batch || batch.audit_diagnostics || !auditBatchHasFailureResults(batch)) return;
    var section = document.createElement('div');
    section.className = 'agente-compara-diagnostics agente-compara-diagnostics-legacy';
    var title = document.createElement('p');
    title.className = 'agente-compara-diagnostics-title';
    title.textContent = 'Diagnóstico da auditoria';
    section.appendChild(title);
    var message = document.createElement('p');
    message.className = 'agente-compara-diagnostics-subtitle';
    message.textContent = 'Este lote foi processado antes da geração do diagnóstico detalhado. Processe a auditoria novamente para analisar as causas.';
    section.appendChild(message);
    var actions = document.createElement('div');
    actions.className = 'agente-compara-error-global-actions';
    var btn = document.createElement('button');
    btn.type = 'button';
    btn.className = 'agente-compara-error-global-btn';
    btn.textContent = 'Atualizar diagnóstico';
    btn.disabled = auditRunInFlight;
    btn.addEventListener('click', function () {
      runAuditProcessing();
    });
    actions.appendChild(btn);
    section.appendChild(actions);
    container.appendChild(section);
  }

  function appendAuditSummaryItem(container, label, value) {
    var item = document.createElement('div');
    item.className = 'agente-compara-run-summary-item';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-run-summary-label';
    labelEl.textContent = label;
    var valueEl = document.createElement('strong');
    valueEl.className = 'agente-compara-run-summary-value';
    valueEl.textContent = String(value == null ? 0 : value);
    item.appendChild(labelEl);
    item.appendChild(valueEl);
    container.appendChild(item);
  }

  function renderAuditRunSummary(container, summary) {
    if (!summary || typeof summary !== 'object') return;
    var block = document.createElement('div');
    block.className = 'agente-compara-run-summary';
    var title = document.createElement('p');
    title.className = 'agente-compara-run-summary-title';
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

  function appendAuditDiagnosticValueList(container, label, values) {
    var safeValues = Array.isArray(values) ? values.filter(hasFieldValue) : [];
    if (!safeValues.length) return;
    var wrap = document.createElement('div');
    wrap.className = 'agente-compara-diagnostic-list-wrap';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-diagnostic-list-label';
    labelEl.textContent = label;
    wrap.appendChild(labelEl);
    var list = document.createElement('div');
    list.className = 'agente-compara-diagnostic-chip-list';
    safeValues.forEach(function (value) {
      var chip = document.createElement('span');
      chip.className = 'agente-compara-diagnostic-chip';
      chip.textContent = String(value);
      list.appendChild(chip);
    });
    wrap.appendChild(list);
    container.appendChild(wrap);
  }

  function diagnosticGroupTitle(group) {
    if (!group || typeof group !== 'object') return 'Diagnóstico da auditoria';
    if (hasFieldValue(group.title)) return String(group.title);
    if (group.code === 'pricing_dimension_mismatch') return 'Dimensão tarifária incompatível';
    return 'Diagnóstico da auditoria';
  }

  function renderAuditDiagnosticGroup(container, group) {
    if (!group || typeof group !== 'object') return;
    var card = document.createElement('article');
    card.className = 'agente-compara-diagnostic-card';

    var header = document.createElement('div');
    header.className = 'agente-compara-diagnostic-card-header';
    var title = document.createElement('p');
    title.className = 'agente-compara-diagnostic-card-title';
    title.textContent = diagnosticGroupTitle(group);
    header.appendChild(title);
    if (hasFieldValue(group.confidence)) {
      var confidence = document.createElement('span');
      confidence.className = 'agente-compara-diagnostic-confidence';
      confidence.textContent = String(group.confidence) === 'high' ? 'confiança alta' : String(group.confidence);
      header.appendChild(confidence);
    }
    card.appendChild(header);

    var message = document.createElement('p');
    message.className = 'agente-compara-diagnostic-message';
    message.textContent = group.message || 'A auditoria encontrou um padrão de falha que precisa de revisão na tabela registrada.';
    card.appendChild(message);

    var meta = document.createElement('div');
    meta.className = 'agente-compara-diagnostic-meta';
    appendDetailRow(meta, 'Etapa', group.failure_stage || 'pricing_rule_match');
    appendDetailRow(meta, 'Linhas afetadas', group.affected_rows != null ? String(group.affected_rows) : '');
    if (Array.isArray(group.sample_row_indexes) && group.sample_row_indexes.length) {
      appendDetailRow(meta, 'Amostra de linhas', group.sample_row_indexes.join(', '));
    }
    if (hasFieldValue(group.candidate_column)) {
      appendDetailRow(meta, 'Coluna candidata encontrada', group.candidate_column);
    }
    card.appendChild(meta);

    appendAuditDiagnosticValueList(card, 'Valores solicitados pela cobertura', group.requested_values);
    appendAuditDiagnosticValueList(card, 'Valores da dimensão tarifária atual', group.available_values);
    appendAuditDiagnosticValueList(card, 'Valores encontrados na coluna candidata', group.candidate_values);

    var actionability = group.actionability && typeof group.actionability === 'object' ? group.actionability : {};
    var note = document.createElement('p');
    note.className = 'agente-compara-diagnostic-actionability';
    note.textContent = actionability.can_apply_automatically
      ? 'Este diagnóstico é apenas informativo nesta fase. Nenhuma correção será aplicada automaticamente.'
      : 'Diagnóstico informativo: revise a tabela registrada ou os arquivos de origem. Nenhuma correção automática será aplicada nesta fase.';
    card.appendChild(note);

    if (actionability.can_review_registered_table === true) {
      var actions = document.createElement('div');
      actions.className = 'agente-compara-diagnostic-actions';
      var fixBtn = document.createElement('button');
      fixBtn.type = 'button';
      fixBtn.className = 'agente-compara-diagnostic-fix-table-btn';
      fixBtn.textContent = 'Corrigir tabela cadastrada';
      fixBtn.addEventListener('click', function () {
        openAuditCorrectionExplanation(group);
      });
      actions.appendChild(fixBtn);
      card.appendChild(actions);
    }

    container.appendChild(card);
  }

  function renderAuditDiagnostics(container, diagnostics) {
    if (!auditDiagnosticsHasErrors(diagnostics)) return;
    var groups = Array.isArray(diagnostics.groups) ? diagnostics.groups : [];
    var section = document.createElement('div');
    section.className = 'agente-compara-diagnostics';
    section.id = 'agenteComparaDiagnostics';
    var title = document.createElement('p');
    title.className = 'agente-compara-diagnostics-title';
    title.textContent = 'Diagnóstico da auditoria';
    section.appendChild(title);
    var subtitle = document.createElement('p');
    subtitle.className = 'agente-compara-diagnostics-subtitle';
    subtitle.textContent = 'A Agente Compara identificou padrões agregados nos erros do processamento. Esta fase apenas explica o problema.';
    section.appendChild(subtitle);
    if (groups.length) {
      groups.forEach(function (group) {
        renderAuditDiagnosticGroup(section, group);
      });
    } else {
      var generic = document.createElement('article');
      generic.className = 'agente-compara-diagnostic-card';
      var genericTitle = document.createElement('p');
      genericTitle.className = 'agente-compara-diagnostic-card-title';
      genericTitle.textContent = 'Erros encontrados na auditoria';
      generic.appendChild(genericTitle);
      var genericMessage = document.createElement('p');
      genericMessage.className = 'agente-compara-diagnostic-message';
      genericMessage.textContent = 'A auditoria encontrou erros no lote, mas não identificou um grupo de causa específico para exibir nesta etapa.';
      generic.appendChild(genericMessage);
      section.appendChild(generic);
    }
    container.appendChild(section);
  }

  var auditDiagnosticModalEl = null;
  var auditDiagnosticEscapeHandler = null;

  function ensureAuditDiagnosticModal() {
    if (auditDiagnosticModalEl) return auditDiagnosticModalEl;

    var modal = document.createElement('div');
    modal.className = 'agente-compara-temp-table-modal agente-compara-diagnostic-modal';
    modal.id = 'agenteComparaDiagnosticModal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'agenteComparaDiagnosticModalTitle');
    modal.hidden = true;

    var backdrop = document.createElement('div');
    backdrop.className = 'agente-compara-temp-table-modal-backdrop';
    backdrop.id = 'agenteComparaDiagnosticModalBackdrop';

    var dialog = document.createElement('div');
    dialog.className = 'agente-compara-temp-table-modal-dialog';

    var header = document.createElement('div');
    header.className = 'agente-compara-temp-table-modal-header';

    var headerMain = document.createElement('div');
    headerMain.className = 'agente-compara-temp-table-modal-header-main';

    var title = document.createElement('h2');
    title.className = 'agente-compara-temp-table-modal-title';
    title.id = 'agenteComparaDiagnosticModalTitle';
    title.textContent = 'Detalhe do erro';

    var subtitle = document.createElement('p');
    subtitle.className = 'agente-compara-temp-table-modal-subtitle';
    subtitle.id = 'agenteComparaDiagnosticModalSubtitle';
    subtitle.textContent = 'Diagnóstico explicativo da auditoria.';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'agente-compara-temp-table-modal-close-btn';
    closeBtn.id = 'agenteComparaDiagnosticModalClose';
    closeBtn.setAttribute('aria-label', 'Fechar diagnóstico');
    closeBtn.innerHTML = '<span aria-hidden="true">&times;</span>';

    headerMain.appendChild(title);
    headerMain.appendChild(subtitle);
    header.appendChild(headerMain);
    header.appendChild(closeBtn);

    var body = document.createElement('div');
    body.className = 'agente-compara-temp-table-modal-body';
    body.id = 'agenteComparaDiagnosticModalBody';

    dialog.appendChild(header);
    dialog.appendChild(body);
    modal.appendChild(backdrop);
    modal.appendChild(dialog);
    document.body.appendChild(modal);

    closeBtn.addEventListener('click', closeAuditDiagnosticModal);
    backdrop.addEventListener('click', closeAuditDiagnosticModal);

    auditDiagnosticModalEl = modal;
    return modal;
  }

  function isAuditDiagnosticModalOpen() {
    return !!(auditDiagnosticModalEl && !auditDiagnosticModalEl.hidden);
  }

  function closeAuditDiagnosticModal() {
    if (!auditDiagnosticModalEl || auditDiagnosticModalEl.hidden) return;
    auditDiagnosticModalEl.hidden = true;
    var body = byId('agenteComparaDiagnosticModalBody');
    if (body) body.textContent = '';
    if (auditDiagnosticEscapeHandler) {
      document.removeEventListener('keydown', auditDiagnosticEscapeHandler, true);
      auditDiagnosticEscapeHandler = null;
    }
  }

  function openAuditDiagnosticModal(titleText, subtitleText, renderContent) {
    var modal = ensureAuditDiagnosticModal();
    var title = byId('agenteComparaDiagnosticModalTitle');
    var subtitle = byId('agenteComparaDiagnosticModalSubtitle');
    var body = byId('agenteComparaDiagnosticModalBody');
    if (title) title.textContent = titleText || 'Diagnóstico da auditoria';
    if (subtitle) subtitle.textContent = subtitleText || 'Diagnóstico explicativo da auditoria.';
    if (body) {
      body.textContent = '';
      renderContent(body);
    }
    modal.hidden = false;
    var closeBtn = byId('agenteComparaDiagnosticModalClose');
    if (closeBtn && typeof closeBtn.focus === 'function') closeBtn.focus();

    if (!auditDiagnosticEscapeHandler) {
      auditDiagnosticEscapeHandler = function (e) {
        if (e.key === 'Escape' && isAuditDiagnosticModalOpen()) {
          e.preventDefault();
          e.stopPropagation();
          closeAuditDiagnosticModal();
        }
      };
      document.addEventListener('keydown', auditDiagnosticEscapeHandler, true);
    }
  }

  function renderLineErrorDetail(body, row, group) {
    var diagnostic = row && row.diagnostic && typeof row.diagnostic === 'object' ? row.diagnostic : {};
    var context = diagnostic.search_context && typeof diagnostic.search_context === 'object'
      ? diagnostic.search_context
      : {};
    var summary = document.createElement('div');
    summary.className = 'agente-compara-line-error-summary';
    appendDetailRow(summary, 'Documento', row.numero_documento || '—');
    appendDetailRow(summary, 'UF destino', context.destination_uf || row.destination_uf || '—');
    appendDetailRow(summary, 'Cidade destino', context.destination_city || row.destination_city || '—');
    appendDetailRow(summary, 'Classificação de cobertura identificada', context.coverage_classification || row.freight_region || '—');
    appendDetailRow(summary, 'Peso', hasFieldValue(row.audited_weight) ? String(row.audited_weight) : '—');
    appendDetailRow(summary, 'Status', auditStatusLabel(row.status));
    appendDetailRow(summary, 'Etapa da falha', diagnostic.failure_stage || (group && group.failure_stage) || '—');
    appendDetailRow(
      summary,
      'Critérios usados na busca',
      [
        context.destination_uf || row.destination_uf,
        context.destination_city || row.destination_city,
        context.coverage_classification || row.freight_region
      ].filter(hasFieldValue).join(' / ') || '—'
    );
    if (Array.isArray(diagnostic.attempted_keys) && diagnostic.attempted_keys.length) {
      appendDetailRow(summary, 'Tentativas de correspondência', diagnostic.attempted_keys.join(', '));
    }
    if (group) {
      appendDetailRow(summary, 'Valores da dimensão tarifária atual', auditDiagnosticListText(group.available_values));
      if (hasFieldValue(group.candidate_column)) {
        appendDetailRow(summary, 'Coluna candidata', group.candidate_column);
      }
      if (Array.isArray(group.candidate_values) && group.candidate_values.length) {
        appendDetailRow(summary, 'Valores da coluna candidata', auditDiagnosticListText(group.candidate_values));
      }
    }
    appendDetailRow(summary, 'Mensagem', diagnostic.message || (group && group.message) || 'A linha não pôde ser calculada com os dados registrados.');
    if (group) {
      appendDetailRow(summary, 'Causa relacionada', diagnosticGroupTitle(group));
    }
    body.appendChild(summary);
  }

  function openLineErrorDetail(row, diagnostics) {
    if (!auditRowHasFailure(row)) return;
    var group = findAuditDiagnosticGroupForRow(row, diagnostics);
    openAuditDiagnosticModal(
      'Detalhe do erro',
      'Linha ' + String(row.row_index == null ? '—' : row.row_index),
      function (body) {
        renderLineErrorDetail(body, row, group);
      }
    );
  }

  function renderCorrectionFileInstructions(body, group) {
    var info = document.createElement('div');
    info.className = 'agente-compara-correction-choice-panel';
    var title = document.createElement('p');
    title.className = 'agente-compara-correction-choice-title';
    title.textContent = 'Corrigir arquivos e refazer o upload';
    info.appendChild(title);
    var instructions = document.createElement('p');
    instructions.className = 'agente-compara-correction-choice-text';
    instructions.textContent = 'Revise os arquivos de origem para que a cobertura e a tabela de frete usem a mesma dimensão tarifária. Depois, faça o upload novamente pelo fluxo normal da auditoria.';
    info.appendChild(instructions);
    appendDetailRow(info, 'Coluna candidata', group && group.candidate_column ? group.candidate_column : '—');
    appendDetailRow(info, 'Valores atuais', group ? auditDiagnosticListText(group.available_values) : '—');
    appendDetailRow(info, 'Valores sugeridos', group ? auditDiagnosticListText(group.candidate_values) : '—');

    var actions = document.createElement('div');
    actions.className = 'agente-compara-correction-choice-actions';
    var backBtn = document.createElement('button');
    backBtn.type = 'button';
    backBtn.className = 'agente-compara-correction-secondary-btn';
    backBtn.textContent = 'Voltar';
    backBtn.addEventListener('click', function () {
      openAuditCorrectionExplanation(group);
    });
    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'agente-compara-correction-secondary-btn';
    closeBtn.textContent = 'Fechar';
    closeBtn.addEventListener('click', closeAuditDiagnosticModal);
    actions.appendChild(backBtn);
    actions.appendChild(closeBtn);
    info.appendChild(actions);
    body.appendChild(info);
  }

  function renderCorrectionExplanation(body, group) {
    var content = document.createElement('div');
    content.className = 'agente-compara-correction-explanation';
    appendDetailRow(content, 'Causa', group && group.message ? group.message : 'Dimensão tarifária incompatível.');
    appendDetailRow(content, 'Coluna candidata', group && group.candidate_column ? group.candidate_column : '—');
    appendDetailRow(content, 'Valores atuais', group ? auditDiagnosticListText(group.available_values) : '—');
    appendDetailRow(content, 'Valores sugeridos', group ? auditDiagnosticListText(group.candidate_values) : '—');
    appendDetailRow(content, 'Evidências', group ? auditDiagnosticListText(group.evidence) : '—');
    var warning = document.createElement('p');
    warning.className = 'agente-compara-correction-warning';
    warning.textContent = 'Nenhuma alteração foi aplicada.';
    content.appendChild(warning);

    var choices = document.createElement('div');
    choices.className = 'agente-compara-correction-choices';
    var previewChoice = document.createElement('button');
    previewChoice.type = 'button';
    previewChoice.className = 'agente-compara-correction-primary-btn';
    previewChoice.textContent = 'Simular correção';
    choices.appendChild(previewChoice);
    var previewHelp = document.createElement('p');
    previewHelp.className = 'agente-compara-correction-help';
    previewHelp.textContent = 'A simulação não altera os resultados atuais nem a tabela cadastrada.';
    choices.appendChild(previewHelp);

    var fileChoice = document.createElement('button');
    fileChoice.type = 'button';
    fileChoice.className = 'agente-compara-correction-secondary-btn';
    fileChoice.textContent = 'Prefiro corrigir os arquivos e refazer o upload';
    fileChoice.addEventListener('click', function () {
      openAuditDiagnosticModal(
        'Corrigir arquivos',
        'Nenhum documento será removido e nenhum estado será alterado.',
        function (fileBody) {
          renderCorrectionFileInstructions(fileBody, group);
        }
      );
    });
    choices.appendChild(fileChoice);
    content.appendChild(choices);
    var previewResult = document.createElement('div');
    previewResult.className = 'agente-compara-correction-preview-result';
    content.appendChild(previewResult);
    previewChoice.addEventListener('click', function () {
      requestAuditCorrectionPreview(group, previewResult, previewChoice);
    });
    body.appendChild(content);
  }

  function openAuditCorrectionExplanation(group) {
    openAuditDiagnosticModal(
      'Corrigir tabela cadastrada',
      'Etapa explicativa. Nenhuma correção real será aplicada.',
      function (body) {
        renderCorrectionExplanation(body, group);
      }
    );
  }

  function appendAuditRowActionCell(tr, row, diagnostics) {
    var td = document.createElement('td');
    td.className = 'agente-compara-run-actions-cell';
    if (auditRowHasFailure(row)) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-run-row-error-btn';
      btn.textContent = 'Ver erro';
      btn.addEventListener('click', function () {
        openLineErrorDetail(row, diagnostics);
      });
      td.appendChild(btn);
    } else {
      td.textContent = '—';
    }
    tr.appendChild(td);
  }

  function renderAuditRunResults(container, results, diagnostics) {
    var rows = Array.isArray(results) ? results : [];
    if (!rows.length) return;
    var section = document.createElement('div');
    section.className = 'agente-compara-run-results';
    var title = document.createElement('p');
    title.className = 'agente-compara-run-results-title';
    title.textContent = 'Resultados por linha';
    section.appendChild(title);

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll agente-compara-run-results-scroll';
    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table agente-compara-run-results-table';
    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['Linha', 'Documento', 'UF', 'Cidade', 'Região', 'Peso', 'Cobrado', 'Esperado', 'Diferença', 'Status', 'Ações'].forEach(function (label) {
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
      appendExpectedFreightCell(tr, row);
      appendTableCell(tr, formatAuditMoney(row.divergence_value), false, true);
      appendTableCell(tr, auditStatusLabel(row.status), false, false);
      appendAuditRowActionCell(tr, row, diagnostics);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    section.appendChild(scrollWrap);
    container.appendChild(section);
  }

  function runAuditProcessing() {
    if (!hasAuditBatch(currentTempTable) || auditRunInFlight) return;
    if (isComparisonConfigurationFlow()) {
      setAuditRunStatus('O cálculo comparativo ainda não foi iniciado.', 'error');
      return;
    }
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
            res.data || 'Não foi possível processar a auditoria.',
            'error'
          );
          return;
        }
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
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

  function normalizeTaxLocationText(value) {
    var text = String(value == null ? '' : value).trim();
    if (!text) return '';
    text = text.normalize('NFD').replace(/[\u0300-\u036f]/g, '');
    text = text.toUpperCase().replace(/[_\-\/.,:;]+/g, ' ').replace(/[^A-Z0-9\s]/g, ' ').replace(/\s+/g, ' ').trim();
    return text;
  }

  function resolveTaxStateNameToUf(value) {
    var key = normalizeTaxLocationText(value);
    return key ? (BR_STATE_NAME_TO_UF[key] || '') : '';
  }

  function resolveTaxCityNameToUf(value) {
    var key = normalizeTaxLocationText(value);
    if (!key) return '';
    return KNOWN_CITY_TO_UF[key] || '';
  }

  function normalizeTaxUf(value) {
    var text = String(value || '').trim().toUpperCase();
    return BRAZILIAN_UFS.indexOf(text) !== -1 ? text : '';
  }

  function extractTaxUfTokens(value) {
    var text = String(value || '').toUpperCase();
    var tokens = [];
    var parts = text.split(/[^A-Z]+/);
    parts.forEach(function (part) {
      if (BRAZILIAN_UFS.indexOf(part) !== -1 && tokens.indexOf(part) === -1) {
        tokens.push(part);
      }
    });
    return tokens;
  }

  function taxDestinationFieldKind(key) {
    var normalized = normalizeTextKey(key).replace(/\s+/g, '_');
    if (normalized === 'destination_uf' || normalized === 'uf_destino' || normalized === 'destino_uf') return 'uf';
    if (normalized.indexOf('uf_destino') !== -1 || normalized.indexOf('destination_uf') !== -1) return 'uf';
    if (normalized.indexOf('estado') !== -1 && (normalized.indexOf('destino') !== -1 || normalized.indexOf('destination') !== -1)) return 'state';
    if (normalized.indexOf('cidade') !== -1 && (normalized.indexOf('destino') !== -1 || normalized.indexOf('destination') !== -1)) return 'city';
    if (normalized.indexOf('destino') !== -1 || normalized.indexOf('destination') !== -1) {
      if (normalized.indexOf('estado') !== -1) return 'state';
      if (normalized.indexOf('uf') !== -1) return 'uf';
      return 'city';
    }
    return '';
  }

  function destinationUfFieldScore(key) {
    var kind = taxDestinationFieldKind(key);
    if (kind === 'uf') return 3;
    if (kind === 'state') return 2;
    if (kind === 'city') return 1;
    return 0;
  }

  function resolveTaxLocationFindings(value, fieldKind) {
    var text = String(value == null ? '' : value).trim();
    if (!text) return [];
    var findings = [];
    var seen = {};

    function append(uf, source) {
      if (!uf || seen[uf]) return;
      seen[uf] = true;
      findings.push({ uf: uf, source: source, evidence: text });
    }

    if (fieldKind === 'uf') {
      var directUf = normalizeTaxUf(text);
      if (directUf) append(directUf, 'automatic');
      else extractTaxUfTokens(text).forEach(function (uf) { append(uf, 'automatic'); });
      return findings;
    }

    if (fieldKind === 'state') {
      var stateUf = resolveTaxStateNameToUf(text);
      if (stateUf) append(stateUf, 'inferred_state');
      else extractTaxUfTokens(text).forEach(function (uf) { append(uf, 'automatic'); });
      return findings;
    }

    var cityUf = resolveTaxCityNameToUf(text);
    if (cityUf) {
      append(cityUf, 'inferred_city');
      return findings;
    }
    stateUf = resolveTaxStateNameToUf(text);
    if (stateUf) {
      append(stateUf, 'inferred_state');
      return findings;
    }
    extractTaxUfTokens(text).forEach(function (uf) { append(uf, 'automatic'); });
    return findings;
  }

  function collectTaxDestinationFindingsFromValue(value, findings, keyHint) {
    if (value === null || value === undefined) return;
    if (Array.isArray(value)) {
      value.forEach(function (item) {
        collectTaxDestinationFindingsFromValue(item, findings, keyHint);
      });
      return;
    }
    if (typeof value === 'object') {
      Object.keys(value).forEach(function (key) {
        var fieldKind = taxDestinationFieldKind(key);
        if (fieldKind) {
          resolveTaxLocationFindings(value[key], fieldKind).forEach(function (item) {
            findings.push(item);
          });
        }
        collectTaxDestinationFindingsFromValue(value[key], findings, key);
      });
      return;
    }
    if (keyHint) {
      var hintKind = taxDestinationFieldKind(keyHint);
      if (hintKind) {
        resolveTaxLocationFindings(value, hintKind).forEach(function (item) {
          findings.push(item);
        });
      }
    }
  }

  function mergeTaxDestinationEntry(byUf, uf, source, evidence, userConfirmed) {
    var normalizedUf = normalizeTaxUf(uf);
    if (!normalizedUf) return;
    var evidenceText = String(evidence || '').trim();
    if (!byUf[normalizedUf]) {
      byUf[normalizedUf] = {
        uf: normalizedUf,
        source: source || 'manual',
        evidence: evidenceText ? [evidenceText] : [],
        user_confirmed: !!userConfirmed
      };
      return;
    }
    var entry = byUf[normalizedUf];
    if (evidenceText && entry.evidence.indexOf(evidenceText) === -1) {
      entry.evidence.push(evidenceText);
    }
    var priority = { manual: 4, automatic: 3, inferred_state: 2, inferred_city: 1 };
    if ((priority[source] || 0) > (priority[entry.source] || 0)) {
      entry.source = source;
    }
    if (userConfirmed) entry.user_confirmed = true;
  }

  function consolidateTaxDestinationUfs(tempTable, submittedDestinationUfs) {
    var byUf = {};
    if (!submittedDestinationUfs) {
      var findings = [];
      if (tempTable) {
        ['destinations', 'routes', 'freight_routes', 'freight_tables', 'extracted_items'].forEach(function (key) {
          collectTaxDestinationFindingsFromValue(tempTable[key], findings, '');
        });
      }
      findings.forEach(function (item) {
        mergeTaxDestinationEntry(byUf, item.uf, item.source, item.evidence, false);
      });
    } else {
      (Array.isArray(submittedDestinationUfs) ? submittedDestinationUfs : []).forEach(function (item) {
        if (!item || typeof item !== 'object') return;
        var uf = normalizeTaxUf(item.uf);
        if (!uf) return;
        var evidence = Array.isArray(item.evidence) ? item.evidence : (item.evidence ? [item.evidence] : []);
        mergeTaxDestinationEntry(byUf, uf, item.source || 'manual', evidence[0] || '', !!item.user_confirmed);
        if (byUf[uf]) {
          evidence.slice(1).forEach(function (extra) {
            var text = String(extra || '').trim();
            if (text && byUf[uf].evidence.indexOf(text) === -1) byUf[uf].evidence.push(text);
          });
        }
      });
    }
    return Object.keys(byUf).sort().map(function (uf) { return byUf[uf]; });
  }

  function extractTaxDestinationUfs(tempTable) {
    return consolidateTaxDestinationUfs(tempTable).map(function (item) { return item.uf; });
  }

  function taxDestinationSourceLabel(source) {
    return TAX_DESTINATION_SOURCE_LABELS[source] || TAX_DESTINATION_SOURCE_LABELS.manual;
  }

  function syncTaxDestinationUfs(tempTable, options) {
    options = options || {};
    var taxConfig = ensureTaxConfigShell(tempTable);
    if (!options.forceRefresh && Array.isArray(taxConfig.destination_ufs) && taxConfig.destination_ufs.length) {
      return taxConfig.destination_ufs;
    }
    taxConfig.destination_ufs = consolidateTaxDestinationUfs(tempTable);
    return taxConfig.destination_ufs;
  }

  function syncTaxScenarioCommonFromTaxConfig(taxConfig) {
    if (!taxConfig || typeof taxConfig !== 'object') return;
    if (taxConfig.origin_uf) taxScenarioCommon.origin_uf = normalizeTaxUf(taxConfig.origin_uf);
    if (taxConfig.origin_city) taxScenarioCommon.origin_city = String(taxConfig.origin_city || '');
    if (taxConfig.iss_rate !== null && taxConfig.iss_rate !== undefined && taxConfig.iss_rate !== '') {
      taxScenarioCommon.iss_rate = parseTaxRateInput(taxConfig.iss_rate);
    }
  }

  function applyTaxScenarioCommonToGlobalTaxConfig() {
    var taxConfig = ensureGlobalTaxConfigShell();
    taxConfig.origin_uf = taxScenarioCommon.origin_uf || '';
    taxConfig.origin_city = taxScenarioCommon.origin_city || '';
    taxConfig.iss_rate = taxScenarioCommon.iss_rate;
    if (taxScenarioCommon.origin_uf) {
      applyTaxManualAdjustments();
    }
  }

  function ensureGlobalTaxConfigShell() {
    if (!globalTaxConfig || typeof globalTaxConfig !== 'object') {
      globalTaxConfig = {
        include_taxes: null,
        origin_uf: '',
        origin_city: '',
        iss_rate: null,
        selected_table_ids: [],
        destination_ufs: [],
        icms_rates: [],
        manual_added_ufs: [],
        manual_removed_ufs: [],
        user_edited_rates: {},
        confirmed: false
      };
    }
    if (!Array.isArray(globalTaxConfig.destination_ufs)) globalTaxConfig.destination_ufs = [];
    if (!Array.isArray(globalTaxConfig.icms_rates)) globalTaxConfig.icms_rates = [];
    if (!Array.isArray(globalTaxConfig.manual_added_ufs)) globalTaxConfig.manual_added_ufs = [];
    if (!Array.isArray(globalTaxConfig.manual_removed_ufs)) globalTaxConfig.manual_removed_ufs = [];
    if (!globalTaxConfig.user_edited_rates || typeof globalTaxConfig.user_edited_rates !== 'object') {
      globalTaxConfig.user_edited_rates = {};
    }
    return globalTaxConfig;
  }

  function restoreGlobalTaxConfig(taxConfig) {
    if (!taxConfig || typeof taxConfig !== 'object') return;
    globalTaxConfig = deepCloneValue(taxConfig);
    taxSelectedTableIds = new Set(Array.isArray(taxConfig.selected_table_ids) ? taxConfig.selected_table_ids : []);
    syncTaxScenarioCommonFromTaxConfig(taxConfig);
    taxConfigDirty = false;
  }

  function initGlobalTaxConfigFromState() {
    if (globalTaxConfig && globalTaxConfig.confirmed) {
      syncTaxScenarioCommonFromTaxConfig(globalTaxConfig);
      return;
    }
    ensureGlobalTaxConfigShell();
    syncTaxScenarioCommonFromTaxConfig(globalTaxConfig);
  }

  function markTaxConfigDirty() {
    taxConfigDirty = true;
    if (globalTaxConfig) globalTaxConfig.confirmed = false;
    comparisonState.canAdvanceToCoverage = false;
    updateTempTableModalFooter();
  }

  function collectSelectedTaxTableIds() {
    return Array.from(taxSelectedTableIds);
  }

  function recomputeSelectedTaxDestinationUfs() {
    var byUf = {};
    var manualRemoved = {};
    (globalTaxConfig && globalTaxConfig.manual_removed_ufs || []).forEach(function (uf) {
      manualRemoved[normalizeTaxUf(uf)] = true;
    });
    taxSelectedTableIds.forEach(function (tableId) {
      var preview = taxTableUfsPreview[tableId];
      if (!preview || !Array.isArray(preview.destination_ufs)) return;
      preview.destination_ufs.forEach(function (uf) {
        var normalized = normalizeTaxUf(uf);
        if (!normalized || manualRemoved[normalized]) return;
        mergeTaxDestinationEntry(byUf, normalized, 'automatic', '', false);
      });
    });
    (globalTaxConfig && globalTaxConfig.manual_added_ufs || []).forEach(function (uf) {
      var normalized = normalizeTaxUf(uf);
      if (!normalized || manualRemoved[normalized]) return;
      mergeTaxDestinationEntry(byUf, normalized, 'manual', '', true);
    });
    return Object.keys(byUf).sort().map(function (uf) { return byUf[uf]; });
  }

  function applyTaxManualAdjustments() {
    var taxConfig = ensureGlobalTaxConfigShell();
    var previousRates = taxConfig.icms_rates || [];
    taxConfig.destination_ufs = recomputeSelectedTaxDestinationUfs();
    var originUf = normalizeTaxUf(taxScenarioCommon.origin_uf || taxConfig.origin_uf);
    if (originUf) {
      taxConfig.icms_rates = buildIcmsRatesForOrigin(null, originUf, previousRates, taxConfig.destination_ufs);
    } else {
      taxConfig.icms_rates = [];
    }
  }

  function toggleTaxCarrierSelection(tableId, checked) {
    if (!tableId) return;
    if (checked) taxSelectedTableIds.add(tableId);
    else taxSelectedTableIds.delete(tableId);
    var taxConfig = ensureGlobalTaxConfigShell();
    taxConfig.selected_table_ids = collectSelectedTaxTableIds();
    applyTaxManualAdjustments();
    markTaxConfigDirty();
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
  }

  function taxCarrierUfCount(tableId) {
    var preview = taxTableUfsPreview[tableId];
    if (!preview) return 0;
    return preview.uf_count || (Array.isArray(preview.destination_ufs) ? preview.destination_ufs.length : 0);
  }

  function renderTaxCarrierCheckboxes(container) {
    var confirmed = confirmedComparisonTables();
    if (!confirmed.length) return;

    var section = document.createElement('div');
    section.className = 'agente-compara-tax-carrier-section';

    var title = document.createElement('p');
    title.className = 'agente-compara-tax-carrier-title';
    title.textContent = 'Transportadoras para incidência dos impostos';
    section.appendChild(title);

    var subtitle = document.createElement('p');
    subtitle.className = 'agente-compara-tax-hint';
    subtitle.textContent = 'Selecione as transportadoras sobre as quais os impostos incidirão.';
    section.appendChild(subtitle);

    var list = document.createElement('div');
    list.className = 'agente-compara-tax-carrier-list';

    confirmed.forEach(function (tableMeta) {
      var tableId = tableMeta.table_id;
      var carrierName = tableCarrierDisplay(tableMeta) || ('Transportadora ' + (tableMeta.slot_number || ''));
      var ufCount = taxCarrierUfCount(tableId);
      var label = document.createElement('label');
      label.className = 'agente-compara-tax-carrier-option';
      if (taxSelectedTableIds.has(tableId)) label.classList.add('is-active');

      var input = document.createElement('input');
      input.type = 'checkbox';
      input.value = tableId;
      input.checked = taxSelectedTableIds.has(tableId);
      input.addEventListener('change', function () {
        toggleTaxCarrierSelection(tableId, input.checked);
      });

      var textWrap = document.createElement('span');
      textWrap.className = 'agente-compara-tax-carrier-option-text';
      textWrap.textContent = carrierName + (ufCount ? ' (' + ufCount + ' UF' + (ufCount === 1 ? '' : 's') + ')' : '');

      label.appendChild(input);
      label.appendChild(textWrap);
      list.appendChild(label);

      if (input.checked && ufCount === 0) {
        var alert = document.createElement('p');
        alert.className = 'agente-compara-tax-hint agente-compara-tax-empty-uf-alert';
        alert.textContent = 'Nenhuma UF foi identificada automaticamente nesta tabela.';
        list.appendChild(alert);
      }
    });

    section.appendChild(list);

    var hint = document.createElement('p');
    hint.className = 'agente-compara-tax-hint agente-compara-tax-carrier-hint';
    hint.textContent = comparisonState.canAdvanceToCoverage && !taxConfigDirty
      ? 'Configuração salva. Você pode continuar para cidades.'
      : 'A configuração será salva ao continuar para cidades.';
    section.appendChild(hint);
    container.appendChild(section);
  }

  function rebuildTaxIcmsRates(tempTable, originUf, previousRates) {
    var taxConfig = ensureTaxConfigShell(tempTable);
    var destinationUfs = syncTaxDestinationUfs(tempTable);
    taxConfig.icms_rates = buildIcmsRatesForOrigin(tempTable, originUf, previousRates, destinationUfs);
    return taxConfig.icms_rates;
  }

  function suggestedIcmsRate(originUf, destinationUf) {
    if (ICMS_7_PERCENT_ORIGIN_UFS.indexOf(originUf) !== -1 && ICMS_7_PERCENT_DESTINATION_UFS.indexOf(destinationUf) !== -1) {
      return 7.0;
    }
    return 12.0;
  }

  function parseTaxRateInput(value) {
    var text = String(value === null || value === undefined ? '' : value).trim().replace('%', '').replace(',', '.');
    if (!text) return null;
    var parsed = Number(text);
    return isFinite(parsed) && parsed >= 0 ? parsed : null;
  }

  function formatTaxRate(value) {
    if (value === null || value === undefined || value === '') return '';
    var n = Number(value);
    if (!isFinite(n)) return '';
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 0, maximumFractionDigits: 4 });
  }

  function taxRatesEqual(a, b) {
    if (a === null || a === undefined || b === null || b === undefined) return a === b;
    return Math.abs(Number(a) - Number(b)) < 0.000001;
  }

  function buildIcmsRatesForOrigin(tempTable, originUf, previousRates, destinationUfs) {
    var byUf = {};
    (Array.isArray(previousRates) ? previousRates : []).forEach(function (rate) {
      if (!rate || typeof rate !== 'object') return;
      var uf = normalizeTaxUf(rate.destination_uf);
      if (uf) byUf[uf] = rate;
    });
    var destinationEntries = Array.isArray(destinationUfs)
      ? destinationUfs
      : syncTaxDestinationUfs(tempTable);
    return destinationEntries.map(function (entry) {
      var destinationUf = typeof entry === 'string' ? entry : entry.uf;
      var previous = byUf[destinationUf] || {};
      var sameUf = originUf === destinationUf;
      var suggested = sameUf ? null : suggestedIcmsRate(originUf, destinationUf);
      var hasPreviousApplied = Object.prototype.hasOwnProperty.call(previous, 'applied_rate');
      var applied = hasPreviousApplied ? previous.applied_rate : suggested;
      if (applied === '') applied = null;
      var userEdited = hasPreviousApplied && !taxRatesEqual(applied, suggested);
      return {
        destination_uf: destinationUf,
        operation_type: sameUf ? 'intermunicipal' : 'interstate',
        suggested_rate: suggested,
        applied_rate: applied,
        source_name: sameUf ? ICMS_INTERMUNICIPAL_SOURCE_NAME : ICMS_INTERSTATE_SOURCE_NAME,
        source_type: sameUf ? 'manual' : 'official',
        user_edited: userEdited,
        is_active: applied !== null && applied !== undefined && applied !== ''
      };
    });
  }

  function ensureTaxConfigShell(tempTable) {
    if (!tempTable.tax_config || typeof tempTable.tax_config !== 'object') {
      tempTable.tax_config = {
        include_taxes: null,
        origin_uf: '',
        origin_city: '',
        iss_rate: null,
        destination_ufs: [],
        icms_rates: []
      };
    }
    if (!Array.isArray(tempTable.tax_config.destination_ufs)) {
      tempTable.tax_config.destination_ufs = [];
    }
    if (!Array.isArray(tempTable.tax_config.icms_rates)) {
      tempTable.tax_config.icms_rates = [];
    }
    return tempTable.tax_config;
  }

  function setTaxOriginUf(tempTable, originUf) {
    var taxConfig = ensureTaxConfigShell(tempTable);
    var normalizedUf = normalizeTaxUf(originUf);
    var previousRates = taxConfig.icms_rates || [];
    taxConfig.origin_uf = normalizedUf;
    syncTaxDestinationUfs(tempTable);
    taxConfig.icms_rates = normalizedUf
      ? buildIcmsRatesForOrigin(tempTable, normalizedUf, previousRates, taxConfig.destination_ufs)
      : [];
  }

  function addManualTaxDestinationUf(uf) {
    var taxConfig = ensureGlobalTaxConfigShell();
    var normalizedUf = normalizeTaxUf(uf);
    if (!normalizedUf) return false;
    if (taxConfig.manual_removed_ufs.indexOf(normalizedUf) !== -1) {
      taxConfig.manual_removed_ufs = taxConfig.manual_removed_ufs.filter(function (item) {
        return normalizeTaxUf(item) !== normalizedUf;
      });
    }
    if (taxConfig.manual_added_ufs.indexOf(normalizedUf) === -1) {
      taxConfig.manual_added_ufs.push(normalizedUf);
    }
    applyTaxManualAdjustments();
    return true;
  }

  function removeTaxDestinationUf(uf) {
    var taxConfig = ensureGlobalTaxConfigShell();
    var normalizedUf = normalizeTaxUf(uf);
    if (!normalizedUf) return;
    if (taxConfig.manual_added_ufs.indexOf(normalizedUf) !== -1) {
      taxConfig.manual_added_ufs = taxConfig.manual_added_ufs.filter(function (item) {
        return normalizeTaxUf(item) !== normalizedUf;
      });
    } else if (taxConfig.manual_removed_ufs.indexOf(normalizedUf) === -1) {
      taxConfig.manual_removed_ufs.push(normalizedUf);
    }
    applyTaxManualAdjustments();
  }

  function renderTaxOption(container, id, label, checked, onChange) {
    var wrap = document.createElement('label');
    wrap.className = 'agente-compara-tax-option';
    var input = document.createElement('input');
    input.type = 'radio';
    input.name = 'agenteComparaTaxInclude';
    input.id = id;
    input.checked = !!checked;
    input.addEventListener('change', onChange);
    var span = document.createElement('span');
    span.textContent = label;
    wrap.appendChild(input);
    wrap.appendChild(span);
    container.appendChild(wrap);
  }

  function renderTaxField(container, labelText, input) {
    var wrap = document.createElement('label');
    wrap.className = 'agente-compara-tax-field';
    var label = document.createElement('span');
    label.textContent = labelText;
    wrap.appendChild(label);
    wrap.appendChild(input);
    container.appendChild(wrap);
  }

  function renderTaxConfigFields(container, tempTable, taxConfig) {
    var fields = document.createElement('div');
    fields.className = 'agente-compara-tax-fields';

    var commonTitle = document.createElement('p');
    commonTitle.className = 'agente-compara-tax-section-label';
    commonTitle.textContent = 'Parâmetros comuns do cenário';
    fields.appendChild(commonTitle);

    var ufSelect = document.createElement('select');
    ufSelect.className = 'form-control agente-compara-tax-input';
    ufSelect.id = 'agenteComparaTaxOriginUf';
    var emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = 'Selecione';
    ufSelect.appendChild(emptyOption);
    BRAZILIAN_UFS.forEach(function (uf) {
      var option = document.createElement('option');
      option.value = uf;
      option.textContent = uf;
      option.selected = taxScenarioCommon.origin_uf === uf;
      ufSelect.appendChild(option);
    });
    ufSelect.addEventListener('change', function () {
      taxScenarioCommon.origin_uf = normalizeTaxUf(ufSelect.value);
      applyTaxScenarioCommonToGlobalTaxConfig();
      markTaxConfigDirty();
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
    });
    renderTaxField(fields, 'UF origem', ufSelect);

    var cityInput = document.createElement('input');
    cityInput.type = 'text';
    cityInput.className = 'form-control agente-compara-tax-input';
    cityInput.id = 'agenteComparaTaxOriginCity';
    cityInput.value = taxScenarioCommon.origin_city || '';
    cityInput.addEventListener('input', function () {
      taxScenarioCommon.origin_city = cityInput.value;
      applyTaxScenarioCommonToGlobalTaxConfig();
      markTaxConfigDirty();
    });
    renderTaxField(fields, 'Cidade origem', cityInput);

    var issInput = document.createElement('input');
    issInput.type = 'number';
    issInput.step = '0.0001';
    issInput.min = '0';
    issInput.className = 'form-control agente-compara-tax-input';
    issInput.id = 'agenteComparaTaxIssRate';
    issInput.value = taxScenarioCommon.iss_rate === null || taxScenarioCommon.iss_rate === undefined ? '' : String(taxScenarioCommon.iss_rate);
    issInput.addEventListener('input', function () {
      taxScenarioCommon.iss_rate = parseTaxRateInput(issInput.value);
      applyTaxScenarioCommonToGlobalTaxConfig();
      markTaxConfigDirty();
    });
    renderTaxField(fields, 'ISS (%)', issInput);

    var issHint = document.createElement('p');
    issHint.className = 'agente-compara-tax-hint';
    issHint.textContent = 'O ISS é informado manualmente. Deixe vazio para ignorar no cálculo.';
    fields.appendChild(issHint);
    container.appendChild(fields);
  }

  function renderDestinationUfsSection(container, taxConfig) {
    var section = document.createElement('div');
    section.className = 'agente-compara-tax-destination-ufs';

    var status = document.createElement('p');
    status.className = 'agente-compara-tax-hint';
    status.textContent = taxConfig.destination_ufs.length
      ? 'UFs de destino identificadas nas tabelas selecionadas. Revise, adicione ou remova antes de continuar.'
      : 'Nenhuma UF de destino foi identificada nas tabelas selecionadas.';
    section.appendChild(status);

    var controls = document.createElement('div');
    controls.className = 'agente-compara-tax-destination-controls';

    var ufSelect = document.createElement('select');
    ufSelect.className = 'form-control agente-compara-tax-input';
    ufSelect.id = 'agenteComparaTaxManualDestinationUf';
    var emptyOption = document.createElement('option');
    emptyOption.value = '';
    emptyOption.textContent = 'Adicionar UF destino';
    ufSelect.appendChild(emptyOption);
    BRAZILIAN_UFS.forEach(function (uf) {
      var option = document.createElement('option');
      option.value = uf;
      option.textContent = uf;
      ufSelect.appendChild(option);
    });
    controls.appendChild(ufSelect);

    var addBtn = document.createElement('button');
    addBtn.type = 'button';
    addBtn.className = 'btn btn-secondary agente-compara-tax-add-uf-btn';
    addBtn.textContent = 'Adicionar';
    addBtn.addEventListener('click', function () {
      if (!addManualTaxDestinationUf(ufSelect.value)) return;
      markTaxConfigDirty();
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
    });
    controls.appendChild(addBtn);
    section.appendChild(controls);

    if (!taxConfig.destination_ufs.length) {
      var emptyAlert = document.createElement('p');
      emptyAlert.className = 'agente-compara-tax-hint agente-compara-tax-empty-uf-alert';
      emptyAlert.textContent = 'Nenhuma alíquota de ICMS será aplicada.';
      section.appendChild(emptyAlert);
      container.appendChild(section);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll agente-compara-tax-table-scroll';
    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table agente-compara-tax-table agente-compara-tax-destination-table';
    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['UF destino', 'Origem', 'Evidências', ''].forEach(function (label) {
      appendTableCell(headerRow, label, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    taxConfig.destination_ufs.forEach(function (entry) {
      var tr = document.createElement('tr');
      appendTableCell(tr, entry.uf, false, true);
      appendTableCell(tr, taxDestinationSourceLabel(entry.source), false, true);
      var evidenceText = Array.isArray(entry.evidence) ? entry.evidence.join(', ') : '';
      appendTableCell(tr, evidenceText || '—', false, true);

      var actionCell = document.createElement('td');
      var removeBtn = document.createElement('button');
      removeBtn.type = 'button';
      removeBtn.className = 'btn btn-link agente-compara-tax-remove-uf-btn';
      removeBtn.textContent = 'Remover';
      removeBtn.addEventListener('click', function () {
        removeTaxDestinationUf(entry.uf);
        markTaxConfigDirty();
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
      });
      actionCell.appendChild(removeBtn);
      tr.appendChild(actionCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    section.appendChild(scrollWrap);
    container.appendChild(section);
  }

  function renderIcmsRatesTable(container, taxConfig) {
    var notice = document.createElement('p');
    notice.className = 'agente-compara-tax-hint';
    notice.textContent = 'As alíquotas sugeridas de ICMS interestadual usam a regra geral da Resolução Senado Federal nº 22/1989. Revise manualmente quando houver regra fiscal específica.';
    container.appendChild(notice);

    var emptyHint = document.createElement('p');
    emptyHint.className = 'agente-compara-tax-hint';
    emptyHint.textContent = 'Alíquotas vazias serão ignoradas no cálculo.';
    container.appendChild(emptyHint);

    if (!taxConfig.origin_uf) return;

    if (!taxConfig.icms_rates.length) {
      var empty = document.createElement('p');
      empty.className = 'agente-compara-temp-table-modal-empty';
      empty.textContent = 'Nenhuma alíquota de ICMS será aplicada.';
      container.appendChild(empty);
      return;
    }

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll agente-compara-tax-table-scroll';
    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table agente-compara-tax-table';
    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    ['UF destino', 'Tipo de operação', 'Alíquota sugerida', 'Alíquota aplicada', 'Fonte', 'Editado pelo usuário', 'Usar no cálculo'].forEach(function (label) {
      appendTableCell(headerRow, label, true, false);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    taxConfig.icms_rates.forEach(function (rate, index) {
      var tr = document.createElement('tr');
      appendTableCell(tr, rate.destination_uf, false, true);
      appendTableCell(tr, rate.operation_type, false, true);
      appendTableCell(tr, formatTaxRate(rate.suggested_rate), false, true);

      var rateCell = document.createElement('td');
      var input = document.createElement('input');
      input.type = 'number';
      input.step = '0.0001';
      input.min = '0';
      input.className = 'form-control agente-compara-tax-rate-input';
      input.value = rate.applied_rate === null || rate.applied_rate === undefined ? '' : String(rate.applied_rate);
      input.addEventListener('input', function () {
        var tax = ensureGlobalTaxConfigShell();
        var row = tax.icms_rates[index];
        if (!row) return;
        var parsed = parseTaxRateInput(input.value);
        row.applied_rate = parsed;
        row.is_active = parsed !== null;
        row.user_edited = !taxRatesEqual(parsed, row.suggested_rate);
        markTaxConfigDirty();
      });
      rateCell.appendChild(input);
      tr.appendChild(rateCell);

      appendTableCell(tr, rate.source_name, false, true);
      appendTableCell(tr, rate.user_edited ? 'Sim' : 'Não', false, true);

      var activeCell = document.createElement('td');
      var checkbox = document.createElement('input');
      checkbox.type = 'checkbox';
      checkbox.checked = !!rate.is_active;
      checkbox.addEventListener('change', function () {
        var tax = ensureGlobalTaxConfigShell();
        var row = tax.icms_rates[index];
        if (!row) return;
        row.is_active = checkbox.checked;
        if (!checkbox.checked) {
          row.applied_rate = null;
          row.user_edited = !taxRatesEqual(null, row.suggested_rate);
        } else if (row.applied_rate === null || row.applied_rate === undefined || row.applied_rate === '') {
          row.applied_rate = row.suggested_rate;
          row.user_edited = false;
        }
        markTaxConfigDirty();
        renderTempTableModalContent(currentTempTable);
      });
      activeCell.appendChild(checkbox);
      tr.appendChild(activeCell);
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    scrollWrap.appendChild(table);
    container.appendChild(scrollWrap);
  }

  function renderTaxTabContent(container, tempTable) {
    appendSectionTitle(container, 'Impostos do cenário');
    var section = document.createElement('div');
    section.className = 'agente-compara-temp-table-modal-section agente-compara-tax-section';
    initGlobalTaxConfigFromState();
    var taxConfig = ensureGlobalTaxConfigShell();
    applyTaxScenarioCommonToGlobalTaxConfig();
    renderTaxConfigFields(section, tempTable, taxConfig);

    var question = document.createElement('p');
    question.className = 'agente-compara-tax-question';
    question.textContent = 'Deseja incluir os impostos no cálculo do frete?';
    section.appendChild(question);

    var options = document.createElement('div');
    options.className = 'agente-compara-tax-options';
    renderTaxOption(options, 'agenteComparaTaxNo', 'Não incluir impostos', taxConfig.include_taxes === false, function () {
      var tax = ensureGlobalTaxConfigShell();
      tax.include_taxes = false;
      tax.selected_table_ids = [];
      tax.destination_ufs = [];
      tax.icms_rates = [];
      taxSelectedTableIds = new Set();
      applyTaxScenarioCommonToGlobalTaxConfig();
      markTaxConfigDirty();
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
    });
    renderTaxOption(options, 'agenteComparaTaxYes', 'Incluir impostos', taxConfig.include_taxes === true, function () {
      var tax = ensureGlobalTaxConfigShell();
      tax.include_taxes = true;
      applyTaxScenarioCommonToGlobalTaxConfig();
      applyTaxManualAdjustments();
      markTaxConfigDirty();
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
    });
    section.appendChild(options);

    if (taxConfig.include_taxes === true) {
      renderTaxCarrierCheckboxes(section);
      applyTaxManualAdjustments();
      renderDestinationUfsSection(section, taxConfig);
      renderIcmsRatesTable(section, taxConfig);
    }
    container.appendChild(section);
  }

  function collectGlobalTaxConfigPayload() {
    var taxConfig = ensureGlobalTaxConfigShell();
    applyTaxScenarioCommonToGlobalTaxConfig();
    if (taxConfig.include_taxes !== true && taxConfig.include_taxes !== false) {
      setTempTableModalError('Escolha se deseja incluir impostos no cálculo do frete.');
      return null;
    }
    var commonOriginUf = normalizeTaxUf(taxScenarioCommon.origin_uf);
    var commonOriginCity = String(taxScenarioCommon.origin_city || '').trim();
    var commonIssRate = parseTaxRateInput(taxScenarioCommon.iss_rate);
    if (commonIssRate !== null && !commonOriginCity) {
      setTempTableModalError('Cidade origem é obrigatória quando ISS estiver preenchido.');
      return null;
    }
    var selectedIds = collectSelectedTaxTableIds();
    if (taxConfig.include_taxes === true && !selectedIds.length) {
      setTempTableModalError('Selecione ao menos uma transportadora.');
      return null;
    }
    if (taxConfig.include_taxes === false) {
      return {
        comparison_id: comparisonState.comparisonId,
        tax_config: {
          include_taxes: false,
          origin_uf: commonOriginUf || null,
          origin_city: commonOriginCity || null,
          iss_rate: commonIssRate,
          selected_table_ids: [],
          destination_ufs: [],
          icms_rates: [],
          manual_added_ufs: [],
          manual_removed_ufs: []
        }
      };
    }
    if (!commonOriginUf) {
      setTempTableModalError('UF origem é obrigatória para incluir impostos.');
      return null;
    }
    applyTaxManualAdjustments();
    var icmsRates = taxConfig.icms_rates || [];
    for (var rateIndex = 0; rateIndex < icmsRates.length; rateIndex++) {
      var activeRow = icmsRates[rateIndex];
      if (!activeRow || !activeRow.is_active) continue;
      if (activeRow.applied_rate === null || activeRow.applied_rate === undefined || activeRow.applied_rate === '') {
        setTempTableModalError('Informe a alíquota aplicada para cada UF marcada para uso no cálculo.');
        return null;
      }
    }
    return {
      comparison_id: comparisonState.comparisonId,
      tax_config: {
        include_taxes: true,
        origin_uf: commonOriginUf,
        origin_city: commonOriginCity || null,
        iss_rate: commonIssRate,
        selected_table_ids: selectedIds,
        destination_ufs: deepCloneValue(taxConfig.destination_ufs) || [],
        icms_rates: deepCloneValue(taxConfig.icms_rates) || [],
        manual_added_ufs: deepCloneValue(taxConfig.manual_added_ufs) || [],
        manual_removed_ufs: deepCloneValue(taxConfig.manual_removed_ufs) || []
      }
    };
  }

  function handleGlobalTaxConfigSaveResponse(res) {
    if (!res.data || res.data.ok !== true) {
      setTempTableModalError(res.data || 'Não foi possível salvar a configuração fiscal.');
      return false;
    }
    if (res.data.comparison) {
      syncComparisonStateFromPayload(res.data.comparison);
    }
    if (res.data.tax_config) {
      restoreGlobalTaxConfig(res.data.tax_config);
    }
    if (typeof res.data.can_advance_to_coverage === 'boolean') {
      comparisonState.canAdvanceToCoverage = res.data.can_advance_to_coverage;
    }
    taxConfigDirty = false;
    return true;
  }

  function saveGlobalTaxConfig(options) {
    options = options || {};
    if (taxSaveInFlight && !options.partOfContinueFlow) {
      return Promise.resolve({ ok: false, inFlight: true });
    }
    var payload = collectGlobalTaxConfigPayload();
    if (!payload) {
      return Promise.resolve({ ok: false, validationFailed: true });
    }
    taxSaveInFlight = true;
    if (!options.partOfContinueFlow) {
      setTempTableModalError('');
    }
    updateTempTableModalFooter();

    return fetch(API_COMPARISON_TAXES, {
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
        var saved = handleGlobalTaxConfigSaveResponse(res);
        var result = {
          ok: saved,
          status: res.status,
          comparison: res.data && res.data.comparison,
          tax_config: res.data && res.data.tax_config,
          can_advance_to_coverage: res.data && res.data.can_advance_to_coverage
        };
        if (saved && !options.partOfContinueFlow) {
          renderTempTableModalContent(currentTempTable);
          fetchDocuments();
        }
        return result;
      })
      .catch(function () {
        setTempTableModalError('Não foi possível salvar a configuração fiscal. Verifique sua conexão e tente novamente.');
        return { ok: false, networkError: true };
      })
      .finally(function () {
        if (!options.partOfContinueFlow) {
          taxSaveInFlight = false;
          updateTempTableModalFooter();
        }
      });
  }

  function advanceTaxesToCoverage(options) {
    options = options || {};
    if ((taxSaveInFlight || tempTableSaveInFlight || taxContinueInFlight) && !options.partOfContinueFlow) {
      return Promise.resolve({ ok: false, inFlight: true });
    }
    if (!options.skipPreconditions) {
      if (taxConfigDirty) {
        setTempTableModalError('Salve a configuração de impostos antes de continuar.');
        return Promise.resolve({ ok: false, preconditionFailed: true });
      }
      if (!comparisonState.canAdvanceToCoverage) {
        setTempTableModalError('Salve a configuração de impostos do cenário antes de continuar.');
        return Promise.resolve({ ok: false, preconditionFailed: true });
      }
    }
    if (!currentTempTable || !currentTempTable.temp_table_id) {
      setTempTableModalError('Nenhuma tabela temporária ativa nesta sessão.');
      return Promise.resolve({ ok: false, validationFailed: true });
    }
    if (!options.partOfContinueFlow) {
      taxSaveInFlight = true;
      tempTableSaveInFlight = true;
      setTempTableModalError('');
      updateTempTableModalFooter();
    }

    return fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        temp_table_id: currentTempTable.temp_table_id,
        comparison_id: comparisonState.comparisonId,
        table_id: comparisonState.activeTableId,
        review_action: 'advance_to_coverage'
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res.data || res.data.ok !== true) {
          setTempTableModalError(res.data || 'Não foi possível avançar para cidades.');
          return { ok: false, status: res.status, data: res.data };
        }
        if (res.data.temp_table) {
          setCurrentTempTable(res.data.temp_table);
        }
        if (res.data.comparison) {
          syncComparisonStateFromPayload(res.data.comparison);
        } else if (res.data.temp_table && res.data.temp_table.comparison) {
          syncComparisonStateFromPayload(res.data.temp_table.comparison);
        }
        if (isComparisonCommonParamsStep('COVERAGE')) {
          activateComparisonCommonParamsStep('COVERAGE');
        } else {
          taxStepActive = false;
          coverageStepActive = true;
          tempTableModalActiveTab = 'coverage';
          renderTempTableModalContent(currentTempTable);
          updateTempTableModalFooter();
        }
        fetchDocuments();
        return { ok: true, status: res.status, data: res.data };
      })
      .catch(function () {
        setTempTableModalError('Não foi possível avançar para cidades. Verifique sua conexão e tente novamente.');
        return { ok: false, networkError: true };
      })
      .finally(function () {
        if (!options.partOfContinueFlow) {
          taxSaveInFlight = false;
          tempTableSaveInFlight = false;
          updateTempTableModalFooter();
        }
      });
  }

  function saveTaxesAndAdvanceToCoverage() {
    if (taxContinueInFlight || taxSaveInFlight || tempTableSaveInFlight) return;
    taxContinueInFlight = true;
    taxSaveInFlight = true;
    tempTableSaveInFlight = true;
    setTempTableModalError('');
    updateTempTableModalFooter();

    var needsSave = taxConfigDirty || !comparisonState.canAdvanceToCoverage;
    var chain = needsSave
      ? saveGlobalTaxConfig({ partOfContinueFlow: true }).then(function (saveResult) {
        if (!saveResult.ok) {
          return Promise.reject('save_failed');
        }
        if (!comparisonState.canAdvanceToCoverage) {
          setTempTableModalError('Não foi possível confirmar a configuração fiscal para continuar.');
          return Promise.reject('cannot_advance');
        }
      })
      : Promise.resolve();

    chain
      .then(function () {
        return advanceTaxesToCoverage({ partOfContinueFlow: true, skipPreconditions: true });
      })
      .catch(function () {
        /* erros já tratados nas etapas anteriores */
      })
      .finally(function () {
        taxContinueInFlight = false;
        taxSaveInFlight = false;
        tempTableSaveInFlight = false;
        updateTempTableModalFooter();
      });
  }

  function saveTaxConfigAndContinue() {
    saveTaxesAndAdvanceToCoverage();
  }

  function renderCoverageUploadHint(container) {
    var hint = document.createElement('p');
    hint.className = 'agente-compara-temp-table-modal-empty';
    hint.textContent = 'Faça upload do arquivo complementar CSV ou XLSX para carregar UF, cidade e região de frete.';
    container.appendChild(hint);
  }

  function renderCoverageDecisionCard(container) {
    var card = document.createElement('div');
    card.className = 'agente-compara-coverage-prompt-card';

    var title = document.createElement('span');
    title.className = 'agente-compara-coverage-prompt-title';
    title.textContent = 'Cidades atendidas';
    card.appendChild(title);

    var description = document.createElement('p');
    description.className = 'agente-compara-coverage-prompt-description';
    description.textContent = 'Deseja informar a relação de cidades atendidas?';
    card.appendChild(description);

    var support = document.createElement('p');
    support.className = 'agente-compara-coverage-prompt-support';
    support.textContent = 'Use essa etapa quando a tabela de frete trabalhar com regiões, praças, rotas ou itinerários.';
    card.appendChild(support);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-coverage-prompt-actions';

    var yesBtn = document.createElement('button');
    yesBtn.type = 'button';
    yesBtn.className = 'agente-compara-coverage-prompt-yes agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-primary';
    yesBtn.textContent = 'Sim, enviar planilha';
    yesBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(true);
    });

    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'agente-compara-coverage-prompt-no agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-secondary';
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
    info.className = 'agente-compara-temp-table-modal-empty agente-compara-coverage-skipped-state';
    info.textContent = 'Etapa ignorada. Você poderá iniciar a auditoria, mas linhas que dependam de regiões sem cidade podem ficar sem mapeamento.';
    container.appendChild(info);
  }

  function renderEditableCoverageTable(container, tempTable) {
    ensureCoverageTableShell(tempTable);
    var rows = tempTable.coverage_table.rows;

    var scrollWrap = document.createElement('div');
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table';

    var thead = document.createElement('thead');
    var headerRow = document.createElement('tr');
    COVERAGE_TABLE_HEADERS.forEach(function (field) {
      appendTableCell(headerRow, field.label, true, false);
    });
    var actionsHeader = document.createElement('th');
    actionsHeader.scope = 'col';
    actionsHeader.className = 'agente-compara-temp-table-modal-actions-col';
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
    addBtn.className = 'btn btn-sm agente-compara-temp-table-modal-add-btn';
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
    scrollWrap.className = 'agente-compara-temp-table-modal-freight-scroll';

    var table = document.createElement('table');
    table.className = 'agente-compara-temp-table-modal-freight-table';

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
    section.className = 'agente-compara-temp-table-modal-section agente-compara-temp-table-modal-coverage-section';

    if (!hasCoverageRows(tempTable) && !coveragePromptAnswered) {
      renderCoverageDecisionCard(section);
    } else if (!hasCoverageRows(tempTable) && coveragePromptAccepted) {
      renderCoverageUploadCard(section, 'agenteComparaCoverageModal');
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
    meta.className = 'agente-compara-temp-table-modal-meta';
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
    var body = byId('agenteComparaTempTableModalBody');
    if (!body) return;
    body.innerHTML = '';

    if (isComparisonConfigurationReady()) {
      prepareConfigurationReviewRender();
      renderTempTableModalTabs(body, tempTable);
      var reviewPanel = document.createElement('div');
      reviewPanel.className = 'agente-compara-temp-table-modal-tab-panel';
      reviewPanel.setAttribute('data-configuration-review-tab', configurationReviewTab || '');
      renderConfigurationReviewContent(reviewPanel);
      body.appendChild(reviewPanel);
      return;
    }

    if (!tempTable) {
      var noData = document.createElement('p');
      noData.className = 'agente-compara-temp-table-modal-empty';
      noData.textContent = 'Nenhuma tabela temporária disponível.';
      body.appendChild(noData);
      return;
    }

    var status = String(tempTable.status || '').toLowerCase();
    if (status === 'processing') {
      var processingMsg = document.createElement('p');
      processingMsg.className = 'agente-compara-temp-table-modal-processing';
      processingMsg.textContent = 'Processamento em andamento. Os dados aparecerão aqui quando a extração terminar.';
      body.appendChild(processingMsg);
      return;
    }

    var showTabs = shouldShowTaxTab(tempTable) || shouldShowCoverageTab(tempTable) || shouldShowAuditTab(tempTable);
    if (showTabs) {
      renderTempTableModalTabs(body, tempTable);
      var panel = document.createElement('div');
      panel.className = 'agente-compara-temp-table-modal-tab-panel';
      if (tempTableModalActiveTab === 'audit') {
        renderAuditFileTabContent(panel, tempTable);
      } else if (tempTableModalActiveTab === 'coverage') {
        renderCoverageTabContent(panel, tempTable);
      } else if (tempTableModalActiveTab === 'taxes') {
        renderTaxTabContent(panel, tempTable);
      } else {
        renderFreightTabContent(panel, tempTable);
      }
      body.appendChild(panel);
      return;
    }

    renderFreightTabContent(body, tempTable);
  }

  function isTempTableModalOpen() {
    var modal = byId('agenteComparaTempTableModal');
    return !!(modal && !modal.hidden);
  }

  function openTempTableModal() {
    if (isComparisonWizardFlowActive()) {
      if (!isComparisonWizardEngaged()) {
        markComparisonWizardEngaged();
      }
      openComparisonWizardModal();
      return;
    }
    var modal = byId('agenteComparaTempTableModal');
    if (!modal) return;
    tempTableSaveInFlight = false;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    if (isComparisonConfigurationReady()) {
      // Reabertura na mesma sessão: reconstruir abas e abrir na 1ª transportadora.
      ensureConfigurationReviewDefaults({ forceFirstCarrier: true });
      selectConfigurationReviewTab(configurationReviewTab, { preferCache: true });
      modal.hidden = false;
      document.body.classList.add('agente-compara-temp-table-modal-open');
      return;
    }
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    modal.hidden = false;
    document.body.classList.add('agente-compara-temp-table-modal-open');
    var saveBtn = byId('agenteComparaTempTableModalSave');
    if (saveBtn) saveBtn.focus();
  }

  function closeTempTableModal() {
    var modal = byId('agenteComparaTempTableModal');
    if (!modal || modal.hidden) return;
    closeAuditCalculationMemory();
    modal.hidden = true;
    document.body.classList.remove('agente-compara-temp-table-modal-open');
    resetFreightTableOpenState();
    tempTableSaveInFlight = false;
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    updateTempTableModalFooter();
    if (isComparisonWizardFlowActive()) {
      comparisonWizardModalSuppressed = true;
    }
    if (lastTempTableCardButton && typeof lastTempTableCardButton.focus === 'function') {
      lastTempTableCardButton.focus();
    }
  }

  function handleTempTableModalSaveClick(event) {
    if (event && (tempTableSaveInFlight || taxContinueInFlight)) {
      event.preventDefault();
      event.stopPropagation();
      return;
    }
    if (tempTableModalActiveTab === 'taxes' && shouldShowTaxTab(currentTempTable)) {
      saveTaxConfigAndContinue();
      return;
    }
    if (tempTableModalActiveTab === 'coverage' && shouldShowCoverageTab(currentTempTable)) {
      saveCoverageTableEdit();
      return;
    }
    saveTempTableAndAdvance();
  }

  function initTempTableModal() {
    var modal = byId('agenteComparaTempTableModal');
    var saveBtn = byId('agenteComparaTempTableModalSave');
    var taxSaveBtn = byId('agenteComparaTempTableModalTaxSave');
    var editBtn = byId('agenteComparaTempTableModalEdit');
    var startAuditBtn = byId('agenteComparaTempTableModalStartAudit');
    var closeBtn = byId('agenteComparaTempTableModalClose');
    var cancelEditBtn = byId('agenteComparaTempTableModalCancelEdit');
    var backdrop = byId('agenteComparaTempTableModalBackdrop');
    if (!modal) return;

    if (saveBtn && !saveBtn.dataset.agenteComparaSaveBound) {
      saveBtn.dataset.agenteComparaSaveBound = '1';
      saveBtn.addEventListener('click', handleTempTableModalSaveClick);
    }
    if (taxSaveBtn && !taxSaveBtn.dataset.agenteComparaTaxSaveBound) {
      taxSaveBtn.dataset.agenteComparaTaxSaveBound = '1';
      taxSaveBtn.addEventListener('click', function (event) {
        if (event && (tempTableSaveInFlight || taxSaveInFlight)) {
          event.preventDefault();
          event.stopPropagation();
          return;
        }
        saveGlobalTaxConfig();
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
    var fileInput = byId('agenteComparaFileInput');
    var uploadItem = byId('agenteComparaUploadItem');
    var clearBtn = byId('agenteComparaClearDocuments');
    if (!fileInput) return;

    if (uploadItem) {
      uploadItem.addEventListener('click', function (e) {
        e.preventDefault();
        if (
          uploadInFlight ||
          freightUploadPreparationInFlight ||
          pendingFreightTableUpload ||
          isCarrierIdentificationOpen()
        ) {
          return;
        }
        if (comparisonStartPromise) return;
        closeActionsMenu();
        // Chooser abre no gesto do usuário; start ocorre após seleção do arquivo.
        fileInput.click();
      });
    }

    fileInput.addEventListener('change', function () {
      var file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      beginPendingFreightTableUpload(file);
    });

    if (clearBtn) {
      clearBtn.addEventListener('click', function () {
        requestRestartComparison();
      });
    }

    var clearSlotBtn = byId('agenteComparaTempTableModalClearSlot');
    if (clearSlotBtn) {
      clearSlotBtn.addEventListener('click', function () {
        clearActiveSlotDocuments();
      });
    }

    var resetCancel = byId('agenteComparaResetConfirmCancel');
    var resetSubmit = byId('agenteComparaResetConfirmSubmit');
    var resetBackdrop = byId('agenteComparaResetConfirmBackdrop');
    if (resetCancel) {
      resetCancel.addEventListener('click', function () {
        if (comparisonResetInFlight) return;
        closeResetConfirmModal();
      });
    }
    if (resetSubmit) {
      resetSubmit.addEventListener('click', function () {
        executeComparisonReset();
      });
    }
    if (resetBackdrop) {
      resetBackdrop.addEventListener('click', function () {
        if (comparisonResetInFlight) return;
        closeResetConfirmModal();
      });
    }
    document.addEventListener('keydown', function (e) {
      var resetModal = byId('agenteComparaResetConfirmModal');
      if (e.key === 'Escape' && resetModal && !resetModal.hidden) {
        if (comparisonResetInFlight) return;
        e.stopPropagation();
        closeResetConfirmModal();
      }
    });

    fetchDocuments();
  }

  function init() {
    runWelcomeTypewriter();

    var attachBtn = byId('agenteComparaAttachBtn');
    if (!attachBtn) return;

    attachBtn.addEventListener('click', function (e) {
      e.preventDefault();
      toggleActionsMenu();
    });

    document.addEventListener('click', function (e) {
      var menu = byId('agenteComparaActionsMenu');
      var btn = byId('agenteComparaAttachBtn');
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
    initCarrierIdentificationPanel();
    initTempTableModal();
    initChat();
    restoreComparisonCalculationFromStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }
})();
