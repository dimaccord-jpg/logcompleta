"""Identidade real das linhas visíveis de adicionais na revisão do AgenteCompara.

Reproduz o bloqueio da SCRUM-146: um frete principal oculto desloca o índice
visual, e excluir/editar a linha visível não pode atingir outro item.
"""
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
  window.__agenteComparaAccessorialTest = {
    configure: function (state) {
      currentTempTable = state.currentTempTable;
      currentCalculationBases = state.calculationBases || [];
      tempTableEditMode = true;
      tempTableValidationErrors = [];
      comparisonState.comparisonId = state.currentTempTable.comparison_id;
      comparisonState.activeTableId = state.currentTempTable.table_id;
      comparisonState.currentStep = 'PREPARE_TABLE_1';
      comparisonState.tables = [{
        table_id: state.currentTempTable.table_id,
        slot_number: 1,
        status: 'needs_review',
        confirmed: false,
        carrier_name: 'COMP-03A'
      }];
    },
    render: function () {
      renderTempTableModalContent(currentTempTable);
    },
    fees: function () {
      return (currentTempTable.accessorial_fees || []).map(function (fee) {
        return {
          fee_id: fee.fee_id,
          name: fee.name,
          value: fee.value,
          unit: fee.unit,
          notes: fee.notes,
          scope: fee.scope,
          calculation_base_id: fee.calculation_base_id || null,
          sentinel: fee.sentinel || null
        };
      });
    },
    errors: function () {
      return collectTempTableAdvanceValidationErrors().map(function (error) {
        return { name: error.name, index: error.index, reason_code: error.reason_code };
      });
    },
    canProceed: function () {
      return tempTableConfirmationCanProceed();
    },
    blockingMessage: function () {
      var validation = resolveTempTableValidationState();
      return tempTableBlockingCountMessage(Number(validation && validation.blocking_count) || 0);
    },
    saveBlocked: function () {
      var saveBtn = document.getElementById('agenteComparaTempTableModalSave');
      return !!(saveBtn && (saveBtn.disabled || saveBtn.getAttribute('aria-disabled') === 'true'));
    },
    saveLabel: function () {
      var saveBtn = document.getElementById('agenteComparaTempTableModalSave');
      return saveBtn ? String(saveBtn.textContent || '') : '';
    }
  };
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
['agenteComparaTempTableModalValidation', 'agenteComparaTempTableModalSave', 'agenteComparaTempTableModalEdit', 'agenteComparaTempTableModalCancelEdit'].forEach((id) => {
  const node = createElement(id === 'agenteComparaTempTableModalValidation' ? 'div' : 'button');
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

const api = windowProxy.__agenteComparaAccessorialTest;
if (!api) throw new Error('harness de adicionais não foi exposto');

function accessorialRows() {
  const rows = [];
  walk(modalBody, (node) => {
    if (node.tagName === 'TR' && node.getAttribute && node.getAttribute('data-accessorial-fee-index') != null) {
      rows.push(node);
    }
  });
  return rows;
}

function inputsWithoutField(row) {
  const found = [];
  walk(row, (node) => {
    if (node.tagName === 'INPUT' && !node.getAttribute('data-field')) found.push(node);
  });
  return found;
}

function rowByName(name) {
  return accessorialRows().find((row) => {
    const input = inputsWithoutField(row)[0];
    return input && input.value === name;
  }) || null;
}

function field(row, name) {
  let found = null;
  walk(row, (node) => {
    if (!found && node.getAttribute && node.getAttribute('data-field') === name) found = node;
  });
  return found;
}

function rowText(row) {
  const parts = [];
  walk(row, (node) => {
    if (['INPUT', 'SELECT', 'BUTTON'].indexOf(node.tagName) !== -1) return;
    if (node.textContent && (!node.children || node.children.length === 0)) parts.push(String(node.textContent));
  });
  return parts.join('\n');
}

function fire(node, type) {
  ((node && node.listeners && node.listeners[type]) || []).slice().forEach((fn) => {
    fn({ target: node, preventDefault() {}, stopPropagation() {} });
  });
}

function setInput(input, value) {
  if (!input) throw new Error('campo ausente');
  input.value = value;
  fire(input, 'input');
}

function deleteRow(name) {
  const row = rowByName(name);
  if (!row) throw new Error('linha visível não encontrada: ' + name);
  let button = null;
  walk(row, (node) => {
    if (!button && node.tagName === 'BUTTON' && node.textContent === 'Excluir') button = node;
  });
  if (!button) throw new Error('botão excluir ausente: ' + name);
  fire(button, 'click');
}

function blocking(feeId, name, extra) {
  return Object.assign({
    fee_id: feeId,
    name,
    value: '',
    unit: '',
    calculation_basis: 'não mapeado / revisar',
    calculation_base_id: null,
    classification_source: 'unmapped_calculation_base',
    status: 'needs_review',
    notes: '',
    scope: 'general',
  }, extra || {});
}

function buildTable() {
  return {
    temp_table_id: 'tt-comp-03a',
    comparison_id: 'cmp-comp-03a',
    table_id: 'tbl-comp-03a',
    status: 'needs_review',
    carrier_name: 'COMP-03A',
    freight_tables: [],
    freight_routes: [],
    accessorial_fees: [
      {
        fee_id: 'primary-peso',
        sentinel: 'keep-primary-peso',
        name: 'Frete peso',
        value: '1,50',
        unit: 'R$/kg',
        calculation_basis: 'por kg',
        notes: 'frete principal',
        scope: 'general',
      },
      blocking('gris', 'GRIS', {
        component_group: 'risk_management',
        canonical_component: 'risk_management',
        modifier_type: 'base_fee',
      }),
      {
        fee_id: 'primary-valor',
        sentinel: 'keep-primary-valor',
        name: 'Frete valor',
        value: '0,30%',
        unit: '%',
        calculation_basis: 'sobre a nf',
        notes: 'frete principal',
        scope: 'general',
      },
      blocking('advalorem', 'Ad Valorem'),
      blocking('pedagio', 'Pedágio'),
      blocking('tde', 'TDE/TDA'),
      blocking('cubagem', 'Cubagem'),
      {
        fee_id: 'minimo',
        name: 'Frete mínimo',
        value: '10,00',
        unit: 'R$',
        minimum_amount: 10,
        modifier_type: 'minimum_amount',
        calculation_type: 'minimum_amount',
        related_to: 'risk_management',
        component_group: 'risk_management',
        canonical_component: 'risk_management',
        classification_source: 'legacy_classifier',
        status: 'calculable',
        notes: '',
        scope: 'general',
      },
    ],
    validation: {
      schema_version: 1,
      can_confirm: false,
      blocking_count: 1,
      warning_count: 0,
      blocking_issues: [],
      warnings: [],
    },
  };
}

const calculationBases = [{
  id: 'pct_nota_fiscal',
  label: '% por nota fiscal',
  unit: '%',
  calculation_type: 'invoice_percentage',
  audit_variable: 'valor_nf',
  operation: 'percentage_of_variable',
  parameters: {},
}];

function configure() {
  api.configure({
    currentTempTable: JSON.parse(JSON.stringify(buildTable())),
    calculationBases,
  });
  api.render();
}

const report = {};
configure();
const adRow = rowByName('Ad Valorem');
if (!adRow) throw new Error('Ad Valorem não apareceu na lista visível');
report.adValoremOriginalIndex = adRow.getAttribute('data-accessorial-fee-index');
report.visibleNamesBeforeEdit = accessorialRows().map((row) => inputsWithoutField(row)[0].value);
report.hiddenPrimaryShown = report.visibleNamesBeforeEdit.indexOf('Frete peso') !== -1
  || report.visibleNamesBeforeEdit.indexOf('Frete valor') !== -1;

const plain = inputsWithoutField(adRow);
setInput(plain[0], 'Ad Valorem revisado');
setInput(field(adRow, 'value'), '2,50');
setInput(field(adRow, 'unit'), 'R$');
setInput(field(adRow, 'notes'), 'obs ad');
setInput(plain[1], 'rota');
const select = field(adRow, 'calculation_base_id');
if (!select) throw new Error('base de cálculo de Ad Valorem ausente');
select.value = 'pct_nota_fiscal';
fire(select, 'change');

const minimumRow = rowByName('Frete mínimo');
if (!minimumRow) throw new Error('Frete mínimo não apareceu na lista visível');
report.minimumLabel = rowText(minimumRow);
setInput(field(minimumRow, 'value'), '12,00');
report.afterEdit = api.fees();

configure();
const staleButtons = {};
accessorialRows().forEach((row) => {
  const name = inputsWithoutField(row)[0].value;
  let button = null;
  walk(row, (node) => {
    if (!button && node.tagName === 'BUTTON' && node.textContent === 'Excluir') button = node;
  });
  staleButtons[name] = button;
});
fire(staleButtons.GRIS, 'click');
fire(staleButtons['Pedágio'], 'click');
report.afterStaleDeletes = api.fees().map((fee) => fee.fee_id);

configure();
deleteRow('Pedágio');
report.afterDeletePedagio = {
  fees: api.fees(),
  errors: api.errors(),
  visibleNames: accessorialRows().map((row) => inputsWithoutField(row)[0].value),
};

configure();
const removed = [];
let guard = 0;
while (accessorialRows().length && guard < 20) {
  const name = inputsWithoutField(accessorialRows()[0])[0].value;
  removed.push(name);
  deleteRow(name);
  guard += 1;
}
report.afterDeleteAllVisible = {
  removed,
  fees: api.fees(),
  errors: api.errors(),
  canProceed: api.canProceed(),
  blockingMessage: api.blockingMessage(),
  saveBlocked: api.saveBlocked(),
  saveLabel: api.saveLabel(),
  visibleCount: accessorialRows().length,
};

process.stdout.write(JSON.stringify(report));
"""


def _node_executable() -> str:
    found = shutil.which("node")
    if found:
        return found
    bundled = Path.home() / "AppData/Local/Programs/cursor/resources/app/resources/helpers/node.exe"
    if bundled.is_file():
        return str(bundled)
    pytest.fail("Node.js é obrigatório para reproduzir a exclusão dos adicionais visíveis.")


def _run_identity_scenario() -> dict:
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "accessorial_identity_runner.js"
        runner.write_text(_RUNNER, encoding="utf-8")
        result = subprocess.run(
            [_node_executable(), str(runner), str(JS_PATH)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=False,
        )
    if result.returncode != 0:
        pytest.fail(
            "Falha ao executar o editor de adicionais.\n"
            f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return json.loads(result.stdout)


def _by_id(fees: list[dict]) -> dict[str, dict]:
    return {fee["fee_id"]: fee for fee in fees}


def test_visible_accessorial_row_keeps_original_fee_identity():
    report = _run_identity_scenario()

    assert report["hiddenPrimaryShown"] is False
    assert report["visibleNamesBeforeEdit"] == [
        "GRIS",
        "Ad Valorem",
        "Pedágio",
        "TDE/TDA",
        "Cubagem",
        "Frete mínimo",
    ]
    # Frete peso (0) e Frete valor (2) estão ocultos; Ad Valorem é o índice real 3.
    assert report["adValoremOriginalIndex"] == "3"
    assert "Mínimo aplicável a GRIS" in report["minimumLabel"]

    edited = _by_id(report["afterEdit"])
    assert edited["advalorem"]["name"] == "Ad Valorem revisado"
    assert edited["advalorem"]["value"] == "2,50"
    assert edited["advalorem"]["unit"] == "%"
    assert edited["advalorem"]["notes"] == "obs ad"
    assert edited["advalorem"]["scope"] == "rota"
    assert edited["advalorem"]["calculation_base_id"] == "pct_nota_fiscal"
    assert edited["gris"]["name"] == "GRIS"
    assert edited["gris"]["calculation_base_id"] is None
    assert edited["primary-peso"]["sentinel"] == "keep-primary-peso"
    assert edited["primary-peso"]["value"] == "1,50"
    assert edited["primary-valor"]["sentinel"] == "keep-primary-valor"
    assert edited["minimo"]["value"] == "12,00"
    assert edited["gris"]["value"] == ""

    assert report["afterStaleDeletes"] == [
        "primary-peso",
        "primary-valor",
        "advalorem",
        "tde",
        "cubagem",
        "minimo",
    ]

    after_delete = report["afterDeletePedagio"]
    assert [fee["fee_id"] for fee in after_delete["fees"]] == [
        "primary-peso",
        "gris",
        "primary-valor",
        "advalorem",
        "tde",
        "cubagem",
        "minimo",
    ]
    assert after_delete["visibleNames"] == [
        "GRIS",
        "Ad Valorem",
        "TDE/TDA",
        "Cubagem",
        "Frete mínimo",
    ]
    error_names = [error["name"] for error in after_delete["errors"]]
    assert "Pedágio" not in error_names
    assert "Frete peso" not in error_names
    assert "Frete valor" not in error_names
    assert error_names == ["GRIS", "Ad Valorem", "TDE/TDA", "Cubagem"]

    cleared = report["afterDeleteAllVisible"]
    assert cleared["removed"] == [
        "GRIS",
        "Ad Valorem",
        "Pedágio",
        "TDE/TDA",
        "Cubagem",
        "Frete mínimo",
    ]
    assert [fee["fee_id"] for fee in cleared["fees"]] == ["primary-peso", "primary-valor"]
    assert cleared["fees"][0]["sentinel"] == "keep-primary-peso"
    assert cleared["fees"][1]["sentinel"] == "keep-primary-valor"
    assert cleared["errors"] == []
    assert cleared["canProceed"] is True
    assert cleared["blockingMessage"] == ""
    assert cleared["saveBlocked"] is False
    assert cleared["saveLabel"] == "Salvar e Avançar"
    assert cleared["visibleCount"] == 0
