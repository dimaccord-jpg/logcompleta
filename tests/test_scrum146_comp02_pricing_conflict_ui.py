"""SCRUM-146 COMP-02: a interface preserva CONFLICTING_LOOKUP_KEY durante a edição."""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
JS_PATH = ROOT / "app" / "static" / "js" / "agente_compara.js"

_RUNNER = r"""
'use strict';

const fs = require('fs');
const vm = require('vm');

const sourcePath = process.argv[2];
let source = fs.readFileSync(sourcePath, 'utf8');
const marker = "  if (document.readyState === 'loading') {";
const exportHook = `
  window.__agenteComparaPricingConflictTest = {
    configure: function (state) {
      comparisonState.comparisonId = state.currentTempTable.comparison_id;
      comparisonState.activeTableId = state.currentTempTable.table_id;
      comparisonState.currentStep = 'PREPARE_TABLE_1';
      comparisonState.tables = [{
        table_id: state.currentTempTable.table_id,
        slot_number: 1,
        status: 'needs_review',
        confirmed: false,
        carrier_name: 'COMP-02'
      }];
      tempTableEditMode = false;
      tempTableValidationErrors = [];
      setCurrentTempTable(state.currentTempTable, { refreshAuditBi: false });
    },
    applyBackend: function (payload) {
      return handleBackendTempTableValidationErrors(payload);
    },
    refreshAccessorial: function () {
      refreshTempTableValidationErrorsAfterAccessorialEdit();
    },
    editGrupo: function (value) {
      function collectInputs(node, found) {
        if (!node) return;
        if (node.tagName === 'INPUT' && String(node.className || '').indexOf('cell-input') !== -1) {
          found.push(node);
        }
        (node.children || []).forEach(function (child) { collectInputs(child, found); });
      }
      const inputs = [];
      collectInputs(document.getElementById('agenteComparaTempTableModalBody'), inputs);
      const grupo = inputs[1];
      if (!grupo) throw new Error('campo Grupo não encontrado');
      grupo.value = value;
      (grupo.listeners.input || []).forEach(function (fn) {
        fn({ target: grupo });
      });
    },
    editRouteCell: function (rowIndex, headerLabel, value) {
      const table = findRenderedFreightTable();
      const located = locateRouteCell(table, rowIndex, headerLabel);
      located.input.value = value;
      (located.input.listeners.input || []).forEach(function (fn) {
        fn({ target: located.input });
      });
      return routeModel();
    },
    deleteRoute: function (rowIndex) {
      const table = findRenderedFreightTable();
      const tbody = (table.children || []).filter(function (node) { return node.tagName === 'TBODY'; })[0];
      const row = tbody && tbody.children ? tbody.children[rowIndex] : null;
      if (!row) throw new Error('rota não encontrada para exclusão');
      let button = null;
      (row.children || []).forEach(function (cell) {
        (cell.children || []).forEach(function (node) {
          if (node.tagName === 'BUTTON' && String(node.textContent || '') === 'Excluir') button = node;
        });
      });
      if (!button) throw new Error('ação de excluir rota não encontrada');
      (button.listeners.click || []).forEach(function (fn) { fn({}); });
      return routeModel();
    },
    applyConfirmedBackend: function () {
      const next = deepCloneTempTable(currentTempTable);
      if (!next) return false;
      next.validation = {
        schema_version: 1,
        can_confirm: true,
        blocking_count: 0,
        warning_count: 0,
        blocking_issues: [],
        warnings: []
      };
      setCurrentTempTable(next, { refreshAuditBi: false });
      clearTempTableValidationErrors();
      tempTableEditMode = false;
      tempTableEditSnapshot = null;
      resetTempTableSaveExecutionId();
      updateTempTableModalFooter();
      return tempTableConfirmationCanProceed() === true && tempTableEditMode === false;
    },
    acceptClean: function () {
      currentTempTable.validation = {
        schema_version: 1,
        can_confirm: true,
        blocking_count: 0,
        warning_count: 0,
        blocking_issues: [],
        warnings: []
      };
      setCurrentTempTable(currentTempTable, { refreshAuditBi: false });
      clearTempTableValidationErrors();
      tempTableEditMode = false;
      updateTempTableModalFooter();
      return tempTableConfirmationCanProceed();
    },
    snapshot: function () {
      return {
        editMode: !!tempTableEditMode,
        canProceed: tempTableConfirmationCanProceed(),
        saveBlocked: (function () {
          var saveBtn = document.getElementById('agenteComparaTempTableModalSave');
          return !!(saveBtn && (saveBtn.disabled || saveBtn.getAttribute('aria-disabled') === 'true'));
        })(),
        validationText: (function () {
          function collect(node, parts) {
            if (!node) return;
            if (node.children && node.children.length) {
              node.children.forEach(function (child) { collect(child, parts); });
              return;
            }
            if (node.textContent) parts.push(String(node.textContent));
          }
          var parts = [];
          collect(document.getElementById('agenteComparaTempTableModalValidation'), parts);
          return parts.join('\\n');
        })(),
        pricingCodes: (function () {
          function collect(node, codes) {
            if (!node) return;
            if (node.getAttribute && node.getAttribute('data-pricing-code')) {
              codes.push(node.getAttribute('data-pricing-code'));
            }
            (node.children || []).forEach(function (child) { collect(child, codes); });
          }
          var codes = [];
          collect(document.getElementById('agenteComparaTempTableModalValidation'), codes);
          return codes;
        })(),
        saveLabel: (function () {
          var saveBtn = document.getElementById('agenteComparaTempTableModalSave');
          return saveBtn ? String(saveBtn.textContent || '') : '';
        })(),
        status: currentTempTable ? String(currentTempTable.status || '') : '',
        storedCanConfirm: !!(currentTempTable && currentTempTable.validation && currentTempTable.validation.can_confirm === true),
        confirmed: !!(comparisonState.tables && comparisonState.tables[0] && comparisonState.tables[0].confirmed),
        routes: routeModel()
      };
    }
  };

  function routeModel() {
    var routes = currentTempTable && Array.isArray(currentTempTable.freight_routes) ? currentTempTable.freight_routes : [];
    return routes.map(function (route) {
      return {
        destination: route && route.destination != null ? String(route.destination) : '',
        weight_10: route && route.weight_10 != null ? String(route.weight_10) : '',
        weight_30: route && route.weight_30 != null ? String(route.weight_30) : ''
      };
    });
  }

  function findRenderedFreightTable() {
    var body = document.getElementById('agenteComparaTempTableModalBody');
    var found = [];
    function walk(node) {
      if (!node) return;
      if (node.tagName === 'TABLE' && String(node.className || '').indexOf('freight-table') !== -1) {
        found.push(node);
      }
      (node.children || []).forEach(walk);
    }
    walk(body);
    if (!found[0]) throw new Error('tabela de rotas não renderizada');
    return found[0];
  }

  function locateRouteCell(table, rowIndex, headerLabel) {
    var thead = (table.children || []).filter(function (node) { return node.tagName === 'THEAD'; })[0];
    var headerRow = thead && thead.children ? thead.children[0] : null;
    if (!headerRow) throw new Error('cabeçalho da tabela de rotas ausente');
    var headers = (headerRow.children || []).map(function (cell) { return String(cell.textContent || ''); });
    var colIndex = headers.indexOf(headerLabel);
    if (colIndex < 0) throw new Error('coluna não encontrada: ' + headerLabel);
    var tbody = (table.children || []).filter(function (node) { return node.tagName === 'TBODY'; })[0];
    var row = tbody && tbody.children ? tbody.children[rowIndex] : null;
    if (!row) throw new Error('rota não encontrada: ' + rowIndex);
    var cell = row.children[colIndex];
    var input = cell && (cell.children || []).filter(function (node) { return node.tagName === 'INPUT'; })[0];
    if (!input) throw new Error('editor real da coluna ' + headerLabel + ' não encontrado');
    return { input: input, headers: headers };
  }

  return;
`;
if (!source.includes(marker)) {
  throw new Error('ponto de injeção do harness não encontrado');
}
source = source.replace(marker, exportHook + marker);

function makeClassList(el) {
  const names = () => String(el.className || '').split(/\s+/).filter(Boolean);
  return {
    add(name) {
      const current = names();
      if (current.indexOf(name) === -1) current.push(name);
      el.className = current.join(' ');
    },
    remove(name) {
      el.className = names().filter((item) => item !== name).join(' ');
    },
    contains(name) {
      return names().indexOf(name) !== -1;
    },
    toggle(name, force) {
      const has = this.contains(name);
      const should = force == null ? !has : !!force;
      if (should) this.add(name);
      else this.remove(name);
      return should;
    },
  };
}

function createElement(tag) {
  const el = {
    tagName: String(tag || '').toUpperCase(),
    className: '',
    textContent: '',
    value: '',
    type: '',
    placeholder: '',
    hidden: false,
    id: '',
    scope: '',
    disabled: false,
    children: [],
    parentNode: null,
    attrs: {},
    listeners: {},
    style: {},
    appendChild(child) {
      if (!child) return child;
      if (child.parentNode && child.parentNode !== el) child.parentNode.removeChild(child);
      el.children.push(child);
      child.parentNode = el;
      return child;
    },
    removeChild(child) {
      const index = el.children.indexOf(child);
      if (index >= 0) el.children.splice(index, 1);
      if (child) child.parentNode = null;
      return child;
    },
    replaceChildren() {
      el.children.forEach((child) => {
        if (child) child.parentNode = null;
      });
      el.children = [];
      Array.from(arguments).forEach((child) => {
        if (child) el.appendChild(child);
      });
    },
    setAttribute(name, value) {
      el.attrs[String(name)] = String(value);
      if (name === 'id') {
        el.id = String(value);
        byIdMap[el.id] = el;
      }
    },
    getAttribute(name) {
      return Object.prototype.hasOwnProperty.call(el.attrs, name) ? el.attrs[name] : null;
    },
    addEventListener(type, fn) {
      (el.listeners[type] = el.listeners[type] || []).push(fn);
    },
    removeEventListener() {},
    focus() {},
    scrollIntoView() {},
    remove() {
      if (el.parentNode) el.parentNode.removeChild(el);
    },
    querySelector(sel) {
      return el.querySelectorAll(sel)[0] || null;
    },
    querySelectorAll(sel) {
      const found = [];
      walk(el, (node) => {
        if (node !== el && matches(node, sel)) found.push(node);
      });
      return found;
    },
  };
  Object.defineProperty(el, 'childNodes', { get: () => el.children });
  Object.defineProperty(el, 'firstChild', { get: () => el.children[0] || null });
  Object.defineProperty(el, 'innerHTML', {
    get: () => '',
    set: () => {
      el.children = [];
    },
  });
  el.classList = makeClassList(el);
  return el;
}

const byIdMap = {};

function matches(node, sel) {
  const selector = String(sel || '').trim();
  if (!node || !node.tagName || !selector) return false;
  if (selector.charAt(0) === '#') return node.id === selector.slice(1);
  if (selector.charAt(0) === '.') {
    return (' ' + String(node.className || '') + ' ').indexOf(' ' + selector.slice(1) + ' ') !== -1;
  }
  if (selector.charAt(0) === '[') {
    const match = selector.match(/^\[([^\]=]+)(?:=["']?([^"'\]]+)["']?)?\]$/);
    if (!match || !node.getAttribute) return false;
    const value = node.getAttribute(match[1]);
    if (match[2] == null) return value != null;
    return value === match[2];
  }
  return node.tagName === selector.toUpperCase();
}

function walk(node, visit) {
  if (!node || typeof node !== 'object') return;
  visit(node);
  (node.children || []).forEach((child) => walk(child, visit));
}

const document = {
  readyState: 'complete',
  body: null,
  documentElement: null,
  createElement,
  createDocumentFragment: () => createElement('fragment'),
  getElementById: (id) => byIdMap[id] || null,
  addEventListener() {},
  removeEventListener() {},
  querySelector(sel) {
    return document.querySelectorAll(sel)[0] || null;
  },
  querySelectorAll(sel) {
    const found = [];
    [document.documentElement, document.body].filter(Boolean).forEach((root) => {
      walk(root, (node) => {
        if (node !== root && matches(node, sel)) found.push(node);
      });
    });
    return found;
  },
};

document.body = createElement('body');
document.documentElement = createElement('html');
document.documentElement.appendChild(document.body);
const modalBody = createElement('div');
modalBody.id = 'agenteComparaTempTableModalBody';
byIdMap[modalBody.id] = modalBody;
document.body.appendChild(modalBody);
[
  'agenteComparaTempTableModalValidation',
  'agenteComparaTempTableModalSave',
  'agenteComparaTempTableModalError',
  'agenteComparaTempTableModalEdit',
  'agenteComparaTempTableModalCancelEdit',
].forEach((id) => {
  const node = createElement(id === 'agenteComparaTempTableModalValidation' || id === 'agenteComparaTempTableModalError' ? 'div' : 'button');
  node.id = id;
  byIdMap[id] = node;
  document.body.appendChild(node);
});

const windowProxy = {
  document,
  setTimeout: (fn) => (typeof fn === 'function' ? fn() : 0),
  clearTimeout() {},
  addEventListener() {},
  removeEventListener() {},
  confirm: () => true,
  crypto: { randomUUID: () => 'test-uuid' },
  getComputedStyle: () => ({ getPropertyValue: () => '' }),
};
global.window = windowProxy;
global.document = document;

vm.runInNewContext(source, {
  window: windowProxy,
  document,
  console,
  setTimeout: windowProxy.setTimeout,
  clearTimeout: windowProxy.clearTimeout,
}, { filename: 'agente_compara.js' });

const api = windowProxy.__agenteComparaPricingConflictTest;
if (!api) throw new Error('harness de conflito tarifário não foi exposto');

function pricingIssue(label, message) {
  return {
    code: 'CONFLICTING_LOOKUP_KEY',
    section: 'pricing_contract',
    item_id: 'pricing:' + label,
    index: 1,
    field: 'lookup_keys',
    label: label,
    reason_code: 'conflicting_lookup_key',
    severity: 'blocking',
    message: message,
  };
}

function backendPayload(issues, canConfirm) {
  return {
    ok: false,
    error: 'TEMP_TABLE_HAS_BLOCKING_ISSUES',
    error_code: 'invalid_pricing_contract',
    message: 'A tabela tarifária não pode ser confirmada. Revise as regras de frete antes de avançar.',
    validation: {
      schema_version: 1,
      can_confirm: canConfirm,
      blocking_count: issues.length,
      warning_count: 0,
      blocking_issues: issues,
      warnings: [],
    },
  };
}

const specificMessage = 'Há duas regras conflitantes para o mesmo destino.';
const revisedMessage = 'A chave Leste ainda possui duas regras conflitantes.';
const scenario = process.argv[3] || 'tables';
const report = {};

function cleanValidation() {
  return {
    schema_version: 1,
    can_confirm: true,
    blocking_count: 0,
    warning_count: 0,
    blocking_issues: [],
    warnings: [],
  };
}

if (scenario === 'tables') {
  api.configure({
    currentTempTable: {
      temp_table_id: 'tt-comp-02',
      comparison_id: 'cmp-comp-02',
      table_id: 'tbl-comp-02',
      status: 'needs_review',
      carrier_name: 'COMP-02',
      freight_tables: [{
        table_title: 'Tabela unica',
        columns: ['Origem', 'Grupo', 'Até 10 kg'],
        rows: [
          { Origem: 'Campinas', Grupo: 'Capital', 'Até 10 kg': '10,00' },
          { Origem: 'Guarulhos', Grupo: 'Capital', 'Até 10 kg': '20,00' },
        ],
      }],
      freight_routes: [],
      accessorial_fees: [],
      validation: cleanValidation(),
    },
  });

  report.applied = api.applyBackend(backendPayload([
    pricingIssue('Capital', specificMessage),
  ], false));
  report.afterConflict = api.snapshot();
  api.refreshAccessorial();
  report.afterAccessorialRefresh = api.snapshot();
  api.editGrupo('Interior');
  report.afterFreightEdit = api.snapshot();
  report.revalidatedConflict = api.applyBackend(backendPayload([
    pricingIssue('Leste', revisedMessage),
  ], false));
  report.afterRevalidatedConflict = api.snapshot();
  report.revalidatedClear = api.acceptClean();
  report.afterRevalidatedClear = api.snapshot();
} else if (scenario === 'routes') {
  function routeTable() {
    return {
      temp_table_id: 'tt-comp-02-routes',
      comparison_id: 'cmp-comp-02-routes',
      table_id: 'tbl-comp-02-routes',
      status: 'needs_review',
      carrier_name: 'COMP-02',
      freight_tables: [],
      freight_routes: [
        {
          origin: 'Campinas',
          destination: 'Capital',
          weight_30: '10,00',
          weight_10: '10,00',
          column_labels: { weight_10: 'Até 10 kg' },
        },
        {
          origin: 'Guarulhos',
          destination: 'Capital',
          weight_30: '20,00',
          weight_10: '20,00',
          column_labels: { weight_10: 'Até 10 kg' },
        },
      ],
      accessorial_fees: [],
      validation: cleanValidation(),
    };
  }

  api.configure({ currentTempTable: routeTable() });
  report.applied = api.applyBackend(backendPayload([
    pricingIssue('Capital', specificMessage),
  ], false));
  report.afterConflict = api.snapshot();

  report.sameValueEdit = api.editRouteCell(0, 'Destino', 'Capital');
  report.afterSameValue = api.snapshot();

  report.stillConflictingEdit = api.editRouteCell(0, 'Até 10 kg', '15,00');
  report.afterStillConflictingEdit = api.snapshot();
  report.stillConflictingBackend = api.applyBackend(backendPayload([
    pricingIssue('Capital', specificMessage),
  ], false));
  report.afterStillConflictingBackend = api.snapshot();

  report.resolvingEdit = api.editRouteCell(0, 'Destino', 'Interior');
  report.afterResolvingEdit = api.snapshot();
  report.revalidatedConflict = api.applyBackend(backendPayload([
    pricingIssue('Leste', revisedMessage),
  ], false));
  report.afterRevalidatedConflict = api.snapshot();
  report.backendAccepted = api.applyConfirmedBackend();
  report.afterBackendAccepted = api.snapshot();

  api.configure({ currentTempTable: routeTable() });
  report.deleteApplied = api.applyBackend(backendPayload([
    pricingIssue('Capital', specificMessage),
  ], false));
  report.afterDeleteConflict = api.snapshot();
  report.deleteRoute = api.deleteRoute(0);
  report.afterDelete = api.snapshot();
} else {
  throw new Error('cenário desconhecido: ' + scenario);
}
process.stdout.write(JSON.stringify(report));
"""


def _node_executable() -> str:
    found = shutil.which("node")
    if found:
        return found
    bundled = Path.home() / "AppData/Local/Programs/cursor/resources/app/resources/helpers/node.exe"
    if bundled.is_file():
        return str(bundled)
    pytest.fail("Node.js é obrigatório para reproduzir a mensagem de conflito tarifário.")


def _run_conflict_scenario(scenario: str = "tables") -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "pricing_conflict_runner.js"
        runner.write_text(_RUNNER, encoding="utf-8")
        result = subprocess.run(
            [_node_executable(), str(runner), str(JS_PATH), scenario],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(
            "Falha ao executar a revisão tarifária.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def test_conflicting_lookup_message_survives_edit_and_clears_when_revalidated():
    report = _run_conflict_scenario()
    specific = "Há duas regras conflitantes para o mesmo destino."
    revised = "A chave Leste ainda possui duas regras conflitantes."

    assert report["applied"] is True
    conflict = report["afterConflict"]
    assert conflict["editMode"] is True
    assert conflict["canProceed"] is False
    assert conflict["saveBlocked"] is True
    assert conflict["pricingCodes"] == ["CONFLICTING_LOOKUP_KEY"]
    assert "Capital" in conflict["validationText"]
    assert specific in conflict["validationText"]

    preserved = report["afterAccessorialRefresh"]
    assert preserved["editMode"] is True
    assert preserved["canProceed"] is False
    assert preserved["saveBlocked"] is True
    assert "Capital" in preserved["validationText"]
    assert specific in preserved["validationText"]

    edited = report["afterFreightEdit"]
    assert edited["canProceed"] is True
    assert edited["saveBlocked"] is False
    assert "Capital" not in edited["validationText"]
    assert specific not in edited["validationText"]

    assert report["revalidatedConflict"] is True
    again = report["afterRevalidatedConflict"]
    assert again["canProceed"] is False
    assert again["saveBlocked"] is True
    assert again["pricingCodes"] == ["CONFLICTING_LOOKUP_KEY"]
    assert "Leste" in again["validationText"]
    assert revised in again["validationText"]
    assert "Capital" not in again["validationText"]
    assert specific not in again["validationText"]

    assert report["revalidatedClear"] is True
    cleared = report["afterRevalidatedClear"]
    assert cleared["canProceed"] is True
    assert cleared["saveBlocked"] is False
    assert cleared["pricingCodes"] == []
    assert "Leste" not in cleared["validationText"]
    assert revised not in cleared["validationText"]


def test_freight_route_edit_reopens_backend_validation_without_confirming():
    report = _run_conflict_scenario("routes")
    specific = "Há duas regras conflitantes para o mesmo destino."
    revised = "A chave Leste ainda possui duas regras conflitantes."

    assert report["applied"] is True
    conflict = report["afterConflict"]
    assert conflict["editMode"] is True
    assert conflict["canProceed"] is False
    assert conflict["saveBlocked"] is True
    assert conflict["saveLabel"] == "Salvar e Avançar"
    assert conflict["storedCanConfirm"] is False
    assert conflict["confirmed"] is False
    assert conflict["status"] == "needs_review"
    assert conflict["pricingCodes"] == ["CONFLICTING_LOOKUP_KEY"]
    assert "Capital" in conflict["validationText"]
    assert specific in conflict["validationText"]
    assert [route["destination"] for route in conflict["routes"]] == ["Capital", "Capital"]

    same_value = report["afterSameValue"]
    assert report["sameValueEdit"][0]["destination"] == "Capital"
    assert same_value["canProceed"] is False
    assert same_value["saveBlocked"] is True
    assert same_value["storedCanConfirm"] is False
    assert same_value["editMode"] is True
    assert "Capital" in same_value["validationText"]
    assert specific in same_value["validationText"]

    assert report["stillConflictingEdit"][0]["weight_10"] == "15,00"
    assert [route["destination"] for route in report["stillConflictingEdit"]] == ["Capital", "Capital"]
    still_open = report["afterStillConflictingEdit"]
    assert still_open["editMode"] is True
    assert still_open["storedCanConfirm"] is False
    assert still_open["confirmed"] is False
    assert still_open["status"] == "needs_review"
    assert still_open["canProceed"] is True
    assert still_open["saveBlocked"] is False
    assert still_open["saveLabel"] == "Salvar e Avançar"
    assert specific not in still_open["validationText"]
    assert "Capital" not in still_open["validationText"]

    assert report["stillConflictingBackend"] is True
    still_blocked = report["afterStillConflictingBackend"]
    assert still_blocked["editMode"] is True
    assert still_blocked["storedCanConfirm"] is False
    assert still_blocked["confirmed"] is False
    assert still_blocked["canProceed"] is False
    assert still_blocked["saveBlocked"] is True
    assert still_blocked["pricingCodes"] == ["CONFLICTING_LOOKUP_KEY"]
    assert "Capital" in still_blocked["validationText"]
    assert specific in still_blocked["validationText"]
    assert still_blocked["routes"][0]["weight_10"] == "15,00"

    assert report["resolvingEdit"][0]["destination"] == "Interior"
    resolved = report["afterResolvingEdit"]
    assert resolved["editMode"] is True
    assert resolved["storedCanConfirm"] is False
    assert resolved["confirmed"] is False
    assert resolved["status"] == "needs_review"
    assert resolved["canProceed"] is True
    assert resolved["saveBlocked"] is False
    assert resolved["saveLabel"] == "Salvar e Avançar"
    assert specific not in resolved["validationText"]
    assert "Capital" not in resolved["validationText"]
    assert resolved["routes"][0]["destination"] == "Interior"
    assert resolved["routes"][1]["destination"] == "Capital"

    assert report["revalidatedConflict"] is True
    again = report["afterRevalidatedConflict"]
    assert again["editMode"] is True
    assert again["storedCanConfirm"] is False
    assert again["confirmed"] is False
    assert again["canProceed"] is False
    assert again["saveBlocked"] is True
    assert again["pricingCodes"] == ["CONFLICTING_LOOKUP_KEY"]
    assert "Leste" in again["validationText"]
    assert revised in again["validationText"]
    assert specific not in again["validationText"]
    assert again["routes"][0]["destination"] == "Interior"

    assert report["backendAccepted"] is True
    accepted = report["afterBackendAccepted"]
    assert accepted["editMode"] is False
    assert accepted["storedCanConfirm"] is True
    assert accepted["confirmed"] is False
    assert accepted["canProceed"] is True
    assert accepted["saveBlocked"] is False
    assert accepted["pricingCodes"] == []
    assert revised not in accepted["validationText"]
    assert specific not in accepted["validationText"]

    delete_conflict = report["afterDeleteConflict"]
    assert delete_conflict["saveBlocked"] is True
    assert specific in delete_conflict["validationText"]
    assert report["deleteRoute"] == [
        {"destination": "Capital", "weight_10": "20,00", "weight_30": "20,00"}
    ]
    deleted = report["afterDelete"]
    assert deleted["editMode"] is True
    assert deleted["storedCanConfirm"] is False
    assert deleted["confirmed"] is False
    assert deleted["canProceed"] is True
    assert deleted["saveBlocked"] is False
    assert specific not in deleted["validationText"]
    assert deleted["routes"] == [
        {"destination": "Capital", "weight_10": "20,00", "weight_30": "20,00"}
    ]
