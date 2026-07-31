"""Regressão do autobloqueio do primeiro upload (preparação vs upload HTTP).

Nível dos testes: estático estrutural sobre app/static/js/agente_compara.js.
O projeto não possui harness DOM/jsdom; estes asserts codificam a causa-raiz
comprovada no navegador e o contrato de flags separado.
"""
from __future__ import annotations

import pathlib
import re


def _js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _pending_block(js: str) -> str:
    return js[
        js.index("function beginPendingFreightTableUpload") : js.index(
            "function openCarrierNameEdit"
        )
    ]


def _cancel_block(js: str) -> str:
    return js[
        js.index("function cancelCarrierIdentification") : js.index("function openCarrierNameEdit")
    ]


def _upload_block(js: str) -> str:
    return js[js.index("function uploadDocument") : js.index("function removeDocument")]


def _prep_loading_block(js: str) -> str:
    return js[
        js.index("function setUploadPreparationLoading") : js.index("function resetFreightFileInput")
    ]


def _http_loading_block(js: str) -> str:
    return js[js.index("function setUploadLoading") : js.index("function docTypeLabel")]


def test_flags_are_separated_preparation_vs_http_upload():
    js = _js()
    assert "var freightUploadPreparationInFlight = false;" in js
    assert "var uploadInFlight = false;" in js
    assert "function setUploadPreparationLoading(on)" in js
    prep = _prep_loading_block(js)
    http = _http_loading_block(js)
    assert "uploadInFlight" not in prep
    assert "uploadInFlight = !!on;" in http
    assert "UPLOAD_PAGE_STATUS_SENDING" in http
    assert "UPLOAD_PAGE_STATUS_SENDING = 'Enviando documento...'" in js
    assert "UPLOAD_PAGE_STATUS_PREPARING" in prep
    assert "UPLOAD_PAGE_STATUS_PREPARING = 'Preparando envio...'" in js
    assert "UPLOAD_PAGE_STATUS_PROCESSING = 'Estruturando tabela temporária...'" in js


def test_begin_pending_does_not_self_block_via_upload_in_flight():
    """Causa-raiz: setUploadLoading(true) + gate em uploadInFlight no then.

    Este teste falharia no código anterior porque beginPending usava
    setUploadLoading (que liga uploadInFlight) e depois abortava no then.
    """
    pending = _pending_block(_js())
    assert "setUploadLoading" not in pending
    assert "freightUploadPreparationInFlight = true" in pending
    assert "setUploadPreparationLoading(true)" in pending
    assert pending.index("freightUploadPreparationInFlight = true") < pending.index(
        "ensureComparisonStarted()"
    )
    assert pending.index("setUploadPreparationLoading(true)") < pending.index(
        "ensureComparisonStarted()"
    )

    before_ensure = pending.split("ensureComparisonStarted()", 1)[0]
    assert "freightUploadPreparationInFlight" in before_ensure
    assert "uploadInFlight" in before_ensure

    then_body = pending.split(".then(function (result)", 1)[1].split(".catch(", 1)[0]
    assert "pendingFreightTableUpload = {" in then_body
    assert "openCarrierIdentificationPanel" in then_body
    assert "API_UPLOAD" not in then_body
    assert "uploadDocument(" not in then_body
    # Preparação própria não pode abortar o then.
    assert "freightUploadPreparationInFlight" not in then_body
    # uploadInFlight no then só bloqueia HTTP real concorrente (não ligado pela prep).
    assert "if (pendingFreightTableUpload || uploadInFlight || carrierIdentifyInFlight)" in then_body


def test_begin_pending_creates_stable_identity_and_opens_carrier_modal():
    pending = _pending_block(_js())
    assert "comparisonId: comparisonState.comparisonId" in pending
    assert "tableId: active.table_id" in pending
    assert "slot: active.slot_number" in pending
    assert "currentStep: comparisonState.currentStep" in pending
    assert "file: file" in pending
    assert pending.index("pendingFreightTableUpload = {") < pending.index(
        "openCarrierIdentificationPanel"
    )
    assert "Não foi possível identificar a tabela ativa para upload." in pending
    assert "Não foi possível preparar o envio da tabela. Tente novamente." in pending


def test_begin_pending_restores_preparation_flag_on_finally_and_error():
    pending = _pending_block(_js())
    assert ".catch(function ()" in pending
    assert ".finally(function ()" in pending
    finally_body = pending.split(".finally(function ()", 1)[1]
    assert "freightUploadPreparationInFlight = false" in finally_body
    assert "setUploadPreparationLoading(false)" in finally_body


def test_cancel_carrier_clears_pending_and_preparation_flags():
    cancel = _cancel_block(_js())
    assert "clearPendingFreightTableUpload()" in cancel
    assert "freightUploadPreparationInFlight = false" in cancel
    assert "setUploadPreparationLoading(false)" in cancel
    assert "resetFreightFileInput()" in cancel
    assert "fetch(" not in cancel
    assert "uploadDocument" not in cancel


def test_upload_document_owns_http_upload_in_flight_only():
    upload = _upload_block(_js())
    assert "if (!file || uploadInFlight) return" in upload
    assert "setUploadLoading(true)" in upload
    assert upload.index("setUploadLoading(true)") < upload.index("fetch(API_UPLOAD")
    assert "setUploadLoading(false)" in upload
    assert "freightUploadPreparationInFlight" not in upload
    assert "setUploadPreparationLoading" not in upload


def test_click_guards_block_double_preparation_and_http_inflight():
    js = _js()
    init = js[js.index("function initDocuments") : js.index("function init()")]
    upload_click = init[
        init.index("uploadItem.addEventListener('click'") : init.index(
            "fileInput.addEventListener('change'"
        )
    ]
    assert "freightUploadPreparationInFlight" in upload_click
    assert "uploadInFlight" in upload_click
    assert "pendingFreightTableUpload" in upload_click
    assert "isCarrierIdentificationOpen()" in upload_click

    wizard = js[
        js.index("function triggerComparisonWizardFileInput") : js.index(
            "function renderComparisonWizardUploadBody"
        )
    ]
    assert "freightUploadPreparationInFlight" in wizard
    assert "uploadInFlight" in wizard


def test_confirm_carrier_calls_upload_once_with_pending_identity():
    js = _js()
    confirm = js[
        js.index("function confirmCarrierIdentification") : js.index(
            "function initCarrierIdentificationPanel"
        )
    ]
    assert confirm.count("uploadDocument(") == 1
    assert "uploadDocument(file, validation.name" in confirm
    assert "comparisonId: pending.comparisonId" in confirm
    assert "tableId: pending.tableId" in confirm
    assert "slot: pending.slot" in confirm
    assert "clearPendingFreightTableUpload()" in confirm
    assert confirm.index("clearPendingFreightTableUpload()") < confirm.index("uploadDocument(")


def test_pending_slot_comes_from_active_table_for_tables_2_and_3():
    """Preserva identidade do slot ativo (1/2/3) no pending — sem hardcode de slot 1."""
    pending = _pending_block(_js())
    assert "slot: active.slot_number" in pending
    assert re.search(r"slot:\s*1\b", pending) is None
    assert "slot: 2" not in pending
    assert "slot: 3" not in pending


def test_reset_clears_preparation_flag():
    js = _js()
    reset = js[
        js.index("function resetAgenteComparaFrontendState") : js.index(
            "function cacheReviewTempTableIfOwned"
        )
    ]
    assert "freightUploadPreparationInFlight = false" in reset
    assert "setUploadPreparationLoading(false)" in reset
    assert "clearPendingFreightTableUpload()" in reset


def test_single_change_listener_routes_to_begin_pending_not_upload():
    js = _js()
    init = js[js.index("function initDocuments") : js.index("function init()")]
    change = init[init.index("fileInput.addEventListener('change'") :]
    assert "beginPendingFreightTableUpload(file)" in change
    assert "uploadDocument(file)" not in change.split("if (clearBtn)")[0]


def test_upload_document_captures_generation_and_identity():
    upload = _upload_block(_js())
    assert "requestGenerationAtStart = comparisonRequestGeneration" in upload
    assert "comparisonIdAtStart" in upload
    assert "tableIdAtStart" in upload
    assert "isCurrentComparisonRequest(requestGenerationAtStart, comparisonIdAtStart, tableIdAtStart)" in upload
    assert "function uploadAttemptStillActive" in upload
    assert "function responseMatchesUploadAttempt" in upload


def test_upload_document_guards_after_fetch_and_json():
    upload = _upload_block(_js())
    assert "stale: true" in upload
    assert upload.index("uploadAttemptStillActive()") < upload.index("r.json()")
    assert "if (!uploadAttemptStillActive())" in upload
    # Após JSON e antes de sincronizar/abrir fluxo
    sync_idx = upload.index("syncComparisonStateFromPayload(res.data.comparison)")
    assert upload[:sync_idx].count("uploadAttemptStillActive()") >= 2
    assert "if (!responseMatchesUploadAttempt(res.data))" in upload


def test_upload_document_discards_stale_without_side_effects():
    """Resposta stale não sincroniza estado, não fetchDocuments, não abre modal.

    Limite: contrato estrutural sobre o source (sem execução JS/jsdom).
    """
    upload = _upload_block(_js())
    # Descarte silencioso: retorno null sem setError no ramo stale
    assert "res.stale || !uploadAttemptStillActive()" in upload
    assert "return null;" in upload
    assert "responseMatchesUploadAttempt(res.data)" in upload
    assert "data.comparison_id && data.comparison_id !== comparisonIdAtStart" in upload
    assert "data.table_id && data.table_id !== tableIdAtStart" in upload
    assert "data.temp_table.comparison_id" in upload
    assert "data.temp_table.table_id" in upload
    # sync / fetchDocuments / handleTempTable só após guardas
    assert upload.index("responseMatchesUploadAttempt(res.data)") < upload.index(
        "syncComparisonStateFromPayload"
    )
    assert upload.index("uploadAttemptStillActive()") < upload.index("fetchDocuments()")
    handle_idx = upload.index("handleTempTableFromStatus({")
    assert "if (!uploadAttemptStillActive()) return null;" in upload[:handle_idx]
    # finally antigo não limpa tentativa de outra geração
    finally_body = upload.split(".finally(function ()", 1)[1]
    assert "requestGenerationAtStart !== comparisonRequestGeneration" in finally_body
    assert "setUploadLoading(false)" in finally_body
    assert finally_body.index("requestGenerationAtStart !== comparisonRequestGeneration") < finally_body.index(
        "setUploadLoading(false)"
    )


def test_upload_document_current_path_still_syncs_and_fetches():
    upload = _upload_block(_js())
    assert "syncComparisonStateFromPayload(res.data.comparison)" in upload
    assert "fetchDocuments()" in upload
    assert "handleTempTableFromStatus({" in upload
    assert "markComparisonWizardEngaged()" in upload
    assert "API_UPLOAD" in upload


def test_is_current_comparison_request_helper_contract():
    js = _js()
    helper = js[
        js.index("function isCurrentComparisonRequest") : js.index(
            "function clearLocalComparisonState"
        )
    ]
    assert "generation !== comparisonRequestGeneration" in helper
    assert "comparisonState.comparisonId" in helper
    assert "comparisonState.activeTableId" in helper
    assert "return false" in helper
    assert "return true" in helper
    assert "fetch(" not in helper


def test_maybe_open_wizard_guards_upload_inflight_and_identity():
    js = _js()
    maybe = js[
        js.index("function maybeOpenComparisonWizardAfterStatus") : js.index(
            "function openComparisonWizardModal"
        )
    ]
    assert "if (!comparisonState.comparisonId) return;" in maybe
    assert "isReviewReadyTempTable(currentTempTable)" in maybe
    assert "if (uploadInFlight) return;" in maybe
    assert "renderAndShowComparisonFlowModal('review')" in maybe
    assert "transitionComparisonFlowModal('processing')" in maybe
    assert "transitionComparisonFlowModal('review')" in maybe
    assert "openComparisonWizardModal()" not in maybe


def test_should_auto_open_rejects_processing_accepts_review_only():
    js = _js()
    auto = js[
        js.index("function shouldAutoOpenComparisonWizard") : js.index(
            "function maybeOpenComparisonWizardAfterStatus"
        )
    ]
    assert "view !== 'review'" in auto
    assert "isReviewReadyTempTable(currentTempTable)" in auto
    assert "if (uploadInFlight) return false;" in auto
    assert "view === 'review' || view === 'processing'" not in auto
    assert "comparisonFlowView === 'uploading'" in auto


def test_is_review_ready_temp_table_requires_needs_review_and_ids():
    js = _js()
    helper = js[
        js.index("function isReviewReadyTempTable") : js.index(
            "function renderComparisonWizardModal"
        )
    ]
    assert "needs_review" in helper
    assert "temp_table_id" in helper
    assert "comparison_id" in helper
    assert "table_id" in helper
    assert "active.confirmed" in helper
    assert "tempTableMatchesActiveSlot" in helper
    assert "fetch(" not in helper


def test_open_modal_requires_successful_render():
    js = _js()
    open_block = js[
        js.index("function openComparisonWizardModal") : js.index(
            "function refreshComparisonWizardAfterTransition"
        )
    ]
    assert "renderAndShowComparisonFlowModal(view)" in open_block
    assert "var rendered = renderComparisonWizardModal();" in open_block
    assert "if (!rendered) return;" in open_block
    assert "showTempTableModalShell()" in open_block
    assert "modal.hidden = false" not in open_block
    shell = js[js.index("function showTempTableModalShell") : js.index("function rememberCarrierIdentifyPanelHome")]
    assert "modal.hidden = false" in shell
    assert "agente-compara-temp-table-modal-open" in shell


def test_processing_view_still_renderable_for_manual_not_auto_open():
    """Processing permanece renderizável no clique manual; auto-open não o usa."""
    js = _js()
    auto = js[
        js.index("function shouldAutoOpenComparisonWizard") : js.index(
            "function maybeOpenComparisonWizardAfterStatus"
        )
    ]
    assert "view !== 'review'" in auto
    item = js[js.index("function renderTempTableItem") : js.index("function renderDocumentItem")]
    assert "openTempTableModal()" in item
    open_temp = js[js.index("function openTempTableModal") : js.index("function closeTempTableModal")]
    assert "renderAndShowComparisonFlowModal('processing')" in open_temp


def test_full_journey_contract_reset_then_stale_upload_then_valid_upload():
    """Jornada A→reset→B: teardown + guarda stale + caminho válido (source-level)."""
    js = _js()
    reset = js[
        js.index("function resetAgenteComparaFrontendState") : js.index(
            "function cacheReviewTempTableIfOwned"
        )
    ]
    upload = _upload_block(js)
    assert "teardownTempTableModal()" in reset
    assert "bumpComparisonRequestGeneration()" in reset
    assert "setCurrentTempTable(null)" in reset
    assert "requestGenerationAtStart" in upload
    assert "stale: true" in upload
    assert "responseMatchesUploadAttempt" in upload
    assert "syncComparisonStateFromPayload(res.data.comparison)" in upload
    assert "fetchDocuments()" in upload
    teardown = js[
        js.index("function teardownTempTableModal") : js.index(
            "function resetAgenteComparaFrontendState"
        )
    ]
    assert "titleEl.textContent = ''" in teardown
    assert "body.replaceChildren()" in teardown
    assert "hideTempTableModalShell()" in teardown


def test_journey_processing_keeps_auto_open_until_needs_review():
    """Upload→processing→needs_review: auto-open só em review-ready; modal contínuo."""
    js = _js()
    auto = js[
        js.index("function shouldAutoOpenComparisonWizard") : js.index(
            "function maybeOpenComparisonWizardAfterStatus"
        )
    ]
    maybe = js[
        js.index("function maybeOpenComparisonWizardAfterStatus") : js.index(
            "function openComparisonWizardModal"
        )
    ]
    status = js[js.index("function handleTempTableFromStatus") : js.index("function formatBytes")]
    assert "view !== 'review'" in auto
    assert "isReviewReadyTempTable(currentTempTable)" in auto
    assert "if (uploadInFlight) return false;" in auto
    assert "transitionComparisonFlowModal('processing')" in maybe
    assert "transitionComparisonFlowModal('review')" in maybe
    assert "renderAndShowComparisonFlowModal('review')" in maybe
    assert "maybeOpenComparisonWizardAfterStatus()" in status
    assert "transitionComparisonFlowModal('processing')" in status
    assert "transitionComparisonFlowModal('review')" in status
    assert "function showTempTableModalShell" in js
    assert "function teardownTempTableModal" in js
    assert "function isCurrentComparisonRequest" in js


def test_normal_first_upload_auto_open_only_after_needs_review():
    """Fluxo sem reset: primeira comparação só auto-abre em needs_review."""
    js = _js()
    auto = js[
        js.index("function shouldAutoOpenComparisonWizard") : js.index(
            "function maybeOpenComparisonWizardAfterStatus"
        )
    ]
    helper = js[
        js.index("function isReviewReadyTempTable") : js.index(
            "function renderComparisonWizardModal"
        )
    ]
    assert "PREPARE_TABLE_1" in auto
    assert "needs_review" in helper
    assert "view !== 'review'" in auto


def test_set_upload_loading_true_prefers_modal_over_page_status():
    http = _http_loading_block(_js())
    assert "if (on)" in http
    assert "isTempTableModalOpen()" in http
    assert "transitionComparisonFlowModal('uploading')" in http
    assert "setStatus(UPLOAD_PAGE_STATUS_SENDING)" in http


def test_set_upload_loading_false_clears_only_sending_not_processing():
    http = _http_loading_block(_js())
    assert "getUploadPageStatusText() === UPLOAD_PAGE_STATUS_SENDING" in http
    assert "setStatus('')" in http
    assert "setStatus(on ? UPLOAD_PAGE_STATUS_SENDING : '')" not in http
    assert "setStatus(on ? 'Enviando documento...' : '')" not in http


def test_complete_temp_table_processing_ui_contract():
    js = _js()
    helper = js[
        js.index("function completeTempTableProcessingUi") : js.index("function setUploadLoading")
    ]
    assert "uploadInFlight = false" in helper
    assert "clearTransientUploadPageStatus()" in helper
    assert "fetch(" not in helper
    assert "openComparisonWizardModal" not in helper
    assert "renderComparisonWizardModal" not in helper
    assert "setCurrentTempTable" not in helper


def test_sync_upload_page_status_clears_when_modal_open():
    sync = _js()[
        _js().index("function syncUploadPageStatusFromTempTable") : _js().index(
            "function completeTempTableProcessingUi"
        )
    ]
    assert "isTempTableModalOpen()" in sync
    assert "isComparisonWizardFlowActive()" in sync
    assert "setStatus('')" in sync
    assert "status === 'processing'" in sync
    assert "status === 'needs_review'" in sync
    assert "status === 'failed'" in sync


def test_handle_temp_table_transitions_open_modal_views():
    status = _js()[
        _js().index("function handleTempTableFromStatus") : _js().index("function formatBytes")
    ]
    assert "syncUploadPageStatusFromTempTable(tempTable)" in status
    assert "transitionComparisonFlowModal('processing')" in status
    assert "transitionComparisonFlowModal('review')" in status
    assert "transitionComparisonFlowModal('failed'" in status
    assert status.index("syncUploadPageStatusFromTempTable(tempTable)") < status.index(
        "maybeOpenComparisonWizardAfterStatus()"
    )


def test_upload_finally_transitions_modal_views():
    upload = _upload_block(_js())
    finally_body = upload.split(".finally(function ()", 1)[1]
    assert "setUploadLoading(false)" in finally_body
    assert "transitionComparisonFlowModal('processing')" in finally_body
    assert "transitionComparisonFlowModal('review')" in finally_body
    assert "maybeOpenComparisonWizardAfterStatus()" in finally_body
    assert "requestGenerationAtStart !== comparisonRequestGeneration" in finally_body


def test_single_official_modal_opener():
    js = _js()
    assert "function showTempTableModalShell()" in js
    assert "function renderAndShowComparisonFlowModal" in js
    shell = js[js.index("function showTempTableModalShell") : js.index("function rememberCarrierIdentifyPanelHome")]
    assert shell.count("modal.hidden = false") == 1
    # Nenhum call site paralelo no painel de transportadora
    open_carrier = js[
        js.index("function openCarrierIdentificationPanel") : js.index("function cancelCarrierIdentification")
    ]
    assert "modal.hidden = false" not in open_carrier
    assert "renderAndShowComparisonFlowModal('carrier_identification'" in open_carrier
    assert "agente-compara-temp-table-modal-open" not in open_carrier


def test_carrier_confirm_transitions_to_uploading_without_closing_modal():
    confirm = _js()[
        _js().index("function confirmCarrierIdentification") : _js().index(
            "function initCarrierIdentificationPanel"
        )
    ]
    assert "transitionComparisonFlowModal('uploading'" in confirm
    assert confirm.index("transitionComparisonFlowModal('uploading'") < confirm.index(
        "uploadDocument("
    )
    assert "closeCarrierIdentificationPanel()" in confirm


def test_flow_views_renderers_exist():
    js = _js()
    for name in (
        "renderCarrierIdentificationFlowView",
        "renderUploadingFlowView",
        "renderProcessingFlowView",
        "renderFailedFlowView",
        "renderReviewFlowView",
        "renderComparisonFlowView",
        "transitionComparisonFlowModal",
    ):
        assert "function " + name in js
    assert "Identifique a transportadora" in js
    assert "Enviando tabela de frete" in js
    assert "Processando tabela de frete" in js
    assert "Não foi possível processar a tabela" in js
    assert "Enviando documento..." in js
    assert "Estruturando tabela temporária..." in js


def test_review_render_clears_transient_before_shell_already_open_path():
    review = _js()[
        _js().index("function renderReviewFlowView") : _js().index("function renderComparisonFlowView")
    ]
    assert "completeTempTableProcessingUi()" in review
    assert "isReviewReadyTempTable(currentTempTable)" in review
    assert "setComparisonWizardModalHeader('review')" in review
    assert "updateTempTableModalFooter()" in review


def test_incompatible_states_guarded_by_technical_predicates():
    js = _js()
    auto = js[
        js.index("function shouldAutoOpenComparisonWizard") : js.index(
            "function maybeOpenComparisonWizardAfterStatus"
        )
    ]
    maybe = js[
        js.index("function maybeOpenComparisonWizardAfterStatus") : js.index(
            "function openComparisonWizardModal"
        )
    ]
    assert "if (uploadInFlight) return false;" in auto
    assert "view !== 'review'" in auto
    assert "isReviewReadyTempTable(currentTempTable)" in auto
    assert "if (uploadInFlight) return;" in maybe
    assert "transitionComparisonFlowModal('review')" in maybe


def test_full_visual_journey_continuous_modal_contract():
    """Jornada contínua: carrier → uploading → processing → review no mesmo modal."""
    js = _js()
    confirm = js[
        js.index("function confirmCarrierIdentification") : js.index(
            "function initCarrierIdentificationPanel"
        )
    ]
    upload = _upload_block(js)
    status = js[js.index("function handleTempTableFromStatus") : js.index("function formatBytes")]
    assert "transitionComparisonFlowModal('uploading'" in confirm
    assert "transitionComparisonFlowModal('processing')" in upload
    assert "transitionComparisonFlowModal('review')" in upload
    assert "transitionComparisonFlowModal('processing')" in status
    assert "transitionComparisonFlowModal('review')" in status
    assert "function showTempTableModalShell" in js
    # Única abertura oficial do root
    assert js.count("function showTempTableModalShell()") == 1


def test_visual_journey_after_reset_preserves_teardown_and_fresh_status():
    js = _js()
    reset = js[
        js.index("function resetAgenteComparaFrontendState") : js.index(
            "function cacheReviewTempTableIfOwned"
        )
    ]
    upload = _upload_block(js)
    assert "teardownTempTableModal()" in reset
    assert "bumpComparisonRequestGeneration()" in reset
    assert "setStatus('')" in reset
    assert "requestGenerationAtStart" in upload
    assert "function showTempTableModalShell" in js


def test_manual_open_uses_same_controller():
    js = _js()
    open_temp = js[js.index("function openTempTableModal") : js.index("function closeTempTableModal")]
    assert "openComparisonWizardModal()" in open_temp
    assert "renderAndShowComparisonFlowModal('processing')" in open_temp
    assert "renderAndShowComparisonFlowModal('review')" in open_temp
    assert "showTempTableModalShell()" in open_temp
    assert "modal.hidden = false" not in open_temp


def test_stale_upload_finally_does_not_clear_current_attempt_status():
    finally_body = _upload_block(_js()).split(".finally(function ()", 1)[1]
    assert "requestGenerationAtStart !== comparisonRequestGeneration" in finally_body
    assert finally_body.index("requestGenerationAtStart !== comparisonRequestGeneration") < finally_body.index(
        "setUploadLoading(false)"
    )
    assert "comparisonIdAtStart !== comparisonState.comparisonId" in finally_body


def test_failed_status_transitions_modal_and_clears_page_status():
    js = _js()
    status = js[js.index("function handleTempTableFromStatus") : js.index("function formatBytes")]
    assert "transitionComparisonFlowModal('failed'" in status
    sync = js[
        js.index("function syncUploadPageStatusFromTempTable") : js.index(
            "function completeTempTableProcessingUi"
        )
    ]
    assert "status === 'failed'" in sync
    assert "isTempTableModalOpen()" in sync
