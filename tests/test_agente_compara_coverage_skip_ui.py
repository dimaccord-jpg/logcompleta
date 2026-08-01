"""Contrato frontend: Agora não conclui cobertura via skip efetivo.

Nível: estático estrutural sobre app/static/js/agente_compara.js.
Prova o call graph e o pós-sucesso sem depender de jsdom.
"""
from __future__ import annotations

import pathlib
import re


def _js() -> str:
    return pathlib.Path("app/static/js/agente_compara.js").read_text(encoding="utf-8")


def _fn(js: str, name: str, next_name: str | None = None) -> str:
    start = js.index(f"function {name}")
    if next_name:
        end = js.index(f"function {next_name}", start + 1)
        return js[start:end]
    # fallback: até a próxima function no mesmo nível aproximado
    match = re.search(r"\n  function \w+", js[start + 1 :])
    end = start + 1 + match.start() if match else len(js)
    return js[start:end]


def test_agora_nao_delegates_to_skip_not_skipped_terminal_state():
    js = _js()
    prompt = _fn(js, "handleCoveragePromptAnswer", "uploadCoverageFile")
    assert "skipComparisonCoverageAndAdvance()" in prompt
    assert "renderCoverageSkippedState" not in prompt
    assert "Etapa ignorada" not in prompt
    assert "iniciar a auditoria" not in prompt
    # Decisão negativa não marca flags locais antes do backend
    false_path = prompt[prompt.index("return skipComparisonCoverageAndAdvance") :]
    assert "coveragePromptAnswered = true" not in false_path
    assert "coveragePromptAccepted = false" not in false_path


def test_accepted_true_preserves_upload_path():
    prompt = _fn(_js(), "handleCoveragePromptAnswer", "uploadCoverageFile")
    assert "coveragePromptAccepted = true" in prompt
    assert "showCoverageUploadArea()" in prompt
    assert "renderTempTableModalContent(currentTempTable)" in prompt


def test_skip_posts_skip_coverage_and_advance():
    skip = _fn(_js(), "skipComparisonCoverageAndAdvance", "appendOperationalMessage")
    assert "review_action: 'skip_coverage_and_advance'" in skip
    assert "API_TEMP_TABLE_SAVE" in skip
    assert "temp_table_id: expectedTempTableId" in skip or "temp_table_id:" in skip


def test_skip_success_syncs_activates_and_renders_immediately():
    skip = _fn(_js(), "skipComparisonCoverageAndAdvance", "appendOperationalMessage")
    assert "applyCoverageCompletionAndRender(res.data, { accepted: false })" in skip
    assert "activateComparisonCommonParamsStep" in _fn(
        _js(), "applyCoverageCompletionAndRender", "handleCoveragePromptAnswer"
    )
    # fetchDocuments é secundário — não é pré-requisito do render
    apply_idx = skip.index("applyCoverageCompletionAndRender")
    fetch_idx = skip.index("fetchDocuments()")
    assert apply_idx < fetch_idx
    # Não depende de handleStartAudit no caminho do Agora não
    prompt = _fn(_js(), "handleCoveragePromptAnswer", "uploadCoverageFile")
    assert "handleStartAudit" not in prompt


def test_apply_coverage_completion_sets_calculation_file_flags():
    apply = _fn(_js(), "applyCoverageCompletionAndRender", "handleCoveragePromptAnswer")
    assert "CALCULATION_FILE" in apply
    assert "coverageStepActive = false" in apply
    assert "coveragePromptAnswered = true" in apply
    assert "auditFileStepActive = true" in apply
    assert "tempTableModalActiveTab = 'audit'" in apply
    assert "activateComparisonCommonParamsStep('CALCULATION_FILE')" in apply
    assert "syncComparisonStateFromPayload" in apply


def test_no_inherited_auditoria_terminal_message_in_active_path():
    js = _js()
    assert "Etapa ignorada. Você poderá iniciar a auditoria" not in js
    assert "function renderCoverageSkippedState" not in js
    assert "function renderCoverageAdvancingState" in js
    advancing = _fn(js, "renderCoverageAdvancingState", "renderEditableCoverageTable")
    assert "Avançando para o arquivo de comparação..." in advancing
    tab = _fn(js, "renderCoverageTabContent", "renderFreightTabContent")
    assert "renderCoverageAdvancingState" in tab
    assert "renderCoverageSkippedState" not in tab


def test_skip_guards_duplicate_click_and_stale_response():
    skip = _fn(_js(), "skipComparisonCoverageAndAdvance", "appendOperationalMessage")
    assert "tempTableSaveInFlight" in skip
    assert "isCurrentComparisonRequest" in skip
    assert "stale: true" in skip
    assert "Promise.resolve({ ok: false, blocked: true })" in skip
    decision = _fn(_js(), "renderCoverageDecisionCard", "renderCoverageAdvancingState")
    assert "aria-busy" in decision
    assert "yesBtn.disabled = busy" in decision
    assert "noBtn.disabled = busy" in decision


def test_skip_error_restores_coverage_actions_after_inflight():
    skip = _fn(_js(), "skipComparisonCoverageAndAdvance", "appendOperationalMessage")
    error_branch = skip[
        skip.index("if (!res.data || res.data.ok !== true)") : skip.index(
            "var applied = applyCoverageCompletionAndRender"
        )
    ]
    assert "applyCoverageCompletionAndRender" not in error_branch
    assert "activateComparisonCommonParamsStep" not in error_branch
    assert "setTempTableModalError" in error_branch
    assert "tempTableSaveInFlight = false" in skip
    finally_block = skip[skip.rindex(".finally") :]
    assert "renderTempTableModalContent(currentTempTable)" in finally_block
    assert "coveragePromptAnswered" in finally_block


def test_open_modal_preserves_configuration_flow_step():
    open_fn = _fn(_js(), "openTempTableModal", "closeTempTableModal")
    assert "isComparisonConfigurationFlow()" in open_fn
    assert open_fn.index("isComparisonConfigurationFlow()") < open_fn.index("needs_review")
    assert "activateComparisonCommonParamsStep(comparisonState.currentStep)" in open_fn
