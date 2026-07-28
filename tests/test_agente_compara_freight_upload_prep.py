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
            "function cancelCarrierIdentification"
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
    assert "Enviando documento..." in http
    assert "Preparando envio..." in prep


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
