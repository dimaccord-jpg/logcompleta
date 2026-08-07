(function () {
  'use strict';

  var API_STATUS = '/api/agente-compara/documents/status';
  var API_UPLOAD = '/api/agente-compara/documents/upload';
  var API_CLEAR = '/api/agente-compara/documents/clear';
  var API_CHAT = '/api/agente-compara/chat';
  var API_AUDIT_CHAT = '/api/agente-compara/audit-chat';
  var API_AUDIT_CHAT_UNLOCK = '/api/agente-compara/audit-chat/unlock';
  var API_COMPARISON_CHAT = '/api/agente-compara/comparison-chat';
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
    analytics: null,
    error: null,
    billingStatus: null
  };
  var comparisonResultsUiState = {
    page: 1,
    pageSize: 50,
    filters: {
      documentNumber: '',
      destinationUf: '',
      destinationCity: '',
      originUf: '',
      originCity: '',
      weightMin: '',
      weightMax: '',
      dateFrom: '',
      dateTo: '',
      status: 'all'
    }
  };
  var comparisonResultChartInstances = [];
  var comparisonResultChartInstancesByWidgetKey = {};
  var COMPARISON_DASHBOARD_PREFERENCES_STORAGE_KEY = 'agente_compara_dashboard_preferences_v1';
  var COMPARISON_DASHBOARD_PREFERENCES_VERSION = 1;
  var COMPARISON_DASHBOARD_WIDGETS = [
    {
      key: 'coverage_by_carrier',
      title: 'Cobertura por transportadora',
      section: 'reliability',
      type: 'chart',
      size: 'standard',
      order: 10,
      hideable: true,
      canvasKey: 'coverage'
    },
    {
      key: 'freight_without_complete_calculation',
      title: 'Fretes sem cálculo completo',
      section: 'reliability',
      type: 'chart',
      size: 'standard',
      order: 20,
      hideable: true,
      canvasKey: 'without_complete'
    },
    {
      key: 'comparability',
      title: 'Comparáveis × parciais × inconclusivos',
      section: 'reliability',
      type: 'chart',
      size: 'standard',
      order: 30,
      hideable: true,
      canvasKey: 'comparability'
    },
    {
      key: 'carrier_wins',
      title: 'Vitórias por transportadora',
      section: 'competitiveness',
      type: 'chart',
      size: 'standard',
      order: 40,
      hideable: true,
      canvasKey: 'wins'
    },
    {
      key: 'comparable_average_cost',
      title: 'Custo médio comparável',
      section: 'competitiveness',
      type: 'chart',
      size: 'standard',
      order: 50,
      hideable: true,
      canvasKey: 'avg_cost'
    },
    {
      key: 'potential_savings',
      title: 'Economia potencial por vencedora',
      section: 'competitiveness',
      type: 'chart',
      size: 'standard',
      order: 60,
      hideable: true,
      canvasKey: 'potential_savings'
    },
    {
      key: 'winner_by_uf_map',
      title: 'Mapa de vencedora por UF',
      section: 'geography',
      type: 'map',
      size: 'wide',
      order: 70,
      hideable: true,
      canvasKey: null
    },
    {
      key: 'uf_savings_ranking',
      title: 'Ranking geográfico',
      section: 'geography',
      type: 'ranking',
      size: 'standard',
      order: 80,
      hideable: true,
      canvasKey: null
    },
    {
      key: 'uf_comparison_matrix',
      title: 'Matriz geográfica',
      section: 'geography',
      type: 'matrix',
      size: 'full',
      order: 90,
      hideable: true,
      canvasKey: null
    }
  ];
  var COMPARISON_DASHBOARD_WIDGET_BY_KEY = (function () {
    var map = {};
    COMPARISON_DASHBOARD_WIDGETS.forEach(function (widget) {
      map[widget.key] = widget;
    });
    return map;
  })();
  var COMPARISON_DASHBOARD_SECTION_META = {
    reliability: {
      titleId: 'agenteComparaReliabilityTitle',
      titleText: 'Confiabilidade da análise'
    },
    competitiveness: {
      titleId: 'agenteComparaCompetitivenessTitle',
      titleText: 'Competitividade de custo'
    },
    geography: {
      sectionId: 'agenteComparaResultsGeography',
      titleText: 'Visão geográfica'
    }
  };
  var comparisonDashboardPreferences = { version: COMPARISON_DASHBOARD_PREFERENCES_VERSION, hidden: [] };
  var comparisonDashboardCustomizeBound = false;
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
    comparisonCalculationState = {
      status: 'not_started',
      executionId: null,
      fingerprintShort: null,
      stale: false,
      result: null,
      analytics: null,
      error: null,
      billingStatus: null
    };
    resetComparisonResultsUiState();
    destroyComparisonResultCharts();
    lastAnnouncedTempTableStatus = null;
    refreshComparisonDashboardView();
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
    if (typeof syncProgressiveChatUnlock === 'function') {
      syncProgressiveChatUnlock();
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
    if (on) {
      setStatus(UPLOAD_PAGE_STATUS_PREPARING);
      return;
    }
    if (getUploadPageStatusText() === UPLOAD_PAGE_STATUS_PREPARING) {
      setStatus('');
    }
  }

  function resetFreightFileInput() {
    var fileInput = byId('agenteComparaFileInput');
    if (fileInput) fileInput.value = '';
  }

  function closeCarrierIdentificationPanel() {
    restoreCarrierIdentifyPanelHome();
    setCarrierIdentifyError('');
    carrierIdentifyInFlight = false;
    updateCarrierIdentifyContinueButton();
    if (comparisonFlowView === 'carrier_identification') {
      comparisonFlowView = null;
    }
  }

  function openCarrierIdentificationPanel(options) {
    options = options || {};
    markComparisonWizardEngaged();
    comparisonWizardModalSuppressed = false;
    renderAndShowComparisonFlowModal('carrier_identification', options);
  }

  function cancelCarrierIdentification() {
    closeCarrierIdentificationPanel();
    clearPendingFreightTableUpload();
    freightUploadPreparationInFlight = false;
    setUploadPreparationLoading(false);
    resetFreightFileInput();
    if (!currentTempTable) {
      closeTempTableModal();
    } else if (isReviewReadyTempTable(currentTempTable)) {
      renderAndShowComparisonFlowModal('review');
    }
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
          if (isReviewReadyTempTable(currentTempTable) && isTempTableModalOpen()) {
            transitionComparisonFlowModal('review');
          }
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
    var fileName = file && file.name ? file.name : '';
    clearPendingFreightTableUpload();
    // Mantém o mesmo modal aberto: identificação → uploading (sem fechar/reabrir).
    closeCarrierIdentificationPanel();
    markComparisonWizardEngaged();
    comparisonWizardModalSuppressed = false;
    transitionComparisonFlowModal('uploading', { fileName: fileName });

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

  function isComparisonReviewMode() {
    return isComparisonPostConfigStep();
  }

  function shouldEnableResultsReviewTab() {
    var step = comparisonState.currentStep || '';
    var status = comparisonCalculationState.status || '';
    if (
      step === 'CALCULATION_RUNNING' ||
      step === 'CALCULATION_READY' ||
      step === 'CALCULATION_FAILED'
    ) {
      return true;
    }
    return (
      status === 'CALCULATION_RUNNING' ||
      status === 'CALCULATION_READY' ||
      status === 'CALCULATION_FAILED' ||
      comparisonCalculationInFlight
    );
  }

  function resetComparisonResultsUiState() {
    comparisonResultsUiState.page = 1;
    comparisonResultsUiState.pageSize = 50;
    comparisonResultsUiState.filters = {
      documentNumber: '',
      destinationUf: '',
      destinationCity: '',
      originUf: '',
      originCity: '',
      weightMin: '',
      weightMax: '',
      dateFrom: '',
      dateTo: '',
      status: 'all'
    };
  }

  function getComparisonDashboardWidget(key) {
    return COMPARISON_DASHBOARD_WIDGET_BY_KEY[key] || null;
  }

  function listComparisonDashboardHideableWidgets() {
    return COMPARISON_DASHBOARD_WIDGETS.filter(function (widget) {
      return widget.hideable === true;
    });
  }

  function defaultComparisonDashboardPreferences() {
    return {
      version: COMPARISON_DASHBOARD_PREFERENCES_VERSION,
      hidden: []
    };
  }

  function normalizeComparisonDashboardPreferences(raw) {
    var defaults = defaultComparisonDashboardPreferences();
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return defaults;
    if (Number(raw.version) !== COMPARISON_DASHBOARD_PREFERENCES_VERSION) return defaults;
    if (!Array.isArray(raw.hidden)) return defaults;
    var known = {};
    var hidden = [];
    raw.hidden.forEach(function (key) {
      var widget = getComparisonDashboardWidget(key);
      if (!widget || widget.hideable !== true) return;
      if (known[widget.key]) return;
      known[widget.key] = true;
      hidden.push(widget.key);
    });
    return { version: COMPARISON_DASHBOARD_PREFERENCES_VERSION, hidden: hidden };
  }

  function loadComparisonDashboardPreferences() {
    try {
      if (typeof window === 'undefined' || !window.localStorage) {
        comparisonDashboardPreferences = defaultComparisonDashboardPreferences();
        return comparisonDashboardPreferences;
      }
      var rawText = window.localStorage.getItem(COMPARISON_DASHBOARD_PREFERENCES_STORAGE_KEY);
      if (!rawText) {
        comparisonDashboardPreferences = defaultComparisonDashboardPreferences();
        return comparisonDashboardPreferences;
      }
      var parsed = JSON.parse(rawText);
      comparisonDashboardPreferences = normalizeComparisonDashboardPreferences(parsed);
      return comparisonDashboardPreferences;
    } catch (_err) {
      comparisonDashboardPreferences = defaultComparisonDashboardPreferences();
      return comparisonDashboardPreferences;
    }
  }

  function saveComparisonDashboardPreferences(prefs) {
    var normalized = normalizeComparisonDashboardPreferences(prefs || comparisonDashboardPreferences);
    comparisonDashboardPreferences = normalized;
    try {
      if (typeof window !== 'undefined' && window.localStorage) {
        window.localStorage.setItem(
          COMPARISON_DASHBOARD_PREFERENCES_STORAGE_KEY,
          JSON.stringify({ version: normalized.version, hidden: normalized.hidden.slice() })
        );
      }
    } catch (_err) { /* ignore quota / private mode */ }
    return comparisonDashboardPreferences;
  }

  function isComparisonDashboardWidgetHidden(key) {
    return (comparisonDashboardPreferences.hidden || []).indexOf(key) !== -1;
  }

  function getComparisonDashboardHiddenWidgets() {
    return listComparisonDashboardHideableWidgets().filter(function (widget) {
      return isComparisonDashboardWidgetHidden(widget.key);
    });
  }

  function getComparisonDashboardVisibleWidgets() {
    return listComparisonDashboardHideableWidgets().filter(function (widget) {
      return !isComparisonDashboardWidgetHidden(widget.key);
    });
  }

  function announceComparisonDashboardPreference(message) {
    var live = document.getElementById('agenteComparaComparisonDashboardLive');
    if (!live) return;
    live.textContent = '';
    live.textContent = message || '';
  }

  function destroyComparisonResultChartByWidgetKey(widgetKey) {
    var chart = comparisonResultChartInstancesByWidgetKey[widgetKey];
    if (!chart) return;
    try {
      if (typeof chart.destroy === 'function') chart.destroy();
    } catch (_err) { /* ignore */ }
    delete comparisonResultChartInstancesByWidgetKey[widgetKey];
    comparisonResultChartInstances = (comparisonResultChartInstances || []).filter(function (item) {
      return item !== chart;
    });
  }

  function registerComparisonResultChart(widgetKey, chart) {
    if (!widgetKey || !chart) return;
    destroyComparisonResultChartByWidgetKey(widgetKey);
    comparisonResultChartInstancesByWidgetKey[widgetKey] = chart;
    comparisonResultChartInstances.push(chart);
  }

  function destroyComparisonResultCharts() {
    Object.keys(comparisonResultChartInstancesByWidgetKey || {}).forEach(function (widgetKey) {
      destroyComparisonResultChartByWidgetKey(widgetKey);
    });
    (comparisonResultChartInstances || []).forEach(function (chart) {
      try {
        if (chart && typeof chart.destroy === 'function') chart.destroy();
      } catch (_err) { /* ignore */ }
    });
    comparisonResultChartInstances = [];
    comparisonResultChartInstancesByWidgetKey = {};
  }

  function comparisonDashboardWidgetSizeClass(size) {
    if (size === 'wide') return 'agente-compara-dashboard-widget--wide';
    if (size === 'full') return 'agente-compara-dashboard-widget--full';
    return 'agente-compara-dashboard-widget--standard';
  }

  function findComparisonDashboardWidgetCard(key) {
    var root = document.getElementById('agenteComparaComparisonCharts');
    if (!root) return null;
    return root.querySelector('[data-comparison-dashboard-widget="' + key + '"]');
  }

  function setComparisonDashboardCustomizeOpen(open) {
    var wrap = document.getElementById('agenteComparaComparisonDashboardCustomize');
    var btn = document.getElementById('agenteComparaComparisonDashboardCustomizeBtn');
    var menu = document.getElementById('agenteComparaComparisonDashboardCustomizeMenu');
    if (!btn || !menu) return;
    var next = open === true;
    menu.hidden = !next;
    btn.setAttribute('aria-expanded', next ? 'true' : 'false');
    if (wrap) wrap.classList.toggle('is-open', next);
  }

  function updateComparisonDashboardCustomizeMenu() {
    var wrap = document.getElementById('agenteComparaComparisonDashboardCustomize');
    var btn = document.getElementById('agenteComparaComparisonDashboardCustomizeBtn');
    var menu = document.getElementById('agenteComparaComparisonDashboardCustomizeMenu');
    var list = document.getElementById('agenteComparaComparisonDashboardHiddenList');
    var showAllBtn = document.getElementById('agenteComparaComparisonDashboardShowAllBtn');
    var countEl = document.getElementById('agenteComparaComparisonDashboardHiddenCount');
    var ready = isComparisonDashboardReady();
    if (wrap) wrap.hidden = !ready;
    if (!ready) {
      setComparisonDashboardCustomizeOpen(false);
      return;
    }
    var hiddenWidgets = getComparisonDashboardHiddenWidgets();
    var count = hiddenWidgets.length;
    if (btn) {
      btn.textContent = count
        ? ('Gráficos ocultos (' + String(count) + ')')
        : 'Personalizar gráficos';
    }
    if (countEl) {
      countEl.textContent = count ? String(count) : '';
      countEl.hidden = count === 0;
    }
    if (showAllBtn) showAllBtn.disabled = count === 0;
    if (list) {
      while (list.firstChild) list.removeChild(list.firstChild);
      if (!count) {
        var empty = document.createElement('p');
        empty.className = 'agente-compara-comparison-dashboard-customize-empty small';
        empty.textContent = 'Nenhum gráfico oculto.';
        list.appendChild(empty);
      } else {
        hiddenWidgets.forEach(function (widget) {
          var item = document.createElement('button');
          item.type = 'button';
          item.className = 'btn btn-sm agente-compara-comparison-dashboard-show-widget-btn';
          item.setAttribute('data-comparison-dashboard-show-widget', widget.key);
          item.setAttribute('aria-label', 'Reexibir gráfico ' + widget.title);
          item.textContent = 'Reexibir ' + widget.title;
          list.appendChild(item);
        });
      }
    }
    if (menu && menu.hidden === false && !count) {
      // keep open only if user opened it; empty state is fine
    }
  }

  function updateComparisonDashboardAllHiddenState() {
    var message = document.getElementById('agenteComparaComparisonDashboardAllHidden');
    var restoreBtn = document.getElementById('agenteComparaComparisonDashboardRestoreChartsBtn');
    var ready = isComparisonDashboardReady();
    var presentHideable = listComparisonDashboardHideableWidgets().filter(function (widget) {
      return !!findComparisonDashboardWidgetCard(widget.key);
    });
    var allHidden =
      ready &&
      presentHideable.length > 0 &&
      presentHideable.every(function (widget) {
        return isComparisonDashboardWidgetHidden(widget.key);
      });
    if (message) message.hidden = !allHidden;
    if (restoreBtn) restoreBtn.hidden = !allHidden;
  }

  function countComparisonDashboardSectionVisible(sectionKey) {
    return COMPARISON_DASHBOARD_WIDGETS.filter(function (widget) {
      if (widget.section !== sectionKey || widget.hideable !== true) return false;
      if (isComparisonDashboardWidgetHidden(widget.key)) return false;
      return !!findComparisonDashboardWidgetCard(widget.key);
    }).length;
  }

  function comparisonDashboardVisibleCountClass(count) {
    if (count <= 0) return '';
    if (count === 1) return 'agente-compara-section-grid--visible-1';
    if (count === 2) return 'agente-compara-section-grid--visible-2';
    return 'agente-compara-section-grid--visible-3-plus';
  }

  function findComparisonDashboardSectionGrid(sectionKey) {
    var root = document.getElementById('agenteComparaComparisonCharts');
    if (!root) return null;
    return root.querySelector('[data-comparison-dashboard-section-grid="' + sectionKey + '"]');
  }

  function findComparisonDashboardSectionBlock(sectionKey) {
    var root = document.getElementById('agenteComparaComparisonCharts');
    if (!root) return null;
    return root.querySelector('[data-comparison-dashboard-section-block="' + sectionKey + '"]');
  }

  function resizeComparisonDashboardVisibleCharts() {
    window.requestAnimationFrame(function () {
      window.requestAnimationFrame(function () {
        Object.keys(comparisonResultChartInstancesByWidgetKey || {}).forEach(function (widgetKey) {
          if (isComparisonDashboardWidgetHidden(widgetKey)) return;
          var instance = comparisonResultChartInstancesByWidgetKey[widgetKey];
          if (instance && typeof instance.resize === 'function') {
            try {
              instance.resize();
            } catch (_err) { /* ignore */ }
          }
        });
      });
    });
  }

  function updateComparisonDashboardSectionLayout(sectionKey) {
    var count = countComparisonDashboardSectionVisible(sectionKey);
    var grid = findComparisonDashboardSectionGrid(sectionKey);
    var block = findComparisonDashboardSectionBlock(sectionKey);
    var meta = COMPARISON_DASHBOARD_SECTION_META[sectionKey] || {};
    var countClasses = [
      'agente-compara-section-grid--visible-1',
      'agente-compara-section-grid--visible-2',
      'agente-compara-section-grid--visible-3-plus'
    ];

    if (sectionKey === 'geography') {
      var geoSection = document.getElementById(meta.sectionId || 'agenteComparaResultsGeography');
      if (geoSection) geoSection.hidden = count === 0;
      var geoLayout = document.querySelector('#agenteComparaResultsGeography .agente-compara-geo-layout');
      if (geoLayout) {
        countClasses.forEach(function (cls) { geoLayout.classList.remove(cls); });
        var nextClass = comparisonDashboardVisibleCountClass(count);
        if (nextClass) geoLayout.classList.add(nextClass);
        var mapHidden = isComparisonDashboardWidgetHidden('winner_by_uf_map');
        var rankHidden = isComparisonDashboardWidgetHidden('uf_savings_ranking');
        var matrixHidden = isComparisonDashboardWidgetHidden('uf_comparison_matrix');
        geoLayout.classList.toggle('is-map-only', !mapHidden && rankHidden);
        geoLayout.classList.toggle('is-rank-only', mapHidden && !rankHidden);
        geoLayout.classList.toggle('is-geo-empty', mapHidden && rankHidden);
        geoLayout.classList.toggle('is-map-rank', !mapHidden && !rankHidden);
        geoLayout.hidden = mapHidden && rankHidden;
      }
      var matrixCard = findComparisonDashboardWidgetCard('uf_comparison_matrix');
      if (matrixCard) {
        matrixCard.classList.toggle('agente-compara-dashboard-widget--solo', !matrixHidden && mapHidden && rankHidden);
      }
      return count;
    }

    if (grid) {
      countClasses.forEach(function (cls) { grid.classList.remove(cls); });
      var cls = comparisonDashboardVisibleCountClass(count);
      if (cls) grid.classList.add(cls);
      grid.hidden = count === 0;
    }
    if (block) block.hidden = count === 0;
    var titleEl = document.getElementById(meta.titleId);
    if (titleEl) titleEl.hidden = count === 0;
    return count;
  }

  function updateComparisonDashboardSectionVisibility() {
    var totalChartVisible = 0;
    ['reliability', 'competitiveness', 'geography'].forEach(function (sectionKey) {
      var count = updateComparisonDashboardSectionLayout(sectionKey);
      if (sectionKey !== 'geography') totalChartVisible += count;
    });

    var chartsSection = document.getElementById('agenteComparaResultsCharts');
    if (chartsSection) chartsSection.hidden = totalChartVisible === 0;
  }

  function applyComparisonDashboardWidgetVisibility() {
    listComparisonDashboardHideableWidgets().forEach(function (widget) {
      var card = findComparisonDashboardWidgetCard(widget.key);
      if (!card) return;
      var hidden = isComparisonDashboardWidgetHidden(widget.key);
      card.hidden = hidden;
      card.classList.toggle('is-hidden', hidden);
      if (hidden && widget.type === 'chart') {
        destroyComparisonResultChartByWidgetKey(widget.key);
      }
    });
    updateComparisonDashboardSectionVisibility();
    updateComparisonDashboardAllHiddenState();
    updateComparisonDashboardCustomizeMenu();
    resizeComparisonDashboardVisibleCharts();
  }

  function recreateComparisonDashboardChart(widgetKey) {
    var widget = getComparisonDashboardWidget(widgetKey);
    if (!widget || widget.type !== 'chart' || isComparisonDashboardWidgetHidden(widgetKey)) return;
    var analytics = comparisonCalculationState.analytics;
    if (!analytics) return;
    var card = findComparisonDashboardWidgetCard(widgetKey);
    if (!card) return;
    var canvas = card.querySelector('canvas');
    if (!canvas || typeof window.Chart !== 'function') return;
    if (comparisonResultChartInstancesByWidgetKey[widgetKey]) return;
    paintComparisonDashboardChart(widgetKey, canvas, analytics);
  }

  function hideComparisonDashboardWidget(widgetKey, options) {
    options = options || {};
    var widget = getComparisonDashboardWidget(widgetKey);
    if (!widget || widget.hideable !== true) return false;
    if (!isComparisonDashboardWidgetHidden(widgetKey)) {
      comparisonDashboardPreferences.hidden = (comparisonDashboardPreferences.hidden || []).concat([widgetKey]);
      saveComparisonDashboardPreferences(comparisonDashboardPreferences);
    }
    var focusCandidate = document.getElementById('agenteComparaComparisonDashboardCustomizeBtn');
    var card = findComparisonDashboardWidgetCard(widgetKey);
    if (card && document.activeElement && card.contains(document.activeElement) && focusCandidate) {
      focusCandidate.focus();
    }
    if (widgetKey === 'winner_by_uf_map') {
      var mapHost = document.getElementById('agenteComparaGeoMap');
      var detail = document.getElementById('agenteComparaGeoMapDetail');
      if (mapHost) {
        while (mapHost.firstChild) mapHost.removeChild(mapHost.firstChild);
      }
      if (detail) {
        while (detail.firstChild) detail.removeChild(detail.firstChild);
        detail.hidden = true;
      }
    }
    applyComparisonDashboardWidgetVisibility();
    if (options.announce !== false) {
      announceComparisonDashboardPreference('Gráfico oculto: ' + widget.title + '.');
    }
    return true;
  }

  function showComparisonDashboardWidget(widgetKey, options) {
    options = options || {};
    var widget = getComparisonDashboardWidget(widgetKey);
    if (!widget || widget.hideable !== true) return false;
    comparisonDashboardPreferences.hidden = (comparisonDashboardPreferences.hidden || []).filter(function (key) {
      return key !== widgetKey;
    });
    saveComparisonDashboardPreferences(comparisonDashboardPreferences);
    applyComparisonDashboardWidgetVisibility();
    if (widget.type === 'chart') {
      recreateComparisonDashboardChart(widgetKey);
    } else if (widget.section === 'geography' && comparisonCalculationState.analytics) {
      ensureComparisonGeographyWidgetsMounted(comparisonCalculationState.analytics);
    }
    resizeComparisonDashboardVisibleCharts();
    if (options.announce !== false) {
      announceComparisonDashboardPreference('Gráfico reexibido: ' + widget.title + '.');
    }
    return true;
  }

  function showAllComparisonDashboardWidgets(options) {
    options = options || {};
    comparisonDashboardPreferences.hidden = [];
    saveComparisonDashboardPreferences(comparisonDashboardPreferences);
    applyComparisonDashboardWidgetVisibility();
    var analytics = comparisonCalculationState.analytics;
    if (analytics) {
      listComparisonDashboardHideableWidgets().forEach(function (widget) {
        if (widget.type === 'chart') recreateComparisonDashboardChart(widget.key);
      });
      ensureComparisonGeographyWidgetsMounted(analytics);
    }
    resizeComparisonDashboardVisibleCharts();
    setComparisonDashboardCustomizeOpen(false);
    if (options.announce !== false) {
      announceComparisonDashboardPreference('Todos os gráficos foram reexibidos.');
    }
    return true;
  }

  function bindComparisonDashboardCustomizeControls() {
    if (comparisonDashboardCustomizeBound) return;
    comparisonDashboardCustomizeBound = true;
    var btn = document.getElementById('agenteComparaComparisonDashboardCustomizeBtn');
    var menu = document.getElementById('agenteComparaComparisonDashboardCustomizeMenu');
    var showAllBtn = document.getElementById('agenteComparaComparisonDashboardShowAllBtn');
    var restoreBtn = document.getElementById('agenteComparaComparisonDashboardRestoreChartsBtn');
    if (btn) {
      btn.addEventListener('click', function () {
        setComparisonDashboardCustomizeOpen(menu ? menu.hidden : true);
      });
    }
    if (showAllBtn) {
      showAllBtn.addEventListener('click', function () {
        showAllComparisonDashboardWidgets();
      });
    }
    if (restoreBtn) {
      restoreBtn.addEventListener('click', function () {
        showAllComparisonDashboardWidgets();
      });
    }
    document.addEventListener('click', function (event) {
      var wrap = document.getElementById('agenteComparaComparisonDashboardCustomize');
      if (!wrap || wrap.hidden || !menu || menu.hidden) return;
      if (wrap.contains(event.target)) return;
      setComparisonDashboardCustomizeOpen(false);
    });
    document.addEventListener('keydown', function (event) {
      if (event.key !== 'Escape') return;
      if (!menu || menu.hidden) return;
      setComparisonDashboardCustomizeOpen(false);
      if (btn) btn.focus();
    });
    var chartsHost = document.getElementById('agenteComparaComparisonCharts');
    if (chartsHost) {
      chartsHost.addEventListener('click', function (event) {
        var hideBtn = event.target && event.target.closest
          ? event.target.closest('[data-comparison-dashboard-hide-widget]')
          : null;
        if (hideBtn) {
          hideComparisonDashboardWidget(hideBtn.getAttribute('data-comparison-dashboard-hide-widget'));
          return;
        }
        var showBtn = event.target && event.target.closest
          ? event.target.closest('[data-comparison-dashboard-show-widget]')
          : null;
        if (showBtn) {
          showComparisonDashboardWidget(showBtn.getAttribute('data-comparison-dashboard-show-widget'));
        }
      });
    }
    var customizeHost = document.getElementById('agenteComparaComparisonDashboardCustomize');
    if (customizeHost) {
      customizeHost.addEventListener('click', function (event) {
        var showBtn = event.target && event.target.closest
          ? event.target.closest('[data-comparison-dashboard-show-widget]')
          : null;
        if (!showBtn) return;
        showComparisonDashboardWidget(showBtn.getAttribute('data-comparison-dashboard-show-widget'));
      });
    }
  }

  function buildComparisonDashboardViewModel() {
    return {
      status: comparisonCalculationState.status || 'not_started',
      stale: comparisonCalculationState.stale === true,
      error: comparisonCalculationState.error || null,
      billingStatus: comparisonCalculationState.billingStatus || null,
      analytics: comparisonCalculationState.analytics || null,
      result: comparisonCalculationState.result || null,
      inFlight: comparisonCalculationInFlight === true,
      currentStep: comparisonState.currentStep || ''
    };
  }

  function buildComparisonDetailViewModel() {
    var result = comparisonCalculationState.result || null;
    return {
      tables: (result && result.tables) || [],
      comparative_rows: (result && result.comparative_rows) || [],
      filters: comparisonResultsUiState.filters,
      page: comparisonResultsUiState.page,
      pageSize: comparisonResultsUiState.pageSize
    };
  }

  function isComparisonDashboardReady(vm) {
    vm = vm || buildComparisonDashboardViewModel();
    return (
      vm.status === 'CALCULATION_READY' &&
      !vm.stale &&
      vm.billingStatus === 'applied' &&
      !vm.inFlight &&
      !!vm.result &&
      Array.isArray(vm.result.comparative_rows)
    );
  }

  function setComparisonDashboardBusy(root, busy) {
    if (!root) return;
    if (busy) root.setAttribute('aria-busy', 'true');
    else root.removeAttribute('aria-busy');
  }

  function clearComparisonDashboardPanels() {
    destroyComparisonResultCharts();
    var root = document.getElementById('agenteComparaComparisonDashboard');
    var statusEl = document.getElementById('agenteComparaComparisonDashboardStatus');
    var kpisEl = document.getElementById('agenteComparaComparisonKpis');
    var chartsEl = document.getElementById('agenteComparaComparisonCharts');
    var unavailableEl = document.getElementById('agenteComparaComparisonDashboardUnavailable');
    var idleEl = document.getElementById('agenteComparaComparisonDashboardIdle');
    var ctaBtn = document.getElementById('agenteComparaComparisonDashboardOpenDetailsBtn');
    var customize = document.getElementById('agenteComparaComparisonDashboardCustomize');
    var allHidden = document.getElementById('agenteComparaComparisonDashboardAllHidden');
    var restoreBtn = document.getElementById('agenteComparaComparisonDashboardRestoreChartsBtn');
    var live = document.getElementById('agenteComparaComparisonDashboardLive');
    if (root) root.hidden = true;
    if (statusEl) {
      statusEl.textContent = '';
      statusEl.className = 'agente-compara-comparison-dashboard-status';
    }
    if (kpisEl) {
      while (kpisEl.firstChild) kpisEl.removeChild(kpisEl.firstChild);
      kpisEl.hidden = true;
    }
    if (chartsEl) {
      while (chartsEl.firstChild) chartsEl.removeChild(chartsEl.firstChild);
      chartsEl.hidden = true;
    }
    if (unavailableEl) {
      unavailableEl.textContent = '';
      unavailableEl.hidden = true;
    }
    if (idleEl) idleEl.hidden = true;
    if (ctaBtn) ctaBtn.hidden = true;
    if (customize) {
      customize.hidden = true;
      setComparisonDashboardCustomizeOpen(false);
    }
    if (allHidden) allHidden.hidden = true;
    if (restoreBtn) restoreBtn.hidden = true;
    if (live) live.textContent = '';
  }

  function openComparisonResultsDetailFromDashboard() {
    if (!isComparisonDashboardReady()) return;
    configurationReviewTab = 'results';
    tempTableModalActiveTab = 'configuration_review';
    if (isComparisonReviewMode()) {
      ensureConfigurationReviewDefaults({ forceResultsTab: true });
      selectConfigurationReviewTab('results', { preferCache: true });
      showTempTableModalShell();
      return;
    }
    var host = document.getElementById('agenteComparaComparisonResultsHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'agenteComparaComparisonResultsHost';
      host.className = 'agente-compara-comparison-results-host';
      var pageRoot = document.querySelector('.agente-compara-page') || document.body;
      pageRoot.appendChild(host);
    }
    refreshComparisonResultsDetailView();
    openTempTableModal();
  }

  function bindComparisonDashboardDetailsButton() {
    var ctaBtn = document.getElementById('agenteComparaComparisonDashboardOpenDetailsBtn');
    if (!ctaBtn || ctaBtn.getAttribute('data-bound') === '1') return;
    ctaBtn.setAttribute('data-bound', '1');
    ctaBtn.addEventListener('click', function () {
      openComparisonResultsDetailFromDashboard();
    });
  }

  function renderComparisonResultsDashboard(result, analytics, state) {
    var root = document.getElementById('agenteComparaComparisonDashboard');
    if (!root) return;
    loadComparisonDashboardPreferences();
    bindComparisonDashboardDetailsButton();
    bindComparisonDashboardCustomizeControls();

    var vm = state && typeof state === 'object'
      ? state
      : buildComparisonDashboardViewModel();
    if (result !== undefined) vm.result = result;
    if (analytics !== undefined) vm.analytics = analytics;

    var statusEl = document.getElementById('agenteComparaComparisonDashboardStatus');
    var kpisEl = document.getElementById('agenteComparaComparisonKpis');
    var chartsEl = document.getElementById('agenteComparaComparisonCharts');
    var unavailableEl = document.getElementById('agenteComparaComparisonDashboardUnavailable');
    var idleEl = document.getElementById('agenteComparaComparisonDashboardIdle');
    var ctaBtn = document.getElementById('agenteComparaComparisonDashboardOpenDetailsBtn');

    function setStatus(text, kind) {
      if (!statusEl) return;
      statusEl.className = 'agente-compara-comparison-dashboard-status' + (kind ? ' is-' + kind : '');
      statusEl.textContent = text || '';
    }

    function hideReadyPanels() {
      if (kpisEl) {
        while (kpisEl.firstChild) kpisEl.removeChild(kpisEl.firstChild);
        kpisEl.hidden = true;
      }
      if (chartsEl) {
        while (chartsEl.firstChild) chartsEl.removeChild(chartsEl.firstChild);
        chartsEl.hidden = true;
      }
      destroyComparisonResultCharts();
    }

    var step = vm.currentStep || comparisonState.currentStep || '';
    var calcStatus = vm.status || 'not_started';
    var billing = vm.billingStatus || '';
    var running =
      vm.inFlight ||
      step === 'CALCULATION_RUNNING' ||
      calcStatus === 'CALCULATION_RUNNING';

    // Placeholder/idle e estados pré-conclusão: seção oculta (sem box vazio).
    if (
      running ||
      (calcStatus === 'CALCULATION_READY' && vm.stale) ||
      (calcStatus === 'CALCULATION_READY' && billing === 'pending') ||
      (calcStatus === 'CALCULATION_READY' && billing === 'failed') ||
      calcStatus === 'CALCULATION_FAILED' ||
      !isComparisonDashboardReady(vm)
    ) {
      if (
        calcStatus === 'CALCULATION_READY' &&
        billing === 'applied' &&
        !vm.stale &&
        (!vm.result || !Array.isArray(vm.result.comparative_rows))
      ) {
        setComparisonDashboardBusy(root, false);
        root.hidden = false;
        if (idleEl) idleEl.hidden = true;
        if (unavailableEl) {
          unavailableEl.hidden = false;
          unavailableEl.textContent = 'Nenhum resultado disponível para exibição.';
        }
        if (ctaBtn) ctaBtn.hidden = true;
        hideReadyPanels();
        setStatus('Nenhum resultado disponível para exibição.', 'error');
        return;
      }
      clearComparisonDashboardPanels();
      setComparisonDashboardBusy(root, false);
      return;
    }

    setComparisonDashboardBusy(root, false);
    root.hidden = false;
    if (idleEl) idleEl.hidden = true;
    if (unavailableEl) unavailableEl.hidden = true;
    setStatus('Cálculos concluídos', 'success');
    if (ctaBtn) ctaBtn.hidden = false;

    if (kpisEl) {
      while (kpisEl.firstChild) kpisEl.removeChild(kpisEl.firstChild);
      if (vm.analytics) {
        kpisEl.hidden = false;
        renderComparisonAnalyticsSummary(kpisEl, vm.analytics);
      } else {
        kpisEl.hidden = true;
        if (unavailableEl) {
          unavailableEl.hidden = false;
          unavailableEl.textContent = 'Os indicadores deste resultado não estão disponíveis.';
        }
      }
    }

    if (chartsEl) {
      while (chartsEl.firstChild) chartsEl.removeChild(chartsEl.firstChild);
      if (vm.analytics) {
        chartsEl.hidden = false;
        renderComparisonResultCharts(chartsEl, vm.analytics);
      } else {
        chartsEl.hidden = true;
        destroyComparisonResultCharts();
      }
    }
    updateComparisonDashboardCustomizeMenu();
    updateComparisonDashboardAllHiddenState();
  }

  function refreshComparisonDashboardView() {
    renderComparisonResultsDashboard(
      comparisonCalculationState.result,
      comparisonCalculationState.analytics,
      buildComparisonDashboardViewModel()
    );
  }

  function refreshComparisonResultsDetailView() {
    var host = document.getElementById('agenteComparaComparisonResultsHost');
    if (!host) return;
    renderComparisonCalculationResults(host, comparisonCalculationState.result);
  }

  function refreshComparisonCalculationViews() {
    refreshComparisonDashboardView();
    var host = document.getElementById('agenteComparaComparisonResultsHost');
    if (host) {
      refreshComparisonResultsDetailView();
      return;
    }
    if (isTempTableModalOpen() && configurationReviewTab === 'results' && isComparisonReviewMode()) {
      renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
      updateTempTableModalFooter();
    }
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
    if (comparisonFlowView === 'carrier_identification') return 'carrier_identification';
    if (uploadInFlight || comparisonFlowView === 'uploading') {
      if (uploadInFlight) return 'uploading';
    }

    var step = comparisonState.currentStep || '';
    if (step === 'ASK_TABLE_3') return 'ask_table_3';
    if (!isComparisonWizardStep(step)) return null;

    var active = activeComparisonTable();
    var tempTable = currentTempTable;
    if (tempTable && tempTable.temp_table_id && tempTableMatchesActiveSlot(tempTable)) {
      var tempStatus = String(tempTable.status || '').toLowerCase();
      if (tempStatus === 'processing') return 'processing';
      if (tempStatus === 'failed' || tempStatus === 'expired' || tempStatus === 'discarded') {
        return 'failed';
      }
      if (tempStatus === 'needs_review' && active && !active.confirmed) return 'review';
    }
    if (comparisonFlowView === 'failed') return 'failed';
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

  function isCurrentComparisonRequest(generation, comparisonId, tableId) {
    if (generation !== comparisonRequestGeneration) return false;
    var activeComparisonId = comparisonState.comparisonId || null;
    if (!comparisonId || !activeComparisonId || comparisonId !== activeComparisonId) return false;
    if (tableId) {
      var activeTableId = comparisonState.activeTableId || null;
      if (activeTableId && tableId !== activeTableId) return false;
    }
    return true;
  }

  function clearLocalComparisonState() {
    comparisonState.comparisonId = null;
    comparisonState.currentStep = null;
    comparisonState.activeTableId = null;
    comparisonState.desiredTableCount = 2;
    comparisonState.primaryTempTableId = null;
    comparisonState.tables = [];
    comparisonState.canAdvanceToCoverage = false;
    if (typeof lockComparisonChat === 'function') {
      lockComparisonChat({ clearHistory: true });
      chatScopedComparisonId = null;
    } else if (typeof clearChatConversation === 'function') {
      clearChatConversation();
      chatScopedComparisonId = null;
    }
  }

  function hideTempTableModalShell() {
    var modal = byId('agenteComparaTempTableModal');
    if (modal) {
      modal.hidden = true;
      modal.removeAttribute('aria-busy');
    }
    document.body.classList.remove('agente-compara-temp-table-modal-open');
    document.body.classList.remove('agente-compara-temp-table-modal-editing');
  }

  /**
   * Único opener oficial da shell #agenteComparaTempTableModal.
   * Se já estiver aberta, não reabre (evita piscar backdrop).
   */
  function showTempTableModalShell() {
    var modal = byId('agenteComparaTempTableModal');
    if (!modal) return false;
    if (!modal.hidden) return true;
    modal.hidden = false;
    document.body.classList.add('agente-compara-temp-table-modal-open');
    return true;
  }

  function rememberCarrierIdentifyPanelHome() {
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    if (!panel || carrierIdentifyPanelHomeParent) return;
    carrierIdentifyPanelHomeParent = panel.parentNode;
    carrierIdentifyPanelHomeNext = panel.nextSibling;
  }

  function restoreCarrierIdentifyPanelHome() {
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    if (!panel) return;
    rememberCarrierIdentifyPanelHome();
    if (carrierIdentifyPanelHomeParent && panel.parentNode !== carrierIdentifyPanelHomeParent) {
      if (carrierIdentifyPanelHomeNext && carrierIdentifyPanelHomeNext.parentNode === carrierIdentifyPanelHomeParent) {
        carrierIdentifyPanelHomeParent.insertBefore(panel, carrierIdentifyPanelHomeNext);
      } else {
        carrierIdentifyPanelHomeParent.appendChild(panel);
      }
    }
    panel.hidden = true;
  }

  function clearPageUploadStatusWhileModalOpen() {
    if (isTempTableModalOpen()) {
      setStatus('');
    }
  }

  function buildFlowStatusBody(message, options) {
    options = options || {};
    var wrap = document.createElement('div');
    wrap.className = 'agente-compara-flow-status-panel';
    wrap.setAttribute('role', 'status');
    wrap.setAttribute('aria-live', 'polite');
    var spinner = document.createElement('div');
    spinner.className = 'agente-compara-flow-status-spinner';
    spinner.setAttribute('aria-hidden', 'true');
    var text = document.createElement('p');
    text.className = 'agente-compara-flow-status-text mb-0';
    text.textContent = message || '';
    wrap.appendChild(spinner);
    wrap.appendChild(text);
    if (options.fileName) {
      var fileEl = document.createElement('p');
      fileEl.className = 'agente-compara-flow-status-file small text-muted mb-0';
      fileEl.textContent = String(options.fileName);
      wrap.appendChild(fileEl);
    }
    return wrap;
  }

  function renderCarrierIdentificationFlowView(options) {
    options = options || {};
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    var body = byId('agenteComparaTempTableModalBody');
    var panel = byId('agenteComparaCarrierIdentifyPanel');
    var input = byId('agenteComparaCarrierNameInput');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    var modal = byId('agenteComparaTempTableModal');
    if (!titleEl || !subtitleEl || !body || !panel || !input) return false;

    rememberCarrierIdentifyPanelHome();
    titleEl.textContent = 'Identifique a transportadora';
    subtitleEl.textContent = 'Confirme a transportadora responsável pela tabela antes de continuar.';
    carrierEditOnlyMode = !!options.editOnly;
    var preset = options.presetName || '';
    if (!preset && pendingFreightTableUpload && pendingFreightTableUpload.carrierName) {
      preset = pendingFreightTableUpload.carrierName;
    }
    input.value = preset;
    setCarrierIdentifyError('');
    body.hidden = false;
    if (footer) footer.hidden = false;
    body.replaceChildren();
    body.appendChild(panel);
    panel.hidden = false;
    if (modal) modal.removeAttribute('aria-busy');
    updateTempTableModalFooter();
    updateCarrierIdentifyContinueButton();
    return !!(titleEl.textContent && !panel.hidden && body.contains(panel));
  }

  function renderUploadingFlowView(options) {
    options = options || {};
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    var modal = byId('agenteComparaTempTableModal');
    if (!titleEl || !subtitleEl || !body) return false;
    restoreCarrierIdentifyPanelHome();
    titleEl.textContent = 'Enviando tabela de frete';
    subtitleEl.textContent = 'Aguarde enquanto o documento é enviado.';
    body.hidden = false;
    if (footer) footer.hidden = false;
    body.replaceChildren();
    body.appendChild(buildFlowStatusBody(UPLOAD_PAGE_STATUS_SENDING, {
      fileName: options.fileName || ''
    }));
    if (modal) modal.setAttribute('aria-busy', 'true');
    setStatus('');
    updateTempTableModalFooter();
    return !!(titleEl.textContent && body.childNodes.length);
  }

  function renderProcessingFlowView(options) {
    options = options || {};
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    var modal = byId('agenteComparaTempTableModal');
    if (!titleEl || !subtitleEl || !body) return false;
    restoreCarrierIdentifyPanelHome();
    titleEl.textContent = 'Processando tabela de frete';
    subtitleEl.textContent = options.subtitle || 'Estamos estruturando os dados para revisão.';
    body.hidden = false;
    if (footer) footer.hidden = false;
    body.replaceChildren();
    body.appendChild(buildFlowStatusBody(UPLOAD_PAGE_STATUS_PROCESSING));
    if (modal) modal.setAttribute('aria-busy', 'true');
    setStatus('');
    updateTempTableModalFooter();
    return !!(titleEl.textContent && body.childNodes.length);
  }

  function renderFailedFlowView(options) {
    options = options || {};
    var titleEl = byId('agenteComparaTempTableModalTitle');
    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    var modal = byId('agenteComparaTempTableModal');
    if (!titleEl || !subtitleEl || !body) return false;
    restoreCarrierIdentifyPanelHome();
    var message = options.message || comparisonFlowFailedMessage ||
      'Não foi possível processar a tabela. Tente novamente.';
    comparisonFlowFailedMessage = message;
    titleEl.textContent = 'Não foi possível processar a tabela';
    subtitleEl.textContent = options.subtitle || 'Revise o arquivo enviado e tente novamente.';
    body.hidden = false;
    if (footer) footer.hidden = false;
    body.replaceChildren();
    var panel = document.createElement('div');
    panel.className = 'agente-compara-flow-failed-panel';
    var text = document.createElement('p');
    text.className = 'agente-compara-flow-failed-text';
    text.textContent = message;
    panel.appendChild(text);
    var retryBtn = document.createElement('button');
    retryBtn.type = 'button';
    retryBtn.className = 'btn btn-sm btn-primary agente-compara-flow-retry-btn';
    retryBtn.textContent = 'Tentar novamente';
    retryBtn.addEventListener('click', function (event) {
      event.preventDefault();
      retryFailedFreightTableUpload();
    });
    panel.appendChild(retryBtn);
    body.appendChild(panel);
    if (modal) modal.removeAttribute('aria-busy');
    setStatus('');
    updateTempTableModalFooter();
    return !!(titleEl.textContent && body.childNodes.length);
  }

  function renderReviewFlowView() {
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    var modal = byId('agenteComparaTempTableModal');
    if (!body) return false;
    if (!isReviewReadyTempTable(currentTempTable)) return false;
    restoreCarrierIdentifyPanelHome();
    body.hidden = false;
    if (footer) footer.hidden = false;
    setComparisonWizardModalHeader('review');
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    if (modal) modal.removeAttribute('aria-busy');
    completeTempTableProcessingUi();
    setStatus('');
    var titleEl = byId('agenteComparaTempTableModalTitle');
    return !!(titleEl && titleEl.textContent && body.childNodes.length);
  }

  function renderComparisonFlowView(view, options) {
    options = options || {};
    if (!view) return false;
    comparisonFlowView = view;
    if (view === 'carrier_identification') return renderCarrierIdentificationFlowView(options);
    if (view === 'uploading') return renderUploadingFlowView(options);
    if (view === 'processing') return renderProcessingFlowView(options);
    if (view === 'failed') return renderFailedFlowView(options);
    if (view === 'review') return renderReviewFlowView();
    // Demais views do wizard/revisão compartilhada.
    restoreCarrierIdentifyPanelHome();
    var body = byId('agenteComparaTempTableModalBody');
    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    if (body) body.hidden = false;
    if (footer) footer.hidden = false;
    if (view === 'upload' || view === 'ask_table_3' || view === 'tables_ready') {
      var renderedLegacy = renderComparisonWizardModal();
      return !!renderedLegacy;
    }
    return false;
  }

  /**
   * Controlador visual único: renderiza a view e abre a shell somente se necessário.
   * Com modal já aberto, apenas troca o conteúdo (sem reabrir backdrop).
   */
  function renderAndShowComparisonFlowModal(view, options) {
    options = options || {};
    if (!view) return false;
    clearTempTableValidationErrors();
    if (view !== 'failed') setTempTableModalError('');
    var rendered = renderComparisonFlowView(view, options);
    if (!rendered) return false;
    clearPageUploadStatusWhileModalOpen();
    if (!showTempTableModalShell()) return false;
    clearPageUploadStatusWhileModalOpen();
    if (view === 'carrier_identification') {
      var input = byId('agenteComparaCarrierNameInput');
      if (input) {
        input.focus();
        if (typeof input.select === 'function') input.select();
      }
    }
    return true;
  }

  function transitionComparisonFlowModal(view, options) {
    if (!isTempTableModalOpen()) {
      return renderAndShowComparisonFlowModal(view, options);
    }
    var rendered = renderComparisonFlowView(view, options);
    if (rendered) clearPageUploadStatusWhileModalOpen();
    return rendered;
  }

  function retryFailedFreightTableUpload() {
    var fileInput = byId('agenteComparaFileInput');
    comparisonFlowFailedMessage = '';
    comparisonFlowView = null;
    if (fileInput) {
      fileInput.click();
    }
  }

  function teardownTempTableModal() {
    hideTempTableModalShell();
    restoreCarrierIdentifyPanelHome();
    comparisonFlowView = null;
    comparisonFlowFailedMessage = '';
    var modal = byId('agenteComparaTempTableModal');
    if (modal) {
      modal.removeAttribute('aria-busy');
    }

    var body = byId('agenteComparaTempTableModalBody');
    if (body) {
      body.replaceChildren();
      body.hidden = false;
    }

    var titleEl = byId('agenteComparaTempTableModalTitle');
    if (titleEl) titleEl.textContent = '';

    var subtitleEl = byId('agenteComparaTempTableModalSubtitle');
    if (subtitleEl) subtitleEl.replaceChildren();

    setTempTableModalError('');
    clearTempTableValidationErrors();

    var banner = byId('agenteComparaTempTableModalEditBanner');
    if (banner) banner.hidden = true;

    var panel = byId('agenteComparaCarrierIdentifyPanel');
    if (panel) panel.hidden = true;
    setCarrierIdentifyError('');
    carrierIdentifyInFlight = false;

    var footer = document.querySelector('.agente-compara-temp-table-modal-footer');
    if (footer) footer.hidden = false;

    var editBtn = byId('agenteComparaTempTableModalEdit');
    var cancelBtn = byId('agenteComparaTempTableModalCancelEdit');
    var saveBtn = byId('agenteComparaTempTableModalSave');
    var taxSaveBtn = byId('agenteComparaTempTableModalTaxSave');
    var startAuditBtn = byId('agenteComparaTempTableModalStartAudit');
    var clearSlotBtn = byId('agenteComparaTempTableModalClearSlot');
    if (editBtn) {
      editBtn.hidden = true;
      editBtn.removeAttribute('aria-busy');
    }
    if (cancelBtn) cancelBtn.hidden = true;
    if (saveBtn) {
      saveBtn.hidden = true;
      saveBtn.removeAttribute('aria-busy');
    }
    if (taxSaveBtn) {
      taxSaveBtn.hidden = true;
      taxSaveBtn.removeAttribute('aria-busy');
    }
    if (startAuditBtn) startAuditBtn.hidden = true;
    if (clearSlotBtn) clearSlotBtn.hidden = true;

    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    tempTableSaveInFlight = false;
    tempTableModalActiveTab = 'freight';
  }

  function resetAgenteComparaFrontendState() {
    bumpComparisonRequestGeneration();
    comparisonStartPromise = null;
    reviewLoadToken += 1;
    stopTempTablePolling();
    clearPendingFreightTableUpload();
    freightUploadPreparationInFlight = false;
    setUploadPreparationLoading(false);
    if (uploadInFlight) {
      setUploadLoading(false);
    } else {
      uploadInFlight = false;
    }
    resetFreightFileInput();
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    tempTableSaveInFlight = false;
    tempTableModalActiveTab = 'freight';
    setCurrentTempTable(null);
    clearLocalComparisonState();
    resetConfigurationReviewState();
    comparisonCalculationState = {
      status: 'not_started',
      executionId: null,
      fingerprintShort: null,
      stale: false,
      result: null,
      analytics: null,
      error: null,
      billingStatus: null
    };
    resetComparisonResultsUiState();
    destroyComparisonResultCharts();
    closeComparisonCalculationMemory();
    teardownTempTableModal();
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
    refreshComparisonDashboardView();
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
    if (!isComparisonReviewMode()) return;
    captureReviewSharedTempTable(currentTempTable);
    // Entrada/reabertura/refresh completo: 1ª transportadora.
    // Atualização soft com modal aberto: preserva a aba visual local.
    if (options.forceFirstCarrier || !configurationReviewTab) {
      configurationReviewTab = defaultConfigurationReviewTab();
    }
    if (options.forceResultsTab && shouldEnableResultsReviewTab()) {
      configurationReviewTab = 'results';
    }
    tempTableModalActiveTab = 'configuration_review';
  }

  function prepareConfigurationReviewRender() {
    if (!isComparisonReviewMode()) return;
    captureReviewSharedTempTable(currentTempTable);
    if (!configurationReviewTab) {
      configurationReviewTab = defaultConfigurationReviewTab();
    }
    if (configurationReviewTab === 'results' && !shouldEnableResultsReviewTab()) {
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
    if (view === 'carrier_identification') {
      titleEl.textContent = 'Identifique a transportadora';
      subtitleEl.textContent = 'Confirme a transportadora responsável pela tabela antes de continuar.';
      return;
    }
    if (view === 'uploading') {
      titleEl.textContent = 'Enviando tabela de frete';
      subtitleEl.textContent = 'Aguarde enquanto o documento é enviado.';
      return;
    }
    if (view === 'failed') {
      titleEl.textContent = 'Não foi possível processar a tabela';
      subtitleEl.textContent = options.subtitle || 'Revise o arquivo enviado e tente novamente.';
      return;
    }
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
        ? ('Transportadora: ' + processingCarrier + '. Estamos estruturando os dados para revisão.')
        : 'Estamos estruturando os dados para revisão.';
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
      subtitleEl.textContent = 'As configurações estão prontas. O cálculo será iniciado somente após sua confirmação.';
      return;
    }
    if (step === 'CALCULATION_RUNNING') {
      titleEl.textContent = 'Processando cÃ¡lculos';
      subtitleEl.textContent = 'Processando cÃ¡lculos comparativos...';
      return;
    }
    if (step === 'CALCULATION_READY') {
      titleEl.textContent = 'Resultados da comparação';
      subtitleEl.textContent = comparisonCalculationState.stale
        ? 'As configurações foram alteradas. Processe novamente para atualizar os resultados.'
        : 'Cálculos concluídos';
      return;
    }
    if (step === 'CALCULATION_FAILED') {
      var failureUi = resolveCalculationStorageFailureUi(comparisonCalculationState.error || {});
      titleEl.textContent = failureUi.title;
      subtitleEl.textContent = failureUi.message;
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
          showTempTableModalShell();
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
      showTempTableModalShell();
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

  function isReviewReadyTempTable(tempTable) {
    if (!tempTable || !tempTable.temp_table_id) return false;
    if (String(tempTable.status || '').toLowerCase() !== 'needs_review') return false;
    if (!comparisonState.comparisonId) return false;
    if (
      tempTable.comparison_id &&
      tempTable.comparison_id !== comparisonState.comparisonId
    ) {
      return false;
    }
    if (
      tempTable.table_id &&
      comparisonState.activeTableId &&
      tempTable.table_id !== comparisonState.activeTableId
    ) {
      return false;
    }
    var active = activeComparisonTable();
    if (!active || active.confirmed) return false;
    if (!tempTableMatchesActiveSlot(tempTable)) return false;
    return true;
  }

  function renderComparisonWizardModal() {
    if (!isComparisonWizardFlowActive()) {
      if (!currentTempTable) return false;
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
      return true;
    }
    var view = resolveComparisonWizardView();
    if (!view) {
      if (!currentTempTable) return false;
      renderTempTableModalContent(currentTempTable);
      updateTempTableModalFooter();
      return true;
    }
    if (
      view === 'carrier_identification' ||
      view === 'uploading' ||
      view === 'processing' ||
      view === 'failed' ||
      view === 'review'
    ) {
      return renderComparisonFlowView(view);
    }
    var body = byId('agenteComparaTempTableModalBody');
    if (!body) return false;
    setComparisonWizardModalHeader(view);
    body.innerHTML = '';
    if (view === 'upload') {
      renderComparisonWizardUploadBody(body);
    } else if (view === 'ask_table_3') {
      renderComparisonWizardAskTable3Body(body);
    } else if (view === 'tables_ready') {
      renderComparisonWizardTablesReadyBody(body);
    } else {
      return false;
    }
    updateTempTableModalFooter();
    return true;
  }

  function shouldAutoOpenComparisonWizard() {
    if (!comparisonWizardEngaged || comparisonWizardModalSuppressed) return false;
    if (!isComparisonWizardFlowActive()) return false;
    if (uploadInFlight) return false;
    if (comparisonFlowView === 'uploading' || comparisonFlowView === 'carrier_identification') {
      return false;
    }
    var step = comparisonState.currentStep || '';
    if (step !== 'PREPARE_TABLE_1') return false;
    var view = resolveComparisonWizardView();
    if (view !== 'review') return false;
    return isReviewReadyTempTable(currentTempTable);
  }

  function maybeOpenComparisonWizardAfterStatus() {
    if (!isComparisonWizardFlowActive()) return;
    if (!comparisonState.comparisonId) return;
    var resolved = resolveComparisonWizardView();
    if (isTempTableModalOpen()) {
      if (!isComparisonWizardEngaged()) return;
      if (resolved === 'processing') {
        transitionComparisonFlowModal('processing');
        return;
      }
      if (resolved === 'failed') {
        transitionComparisonFlowModal('failed');
        return;
      }
      if (resolved === 'review' && isReviewReadyTempTable(currentTempTable)) {
        transitionComparisonFlowModal('review');
        return;
      }
      if (resolved === 'uploading' || resolved === 'carrier_identification') {
        return;
      }
      renderComparisonWizardModal();
      return;
    }
    if (uploadInFlight) return;
    if (!shouldAutoOpenComparisonWizard()) return;
    if (resolveComparisonWizardView() !== 'review') return;
    if (!isReviewReadyTempTable(currentTempTable)) return;
    renderAndShowComparisonFlowModal('review');
  }

  function openComparisonWizardModal() {
    if (!isComparisonWizardEngaged()) return;
    comparisonWizardModalSuppressed = false;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    tempTableEditMode = false;
    tempTableEditSnapshot = null;
    tempTableModalActiveTab = 'freight';
    var view = resolveComparisonWizardView() || 'upload';
    if (view === 'review' || view === 'processing' || view === 'uploading' ||
        view === 'failed' || view === 'carrier_identification') {
      renderAndShowComparisonFlowModal(view);
      return;
    }
    var rendered = renderComparisonWizardModal();
    if (!rendered) return;
    showTempTableModalShell();
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
  var UPLOAD_PAGE_STATUS_SENDING = 'Enviando documento...';
  var UPLOAD_PAGE_STATUS_PREPARING = 'Preparando envio...';
  var UPLOAD_PAGE_STATUS_PROCESSING = 'Estruturando tabela temporária...';
  var comparisonFlowView = null;
  var comparisonFlowFailedMessage = '';
  var carrierIdentifyPanelHomeParent = null;
  var carrierIdentifyPanelHomeNext = null;
  var comparisonWizardEngaged = false;
  var comparisonWizardModalSuppressed = false;
  var chatInFlight = false;
  var chatUnlocked = false;
  var chatHistory = [];
  var MAX_CHAT_HISTORY = 10;
  var CHAT_LOADING_ID = 'agenteComparaChatLoading';
  var chatCapability = 'locked';
  var chatAvailable = false;
  var chatScopedComparisonId = null;
  var chatUiContext = null;
  var chatSendGeneration = 0;
  var CHAT_BLOCKED_MESSAGE = 'Faça o upload da tabela de frete.';
  var CHAT_LOCKED_PLACEHOLDER = 'Faça o upload da tabela de frete.';
  var CHAT_READY_PLACEHOLDER = 'Pergunte sobre cobertura, UFs, documentos ou peça um resumo...';
  var CHAT_UNLOCKED_MESSAGE = 'O chat está liberado para consultas sobre a comparação vigente.';
  var CHAT_RESPONSIBILITY_MESSAGE = 'As análises apoiam sua avaliação. A decisão final sobre transportadoras e tabelas é responsabilidade do usuário.';
  var CHAT_SUGGESTIONS_READY = [
    'Qual transportadora teve maior cobertura?',
    'Quais UFs apresentaram maior economia potencial?',
    'Explique os fretes sem cálculo.',
    'Crie um resumo executivo.',
    'Quais documentos possuem maior diferença?',
    'Compare as principais taxas.',
    'Quais são os riscos desta análise?'
  ];
  var CHAT_NOT_READY_MESSAGE = 'Faça o upload da tabela de frete.';
  var CHAT_STALE_MESSAGE = 'Resultado desatualizado. Recalcule para liberar o chat.';
  var CHAT_LIMIT_MESSAGE = 'Limite do plano atingido para esta operação.';
  var CHAT_SESSION_MESSAGE = 'É necessário estar logado para conversar com a Agente Compara.';
  var CHAT_PROVIDER_MESSAGE = 'O serviço de inteligência artificial está indisponível no momento. Tente novamente em instantes.';
  var CHAT_PROVIDER_NOT_CONFIGURED_MESSAGE = 'O serviço de inteligência artificial não está configurado neste ambiente.';
  var CHAT_PROVIDER_INIT_MESSAGE = 'Não foi possível iniciar o serviço de inteligência artificial.';
  var CHAT_PROVIDER_TIMEOUT_MESSAGE = 'A resposta demorou mais que o esperado. Tente novamente.';
  var CHAT_PROVIDER_EMPTY_MESSAGE = 'O serviço não conseguiu gerar uma resposta válida. Tente novamente.';
  var CHAT_NETWORK_MESSAGE = 'Não foi possível conectar ao serviço. Verifique sua conexão e tente novamente.';

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
    network: 'Não foi possível conectar ao serviço. Verifique sua conexão e tente novamente.',
    service: 'O serviço de inteligência artificial está indisponível no momento. Tente novamente em instantes.'
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


  function getCalculationBasesByOperation(operation) {
    var wanted = String(operation || '').trim();
    if (!wanted) return [];
    return currentCalculationBases.filter(function (base) {
      return base && String(base.operation || '').trim() === wanted;
    });
  }

  function normalizeAccessorialSemanticText(value) {
    return normalizeTextKey(value).replace(/\s+/g, ' ').trim();
  }

  function calculationBaseSemanticTokens(base) {
    if (!base || typeof base !== 'object') return [];
    var tokens = [];
    var values = []
      .concat(base.id || [])
      .concat(base.label || [])
      .concat(Array.isArray(base.aliases) ? base.aliases : []);
    values.forEach(function (value) {
      var normalized = normalizeAccessorialSemanticText(value);
      if (normalized && tokens.indexOf(normalized) === -1) tokens.push(normalized);
    });
    return tokens;
  }

  function matchCalculationBaseBySemanticText(text) {
    var normalizedText = normalizeAccessorialSemanticText(text);
    if (!normalizedText) return null;
    for (var i = 0; i < currentCalculationBases.length; i += 1) {
      var base = currentCalculationBases[i];
      var tokens = calculationBaseSemanticTokens(base);
      for (var j = 0; j < tokens.length; j += 1) {
        var token = tokens[j];
        if (!token) continue;
        if (
          normalizedText === token
          || normalizedText.indexOf(token) !== -1
          || token.indexOf(normalizedText) !== -1
        ) {
          return base;
        }
      }
    }
    return null;
  }

  function findCalculationBaseByDomainSemantics(item) {
    if (!item || typeof item !== 'object') return null;
    var explicitBase = getCalculationBaseById(item.calculation_base_id || item.editable_calculation_base_id);
    if (explicitBase) return explicitBase;

    var explicitEditableBase = getCalculationBaseById(
      item.edit_resolution && item.edit_resolution.calculation_base_id
    );
    if (explicitEditableBase) return explicitEditableBase;

    var presentation = item.review_presentation && typeof item.review_presentation === 'object'
      ? item.review_presentation
      : null;
    var semanticCandidates = [];
    if (item.calculation_base_label) semanticCandidates.push(item.calculation_base_label);
    if (item.calculation_basis) semanticCandidates.push(item.calculation_basis);
    if (item.raw_calculation_basis) semanticCandidates.push(item.raw_calculation_basis);
    if (presentation && presentation.basis_label) semanticCandidates.push(presentation.basis_label);

    for (var i = 0; i < semanticCandidates.length; i += 1) {
      var matchedBase = matchCalculationBaseBySemanticText(semanticCandidates[i]);
      if (matchedBase) return matchedBase;
    }

    var calculationType = String(item.calculation_type || '').trim();
    var operation = String(item.operation || '').trim();
    var auditVariable = String(item.audit_variable || '').trim();
    var normalizedUnit = normalizeCalculationUnit(item.unit);

    if (
      (calculationType === 'invoice_percentage' || operation === 'percentage_of_variable')
      && normalizedUnit === '%'
      && auditVariable === 'valor_nf'
    ) {
      return getCalculationBaseById('pct_nota_fiscal');
    }

    if (calculationType === 'weight_fraction' || operation === 'ceil_fraction') {
      var fractionBases = getCalculationBasesByOperation('ceil_fraction');
      if (fractionBases.length === 1) return fractionBases[0];
    }

    if (
      (calculationType === 'weight' || calculationType === 'weight_rate' || operation === 'multiply_by_variable')
      && auditVariable === 'peso'
    ) {
      var weightBase = getCalculationBaseById('por_kg');
      if (weightBase) return weightBase;
    }

    if (calculationType === 'fixed_amount' || operation === 'fixed_amount') {
      for (var k = 0; k < semanticCandidates.length; k += 1) {
        var fixedBase = matchCalculationBaseBySemanticText(semanticCandidates[k]);
        if (fixedBase) return fixedBase;
      }
    }

    return null;
  }

  function accessorialMinimumLinkLabel(item, fees, feeIndex) {
    var presentation = item && item.review_presentation && typeof item.review_presentation === 'object'
      ? item.review_presentation
      : null;
    if (presentation && hasFieldValue(presentation.basis_label)) {
      return String(presentation.basis_label);
    }
    var linkedBaseFee = findLinkedAccessorialBaseFee(item, fees || [], feeIndex);
    if (linkedBaseFee && hasFieldValue(linkedBaseFee.name)) {
      return 'Mínimo aplicável a ' + String(linkedBaseFee.name);
    }
    var relatedLabel = '';
    if (presentation && hasFieldValue(presentation.related_to_label)) {
      relatedLabel = String(presentation.related_to_label);
    } else if (hasFieldValue(item && item.related_to)) {
      relatedLabel = String(item.related_to);
    }
    return relatedLabel ? 'Mínimo aplicável a ' + relatedLabel : 'Mínimo sem vínculo válido';
  }

  function resolveAccessorialEditMode(item, fees, feeIndex) {
    if (accessorialFeeIsMinimumAmount(item)) {
      return {
        mode: 'minimum_link',
        label: accessorialMinimumLinkLabel(item, fees, feeIndex)
      };
    }
    var base = findCalculationBaseByDomainSemantics(item);
    if (base) {
      return {
        mode: 'base_select',
        base: base
      };
    }
    return {
      mode: 'unmapped',
      base: null
    };
  }

  function hydrateAccessorialFeesForEditing(fees) {
    if (!Array.isArray(fees)) return;
    fees.forEach(function (fee, feeIndex) {
      if (!fee || typeof fee !== 'object') return;
      var editMode = resolveAccessorialEditMode(fee, fees, feeIndex);
      fee.edit_mode = editMode.mode;
      fee.edit_display_label = editMode.label || '';
      fee.editable_calculation_base_id = editMode.base ? String(editMode.base.id || '') : '';
    });
  }

  function applyResolvedCalculationBaseForSave(fee) {
    if (!fee || typeof fee !== 'object' || accessorialFeeIsMinimumAmount(fee)) return;
    var existingBase = getCalculationBaseById(fee.calculation_base_id);
    if (existingBase) return;
    var resolvedBase = findCalculationBaseByDomainSemantics(fee);
    if (!resolvedBase) return;
    fee.calculation_base_id = resolvedBase.id || null;
    fee.calculation_base_label = resolvedBase.label || '';
    if (
      !hasFieldValue(fee.calculation_basis)
      || normalizeTextKey(fee.calculation_basis) === normalizeTextKey('não mapeado / revisar')
    ) {
      fee.calculation_basis = resolvedBase.label || '';
    }
    if (!hasFieldValue(fee.calculation_type) || String(fee.calculation_type).trim() === 'unknown') {
      fee.calculation_type = resolvedBase.calculation_type || 'unknown';
    }
    if (!hasFieldValue(fee.operation)) {
      fee.operation = resolvedBase.operation || null;
    }
    if (!hasFieldValue(fee.audit_variable) && hasFieldValue(resolvedBase.audit_variable)) {
      fee.audit_variable = resolvedBase.audit_variable;
    }
    if (
      (!fee.operation_parameters || typeof fee.operation_parameters !== 'object')
      && resolvedBase.parameters
    ) {
      fee.operation_parameters = deepCloneValue(resolvedBase.parameters);
    }
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
      button.textContent = 'Processando cÃ¡lculos...';
      button.setAttribute('title', 'Processando cÃ¡lculos comparativos...');
    } else if (step === 'CALCULATION_READY' && comparisonCalculationState.stale) {
      button.classList.remove('is-loading');
      button.textContent = 'Processar Cálculos';
      button.setAttribute('title', 'As configurações foram alteradas. Processe novamente para atualizar os resultados.');
    } else if (
      step === 'CALCULATION_READY' &&
      !comparisonCalculationState.stale &&
      (billing === 'pending' || billing === 'failed')
    ) {
      button.classList.remove('is-loading');
      button.textContent = billing === 'pending' ? 'Finalizar processamento' : 'Tentar novamente';
      button.setAttribute(
        'title',
        billing === 'pending'
          ? 'Cálculo concluído. Finalize o processamento.'
          : 'Regularize o processamento sem recalcular.'
      );
    } else if (step === 'CALCULATION_READY' && !comparisonCalculationState.stale) {
      button.classList.remove('is-loading');
      button.textContent = 'Cálculos concluídos';
      button.setAttribute('title', 'Cálculos comparativos já concluídos para a configuração atual.');
    } else if (step === 'CALCULATION_FAILED') {
      button.classList.remove('is-loading');
      button.textContent = 'Processar novamente';
      button.setAttribute('title', 'Tentar processar os cÃ¡lculos novamente.');
    } else {
      button.classList.remove('is-loading');
      button.textContent = 'Processar Cálculos';
      button.setAttribute(
        'title',
        enable
          ? 'Processar cÃ¡lculos comparativos das transportadoras confirmadas.'
          : 'Conclua a configuração para processar os cÃ¡lculos.'
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
      status.textContent = 'As configurações foram alteradas. Processe novamente para atualizar os resultados.';
    } else if (calcStatus === 'CALCULATION_READY' && billing === 'pending') {
      status.classList.add('is-loading');
      status.textContent = 'Finalizando processamento...';
    } else if (calcStatus === 'CALCULATION_READY' && billing === 'failed') {
      status.classList.add('is-error');
      status.textContent = 'Não foi possível concluir a regularização da execução.';
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
    } else if (step === 'CONFIGURATION_READY') {
      status.textContent = 'As configurações estão prontas. O cálculo será iniciado somente após sua confirmação.';
    } else {
      return;
    }
    container.appendChild(status);
  }

  function comparisonRowIssueDate(row) {
    if (!row || typeof row !== 'object') return '';
    var raw = row.issue_date != null ? row.issue_date : row.data_emissao;
    return raw == null ? '' : String(raw).trim();
  }

  function comparisonRowHasOrigin(rows) {
    var hasCity = false;
    var hasUf = false;
    (rows || []).forEach(function (row) {
      if (row && hasFieldValue(row.origin_city)) hasCity = true;
      if (row && hasFieldValue(row.origin_uf)) hasUf = true;
    });
    return { city: hasCity, uf: hasUf };
  }

  function comparisonRowHasIssueDate(rows) {
    return (rows || []).some(function (row) { return !!comparisonRowIssueDate(row); });
  }

  function comparisonRowHasInvoice(rows) {
    return (rows || []).some(function (row) { return row && row.invoice_value != null && row.invoice_value !== ''; });
  }

  function getComparisonCellForTable(row, tableId) {
    var tableResults = (row && row.table_results) || {};
    var cell = tableResults[tableId];
    if (!cell && Array.isArray(tableResults)) {
      cell = tableResults.find(function (item) {
        return item && item.table_id === tableId;
      });
    }
    return cell || null;
  }

  function isComparisonCellCalculated(cell) {
    if (!cell) return false;
    var status = cell.final_status || cell.status;
    if (status && status !== 'calculated' && status !== 'calculated_with_warnings') return false;
    if (cell.is_partial_value) return false;
    var n = Number(cell.calculated_freight);
    return isFinite(n);
  }

  function isComparisonCellIncomplete(cell) {
    if (!cell) return false;
    var status = cell.final_status || cell.status;
    return status === 'incomplete' || !!cell.is_partial_value;
  }

  function comparisonCellStatusLabel(cell, memory) {
    var status = (memory && memory.status) || (cell && (cell.final_status || cell.status)) || '';
    if (memory && memory.status_label) return String(memory.status_label);
    if (status === 'incomplete') return 'Cálculo incompleto';
    if (status === 'calculated_with_warnings') return 'Calculado com ressalvas';
    if (status === 'calculated') return 'Calculado';
    return 'Não calculado';
  }

  function filterComparativeRows(rows, tables, filters) {
    filters = filters || comparisonResultsUiState.filters;
    tables = tables || [];
    var tableIds = tables.map(function (t) { return t.table_id; });
    return (rows || []).filter(function (row) {
      if (!row) return false;
      var doc = String(row.document_number || '');
      if (filters.documentNumber && doc.toLowerCase().indexOf(String(filters.documentNumber).toLowerCase()) === -1) {
        return false;
      }
      if (filters.destinationUf && String(row.destination_uf || '').toUpperCase() !== String(filters.destinationUf).toUpperCase()) {
        return false;
      }
      if (filters.destinationCity && String(row.destination_city || '').toLowerCase().indexOf(String(filters.destinationCity).toLowerCase()) === -1) {
        return false;
      }
      if (filters.originUf && String(row.origin_uf || '').toUpperCase() !== String(filters.originUf).toUpperCase()) {
        return false;
      }
      if (filters.originCity && String(row.origin_city || '').toLowerCase().indexOf(String(filters.originCity).toLowerCase()) === -1) {
        return false;
      }
      var weight = Number(row.weight);
      if (filters.weightMin !== '' && filters.weightMin != null) {
        var minW = Number(filters.weightMin);
        if (isFinite(minW) && !(isFinite(weight) && weight >= minW)) return false;
      }
      if (filters.weightMax !== '' && filters.weightMax != null) {
        var maxW = Number(filters.weightMax);
        if (isFinite(maxW) && !(isFinite(weight) && weight <= maxW)) return false;
      }
      var issue = comparisonRowIssueDate(row);
      if (filters.dateFrom && (!issue || issue < String(filters.dateFrom))) return false;
      if (filters.dateTo && (!issue || issue > String(filters.dateTo))) return false;
      if (filters.status && filters.status !== 'all' && tableIds.length) {
        var calculatedCount = 0;
        var errorCount = 0;
        tableIds.forEach(function (tid) {
          if (isComparisonCellCalculated(getComparisonCellForTable(row, tid))) calculatedCount += 1;
          else errorCount += 1;
        });
        if (filters.status === 'all_calculated' && calculatedCount !== tableIds.length) return false;
        if (filters.status === 'any_error' && errorCount === 0) return false;
        if (filters.status === 'none_calculated' && calculatedCount !== 0) return false;
      }
      return true;
    });
  }

  function paginateRows(rows, page, pageSize) {
    var total = (rows || []).length;
    var size = Math.max(1, Number(pageSize) || 50);
    var maxPage = Math.max(1, Math.ceil(total / size) || 1);
    var current = Math.min(Math.max(1, Number(page) || 1), maxPage);
    var start = (current - 1) * size;
    return {
      rows: (rows || []).slice(start, start + size),
      page: current,
      pageSize: size,
      total: total,
      maxPage: maxPage
    };
  }

  function appendAnalyticsMetricCard(container, label, value) {
    var card = document.createElement('div');
    card.className = 'agente-compara-analytics-card';
    var labelEl = document.createElement('span');
    labelEl.className = 'agente-compara-analytics-card-label';
    labelEl.textContent = label;
    var valueEl = document.createElement('strong');
    valueEl.className = 'agente-compara-analytics-card-value';
    valueEl.textContent = value == null || value === '' ? '—' : String(value);
    card.appendChild(labelEl);
    card.appendChild(valueEl);
    container.appendChild(card);
  }

  function formatComparisonPercent(value) {
    if (value === null || value === undefined || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toLocaleString('pt-BR', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) + '%';
  }

  function comparisonTablePalette(index) {
    var palette = [
      { fill: 'rgba(0, 196, 140, 0.72)', solid: 'rgb(0, 196, 140)' },
      { fill: 'rgba(99, 140, 255, 0.72)', solid: 'rgb(99, 140, 255)' },
      { fill: 'rgba(255, 176, 72, 0.72)', solid: 'rgb(255, 176, 72)' }
    ];
    return palette[Math.abs(Number(index) || 0) % palette.length];
  }

  function comparisonStatusPalette() {
    return {
      complete: 'rgba(0, 196, 140, 0.75)',
      incomplete: 'rgba(255, 176, 72, 0.75)',
      notCalculated: 'rgba(255, 120, 120, 0.65)',
      comparable: 'rgba(0, 196, 140, 0.75)',
      partial: 'rgba(255, 176, 72, 0.75)',
      inconclusive: 'rgba(160, 160, 184, 0.65)'
    };
  }

  function hasExecutiveComparisonAnalytics(analytics) {
    return !!(
      analytics &&
      analytics.comparability &&
      Array.isArray(analytics.carrier_competitiveness) &&
      analytics.competitive_summary
    );
  }

  function appendAnalyticsNote(container, text) {
    if (!container || !text) return;
    var note = document.createElement('p');
    note.className = 'agente-compara-analytics-note';
    note.textContent = text;
    container.appendChild(note);
  }

  function renderComparisonAnalyticsSummary(container, analytics) {
    if (!container || !analytics) return;
    var section = document.createElement('section');
    section.className = 'agente-compara-analytics-summary';
    section.id = 'agenteComparaAnalyticsSummary';
    section.setAttribute('aria-label', 'Resumo executivo da comparação');

    var title = document.createElement('h3');
    title.className = 'agente-compara-run-summary-title';
    title.textContent = 'Resumo executivo';
    section.appendChild(title);

    var exec = analytics.executive_summary || {};
    var global = analytics.global_summary || {};
    var cmp = analytics.comparability || {};
    var competitive = analytics.competitive_summary || {};
    var geography = analytics.geography || {};
    var carriers = analytics.carrier_competitiveness || analytics.tables || [];

    var globalGrid = document.createElement('div');
    globalGrid.className = 'agente-compara-analytics-grid';
    appendAnalyticsMetricCard(globalGrid, 'Transportadoras comparadas', analytics.table_count);
    appendAnalyticsMetricCard(globalGrid, 'Documentos', global.document_count != null ? global.document_count : analytics.row_count);
    if (hasExecutiveComparisonAnalytics(analytics)) {
      appendAnalyticsMetricCard(globalGrid, 'Documentos comparáveis', cmp.fully_comparable_rows);
      appendAnalyticsMetricCard(globalGrid, 'Cobertura comparável', formatComparisonPercent(cmp.fully_comparable_percentage));
      appendAnalyticsMetricCard(
        globalGrid,
        'Líder em vitórias',
        exec.lead_display_name
          ? String(exec.lead_display_name) +
            (exec.lead_win_percentage != null
              ? ' (' + formatComparisonPercent(exec.lead_win_percentage) + ')'
              : '')
          : 'Sem base decisiva'
      );
      appendAnalyticsMetricCard(globalGrid, 'Economia potencial', formatComparisonMoney(exec.total_potential_savings));
      appendAnalyticsMetricCard(
        globalGrid,
        'Fretes sem cálculo completo',
        exec.rows_without_complete_calculation
      );
      appendAnalyticsMetricCard(
        globalGrid,
        'UFs com base comparável',
        geography.ufs_with_comparable_base != null
          ? geography.ufs_with_comparable_base
          : exec.ufs_with_comparable_base
      );
    } else {
      appendAnalyticsMetricCard(globalGrid, 'Células calculadas', global.calculated_cells);
      appendAnalyticsMetricCard(globalGrid, 'Células com erro', global.error_cells);
      appendAnalyticsMetricCard(globalGrid, 'Cobertura média', formatComparisonPercent(global.calculation_coverage_percentage));
    }
    section.appendChild(globalGrid);

    if (hasExecutiveComparisonAnalytics(analytics)) {
      appendAnalyticsNote(
        section,
        'Economia potencial considerando a diferença entre a menor tarifa calculada e a segunda menor, apenas em documentos totalmente comparáveis.'
      );
      if (competitive.decisive_row_count != null) {
        appendAnalyticsNote(
          section,
          'Base de vitórias: ' +
            String(competitive.decisive_row_count) +
            ' documentos com decisão' +
            (competitive.tie_count ? (' (' + String(competitive.tie_count) + ' empates excluídos do percentual).') : '.')
        );
      }
    } else {
      var tablesTitle = document.createElement('h4');
      tablesTitle.className = 'agente-compara-analytics-subtitle';
      tablesTitle.textContent = 'Indicadores por transportadora';
      section.appendChild(tablesTitle);
      var tablesWrap = document.createElement('div');
      tablesWrap.className = 'agente-compara-analytics-tables';
      carriers.forEach(function (table) {
        var card = document.createElement('article');
        card.className = 'agente-compara-analytics-table-card';
        card.setAttribute('data-table-id', table.table_id || '');
        card.setAttribute('data-slot-number', String(table.slot_number || ''));
        var heading = document.createElement('h5');
        heading.textContent = table.display_name || table.carrier_name || ('Tabela ' + String(table.slot_number || ''));
        card.appendChild(heading);
        var metrics = document.createElement('div');
        metrics.className = 'agente-compara-analytics-table-metrics';
        appendAnalyticsMetricCard(metrics, 'Total calculado (cobertura individual)', formatComparisonMoney(table.calculated_freight_total));
        appendAnalyticsMetricCard(metrics, 'Frete médio coberto', formatComparisonMoney(table.calculated_freight_average));
        appendAnalyticsMetricCard(metrics, 'Linhas calculadas', table.calculated_rows);
        appendAnalyticsMetricCard(metrics, 'Cobertura do cálculo', formatComparisonPercent(table.coverage_percentage));
        card.appendChild(metrics);
        tablesWrap.appendChild(card);
      });
      section.appendChild(tablesWrap);
      appendAnalyticsNote(
        section,
        'Indicadores legados de cobertura individual. Totais brutos não substituem a análise no universo comparável.'
      );
    }

    container.appendChild(section);
  }

  function createFilterField(filtersWrap, opts) {
    var field = document.createElement('div');
    field.className = 'agente-compara-results-filter-field';
    var label = document.createElement('label');
    label.setAttribute('for', opts.id);
    label.textContent = opts.label;
    var input;
    if (opts.type === 'select') {
      input = document.createElement('select');
      (opts.options || []).forEach(function (opt) {
        var option = document.createElement('option');
        option.value = opt.value;
        option.textContent = opt.label;
        if (String(opt.value) === String(opts.value || '')) option.selected = true;
        input.appendChild(option);
      });
    } else {
      input = document.createElement('input');
      input.type = opts.type || 'text';
      if (opts.value != null) input.value = String(opts.value);
      if (opts.placeholder) input.placeholder = opts.placeholder;
      if (opts.min != null) input.min = opts.min;
      if (opts.step != null) input.step = opts.step;
    }
    input.id = opts.id;
    input.className = 'agente-compara-results-filter-input';
    field.appendChild(label);
    field.appendChild(input);
    filtersWrap.appendChild(field);
    return input;
  }

  function renderComparisonResultsFilters(container, result, onChange) {
    var rows = (result && result.comparative_rows) || [];
    var originAvail = comparisonRowHasOrigin(rows);
    var hasDates = comparisonRowHasIssueDate(rows);
    var wrap = document.createElement('section');
    wrap.className = 'agente-compara-results-filters';
    wrap.id = 'agenteComparaResultsFilters';
    wrap.setAttribute('aria-label', 'Filtros do resultado comparativo');

    var heading = document.createElement('h3');
    heading.className = 'agente-compara-run-summary-title';
    heading.textContent = 'Filtros';
    wrap.appendChild(heading);

    var grid = document.createElement('div');
    grid.className = 'agente-compara-results-filters-grid';
    var f = comparisonResultsUiState.filters;

    var docInput = createFilterField(grid, {
      id: 'agenteComparaFilterDocument',
      label: 'Número do documento',
      value: f.documentNumber,
      placeholder: 'Buscar documento'
    });
    var ufInput = createFilterField(grid, {
      id: 'agenteComparaFilterDestinationUf',
      label: 'UF de destino',
      value: f.destinationUf,
      placeholder: 'UF'
    });
    var cityInput = createFilterField(grid, {
      id: 'agenteComparaFilterDestinationCity',
      label: 'Cidade de destino',
      value: f.destinationCity,
      placeholder: 'Cidade'
    });
    var originUfInput = null;
    var originCityInput = null;
    if (originAvail.uf) {
      originUfInput = createFilterField(grid, {
        id: 'agenteComparaFilterOriginUf',
        label: 'UF de origem',
        value: f.originUf,
        placeholder: 'UF origem'
      });
    }
    if (originAvail.city) {
      originCityInput = createFilterField(grid, {
        id: 'agenteComparaFilterOriginCity',
        label: 'Cidade de origem',
        value: f.originCity,
        placeholder: 'Cidade origem'
      });
    }
    var weightMinInput = createFilterField(grid, {
      id: 'agenteComparaFilterWeightMin',
      label: 'Peso mínimo',
      type: 'number',
      value: f.weightMin,
      min: '0',
      step: '0.001'
    });
    var weightMaxInput = createFilterField(grid, {
      id: 'agenteComparaFilterWeightMax',
      label: 'Peso máximo',
      type: 'number',
      value: f.weightMax,
      min: '0',
      step: '0.001'
    });
    var dateFromInput = null;
    var dateToInput = null;
    if (hasDates) {
      dateFromInput = createFilterField(grid, {
        id: 'agenteComparaFilterDateFrom',
        label: 'Data de emissão inicial',
        type: 'date',
        value: f.dateFrom
      });
      dateToInput = createFilterField(grid, {
        id: 'agenteComparaFilterDateTo',
        label: 'Data de emissão final',
        type: 'date',
        value: f.dateTo
      });
    }
    var statusInput = createFilterField(grid, {
      id: 'agenteComparaFilterStatus',
      label: 'Status do cálculo',
      type: 'select',
      value: f.status || 'all',
      options: [
        { value: 'all', label: 'Todas' },
        { value: 'all_calculated', label: 'Calculadas em todas as tabelas' },
        { value: 'any_error', label: 'Com erro em pelo menos uma tabela' },
        { value: 'none_calculated', label: 'Não calculadas em nenhuma tabela' }
      ]
    });
    wrap.appendChild(grid);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-results-filters-actions';
    var applyBtn = document.createElement('button');
    applyBtn.type = 'button';
    applyBtn.className = 'agente-compara-run-btn';
    applyBtn.id = 'agenteComparaApplyFiltersButton';
    applyBtn.textContent = 'Aplicar filtros';
    var clearBtn = document.createElement('button');
    clearBtn.type = 'button';
    clearBtn.className = 'btn btn-sm btn-outline-primary';
    clearBtn.id = 'agenteComparaClearFiltersButton';
    clearBtn.textContent = 'Limpar filtros';
    actions.appendChild(applyBtn);
    actions.appendChild(clearBtn);
    wrap.appendChild(actions);

    function readFilters() {
      return {
        documentNumber: docInput.value || '',
        destinationUf: ufInput.value || '',
        destinationCity: cityInput.value || '',
        originUf: originUfInput ? (originUfInput.value || '') : '',
        originCity: originCityInput ? (originCityInput.value || '') : '',
        weightMin: weightMinInput.value || '',
        weightMax: weightMaxInput.value || '',
        dateFrom: dateFromInput ? (dateFromInput.value || '') : '',
        dateTo: dateToInput ? (dateToInput.value || '') : '',
        status: statusInput.value || 'all'
      };
    }

    applyBtn.addEventListener('click', function () {
      comparisonResultsUiState.filters = readFilters();
      comparisonResultsUiState.page = 1;
      if (typeof onChange === 'function') onChange();
    });
    clearBtn.addEventListener('click', function () {
      resetComparisonResultsUiState();
      if (typeof onChange === 'function') onChange();
    });

    container.appendChild(wrap);
  }

  function renderComparisonResultsPagination(container, pageInfo, onChange) {
    var wrap = document.createElement('div');
    wrap.className = 'agente-compara-results-pagination';
    wrap.id = 'agenteComparaResultsPagination';
    wrap.setAttribute('role', 'navigation');
    wrap.setAttribute('aria-label', 'Paginação dos resultados');

    var info = document.createElement('p');
    info.className = 'agente-compara-results-pagination-info';
    info.setAttribute('role', 'status');
    info.textContent =
      'Exibindo ' +
      String(pageInfo.total ? ((pageInfo.page - 1) * pageInfo.pageSize + 1) : 0) +
      '–' +
      String(Math.min(pageInfo.page * pageInfo.pageSize, pageInfo.total)) +
      ' de ' +
      String(pageInfo.total) +
      ' documentos filtrados';
    wrap.appendChild(info);

    var sizeLabel = document.createElement('label');
    sizeLabel.setAttribute('for', 'agenteComparaResultsPageSize');
    sizeLabel.textContent = 'Linhas por página';
    var sizeSelect = document.createElement('select');
    sizeSelect.id = 'agenteComparaResultsPageSize';
    [25, 50, 100].forEach(function (size) {
      var opt = document.createElement('option');
      opt.value = String(size);
      opt.textContent = String(size);
      if (size === pageInfo.pageSize) opt.selected = true;
      sizeSelect.appendChild(opt);
    });
    sizeSelect.addEventListener('change', function () {
      comparisonResultsUiState.pageSize = Number(sizeSelect.value) || 50;
      comparisonResultsUiState.page = 1;
      if (typeof onChange === 'function') onChange();
    });
    wrap.appendChild(sizeLabel);
    wrap.appendChild(sizeSelect);

    var prev = document.createElement('button');
    prev.type = 'button';
    prev.id = 'agenteComparaResultsPrevPage';
    prev.textContent = 'Anterior';
    prev.disabled = pageInfo.page <= 1;
    prev.setAttribute('aria-disabled', prev.disabled ? 'true' : 'false');
    prev.addEventListener('click', function () {
      if (comparisonResultsUiState.page <= 1) return;
      comparisonResultsUiState.page -= 1;
      if (typeof onChange === 'function') onChange();
    });

    var next = document.createElement('button');
    next.type = 'button';
    next.id = 'agenteComparaResultsNextPage';
    next.textContent = 'Próxima';
    next.disabled = pageInfo.page >= pageInfo.maxPage;
    next.setAttribute('aria-disabled', next.disabled ? 'true' : 'false');
    next.addEventListener('click', function () {
      if (comparisonResultsUiState.page >= pageInfo.maxPage) return;
      comparisonResultsUiState.page += 1;
      if (typeof onChange === 'function') onChange();
    });

    var pageLabel = document.createElement('span');
    pageLabel.textContent = 'Página ' + String(pageInfo.page) + ' de ' + String(pageInfo.maxPage);
    wrap.appendChild(prev);
    wrap.appendChild(pageLabel);
    wrap.appendChild(next);
    container.appendChild(wrap);
  }

  function renderComparisonGeographySection(container, analytics) {
    var geography = analytics.geography || {};
    var ufs = Array.isArray(geography.destination_ufs) ? geography.destination_ufs.slice() : [];
    var ranking = Array.isArray(geography.uf_potential_ranking) ? geography.uf_potential_ranking.slice() : [];
    var carriers = Array.isArray(analytics.carrier_competitiveness)
      ? analytics.carrier_competitiveness
      : (analytics.tables || []);
    if (!ufs.length) return;

    var section = document.createElement('section');
    section.className = 'agente-compara-results-geography';
    section.id = 'agenteComparaResultsGeography';
    section.setAttribute('aria-label', 'Visão geográfica por UF de destino');

    var title = document.createElement('h3');
    title.className = 'agente-compara-run-summary-title';
    title.id = 'agenteComparaGeographyTitle';
    title.textContent = 'Visão geográfica';
    section.appendChild(title);
    appendAnalyticsNote(
      section,
      'Mapa por UF de destino. Vencedora = maior número de vitórias nos documentos comparáveis; empate de vitórias usa menor custo médio comparável.'
    );

    var layout = document.createElement('div');
    layout.className = 'agente-compara-geo-layout';

    var mapWidget = getComparisonDashboardWidget('winner_by_uf_map');
    var mapPanel = document.createElement('div');
    mapPanel.className =
      'agente-compara-geo-map-panel agente-compara-dashboard-widget ' +
      comparisonDashboardWidgetSizeClass(mapWidget ? mapWidget.size : 'wide');
    mapPanel.setAttribute('data-comparison-dashboard-widget', 'winner_by_uf_map');
    mapPanel.setAttribute('data-widget-section', 'geography');
    mapPanel.setAttribute('data-widget-type', 'map');

    var mapHeader = document.createElement('div');
    mapHeader.className = 'agente-compara-dashboard-widget-header';
    var mapTitle = document.createElement('h4');
    mapTitle.className = 'agente-compara-analytics-subtitle';
    mapTitle.textContent = 'Mapa do Brasil por transportadora vencedora';
    mapHeader.appendChild(mapTitle);
    var mapHideBtn = document.createElement('button');
    mapHideBtn.type = 'button';
    mapHideBtn.className = 'btn btn-sm agente-compara-comparison-dashboard-hide-widget-btn';
    mapHideBtn.setAttribute('data-comparison-dashboard-hide-widget', 'winner_by_uf_map');
    mapHideBtn.setAttribute('aria-label', 'Ocultar gráfico Mapa de vencedora por UF');
    mapHideBtn.title = 'Ocultar mapa';
    mapHideBtn.textContent = 'Ocultar';
    mapHeader.appendChild(mapHideBtn);
    mapPanel.appendChild(mapHeader);

    var mapHost = document.createElement('div');
    mapHost.className = 'agente-compara-geo-map-wrap';
    mapHost.id = 'agenteComparaGeoMap';
    mapHost.setAttribute('role', 'img');
    mapHost.setAttribute('aria-label', 'Mapa do Brasil colorido pela transportadora vencedora em cada UF');
    var mapLoading = document.createElement('p');
    mapLoading.className = 'agente-compara-analytics-note';
    mapLoading.textContent = 'Carregando mapa do Brasil…';
    mapHost.appendChild(mapLoading);
    mapPanel.appendChild(mapHost);

    var legend = document.createElement('div');
    legend.className = 'agente-compara-geo-legend';
    legend.id = 'agenteComparaGeoMapLegend';
    legend.setAttribute('aria-label', 'Legenda do mapa por UF');
    carriers.forEach(function (carrier, idx) {
      var item = document.createElement('span');
      item.className = 'agente-compara-geo-legend-item';
      var swatch = document.createElement('span');
      swatch.className = 'agente-compara-geo-legend-swatch';
      swatch.style.backgroundColor = comparisonTablePalette(idx).solid;
      item.appendChild(swatch);
      var label = document.createElement('span');
      label.textContent = carrier.display_name || carrier.carrier_name || ('Tabela ' + String(carrier.slot_number || ''));
      item.appendChild(label);
      legend.appendChild(item);
    });
    [
      { label: 'Empate', color: comparisonBrasilMapStatusColors().tie },
      { label: 'Sem base comparável', color: comparisonBrasilMapStatusColors().noBase },
      { label: 'Sem dados nesta base', color: comparisonBrasilMapStatusColors().noData }
    ].forEach(function (entry) {
      var item = document.createElement('span');
      item.className = 'agente-compara-geo-legend-item';
      var swatch = document.createElement('span');
      swatch.className = 'agente-compara-geo-legend-swatch';
      swatch.style.backgroundColor = entry.color;
      item.appendChild(swatch);
      var label = document.createElement('span');
      label.textContent = entry.label;
      item.appendChild(label);
      legend.appendChild(item);
    });
    var lowItem = document.createElement('span');
    lowItem.className = 'agente-compara-geo-legend-item';
    var lowSwatch = document.createElement('span');
    lowSwatch.className = 'agente-compara-geo-legend-swatch is-low-sample';
    lowItem.appendChild(lowSwatch);
    var lowLabel = document.createElement('span');
    lowLabel.textContent = 'Baixa amostra (borda tracejada)';
    lowItem.appendChild(lowLabel);
    legend.appendChild(lowItem);
    mapPanel.appendChild(legend);

    var detail = document.createElement('div');
    detail.className = 'agente-compara-geo-map-detail';
    detail.id = 'agenteComparaGeoMapDetail';
    detail.setAttribute('aria-live', 'polite');
    detail.hidden = true;
    mapPanel.appendChild(detail);

    var fallback = document.createElement('p');
    fallback.className = 'agente-compara-geo-map-fallback';
    fallback.id = 'agenteComparaGeoMapFallback';
    fallback.textContent =
      'Resumo textual: use a tabela e o ranking ao lado. Selecione uma UF no mapa (clique, toque ou teclado) para ver o detalhe.';
    mapPanel.appendChild(fallback);
    layout.appendChild(mapPanel);

    var sidePanel = document.createElement('div');
    sidePanel.className = 'agente-compara-geo-side-panel';

    var rankWidget = getComparisonDashboardWidget('uf_savings_ranking');
    var rankWrap = document.createElement('div');
    rankWrap.className =
      'agente-compara-geo-ranking agente-compara-dashboard-widget ' +
      comparisonDashboardWidgetSizeClass(rankWidget ? rankWidget.size : 'standard');
    rankWrap.setAttribute('data-comparison-dashboard-widget', 'uf_savings_ranking');
    rankWrap.setAttribute('data-widget-section', 'geography');
    rankWrap.setAttribute('data-widget-type', 'ranking');

    var rankHeader = document.createElement('div');
    rankHeader.className = 'agente-compara-dashboard-widget-header';
    var rankTitle = document.createElement('h4');
    rankTitle.className = 'agente-compara-analytics-subtitle';
    rankTitle.textContent = 'Ranking de UFs por economia potencial';
    rankHeader.appendChild(rankTitle);
    var rankHideBtn = document.createElement('button');
    rankHideBtn.type = 'button';
    rankHideBtn.className = 'btn btn-sm agente-compara-comparison-dashboard-hide-widget-btn';
    rankHideBtn.setAttribute('data-comparison-dashboard-hide-widget', 'uf_savings_ranking');
    rankHideBtn.setAttribute('aria-label', 'Ocultar gráfico Ranking geográfico');
    rankHideBtn.title = 'Ocultar ranking';
    rankHideBtn.textContent = 'Ocultar';
    rankHeader.appendChild(rankHideBtn);
    rankWrap.appendChild(rankHeader);
    appendAnalyticsNote(rankWrap, 'Top 10 por economia potencial total no universo comparável.');

    if (!ranking.length) {
      appendAnalyticsNote(rankWrap, 'Nenhuma UF com base comparável neste resultado.');
    } else {
      var list = document.createElement('ol');
      list.className = 'agente-compara-geo-ranking-list';
      ranking.slice(0, 10).forEach(function (item) {
        var li = document.createElement('li');
        if (item.low_sample) li.className = 'is-low-sample';
        var label = document.createElement('strong');
        label.textContent = item.uf_label || item.uf || 'N/D';
        li.appendChild(label);
        var detailText = document.createElement('span');
        detailText.textContent =
          ' — ' +
          (item.winner_display_name || (item.is_tie ? 'Empate' : 'Sem vencedora')) +
          ' | Comparáveis: ' +
          String(item.comparable_row_count || 0) +
          ' | Economia: ' +
          formatComparisonMoney(item.total_potential_savings) +
          (item.low_sample ? ' | Baixa amostra' : '');
        li.appendChild(detailText);
        list.appendChild(li);
      });
      rankWrap.appendChild(list);
    }
    sidePanel.appendChild(rankWrap);
    layout.appendChild(sidePanel);
    section.appendChild(layout);

    var matrixWidget = getComparisonDashboardWidget('uf_comparison_matrix');
    var matrixWrap = document.createElement('div');
    matrixWrap.className =
      'agente-compara-geo-matrix-wrap agente-compara-dashboard-widget ' +
      comparisonDashboardWidgetSizeClass(matrixWidget ? matrixWidget.size : 'full');
    matrixWrap.setAttribute('data-comparison-dashboard-widget', 'uf_comparison_matrix');
    matrixWrap.setAttribute('data-widget-section', 'geography');
    matrixWrap.setAttribute('data-widget-type', 'matrix');

    var matrixHeader = document.createElement('div');
    matrixHeader.className = 'agente-compara-dashboard-widget-header';
    var matrixTitle = document.createElement('h4');
    matrixTitle.className = 'agente-compara-analytics-subtitle';
    matrixTitle.textContent = 'Tabela geográfica por UF';
    matrixHeader.appendChild(matrixTitle);
    var matrixHideBtn = document.createElement('button');
    matrixHideBtn.type = 'button';
    matrixHideBtn.className = 'btn btn-sm agente-compara-comparison-dashboard-hide-widget-btn';
    matrixHideBtn.setAttribute('data-comparison-dashboard-hide-widget', 'uf_comparison_matrix');
    matrixHideBtn.setAttribute('aria-label', 'Ocultar gráfico Matriz geográfica');
    matrixHideBtn.title = 'Ocultar matriz';
    matrixHideBtn.textContent = 'Ocultar';
    matrixHeader.appendChild(matrixHideBtn);
    matrixWrap.appendChild(matrixHeader);
    appendAnalyticsNote(
      matrixWrap,
      'Colunas: documentos comparáveis, vencedora, vitórias por transportadora, economia potencial e aviso de amostra.'
    );

    var table = document.createElement('table');
    table.className = 'agente-compara-geo-matrix';
    table.setAttribute('aria-label', 'Tabela geográfica de vitórias por UF e transportadora');
    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');
    ['UF', 'Comparáveis'].concat(
      carriers.map(function (c) { return c.display_name || c.carrier_name || ('Tabela ' + String(c.slot_number || '')); }),
      ['Vencedora', 'Economia potencial', 'Amostra']
    ).forEach(function (label) {
      var th = document.createElement('th');
      th.scope = 'col';
      th.textContent = label;
      headRow.appendChild(th);
    });
    thead.appendChild(headRow);
    table.appendChild(thead);

    var tbody = document.createElement('tbody');
    ufs
      .slice()
      .sort(function (a, b) {
        var la = String(a.uf_label || a.uf || '');
        var lb = String(b.uf_label || b.uf || '');
        return la.localeCompare(lb, 'pt-BR');
      })
      .forEach(function (ufItem) {
        var tr = document.createElement('tr');
        if (ufItem.low_sample) tr.className = 'is-low-sample';
        if (!ufItem.has_comparable_base) tr.className = (tr.className ? tr.className + ' ' : '') + 'is-no-base';

        function addCell(text, className) {
          var td = document.createElement('td');
          if (className) td.className = className;
          td.textContent = text == null || text === '' ? '—' : String(text);
          tr.appendChild(td);
          return td;
        }

        addCell(ufItem.uf_label || ufItem.uf || 'N/D');
        addCell(
          String(ufItem.comparable_row_count != null ? ufItem.comparable_row_count : 0) +
            '/' +
            String(ufItem.row_count != null ? ufItem.row_count : 0)
        );

        var byTable = {};
        (ufItem.tables || []).forEach(function (t) {
          byTable[t.table_id] = t;
        });
        carriers.forEach(function (carrier, idx) {
          var cell = byTable[carrier.table_id] || {};
          var td = addCell(cell.wins != null ? cell.wins : 0, 'agente-compara-geo-wins');
          td.style.backgroundColor = comparisonTablePalette(idx).fill.replace('0.72', '0.18');
          var avg = cell.comparable_freight_average;
          td.title =
            'Vitórias: ' +
            String(cell.wins != null ? cell.wins : 0) +
            (avg != null ? ' | Custo médio comparável: ' + formatComparisonMoney(avg) : '') +
            (cell.coverage_percentage != null
              ? ' | Cobertura: ' + formatComparisonPercent(cell.coverage_percentage)
              : '');
        });

        if (!ufItem.has_comparable_base || ufItem.map_status === 'no_comparable_base') {
          addCell('Sem base comparável', 'agente-compara-geo-winner');
        } else if (ufItem.is_tie || ufItem.map_status === 'tie' || !ufItem.winner_display_name) {
          addCell('Empate', 'agente-compara-geo-winner');
        } else {
          var winText = String(ufItem.winner_display_name);
          if (ufItem.winner_share != null) {
            winText += ' (' + formatComparisonPercent(ufItem.winner_share) + ')';
          }
          addCell(winText, 'agente-compara-geo-winner');
        }
        addCell(formatComparisonMoney(ufItem.total_potential_savings));
        addCell(ufItem.low_sample ? 'Baixa amostra' : (ufItem.has_comparable_base ? 'Robusta' : 'Sem base'));
        tbody.appendChild(tr);
      });
    table.appendChild(tbody);
    matrixWrap.appendChild(table);
    section.appendChild(matrixWrap);
    container.appendChild(section);

    if (!isComparisonDashboardWidgetHidden('winner_by_uf_map')) {
      mountComparisonBrasilMap(mapHost, detail, ufs, carriers);
    } else {
      while (mapHost.firstChild) mapHost.removeChild(mapHost.firstChild);
    }
  }

  function ensureComparisonGeographyWidgetsMounted(analytics) {
    if (!analytics || !hasExecutiveComparisonAnalytics(analytics)) return;
    var mapHost = document.getElementById('agenteComparaGeoMap');
    var detail = document.getElementById('agenteComparaGeoMapDetail');
    if (!mapHost || isComparisonDashboardWidgetHidden('winner_by_uf_map')) return;
    if (mapHost.querySelector('svg path[id]')) return;
    var geography = analytics.geography || {};
    var ufs = Array.isArray(geography.destination_ufs) ? geography.destination_ufs : [];
    var carriers = Array.isArray(analytics.carrier_competitiveness)
      ? analytics.carrier_competitiveness
      : (analytics.tables || []);
    if (!ufs.length) return;
    mountComparisonBrasilMap(mapHost, detail, ufs, carriers);
  }

  var comparisonBrasilMapSvgCache = null;

  function comparisonBrasilMapSvgUrl() {
    var shell = document.getElementById('agenteComparaShell');
    var fromDom = shell && shell.getAttribute('data-brasil-ufs-svg-url');
    return fromDom || '/static/img/brasil-ufs.svg';
  }

  function comparisonBrasilMapStatusColors() {
    return {
      tie: '#8B8BA3',
      noBase: '#4A4A62',
      noData: '#2F2F40'
    };
  }

  function comparisonUfMapStatus(ufItem) {
    if (!ufItem) return 'no_data';
    if (ufItem.map_status) return String(ufItem.map_status);
    if (!ufItem.has_comparable_base) return 'no_comparable_base';
    if (ufItem.is_tie || !ufItem.winner_display_name) return 'tie';
    return 'winner';
  }

  function comparisonUfWinnerLabel(ufItem) {
    var status = comparisonUfMapStatus(ufItem);
    if (status === 'no_comparable_base') return 'Sem base comparável';
    if (status === 'tie') return 'Empate';
    if (status === 'winner' && ufItem && ufItem.winner_display_name) return String(ufItem.winner_display_name);
    return 'Sem dados nesta base';
  }

  function buildComparisonUfDetailLines(ufItem, carriers) {
    var lines = [];
    if (!ufItem) {
      lines.push('UF sem documentos nesta comparação.');
      return lines;
    }
    var ufCode = ufItem.uf_label || ufItem.uf || 'N/D';
    lines.push('UF: ' + ufCode);
    lines.push('Vencedora: ' + comparisonUfWinnerLabel(ufItem));
    lines.push(
      'Documentos comparáveis: ' +
        String(ufItem.comparable_row_count != null ? ufItem.comparable_row_count : 0) +
        ' de ' +
        String(ufItem.row_count != null ? ufItem.row_count : 0)
    );
    var byTable = {};
    (ufItem.tables || []).forEach(function (t) {
      byTable[t.table_id] = t;
    });
    carriers.forEach(function (carrier) {
      var cell = byTable[carrier.table_id] || {};
      var name = carrier.display_name || carrier.carrier_name || ('Tabela ' + String(carrier.slot_number || ''));
      lines.push(
        name +
          ' — vitórias: ' +
          String(cell.wins != null ? cell.wins : 0) +
          '; custo médio comparável: ' +
          formatComparisonMoney(cell.comparable_freight_average) +
          '; cobertura: ' +
          formatComparisonPercent(cell.coverage_percentage)
      );
    });
    lines.push('Economia potencial total: ' + formatComparisonMoney(ufItem.total_potential_savings));
    if (ufItem.average_potential_savings != null) {
      lines.push('Economia potencial média: ' + formatComparisonMoney(ufItem.average_potential_savings));
    }
    if (ufItem.low_sample) lines.push('Sinalização: baixa amostra.');
    if (comparisonUfMapStatus(ufItem) === 'tie') lines.push('Indicação: empate explícito na UF.');
    if (comparisonUfMapStatus(ufItem) === 'no_comparable_base') {
      lines.push('Indicação: sem base comparável suficiente.');
    }
    return lines;
  }

  function fillComparisonUfDetailPanel(detailEl, ufItem, carriers, ufCode) {
    if (!detailEl) return;
    while (detailEl.firstChild) detailEl.removeChild(detailEl.firstChild);
    detailEl.hidden = false;
    var heading = document.createElement('p');
    heading.className = 'agente-compara-geo-map-detail-title';
    heading.textContent = 'Detalhe da UF ' + String(ufCode || (ufItem && (ufItem.uf_label || ufItem.uf)) || '');
    detailEl.appendChild(heading);
    var list = document.createElement('ul');
    list.className = 'agente-compara-geo-map-detail-list';
    buildComparisonUfDetailLines(ufItem, carriers).forEach(function (line) {
      var li = document.createElement('li');
      li.textContent = line;
      list.appendChild(li);
    });
    detailEl.appendChild(list);
    var analyzeBtn = document.createElement('button');
    analyzeBtn.type = 'button';
    analyzeBtn.className = 'btn btn-sm btn-outline-secondary agente-compara-contextual-chat-action';
    analyzeBtn.setAttribute('data-chat-contextual-action', 'analyze_uf');
    analyzeBtn.textContent = 'Analisar esta UF';
    analyzeBtn.addEventListener('click', function (event) {
      event.preventDefault();
      var uf = String(ufCode || (ufItem && (ufItem.uf_label || ufItem.uf)) || '').toUpperCase();
      prepareContextualChatQuestion('Analise a UF ' + uf + '.', {
        intent_hint: 'geography',
        selected_uf: uf,
        selected_widget: 'winner_by_uf_map',
        active_view: 'dashboard',
        visual_focus: { selected_uf: uf, destination_uf: uf }
      });
    });
    detailEl.appendChild(analyzeBtn);
  }

  function comparisonUfMapFillColor(ufItem, carriers) {
    var statusColors = comparisonBrasilMapStatusColors();
    var status = comparisonUfMapStatus(ufItem);
    if (status === 'no_comparable_base') return statusColors.noBase;
    if (status === 'tie') return statusColors.tie;
    if (status === 'winner' && ufItem && ufItem.winner_table_id) {
      var idx = -1;
      for (var i = 0; i < carriers.length; i += 1) {
        if (carriers[i].table_id === ufItem.winner_table_id) {
          idx = i;
          break;
        }
      }
      if (idx >= 0) return comparisonTablePalette(idx).solid;
    }
    return statusColors.noData;
  }

  function paintComparisonBrasilMap(svg, ufByCode, carriers, detailEl) {
    if (!svg) return;
    svg.setAttribute('preserveAspectRatio', 'xMidYMid meet');
    svg.setAttribute('role', 'img');
    svg.setAttribute('aria-label', 'Mapa do Brasil por UF com transportadora vencedora');
    var paths = svg.querySelectorAll('path[id]');
    var selectedPath = null;

    function selectPath(path) {
      if (selectedPath) selectedPath.classList.remove('is-selected');
      selectedPath = path || null;
      if (selectedPath) selectedPath.classList.add('is-selected');
    }

    Array.prototype.forEach.call(paths, function (path) {
      var ufCode = String(path.getAttribute('id') || '').toUpperCase();
      var ufName = path.getAttribute('name') || ufCode;
      var ufItem = ufByCode[ufCode] || null;
      var fill = comparisonUfMapFillColor(ufItem, carriers);
      path.setAttribute('fill', fill);
      path.setAttribute('tabindex', '0');
      path.setAttribute('role', 'button');
      var winnerLabel = comparisonUfWinnerLabel(ufItem);
      var aria =
        ufName +
        ' (' +
        ufCode +
        '). Vencedora: ' +
        winnerLabel +
        '. Comparáveis: ' +
        String(ufItem && ufItem.comparable_row_count != null ? ufItem.comparable_row_count : 0) +
        ' de ' +
        String(ufItem && ufItem.row_count != null ? ufItem.row_count : 0) +
        '.';
      if (ufItem && ufItem.low_sample) {
        aria += ' Baixa amostra.';
        path.classList.add('is-low-sample');
      } else {
        path.classList.remove('is-low-sample');
      }
      path.setAttribute('aria-label', aria);
      path.setAttribute('title', aria);

      function activate() {
        selectPath(path);
        fillComparisonUfDetailPanel(detailEl, ufItem, carriers, ufCode);
      }

      path.addEventListener('click', activate);
      path.addEventListener('keydown', function (event) {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          activate();
        }
      });
      path.addEventListener('mouseenter', function () {
        fillComparisonUfDetailPanel(detailEl, ufItem, carriers, ufCode);
      });
      path.addEventListener('focus', function () {
        fillComparisonUfDetailPanel(detailEl, ufItem, carriers, ufCode);
      });
    });
  }

  function mountComparisonBrasilMap(mapHost, detailEl, ufs, carriers) {
    if (!mapHost) return;
    var ufByCode = {};
    (ufs || []).forEach(function (item) {
      var code = String(item.uf || item.uf_label || '').toUpperCase();
      if (code && code !== 'N/D') ufByCode[code] = item;
    });

    function applySvgText(svgText) {
      while (mapHost.firstChild) mapHost.removeChild(mapHost.firstChild);
      var parser = new DOMParser();
      var doc = parser.parseFromString(svgText, 'image/svg+xml');
      var parsed = doc.documentElement;
      if (!parsed || parsed.nodeName.toLowerCase() !== 'svg' || doc.querySelector('parsererror')) {
        var err = document.createElement('p');
        err.className = 'agente-compara-analytics-note';
        err.textContent = 'Não foi possível renderizar o mapa local. Use a tabela geográfica abaixo.';
        mapHost.appendChild(err);
        return;
      }
      var svg = document.importNode(parsed, true);
      mapHost.appendChild(svg);
      paintComparisonBrasilMap(svg, ufByCode, carriers || [], detailEl);
    }

    if (comparisonBrasilMapSvgCache) {
      applySvgText(comparisonBrasilMapSvgCache);
      return;
    }

    fetch(comparisonBrasilMapSvgUrl(), { credentials: 'same-origin' })
      .then(function (response) {
        if (!response.ok) throw new Error('svg_http');
        return response.text();
      })
      .then(function (text) {
        comparisonBrasilMapSvgCache = text;
        applySvgText(text);
      })
      .catch(function () {
        while (mapHost.firstChild) mapHost.removeChild(mapHost.firstChild);
        var err = document.createElement('p');
        err.className = 'agente-compara-analytics-note';
        err.textContent = 'Mapa indisponível no momento. A tabela e o ranking geográficos permanecem abaixo.';
        mapHost.appendChild(err);
      });
  }

  function paintComparisonDashboardChart(widgetKey, canvas, analytics) {
    if (!widgetKey || !canvas || !analytics || typeof window.Chart !== 'function') return null;
    if (isComparisonDashboardWidgetHidden(widgetKey)) return null;
    var widget = getComparisonDashboardWidget(widgetKey);
    if (!widget || widget.type !== 'chart') return null;
    var executive = hasExecutiveComparisonAnalytics(analytics);
    var carriers = executive
      ? (analytics.carrier_competitiveness || [])
      : (analytics.tables || []);
    if (!Array.isArray(carriers) || !carriers.length) return null;

    var labels = carriers.map(function (t) {
      return t.display_name || t.carrier_name || ('Tabela ' + String(t.slot_number || ''));
    });
    var colors = carriers.map(function (_t, idx) {
      return comparisonTablePalette(idx).fill;
    });
    var statusColors = comparisonStatusPalette();
    var completeRows = carriers.map(function (t) { return Number(t.calculated_rows) || 0; });
    var incompleteRows = carriers.map(function (t) { return Number(t.incomplete_rows) || 0; });
    var notCalcRows = carriers.map(function (t) {
      if (t.not_calculated_rows != null) return Number(t.not_calculated_rows) || 0;
      return Number(t.uncalculated_rows != null ? t.uncalculated_rows : t.error_rows) || 0;
    });
    var withoutComplete = carriers.map(function (t, idx) {
      if (t.rows_without_complete_calculation != null) {
        return Number(t.rows_without_complete_calculation) || 0;
      }
      return (incompleteRows[idx] || 0) + (notCalcRows[idx] || 0);
    });
    var commonOpts = {
      responsive: true,
      maintainAspectRatio: true,
      plugins: {
        legend: { display: true },
        tooltip: { enabled: true }
      }
    };
    var summary = document.getElementById('agenteComparaChartSummary_' + (widget.canvasKey || widgetKey));
    function setSummary(text) {
      if (summary) summary.textContent = text || '';
    }

    var chart = null;
    if (widgetKey === 'coverage_by_carrier') {
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
          labels: labels,
          datasets: executive
            ? [
                { label: 'Completo', data: completeRows, backgroundColor: statusColors.complete },
                { label: 'Incompleto', data: incompleteRows, backgroundColor: statusColors.incomplete },
                { label: 'Não calculado', data: notCalcRows, backgroundColor: statusColors.notCalculated }
              ]
            : [
                { label: 'Cobertura (%)', data: carriers.map(function (t) { return Number(t.coverage_percentage) || 0; }), backgroundColor: colors }
              ]
        },
        options: Object.assign({}, commonOpts, {
          indexAxis: executive ? 'y' : 'x',
          scales: executive
            ? { x: { stacked: true, beginAtZero: true }, y: { stacked: true } }
            : { y: { beginAtZero: true, max: 100 } }
        })
      });
      setSummary(
        labels.map(function (name, idx) {
          return name + ': ' + formatComparisonPercent(carriers[idx].coverage_percentage);
        }).join(' · ')
      );
    } else if (widgetKey === 'freight_without_complete_calculation') {
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
          labels: labels,
          datasets: executive
            ? [{ label: 'Sem cálculo completo', data: withoutComplete, backgroundColor: statusColors.notCalculated }]
            : [
                { label: 'Calculadas', data: completeRows, backgroundColor: statusColors.complete },
                { label: 'Não calculadas', data: withoutComplete, backgroundColor: statusColors.notCalculated }
              ]
        },
        options: Object.assign({}, commonOpts, {
          indexAxis: 'y',
          scales: { x: { beginAtZero: true } }
        })
      });
      setSummary(
        labels.map(function (name, idx) {
          return name + ': ' + String(withoutComplete[idx]);
        }).join(' · ')
      );
    } else if (widgetKey === 'comparability' && executive) {
      var cmp = analytics.comparability || {};
      var cmpLabels = ['Totalmente comparáveis', 'Parcialmente comparáveis', 'Inconclusivos'];
      var cmpData = [
        Number(cmp.fully_comparable_rows) || 0,
        Number(cmp.partially_comparable_rows) || 0,
        Number(cmp.inconclusive_rows) || 0
      ];
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'doughnut',
        data: {
          labels: cmpLabels,
          datasets: [{
            data: cmpData,
            backgroundColor: [statusColors.comparable, statusColors.partial, statusColors.inconclusive]
          }]
        },
        options: commonOpts
      });
      setSummary(
        cmpLabels.map(function (name, idx) {
          return name + ': ' + String(cmpData[idx]);
        }).join(' · ')
      );
    } else if (widgetKey === 'carrier_wins' && executive) {
      var wins = carriers.map(function (t) { return Number(t.wins) || 0; });
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{ label: 'Vitórias', data: wins, backgroundColor: colors }]
        },
        options: Object.assign({}, commonOpts, {
          indexAxis: 'y',
          scales: { x: { beginAtZero: true } }
        })
      });
      var decisive = (analytics.competitive_summary || {}).decisive_row_count || 0;
      setSummary(
        labels.map(function (name, idx) {
          var pct = carriers[idx].win_percentage;
          return name + ': ' + String(wins[idx]) + (pct != null ? ' (' + formatComparisonPercent(pct) + ')' : '');
        }).join(' · ') +
          (decisive ? ' · Base decisiva: ' + String(decisive) : '')
      );
    } else if (widgetKey === 'comparable_average_cost') {
      var avgData = executive
        ? carriers.map(function (t) { return Number(t.comparable_freight_average) || 0; })
        : carriers.map(function (t) { return Number(t.calculated_freight_total) || 0; });
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{
            label: executive ? 'Frete médio comparável (R$)' : 'Total calculado (R$)',
            data: avgData,
            backgroundColor: colors
          }]
        },
        options: Object.assign({}, commonOpts, {
          scales: { y: { beginAtZero: true } }
        })
      });
      setSummary(
        labels.map(function (name, idx) {
          if (executive) {
            return (
              name +
              ': ' +
              formatComparisonMoney(carriers[idx].comparable_freight_average) +
              (carriers[idx].comparable_freight_per_kg_average != null
                ? ' | frete/kg ' + formatComparisonMoney(carriers[idx].comparable_freight_per_kg_average)
                : '')
            );
          }
          return name + ': ' + formatComparisonMoney(carriers[idx].calculated_freight_total);
        }).join(' · ')
      );
    } else if (widgetKey === 'potential_savings' && executive) {
      var savings = carriers.map(function (t) { return Number(t.potential_savings_when_winner) || 0; });
      chart = new window.Chart(canvas.getContext('2d'), {
        type: 'bar',
        data: {
          labels: labels,
          datasets: [{ label: 'Economia potencial (R$)', data: savings, backgroundColor: colors }]
        },
        options: Object.assign({}, commonOpts, {
          indexAxis: 'y',
          scales: { x: { beginAtZero: true } }
        })
      });
      setSummary(
        'Potencial estimado no universo comparável; não representa economia realizada. ' +
          labels.map(function (name, idx) {
            return name + ': ' + formatComparisonMoney(savings[idx]);
          }).join(' · ')
      );
    }

    if (chart) registerComparisonResultChart(widgetKey, chart);
    return chart;
  }

  function renderComparisonResultCharts(container, analytics) {
    if (!container || !analytics) return;
    destroyComparisonResultCharts();
    loadComparisonDashboardPreferences();

    var executive = hasExecutiveComparisonAnalytics(analytics);
    var carriers = executive
      ? (analytics.carrier_competitiveness || [])
      : (analytics.tables || []);
    if (!Array.isArray(carriers) || !carriers.length) return;

    var section = document.createElement('section');
    section.className = 'agente-compara-results-charts';
    section.id = 'agenteComparaResultsCharts';
    section.setAttribute('aria-label', 'Indicadores executivos da comparação');

    var title = document.createElement('h3');
    title.className = 'agente-compara-run-summary-title';
    title.textContent = executive ? 'Confiabilidade e competitividade' : 'Gráficos comparativos';
    section.appendChild(title);

    var desc = document.createElement('p');
    desc.className = 'agente-compara-results-charts-desc';
    desc.textContent = executive
      ? 'Cobertura usa o universo total por transportadora. Vitórias, custo médio e economia usam somente documentos totalmente comparáveis.'
      : 'Indicadores legados por transportadora. Totais brutos não substituem a análise no universo comparável.';
    section.appendChild(desc);

    var chartDefs = executive
      ? [
          {
            widgetKey: 'coverage_by_carrier',
            canvasKey: 'coverage',
            chartTitle: 'Cobertura por transportadora',
            chartDesc: 'Cobertura considera cÃ¡lculos completos por tabela no universo total.',
            sectionKey: 'reliability'
          },
          {
            widgetKey: 'freight_without_complete_calculation',
            canvasKey: 'without_complete',
            chartTitle: 'Fretes sem cálculo completo',
            chartDesc: 'Soma de incompletos e não calculados por transportadora.',
            sectionKey: 'reliability'
          },
          {
            widgetKey: 'comparability',
            canvasKey: 'comparability',
            chartTitle: 'Comparáveis × parciais × inconclusivos',
            chartDesc: 'Classificação do universo total de documentos.',
            sectionKey: 'reliability'
          },
          {
            widgetKey: 'carrier_wins',
            canvasKey: 'wins',
            chartTitle: 'Vitórias por transportadora',
            chartDesc: 'Considera somente documentos calculados por todas as transportadoras, excluindo empates do percentual.',
            sectionKey: 'competitiveness'
          },
          {
            widgetKey: 'comparable_average_cost',
            canvasKey: 'avg_cost',
            chartTitle: 'Custo médio comparável',
            chartDesc: 'Média de frete no universo comparável (mesma base amostral).',
            sectionKey: 'competitiveness'
          },
          {
            widgetKey: 'potential_savings',
            canvasKey: 'potential_savings',
            chartTitle: 'Economia potencial por vencedora',
            chartDesc: 'Potencial estimado no universo comparável; não representa economia realizada.',
            sectionKey: 'competitiveness'
          }
        ]
      : [
          {
            widgetKey: 'coverage_by_carrier',
            canvasKey: 'coverage',
            chartTitle: 'Cobertura de cálculo por tabela',
            chartDesc: 'Percentual de linhas com cálculo completo em cada transportadora.',
            sectionKey: 'reliability'
          },
          {
            widgetKey: 'freight_without_complete_calculation',
            canvasKey: 'without_complete',
            chartTitle: 'Calculadas versus não calculadas',
            chartDesc: 'Quantidade de linhas calculadas e não calculadas por tabela.',
            sectionKey: 'reliability'
          },
          {
            widgetKey: 'comparable_average_cost',
            canvasKey: 'avg_cost',
            chartTitle: 'Total calculado por cobertura individual',
            chartDesc: 'Soma operacional por tabela. Não substitui a análise no universo comparável.',
            sectionKey: 'competitiveness'
          }
        ];

    var gridsBySection = {};

    function ensureSection(sectionKey, sectionTitle) {
      if (gridsBySection[sectionKey]) return gridsBySection[sectionKey];
      var block = document.createElement('div');
      block.className = 'agente-compara-dashboard-section-block';
      block.setAttribute('data-comparison-dashboard-section-block', sectionKey);
      var heading = document.createElement('h4');
      heading.className = 'agente-compara-analytics-subtitle';
      if (sectionKey === 'reliability') heading.id = 'agenteComparaReliabilityTitle';
      if (sectionKey === 'competitiveness') heading.id = 'agenteComparaCompetitivenessTitle';
      heading.textContent = sectionTitle;
      block.appendChild(heading);
      var grid = document.createElement('div');
      grid.className = 'agente-compara-results-charts-grid agente-compara-section-grid';
      grid.setAttribute('data-comparison-dashboard-section-grid', sectionKey);
      block.appendChild(grid);
      section.appendChild(block);
      gridsBySection[sectionKey] = grid;
      return grid;
    }

    function addChartCard(def) {
      var widget = getComparisonDashboardWidget(def.widgetKey);
      var sectionTitle =
        def.sectionKey === 'competitiveness'
          ? 'Competitividade de custo'
          : (executive ? 'Confiabilidade da análise' : 'Indicadores');
      var grid = ensureSection(def.sectionKey, sectionTitle);
      var card = document.createElement('div');
      card.className =
        'agente-compara-results-chart-card agente-compara-dashboard-widget ' +
        comparisonDashboardWidgetSizeClass(widget ? widget.size : 'standard');
      card.setAttribute('data-chart-key', def.canvasKey);
      card.setAttribute('data-comparison-dashboard-widget', def.widgetKey);
      card.setAttribute('data-widget-section', def.sectionKey);
      card.setAttribute('data-widget-type', 'chart');
      if (isComparisonDashboardWidgetHidden(def.widgetKey)) {
        card.hidden = true;
        card.classList.add('is-hidden');
      }
      var header = document.createElement('div');
      header.className = 'agente-compara-dashboard-widget-header';
      var h = document.createElement('h4');
      h.textContent = def.chartTitle;
      header.appendChild(h);
      var hideBtn = document.createElement('button');
      hideBtn.type = 'button';
      hideBtn.className = 'btn btn-sm agente-compara-comparison-dashboard-hide-widget-btn';
      hideBtn.setAttribute('data-comparison-dashboard-hide-widget', def.widgetKey);
      hideBtn.setAttribute('aria-label', 'Ocultar gráfico ' + (widget ? widget.title : def.chartTitle));
      hideBtn.title = 'Ocultar gráfico';
      hideBtn.textContent = 'Ocultar';
      header.appendChild(hideBtn);
      var explainBtn = document.createElement('button');
      explainBtn.type = 'button';
      explainBtn.className = 'btn btn-sm btn-outline-secondary agente-compara-contextual-chat-action';
      explainBtn.setAttribute('data-chat-contextual-action', 'explain_chart');
      explainBtn.textContent = 'Explicar este gráfico';
      explainBtn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        prepareContextualChatQuestion('Explique este gráfico.', {
          intent_hint: def.sectionKey === 'competitiveness' ? 'competitiveness' : 'coverage',
          selected_widget: def.widgetKey,
          active_view: 'dashboard',
          visual_focus: { selected_widget: def.widgetKey, chart_key: def.canvasKey }
        });
      });
      header.appendChild(explainBtn);
      var p = document.createElement('p');
      p.className = 'agente-compara-results-charts-desc';
      p.textContent = def.chartDesc;
      var canvas = document.createElement('canvas');
      canvas.id = 'agenteComparaChart_' + def.canvasKey;
      canvas.setAttribute('role', 'img');
      canvas.setAttribute('aria-label', def.chartTitle + '. ' + def.chartDesc);
      var summary = document.createElement('p');
      summary.className = 'agente-compara-chart-text-summary';
      summary.id = 'agenteComparaChartSummary_' + def.canvasKey;
      card.appendChild(header);
      card.appendChild(p);
      card.appendChild(canvas);
      card.appendChild(summary);
      grid.appendChild(card);
      return { widgetKey: def.widgetKey, canvas: canvas, canvasKey: def.canvasKey };
    }

    var chartRefs = chartDefs.map(function (def) {
      return addChartCard(def);
    });

    container.appendChild(section);

    if (executive) {
      renderComparisonGeographySection(container, analytics);
    }

    applyComparisonDashboardWidgetVisibility();

    if (typeof window.Chart !== 'function') {
      var fallback = document.createElement('p');
      fallback.className = 'agente-compara-temp-table-modal-empty';
      fallback.textContent = 'Chart.js indisponível nesta página. Os indicadores numéricos permanecem no resumo.';
      section.appendChild(fallback);
      return;
    }

    chartRefs.forEach(function (ref) {
      if (isComparisonDashboardWidgetHidden(ref.widgetKey)) return;
      paintComparisonDashboardChart(ref.widgetKey, ref.canvas, analytics);
    });
    resizeComparisonDashboardVisibleCharts();
  }

  var comparisonCalculationMemoryModalEl = null;
  var comparisonCalculationMemoryEscapeHandler = null;
  var comparisonCalculationMemoryOpenerEl = null;

  function comparisonMemoryDisplayText(value) {
    if (value === null || value === undefined) return '';
    var text = String(value).trim();
    return text;
  }

  function comparisonMemoryHasValue(value) {
    return !(value === null || value === undefined || value === '');
  }

  function resolveComparisonCalculationMemory(row, tableResult) {
    if (!row || !tableResult || typeof tableResult !== 'object') return null;
    if (!comparisonMemoryHasValue(row.row_index) || !comparisonMemoryHasValue(tableResult.table_id)) {
      return null;
    }
    var owned = getComparisonCellForTable(row, tableResult.table_id);
    if (!owned || owned !== tableResult) {
      if (!owned || String(owned.table_id || '') !== String(tableResult.table_id || '')) {
        return null;
      }
    }
    var memory = tableResult.calculation_memory;
    if (memory && typeof memory === 'object') return memory;
    if (comparisonMemoryHasValue(tableResult.memory_ref)) {
      return { memory_ref: String(tableResult.memory_ref), status: tableResult.status || tableResult.final_status || 'not_loaded' };
    }
    var fallbackStatus = 'not_calculated';
    if (isComparisonCellIncomplete(tableResult)) fallbackStatus = 'incomplete';
    else if (isComparisonCellCalculated(tableResult)) {
      fallbackStatus =
        (tableResult.final_status || tableResult.status) === 'calculated_with_warnings'
          ? 'calculated_with_warnings'
          : 'calculated';
    }
    return {
      schema_version: 1,
      status: fallbackStatus,
      status_label: comparisonCellStatusLabel(tableResult, { status: fallbackStatus }),
      total_label: fallbackStatus === 'incomplete' ? 'Valor parcial calculado' : 'Total calculado',
      row_index: row.row_index,
      table_id: tableResult.table_id,
      slot_number: tableResult.slot_number,
      carrier_name: tableResult.carrier_name,
      calculated_freight: tableResult.calculated_freight,
      is_partial_value: fallbackStatus === 'incomplete',
      components: [],
      taxes: [],
      total: fallbackStatus === 'not_calculated' ? null : tableResult.calculated_freight,
      blocking_issues: tableResult.blocking_issues || [],
      warnings: tableResult.warnings || [],
      evidence: tableResult.evidence || {},
      diagnostic: tableResult.error
        ? {
            code: tableResult.error.code || tableResult.status || 'not_calculated',
            message: tableResult.error.message || 'Não foi possível calcular esta linha.',
            component: null,
            reason: tableResult.status || null,
            evidence: tableResult.evidence || {}
          }
        : null
    };
  }

  function fetchComparisonCalculationMemory(row, tableResult) {
    var resolved = resolveComparisonCalculationMemory(row, tableResult);
    if (resolved && !resolved.memory_ref) return Promise.resolve(resolved);
    if (!resolved || !resolved.memory_ref || !comparisonState.comparisonId) return Promise.resolve(resolved);
    var url = API_BASE + '/comparison/calculation-memory?comparison_id=' + encodeURIComponent(comparisonState.comparisonId)
      + '&memory_ref=' + encodeURIComponent(resolved.memory_ref)
      + '&table_id=' + encodeURIComponent(String(tableResult.table_id || ''))
      + '&row_index=' + encodeURIComponent(String(row.row_index));
    return fetch(url, {
      method: 'GET',
      credentials: 'same-origin',
      headers: { Accept: 'application/json' }
    }).then(function (r) {
      return r.json().then(function (data) {
        if (!r.ok || !data || data.ok === false || !data.memory) {
          throw new Error((data && data.message) || 'N?o foi poss?vel carregar a mem?ria de c?lculo.');
        }
        return data.memory.calculation_memory || data.memory;
      });
    });
  }

  function ensureComparisonCalculationMemoryModal() {
    if (comparisonCalculationMemoryModalEl) return comparisonCalculationMemoryModalEl;

    var modal = document.createElement('div');
    modal.className = 'agente-compara-temp-table-modal agente-compara-calculation-memory-modal';
    modal.id = 'agenteComparaComparisonCalculationMemoryModal';
    modal.setAttribute('role', 'dialog');
    modal.setAttribute('aria-modal', 'true');
    modal.setAttribute('aria-labelledby', 'agenteComparaComparisonCalculationMemoryModalTitle');
    modal.setAttribute('aria-describedby', 'agenteComparaComparisonCalculationMemoryModalSubtitle');
    modal.hidden = true;

    var backdrop = document.createElement('div');
    backdrop.className = 'agente-compara-temp-table-modal-backdrop';
    backdrop.id = 'agenteComparaComparisonCalculationMemoryModalBackdrop';

    var dialog = document.createElement('div');
    dialog.className = 'agente-compara-temp-table-modal-dialog';

    var header = document.createElement('div');
    header.className = 'agente-compara-temp-table-modal-header';

    var headerMain = document.createElement('div');
    headerMain.className = 'agente-compara-temp-table-modal-header-main';

    var title = document.createElement('h2');
    title.className = 'agente-compara-temp-table-modal-title';
    title.id = 'agenteComparaComparisonCalculationMemoryModalTitle';
    title.textContent = 'Memória de cálculo';

    var subtitle = document.createElement('p');
    subtitle.className = 'agente-compara-temp-table-modal-subtitle';
    subtitle.id = 'agenteComparaComparisonCalculationMemoryModalSubtitle';
    subtitle.textContent = 'Detalhamento do frete calculado da célula selecionada.';

    var closeBtn = document.createElement('button');
    closeBtn.type = 'button';
    closeBtn.className = 'agente-compara-temp-table-modal-close-btn';
    closeBtn.id = 'agenteComparaComparisonCalculationMemoryModalClose';
    closeBtn.setAttribute('aria-label', 'Fechar memória de cálculo');
    var closeIcon = document.createElement('span');
    closeIcon.setAttribute('aria-hidden', 'true');
    closeIcon.textContent = '\u00d7';
    closeBtn.appendChild(closeIcon);

    headerMain.appendChild(title);
    headerMain.appendChild(subtitle);
    header.appendChild(headerMain);
    header.appendChild(closeBtn);

    var body = document.createElement('div');
    body.className = 'agente-compara-temp-table-modal-body';
    body.id = 'agenteComparaComparisonCalculationMemoryModalBody';

    var footer = document.createElement('div');
    footer.className = 'agente-compara-temp-table-modal-footer';
    footer.id = 'agenteComparaComparisonCalculationMemoryModalFooter';
    var footerClose = document.createElement('button');
    footerClose.type = 'button';
    footerClose.className = 'agente-compara-btn agente-compara-btn-secondary';
    footerClose.id = 'agenteComparaComparisonCalculationMemoryModalFooterClose';
    footerClose.textContent = 'Fechar';
    footer.appendChild(footerClose);

    dialog.appendChild(header);
    dialog.appendChild(body);
    dialog.appendChild(footer);
    modal.appendChild(backdrop);
    modal.appendChild(dialog);
    document.body.appendChild(modal);

    closeBtn.addEventListener('click', closeComparisonCalculationMemory);
    footerClose.addEventListener('click', closeComparisonCalculationMemory);
    backdrop.addEventListener('click', closeComparisonCalculationMemory);

    comparisonCalculationMemoryModalEl = modal;
    return modal;
  }

  function isComparisonCalculationMemoryModalOpen() {
    return !!(comparisonCalculationMemoryModalEl && !comparisonCalculationMemoryModalEl.hidden);
  }

  function closeComparisonCalculationMemory() {
    if (!comparisonCalculationMemoryModalEl || comparisonCalculationMemoryModalEl.hidden) return;
    comparisonCalculationMemoryModalEl.hidden = true;
    var body = byId('agenteComparaComparisonCalculationMemoryModalBody');
    if (body) {
      while (body.firstChild) body.removeChild(body.firstChild);
    }
    if (comparisonCalculationMemoryEscapeHandler) {
      document.removeEventListener('keydown', comparisonCalculationMemoryEscapeHandler, true);
      comparisonCalculationMemoryEscapeHandler = null;
    }
    var opener = comparisonCalculationMemoryOpenerEl;
    comparisonCalculationMemoryOpenerEl = null;
    if (opener && document.contains(opener) && typeof opener.focus === 'function') {
      opener.focus();
    } else {
      var region = byId('agenteComparaComparisonResultsHost') || byId('agenteComparaComparisonResultsTable');
      if (region && typeof region.focus === 'function') region.focus();
    }
  }

  function appendComparisonMemoryDetail(container, label, value) {
    if (!comparisonMemoryHasValue(value) && value !== 0) return;
    appendDetailRow(container, label, String(value));
  }

  function buildLegacyComparisonMemoryRows(tableResult) {
    var rows = [];
    var components = (tableResult && tableResult.components) || {};
    if (comparisonMemoryHasValue(components.weight_freight)) {
      rows.push({ label: 'Frete-peso', basis: '', amount: components.weight_freight, note: '' });
    }
    if (comparisonMemoryHasValue(components.freight_value_component)) {
      rows.push({ label: 'Frete-valor', basis: '', amount: components.freight_value_component, note: '' });
    }
    if (comparisonMemoryHasValue(components.toll)) {
      rows.push({ label: 'Pedágio', basis: '', amount: components.toll, note: '' });
    }
    if (Array.isArray(components.accessorials)) {
      components.accessorials.forEach(function (item) {
        if (!item || typeof item !== 'object') return;
        rows.push({
          label: comparisonMemoryDisplayText(item.label || item.name) || 'Adicional',
          basis: comparisonMemoryDisplayText(item.details),
          amount: item.amount,
          note: item.minimum_applied ? 'Mínimo aplicado' : ''
        });
      });
    }
    return rows;
  }

  function renderComparisonCalculationMemoryContent(row, tableResult) {
    var body = byId('agenteComparaComparisonCalculationMemoryModalBody');
    var incompleteDisclaimerText = 'não é um frete definitivo';
    if (!body) return;
    while (body.firstChild) body.removeChild(body.firstChild);

    body.textContent = 'Carregando mem?ria de c?lculo...';
    fetchComparisonCalculationMemory(row, tableResult)
      .then(function (memory) {
        memory = memory || {};
        while (body.firstChild) body.removeChild(body.firstChild);
        var isIncomplete = isComparisonCellIncomplete(tableResult) || memory.status === 'incomplete';
        var isCalculated =
          !isIncomplete &&
          isComparisonCellCalculated(tableResult) &&
          memory.status !== 'not_calculated';
        var carrier = comparisonMemoryDisplayText(tableResult.carrier_name) || 'Transportadora';
        var slot = comparisonMemoryHasValue(tableResult.slot_number) ? String(tableResult.slot_number) : '?';

        var summary = document.createElement('div');
        summary.className = 'agente-compara-calculation-memory-summary';
        appendComparisonMemoryDetail(summary, 'Documento', row.document_number);
        var destParts = [];
        if (comparisonMemoryDisplayText(row.destination_city)) destParts.push(comparisonMemoryDisplayText(row.destination_city));
        if (comparisonMemoryDisplayText(row.destination_uf)) destParts.push(comparisonMemoryDisplayText(row.destination_uf));
        appendComparisonMemoryDetail(summary, 'Destino', destParts.length ? destParts.join(' / ') : null);
        appendComparisonMemoryDetail(summary, 'Peso', comparisonMemoryHasValue(row.weight) ? formatComparisonWeight(row.weight) : null);
        appendComparisonMemoryDetail(summary, 'Valor da NF', comparisonMemoryHasValue(row.invoice_value) ? formatComparisonMoney(row.invoice_value) : null);
        appendComparisonMemoryDetail(summary, 'Transportadora', carrier + ' ? Tabela ' + slot);
        appendComparisonMemoryDetail(summary, 'Status', comparisonCellStatusLabel(tableResult, memory));
        body.appendChild(summary);

        if (isIncomplete) {
          var incompleteSection = document.createElement('div');
          incompleteSection.className = 'agente-compara-calculation-memory-diagnostic';
          appendComparisonMemoryDetail(
            incompleteSection,
            memory.total_label || 'Valor parcial calculado',
            formatComparisonMoney(memory.total != null ? memory.total : tableResult.calculated_freight)
          );
          var blocking = memory.blocking_issues || tableResult.blocking_issues || [];
          if (Array.isArray(blocking) && blocking.length) {
            blocking.forEach(function (issue, idx) {
              if (!issue || typeof issue !== 'object') return;
              var label = comparisonMemoryDisplayText(issue.label) || comparisonMemoryDisplayText(issue.component_code) || ('Componente ' + String(idx + 1));
              appendComparisonMemoryDetail(
                incompleteSection,
                'N?o avaliado: ' + label,
                comparisonMemoryDisplayText(issue.message) || comparisonMemoryDisplayText(issue.reason_code) || 'Componente cr?tico n?o resolvido'
              );
            });
          } else {
            appendComparisonMemoryDetail(
              incompleteSection,
              'Motivo',
              (tableResult.error && tableResult.error.message) || 'H? componentes potencialmente aplic?veis n?o avaliados.'
            );
          }
          body.appendChild(incompleteSection);

          var incompleteNote = document.createElement('p');
          incompleteNote.className = 'agente-compara-temp-table-modal-empty';
          incompleteNote.textContent = 'Este valor parcial ' + incompleteDisclaimerText + ' e não entra na comparação como cálculo completo.';
          body.appendChild(incompleteNote);

          var incompleteComponents = Array.isArray(memory.components) ? memory.components : [];
          if (incompleteComponents.length) {
            var incTitle = document.createElement('h3');
            incTitle.className = 'agente-compara-temp-table-modal-section-title';
            incTitle.textContent = 'Componentes';
            body.appendChild(incTitle);
            incompleteComponents.forEach(function (item) {
              if (!item || typeof item !== 'object') return;
              var note = item.ignored
                ? (comparisonMemoryDisplayText(item.reason) || 'Ignorado')
                : (item.amount != null ? formatComparisonMoney(item.amount) : '');
              appendComparisonMemoryDetail(
                body,
                comparisonMemoryDisplayText(item.label) || comparisonMemoryDisplayText(item.code) || 'Componente',
                note
              );
            });
          }
          return;
        }

        if (!isCalculated) {
          var diagnostic = memory.diagnostic || {};
          var diagSection = document.createElement('div');
          diagSection.className = 'agente-compara-calculation-memory-diagnostic';
          appendComparisonMemoryDetail(diagSection, 'Motivo', diagnostic.message || (tableResult.error && tableResult.error.message) || 'N?o foi poss?vel calcular esta linha.');
          appendComparisonMemoryDetail(diagSection, 'C?digo', diagnostic.code || (tableResult.error && tableResult.error.code) || tableResult.status);
          appendComparisonMemoryDetail(diagSection, 'Componente/regra', diagnostic.component || diagnostic.reason);
          body.appendChild(diagSection);

          var orientation = document.createElement('p');
          orientation.className = 'agente-compara-temp-table-modal-empty';
          var code = String(diagnostic.code || tableResult.status || '');
          if (code.indexOf('missing_coverage') >= 0) {
            orientation.textContent = 'Orienta??o: destino n?o atendido ou sem mapeamento de cobertura.';
          } else if (code.indexOf('missing_freight_rule') >= 0) {
            orientation.textContent = 'Orienta??o: nenhuma faixa ou regra de frete aplic?vel para os dados informados.';
          } else if (code.indexOf('unsupported_pricing') >= 0) {
            orientation.textContent = 'Orienta??o: regra incompat?vel com o modelo de precifica??o suportado.';
          } else if (code.indexOf('invalid_') >= 0) {
            orientation.textContent = 'Orienta??o: valor obrigat?rio ausente ou inv?lido para o c?lculo.';
          } else {
            orientation.textContent = 'Orienta??o: revise os dados operacionais e a tabela preparada para esta c?lula.';
          }
          body.appendChild(orientation);

          var diagnosticEvidence = (diagnostic && diagnostic.evidence) || memory.evidence || {};
          var evidenceKeys = Object.keys(diagnosticEvidence || {});
          if (evidenceKeys.length) {
            var evidenceTitle = document.createElement('h3');
            evidenceTitle.className = 'agente-compara-temp-table-modal-section-title';
            evidenceTitle.textContent = 'Evid?ncias';
            body.appendChild(evidenceTitle);
            evidenceKeys.forEach(function (key) {
              var value = diagnosticEvidence[key];
              if (Array.isArray(value)) value = value.join(', ');
              else if (value && typeof value === 'object') return;
              appendComparisonMemoryDetail(body, key, value);
            });
          }
          return;
        }

        var pricing = memory.pricing || {};
        var evidence = memory.evidence || tableResult.evidence || {};
        appendComparisonMemoryDetail(
          body,
          memory.total_label || 'Total calculado',
          formatComparisonMoney(memory.total != null ? memory.total : tableResult.calculated_freight)
        );
        if (memory.status === 'calculated_with_warnings' || (Array.isArray(memory.warnings) && memory.warnings.length)) {
          (memory.warnings || []).forEach(function (warning, idx) {
            if (!warning || typeof warning !== 'object') return;
            appendComparisonMemoryDetail(
              body,
              'Ressalva ' + String(idx + 1),
              comparisonMemoryDisplayText(warning.message) || comparisonMemoryDisplayText(warning.reason_code) || 'Ressalva n?o bloqueante'
            );
          });
        }
        appendComparisonMemoryDetail(body, 'Regi?o', pricing.freight_region || evidence.freight_region);
        appendComparisonMemoryDetail(body, 'Faixa', pricing.weight_band || evidence.weight_band);
        appendComparisonMemoryDetail(body, 'Base', pricing.weight_basis || evidence.weight_basis || evidence.calculation_basis);
        appendComparisonMemoryDetail(body, 'Precifica??o', pricing.pricing_type || evidence.pricing_type);

        var componentRows = [];
        if (Array.isArray(memory.components) && memory.components.length) {
          memory.components.forEach(function (item) {
            if (!item || typeof item !== 'object') return;
            var noteParts = [];
            if (item.ignored) noteParts.push('Ignorado');
            if (item.minimum_applied) noteParts.push('M?nimo aplicado');
            if (comparisonMemoryDisplayText(item.reason)) noteParts.push(comparisonMemoryDisplayText(item.reason));
            componentRows.push({
              label: comparisonMemoryDisplayText(item.label) || comparisonMemoryDisplayText(item.code) || 'Componente',
              basis: comparisonMemoryDisplayText(item.basis) || comparisonMemoryDisplayText(item.operation),
              rate: comparisonMemoryHasValue(item.rate) ? String(item.rate) : '',
              quantity: comparisonMemoryHasValue(item.quantity) ? String(item.quantity) : '',
              minimum: comparisonMemoryHasValue(item.minimum_amount) ? formatComparisonMoney(item.minimum_amount) : '',
              amount: item.ignored ? null : item.amount,
              note: noteParts.join(' ? '),
              ignored: !!item.ignored
            });
          });
        } else {
          componentRows = buildLegacyComparisonMemoryRows(tableResult);
        }

        if (componentRows.length) {
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
          ['Componente', 'Base/regra', 'Taxa', 'Qtd.', 'M?nimo', 'Valor', 'Observa??o'].forEach(function (label) {
            appendTableCell(headerRow, label, true, false);
          });
          thead.appendChild(headerRow);
          table.appendChild(thead);
          var tbody = document.createElement('tbody');
          componentRows.forEach(function (memoryRow) {
            var tr = document.createElement('tr');
            appendTableCell(tr, memoryRow.label, false, true);
            appendTableCell(tr, memoryRow.basis || '', false, true);
            appendTableCell(tr, memoryRow.rate || '', false, true);
            appendTableCell(tr, memoryRow.quantity || '', false, true);
            appendTableCell(tr, memoryRow.minimum || '', false, true);
            appendTableCell(
              tr,
              memoryRow.ignored ? 'Ignorado' : formatComparisonMoney(memoryRow.amount),
              false,
              true
            );
            appendTableCell(tr, memoryRow.note || '', false, true);
            tbody.appendChild(tr);
          });
          table.appendChild(tbody);
          scrollWrap.appendChild(table);
          body.appendChild(scrollWrap);
        }

        var taxes = Array.isArray(memory.taxes) ? memory.taxes : [];
        if (!taxes.length && tableResult.components && comparisonMemoryHasValue(tableResult.components.taxes)) {
          if (comparisonMemoryHasValue(tableResult.components.icms)) {
            taxes.push({ label: 'ICMS', amount: tableResult.components.icms, applied: true });
          }
          if (comparisonMemoryHasValue(tableResult.components.iss)) {
            taxes.push({ label: 'ISS', amount: tableResult.components.iss, applied: true });
          }
        }
        if (taxes.length) {
          var taxTitle = document.createElement('h3');
          taxTitle.className = 'agente-compara-temp-table-modal-section-title';
          taxTitle.textContent = 'Impostos';
          body.appendChild(taxTitle);
          var taxScroll = document.createElement('div');
          taxScroll.className = 'agente-compara-temp-table-modal-freight-scroll agente-compara-calculation-memory-table-scroll';
          var taxTable = document.createElement('table');
          taxTable.className = 'agente-compara-temp-table-modal-freight-table agente-compara-calculation-memory-table';
          var taxHead = document.createElement('thead');
          var taxHeadRow = document.createElement('tr');
          ['Imposto', 'Base', 'Al?quota', 'Valor'].forEach(function (label) {
            appendTableCell(taxHeadRow, label, true, false);
          });
          taxHead.appendChild(taxHeadRow);
          taxTable.appendChild(taxHead);
          var taxBody = document.createElement('tbody');
          taxes.forEach(function (tax) {
            if (!tax || typeof tax !== 'object') return;
            var tr = document.createElement('tr');
            appendTableCell(tr, comparisonMemoryDisplayText(tax.label || tax.tax_type) || 'Imposto', false, true);
            appendTableCell(tr, comparisonMemoryHasValue(tax.basis) ? formatComparisonMoney(tax.basis) : '', false, true);
            appendTableCell(tr, comparisonMemoryHasValue(tax.rate) ? String(tax.rate) : '', false, true);
            appendTableCell(
              tr,
              tax.ignored || tax.applied === false
                ? (comparisonMemoryDisplayText(tax.reason) || 'N?o aplicado')
                : formatComparisonMoney(tax.amount),
              false,
              true
            );
            taxBody.appendChild(tr);
          });
          taxTable.appendChild(taxBody);
          taxScroll.appendChild(taxTable);
          body.appendChild(taxScroll);
        }

        var totals = document.createElement('div');
        totals.className = 'agente-compara-calculation-memory-total';
        var subtotalLabel = document.createElement('span');
        subtotalLabel.className = 'agente-compara-calculation-memory-total-label';
        var subtotalValue = memory.subtotal_before_taxes;
        if (!comparisonMemoryHasValue(subtotalValue) && tableResult.components) {
          subtotalValue = tableResult.components.subtotal;
        }
        subtotalLabel.textContent = comparisonMemoryHasValue(subtotalValue)
          ? ('Subtotal: ' + formatComparisonMoney(subtotalValue))
          : 'Subtotal: ?';
        var totalLabel = document.createElement('strong');
        totalLabel.className = 'agente-compara-calculation-memory-total-value';
        totalLabel.textContent = 'Total: ' + formatComparisonMoney(memory.total != null ? memory.total : tableResult.calculated_freight);
        totals.appendChild(subtotalLabel);
        totals.appendChild(totalLabel);
        body.appendChild(totals);

        if (evidence && typeof evidence === 'object') {
          var safeEvidenceKeys = [
            'freight_region',
            'calculation_basis',
            'calculation_details',
            'pricing_type',
            'pricing_lookup_key',
            'pricing_lookup_kind',
            'weight_band',
            'weight_basis'
          ];
          var hasEvidence = safeEvidenceKeys.some(function (key) {
            return comparisonMemoryHasValue(evidence[key]);
          });
          if (hasEvidence) {
            var evidenceTitle = document.createElement('h3');
            evidenceTitle.className = 'agente-compara-temp-table-modal-section-title';
            evidenceTitle.textContent = 'Evid?ncias';
            body.appendChild(evidenceTitle);
            safeEvidenceKeys.forEach(function (key) {
              appendComparisonMemoryDetail(body, key, evidence[key]);
            });
          }
        }
      })
      .catch(function () {
        while (body.firstChild) body.removeChild(body.firstChild);
        var failure = document.createElement('div');
        failure.className = 'agente-compara-calculation-memory-diagnostic';
        appendComparisonMemoryDetail(failure, 'Mem?ria', 'N?o foi poss?vel carregar a mem?ria de c?lculo.');
        body.appendChild(failure);
      });
  }

  function openComparisonCalculationMemory(row, tableResult, openerEl) {
    if (!row || !tableResult) return;
    if (!comparisonMemoryHasValue(row.row_index) || !comparisonMemoryHasValue(tableResult.table_id)) return;
    var owned = getComparisonCellForTable(row, tableResult.table_id);
    if (!owned) return;

    var modal = ensureComparisonCalculationMemoryModal();
    var subtitle = byId('agenteComparaComparisonCalculationMemoryModalSubtitle');
    var carrier = comparisonMemoryDisplayText(tableResult.carrier_name) || 'Transportadora';
    var slot = comparisonMemoryHasValue(tableResult.slot_number) ? String(tableResult.slot_number) : '—';
    var doc = comparisonMemoryDisplayText(row.document_number);
    if (subtitle) {
      subtitle.textContent = doc
        ? ('Linha ' + String(row.row_index) + ' — ' + carrier + ' — Tabela ' + slot + ' — documento ' + doc)
        : ('Linha ' + String(row.row_index) + ' — ' + carrier + ' — Tabela ' + slot);
    }
    renderComparisonCalculationMemoryContent(row, owned);
    var memoryActions = byId('agenteComparaComparisonCalculationMemoryChatActions');
    if (!memoryActions) {
      memoryActions = document.createElement('div');
      memoryActions.id = 'agenteComparaComparisonCalculationMemoryChatActions';
      memoryActions.className = 'agente-compara-contextual-chat-action';
      var memoryBody = byId('agenteComparaComparisonCalculationMemoryModalBody');
      if (memoryBody && memoryBody.parentNode) {
        memoryBody.parentNode.insertBefore(memoryActions, memoryBody.nextSibling);
      }
    }
    if (memoryActions) {
      memoryActions.innerHTML = '';
      var explainCalcBtn = document.createElement('button');
      explainCalcBtn.type = 'button';
      explainCalcBtn.className = 'btn btn-sm btn-outline-secondary';
      explainCalcBtn.textContent = 'Explicar este cálculo';
      explainCalcBtn.addEventListener('click', function () {
        prepareContextualChatQuestion('Explique este cálculo.', {
          intent_hint: 'calculation_memory',
          document_number: row.document_number || null,
          row_index: row.row_index,
          table_id: owned.table_id || tableResult.table_id || null
        });
      });
      memoryActions.appendChild(explainCalcBtn);
    }
    comparisonCalculationMemoryOpenerEl = openerEl || null;
    modal.hidden = false;
    var closeBtn = byId('agenteComparaComparisonCalculationMemoryModalClose');
    if (closeBtn && typeof closeBtn.focus === 'function') closeBtn.focus();

    if (!comparisonCalculationMemoryEscapeHandler) {
      comparisonCalculationMemoryEscapeHandler = function (e) {
        if (e.key === 'Escape' && isComparisonCalculationMemoryModalOpen()) {
          e.preventDefault();
          e.stopPropagation();
          closeComparisonCalculationMemory();
        }
      };
      document.addEventListener('keydown', comparisonCalculationMemoryEscapeHandler, true);
    }
  }

  function appendComparisonFreightCell(td, row, tableResult, columnTitle) {
    var carrier = comparisonMemoryDisplayText(tableResult && tableResult.carrier_name) || 'Transportadora';
    if (isComparisonCellIncomplete(tableResult)) {
      var partialMoney = formatComparisonMoney(tableResult.calculated_freight);
      var incompleteBtn = document.createElement('button');
      incompleteBtn.type = 'button';
      incompleteBtn.className = 'agente-compara-comparison-calc-memory-link agente-compara-comparison-calc-incomplete';
      incompleteBtn.textContent = partialMoney
        ? (partialMoney + ' parcial — ver detalhes')
        : 'Cálculo incompleto — ver detalhes';
      incompleteBtn.title = 'Cálculo incompleto: valor parcial não é frete definitivo';
      incompleteBtn.setAttribute(
        'aria-label',
        'Cálculo incompleto do frete' +
          (partialMoney ? ' (valor parcial ' + partialMoney + ')' : '') +
          ' — ' + carrier + (columnTitle ? ' (' + columnTitle + ')' : '') +
          '. Abrir memória de cálculo.'
      );
      incompleteBtn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        openComparisonCalculationMemory(row, tableResult, incompleteBtn);
      });
      td.appendChild(incompleteBtn);
      return;
    }
    if (!isComparisonCellCalculated(tableResult)) {
      var detail =
        (tableResult && tableResult.error && (tableResult.error.message || tableResult.error.code)) ||
        (tableResult && tableResult.status) ||
        'Erro de domínio';
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-comparison-calc-memory-link';
      btn.textContent = 'Não calculado — Ver motivo';
      btn.title = String(detail);
      btn.setAttribute(
        'aria-label',
        'Ver motivo do frete não calculado — ' + carrier + (columnTitle ? ' (' + columnTitle + ')' : '')
      );
      btn.addEventListener('click', function (event) {
        event.preventDefault();
        event.stopPropagation();
        openComparisonCalculationMemory(row, tableResult, btn);
      });
      td.appendChild(btn);
      return;
    }

    var money = formatComparisonMoney(tableResult.calculated_freight);
    var calcBtn = document.createElement('button');
    calcBtn.type = 'button';
    calcBtn.className = 'agente-compara-comparison-calc-memory-link';
    var status = tableResult.final_status || tableResult.status;
    if (status === 'calculated_with_warnings') {
      calcBtn.textContent = money + ' (ressalvas)';
      calcBtn.title = 'Calculado com ressalvas — abrir memória';
    } else {
      calcBtn.textContent = money;
    }
    calcBtn.setAttribute(
      'aria-label',
      'Ver memória de cálculo do frete de ' + money + ' — ' + carrier
    );
    calcBtn.addEventListener('click', function (event) {
      event.preventDefault();
      event.stopPropagation();
      openComparisonCalculationMemory(row, tableResult, calcBtn);
    });
    td.appendChild(calcBtn);
  }

  function renderComparisonResultsTable(container, result, pageRows) {
    var hasOrigin = comparisonRowHasOrigin(result.comparative_rows || []);
    var hasInvoice = comparisonRowHasInvoice(result.comparative_rows || []);
    var hasDates = comparisonRowHasIssueDate(result.comparative_rows || []);
    var scroll = document.createElement('div');
    scroll.className = 'agente-compara-comparison-calculation-scroll';
    var table = document.createElement('table');
    table.className = 'agente-compara-comparison-calculation-table';
    table.id = 'agenteComparaComparisonResultsTable';
    table.setAttribute('aria-label', 'Tabela comparativa de frete calculado');
    table.tabIndex = -1;

    var columns = disambiguateCarrierColumnTitle(result.tables || []);
    var thead = document.createElement('thead');
    var headRow = document.createElement('tr');
    var commonHeaders = ['Documento'];
    if (hasOrigin.city || hasOrigin.uf) commonHeaders.push('Origem');
    commonHeaders.push('Destino', 'Peso');
    if (hasInvoice) commonHeaders.push('Valor da NF');
    if (hasDates) commonHeaders.push('Data de emissão');
    commonHeaders.forEach(function (label) {
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
    if (!pageRows.length) {
      var emptyTr = document.createElement('tr');
      var emptyTd = document.createElement('td');
      emptyTd.colSpan = commonHeaders.length + columns.length;
      emptyTd.textContent = 'Nenhum documento corresponde aos filtros atuais.';
      emptyTr.appendChild(emptyTd);
      fragment.appendChild(emptyTr);
    }
    pageRows.forEach(function (row) {
      var tr = document.createElement('tr');
      var originParts = [];
      if (hasFieldValue(row.origin_city)) originParts.push(String(row.origin_city));
      if (hasFieldValue(row.origin_uf)) originParts.push(String(row.origin_uf));
      var destParts = [];
      if (hasFieldValue(row.destination_city)) destParts.push(String(row.destination_city));
      if (hasFieldValue(row.destination_uf)) destParts.push(String(row.destination_uf));
      var values = [row.document_number];
      if (hasOrigin.city || hasOrigin.uf) values.push(originParts.join(' / ') || '—');
      values.push(destParts.join(' / ') || '—');
      values.push(formatComparisonWeight(row.weight));
      if (hasInvoice) values.push(formatComparisonMoney(row.invoice_value));
      if (hasDates) values.push(comparisonRowIssueDate(row) || '—');
      values.forEach(function (value) {
        var td = document.createElement('td');
        td.textContent = hasFieldValue(value) ? String(value) : '—';
        tr.appendChild(td);
      });
      columns.forEach(function (col) {
        var td = document.createElement('td');
        td.setAttribute('data-table-id', col.table_id || '');
        var cell = getComparisonCellForTable(row, col.table_id);
        appendComparisonFreightCell(td, row, cell, col.title);
        tr.appendChild(td);
      });
      fragment.appendChild(tr);
    });
    tbody.appendChild(fragment);
    table.appendChild(tbody);
    scroll.appendChild(table);
    container.appendChild(scroll);
  }

  function refreshComparisonResultsView() {
    refreshComparisonCalculationViews();
  }

  function renderComparisonResultsDetailTable(container, result) {
    if (!container || !result || !Array.isArray(result.comparative_rows)) return;

    var region = document.createElement('div');
    region.className = 'agente-compara-comparison-calculation-results';
    region.id = 'agenteComparaComparisonCalculationResults';
    region.setAttribute('role', 'region');
    region.setAttribute('aria-label', 'Detalhamento operacional do cálculo comparativo');

    function paint() {
      var existingFilters = region.querySelector('#agenteComparaResultsFilters');
      if (existingFilters) existingFilters.remove();
      var existingTable = region.querySelector('#agenteComparaComparisonResultsTable');
      if (existingTable && existingTable.parentNode) existingTable.parentNode.remove();
      var existingPager = region.querySelector('#agenteComparaResultsPagination');
      if (existingPager) existingPager.remove();
      var existingCount = region.querySelector('#agenteComparaResultsVisibleCount');
      if (existingCount) existingCount.remove();

      var filtered = filterComparativeRows(
        result.comparative_rows,
        result.tables || [],
        comparisonResultsUiState.filters
      );
      var pageInfo = paginateRows(
        filtered,
        comparisonResultsUiState.page,
        comparisonResultsUiState.pageSize
      );
      comparisonResultsUiState.page = pageInfo.page;
      comparisonResultsUiState.pageSize = pageInfo.pageSize;

      renderComparisonResultsFilters(region, result, function () {
        paint();
      });

      var countEl = document.createElement('p');
      countEl.className = 'agente-compara-results-visible-count';
      countEl.id = 'agenteComparaResultsVisibleCount';
      countEl.setAttribute('role', 'status');
      countEl.textContent =
        'Exibindo ' +
        String(filtered.length) +
        ' de ' +
        String(result.comparative_rows.length) +
        ' documentos';
      region.appendChild(countEl);

      renderComparisonResultsTable(region, result, pageInfo.rows);
      renderComparisonResultsPagination(region, pageInfo, function () {
        paint();
      });
    }

    paint();
    container.appendChild(region);
  }

  function renderComparisonCalculationResults(container, result) {
    clearComparisonCalculationResults(container);
    renderComparisonCalculationStatus(container);

    var billing = comparisonCalculationState.billingStatus || '';
    var calcStatus = comparisonCalculationState.status || '';
    if (comparisonCalculationState.stale) return;
    if (calcStatus === 'CALCULATION_READY' && (billing === 'pending' || billing === 'failed')) return;
    if (calcStatus === 'CALCULATION_RUNNING' || comparisonCalculationInFlight) return;
    if (calcStatus === 'CALCULATION_FAILED') return;
    if (!result || !Array.isArray(result.comparative_rows)) {
      if (calcStatus === 'CALCULATION_READY' && billing === 'applied') {
        var missing = document.createElement('p');
        missing.className = 'agente-compara-temp-table-modal-empty';
        missing.setAttribute('role', 'alert');
        missing.textContent = 'Nenhum resultado disponível para exibição.';
        container.appendChild(missing);
      }
      return;
    }

    renderComparisonResultsDetailTable(container, result);
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
        message: payload.message || null,
        stage: payload.error_stage || null,
        artifact_type: payload.artifact_type || null,
        retryable: payload.retryable === true,
        table_id: payload.table_id || payload.failed_table_id || null,
        slot_number: payload.slot_number || payload.failed_slot || null,
        carrier_name: payload.carrier_name || payload.failed_table_name || null,
        failure_origin: payload.failure_origin || null,
        failure_code: payload.failure_code || null,
        credit_disposition: payload.credit_disposition || null,
        retry_of: payload.retry_of || null,
        is_free_retry: payload.is_free_retry === true,
        safe_message: payload.safe_message || payload.message || null
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
      comparisonCalculationState.analytics =
        payload.analytics && typeof payload.analytics === 'object' ? payload.analytics : null;
    } else if (payload.status === 'CALCULATION_FAILED') {
      comparisonCalculationState.result = null;
      comparisonCalculationState.analytics = null;
    } else if (
      payload.status === 'CALCULATION_READY' &&
      (payload.billing_status === 'pending' || payload.billing_status === 'failed' || payload.stale)
    ) {
      comparisonCalculationState.result = null;
      comparisonCalculationState.analytics = null;
    } else if (payload.status === 'not_started' || payload.status === 'CALCULATION_RUNNING') {
      comparisonCalculationState.result = null;
      comparisonCalculationState.analytics = null;
    }
    if (payload.current_step) {
      comparisonState.currentStep = payload.current_step;
    }
    if (typeof syncProgressiveChatUnlock === 'function') {
      syncProgressiveChatUnlock();
    }
  }

  function resolveCalculationStorageFailureUi(error) {
    var code = (error && error.code) || '';
    var artifact = (error && error.artifact_type) || '';
    if (code === 'comparison_table_preparation_failed') {
      return resolveComparisonPreparationFailureUi(error);
    }
    if (code === 'calculation_memory_too_large' || code === 'calculation_result_too_large') {
      return {
        title: artifact === 'memory' ? 'Detalhes do c?lculo excederam o limite' : 'Resultado excedeu o limite',
        message: error.message || 'Os cálculos foram processados, mas o armazenamento excedeu o limite técnico.'
      };
    }
    if (artifact === 'memory') {
      return {
        title: 'Falha ao armazenar detalhes do cálculo',
        message: error.message || 'Os cálculos foram processados, mas os detalhes não puderam ser salvos.'
      };
    }
    if (artifact === 'result') {
      return {
        title: 'Falha ao armazenar o resultado',
        message: error.message || 'Os cálculos foram processados, mas o resultado comparativo não pôde ser salvo.'
      };
    }
    return {
      title: 'Falha no c?lculo',
      message: error.message || 'N?o foi poss?vel concluir o c?lculo comparativo.'
    };
  }

  function resolveComparisonPreparationFailureUi(error) {
    var carrierName = String((error && error.carrier_name) || '').trim();
    var slotNumber = error && error.slot_number != null ? String(error.slot_number) : '';
    var tableName = carrierName || (slotNumber ? ('Tabela ' + slotNumber) : 'esta tabela');
    var safeMessage = (error && error.safe_message) || (error && error.message) || 'Não foi possível preparar a tabela neste momento.';
    if (
      error &&
      error.failure_origin === 'platform' &&
      error.retryable === true &&
      error.credit_disposition === 'preserved'
    ) {
      return {
        title: 'Falha temporária ao preparar a tabela',
        subtitle: 'Somente a tabela afetada precisa de uma nova tentativa.',
        message: 'Não foi possível concluir a preparação da tabela ' + tableName + '. Nenhum crédito foi consumido por esta tentativa.',
        retryLabel: 'Tentar novamente'
      };
    }
    if (error && error.retry_of && error.is_free_retry === true && error.credit_disposition === 'not_consumed') {
      return {
        title: 'Nova tentativa da tabela',
        subtitle: 'Somente a tabela afetada precisa de uma nova tentativa.',
        message: safeMessage + ' Esta nova tentativa não consumirá outro crédito.',
        retryLabel: 'Tentar novamente'
      };
    }
    return {
      title: 'Tabela pendente de correção',
      subtitle: 'Corrija o arquivo desta tabela para continuar.',
      message: safeMessage,
      retryLabel: 'Tentar novamente'
    };
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
        if (!res.data) return null;
        // ok pode ser false em billing pending/failed; ainda assim restaura estado.
        if (res.data.status || res.data.billing_status || res.data.current_step) {
          applyComparisonCalculationPayload(res.data);
        } else if (!res.data.ok) {
          return null;
        }
        if (shouldEnableResultsReviewTab()) {
          configurationReviewTab = 'results';
        }
        refreshComparisonDashboardView();
        if (isComparisonReviewMode() && isTempTableModalOpen()) {
          setComparisonCommonParamsModalHeader();
          renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
          updateTempTableModalFooter();
        } else {
          refreshComparisonResultsDetailView();
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
    comparisonState.currentStep = comparisonState.currentStep === 'CONFIGURATION_READY'
      ? 'CALCULATION_RUNNING'
      : (comparisonState.currentStep || 'CALCULATION_RUNNING');
    if (comparisonCalculationState.status === 'not_started' || !comparisonCalculationState.status) {
      comparisonCalculationState.status = 'CALCULATION_RUNNING';
    }
    configurationReviewTab = 'results';
    tempTableModalActiveTab = 'configuration_review';

    var button = document.getElementById('agenteComparaProcessCalculationsButton');
    setProcessCalculationsButtonState(button);
    refreshComparisonDashboardView();
    if (isComparisonReviewMode()) {
      renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
      updateTempTableModalFooter();
    }

    var executionId = generateRequestId();
    var comparisonId = comparisonState.comparisonId;
    if (!comparisonId) {
      comparisonCalculationInFlight = false;
      setProcessCalculationsButtonState(button);
      return;
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
              message: 'Não foi possível processar os cÃ¡lculos comparativos.'
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
            message: data.message || 'Conflito ao processar os cÃ¡lculos.',
            stage: data.error_stage || ((data.error && data.error.stage) || ''),
            retryable: data.retryable === true,
            table_id: data.table_id || data.failed_table_id || null,
            slot_number: data.slot_number || data.failed_slot || null,
            carrier_name: data.carrier_name || data.failed_table_name || null,
            failure_origin: data.failure_origin || null,
            failure_code: data.failure_code || null,
            credit_disposition: data.credit_disposition || null,
            retry_of: data.retry_of || null,
            is_free_retry: data.is_free_retry === true,
            safe_message: data.safe_message || data.message || null
          };
          if (data.status) applyComparisonCalculationPayload(data);
          else comparisonCalculationState.status = 'CALCULATION_FAILED';
        } else if (data.status === 'CALCULATION_READY') {
          applyComparisonCalculationPayload(data);
          comparisonState.currentStep = 'CALCULATION_READY';
          if (window.LogCompletaPixel && typeof window.LogCompletaPixel.trackFunnelEvent === 'function') {
            try {
              var calculationFunnelEvent = data.funnel_event;
              window.LogCompletaPixel.trackFunnelEvent(calculationFunnelEvent, {
                content_name: 'Agente Compara',
                source: calculationFunnelEvent && calculationFunnelEvent.source
                  ? calculationFunnelEvent.source
                  : undefined
              });
            } catch (pixelErr) {
              // Meta Pixel nunca bloqueia o fluxo de cálculo.
            }
          }
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

        configurationReviewTab = 'results';
        refreshComparisonDashboardView();
        if (isComparisonReviewMode()) {
          setComparisonCommonParamsModalHeader();
          renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
          updateTempTableModalFooter();
        } else {
          button = document.getElementById('agenteComparaProcessCalculationsButton');
          setProcessCalculationsButtonState(button);
          refreshComparisonResultsDetailView();
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
        configurationReviewTab = 'results';
        refreshComparisonDashboardView();
        if (isComparisonReviewMode()) {
          renderTempTableModalContent(getReviewSharedTempTable() || currentTempTable);
          updateTempTableModalFooter();
        } else {
          button = document.getElementById('agenteComparaProcessCalculationsButton');
          setProcessCalculationsButtonState(button);
          refreshComparisonResultsDetailView();
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
        hint.textContent = 'As configurações foram alteradas. Processe novamente para atualizar os resultados.';
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

  function applyCoverageCompletionAndRender(payload, options) {
    options = options || {};
    if (payload && payload.temp_table) {
      setCurrentTempTable(payload.temp_table);
    }
    if (payload && payload.comparison) {
      syncComparisonStateFromPayload(payload.comparison);
    } else if (payload && payload.temp_table && payload.temp_table.comparison) {
      syncComparisonStateFromPayload(payload.temp_table.comparison);
    }
    if ((comparisonState.currentStep || '') !== 'CALCULATION_FILE') {
      return false;
    }
    coverageStepActive = false;
    coveragePromptAnswered = true;
    coveragePromptAccepted = options.accepted === true;
    auditFileStepActive = true;
    tempTableModalActiveTab = 'audit';
    activateComparisonCommonParamsStep('CALCULATION_FILE');
    return true;
  }

  function handleCoveragePromptAnswer(accepted) {
    if (accepted) {
      if (coveragePromptAnswered && coveragePromptAccepted) return;
      if (tempTableSaveInFlight) return;
      coveragePromptAnswered = true;
      coveragePromptAccepted = true;
      if (coverageStepActive) {
        tempTableModalActiveTab = 'coverage';
        setTempTableModalError('');
        renderTempTableModalContent(currentTempTable);
        updateTempTableModalFooter();
        return;
      }
      appendOperationalMessage('Certo. Você pode enviar o arquivo complementar com as cidades atendidas.');
      showCoverageUploadArea();
      return;
    }
    // Decisão negativa é conclusiva: inicia o skip efetivo no backend.
    return skipComparisonCoverageAndAdvance();
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
          if ((comparisonState.currentStep || '') === 'CALCULATION_FILE') {
            applyCoverageCompletionAndRender(res.data, { accepted: true });
            setCoverageUploadStatus('Arquivo de cidades carregado. Continue com o arquivo operacional.', 'success');
          } else {
            coverageStepActive = true;
            coveragePromptAnswered = true;
            coveragePromptAccepted = true;
            tempTableModalActiveTab = 'coverage';
            setCoverageUploadStatus('Arquivo carregado. Revise a aba Cidades atendidas.', 'success');
            if (isTempTableModalOpen()) {
              renderTempTableModalContent(currentTempTable);
              updateTempTableModalFooter();
            }
          }
          if (!isTempTableModalOpen()) {
            appendOperationalMessage('Relação de cidades atendidas carregada.');
            fetchDocuments();
            openTempTableModal();
          } else {
            fetchDocuments();
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
        tempTableEditMode = false;
        tempTableEditSnapshot = null;
        if (!applyCoverageCompletionAndRender(res.data, { accepted: true })) {
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
    if (isComparisonReviewMode()) {
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
      var freightSaveBlocked = false;
      if (!onTaxTab && !onCoverageTab && !onAuditTab) {
        freightSaveBlocked = !tempTableConfirmationCanProceed();
        renderTempTableConfirmationValidationMessage();
      } else {
        clearTempTableConfirmationValidationMessage();
      }
      var saveDisabled = !!(
        tempTableSaveInFlight
        || taxSaveInFlight
        || taxContinueInFlight
        || coverageSaveInFlight
        || freightSaveBlocked
      );
      saveBtn.disabled = saveDisabled;
      saveBtn.setAttribute('aria-disabled', saveDisabled ? 'true' : 'false');
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
    hydrateAccessorialFeesForEditing(editTarget.accessorial_fees);
    editTarget.accessorial_fees.forEach(function (fee) {
      applyResolvedCalculationBaseForSave(fee);
    });
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
    return 'Vincule a uma taxa principal válida ou exclua a regra.';
  }

  function accessorialFeeHasFormalUnmappedBase(fee) {
    if (!fee || typeof fee !== 'object') return false;
    var baseId = String(fee.calculation_base_id || '').trim();
    var basis = normalizeTextKey(fee.calculation_basis);
    var source = String(fee.classification_source || '').trim();
    if (source === 'unmapped_calculation_base') return true;
    if (!baseId && basis === normalizeTextKey('não mapeado / revisar')) return true;
    return false;
  }

  function accessorialFeeMissingCalculationBase(fee) {
    if (accessorialFeeIsMinimumAmount(fee)) return false;
    var baseId = String((fee && fee.calculation_base_id) || '').trim();
    var base = getCalculationBaseById(baseId);
    var basis = normalizeTextKey(fee && fee.calculation_basis);
    return !baseId || !base || basis === normalizeTextKey('não mapeado / revisar') || accessorialFeeHasFormalUnmappedBase(fee);
  }

  function accessorialFeeIsUserCommitted(fee) {
    var source = String((fee && fee.classification_source) || '').trim();
    return source.indexOf('manual_') === 0;
  }

  function accessorialFeeUsesNewBaseContract(fee) {
    var baseId = String((fee && fee.calculation_base_id) || '').trim();
    var basis = normalizeTextKey(fee && fee.calculation_basis);
    var source = String((fee && fee.classification_source) || '').trim();
    return !!baseId || basis === normalizeTextKey('não mapeado / revisar') || source.indexOf('manual_') === 0;
  }

  function accessorialFeeIsExtractionHypothesis(fee) {
    if (!fee || accessorialFeeIsUserCommitted(fee)) return false;
    if (accessorialFeeHasFormalUnmappedBase(fee)) return false;
    var baseId = String(fee.calculation_base_id || '').trim();
    if (baseId) return false;
    if (accessorialFeeUsesNewBaseContract(fee)) return false;
    var source = String(fee.classification_source || '').trim();
    var status = String(fee.status || '').trim();
    if (
      source === 'legacy_classifier'
      && (status === 'needs_review' || status === 'unknown' || status === 'unsupported' || !status)
      && !accessorialFeeHasRequiredValue(fee)
    ) {
      return true;
    }
    return false;
  }

  function accessorialFeeShouldBlockAdvance(fee) {
    if (!fee || typeof fee !== 'object' || isPrimaryFreightAccessorialFee(fee)) return false;
    if (accessorialFeeHasFormalUnmappedBase(fee)) return true;
    if (accessorialFeeIsMinimumAmount(fee)) return true;
    if (accessorialFeeIsExtractionHypothesis(fee)) return true;
    var baseId = String(fee.calculation_base_id || '').trim();
    var basis = normalizeTextKey(fee.calculation_basis);
    var source = String(fee.classification_source || '').trim();
    if (!baseId && basis !== normalizeTextKey('não mapeado / revisar') && source.indexOf('manual_') !== 0) {
      return false;
    }
    return true;
  }

  function buildLocalTempTableValidationSummary() {
    var errors = collectTempTableAdvanceValidationErrors();
    return {
      schema_version: 1,
      can_confirm: errors.length === 0,
      blocking_count: errors.length,
      warning_count: 0,
      blocking_issues: errors.map(function (error, idx) {
        return {
          code: error.reason_code === 'missing_calculation_base'
            ? 'UNMAPPED_CALCULATION_BASE'
            : (error.code || error.reason_code || 'BLOCKING_ISSUE'),
          section: error.section || 'accessorial_fees',
          item_id: error.item_id || ('accessorial_fees:' + (error.index != null ? error.index : idx)),
          index: error.index,
          field: error.field || '',
          label: error.name || ('Item ' + ((error.index != null ? error.index : idx) + 1)),
          reason_code: error.reason_code,
          severity: 'blocking',
          message: error.message || accessorialFieldErrorMessage(error)
        };
      }),
      warnings: []
    };
  }

  function resolveTempTableValidationState() {
    if (tempTableEditMode) {
      return buildLocalTempTableValidationSummary();
    }
    var remote = currentTempTable && currentTempTable.validation;
    if (remote && typeof remote === 'object' && typeof remote.can_confirm === 'boolean') {
      return remote;
    }
    return buildLocalTempTableValidationSummary();
  }

  function tempTableConfirmationCanProceed() {
    var validation = resolveTempTableValidationState();
    return !!(validation && validation.can_confirm === true && Number(validation.blocking_count || 0) === 0);
  }

  function tempTableBlockingCountMessage(count) {
    var n = Number(count) || 0;
    if (n <= 0) return '';
    if (n === 1) {
      return 'Resolva 1 pendência antes de salvar e avançar.';
    }
    return 'Resolva ' + n + ' pendências antes de salvar e avançar.';
  }

  function clearTempTableConfirmationValidationMessage() {
    var el = byId('agenteComparaTempTableModalValidation');
    if (!el) return;
    el.hidden = true;
    el.replaceChildren();
  }

  function renderTempTableConfirmationValidationMessage() {
    var el = byId('agenteComparaTempTableModalValidation');
    if (!el) return;
    var validation = resolveTempTableValidationState();
    var count = Number(validation && validation.blocking_count) || 0;
    var issues = (validation && Array.isArray(validation.blocking_issues)) ? validation.blocking_issues : [];
    if (count <= 0) {
      el.hidden = true;
      el.replaceChildren();
      return;
    }
    el.hidden = false;
    el.setAttribute('role', 'alert');
    var summary = document.createElement('p');
    summary.className = 'agente-compara-temp-table-validation-summary';
    summary.textContent = tempTableBlockingCountMessage(count);
    el.replaceChildren(summary);
    if (issues.length) {
      var list = document.createElement('ul');
      list.className = 'agente-compara-temp-table-validation-list';
      issues.slice(0, 8).forEach(function (issue) {
        var item = document.createElement('li');
        var label = (issue && issue.label) ? String(issue.label) : 'Item';
        var message = (issue && issue.message) ? String(issue.message) : '';
        item.textContent = message ? (label + ': ' + message) : label;
        if (issue && issue.index != null) {
          item.setAttribute('data-accessorial-fee-index', String(issue.index));
        }
        list.appendChild(item);
      });
      el.appendChild(list);
    }
  }

  function validateLinkedMinimumAccessorialFee(fee, fees, feeIndex) {
    var feeName = hasFieldValue(fee && fee.name) ? String(fee.name) : '';
    var displayName = feeName || ('Item ' + (feeIndex + 1));
    if (!accessorialFeeHasValidMinimumAmount(fee)) {
      return {
        section: 'accessorial_fees',
        index: feeIndex,
        name: displayName,
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
        name: displayName,
        field: 'related_to',
        reason_code: relatedTo ? 'invalid_minimum_base_link' : 'missing_minimum_base_link',
        message: feeName
          ? ('Vincule ' + feeName + ' a uma taxa principal válida ou exclua a regra.')
          : accessorialMinimumLinkErrorMessage()
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
      return error.message || accessorialMinimumLinkErrorMessage();
    }
    return error.message || '';
  }

  function accessorialAdvanceValidationCountMessage(count) {
    return tempTableBlockingCountMessage(count);
  }

  function accessorialValidationSummaryMessage(errors) {
    if (!errors || !errors.length) return '';
    return accessorialAdvanceValidationCountMessage(errors.length);
  }

  function resolveTempTableSaveErrorMessage(data) {
    if (!data || typeof data !== 'object') return '';
    if (data.validation && Number(data.validation.blocking_count) > 0) {
      return tempTableBlockingCountMessage(data.validation.blocking_count);
    }
    if (Array.isArray(data.errors) && data.errors.length) {
      return accessorialValidationSummaryMessage(data.errors);
    }
    if (data.error_code === 'invalid_accessorial_fees' && data.message) {
      return String(data.message);
    }
    if (data.error === 'TEMP_TABLE_HAS_BLOCKING_ISSUES' && data.message) {
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
      } else if (accessorialFeeIsExtractionHypothesis(fee) || accessorialFeeMissingCalculationBase(fee)) {
        var feeName = hasFieldValue(fee.name) ? String(fee.name) : '';
        var isHypothesis = accessorialFeeIsExtractionHypothesis(fee);
        error = {
          section: 'accessorial_fees',
          index: feeIndex,
          name: feeName || ('Item ' + (feeIndex + 1)),
          field: 'calculation_base_id',
          reason_code: isHypothesis ? 'unconfirmed_extracted_rule' : 'missing_calculation_base',
          code: isHypothesis ? 'UNCONFIRMED_EXTRACTED_RULE' : 'UNMAPPED_CALCULATION_BASE',
          message: feeName
            ? ('Selecione a base de cálculo de ' + feeName + '.')
            : 'Selecione a base de cálculo antes de continuar.'
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
    updateTempTableModalFooter();
  }

  function refreshTempTableValidationErrorsAfterAccessorialEdit() {
    if (currentTempTable && typeof currentTempTable === 'object') {
      currentTempTable.validation = buildLocalTempTableValidationSummary();
    }
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
    if (currentTempTable && typeof currentTempTable === 'object') {
      currentTempTable.validation = buildLocalTempTableValidationSummary();
    }
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
    var validation = data.validation && typeof data.validation === 'object' ? data.validation : null;
    var errors = Array.isArray(data.errors) ? data.errors : [];
    if (validation && Array.isArray(validation.blocking_issues) && validation.blocking_issues.length && !errors.length) {
      errors = validation.blocking_issues.map(function (issue, idx) {
        return {
          section: issue.section || 'accessorial_fees',
          index: issue.index != null ? issue.index : idx,
          name: issue.label || ('Item ' + (idx + 1)),
          field: issue.field || 'calculation_base_id',
          reason_code: issue.reason_code || issue.code,
          code: issue.code,
          message: issue.message || ''
        };
      });
    }
    var isBlocking =
      data.error_code === 'invalid_accessorial_fees'
      || data.error === 'TEMP_TABLE_HAS_BLOCKING_ISSUES'
      || !!(validation && validation.can_confirm === false);
    if (isBlocking) {
      if (currentTempTable && validation) {
        currentTempTable.validation = validation;
      }
      if (errors.length) {
        setTempTableValidationErrors(errors);
      } else if (data.message) {
        setTempTableModalError(String(data.message));
        updateTempTableModalFooter();
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
    if (!tempTableConfirmationCanProceed()) {
      validateTempTableBeforeAdvance();
      updateTempTableModalFooter();
      return;
    }
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
            if (res.data && (res.data.error_code === 'invalid_accessorial_fees' || res.data.error === 'TEMP_TABLE_HAS_BLOCKING_ISSUES')) {
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
    if (!currentTempTable || !currentTempTable.temp_table_id || tempTableSaveInFlight) {
      return Promise.resolve({ ok: false, blocked: true });
    }
    var generation = comparisonRequestGeneration;
    var expectedComparisonId = comparisonState.comparisonId;
    var expectedTableId = comparisonState.activeTableId;
    var expectedTempTableId = currentTempTable.temp_table_id;
    tempTableSaveInFlight = true;
    setTempTableModalError('');
    if (isTempTableModalOpen()) {
      renderTempTableModalContent(currentTempTable);
    }
    updateTempTableModalFooter();
    return fetch(API_TEMP_TABLE_SAVE, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify({
        temp_table_id: expectedTempTableId,
        review_action: 'skip_coverage_and_advance'
      })
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!isCurrentComparisonRequest(generation, expectedComparisonId, expectedTableId)) {
          return { ok: false, stale: true };
        }
        if (!currentTempTable || currentTempTable.temp_table_id !== expectedTempTableId) {
          return { ok: false, stale: true };
        }
        if (!res.data || res.data.ok !== true) {
          setTempTableModalError(res.data || 'Não foi possível avançar para o arquivo operacional.');
          return { ok: false, status: res.status, data: res.data, restoreCoverage: true };
        }
        var applied = applyCoverageCompletionAndRender(res.data, { accepted: false });
        if (!applied) {
          setTempTableModalError('Não foi possível avançar para o arquivo operacional.');
          return { ok: false, invalidStep: true, restoreCoverage: true };
        }
        // Sincronização secundária; a renderização imediata já ocorreu acima.
        fetchDocuments();
        return { ok: true, status: res.status, data: res.data };
      })
      .catch(function () {
        if (!isCurrentComparisonRequest(generation, expectedComparisonId, expectedTableId)) {
          return { ok: false, stale: true };
        }
        setTempTableModalError('Não foi possível avançar para o arquivo operacional. Verifique sua conexão e tente novamente.');
        return { ok: false, networkError: true, restoreCoverage: true };
      })
      .finally(function () {
        if (!isCurrentComparisonRequest(generation, expectedComparisonId, expectedTableId)) {
          return;
        }
        tempTableSaveInFlight = false;
        if (
          isTempTableModalOpen()
          && (comparisonState.currentStep || '') === 'COVERAGE'
          && !coveragePromptAnswered
        ) {
          renderTempTableModalContent(currentTempTable);
        }
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
        teardownTempTableModal();
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
    syncUploadPageStatusFromTempTable(tempTable);
    if (tempTable && tempTable.temp_table_id && isTempTableModalOpen() && isComparisonWizardEngaged()) {
      var flowStatus = String(tempTable.status || '').toLowerCase();
      if (flowStatus === 'processing') {
        transitionComparisonFlowModal('processing');
      } else if (flowStatus === 'needs_review' && isReviewReadyTempTable(currentTempTable || tempTable)) {
        transitionComparisonFlowModal('review');
      } else if (
        flowStatus === 'failed' ||
        flowStatus === 'expired' ||
        flowStatus === 'discarded'
      ) {
        transitionComparisonFlowModal('failed', {
          message: tempTableContextNote(tempTable)
        });
      }
    }
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
      if (messageOrPayload.error_code === 'auth_required') {
        el.replaceChildren();
        el.appendChild(document.createTextNode(
          'Autenticação necessária para documentos da Agente Compara. '
        ));
        var loginLink = document.createElement('a');
        loginLink.href = '/login?next=/agente-compara';
        loginLink.textContent = 'Faça login';
        loginLink.setAttribute('rel', 'noopener noreferrer');
        el.appendChild(loginLink);
        el.appendChild(document.createTextNode(' para continuar usando.'));
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

  function getUploadPageStatusText() {
    var el = byId('agenteComparaUploadStatus');
    return el ? String(el.textContent || '') : '';
  }

  function isTransientUploadPageStatus(message) {
    var text = String(message || '');
    if (!text) return false;
    if (text === UPLOAD_PAGE_STATUS_SENDING) return true;
    if (text === UPLOAD_PAGE_STATUS_PREPARING) return true;
    if (text === UPLOAD_PAGE_STATUS_PROCESSING) return true;
    if (text.indexOf('Estruturando tabela temporária') === 0) return true;
    return false;
  }

  function clearTransientUploadPageStatus() {
    if (isTransientUploadPageStatus(getUploadPageStatusText())) {
      setStatus('');
    }
  }

  function setTempTableProcessingPageStatus() {
    setStatus(UPLOAD_PAGE_STATUS_PROCESSING);
  }

  function syncUploadPageStatusFromTempTable(tempTable) {
    // Com wizard/modal aberto, o status operacional fica só dentro do modal.
    if (isTempTableModalOpen() && isComparisonWizardFlowActive()) {
      setStatus('');
      return;
    }
    if (uploadInFlight) return;
    if (!tempTable || !tempTable.status) {
      clearTransientUploadPageStatus();
      return;
    }
    var status = String(tempTable.status || '').toLowerCase();
    if (status === 'processing') {
      setTempTableProcessingPageStatus();
      return;
    }
    if (
      status === 'needs_review' ||
      status === 'failed' ||
      status === 'expired' ||
      status === 'discarded' ||
      status === 'awaiting_validation' ||
      status === 'validated'
    ) {
      clearTransientUploadPageStatus();
    }
  }

  /**
   * Encerra estado visual transitório de upload/processamento antes da revisão.
   * Não toca no modal, não faz fetch e não altera dados da tabela.
   */
  function completeTempTableProcessingUi() {
    if (uploadInFlight) {
      uploadInFlight = false;
      var attachBtn = byId('agenteComparaAttachBtn');
      if (attachBtn) {
        attachBtn.setAttribute('aria-busy', 'false');
      }
    }
    clearTransientUploadPageStatus();
  }

  function setUploadLoading(on) {
    uploadInFlight = !!on;
    var attachBtn = byId('agenteComparaAttachBtn');
    var fileInput = byId('agenteComparaFileInput');
    if (attachBtn) {
      attachBtn.setAttribute('aria-busy', on ? 'true' : 'false');
    }
    if (fileInput && on) fileInput.value = '';
    if (on) {
      // Mensagem operacional no modal; página permanece neutra durante o wizard.
      if (isTempTableModalOpen()) {
        setStatus('');
        if (comparisonFlowView !== 'uploading') {
          transitionComparisonFlowModal('uploading');
        }
      } else {
        setStatus(UPLOAD_PAGE_STATUS_SENDING);
      }
      return;
    }
    if (getUploadPageStatusText() === UPLOAD_PAGE_STATUS_SENDING) {
      setStatus('');
    }
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
    var requestGenerationAtStart = comparisonRequestGeneration;
    var comparisonIdAtStart = identityOverride && identityOverride.comparisonId
      ? identityOverride.comparisonId
      : comparisonState.comparisonId;
    var tableIdAtStart = identityOverride && identityOverride.tableId
      ? identityOverride.tableId
      : comparisonState.activeTableId;
    var slot = identityOverride && identityOverride.slot != null
      ? identityOverride.slot
      : (activeComparisonTable() ? activeComparisonTable().slot_number : null);
    if (!comparisonIdAtStart || !tableIdAtStart || slot == null || slot === '') {
      setError('Identidade da comparação inconsistente. Selecione o arquivo novamente.');
      return Promise.resolve(null);
    }
    if (!isCurrentComparisonRequest(requestGenerationAtStart, comparisonIdAtStart, tableIdAtStart)) {
      return Promise.resolve(null);
    }
    setError('');
    setUploadLoading(true);

    function uploadAttemptStillActive() {
      return isCurrentComparisonRequest(
        requestGenerationAtStart,
        comparisonIdAtStart,
        tableIdAtStart
      );
    }

    function responseMatchesUploadAttempt(data) {
      if (!data) return false;
      if (data.comparison_id && data.comparison_id !== comparisonIdAtStart) return false;
      if (
        data.comparison &&
        data.comparison.comparison_id &&
        data.comparison.comparison_id !== comparisonIdAtStart
      ) {
        return false;
      }
      if (data.table_id && data.table_id !== tableIdAtStart) return false;
      if (data.temp_table) {
        if (
          data.temp_table.comparison_id &&
          data.temp_table.comparison_id !== comparisonIdAtStart
        ) {
          return false;
        }
        if (data.temp_table.table_id && data.temp_table.table_id !== tableIdAtStart) {
          return false;
        }
      }
      return true;
    }

    if (!uploadAttemptStillActive()) {
      if (requestGenerationAtStart === comparisonRequestGeneration) {
        setUploadLoading(false);
      }
      return Promise.resolve(null);
    }

    var formData = new FormData();
    formData.append('file', file, file.name);
    formData.append('carrier_name', validation.name);
    formData.append('comparison_id', comparisonIdAtStart);
    formData.append('table_id', tableIdAtStart);
    formData.append('slot', String(slot));

    return fetch(API_UPLOAD, {
      method: 'POST',
      body: formData,
      credentials: 'same-origin'
    })
      .then(function (r) {
        if (!uploadAttemptStillActive()) {
          return { status: r.status, data: null, stale: true };
        }
        return r.json().then(function (data) {
          if (!uploadAttemptStillActive()) {
            return { status: r.status, data: null, stale: true };
          }
          return { status: r.status, data: data };
        });
      })
      .then(function (res) {
        if (!res || res.stale || !uploadAttemptStillActive()) {
          return null;
        }
        if (!res.data || res.data.ok !== true) {
          setError(res.data || friendlyError(res.data));
          if (isTempTableModalOpen()) {
            transitionComparisonFlowModal('failed', {
              message: friendlyError(res.data) || 'Não foi possível enviar o documento. Tente novamente.'
            });
          }
          return null;
        }
        if (!responseMatchesUploadAttempt(res.data)) {
          return null;
        }
        if (window.LogCompletaPixel && typeof window.LogCompletaPixel.trackFunnelEvent === 'function') {
          try {
            var uploadFunnelEvent = res.data.funnel_event;
            window.LogCompletaPixel.trackFunnelEvent(uploadFunnelEvent, {
              content_name: 'Agente Compara',
              source: uploadFunnelEvent && uploadFunnelEvent.source ? uploadFunnelEvent.source : undefined
            });
          } catch (pixelErr) {
            // Meta Pixel nunca bloqueia o fluxo de upload.
          }
        }
        setError('');
        comparisonWizardModalSuppressed = false;
        markComparisonWizardEngaged();
        if (res.data.comparison) {
          if (!uploadAttemptStillActive()) return null;
          syncComparisonStateFromPayload(res.data.comparison);
        }
        if (!uploadAttemptStillActive()) return null;
        return fetchDocuments().then(function (statusData) {
          if (!uploadAttemptStillActive()) return null;
          if (statusData) return statusData;
          if (res.data.temp_table) {
            if (!uploadAttemptStillActive()) return null;
            if (!responseMatchesUploadAttempt(res.data)) return null;
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
        if (!uploadAttemptStillActive()) return null;
        setError('Não foi possível enviar o documento. Tente novamente.');
        if (isTempTableModalOpen()) {
          transitionComparisonFlowModal('failed', {
            message: 'Não foi possível enviar o documento. Tente novamente.'
          });
        }
        return null;
      })
      .finally(function () {
        if (requestGenerationAtStart !== comparisonRequestGeneration) return;
        if (
          comparisonState.comparisonId &&
          comparisonIdAtStart !== comparisonState.comparisonId
        ) {
          return;
        }
        // Encerra só a fase HTTP; processing/review assumem a view do modal.
        setUploadLoading(false);
        syncUploadPageStatusFromTempTable(currentTempTable);
        if (isTempTableModalOpen() && currentTempTable) {
          var endStatus = String(currentTempTable.status || '').toLowerCase();
          if (endStatus === 'processing') {
            transitionComparisonFlowModal('processing');
          } else if (endStatus === 'needs_review' && isReviewReadyTempTable(currentTempTable)) {
            transitionComparisonFlowModal('review');
          } else if (
            endStatus === 'failed' ||
            endStatus === 'expired' ||
            endStatus === 'discarded'
          ) {
            transitionComparisonFlowModal('failed', {
              message: tempTableContextNote(currentTempTable)
            });
          }
        }
        maybeOpenComparisonWizardAfterStatus();
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

  function resolveChatCapabilityFromState() {
    var calcStatus = (comparisonCalculationState && comparisonCalculationState.status) || 'not_started';
    var stale = !!(comparisonCalculationState && comparisonCalculationState.stale);
    var hasResult = !!(comparisonCalculationState && comparisonCalculationState.result);
    var hasAnalytics = !!(
      comparisonCalculationState &&
      comparisonCalculationState.analytics &&
      typeof comparisonCalculationState.analytics === 'object'
    );
    if (stale && calcStatus === 'CALCULATION_READY') return 'stale';
    if (calcStatus === 'CALCULATION_FAILED') return 'failed';
    if (calcStatus === 'CALCULATION_RUNNING') return 'running';
    if (calcStatus === 'CALCULATION_READY' && !stale && hasResult && hasAnalytics) {
      return 'ready';
    }
    return 'locked';
  }

  function isComparisonChatAvailable() {
    return resolveChatCapabilityFromState() === 'ready';
  }

  function placeholderForChatCapability(capability) {
    if (capability === 'ready') return CHAT_READY_PLACEHOLDER;
    return CHAT_LOCKED_PLACEHOLDER;
  }

  function suggestionsForChatCapability(capability) {
    if (capability === 'ready') return CHAT_SUGGESTIONS_READY.slice();
    return [];
  }

  function setChatMetaVisible(visible) {
    var meta = byId('agenteComparaChatMeta');
    var responsibility = byId('agenteComparaChatResponsibility');
    var suggestions = byId('agenteComparaChatSuggestions');
    var scope = byId('agenteComparaChatScope');
    var clearBtn = byId('agenteComparaChatClearBtn');
    var clearWrap = clearBtn ? clearBtn.parentElement : null;
    if (meta) {
      if (visible) meta.classList.remove('d-none');
      else meta.classList.add('d-none');
      meta.hidden = !visible;
    }
    if (responsibility) {
      responsibility.hidden = !visible;
      if (visible) responsibility.classList.remove('d-none');
      else responsibility.classList.add('d-none');
    }
    if (suggestions) {
      suggestions.hidden = !visible;
      if (visible) suggestions.classList.remove('d-none');
      else suggestions.classList.add('d-none');
    }
    if (scope) {
      scope.hidden = !visible;
      if (!visible) scope.textContent = '';
    }
    if (clearBtn) {
      clearBtn.hidden = !visible;
      if (visible) clearBtn.classList.remove('d-none');
      else clearBtn.classList.add('d-none');
    }
    if (clearWrap) {
      clearWrap.hidden = !visible;
      if (visible) clearWrap.classList.remove('d-none');
      else clearWrap.classList.add('d-none');
    }
  }

  function renderChatSuggestions() {
    var host = byId('agenteComparaChatSuggestions');
    if (!host) return;
    host.innerHTML = '';
    if (!chatAvailable) return;
    var items = suggestionsForChatCapability(chatCapability);
    items.forEach(function (text) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'agente-compara-chat-suggestion-btn';
      btn.setAttribute('data-chat-suggestion', text);
      btn.textContent = text;
      btn.addEventListener('click', function () {
        if (!isComparisonChatAvailable()) return;
        prepareContextualChatQuestion(text, null);
      });
      host.appendChild(btn);
    });
  }

  function updateChatScopeLabel(scope) {
    var el = byId('agenteComparaChatScope');
    if (!el) return;
    if (!chatAvailable) {
      el.textContent = '';
      return;
    }
    if (!scope) {
      el.textContent = '';
      return;
    }
    el.textContent = 'Escopo: ' + scope;
  }

  function clearChatConversation() {
    var container = byId('agenteComparaMessages');
    if (container) {
      var nodes = container.querySelectorAll('.agente-compara-chat-msg');
      nodes.forEach(function (node) { node.remove(); });
    }
    chatHistory = [];
    chatUiContext = null;
    updateChatScopeLabel(null);
  }

  function clearFlowGuidanceMessages() {
    var container = byId('agenteComparaMessages');
    if (!container) return;
    var nodes = container.querySelectorAll(
      '[data-chat-flow-guidance="true"], [data-chat-blocked-guidance="true"]'
    );
    nodes.forEach(function (node) { node.remove(); });
  }

  function markLastUserBubbleAsFlowGuidance() {
    var container = byId('agenteComparaMessages');
    if (!container) return;
    var nodes = container.querySelectorAll('.agente-compara-chat-msg-user');
    if (!nodes.length) return;
    var last = nodes[nodes.length - 1];
    last.setAttribute('data-chat-flow-guidance', 'true');
  }

  function lockComparisonChat(options) {
    var opts = options || {};
    chatSendGeneration += 1;
    chatUnlocked = false;
    chatAvailable = false;
    chatCapability = 'locked';
    chatUiContext = null;
    if (opts.clearAnalyticalHistory === true) {
      chatHistory = [];
    }
    if (opts.clearHistory !== false) {
      clearChatConversation();
    }
    setChatMetaVisible(false);
    updateChatLockedUi();
    renderChatSuggestions();
  }

  function syncChatHistoryToComparison() {
    var nextId = (comparisonState && comparisonState.comparisonId) || null;
    if (chatScopedComparisonId && nextId && chatScopedComparisonId !== nextId) {
      lockComparisonChat({ clearHistory: true });
    }
    if (!nextId && chatScopedComparisonId) {
      lockComparisonChat({ clearHistory: true });
    }
    chatScopedComparisonId = nextId;
  }

  function syncProgressiveChatUnlock() {
    syncChatHistoryToComparison();
    var wasAvailable = !!chatAvailable;
    chatCapability = resolveChatCapabilityFromState();
    chatAvailable = chatCapability === 'ready';
    chatUnlocked = chatAvailable;
    if (!chatAvailable) {
      setChatMetaVisible(false);
      updateChatLockedUi();
      renderChatSuggestions();
      updateChatScopeLabel(null);
      return;
    }
    // Transição para READY: remove orientação transitória e inicia histórico analítico limpo.
    if (!wasAvailable) {
      clearFlowGuidanceMessages();
      chatHistory = [];
      chatUiContext = null;
    }
    setChatMetaVisible(true);
    updateChatLockedUi();
    renderChatSuggestions();
    updateChatScopeLabel(null);
  }

  function prepareContextualChatQuestion(question, structuredContext) {
    if (!isComparisonChatAvailable()) return;
    var input = byId('agenteComparaInput');
    if (!input) return;
    chatUiContext = structuredContext && typeof structuredContext === 'object'
      ? structuredContext
      : null;
    input.value = question || '';
    syncProgressiveChatUnlock();
    if (input.disabled) return;
    input.focus();
    try {
      var len = input.value.length;
      input.setSelectionRange(len, len);
    } catch (_) {}
  }

  function buildComparisonChatUiContext() {
    var ctx = chatUiContext && typeof chatUiContext === 'object' ? Object.assign({}, chatUiContext) : {};
    ctx.active_view = ctx.active_view || 'dashboard';
    if (comparisonResultsUiState && comparisonResultsUiState.filters) {
      var filters = comparisonResultsUiState.filters;
      var activeFilters = {};
      if (filters.destinationUf) activeFilters.destination_uf = filters.destinationUf;
      if (filters.originUf) activeFilters.origin_uf = filters.originUf;
      if (filters.documentNumber) activeFilters.document_number = filters.documentNumber;
      if (filters.status && filters.status !== 'all') activeFilters.status = filters.status;
      if (Object.keys(activeFilters).length) ctx.active_filters = activeFilters;
      if (!ctx.selected_uf && filters.destinationUf) ctx.selected_uf = filters.destinationUf;
      if (!ctx.document_number && filters.documentNumber) ctx.document_number = filters.documentNumber;
    }
    return ctx;
  }

  function updateChatLockedUi() {
    var input = byId('agenteComparaInput');
    var composer = byId('agenteComparaComposer');
    var sendBtn = byId('agenteComparaSend');
    if (!input) return;

    var analytical = !!chatAvailable && chatCapability === 'ready';
    // Pré-READY e READY: composer permanece utilizável (só in-flight desabilita via setChatInputEnabled).
    input.disabled = false;
    input.placeholder = placeholderForChatCapability(analytical ? 'ready' : 'locked');
    input.setAttribute('aria-disabled', 'false');
    input.classList.remove('agente-compara-input--locked');
    if (composer) composer.classList.remove('agente-compara-composer--chat-locked');
    if (sendBtn) {
      sendBtn.disabled = false;
      sendBtn.setAttribute('aria-disabled', 'false');
    }
  }

  function unlockChat() {
    syncProgressiveChatUnlock();
    if (!chatAvailable) return;
  }

  function appendBlockedChatGuidance() {
    var container = byId('agenteComparaMessages');
    if (!container) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-bot';
    msg.setAttribute('data-chat-role', 'assistant');
    msg.setAttribute('data-chat-blocked-guidance', 'true');
    msg.setAttribute('data-chat-flow-guidance', 'true');

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

  function respondWithPreReadyGuidance(userText) {
    appendChatBubble('user', userText, { flowGuidance: true });
    appendBlockedChatGuidance();
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
    // Pré-READY: composer permanece acionável (orientação local, sem fetch).
    if (!chatAvailable) {
      if (input) {
        input.disabled = false;
        input.setAttribute('aria-disabled', 'false');
      }
      if (sendBtn) {
        sendBtn.disabled = false;
        sendBtn.setAttribute('aria-disabled', 'false');
      }
      return;
    }
    if (input) {
      input.disabled = !enabled;
      input.setAttribute('aria-disabled', enabled ? 'false' : 'true');
    }
    if (sendBtn) {
      sendBtn.disabled = !enabled;
      sendBtn.setAttribute('aria-disabled', enabled ? 'false' : 'true');
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

  function appendChatBubble(role, textOrPayload, options) {
    var container = byId('agenteComparaMessages');
    if (!container) return;
    var opts = options || {};

    var isUser = role === 'user';
    var limitPayload = !isUser ? resolvePlanLimitPayload(textOrPayload) : null;
    var text = typeof textOrPayload === 'string'
      ? textOrPayload
      : (textOrPayload && textOrPayload.message) || '';
    if (!limitPayload && !text) return;

    var msg = document.createElement('div');
    msg.className = 'agente-compara-chat-msg agente-compara-chat-msg-' + (isUser ? 'user' : 'bot');
    msg.setAttribute('data-chat-role', isUser ? 'user' : 'assistant');
    if (opts.flowGuidance) {
      msg.setAttribute('data-chat-flow-guidance', 'true');
    }

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
    var code = (data && (data.error_code || data.error)) || '';
    if (typeof code !== 'string') code = '';
    if (status === 401) {
      return (data && data.message) || CHAT_SESSION_MESSAGE;
    }
    if (status === 403) {
      if (code === 'franquia_blocked' || resolvePlanLimitPayload(data)) {
        return (data && data.message) || CHAT_LIMIT_MESSAGE;
      }
      return (data && data.message) || friendlyError(data);
    }
    if (status === 409 && (code === 'COMPARISON_CHAT_NOT_READY' || code === 'comparison_chat_not_ready')) {
      return (data && data.message) || CHAT_NOT_READY_MESSAGE;
    }
    if (status === 409 && (code === 'agente_compara_comparison_scope_mismatch' || String(code).indexOf('stale') >= 0)) {
      return (data && data.message) || CHAT_STALE_MESSAGE;
    }
    if (status === 429) {
      return (data && data.message) || CHAT_LIMIT_MESSAGE;
    }
    if (code === 'provider_not_configured') {
      return (data && data.message) || CHAT_PROVIDER_NOT_CONFIGURED_MESSAGE;
    }
    if (code === 'provider_initialization_failed') {
      return (data && data.message) || CHAT_PROVIDER_INIT_MESSAGE;
    }
    if (code === 'provider_timeout') {
      return (data && data.message) || CHAT_PROVIDER_TIMEOUT_MESSAGE;
    }
    if (code === 'provider_empty_response' || code === 'provider_invalid_response') {
      return (data && data.message) || CHAT_PROVIDER_EMPTY_MESSAGE;
    }
    if (code === 'provider_request_failed' || code === 'service_unavailable') {
      return (data && data.message) || CHAT_PROVIDER_MESSAGE;
    }
    if (status === 503) {
      return (data && data.message) || CHAT_PROVIDER_MESSAGE;
    }
    if (data && data.message) return data.message;
    return CHAT_FIXED_ERRORS.service;
  }

  function applyComparisonChatAvailabilityFromError(data, status) {
    if (status === 409) {
      var code = (data && (data.error_code || data.error)) || '';
      if (code === 'COMPARISON_CHAT_NOT_READY' || code === 'comparison_chat_not_ready') {
        // Volta ao modo pré-READY sem apagar o DOM; limpa só o histórico analítico.
        lockComparisonChat({ clearHistory: false, clearAnalyticalHistory: true });
      }
      return;
    }
    // Falha de provider: chat permanece disponível se a comparação continua READY.
    if (data && data.chat_available === true) {
      chatAvailable = true;
      chatUnlocked = true;
      chatCapability = 'ready';
      setChatMetaVisible(true);
      updateChatLockedUi();
    }
  }

  function isComparisonChatNotReadyError(data, status) {
    if (status !== 409) return false;
    var code = (data && (data.error_code || data.error)) || '';
    return code === 'COMPARISON_CHAT_NOT_READY' || code === 'comparison_chat_not_ready';
  }

  function sendChatMessage() {
    var input = byId('agenteComparaInput');
    if (!input || chatInFlight) return;

    syncProgressiveChatUnlock();

    var text = (input.value || '').trim();
    if (!text) return;

    // Pré-READY: orientação local determinística — zero backend / zero Gemini / zero consumo.
    if (!chatAvailable || !isComparisonChatAvailable()) {
      input.value = '';
      respondWithPreReadyGuidance(text);
      if (input) input.focus();
      return;
    }

    var sendGeneration = chatSendGeneration;
    input.value = '';
    appendChatBubble('user', text);

    var historyForApi = trimChatHistory(chatHistory.slice());
    var requestId = generateRequestId();
    var uiContext = buildComparisonChatUiContext();
    var visualFocus = null;
    if (uiContext.visual_focus) {
      visualFocus = uiContext.visual_focus;
    }

    chatInFlight = true;
    setChatInputEnabled(false);
    setChatLoading(true);

    var payload = {
      message: text,
      question: text,
      history: historyForApi,
      request_id: requestId,
      comparison_id: (comparisonState && comparisonState.comparisonId) || null,
      ui_context: uiContext,
      visual_focus: visualFocus
    };
    // Consume one-shot structured reference after send preparation.
    chatUiContext = null;

    fetch(API_COMPARISON_CHAT, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'same-origin',
      body: JSON.stringify(payload)
    })
      .then(function (r) {
        return r.json().then(function (data) {
          return { status: r.status, data: data };
        }).catch(function () {
          return { status: r.status, data: null };
        });
      })
      .then(function (res) {
        if (sendGeneration !== chatSendGeneration) return;
        setChatLoading(false);
        if (res.status === 401 || res.status === 403) {
          if (resolvePlanLimitPayload(res.data)) {
            appendChatBubble('assistant', res.data);
          } else {
            appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          }
          // Erro tÃ©cnico: não entra no histórico enviado ao Gemini.
          return;
        }
        if (!res.data || res.data.ok !== true) {
          applyComparisonChatAvailabilityFromError(res.data, res.status);
          if (isComparisonChatNotReadyError(res.data, res.status)) {
            // Corrida de estado: orientação fixa local, sem erro tÃ©cnico de provider.
            markLastUserBubbleAsFlowGuidance();
            appendBlockedChatGuidance();
            return;
          }
          if (resolvePlanLimitPayload(res.data)) {
            appendChatBubble('assistant', res.data);
          } else {
            appendChatBubble('assistant', chatErrorMessage(res.data, res.status));
          }
          // Preserva a pergunta no UI, mas não adiciona resposta técnica ao history.
          return;
        }

        var answer = typeof res.data.answer === 'string' ? res.data.answer : '';
        appendChatBubble('assistant', answer || CHAT_FIXED_ERRORS.service);
        if (res.data.scope) updateChatScopeLabel(res.data.scope);
        chatHistory.push({ role: 'user', content: text });
        chatHistory.push({ role: 'assistant', content: answer });
        chatHistory = trimChatHistory(chatHistory);
        refreshAttachmentsAfterChat();
      })
      .catch(function () {
        if (sendGeneration !== chatSendGeneration) return;
        setChatLoading(false);
        appendChatBubble('assistant', CHAT_NETWORK_MESSAGE);
      })
      .finally(function () {
        if (sendGeneration !== chatSendGeneration) {
          chatInFlight = false;
          return;
        }
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
    var clearBtn = byId('agenteComparaChatClearBtn');
    if (!form || !input || !sendBtn) return;

    // Estado inicial: modo pré-READY (orientação local; composer utilizável).
    lockComparisonChat({ clearHistory: false });
    syncProgressiveChatUnlock();

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
    if (clearBtn && !clearBtn.getAttribute('data-bound')) {
      clearBtn.setAttribute('data-bound', '1');
      clearBtn.addEventListener('click', function (e) {
        e.preventDefault();
        if (!isComparisonChatAvailable()) return;
        clearChatConversation();
      });
    }

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
          .catch(function () { markAgenteComparaCopied(target, 'Falhou'); });
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
    var presentation = item && item.review_presentation && typeof item.review_presentation === 'object'
      ? item.review_presentation
      : null;

    if (presentation) {
      var state = String(presentation.state || '').trim();
      var severity = String(presentation.severity || '').trim() || 'info';
      var requiresAction = presentation.requires_action === true;
      var isBlocking = presentation.is_blocking === true;
      var basisLabel = hasFieldValue(presentation.basis_label)
        ? String(presentation.basis_label)
        : '';
      var secondaryText = hasFieldValue(presentation.secondary_text)
        ? String(presentation.secondary_text)
        : '';

      td.className = 'accessorial-basis-review accessorial-basis-review--' + severity;
      if (requiresAction || isBlocking) {
        td.className += ' accessorial-basis-review--action-required';
      }

      var labelEl = document.createElement('div');
      labelEl.className = 'accessorial-basis-review__label';
      labelEl.textContent = basisLabel || 'Base não classificada';
      td.appendChild(labelEl);

      if (secondaryText) {
        var secondaryEl = document.createElement('div');
        secondaryEl.className = 'accessorial-basis-review__secondary';
        secondaryEl.textContent = secondaryText;
        td.appendChild(secondaryEl);
      }

      if (severity === 'error') {
        var badge = document.createElement('span');
        badge.className = 'accessorial-basis-review__badge accessorial-basis-review__badge--error';
        badge.setAttribute('aria-hidden', 'true');
        badge.textContent = 'Bloqueante';
        td.appendChild(badge);
      }

      var ariaParts = [basisLabel || 'Base de cálculo'];
      if (secondaryText) ariaParts.push(secondaryText);
      if (requiresAction) ariaParts.push('ação obrigatória');
      if (state) ariaParts.push('estado ' + state);
      td.setAttribute('aria-label', ariaParts.join('. '));
      tr.appendChild(td);
      return;
    }

    // Compatibilidade com payload legado sem review_presentation.
    var baseId = String((item && item.calculation_base_id) || '').trim();
    var resolvedBase = baseId ? getCalculationBaseById(baseId) : null;
    var source = String((item && item.classification_source) || '').trim();
    var modifier = String((item && item.modifier_type) || '').trim();
    var calcType = String((item && item.calculation_type) || '').trim();
    var relatedTo = String((item && item.related_to) || '').trim();

    if (resolvedBase) {
      td.className = 'accessorial-basis-review accessorial-basis-review--info';
      td.textContent = hasFieldValue(item.calculation_base_label)
        ? String(item.calculation_base_label)
        : String(resolvedBase.label || '');
      tr.appendChild(td);
      return;
    }

    if (source === 'unmapped_calculation_base' || accessorialFeeHasFormalUnmappedBase(item)) {
      td.className = 'accessorial-basis-review accessorial-basis-review--error accessorial-basis-review--action-required';
      var unresolvedLabel = document.createElement('div');
      unresolvedLabel.className = 'accessorial-basis-review__label';
      unresolvedLabel.textContent = 'Base de cálculo não identificada';
      td.appendChild(unresolvedLabel);
      var unresolvedHint = document.createElement('div');
      unresolvedHint.className = 'accessorial-basis-review__secondary';
      unresolvedHint.textContent = 'Selecione a base de cálculo antes de continuar.';
      td.appendChild(unresolvedHint);
      var unresolvedBadge = document.createElement('span');
      unresolvedBadge.className = 'accessorial-basis-review__badge accessorial-basis-review__badge--error';
      unresolvedBadge.setAttribute('aria-hidden', 'true');
      unresolvedBadge.textContent = 'Bloqueante';
      td.appendChild(unresolvedBadge);
      td.setAttribute('aria-label', 'Base de cálculo não identificada. Ação obrigatória.');
      tr.appendChild(td);
      return;
    }

    if ((modifier === 'minimum_amount' || calcType === 'minimum_amount') && relatedTo) {
      td.className = 'accessorial-basis-review accessorial-basis-review--info';
      td.textContent = 'Mínimo aplicável a ' + relatedTo;
      td.setAttribute('aria-label', 'Mínimo aplicável a ' + relatedTo);
      tr.appendChild(td);
      return;
    }

    td.className = 'accessorial-basis-review accessorial-basis-review--error accessorial-basis-review--action-required';
    var legacyLabel = document.createElement('div');
    legacyLabel.className = 'accessorial-basis-review__label';
    legacyLabel.textContent = 'Base não classificada';
    td.appendChild(legacyLabel);
    var legacyHint = document.createElement('div');
    legacyHint.className = 'accessorial-basis-review__secondary';
    legacyHint.textContent = 'Selecione a base de cálculo antes de continuar.';
    td.appendChild(legacyHint);
    var extracted = hasFieldValue(item && item.raw_calculation_basis)
      ? String(item.raw_calculation_basis)
      : (hasFieldValue(item && item.calculation_basis) ? String(item.calculation_basis) : '');
    if (
      extracted
      && normalizeTextKey(extracted) !== normalizeTextKey('não mapeado / revisar')
      && normalizeTextKey(extracted) !== normalizeTextKey('Base não classificada')
    ) {
      var extra = document.createElement('div');
      extra.className = 'accessorial-basis-extracted-text';
      extra.textContent = 'texto extraído: ' + extracted;
      td.appendChild(extra);
    }
    var legacyBadge = document.createElement('span');
    legacyBadge.className = 'accessorial-basis-review__badge accessorial-basis-review__badge--error';
    legacyBadge.setAttribute('aria-hidden', 'true');
    legacyBadge.textContent = 'Bloqueante';
    td.appendChild(legacyBadge);
    td.setAttribute('aria-label', 'Base não classificada. Ação obrigatória.');
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
    placeholder.textContent = 'n\u00e3o mapeado / revisar';
    select.appendChild(placeholder);
    currentCalculationBases.forEach(function (base) {
      if (!base || !base.id) return;
      var option = document.createElement('option');
      option.value = String(base.id);
      option.textContent = calculationBaseOptionLabel(base);
      select.appendChild(option);
    });
    var selectedBaseId = item && item.editable_calculation_base_id
      ? String(item.editable_calculation_base_id)
      : (item && item.calculation_base_id ? String(item.calculation_base_id) : '');
    select.value = selectedBaseId;
    select.addEventListener('change', function () {
      if (typeof onChange === 'function') onChange(select.value);
    });
    td.appendChild(select);
    if (validationError) {
      var icon = document.createElement('span');
      icon.className = 'accessorial-field-error-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = '!';
      td.appendChild(icon);
      var hint = document.createElement('div');
      hint.className = 'accessorial-field-error';
      hint.setAttribute('role', 'note');
      hint.textContent = accessorialFieldErrorMessage(validationError);
      td.appendChild(hint);
    }
    tr.appendChild(td);
  }

  function appendMinimumLinkCell(tr, item, fees, feeIndex, validationError) {
    var td = document.createElement('td');
    td.className = validationError ? 'calculation-base-cell calculation-base-cell--invalid' : 'calculation-base-cell';
    var wrapper = document.createElement('div');
    wrapper.className = 'accessorial-basis-review accessorial-basis-review--info';
    var label = document.createElement('div');
    label.className = 'accessorial-basis-review__label';
    label.textContent = item && item.edit_display_label
      ? String(item.edit_display_label)
      : accessorialMinimumLinkLabel(item, fees, feeIndex);
    wrapper.appendChild(label);
    td.appendChild(wrapper);
    if (validationError) {
      var icon = document.createElement('span');
      icon.className = 'accessorial-field-error-icon';
      icon.setAttribute('aria-hidden', 'true');
      icon.textContent = 'â ';
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
    hydrateAccessorialFeesForEditing(currentTempTable.accessorial_fees || []);
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
      if (item.edit_mode === 'minimum_link') {
        appendMinimumLinkCell(
          tr,
          item,
          currentTempTable.accessorial_fees || [],
          feeIndex,
          getAccessorialFeeValidationError(feeIndex, 'calculation_base_id')
        );
      } else {
        appendCalculationBaseSelectCell(tr, item, function (baseId) {
          var fee = currentTempTable.accessorial_fees[feeIndex];
          if (!fee) return;
          var base = getCalculationBaseById(baseId);
          if (base) {
            applyCalculationBaseToAccessorialFee(fee, base);
            fee.editable_calculation_base_id = String(base.id || '');
            fee.edit_mode = 'base_select';
            fee.edit_display_label = '';
          } else {
            markAccessorialFeeAsUnmapped(fee);
            fee.editable_calculation_base_id = '';
            fee.edit_mode = 'unmapped';
            fee.edit_display_label = '';
          }
          refreshTempTableValidationErrorsAfterAccessorialEdit();
          renderTempTableModalContent(currentTempTable);
        }, getAccessorialFeeValidationError(feeIndex, 'calculation_base_id'));
      }
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

  function shouldShowConfigurationProcessCta() {
    if (!isComparisonReviewMode()) return false;
    if (!comparisonState.comparisonId) return false;
    if (auditUploadInFlight || comparisonCalculationInFlight) return true;
    var step = comparisonState.currentStep || '';
    var billing = comparisonCalculationState.billingStatus || '';
    if (step === 'CONFIGURATION_READY') return true;
    if (step === 'CALCULATION_FAILED') return true;
    if (step === 'CALCULATION_READY' && comparisonCalculationState.stale) return true;
    if (step === 'CALCULATION_READY' && (billing === 'pending' || billing === 'failed')) return true;
    return false;
  }

  function renderConfigurationReadyConfirmation(container) {
    if (!container || !shouldShowConfigurationProcessCta()) return;
    var existing = container.querySelector('#agenteComparaConfigurationReadyConfirmation');
    if (existing) existing.remove();

    var panel = document.createElement('section');
    panel.className = 'agente-compara-configuration-ready-confirmation';
    panel.id = 'agenteComparaConfigurationReadyConfirmation';
    panel.setAttribute('role', 'region');
    panel.setAttribute('aria-label', 'Confirmação para processar cÃ¡lculos');

    var title = document.createElement('h3');
    title.className = 'agente-compara-configuration-ready-title';
    title.textContent = 'Configuração pronta para cálculo';
    panel.appendChild(title);

    var text = document.createElement('p');
    text.className = 'agente-compara-configuration-ready-text';
    text.textContent = 'As configurações estão prontas. O cálculo será iniciado somente após sua confirmação.';
    panel.appendChild(text);

    var summary = document.createElement('div');
    summary.className = 'agente-compara-configuration-ready-summary';
    var confirmed = confirmedComparisonTablesForReview();
    var carrierCounts = {};
    confirmed.forEach(function (tableMeta) {
      var name = tableCarrierDisplay(tableMeta) || ('Transportadora ' + String(tableMeta.slot_number || ''));
      carrierCounts[name] = (carrierCounts[name] || 0) + 1;
    });
    confirmed.forEach(function (tableMeta) {
      var row = document.createElement('div');
      row.className = 'agente-compara-configuration-ready-summary-row';
      var name = tableCarrierDisplay(tableMeta) || ('Transportadora ' + String(tableMeta.slot_number || ''));
      var label = name;
      if (carrierCounts[name] > 1) {
        label = name + ' — Tabela ' + String(tableMeta.slot_number || '');
      }
      row.textContent = 'Tabela ' + String(tableMeta.slot_number || '') + ': ' + label + ' (confirmada)';
      summary.appendChild(row);
    });

    var shared = getReviewSharedTempTable() || currentTempTable;
    var meta = getCalculationFileMetadata(shared);
    if (meta) {
      var fileRow = document.createElement('div');
      fileRow.className = 'agente-compara-configuration-ready-summary-row';
      fileRow.textContent = 'Arquivo: ' + (meta.source_file_name || '—');
      summary.appendChild(fileRow);
      var rowsRow = document.createElement('div');
      rowsRow.className = 'agente-compara-configuration-ready-summary-row';
      rowsRow.textContent = 'Linhas do arquivo: ' + (meta.row_count != null ? String(meta.row_count) : '—');
      summary.appendChild(rowsRow);
    }
    panel.appendChild(summary);

    var actions = document.createElement('div');
    actions.className = 'agente-compara-run-actions';
    actions.id = 'agenteComparaProcessCalculationsActions';
    var runBtn = document.createElement('button');
    runBtn.type = 'button';
    runBtn.className = 'agente-compara-run-btn';
    runBtn.id = 'agenteComparaProcessCalculationsButton';
    runBtn.textContent = 'Processar Cálculos';
    setProcessCalculationsButtonState(runBtn);
    actions.appendChild(runBtn);
    panel.appendChild(actions);

    var hint = document.createElement('p');
    hint.className = 'agente-compara-process-calculations-hint';
    hint.id = 'agenteComparaProcessCalculationsHint';
    if (comparisonCalculationState.stale) {
      hint.textContent = 'As configurações foram alteradas. Processe novamente para atualizar os resultados.';
    } else if (comparisonCalculationState.status === 'CALCULATION_FAILED') {
      hint.textContent = 'Houve uma falha no cálculo. Você pode processar novamente.';
    } else if (comparisonCalculationState.billingStatus === 'failed') {
      hint.textContent = 'Tente novamente para regularizar o processamento sem recalcular.';
    } else {
      hint.textContent = 'As configurações estão prontas. O cálculo será iniciado somente após sua confirmação.';
    }
    panel.appendChild(hint);
    container.appendChild(panel);
  }

  function renderConfigurationReviewResultsContent(container) {
    appendSectionTitle(container, 'Resultados');
    var host = document.createElement('div');
    host.className = 'agente-compara-comparison-results-host';
    host.id = 'agenteComparaComparisonResultsHost';
    container.appendChild(host);

    if (!shouldEnableResultsReviewTab()) {
      var waiting = document.createElement('p');
      waiting.className = 'agente-compara-temp-table-modal-empty';
      waiting.textContent = 'Os resultados aparecerão aqui após o processamento dos cÃ¡lculos.';
      host.appendChild(waiting);
      return;
    }

    var actions = document.createElement('div');
    actions.className = 'agente-compara-run-actions';
    if (!document.getElementById('agenteComparaProcessCalculationsButton')) {
      var runBtn = document.createElement('button');
      runBtn.type = 'button';
      runBtn.className = 'agente-compara-run-btn';
      runBtn.id = 'agenteComparaProcessCalculationsButton';
      runBtn.textContent = 'Processar Cálculos';
      setProcessCalculationsButtonState(runBtn);
      actions.appendChild(runBtn);
      host.appendChild(actions);
    } else {
      var btn = document.getElementById('agenteComparaProcessCalculationsButton');
      setProcessCalculationsButtonState(btn);
    }

    renderComparisonCalculationResults(host, comparisonCalculationState.result);
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
      renderCalculationFileSummary(card, tempTable, {
        showProcessButton: !shouldShowConfigurationProcessCta()
      });
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
    if (tabId === 'results') {
      renderConfigurationReviewResultsContent(panel);
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

    var resultsEnabled = shouldEnableResultsReviewTab();
    var resultsTab = makeReviewTab('agenteComparaReviewTabResults', 'Resultados', activeTab === 'results', false);
    resultsTab.setAttribute('data-review-tab', 'results');
    if (!resultsEnabled) {
      resultsTab.disabled = true;
      resultsTab.setAttribute('aria-disabled', 'true');
      resultsTab.title = 'Disponível após iniciar o processamento dos cÃ¡lculos.';
    } else {
      resultsTab.setAttribute('aria-disabled', 'false');
      resultsTab.addEventListener('click', function () {
        selectConfigurationReviewTab('results');
      });
    }
    tabs.appendChild(resultsTab);

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
        if (!isComparisonReviewMode()) {
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
    if (!isComparisonReviewMode()) return;
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
    if (isComparisonReviewMode()) {
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
    if (isComparisonReviewMode()) {
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

    var busy = !!tempTableSaveInFlight;

    var yesBtn = document.createElement('button');
    yesBtn.type = 'button';
    yesBtn.className = 'agente-compara-coverage-prompt-yes agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-primary';
    yesBtn.textContent = 'Sim, enviar planilha';
    yesBtn.disabled = busy;
    yesBtn.setAttribute('aria-busy', busy ? 'true' : 'false');
    yesBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(true);
    });

    var noBtn = document.createElement('button');
    noBtn.type = 'button';
    noBtn.className = 'agente-compara-coverage-prompt-no agente-compara-coverage-prompt-btn agente-compara-coverage-prompt-btn-secondary';
    noBtn.textContent = 'Agora não';
    noBtn.disabled = busy;
    noBtn.setAttribute('aria-busy', busy ? 'true' : 'false');
    noBtn.addEventListener('click', function () {
      handleCoveragePromptAnswer(false);
    });

    actions.appendChild(yesBtn);
    actions.appendChild(noBtn);
    card.appendChild(actions);
    container.appendChild(card);
  }

  function renderCoverageAdvancingState(container) {
    var info = document.createElement('p');
    info.className = 'agente-compara-temp-table-modal-empty agente-compara-coverage-advancing-state';
    info.setAttribute('role', 'status');
    info.setAttribute('aria-live', 'polite');
    info.setAttribute('aria-busy', 'true');
    info.textContent = 'Avançando para o arquivo de comparação...';
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

    if (!hasCoverageRows(tempTable) && tempTableSaveInFlight && !coveragePromptAnswered) {
      renderCoverageAdvancingState(section);
    } else if (!hasCoverageRows(tempTable) && !coveragePromptAnswered) {
      renderCoverageDecisionCard(section);
    } else if (!hasCoverageRows(tempTable) && coveragePromptAccepted) {
      renderCoverageUploadCard(section, 'agenteComparaCoverageModal');
    } else if (!hasCoverageRows(tempTable) && coveragePromptAnswered && !coveragePromptAccepted) {
      // Estado residual sem avanço: reapresenta a decisão (sem tela terminal órfã).
      renderCoverageDecisionCard(section);
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
    if (
      tempTable &&
      tempTable.comparison_id &&
      comparisonState.comparisonId &&
      tempTable.comparison_id !== comparisonState.comparisonId
    ) {
      return;
    }
    if (
      tempTable &&
      tempTable.table_id &&
      comparisonState.activeTableId &&
      tempTable.table_id !== comparisonState.activeTableId &&
      !isComparisonReviewMode()
    ) {
      return;
    }
    body.replaceChildren();

    if (isComparisonReviewMode()) {
      prepareConfigurationReviewRender();
      renderConfigurationReadyConfirmation(body);
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
    tempTableSaveInFlight = false;
    clearTempTableValidationErrors();
    setTempTableModalError('');
    if (isComparisonReviewMode()) {
      ensureConfigurationReviewDefaults({
        forceFirstCarrier: !shouldEnableResultsReviewTab(),
        forceResultsTab: shouldEnableResultsReviewTab()
      });
      selectConfigurationReviewTab(configurationReviewTab, { preferCache: true });
      showTempTableModalShell();
      return;
    }
    if (isComparisonConfigurationFlow()) {
      activateComparisonCommonParamsStep(comparisonState.currentStep);
      return;
    }
    if (currentTempTable) {
      var status = String(currentTempTable.status || '').toLowerCase();
      if (status === 'processing') {
        renderAndShowComparisonFlowModal('processing');
        return;
      }
      if (status === 'failed' || status === 'expired' || status === 'discarded') {
        renderAndShowComparisonFlowModal('failed');
        return;
      }
      if (status === 'needs_review') {
        renderAndShowComparisonFlowModal('review');
        return;
      }
    }
    renderTempTableModalContent(currentTempTable);
    updateTempTableModalFooter();
    if (!showTempTableModalShell()) return;
    var saveBtn = byId('agenteComparaTempTableModalSave');
    if (saveBtn) saveBtn.focus();
  }

  function closeTempTableModal() {
    var modal = byId('agenteComparaTempTableModal');
    if (!modal || modal.hidden) return;
    closeAuditCalculationMemory();
    hideTempTableModalShell();
    restoreCarrierIdentifyPanelHome();
    comparisonFlowView = null;
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
    var saveBtn = byId('agenteComparaTempTableModalSave');
    if (saveBtn && (saveBtn.disabled || saveBtn.getAttribute('aria-disabled') === 'true')) {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      validateTempTableBeforeAdvance();
      updateTempTableModalFooter();
      return;
    }
    if (!tempTableConfirmationCanProceed()) {
      if (event) {
        event.preventDefault();
        event.stopPropagation();
      }
      validateTempTableBeforeAdvance();
      updateTempTableModalFooter();
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
    loadComparisonDashboardPreferences();
    bindComparisonDashboardDetailsButton();
    bindComparisonDashboardCustomizeControls();
    refreshComparisonDashboardView();
    restoreComparisonCalculationFromStatus();
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', init);
  } else {
    init();
  }

  // Hook de teste/browser: injeta resultado já calculado sem POST calculate.
  document.addEventListener('agente-compara:inject-comparison-result', function (event) {
    var detail = event && event.detail;
    if (!detail || typeof detail !== 'object') return;
    var payload = detail.payload && typeof detail.payload === 'object' ? detail.payload : detail;
    if (payload.comparison_id) {
      comparisonState.comparisonId = payload.comparison_id;
    }
    applyComparisonCalculationPayload(payload);
    if (payload.current_step) {
      comparisonState.currentStep = payload.current_step;
    } else if (payload.status === 'CALCULATION_READY') {
      comparisonState.currentStep = 'CALCULATION_READY';
    }
    configurationReviewTab = 'results';
    var host = document.getElementById('agenteComparaComparisonResultsHost');
    if (!host) {
      host = document.createElement('div');
      host.id = 'agenteComparaComparisonResultsHost';
      host.className = 'agente-compara-comparison-results-host';
      var root = document.querySelector('.agente-compara-page') || document.body;
      root.appendChild(host);
    }
    refreshComparisonCalculationViews();
  });

  // Hooks de teste do chat: exercitam guards sem expor API pública.
  document.addEventListener('agente-compara:test-send-chat', function () {
    sendChatMessage();
  });
  document.addEventListener('agente-compara:test-lock-chat', function () {
    lockComparisonChat({ clearHistory: true });
    clearLocalComparisonState();
    comparisonCalculationState.status = 'not_started';
    comparisonCalculationState.result = null;
    comparisonCalculationState.analytics = null;
    comparisonCalculationState.stale = false;
    syncProgressiveChatUnlock();
  });
})();
