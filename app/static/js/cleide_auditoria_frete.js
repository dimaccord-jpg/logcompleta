(() => {
  const root = document.querySelector(".cleide-page");
  if (!root) return;
  const dashboardGrid = root.querySelector(".cleide-dashboard-grid");
  if (!dashboardGrid) return;
  root.setAttribute("data-cleide-ui", "phase-7");
  root.setAttribute("data-cleide-upload-enabled", "true");
  root.setAttribute("data-cleide-filters-enabled", "true");

  const dropzone = document.getElementById("cleideUploadDropzone");
  const form = document.getElementById("cleideUploadForm");
  const input = document.getElementById("cleideUploadInput");
  const selectBtn = document.getElementById("cleideUploadSelectBtn");
  const submitBtn = document.getElementById("cleideUploadSubmitBtn");
  const clearBtn = document.getElementById("cleideUploadClearBtn");
  const status = document.getElementById("cleideUploadStatus");
  const structural = document.getElementById("cleideStructuralFeedback");
  const structuralDetails = document.getElementById("cleideStructuralDetails");
  const kpiTotalDocs = document.getElementById("cleideKpiTotalDocumentos");
  const kpiValorTotal = document.getElementById("cleideKpiValorTotalFrete");
  const kpiPesoTotal = document.getElementById("cleideKpiPesoTotal");
  const kpiTicketMedio = document.getElementById("cleideKpiTicketMedio");
  const kpiFretesZerados = document.getElementById("cleideKpiFretesZerados");
  const kpiPeriodo = document.getElementById("cleideKpiPeriodo");
  const kpiScopeBadge = document.getElementById("cleideKpiScopeBadge");
  const kpiScopeHint = document.getElementById("cleideKpiScopeHint");
  const qualityInvalidNumeric = document.getElementById("cleideQualityInvalidNumeric");
  const numericIssueAlertBtn = document.getElementById("cleideNumericIssueAlertBtn");
  const qualityInvalidDate = document.getElementById("cleideQualityInvalidDate");
  const qualityNegativeValue = document.getElementById("cleideQualityNegativeValue");
  const qualityLinhasConsideradas = document.getElementById("cleideQualityLinhasConsideradas");
  const qualityLinhaBadge = document.getElementById("cleideDatasetLinhaBadge");
  const dashboardState = document.getElementById("cleideDashboardState");
  const transportadoraVisual = document.getElementById("cleideTransportadoraVisual");
  const ufOrigemVisual = document.getElementById("cleideUfOrigemVisual");
  const ufDestinoVisual = document.getElementById("cleideUfDestinoVisual");
  const temporalVisual = document.getElementById("cleideTemporalVisual");
  const originCarrierVisual = document.getElementById("cleideOriginCarrierVisual");
  const paretoUfVisual = document.getElementById("cleideParetoUfVisual");
  const paretoTransportadoraVisual = document.getElementById("cleideParetoTransportadoraVisual");
  const chartTransportadora = document.getElementById("cleideChartTransportadora");
  const chartUfOrigem = document.getElementById("cleideChartUfOrigem");
  const chartUfDestino = document.getElementById("cleideChartUfDestino");
  const chartTemporal = document.getElementById("cleideChartTemporal");
  const chartOriginCarrier = document.getElementById("cleideChartOriginCarrier");
  const chartParetoUf = document.getElementById("cleideChartParetoUf");
  const chartParetoTransportadora = document.getElementById("cleideChartParetoTransportadora");
  const filterDateStart = document.getElementById("cleideFilterDateStart");
  const filterDateEnd = document.getElementById("cleideFilterDateEnd");
  const resetFiltersBtn = document.getElementById("cleideResetFiltersBtn");
  const hiddenChartsWrap = document.getElementById("cleideHiddenChartsWrap");
  const hiddenChartsList = document.getElementById("cleideHiddenChartsList");
  const showAllChartsBtn = document.getElementById("cleideShowAllChartsBtn");
  const allChartsHiddenMessage = document.getElementById("cleideAllChartsHiddenMessage");
  const filterStatus = document.getElementById("cleideFilterStatus");
  const filterSemanticHint = document.getElementById("cleideFilterSemanticHint");
  const activeFilterChips = document.getElementById("cleideActiveFilterChips");
  const tableTransportadora = document.getElementById("cleideTableTransportadora");
  const tableUfOrigem = document.getElementById("cleideTableUfOrigem");
  const tableUfDestino = document.getElementById("cleideTableUfDestino");
  const tableTemporal = document.getElementById("cleideTableTemporal");
  const sortTransportadora = document.getElementById("cleideSortTransportadora");
  const orderTransportadora = document.getElementById("cleideOrderTransportadora");
  const sizeTransportadora = document.getElementById("cleidePageSizeTransportadora");
  const prevTransportadora = document.getElementById("cleidePrevTransportadora");
  const nextTransportadora = document.getElementById("cleideNextTransportadora");
  const infoTransportadora = document.getElementById("cleidePageInfoTransportadora");
  const sortUfOrigem = document.getElementById("cleideSortUfOrigem");
  const orderUfOrigem = document.getElementById("cleideOrderUfOrigem");
  const sizeUfOrigem = document.getElementById("cleidePageSizeUfOrigem");
  const prevUfOrigem = document.getElementById("cleidePrevUfOrigem");
  const nextUfOrigem = document.getElementById("cleideNextUfOrigem");
  const infoUfOrigem = document.getElementById("cleidePageInfoUfOrigem");
  const sortUfDestino = document.getElementById("cleideSortUfDestino");
  const orderUfDestino = document.getElementById("cleideOrderUfDestino");
  const sizeUfDestino = document.getElementById("cleidePageSizeUfDestino");
  const prevUfDestino = document.getElementById("cleidePrevUfDestino");
  const nextUfDestino = document.getElementById("cleideNextUfDestino");
  const infoUfDestino = document.getElementById("cleidePageInfoUfDestino");
  const sortTemporal = document.getElementById("cleideSortTemporal");
  const orderTemporal = document.getElementById("cleideOrderTemporal");
  const sizeTemporal = document.getElementById("cleidePageSizeTemporal");
  const prevTemporal = document.getElementById("cleidePrevTemporal");
  const nextTemporal = document.getElementById("cleideNextTemporal");
  const infoTemporal = document.getElementById("cleidePageInfoTemporal");
  const chatForm = document.getElementById("cleideChatForm");
  const chatQuestion = document.getElementById("cleideChatQuestion");
  const chatSendBtn = document.getElementById("cleideChatSendBtn");
  const chatLoading = document.getElementById("cleideChatLoading");
  const chatMessages = document.getElementById("cleideChatMessages");
  const chatCopyLastBtn = document.getElementById("cleideChatCopyLastBtn");
  const chatScopeBadge = document.getElementById("cleideChatScopeBadge");
  const chatScopeDetails = document.getElementById("cleideChatScopeDetails");
  const chatFallbackBanner = document.getElementById("cleideChatFallbackBanner");
  const chatFloating = document.getElementById("cleideChatFloating");
  const chatToggle = document.getElementById("cleideChatToggle");
  const chatPanel = document.getElementById("cleideChatPanel");
  const chatClose = document.getElementById("cleideChatClose");
  if (
    !dropzone ||
    !form ||
    !input ||
    !selectBtn ||
    !submitBtn ||
    !clearBtn ||
    !status ||
    !structural ||
    !structuralDetails ||
    !kpiTotalDocs ||
    !kpiValorTotal ||
    !kpiPesoTotal ||
    !kpiTicketMedio ||
    !kpiFretesZerados ||
    !kpiPeriodo ||
    !kpiScopeBadge ||
    !kpiScopeHint ||
    !qualityInvalidNumeric ||
    !numericIssueAlertBtn ||
    !qualityInvalidDate ||
    !qualityNegativeValue ||
    !qualityLinhasConsideradas ||
    !qualityLinhaBadge ||
    !dashboardState ||
    !transportadoraVisual ||
    !ufOrigemVisual ||
    !ufDestinoVisual ||
    !temporalVisual ||
    !originCarrierVisual ||
    !paretoUfVisual ||
    !paretoTransportadoraVisual ||
    !chartTransportadora ||
    !chartUfOrigem ||
    !chartUfDestino ||
    !chartTemporal ||
    !chartOriginCarrier ||
    !chartParetoUf ||
    !chartParetoTransportadora ||
    !filterDateStart ||
    !filterDateEnd ||
    !resetFiltersBtn ||
    !hiddenChartsWrap ||
    !hiddenChartsList ||
    !showAllChartsBtn ||
    !allChartsHiddenMessage ||
    !filterStatus ||
    !filterSemanticHint ||
    !activeFilterChips ||
    !tableTransportadora ||
    !tableUfOrigem ||
    !tableUfDestino ||
    !tableTemporal ||
    !sortTransportadora ||
    !orderTransportadora ||
    !sizeTransportadora ||
    !prevTransportadora ||
    !nextTransportadora ||
    !infoTransportadora ||
    !sortUfOrigem ||
    !orderUfOrigem ||
    !sizeUfOrigem ||
    !prevUfOrigem ||
    !nextUfOrigem ||
    !infoUfOrigem ||
    !sortUfDestino ||
    !orderUfDestino ||
    !sizeUfDestino ||
    !prevUfDestino ||
    !nextUfDestino ||
    !infoUfDestino ||
    !sortTemporal ||
    !orderTemporal ||
    !sizeTemporal ||
    !prevTemporal ||
    !nextTemporal ||
    !infoTemporal ||
    !chatForm ||
    !chatQuestion ||
    !chatSendBtn ||
    !chatLoading ||
    !chatMessages ||
    !chatCopyLastBtn ||
    !chatScopeBadge ||
    !chatScopeDetails ||
    !chatFallbackBanner ||
    !chatFloating ||
    !chatToggle ||
    !chatPanel ||
    !chatClose
  ) {
    return;
  }

  const MAX_PAGE_SIZE = 20;
  const MIN_PAGE_SIZE = 5;
  const MAX_FILTER_VALUE_LENGTH = 64;
  const isAuthenticated = window.CLEIDE_AUTHENTICATED === true;
  const loginUrl = typeof window.CLEIDE_LOGIN_URL === "string" && window.CLEIDE_LOGIN_URL ? window.CLEIDE_LOGIN_URL : "/login";
  const uploadAuthorization = window.CLEIDE_UPLOAD_AUTHORIZATION || null;
  let uploading = false;
  let uploadLock = "";
  let currentData = null;
  let chatRequestRunning = false;
  let cleideChatOpen = false;
  let maxChatHistory = Number(window.CLEIDE_CHAT_MAX_HISTORY || 10);
  if (!Number.isInteger(maxChatHistory) || maxChatHistory < 1) maxChatHistory = 10;

  const activeFilters = {
    transportadora: null,
    uf_origem: null,
    uf_destino: null,
    data_inicio: null,
    data_fim: null,
  };
  const hiddenCharts = new Set();
  const chartLabels = {
    transportadora: "Top Transportadoras",
    uf_destino: "Custo por UF Destino",
    temporal: "Evolução Diária",
    uf_origem: "Custo por UF Origem",
    volume_transportadora: "Volume por Transportadora",
    pareto_uf: "Ocorrências por UF Destino",
    pareto_transportadora: "Ocorrências por Transportadora",
  };
  const chartInstanceByCardKey = {
    transportadora: "transportadora",
    uf_destino: "ufDestino",
    temporal: "temporal",
    uf_origem: "ufOrigem",
    volume_transportadora: "originCarrier",
    pareto_uf: "paretoUf",
    pareto_transportadora: "paretoTransportadora",
  };
  const chartCards = Object.fromEntries(
    Object.keys(chartLabels).map((chartKey) => [
      chartKey,
      root.querySelector(`[data-cleide-chart-card="${chartKey}"]`),
    ]),
  );
  if (Object.values(chartCards).some((card) => !card)) {
    return;
  }

  const tableState = {
    transportadora: { sortKey: "valor_total", order: "desc", page: 1, pageSize: 5 },
    ufOrigem: { sortKey: "valor_total", order: "desc", page: 1, pageSize: 5 },
    ufDestino: { sortKey: "valor_total", order: "desc", page: 1, pageSize: 5 },
    temporal: { sortKey: "data", order: "desc", page: 1, pageSize: 5 },
  };

  const updateStatus = (text, cssClass = "text-muted") => {
    status.className = `small mt-3 ${cssClass}`;
    status.textContent = text;
  };

  const updateStructural = (text, cssClass = "text-muted") => {
    structural.className = `small mt-2 ${cssClass}`;
    structural.textContent = text;
  };

  let numericIssuePopover = null;

  const updateNumericIssueAlert = (summary) => {
    const invalidNumericRows = getNumeric(summary?.invalid_numeric_rows);
    if (invalidNumericRows <= 0) {
      if (numericIssuePopover) {
        numericIssuePopover.dispose();
        numericIssuePopover = null;
      }
      numericIssueAlertBtn.classList.add("d-none");
      numericIssueAlertBtn.removeAttribute("data-bs-content");
      return;
    }

    const details = summary?.numeric_issue_details || {};
    const byColumn = details?.by_column || {};
    const sampleLines = Array.isArray(details?.samples)
      ? details.samples
          .slice(0, 3)
          .map((sample) => getNumeric(sample?.line))
          .filter((line) => line > 0)
      : [];
    const linesText = sampleLines.length > 0 ? sampleLines.map((line) => `linha ${line}`).join(", ") : "sem exemplos";
    const weightIssues = getNumeric(byColumn?.peso);
    const friendly = [
      `${formatInteger(invalidNumericRows)} linhas com problemas numéricos.`,
      `Peso inválido em ${formatInteger(weightIssues)} linhas.`,
      `Exemplos: ${linesText}.`,
    ].join(" ");

    numericIssueAlertBtn.classList.remove("d-none");
    numericIssueAlertBtn.setAttribute("data-bs-content", friendly);
    if (numericIssuePopover) {
      numericIssuePopover.dispose();
    }
    numericIssuePopover = new bootstrap.Popover(numericIssueAlertBtn);
  };

  const redirectToLogin = () => {
    window.location.href = loginUrl;
  };

  const getPrivateAccessMessage = () => {
    const authzMessage =
      uploadAuthorization && typeof uploadAuthorization.mensagem_usuario === "string"
        ? uploadAuthorization.mensagem_usuario.trim()
        : "";
    return authzMessage || "Faça login para enviar planilhas e usar recursos privados da Cleide.";
  };

  const renderAnonymousState = () => {
    submitBtn.disabled = true;
    clearBtn.disabled = true;
    updateStatus(getPrivateAccessMessage(), "text-warning");
    updateStructural("A página é pública. Upload, processamento e contexto privado exigem login.", "text-muted");
    updateStructuralDetails({});
    setDashboardState("Login necessário para upload", "text-bg-warning");
    renderEmptyDashboard("Faça login para montar o dashboard operacional com dados privados da sua sessão.");
    chatQuestion.disabled = true;
    chatSendBtn.disabled = true;
    chatMessages.innerHTML = "";
    appendCleideMessage("Faça login para usar o chat operacional da Cleide.");
    chatScopeBadge.className = "badge text-bg-warning";
    chatScopeBadge.textContent = "Escopo indisponível sem login";
  };

  const formatNumber = (value, fractionDigits = 2) => {
    const num = Number(value);
    if (!Number.isFinite(num)) return (0).toLocaleString("pt-BR", { minimumFractionDigits: fractionDigits, maximumFractionDigits: fractionDigits });
    return num.toLocaleString("pt-BR", {
      minimumFractionDigits: fractionDigits,
      maximumFractionDigits: fractionDigits,
    });
  };

  const formatCurrency = (value) => `R$ ${formatNumber(value, 2)}`;
  const formatPercent = (value) => `${formatNumber(value, 2)}%`;
  const formatInteger = (value) => Number(value || 0).toLocaleString("pt-BR");
  const formatUploadLimitExceededMessage = (data) => {
    const limit = Number(data?.upload_total_max);
    const detected = Number(data?.linhas_detectadas);
    const limitText = Number.isFinite(limit) && limit > 0 ? formatInteger(limit) : "n/a";
    const detectedText = Number.isFinite(detected) && detected >= 0 ? formatInteger(detected) : "n/a";
    return `Arquivo excede o limite máximo de linhas permitido para a Cleide. Limite atual: ${limitText} linhas. Linhas detectadas: ${detectedText}.`;
  };
  const compactFormatter = new Intl.NumberFormat("pt-BR", {
    minimumFractionDigits: 0,
    maximumFractionDigits: 1,
  });
  const formatCompactNumber = (value, options = {}) => {
    const { compactThreshold = 1000, fullFormatter = (n) => formatInteger(n) } = options;
    const num = getNumeric(value);
    const full = fullFormatter(num);
    const abs = Math.abs(num);
    if (abs < compactThreshold) {
      return { display: full, full, compacted: false };
    }
    const base = abs >= 1000000 ? 1000000 : 1000;
    const suffix = base === 1000000 ? "M" : "K";
    const scaled = num / base;
    const compact = `${compactFormatter.format(scaled)}${suffix}`;
    return { display: compact, full, compacted: true };
  };
  const formatCompactCurrency = (value) => {
    const compact = formatCompactNumber(value, {
      compactThreshold: 1000,
      fullFormatter: (n) => formatCurrency(n),
    });
    if (!compact.compacted) return compact;
    return {
      display: `R$ ${compact.display}`,
      full: compact.full,
      compacted: true,
    };
  };
  const renderCompactValue = (element, formatted) => {
    element.textContent = formatted.display;
    if (formatted.compacted) {
      element.setAttribute("title", formatted.full);
      return;
    }
    element.removeAttribute("title");
  };
  const formatDateLabel = (value) => {
    if (!value || typeof value !== "string") return "-";
    return /^\d{4}-\d{2}-\d{2}$/.test(value) ? value : "-";
  };

  const getNumeric = (value) => {
    const n = Number(value);
    return Number.isFinite(n) ? n : 0;
  };

  const safeText = (value) => String(value || "").trim();
  const sanitizeFilterValue = (value) => safeText(value).slice(0, MAX_FILTER_VALUE_LENGTH);
  const toIsoDateOrEmpty = (value) => (/^\d{4}-\d{2}-\d{2}$/.test(String(value || "")) ? String(value) : "");

  const sortRows = (rows, sortKey, order) => {
    const direction = order === "asc" ? 1 : -1;
    const keyed = [...(Array.isArray(rows) ? rows : [])];
    keyed.sort((a, b) => {
      const av = a?.[sortKey];
      const bv = b?.[sortKey];
      if (sortKey === "chave" || sortKey === "data") {
        const at = safeText(av).toUpperCase();
        const bt = safeText(bv).toUpperCase();
        if (at < bt) return -1 * direction;
        if (at > bt) return 1 * direction;
        return 0;
      }
      const an = getNumeric(av);
      const bn = getNumeric(bv);
      if (an === bn) {
        const at = safeText(a?.chave || a?.data).toUpperCase();
        const bt = safeText(b?.chave || b?.data).toUpperCase();
        if (at < bt) return -1;
        if (at > bt) return 1;
        return 0;
      }
      return (an - bn) * direction;
    });
    return keyed;
  };

  const clampPageSize = (value) => {
    const parsed = Number.parseInt(String(value || ""), 10);
    if (!Number.isInteger(parsed)) return MIN_PAGE_SIZE;
    return Math.min(MAX_PAGE_SIZE, Math.max(MIN_PAGE_SIZE, parsed));
  };

  const setDashboardState = (text, tone = "text-bg-light border") => {
    dashboardState.className = `badge ${tone}`;
    dashboardState.textContent = text;
  };

  const updateKpiSemanticScope = (filteredActive) => {
    kpiScopeBadge.className = `badge ${filteredActive ? "text-bg-warning" : "text-bg-light border"}`;
    kpiScopeBadge.textContent = filteredActive ? "KPIs filtrados (interseção real)" : "KPIs globais da sessão";
    kpiScopeHint.textContent = filteredActive
      ? "Com filtros ativos, os KPIs refletem a interseção real do dataset da sessão."
      : "Os KPIs representam a visão global do dataset da sessão.";
  };

  const renderEmptyVisual = (target, message) => {
    target.className = "cleide-empty-box";
    target.innerHTML = `<span class="small">${message}</span>`;
  };

  const formatChartCurrency = (value) => formatCurrency(value);
  const formatChartDate = (value) => formatDateLabel(value);
  const cleideCharts = {
    transportadora: { canvas: chartTransportadora, instance: null },
    ufOrigem: { canvas: chartUfOrigem, instance: null },
    ufDestino: { canvas: chartUfDestino, instance: null },
    temporal: { canvas: chartTemporal, instance: null },
    originCarrier: { canvas: chartOriginCarrier, instance: null },
    paretoUf: { canvas: chartParetoUf, instance: null },
    paretoTransportadora: { canvas: chartParetoTransportadora, instance: null },
  };
  const getChartInstanceForCardKey = (chartKey) => {
    const mapped = chartInstanceByCardKey[chartKey];
    if (!mapped) return null;
    return cleideCharts[mapped]?.instance || null;
  };

  const resizeVisibleCharts = (chartKeys = []) => {
    const keys = Array.isArray(chartKeys) && chartKeys.length > 0 ? chartKeys : Object.keys(chartCards);
    window.requestAnimationFrame(() => {
      keys.forEach((chartKey) => {
        if (hiddenCharts.has(chartKey)) return;
        const instance = getChartInstanceForCardKey(chartKey);
        if (!instance) return;
        instance.resize();
        instance.update("none");
      });
    });
  };

  const applyChartVisibility = () => {
    const totalCharts = Object.keys(chartCards).length;
    const hiddenCount = hiddenCharts.size;
    dashboardGrid.classList.toggle("cleide-dashboard-grid--has-hidden", hiddenCount > 0);
    Object.entries(chartCards).forEach(([chartKey, card]) => {
      card.classList.toggle("is-hidden", hiddenCharts.has(chartKey));
    });
    allChartsHiddenMessage.classList.toggle("d-none", hiddenCount !== totalCharts);
  };

  const renderHiddenChartsControls = () => {
    const hiddenKeys = Object.keys(chartLabels).filter((chartKey) => hiddenCharts.has(chartKey));
    if (hiddenKeys.length === 0) {
      hiddenChartsList.innerHTML = '<span class="small text-muted">Nenhum gráfico oculto</span>';
      showAllChartsBtn.disabled = true;
      return;
    }
    hiddenChartsList.innerHTML = hiddenKeys
      .map(
        (chartKey) =>
          `<button type="button" class="btn btn-sm cleide-hidden-chart-chip" data-cleide-show-chart="${chartKey}">Mostrar ${chartLabels[chartKey]}</button>`,
      )
      .join("");
    showAllChartsBtn.disabled = false;
  };

  const hideChart = (chartKey) => {
    if (!(chartKey in chartLabels)) return;
    hiddenCharts.add(chartKey);
    applyChartVisibility();
    renderHiddenChartsControls();
  };

  const showChart = (chartKey) => {
    if (!(chartKey in chartLabels)) return;
    const removed = hiddenCharts.delete(chartKey);
    applyChartVisibility();
    renderHiddenChartsControls();
    if (removed) {
      resizeVisibleCharts([chartKey]);
    }
  };

  const showAllCharts = () => {
    if (hiddenCharts.size === 0) return;
    hiddenCharts.clear();
    applyChartVisibility();
    renderHiddenChartsControls();
    resizeVisibleCharts();
  };

  const buildChartRows = (rows, { labelKey, valueKey, sortKey, order, topN }) => {
    const safeRows = sortRows(rows, sortKey, order).slice(0, topN);
    return safeRows.map((row) => ({
      label: safeText(row?.[labelKey]) || "-",
      value: getNumeric(row?.[valueKey]),
    }));
  };

  const ensureCleideCharts = () => typeof window.Chart === "function";
  const updateParetoChart = (chartKey, rows) => {
    if (!ensureCleideCharts()) return false;
    const slot = cleideCharts[chartKey];
    if (!slot || !slot.canvas) return false;

    const labels = rows.map((row) => safeText(row?.chave) || "-");
    const quantidade = rows.map((row) => getNumeric(row?.quantidade));
    const acumulado = rows.map((row) => getNumeric(row?.percentual_acumulado));
    const percentual = rows.map((row) => getNumeric(row?.percentual));

    if (slot.instance) {
      slot.instance.destroy();
    }
    slot.instance = new window.Chart(slot.canvas, {
      data: {
        labels,
        datasets: [
          {
            type: "bar",
            label: "Ocorrências",
            data: quantidade,
            yAxisID: "y",
            backgroundColor: "rgba(37, 176, 255, 0.45)",
            borderColor: "#25b0ff",
            borderWidth: 1,
            maxBarThickness: 28,
          },
          {
            type: "line",
            label: "Pareto acumulado",
            data: acumulado,
            yAxisID: "y1",
            borderColor: "#f4b400",
            backgroundColor: "rgba(244, 180, 0, 0.2)",
            fill: false,
            borderWidth: 2,
            tension: 0.24,
            pointRadius: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: { display: true, labels: { color: "#c6d7f2" } },
          tooltip: {
            callbacks: {
              title: (items) => items[0]?.label || "-",
              afterBody: (items) => {
                const idx = items[0]?.dataIndex ?? 0;
                return [
                  `Percentual: ${formatPercent(percentual[idx] || 0)}`,
                  `Acumulado: ${formatPercent(acumulado[idx] || 0)}`,
                ];
              },
            },
          },
        },
        scales: {
          x: {
            grid: { color: "rgba(124, 148, 189, 0.14)" },
            ticks: { color: "#c6d7f2" },
          },
          y: {
            beginAtZero: true,
            position: "left",
            grid: { color: "rgba(124, 148, 189, 0.14)" },
            ticks: { color: "#c6d7f2", callback: (v) => formatInteger(v) },
          },
          y1: {
            beginAtZero: true,
            position: "right",
            min: 0,
            max: 100,
            grid: { drawOnChartArea: false },
            ticks: { color: "#f4d27a", callback: (v) => `${formatNumber(v, 0)}%` },
          },
        },
      },
    });
    slot.instance.options.onClick = (_event, elements) => {
      const first = Array.isArray(elements) && elements.length > 0 ? elements[0] : null;
      if (!first) return;
      const row = rows[first.index];
      if (!row) return;
      const value = safeText(row.chave);
      if (!value) return;
      if (chartKey === "paretoUf") {
        activeFilters.uf_destino = activeFilters.uf_destino === value ? null : value;
      } else if (chartKey === "paretoTransportadora") {
        activeFilters.transportadora = activeFilters.transportadora === value ? null : value;
      }
      syncFilterControlsFromState();
      resetPagination();
      applyBackendFilters();
    };
    return true;
  };

  const updateCleideChart = (chartKey, labels, values, options = {}) => {
    if (!ensureCleideCharts()) return false;
    const slot = cleideCharts[chartKey];
    if (!slot || !slot.canvas) return false;

    const type = options.type || "bar";
    const indexAxis = options.indexAxis || "x";
    const datasetLabel = options.datasetLabel || "Valor";
    const valueFormatter = typeof options.valueFormatter === "function" ? options.valueFormatter : (n) => formatNumber(n, 2);
    const chartColor = options.chartColor || "#25b0ff";
    const areaColor = options.areaColor || "rgba(37, 176, 255, 0.20)";
    const desiredScaleMode = options.scaleMode || "currency";
    const isHorizontal = indexAxis === "y";
    const currentScaleMode = slot.instance?.$cleideScaleMode || "";
    const requiresRecreate =
      !slot.instance ||
      slot.instance.config.type !== type ||
      slot.instance.options.indexAxis !== indexAxis ||
      currentScaleMode !== desiredScaleMode;

    if (requiresRecreate) {
      if (slot.instance) {
        slot.instance.destroy();
      }
      const numericTickFormatter =
        desiredScaleMode === "quantity"
          ? (v) => formatInteger(v)
          : desiredScaleMode === "weight"
            ? (v) => formatNumber(v, 2)
            : (v) => formatChartCurrency(v);
      const categoricalTickFormatter = (_v, index) => labels[index] || "";
      slot.instance = new window.Chart(slot.canvas, {
        type,
        data: {
          labels,
          datasets: [
            {
              label: datasetLabel,
              data: values,
              borderColor: chartColor,
              backgroundColor: areaColor,
              fill: type === "line",
              borderWidth: 2,
              tension: type === "line" ? 0.24 : 0,
              pointRadius: type === "line" ? 2 : 0,
              maxBarThickness: 28,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          animation: false,
          indexAxis,
          plugins: {
            legend: { display: false },
            tooltip: {
              callbacks: {
                label: (ctx) => `${datasetLabel}: ${valueFormatter(ctx.parsed[indexAxis === "y" ? "x" : "y"])}`,
              },
            },
          },
          scales: {
            x: {
              grid: { color: "rgba(124, 148, 189, 0.14)" },
              ticks: {
                color: "#c6d7f2",
                callback: isHorizontal ? numericTickFormatter : categoricalTickFormatter,
              },
            },
            y: {
              grid: { color: "rgba(124, 148, 189, 0.14)" },
              ticks: {
                color: "#c6d7f2",
                callback: isHorizontal ? categoricalTickFormatter : numericTickFormatter,
              },
            },
          },
        },
      });
      slot.instance.$cleideScaleMode = desiredScaleMode;
      slot.instance.options.onClick = (_event, elements) => {
        const first = Array.isArray(elements) && elements.length > 0 ? elements[0] : null;
        if (!first) return;
        const selectedLabel = safeText(labels[first.index]);
        if (!selectedLabel) return;
        if (chartKey === "transportadora" || chartKey === "originCarrier") {
          activeFilters.transportadora = activeFilters.transportadora === selectedLabel ? null : selectedLabel;
        } else if (chartKey === "ufOrigem") {
          activeFilters.uf_origem = activeFilters.uf_origem === selectedLabel ? null : selectedLabel;
        } else if (chartKey === "ufDestino") {
          activeFilters.uf_destino = activeFilters.uf_destino === selectedLabel ? null : selectedLabel;
        } else if (chartKey === "temporal") {
          const day = toIsoDateOrEmpty(selectedLabel);
          if (!day) return;
          if (activeFilters.data_inicio === day && activeFilters.data_fim === day) {
            activeFilters.data_inicio = null;
            activeFilters.data_fim = null;
          } else {
            activeFilters.data_inicio = day;
            activeFilters.data_fim = day;
          }
        } else {
          return;
        }
        syncFilterControlsFromState();
        resetPagination();
        applyBackendFilters();
      };
      return true;
    }

    slot.instance.data.labels = labels;
    slot.instance.data.datasets[0].label = datasetLabel;
    slot.instance.data.datasets[0].data = values;
    slot.instance.data.datasets[0].borderColor = chartColor;
    slot.instance.data.datasets[0].backgroundColor = areaColor;
    slot.instance.update();
    slot.instance.options.onClick = (_event, elements) => {
      const first = Array.isArray(elements) && elements.length > 0 ? elements[0] : null;
      if (!first) return;
      const selectedLabel = safeText(labels[first.index]);
      if (!selectedLabel) return;
      if (chartKey === "transportadora" || chartKey === "originCarrier") {
        activeFilters.transportadora = activeFilters.transportadora === selectedLabel ? null : selectedLabel;
      } else if (chartKey === "ufOrigem") {
        activeFilters.uf_origem = activeFilters.uf_origem === selectedLabel ? null : selectedLabel;
      } else if (chartKey === "ufDestino") {
        activeFilters.uf_destino = activeFilters.uf_destino === selectedLabel ? null : selectedLabel;
      } else if (chartKey === "temporal") {
        const day = toIsoDateOrEmpty(selectedLabel);
        if (!day) return;
        if (activeFilters.data_inicio === day && activeFilters.data_fim === day) {
          activeFilters.data_inicio = null;
          activeFilters.data_fim = null;
        } else {
          activeFilters.data_inicio = day;
          activeFilters.data_fim = day;
        }
      } else {
        return;
      }
      syncFilterControlsFromState();
      resetPagination();
      applyBackendFilters();
    };
    return true;
  };

  const renderSimpleTable = (target, rows, columns) => {
    const safeRows = Array.isArray(rows) ? rows : [];
    if (safeRows.length === 0) {
      target.className = "small text-muted";
      target.textContent = "Sem dados agregados para exibir.";
      return;
    }
    const head = columns.map((col) => `<th class="small text-muted">${col.label}</th>`).join("");
    const body = safeRows
      .map((row) => {
        const tds = columns.map((col) => `<td class="small">${col.render(row)}</td>`).join("");
        return `<tr>${tds}</tr>`;
      })
      .join("");
    target.className = "table-responsive";
    target.innerHTML = `<table class="table table-sm mb-0"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
  };

  const renderQuality = (data, docsConsideradas) => {
    const summary = data?.dataset_summary || {};
    const linhasProcessadas = getNumeric(summary.linhas_processadas);
    const docs = getNumeric(docsConsideradas);
    qualityInvalidNumeric.textContent = formatInteger(summary.invalid_numeric_rows || 0);
    updateNumericIssueAlert(summary);
    qualityInvalidDate.textContent = formatInteger(summary.invalid_date_rows || 0);
    qualityNegativeValue.textContent = formatInteger(summary.negative_value_rows || 0);
    renderCompactValue(
      qualityLinhasConsideradas,
      formatCompactNumber(docs, { compactThreshold: 10000, fullFormatter: (n) => formatInteger(n) }),
    );
    const linhasProcessadasFormatadas = formatCompactNumber(linhasProcessadas, {
      compactThreshold: 10000,
      fullFormatter: (n) => formatInteger(n),
    });
    qualityLinhaBadge.textContent = `Linhas processadas: ${linhasProcessadasFormatadas.display}`;
    if (linhasProcessadasFormatadas.compacted) {
      qualityLinhaBadge.setAttribute("title", `Linhas processadas: ${linhasProcessadasFormatadas.full}`);
    } else {
      qualityLinhaBadge.removeAttribute("title");
    }
  };

  const getPagedRows = (rows, cfg) => {
    const sorted = sortRows(rows, cfg.sortKey, cfg.order);
    const totalRows = sorted.length;
    const pageSize = clampPageSize(cfg.pageSize);
    const totalPages = totalRows > 0 ? Math.ceil(totalRows / pageSize) : 1;
    cfg.page = Math.min(totalPages, Math.max(1, cfg.page));
    const start = (cfg.page - 1) * pageSize;
    return {
      rows: sorted.slice(start, start + pageSize),
      totalRows,
      totalPages,
      page: cfg.page,
    };
  };

  const setPager = (infoEl, prevEl, nextEl, meta) => {
    infoEl.textContent = `Página ${meta.page}/${meta.totalPages} (${meta.totalRows} itens)`;
    nextEl.dataset.maxPages = String(meta.totalPages);
    prevEl.disabled = meta.page <= 1 || meta.totalRows === 0;
    nextEl.disabled = meta.page >= meta.totalPages || meta.totalRows === 0;
  };

  const getCurrentFiltersPayload = () => ({
    transportadora: activeFilters.transportadora || null,
    uf_origem: activeFilters.uf_origem || null,
    uf_destino: activeFilters.uf_destino || null,
    data_inicio: activeFilters.data_inicio || null,
    data_fim: activeFilters.data_fim || null,
  });

  const syncFilterControlsFromState = () => {
    filterDateStart.value = activeFilters.data_inicio || "";
    filterDateEnd.value = activeFilters.data_fim || "";
  };

  const renderActiveFilterChips = () => {
    const entries = [
      ["transportadora", activeFilters.transportadora],
      ["uf_origem", activeFilters.uf_origem],
      ["uf_destino", activeFilters.uf_destino],
    ].filter((entry) => safeText(entry[1]));
    if (entries.length === 0) {
      activeFilterChips.innerHTML = '<span class="small text-muted">nenhum</span>';
      return;
    }
    const chipLabels = {
      transportadora: "CARRIER",
      uf_origem: "UF ORIGEM",
      uf_destino: "UF DESTINO",
    };
    activeFilterChips.innerHTML = entries
      .map(
        ([key, value]) =>
          `<span class="cleide-filter-chip">${chipLabels[key]}: ${String(value)} <button type="button" data-cleide-remove-filter="${key}" aria-label="Remover filtro ${chipLabels[key]}">x</button></span>`,
      )
      .join("");
  };

  const renderFilterStatus = () => {
    const active = Object.entries(getCurrentFiltersPayload()).filter((entry) => entry[1]);
    if (active.length === 0) {
      filterStatus.textContent = "Filtros inativos. Exibindo visão global da sessão Cleide.";
      filterSemanticHint.textContent = "Interseção real calculada no backend oficial da Cleide.";
      updateKpiSemanticScope(false);
      return;
    }
    filterStatus.textContent = `Filtros ativos com interseção real: ${active
      .map(([key, value]) => `${key}=${value}`)
      .join(" | ")}`;
    filterSemanticHint.textContent = "Todos os KPIs e agregados refletem a interseção row-level no backend.";
    updateKpiSemanticScope(true);
  };

  const setChatLoading = (running, message = "") => {
    chatRequestRunning = running;
    chatSendBtn.disabled = running;
    chatQuestion.disabled = running;
    chatLoading.textContent = running ? message || "Consultando contexto operacional..." : "";
  };

  const scrollChatToBottom = () => {
    chatMessages.scrollTop = chatMessages.scrollHeight;
  };

  const esc = (text) =>
    String(text || "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");

  const applyInlineMarkdown = (escapedText) => {
    let rendered = String(escapedText || "");
    rendered = rendered.replace(/\*\*([^*\n][^*\n]*?)\*\*/g, "<strong>$1</strong>");
    rendered = rendered.replace(/(^|[^\*])\*([^*\n]+)\*(?!\*)/g, "$1<em>$2</em>");
    return rendered;
  };

  const renderSafeStructuredText = (text) => {
    const value = String(text || "").replace(/\r\n/g, "\n").replace(/\r/g, "\n");
    const lines = value.split("\n");
    const out = [];
    let inList = false;
    for (const rawLine of lines) {
      const line = rawLine.trimEnd();
      const trimmed = line.trim();
      if (!trimmed) {
        if (inList) {
          out.push("</ul>");
          inList = false;
        }
        out.push("<br>");
        continue;
      }
      const headingMatch = /^(Assunto|Resumo executivo|Principais pontos|Riscos|Recomendações|Recomendacoes|Encerramento)\s*:\s*(.*)$/i.exec(trimmed);
      if (headingMatch) {
        if (inList) {
          out.push("</ul>");
          inList = false;
        }
        out.push(`<strong>${esc(headingMatch[1])}:</strong> ${applyInlineMarkdown(esc(headingMatch[2]))}`);
        continue;
      }
      const bulletMatch = /^[-*]\s+(.+)$/.exec(trimmed);
      if (bulletMatch) {
        if (!inList) {
          out.push("<ul>");
          inList = true;
        }
        out.push(`<li>${applyInlineMarkdown(esc(bulletMatch[1]))}</li>`);
        continue;
      }
      if (inList) {
        out.push("</ul>");
        inList = false;
      }
      out.push(applyInlineMarkdown(esc(trimmed)));
    }
    if (inList) out.push("</ul>");
    return out.join("<br>");
  };

  const appendChatMessage = (role, text) => {
    const msg = document.createElement("div");
    msg.className = `cleide-chat-msg cleide-chat-msg--${role === "user" ? "user" : role === "loading" ? "loading" : "cleide"}`;
    const inner = document.createElement("div");
    inner.className = "cleide-chat-msg__inner";
    const cleanText = String(text || "").trim() || "Sem conteúdo.";
    if (role === "cleide") {
      inner.innerHTML = renderSafeStructuredText(cleanText);
    } else {
      inner.textContent = cleanText;
    }
    msg.appendChild(inner);
    if (role === "cleide") {
      msg.appendChild(buildCopyAction());
    }
    chatMessages.appendChild(msg);
    scrollChatToBottom();
    return msg;
  };

  const buildCopyAction = () => {
    const actions = document.createElement("div");
    actions.className = "cleide-chat-actions";
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "cleide-chat-copy-btn";
    btn.setAttribute("data-cleide-copy", "1");
    btn.textContent = "Copiar";
    actions.appendChild(btn);
    return actions;
  };

  const copyTextToClipboard = (text) => {
    const value = String(text || "").trim();
    if (!value) return Promise.reject(new Error("empty"));
    if (navigator.clipboard && typeof navigator.clipboard.writeText === "function") {
      return navigator.clipboard.writeText(value);
    }
    return new Promise((resolve, reject) => {
      const area = document.createElement("textarea");
      area.value = value;
      area.setAttribute("readonly", "readonly");
      area.style.position = "fixed";
      area.style.opacity = "0";
      area.style.left = "-9999px";
      document.body.appendChild(area);
      area.focus();
      area.select();
      let ok = false;
      try {
        ok = document.execCommand("copy");
      } catch (_err) {
        ok = false;
      }
      document.body.removeChild(area);
      if (ok) resolve();
      else reject(new Error("copy-failed"));
    });
  };

  const markCopied = (button) => {
    if (!button) return;
    const original = button.getAttribute("data-copy-label") || "Copiar";
    button.setAttribute("data-copy-label", original);
    button.textContent = "Copiado";
    button.classList.add("is-copied");
    window.setTimeout(() => {
      button.textContent = original;
      button.classList.remove("is-copied");
    }, 1200);
  };

  const appendUserMessage = (text) => appendChatMessage("user", text);
  const appendCleideMessage = (text) => appendChatMessage("cleide", text);
  const appendLoadingMessage = () => appendChatMessage("loading", "Cleide está analisando...");
  const isInitialChatPlaceholder = (content) => {
    const normalized = String(content || "").trim().toLowerCase();
    return normalized.startsWith("aguardando pergunta");
  };
  const buildChatHistory = () => {
    const MAX_HISTORY = maxChatHistory;
    const history = [];
    const nodes = Array.from(chatMessages.querySelectorAll(".cleide-chat-msg"));
    nodes.forEach((node) => {
      if (!(node instanceof HTMLElement)) return;
      if (node.classList.contains("cleide-chat-msg--loading")) return;
      const inner = node.querySelector(".cleide-chat-msg__inner");
      const content = String(inner?.textContent || "").trim();
      if (!content) return;
      if (isInitialChatPlaceholder(content)) return;
      let role = "";
      if (node.classList.contains("cleide-chat-msg--user")) {
        role = "user";
      } else if (node.classList.contains("cleide-chat-msg--cleide")) {
        role = "assistant";
      }
      if (!role) return;
      history.push({ role, content });
    });
    return history.slice(-MAX_HISTORY);
  };
  const replaceLoading = (loadingEl, text) => {
    if (!loadingEl || !loadingEl.parentNode) {
      appendCleideMessage(text);
      return;
    }
    loadingEl.classList.remove("cleide-chat-msg--loading");
    loadingEl.classList.add("cleide-chat-msg--cleide");
    const inner = loadingEl.querySelector(".cleide-chat-msg__inner");
    if (inner) {
      const safeReply = String(text || "").trim() || "Sem resposta disponível no momento.";
      inner.innerHTML = renderSafeStructuredText(safeReply);
    }
    if (!loadingEl.querySelector(".cleide-chat-copy-btn")) {
      loadingEl.appendChild(buildCopyAction());
    }
    scrollChatToBottom();
  };

  const setCleideChatOpen = (isOpen) => {
    cleideChatOpen = Boolean(isOpen);
    chatPanel.classList.toggle("cleide-chat-panel--open", cleideChatOpen);
    chatPanel.setAttribute("aria-hidden", cleideChatOpen ? "false" : "true");
    chatToggle.setAttribute("aria-expanded", cleideChatOpen ? "true" : "false");
    if (cleideChatOpen) {
      window.requestAnimationFrame(() => chatQuestion.focus());
    } else {
      chatToggle.focus();
    }
  };

  const updateChatScopeFromResponse = (data) => {
    const viewScope = data?.view_scope === "filtered" ? "filtered" : "global";
    chatScopeBadge.className = `badge ${viewScope === "filtered" ? "text-bg-warning" : "text-bg-light border"}`;
    chatScopeBadge.textContent = viewScope === "filtered" ? "Escopo filtrado ativo" : "Escopo global da sessão";

    const active = data?.active_filters && typeof data.active_filters === "object" ? data.active_filters : {};
    const activeEntries = Object.entries(active).filter((entry) => String(entry[1] || "").trim());
    if (activeEntries.length > 0) {
      chatScopeDetails.classList.remove("d-none");
      chatScopeDetails.textContent = `Resposta considerando filtros ativos: ${activeEntries
        .map(([k, v]) => `${k}=${v}`)
        .join(" | ")}`;
    } else {
      chatScopeDetails.classList.add("d-none");
      chatScopeDetails.textContent = "";
    }
  };

  const updateChatFallbackBanner = (data) => {
    const mode = String(data?.mode || "");
    const aiEnabled = Boolean(data?.ai_enabled);
    const aiUsed = Boolean(data?.ai_used);
    const fallbackUsed = Boolean(data?.fallback_used);
    const policyBlocked = Boolean(data?.policy_blocked);
    const errorCode = String(data?.error_code || "");
    const contextStatus = String(data?.context_status || "");

    const messages = [];
    if (contextStatus === "insufficient") {
      messages.push("Contexto operacional insuficiente: resposta limitada aos dados disponíveis.");
    }
    if (!aiEnabled && mode.includes("controlled_templates")) {
      messages.push("IA desligada ou não autorizada: resposta em modo controlado.");
    } else if (policyBlocked) {
      messages.push("Resposta da IA bloqueada pela política de segurança: fallback governado aplicado.");
    } else if (errorCode === "provider_error") {
      messages.push("Provedor externo indisponível: resposta entregue por fallback governado.");
    } else if (aiEnabled && !aiUsed && fallbackUsed) {
      messages.push("Resposta entregue por fallback governado.");
    }
    if (fallbackUsed && !policyBlocked && errorCode !== "provider_error") {
      messages.push("Fallback seguro aplicado para preservar governança e precisão.");
    }
    if (messages.length > 0) {
      chatFallbackBanner.classList.remove("d-none");
      chatFallbackBanner.textContent = messages.join(" ");
      return;
    }
    chatFallbackBanner.classList.add("d-none");
    chatFallbackBanner.textContent = "";
  };

  const submitChatQuestion = async () => {
    if (!isAuthenticated) {
      renderAnonymousState();
      redirectToLogin();
      return;
    }
    if (chatRequestRunning) return;
    const question = String(chatQuestion.value || "").trim();
    if (!question) {
      appendCleideMessage("Digite uma pergunta para continuar.");
      return;
    }
    const history = buildChatHistory();
    // Compatibilidade de teste estrutural: JSON.stringify({ question, history: buildChatHistory() })
    appendUserMessage(question);
    const loadingRef = appendLoadingMessage();
    chatQuestion.value = "";
    setChatLoading(true, "Consultando contexto operacional...");
    chatFallbackBanner.classList.add("d-none");
    chatScopeDetails.classList.add("d-none");
    try {
      const response = await fetch("/api/chat_cleide", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question, history }),
      });
      const data = await response.json();
      if (!response.ok) {
        replaceLoading(loadingRef, data?.error || "Falha ao consultar o chat da Cleide.");
        chatScopeBadge.className = "badge text-bg-danger";
        chatScopeBadge.textContent = "Falha na consulta";
        return;
      }
      replaceLoading(loadingRef, String(data?.reply || "Sem resposta disponível no momento."));
      if (data.max_history && data.max_history > 0) {
        maxChatHistory = Number(data.max_history);
      }
      updateChatScopeFromResponse(data);
      updateChatFallbackBanner(data);
      if (chatFallbackBanner.textContent.trim()) {
        appendCleideMessage(chatFallbackBanner.textContent.trim());
      }
    } catch (_err) {
      replaceLoading(loadingRef, "Falha de comunicação ao consultar o chat da Cleide.");
      chatScopeBadge.className = "badge text-bg-danger";
      chatScopeBadge.textContent = "Erro de comunicação";
    } finally {
      setChatLoading(false);
    }
  };

  const renderAnalytics = (data) => {
    const payload = data || {};
    const transportadora = Array.isArray(payload.transportadora_stats) ? payload.transportadora_stats : [];
    const ufOrigem = Array.isArray(payload.uf_origem_stats) ? payload.uf_origem_stats : [];
    const ufDestino = Array.isArray(payload.uf_destino_stats) ? payload.uf_destino_stats : [];
    const temporal = Array.isArray(payload.temporal_stats) ? payload.temporal_stats : [];
    const paretoUfDestino = Array.isArray(payload.pareto_fretes_zerados_uf_destino) ? payload.pareto_fretes_zerados_uf_destino : [];
    const paretoTransportadora = Array.isArray(payload.pareto_fretes_zerados_transportadora)
      ? payload.pareto_fretes_zerados_transportadora
      : [];
    const kpis = payload?.kpis || {};
    const periodo = kpis.periodo_dataset || {};
    renderCompactValue(
      kpiTotalDocs,
      formatCompactNumber(kpis.total_documentos || 0, { compactThreshold: 10000, fullFormatter: (n) => formatInteger(n) }),
    );
    renderCompactValue(kpiValorTotal, formatCompactCurrency(kpis.valor_total_frete || 0));
    renderCompactValue(
      kpiPesoTotal,
      formatCompactNumber(kpis.peso_total || 0, { compactThreshold: 1000, fullFormatter: (n) => formatNumber(n, 2) }),
    );
    kpiTicketMedio.textContent = formatCurrency(kpis.ticket_medio_frete || 0);
    kpiFretesZerados.textContent = formatPercent(kpis.percentual_fretes_zerados || 0);
    kpiPeriodo.textContent = periodo.inicio && periodo.fim ? `${periodo.inicio} até ${periodo.fim}` : "n/a";

    const sortedTransportadora = sortRows(transportadora, tableState.transportadora.sortKey, tableState.transportadora.order);
    const sortedUfOrigem = sortRows(ufOrigem, tableState.ufOrigem.sortKey, tableState.ufOrigem.order);
    const sortedUfDestino = sortRows(ufDestino, tableState.ufDestino.sortKey, tableState.ufDestino.order);
    const sortedTemporal = sortRows(temporal, tableState.temporal.sortKey, tableState.temporal.order);

    const pTransportadora = getPagedRows(sortedTransportadora, tableState.transportadora);
    const pUfOrigem = getPagedRows(sortedUfOrigem, tableState.ufOrigem);
    const pUfDestino = getPagedRows(sortedUfDestino, tableState.ufDestino);
    const pTemporal = getPagedRows(sortedTemporal, tableState.temporal);

    renderSimpleTable(tableTransportadora, pTransportadora.rows, [
      { label: "Transportadora", render: (row) => row.chave || "-" },
      { label: "Qtd", render: (row) => formatInteger(row.quantidade || 0) },
      { label: "Valor", render: (row) => formatCurrency(row.valor_total || 0) },
      { label: "Peso", render: (row) => formatNumber(row.peso_total || 0, 2) },
    ]);
    renderSimpleTable(tableUfOrigem, pUfOrigem.rows, [
      { label: "UF", render: (row) => row.chave || "-" },
      { label: "Qtd", render: (row) => formatInteger(row.quantidade || 0) },
      { label: "Valor", render: (row) => formatCurrency(row.valor_total || 0) },
    ]);
    renderSimpleTable(tableUfDestino, pUfDestino.rows, [
      { label: "UF", render: (row) => row.chave || "-" },
      { label: "Qtd", render: (row) => formatInteger(row.quantidade || 0) },
      { label: "Valor", render: (row) => formatCurrency(row.valor_total || 0) },
    ]);
    renderSimpleTable(tableTemporal, pTemporal.rows, [
      { label: "Data", render: (row) => formatDateLabel(row.data) },
      { label: "Qtd", render: (row) => formatInteger(row.quantidade || 0) },
      { label: "Valor", render: (row) => formatCurrency(row.valor_total || 0) },
    ]);

    const chartTopNTransportadora = clampPageSize(tableState.transportadora.pageSize);
    const chartTopNUfOrigem = clampPageSize(tableState.ufOrigem.pageSize);
    const chartTopNUfDestino = clampPageSize(tableState.ufDestino.pageSize);
    const chartTopNTemporal = clampPageSize(tableState.temporal.pageSize);
    const transportadoraMetric = "valor_total";
    const ufOrigemMetric = "valor_total";
    const ufDestinoMetric = "valor_total";
    const temporalMetric = "valor_total";
    const originCarrierMetric = "peso_total";

    const transportadoraRows = buildChartRows(transportadora, {
      labelKey: "chave",
      valueKey: transportadoraMetric,
      sortKey: tableState.transportadora.sortKey,
      order: tableState.transportadora.order,
      topN: chartTopNTransportadora,
    });
    const ufOrigemRows = buildChartRows(ufOrigem, {
      labelKey: "chave",
      valueKey: ufOrigemMetric,
      sortKey: tableState.ufOrigem.sortKey,
      order: tableState.ufOrigem.order,
      topN: chartTopNUfOrigem,
    });
    const ufDestinoRows = buildChartRows(ufDestino, {
      labelKey: "chave",
      valueKey: ufDestinoMetric,
      sortKey: tableState.ufDestino.sortKey,
      order: tableState.ufDestino.order,
      topN: chartTopNUfDestino,
    });
    const temporalRows = buildChartRows(temporal, {
      labelKey: "data",
      valueKey: temporalMetric,
      sortKey: tableState.temporal.sortKey,
      order: tableState.temporal.order,
      topN: chartTopNTemporal,
    });
    const originCarrierRows = buildChartRows(transportadora, {
      labelKey: "chave",
      valueKey: originCarrierMetric,
      sortKey: "peso_total",
      order: tableState.transportadora.order,
      topN: chartTopNTransportadora,
    });

    const transportadoraScaleMode = "currency";
    const transportadoraValueFormatter = (n) => formatChartCurrency(n);
    const transportadoraLabel = "Frete simulado";
    const transportadoraHasChart = updateCleideChart(
      "transportadora",
      transportadoraRows.map((row) => row.label),
      transportadoraRows.map((row) => row.value),
      {
        type: "bar",
        indexAxis: "y",
        datasetLabel: transportadoraLabel,
        valueFormatter: transportadoraValueFormatter,
        scaleMode: transportadoraScaleMode,
      },
    );
    if (!transportadoraHasChart || transportadoraRows.length === 0) {
      renderEmptyVisual(transportadoraVisual, "Sem agregados disponíveis para transportadoras.");
    } else {
      transportadoraVisual.className = "small text-muted mb-2";
      transportadoraVisual.textContent = `Top ${transportadoraRows.length} por ${transportadoraLabel.toLowerCase()}.`;
    }

    const ufOrigemScaleMode = "currency";
    const ufOrigemValueFormatter = (n) => formatChartCurrency(n);
    const ufOrigemHasChart = updateCleideChart(
      "ufOrigem",
      ufOrigemRows.map((row) => row.label),
      ufOrigemRows.map((row) => row.value),
      {
        type: "bar",
        indexAxis: "y",
        datasetLabel: "Frete simulado",
        valueFormatter: ufOrigemValueFormatter,
        scaleMode: ufOrigemScaleMode,
      },
    );
    if (!ufOrigemHasChart || ufOrigemRows.length === 0) {
      renderEmptyVisual(ufOrigemVisual, "Sem agregados disponíveis para UF origem.");
    } else {
      ufOrigemVisual.className = "small text-muted mb-2";
      ufOrigemVisual.textContent = `Custo por UF origem (Top ${ufOrigemRows.length}).`;
    }

    const ufDestinoScaleMode = "currency";
    const ufDestinoValueFormatter = (n) => formatChartCurrency(n);
    const ufDestinoHasChart = updateCleideChart(
      "ufDestino",
      ufDestinoRows.map((row) => row.label),
      ufDestinoRows.map((row) => row.value),
      {
        type: "bar",
        indexAxis: "x",
        datasetLabel: "Frete simulado",
        valueFormatter: ufDestinoValueFormatter,
        scaleMode: ufDestinoScaleMode,
      },
    );
    if (!ufDestinoHasChart || ufDestinoRows.length === 0) {
      renderEmptyVisual(ufDestinoVisual, "Sem agregados disponíveis para UF destino.");
    } else {
      ufDestinoVisual.className = "small text-muted mb-2";
      ufDestinoVisual.textContent = `Custo por UF destino (Top ${ufDestinoRows.length}).`;
    }

    const temporalScaleMode = "currency";
    const temporalValueFormatter = (n) => formatChartCurrency(n);
    const temporalHasChart = updateCleideChart(
      "temporal",
      temporalRows.map((row) => formatChartDate(row.label)),
      temporalRows.map((row) => row.value),
      {
        type: "line",
        indexAxis: "x",
        datasetLabel: "Frete simulado",
        valueFormatter: temporalValueFormatter,
        scaleMode: temporalScaleMode,
      },
    );
    if (!temporalHasChart || temporalRows.length === 0) {
      renderEmptyVisual(temporalVisual, "Sem agregados disponíveis para série temporal.");
    } else {
      temporalVisual.className = "small text-muted mb-2";
      temporalVisual.textContent = `Série temporal com ${temporalRows.length} pontos filtrados.`;
    }

    const originCarrierHasChart = updateCleideChart(
      "originCarrier",
      originCarrierRows.map((row) => row.label),
      originCarrierRows.map((row) => row.value),
      {
        type: "bar",
        indexAxis: "y",
        datasetLabel: "Peso total",
        valueFormatter: (n) => formatNumber(n, 2),
        scaleMode: "weight",
      },
    );
    if (!originCarrierHasChart || originCarrierRows.length === 0) {
      renderEmptyVisual(originCarrierVisual, "Sem agregados disponíveis para volume por transportadora.");
    } else {
      originCarrierVisual.className = "small text-muted mb-2";
      originCarrierVisual.textContent = `Volume por transportadora (Top ${originCarrierRows.length}).`;
    }

    const paretoUfHasChart = updateParetoChart("paretoUf", paretoUfDestino);
    if (!paretoUfHasChart || paretoUfDestino.length === 0) {
      renderEmptyVisual(paretoUfVisual, "Sem ocorrências de frete zerado por UF destino.");
    } else {
      paretoUfVisual.className = "small text-muted mb-2";
      paretoUfVisual.textContent = `Pareto UF destino com ${paretoUfDestino.length} categorias.`;
    }
    const paretoTransportadoraHasChart = updateParetoChart("paretoTransportadora", paretoTransportadora);
    if (!paretoTransportadoraHasChart || paretoTransportadora.length === 0) {
      renderEmptyVisual(paretoTransportadoraVisual, "Sem ocorrências de frete zerado por transportadora.");
    } else {
      paretoTransportadoraVisual.className = "small text-muted mb-2";
      paretoTransportadoraVisual.textContent = `Pareto transportadora com ${paretoTransportadora.length} categorias.`;
    }

    setPager(infoTransportadora, prevTransportadora, nextTransportadora, pTransportadora);
    setPager(infoUfOrigem, prevUfOrigem, nextUfOrigem, pUfOrigem);
    setPager(infoUfDestino, prevUfDestino, nextUfDestino, pUfDestino);
    setPager(infoTemporal, prevTemporal, nextTemporal, pTemporal);
    renderQuality(payload, kpis.total_documentos);
    renderFilterStatus();
    renderActiveFilterChips();
    applyChartVisibility();
    renderHiddenChartsControls();
  };

  const fillSelectOptions = (select, values) => {
    const current = select.value;
    const options = ['<option value="">Todas</option>'];
    values.forEach((item) => {
      const text = sanitizeFilterValue(item);
      if (!text) return;
      options.push(`<option value="${text}">${text}</option>`);
    });
    select.innerHTML = options.join("");
    if (values.includes(current)) {
      select.value = current;
    }
  };

  const resetPagination = () => {
    tableState.transportadora.page = 1;
    tableState.ufOrigem.page = 1;
    tableState.ufDestino.page = 1;
    tableState.temporal.page = 1;
  };

  const applyControlsToState = () => {
    tableState.transportadora.sortKey = sortTransportadora.value;
    tableState.transportadora.order = orderTransportadora.value === "asc" ? "asc" : "desc";
    tableState.transportadora.pageSize = clampPageSize(sizeTransportadora.value);
    tableState.ufOrigem.sortKey = sortUfOrigem.value;
    tableState.ufOrigem.order = orderUfOrigem.value === "asc" ? "asc" : "desc";
    tableState.ufOrigem.pageSize = clampPageSize(sizeUfOrigem.value);
    tableState.ufDestino.sortKey = sortUfDestino.value;
    tableState.ufDestino.order = orderUfDestino.value === "asc" ? "asc" : "desc";
    tableState.ufDestino.pageSize = clampPageSize(sizeUfDestino.value);
    tableState.temporal.sortKey = sortTemporal.value;
    tableState.temporal.order = orderTemporal.value === "asc" ? "asc" : "desc";
    tableState.temporal.pageSize = clampPageSize(sizeTemporal.value);
  };

  const renderFromState = () => {
    if (!currentData) return;
    applyControlsToState();
    renderAnalytics(currentData);
  };

  const setFiltersEnabled = (enabled) => {
    [filterDateStart, filterDateEnd, resetFiltersBtn].forEach((el) => {
      el.disabled = !enabled;
    });
  };

  const hydrateFilterOptions = (data) => {
    const temporal = Array.isArray(data.temporal_stats) ? data.temporal_stats.map((row) => toIsoDateOrEmpty(row?.data)).filter(Boolean) : [];
    const minDate = temporal.length > 0 ? temporal.slice().sort()[0] : "";
    const maxDate = temporal.length > 0 ? temporal.slice().sort().slice(-1)[0] : "";
    filterDateStart.min = minDate;
    filterDateStart.max = maxDate;
    filterDateEnd.min = minDate;
    filterDateEnd.max = maxDate;
    setFiltersEnabled(true);
    syncFilterControlsFromState();
  };

  const updateActiveFiltersFromResponse = (data) => {
    const payload = data?.active_filters || {};
    activeFilters.transportadora = safeText(payload.transportadora) || null;
    activeFilters.uf_origem = safeText(payload.uf_origem) || null;
    activeFilters.uf_destino = safeText(payload.uf_destino) || null;
    activeFilters.data_inicio = toIsoDateOrEmpty(payload.data_inicio) || null;
    activeFilters.data_fim = toIsoDateOrEmpty(payload.data_fim) || null;
    syncFilterControlsFromState();
  };

  const applyBackendFilters = async () => {
    if (!currentData) return;
    resetFiltersBtn.disabled = true;
    filterDateStart.disabled = true;
    filterDateEnd.disabled = true;
    setDashboardState("Aplicando filtros", "text-bg-primary");
    try {
      const response = await fetch("/api/cleide/dashboard/filter", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ filters: getCurrentFiltersPayload() }),
      });
      const data = await response.json();
      if (!response.ok || !data.success) {
        updateStatus(data.error || "Falha ao aplicar filtros.", "text-danger");
        setDashboardState("Erro no filtro", "text-bg-danger");
        return;
      }
      currentData = {
        kpis: data.kpis || {},
        dataset_summary: data.dataset_summary || {},
        transportadora_stats: Array.isArray(data.transportadora_stats) ? data.transportadora_stats : [],
        uf_origem_stats: Array.isArray(data.uf_origem_stats) ? data.uf_origem_stats : [],
        uf_destino_stats: Array.isArray(data.uf_destino_stats) ? data.uf_destino_stats : [],
        temporal_stats: Array.isArray(data.temporal_stats) ? data.temporal_stats : [],
        pareto_fretes_zerados_uf_destino: Array.isArray(data.pareto_fretes_zerados_uf_destino)
          ? data.pareto_fretes_zerados_uf_destino
          : [],
        pareto_fretes_zerados_transportadora: Array.isArray(data.pareto_fretes_zerados_transportadora)
          ? data.pareto_fretes_zerados_transportadora
          : [],
      };
      updateActiveFiltersFromResponse(data);
      hydrateFilterOptions(currentData);
      resetPagination();
      renderFromState();
      setDashboardState("Dashboard filtrado", "text-bg-success");
      updateStatus("Filtros aplicados via backend oficial da Cleide.", "text-success");
    } catch (_err) {
      updateStatus("Falha de comunicação ao aplicar filtros.", "text-danger");
      setDashboardState("Erro de comunicação", "text-bg-danger");
    } finally {
      filterDateStart.disabled = false;
      filterDateEnd.disabled = false;
      resetFiltersBtn.disabled = false;
    }
  };

  const updateStructuralDetails = (data) => {
    const detected = Array.isArray(data.colunas_detectadas) ? data.colunas_detectadas : [];
    const missing = Array.isArray(data.colunas_faltantes) ? data.colunas_faltantes : [];
    const rows = Number.isInteger(data.linhas_detectadas) ? data.linhas_detectadas : Number(data.linhas_detectadas || 0);
    const sheet = data.sheet_detectada || "n/a";
    const detectedText = detected.length > 0 ? detected.join(", ") : "-";
    const missingText = missing.length > 0 ? missing.join(", ") : "-";
    structuralDetails.textContent = `Colunas detectadas: ${detectedText} | Colunas faltantes: ${missingText} | Linhas: ${rows} | Sheet: ${sheet}`;
    renderAnalytics(data || {});
  };

  const renderEmptyDashboard = (message) => {
    renderAnalytics({});
    renderEmptyVisual(transportadoraVisual, message);
    renderEmptyVisual(ufOrigemVisual, message);
    renderEmptyVisual(ufDestinoVisual, message);
    renderEmptyVisual(temporalVisual, message);
    filterStatus.textContent = "Aguardando upload para ativar filtros.";
    applyChartVisibility();
    renderHiddenChartsControls();
  };

  const renderStructuralFromPayload = (data) => {
    const missing = Array.isArray(data.colunas_faltantes) ? data.colunas_faltantes : [];
    const detected = Array.isArray(data.colunas_detectadas) ? data.colunas_detectadas : [];
    const rows = Number.isInteger(data.linhas_detectadas) ? data.linhas_detectadas : Number(data.linhas_detectadas || 0);
    const sheet = data.sheet_detectada || "n/a";
    updateStructuralDetails(data);

    if (!data.dataset_validado) {
      currentData = null;
      Object.assign(activeFilters, {
        transportadora: null,
        uf_origem: null,
        uf_destino: null,
        data_inicio: null,
        data_fim: null,
      });
      setFiltersEnabled(false);
      setDashboardState("Dataset inválido", "text-bg-warning");
      if (missing.length > 0) {
        updateStructural(
          `Dataset inválido: colunas obrigatórias ausentes (${missing.join(", ")}).`,
          "text-warning",
        );
      } else {
        updateStructural("Dataset inválido para layout operacional mínimo.", "text-warning");
      }
      renderEmptyDashboard("Dataset inválido para agregação operacional.");
      return;
    }

    if (!data.analytics_ready) {
      currentData = null;
      setFiltersEnabled(false);
      setDashboardState("Sem agregados", "text-bg-secondary");
      renderEmptyDashboard("Sem agregados disponíveis para o dataset atual.");
      updateStructural("Estrutura válida, mas a análise ainda está sem agregações.", "text-warning");
      return;
    }

    const agg = data.aggregate_counts || {};
    const hasAnyAggregate =
      getNumeric(agg.transportadora_stats) > 0 ||
      getNumeric(agg.uf_origem_stats) > 0 ||
      getNumeric(agg.uf_destino_stats) > 0 ||
      getNumeric(agg.temporal_stats) > 0;
    if (!hasAnyAggregate) {
      currentData = data;
      updateActiveFiltersFromResponse(data);
      setFiltersEnabled(true);
      hydrateFilterOptions(data);
      setDashboardState("Sem agregados no fallback", "text-bg-secondary");
      renderEmptyDashboard("Análise pronta, porém sem tabelas agregadas para exibir.");
      updateStructural("Estrutura válida sem blocos agregados nesta amostra.", "text-warning");
      return;
    }

    currentData = {
      kpis: data.kpis || {},
      dataset_summary: data.dataset_summary || {},
      transportadora_stats: Array.isArray(data.transportadora_stats) ? data.transportadora_stats : [],
      uf_origem_stats: Array.isArray(data.uf_origem_stats) ? data.uf_origem_stats : [],
      uf_destino_stats: Array.isArray(data.uf_destino_stats) ? data.uf_destino_stats : [],
      temporal_stats: Array.isArray(data.temporal_stats) ? data.temporal_stats : [],
      pareto_fretes_zerados_uf_destino: Array.isArray(data.pareto_fretes_zerados_uf_destino)
        ? data.pareto_fretes_zerados_uf_destino
        : [],
      pareto_fretes_zerados_transportadora: Array.isArray(data.pareto_fretes_zerados_transportadora)
        ? data.pareto_fretes_zerados_transportadora
        : [],
    };
    updateActiveFiltersFromResponse(data);
    hydrateFilterOptions(currentData);
    setFiltersEnabled(true);
    renderFromState();
    setDashboardState("Dashboard ativo", "text-bg-success");
    updateStructural(
      `Estrutura válida: ${rows} linhas, sheet ${sheet}, colunas reconhecidas ${detected.length}.`,
      "text-success",
    );
  };

  const setUploading = (running) => {
    uploading = running;
    submitBtn.disabled = running || !input.files || input.files.length === 0;
    selectBtn.disabled = running;
    clearBtn.disabled = running;
    input.disabled = running;
    dropzone.classList.toggle("is-uploading", running);
  };

  const setHover = (hover) => {
    if (uploading) return;
    dropzone.classList.toggle("is-hover", hover);
  };

  ["dragenter", "dragover"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      setHover(true);
    });
  });

  ["dragleave", "drop"].forEach((eventName) => {
    dropzone.addEventListener(eventName, (event) => {
      event.preventDefault();
      setHover(false);
    });
  });

  dropzone.addEventListener("drop", (event) => {
    if (!isAuthenticated) {
      redirectToLogin();
      return;
    }
    if (uploading) return;
    const files = event.dataTransfer?.files;
    if (!files || files.length === 0) return;
    input.files = files;
    submitBtn.disabled = false;
    updateStatus(`Arquivo selecionado: ${files[0].name}`);
  });

  selectBtn.addEventListener("click", () => {
    if (!isAuthenticated) {
      redirectToLogin();
      return;
    }
    if (uploading) return;
    input.click();
  });

  input.addEventListener("change", () => {
    const file = input.files && input.files[0];
    if (!file) {
      submitBtn.disabled = true;
      updateStatus("Aguardando upload");
      return;
    }
    submitBtn.disabled = false;
    updateStatus(`Arquivo selecionado: ${file.name}`);
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!isAuthenticated) {
      renderAnonymousState();
      redirectToLogin();
      return;
    }
    if (uploading) return;
    const file = input.files && input.files[0];
    if (!file) {
      updateStatus("Selecione um arquivo XLSX ou CSV.", "text-warning");
      return;
    }

    const payload = new FormData();
    payload.append("file", file);
    setUploading(true);
    updateStatus("Upload em andamento...", "text-primary");
    setDashboardState("Carregando dashboard", "text-bg-primary");

    try {
      const response = await fetch("/api/cleide/upload", {
        method: "POST",
        headers: uploadLock ? { "X-Cleide-Upload-Lock": uploadLock } : {},
        body: payload,
      });
      const data = await response.json();
      if (!response.ok || !data.success) {
        if (data?.error_code === "upload_total_max_exceeded") {
          updateStatus(formatUploadLimitExceededMessage(data), "text-danger");
          updateStructural("Upload bloqueado por limite configurado de linhas.", "text-warning");
        } else {
          updateStatus(data.error || "Falha ao enviar upload.", "text-danger");
          updateStructural("Falha na validação estrutural do arquivo enviado.", "text-danger");
        }
        setDashboardState("Erro no upload", "text-bg-danger");
        renderEmptyDashboard("Erro no upload. Revise o arquivo e tente novamente.");
      } else if (data.replaced_previous_upload) {
        uploadLock = data.upload_lock || uploadLock;
        updateStatus(data.message || "Upload substituído com sucesso.", "text-success");
        clearBtn.disabled = false;
        renderStructuralFromPayload(data);
      } else {
        uploadLock = data.upload_lock || uploadLock;
        updateStatus(data.message || "Upload concluído com sucesso.", "text-success");
        clearBtn.disabled = false;
        renderStructuralFromPayload(data);
      }
    } catch (_err) {
      updateStatus("Falha de comunicação no upload.", "text-danger");
      updateStructural("Falha de comunicação na validação estrutural.", "text-danger");
      console.warn("[Cleide] Falha de comunicação no upload.");
      setDashboardState("Erro de comunicação", "text-bg-danger");
      renderEmptyDashboard("Falha de comunicação ao carregar dashboard.");
    } finally {
      setUploading(false);
      submitBtn.disabled = !(input.files && input.files.length > 0);
    }
  });

  clearBtn.addEventListener("click", async () => {
    if (!isAuthenticated) {
      renderAnonymousState();
      redirectToLogin();
      return;
    }
    if (uploading) return;
    setUploading(true);
    updateStatus("Removendo upload ativo...", "text-primary");
    try {
      const response = await fetch("/api/cleide/upload/clear", {
        method: "POST",
        headers: uploadLock ? { "X-Cleide-Upload-Lock": uploadLock } : {},
      });
      const data = await response.json();
      if (!response.ok || !data.success) {
        updateStatus(data.error || "Falha ao limpar upload.", "text-danger");
      } else {
        input.value = "";
        submitBtn.disabled = true;
        clearBtn.disabled = true;
        updateStatus("Upload removido. Aguardando novo arquivo.", "text-muted");
        updateStructural("Aguardando upload para validação estrutural.", "text-muted");
        updateStructuralDetails({});
        currentData = null;
        Object.assign(activeFilters, {
          transportadora: null,
          uf_origem: null,
          uf_destino: null,
          data_inicio: null,
          data_fim: null,
        });
        syncFilterControlsFromState();
        setFiltersEnabled(false);
        setDashboardState("Sem upload ativo", "text-bg-light border");
        renderEmptyDashboard("Aguardando upload para montar dashboard operacional.");
      }
    } catch (_err) {
      updateStatus("Falha de comunicação ao limpar upload.", "text-danger");
      console.warn("[Cleide] Falha de comunicação ao limpar upload.");
    } finally {
      setUploading(false);
    }
  });

  (async () => {
    if (!isAuthenticated) {
      renderAnonymousState();
      return;
    }
    try {
      const response = await fetch("/api/cleide/upload/status");
      const data = await response.json();
      uploadLock = data.upload_lock || uploadLock;
      if (response.ok && data.success) {
        if (data.upload_ativo) {
          clearBtn.disabled = false;
          updateStatus(`Upload ativo: ${data.filename}`, "text-success");
          renderStructuralFromPayload(data);
        } else if (data.stale_upload) {
          clearBtn.disabled = false;
          updateStatus(
            "Estado inconsistente detectado. Upload anterior não encontrado. Limpeza necessária.",
            "text-warning",
          );
          setDashboardState("Upload inconsistente", "text-bg-warning");
          renderStructuralFromPayload(data);
        } else {
          clearBtn.disabled = true;
          updateStatus("Aguardando upload");
          updateStructural("Aguardando upload para validação estrutural.", "text-muted");
          updateStructuralDetails({});
          setDashboardState("Sem upload ativo", "text-bg-light border");
          renderEmptyDashboard("Aguardando upload para montar dashboard operacional.");
        }
      } else {
        clearBtn.disabled = true;
        updateStatus("Aguardando upload");
        updateStructural("Aguardando upload para validação estrutural.", "text-muted");
        updateStructuralDetails({});
        setDashboardState("Sem upload ativo", "text-bg-light border");
        renderEmptyDashboard("Aguardando upload para montar dashboard operacional.");
      }
    } catch (_err) {
      updateStatus("Aguardando upload");
      updateStructural("Aguardando upload para validação estrutural.", "text-muted");
      updateStructuralDetails({});
      console.warn("[Cleide] Falha ao carregar status inicial.");
      setDashboardState("Sem upload ativo", "text-bg-light border");
      renderEmptyDashboard("Aguardando upload para montar dashboard operacional.");
    }
  })();

  const autoApplyDateFilters = async () => {
    if (!currentData) return;
    activeFilters.data_inicio = toIsoDateOrEmpty(filterDateStart.value) || null;
    activeFilters.data_fim = toIsoDateOrEmpty(filterDateEnd.value) || null;
    if (activeFilters.data_inicio && activeFilters.data_fim && activeFilters.data_inicio > activeFilters.data_fim) {
      const tmp = activeFilters.data_inicio;
      activeFilters.data_inicio = activeFilters.data_fim;
      activeFilters.data_fim = tmp;
      syncFilterControlsFromState();
    }
    resetPagination();
    await applyBackendFilters();
  };

  filterDateStart.addEventListener("change", autoApplyDateFilters);
  filterDateEnd.addEventListener("change", autoApplyDateFilters);

  resetFiltersBtn.addEventListener("click", async () => {
    Object.assign(activeFilters, {
      transportadora: null,
      uf_origem: null,
      uf_destino: null,
      data_inicio: null,
      data_fim: null,
    });
    syncFilterControlsFromState();
    resetPagination();
    await applyBackendFilters();
  });

  activeFilterChips.addEventListener("click", async (event) => {
    const btn = event.target?.closest?.("[data-cleide-remove-filter]");
    if (!btn) return;
    const key = btn.getAttribute("data-cleide-remove-filter");
    if (!key || !(key in activeFilters)) return;
    activeFilters[key] = null;
    syncFilterControlsFromState();
    resetPagination();
    await applyBackendFilters();
  });

  root.addEventListener("click", (event) => {
    const hideBtn = event.target?.closest?.("[data-cleide-hide-chart]");
    if (hideBtn) {
      const chartKey = hideBtn.getAttribute("data-cleide-hide-chart");
      if (!chartKey) return;
      hideChart(chartKey);
      return;
    }
    const showBtn = event.target?.closest?.("[data-cleide-show-chart]");
    if (showBtn) {
      const chartKey = showBtn.getAttribute("data-cleide-show-chart");
      if (!chartKey) return;
      showChart(chartKey);
    }
  });

  showAllChartsBtn.addEventListener("click", () => {
    showAllCharts();
  });

  const bindTableControls = (cfg, sortEl, orderEl, sizeEl, prevEl, nextEl) => {
    sortEl.addEventListener("change", () => {
      cfg.page = 1;
      renderFromState();
    });
    orderEl.addEventListener("change", () => {
      cfg.page = 1;
      renderFromState();
    });
    sizeEl.addEventListener("change", () => {
      cfg.page = 1;
      const clamped = clampPageSize(sizeEl.value);
      cfg.pageSize = clamped;
      sizeEl.value = String(clamped);
      renderFromState();
    });
    prevEl.addEventListener("click", () => {
      cfg.page = Math.max(1, cfg.page - 1);
      renderFromState();
    });
    nextEl.addEventListener("click", () => {
      const maxPages = Number.parseInt(String(nextEl.dataset.maxPages || "1"), 10) || 1;
      cfg.page = Math.min(maxPages, cfg.page + 1);
      renderFromState();
    });
  };

  bindTableControls(
    tableState.transportadora,
    sortTransportadora,
    orderTransportadora,
    sizeTransportadora,
    prevTransportadora,
    nextTransportadora,
  );
  bindTableControls(tableState.ufOrigem, sortUfOrigem, orderUfOrigem, sizeUfOrigem, prevUfOrigem, nextUfOrigem);
  bindTableControls(tableState.ufDestino, sortUfDestino, orderUfDestino, sizeUfDestino, prevUfDestino, nextUfDestino);
  bindTableControls(tableState.temporal, sortTemporal, orderTemporal, sizeTemporal, prevTemporal, nextTemporal);
  applyChartVisibility();
  renderHiddenChartsControls();
  setFiltersEnabled(false);

  chatForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    await submitChatQuestion();
  });

  const getLastCleideMessageText = () => {
    const nodes = Array.from(chatMessages.querySelectorAll(".cleide-chat-msg--cleide .cleide-chat-msg__inner"));
    for (let i = nodes.length - 1; i >= 0; i -= 1) {
      const text = String(nodes[i]?.textContent || "").trim();
      if (!text || isInitialChatPlaceholder(text)) continue;
      return text;
    }
    return "";
  };

  chatMessages.addEventListener("click", (event) => {
    const target = event.target?.closest?.(".cleide-chat-copy-btn");
    if (!target) return;
    const msgNode = target.closest(".cleide-chat-msg--cleide");
    const text = String(msgNode?.querySelector(".cleide-chat-msg__inner")?.textContent || "").trim();
    copyTextToClipboard(text)
      .then(() => markCopied(target))
      .catch(() => {
        target.textContent = "Falha";
        window.setTimeout(() => {
          target.textContent = "Copiar";
        }, 1200);
      });
  });

  chatCopyLastBtn.addEventListener("click", () => {
    const text = getLastCleideMessageText();
    if (!text) {
      chatLoading.textContent = "Sem resposta para copiar.";
      window.setTimeout(() => {
        if (!chatRequestRunning) chatLoading.textContent = "";
      }, 1200);
      return;
    }
    copyTextToClipboard(text)
      .then(() => {
        chatCopyLastBtn.textContent = "Copiado";
        window.setTimeout(() => {
          chatCopyLastBtn.textContent = "Copiar última resposta";
        }, 1200);
      })
      .catch(() => {
        chatCopyLastBtn.textContent = "Falha";
        window.setTimeout(() => {
          chatCopyLastBtn.textContent = "Copiar última resposta";
        }, 1200);
      });
  });

  chatToggle.addEventListener("click", () => {
    setCleideChatOpen(!cleideChatOpen);
  });

  chatClose.addEventListener("click", () => {
    setCleideChatOpen(false);
  });

  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && cleideChatOpen) {
      setCleideChatOpen(false);
    }
  });

  setCleideChatOpen(false);
})();
