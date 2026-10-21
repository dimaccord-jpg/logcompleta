"""SCRUM-146: coverage pertence à tabela, não à temp_table ativa da sessão."""
from __future__ import annotations

import copy
import io
from uuid import uuid4

import pytest

from app.agente_compara_calculation_service import calculate_single_table
from app.agente_compara_comparison_calculation_service import build_multi_table_calculation_context
from app.agente_compara_comparison_state import (
    AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY,
    STEP_CALCULATION_FILE,
    STEP_COVERAGE,
    get_comparison_state,
    get_table_by_slot,
)
from app.agente_compara_coverage_ownership import (
    blocking_coverage_message,
    evaluate_table_coverage,
)
from app.agente_compara_doc_service import (
    AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY,
    ERROR_COVERAGE_TABLE_REQUIRED,
    FIELD_COMPARISON_ID,
    FIELD_SLOT_NUMBER,
    FIELD_TABLE_ID,
    TEMP_TABLE_STATUS_NEEDS_REVIEW,
    _compile_pricing_contract,
    _temp_table_path,
    _write_temp_table_atomic,
    build_pricing_contract,
    get_temp_table_id,
    load_temp_table_record,
    pricing_source_fingerprint,
)
from tests.cleiton_doc_fixtures import make_csv
from tests.test_agente_compara_comparison_journey import (
    _authorized,
    _load_web_module,
    _post_temp_table_save,
    _setup_env,
)
from tests.test_agente_compara_pricing_contract import (
    _COMP01B_ZONES,
    _comp01b_sulcarga_table,
    _rota_simples_city_uf_table,
)
from tests.test_agente_compara_single_table_calculation import _make_context, _row


@pytest.fixture
def web_client(app, tmp_path, monkeypatch, ctx):
    with app.app_context():
        _setup_env(monkeypatch, tmp_path)
    web = _load_web_module()
    _authorized(monkeypatch, web)
    web.app.config["TESTING"] = True
    web.app.config["SECRET_KEY"] = "test-secret"
    return web.app.test_client()


def _coverage_csv(rows: list[list[str]]) -> bytes:
    return make_csv([["uf_destino", "cidade_destino", "regiao_frete"], *rows])


def _comp01_coverage_rows() -> list[list[str]]:
    return [
        [zone["uf"], zone["city"], zone["zone"]]
        for zone in _COMP01B_ZONES
    ]


def _install_record(web_client, state, entry, payload: dict) -> dict:
    with web_client.application.app_context():
        with web_client.application.test_request_context():
            from app.agente_compara_doc_service import _coerce_temp_table_payload

            record = _coerce_temp_table_payload(payload, source_doc_ids=[])
    record[FIELD_COMPARISON_ID] = state["comparison_id"]
    record[FIELD_TABLE_ID] = entry["table_id"]
    record[FIELD_SLOT_NUMBER] = entry["slot_number"]
    record["temp_table_id"] = uuid4().hex
    record["status"] = TEMP_TABLE_STATUS_NEEDS_REVIEW
    record["source_documents"] = []
    record["freight_tables"] = payload.get("freight_tables") or []
    record["freight_routes"] = payload.get("freight_routes") or []
    record.pop("coverage_table", None)
    with web_client.application.app_context():
        _write_temp_table_atomic(_temp_table_path(record["temp_table_id"]), record)
    entry["temp_table_id"] = record["temp_table_id"]
    entry["confirmed"] = True
    entry["status"] = "confirmed"
    entry["doc_ids"] = []
    return record


def _start_coverage_comparison(web_client, payloads: list[tuple[str, dict]]) -> dict:
    from app.agente_compara_comparison_state import create_comparison

    records = {}
    with web_client.session_transaction() as sess:
        state = create_comparison(session_obj=sess)
        if len(payloads) >= 3:
            third = {
                "table_id": uuid4().hex,
                "slot_number": 3,
                "status": "confirmed",
                "doc_ids": [],
                "temp_table_id": None,
                "carrier_name": None,
                "confirmed": False,
                "error": None,
            }
            state["tables"][third["table_id"]] = third
            state["desired_table_count"] = 3
        for slot, (carrier_name, payload) in enumerate(payloads, start=1):
            entry = get_table_by_slot(state, slot)
            record = _install_record(web_client, state, entry, payload)
            entry["carrier_name"] = carrier_name
            records[slot] = record
        state["primary_temp_table_id"] = records[1]["temp_table_id"]
        state["current_step"] = STEP_COVERAGE
        state["active_table_id"] = get_table_by_slot(state, 1)["table_id"]
        sess[AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY] = state
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = records[1]["temp_table_id"]
        comparison_id = state["comparison_id"]
    return {"comparison_id": comparison_id, "records": records}


def _sulcarga_without_routes() -> dict:
    table = _comp01b_sulcarga_table()
    table["freight_routes"] = []
    return table


def _post_coverage(web_client, content: bytes, *, table_id: str, comparison_id: str, filename: str):
    return web_client.post(
        "/api/agente-compara/coverage/upload",
        data={
            "file": (io.BytesIO(content), filename, "text/csv"),
            "table_id": table_id,
            "comparison_id": comparison_id,
        },
        content_type="multipart/form-data",
    )


def _rows_of(web_client, temp_table_id: str) -> list[dict]:
    record = load_temp_table_record(temp_table_id, ttl_hours=24)
    assert record is not None
    coverage = record.get("coverage_table") or {}
    return list(coverage.get("rows") or [])


def _cities(rows: list[dict]) -> list[str]:
    return [row.get("destination_city") for row in rows]


def test_coverage_upload_requires_table_id_in_multitable_step(web_client):
    boot = _start_coverage_comparison(
        web_client,
        [
            ("EixoLog Alfa", _rota_simples_city_uf_table(title="EixoLog Alfa", first_bracket="32")),
            ("SulCarga Beta", _sulcarga_without_routes()),
        ],
    )
    resp = web_client.post(
        "/api/agente-compara/coverage/upload",
        data={"file": (io.BytesIO(_coverage_csv([["SP", "Campinas", "Capital"]])), "cidades.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 400
    assert resp.get_json()["error_code"] == ERROR_COVERAGE_TABLE_REQUIRED
    assert boot["records"][2].get("coverage_table") is None


def test_each_table_keeps_its_own_coverage_until_all_are_resolved(web_client):
    eixo = _rota_simples_city_uf_table(title="EixoLog Alfa", first_bracket="32")
    sul = _sulcarga_without_routes()
    boot = _start_coverage_comparison(web_client, [("EixoLog Alfa", eixo), ("SulCarga Beta", sul)])
    table_a = boot["records"][1][FIELD_TABLE_ID]
    table_b = boot["records"][2][FIELD_TABLE_ID]
    tt_a = boot["records"][1]["temp_table_id"]
    tt_b = boot["records"][2]["temp_table_id"]

    first = _post_coverage(
        web_client,
        _coverage_csv([["SP", "Campinas", "Capital"]]),
        table_id=table_a,
        comparison_id=boot["comparison_id"],
        filename="COMP-01A_Abrangencia.csv",
    )
    assert first.status_code == 200, first.get_json()
    comparison = first.get_json()["temp_table"]["comparison"]
    assert comparison["current_step"] == STEP_COVERAGE
    by_carrier = {item["carrier_name"]: item for item in comparison["coverage_tables"]}
    assert by_carrier["EixoLog Alfa"]["coverage_status"] == "configured"
    assert by_carrier["EixoLog Alfa"]["coverage_ready"] is True
    assert by_carrier["SulCarga Beta"]["coverage_status"] == "pending"
    assert by_carrier["SulCarga Beta"]["coverage_ready"] is False
    assert _cities(_rows_of(web_client, tt_a)) == ["Campinas"]
    assert _rows_of(web_client, tt_b) == []

    partial = _post_coverage(
        web_client,
        _coverage_csv([["PR", "Curitiba", "P1"]]),
        table_id=table_b,
        comparison_id=boot["comparison_id"],
        filename="COMP-01B_Abrangencia.csv",
    )
    assert partial.status_code == 200, partial.get_json()
    assert partial.get_json()["temp_table"]["comparison"]["current_step"] == STEP_COVERAGE
    assert _cities(_rows_of(web_client, tt_a)) == ["Campinas"]
    assert _cities(_rows_of(web_client, tt_b)) == ["Curitiba"]

    complete = _post_coverage(
        web_client,
        _coverage_csv(_comp01_coverage_rows()),
        table_id=table_b,
        comparison_id=boot["comparison_id"],
        filename="COMP-01B_Abrangencia.csv",
    )
    assert complete.status_code == 200, complete.get_json()
    assert complete.get_json()["temp_table"]["comparison"]["current_step"] == STEP_CALCULATION_FILE
    assert _cities(_rows_of(web_client, tt_a)) == ["Campinas"]
    assert _cities(_rows_of(web_client, tt_b)) == [zone["city"] for zone in _COMP01B_ZONES]

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
        assert state["primary_temp_table_id"] == tt_a
        assert get_temp_table_id(sess) == tt_a
        assert get_temp_table_id(sess, table_id=table_b) == tt_b

    record_b = load_temp_table_record(tt_b, ttl_hours=24)
    contract = build_pricing_contract(record_b)
    p1 = next(rule for rule in contract["rules"] if rule["region"] == "P1")
    assert "PR|CURITIBA" not in (p1.get("lookup_keys") or [])

    zone = next(item for item in _COMP01B_ZONES if item["zone"] == "P1")
    for weight, amount in zone["expected"].items():
        calculated = calculate_single_table(
            _make_context(
                record=record_b,
                coverage=record_b["coverage_table"],
                rows=[
                    _row(
                        row_index=1,
                        destination_city=zone["city"],
                        destination_uf=zone["uf"],
                        weight=weight,
                        invoice_value=None,
                    )
                ],
                carrier_name="SulCarga Beta",
                table_id=table_b,
                slot_number=2,
                temp_table_id=tt_b,
            )
        )
        result = calculated["results"][0]
        assert result["status"] == "calculated", (weight, result)
        assert result["calculated_freight"] == amount

    for other in _COMP01B_ZONES:
        calculated = calculate_single_table(
            _make_context(
                record=record_b,
                coverage=record_b["coverage_table"],
                rows=[
                    _row(
                        row_index=1,
                        destination_city=other["city"],
                        destination_uf=other["uf"],
                        weight=6,
                        invoice_value=None,
                    )
                ],
                carrier_name="SulCarga Beta",
                table_id=table_b,
                slot_number=2,
                temp_table_id=tt_b,
            )
        )
        result = calculated["results"][0]
        assert result["status"] == "calculated", other["zone"]
        assert result["calculated_freight"] == other["expected"][6]

    with web_client.session_transaction() as sess:
        state = get_comparison_state(sess)
    record_a = load_temp_table_record(tt_a, ttl_hours=24)
    context = build_multi_table_calculation_context(
        comparison_id=boot["comparison_id"],
        comparison_state=state,
        normalized_rows=[_row(row_index=1, destination_city="Curitiba", destination_uf="PR", weight=6, invoice_value=None)],
        table_records={
            tt_a: record_a,
            tt_b: record_b,
            table_a: record_a,
            table_b: record_b,
        },
        coverage_table={"rows": [{"destination_uf": "XX", "destination_city": "Nenhuma", "freight_region": "Z9"}]},
    )
    by_table = {item.table_id: item for item in context.table_contexts}
    assert _cities(by_table[table_a].coverage_table["rows"]) == ["Campinas"]
    assert _cities(by_table[table_b].coverage_table["rows"]) == [zone["city"] for zone in _COMP01B_ZONES]
    assert all(row.get("freight_region") != "Z9" for row in by_table[table_b].coverage_table["rows"])


def test_skip_blocks_zone_table_and_allows_city_tables(web_client):
    blocked = _start_coverage_comparison(
        web_client,
        [
            ("EixoLog Alfa", _rota_simples_city_uf_table(title="EixoLog Alfa", first_bracket="32")),
            ("SulCarga Beta", _sulcarga_without_routes()),
        ],
    )
    resp = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": blocked["records"][1]["temp_table_id"],
            "comparison_id": blocked["comparison_id"],
            "table_id": blocked["records"][1][FIELD_TABLE_ID],
            "review_action": "skip_coverage_and_advance",
        },
    )
    assert resp.status_code == 409
    body = resp.get_json()
    assert "SulCarga Beta" in body["message"]
    assert "ainda precisa da configuração de cidades atendidas" in body["message"]
    with web_client.session_transaction() as sess:
        assert get_comparison_state(sess)["current_step"] == STEP_COVERAGE

    allowed = _start_coverage_comparison(
        web_client,
        [
            ("Rota Simples A", _rota_simples_city_uf_table(title="COMP-03", first_bracket="32")),
            ("Rota Simples B", _rota_simples_city_uf_table(title="COMP-03B", first_bracket="34")),
        ],
    )
    skipped = _post_temp_table_save(
        web_client,
        {
            "temp_table_id": allowed["records"][1]["temp_table_id"],
            "comparison_id": allowed["comparison_id"],
            "table_id": allowed["records"][1][FIELD_TABLE_ID],
            "review_action": "skip_coverage_and_advance",
        },
    )
    assert skipped.status_code == 200, skipped.get_json()
    assert skipped.get_json()["comparison"]["current_step"] == STEP_CALCULATION_FILE


def test_three_tables_follow_the_same_coverage_ownership(web_client):
    boot = _start_coverage_comparison(
        web_client,
        [
            ("EixoLog Alfa", _rota_simples_city_uf_table(title="EixoLog Alfa", first_bracket="32")),
            ("SulCarga Beta", _sulcarga_without_routes()),
            ("Norte Gamma", _sulcarga_without_routes()),
        ],
    )
    ids = [boot["records"][slot][FIELD_TABLE_ID] for slot in (1, 2, 3)]
    first = _post_coverage(
        web_client,
        _coverage_csv([["SP", "Campinas", "Capital"]]),
        table_id=ids[0],
        comparison_id=boot["comparison_id"],
        filename="a.csv",
    )
    assert first.get_json()["temp_table"]["comparison"]["current_step"] == STEP_COVERAGE
    second = _post_coverage(
        web_client,
        _coverage_csv(_comp01_coverage_rows()),
        table_id=ids[1],
        comparison_id=boot["comparison_id"],
        filename="b.csv",
    )
    assert second.get_json()["temp_table"]["comparison"]["current_step"] == STEP_COVERAGE
    third = _post_coverage(
        web_client,
        _coverage_csv(_comp01_coverage_rows()),
        table_id=ids[2],
        comparison_id=boot["comparison_id"],
        filename="c.csv",
    )
    assert third.status_code == 200, third.get_json()
    assert third.get_json()["temp_table"]["comparison"]["current_step"] == STEP_CALCULATION_FILE
    assert _cities(_rows_of(web_client, boot["records"][1]["temp_table_id"])) == ["Campinas"]
    assert _cities(_rows_of(web_client, boot["records"][2]["temp_table_id"])) == [zone["city"] for zone in _COMP01B_ZONES]


def test_legacy_upload_without_comparison_still_uses_session_temp_table(web_client):
    boot = _start_coverage_comparison(
        web_client,
        [
            ("EixoLog Alfa", _rota_simples_city_uf_table(title="EixoLog Alfa", first_bracket="32")),
            ("SulCarga Beta", _sulcarga_without_routes()),
        ],
    )
    tt_a = boot["records"][1]["temp_table_id"]
    with web_client.session_transaction() as sess:
        sess.pop(AGENTE_COMPARA_COMPARISON_STATE_SESSION_KEY, None)
        sess[AGENTE_COMPARA_TEMP_TABLE_ID_SESSION_KEY] = tt_a
    resp = web_client.post(
        "/api/agente-compara/coverage/upload",
        data={"file": (io.BytesIO(_coverage_csv([["SP", "Campinas", "Capital"]])), "legado.csv", "text/csv")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    assert _cities(_rows_of(web_client, tt_a)) == ["Campinas"]
    assert _rows_of(web_client, boot["records"][2]["temp_table_id"]) == []


def test_historical_waived_contract_keeps_recognized_coverage_without_coverage_file():
    table = _comp01b_sulcarga_table()
    historical, _findings = _compile_pricing_contract(table, include_freight_routes=True)
    record = copy.deepcopy(table)
    record["pricing_contract"] = historical
    record.pop("coverage_table", None)
    fingerprint = historical["source_fingerprint"]
    rules_before = copy.deepcopy(historical["rules"])
    assert fingerprint == pricing_source_fingerprint(record)

    entry = {
        "table_id": "hist-sulcarga",
        "slot_number": 1,
        "carrier_name": "SulCarga Beta",
        "coverage_disposition": "waived",
    }
    situation = evaluate_table_coverage(entry, record)
    assert situation["coverage_required"] is False
    assert situation["coverage_ready"] is True
    assert situation["coverage_status"] == "waived"
    assert blocking_coverage_message([situation]) is None
    assert record["pricing_contract"]["rules"] == rules_before
    assert record["pricing_contract"]["source_fingerprint"] == fingerprint

    calculated = calculate_single_table(
        _make_context(
            record=record,
            coverage={"rows": []},
            rows=[
                _row(
                    destination_city="Curitiba",
                    destination_uf="PR",
                    weight=620,
                    invoice_value=None,
                )
            ],
            carrier_name="SulCarga Beta",
        )
    )
    assert calculated["results"][0]["status"] == "calculated"
    assert calculated["results"][0]["calculated_freight"] == 355.20
    assert record["pricing_contract"]["source_fingerprint"] == fingerprint

    current = copy.deepcopy(table)
    current["pricing_contract"] = build_pricing_contract(current)
    current.pop("coverage_table", None)
    current_situation = evaluate_table_coverage(entry, current)
    assert current_situation["coverage_required"] is True
    assert current_situation["coverage_ready"] is False
    assert current_situation["coverage_status"] == "pending"
    executable_p1 = next(rule for rule in current["pricing_contract"]["rules"] if rule["region"] == "P1")
    assert "PR|CURITIBA" not in (executable_p1.get("lookup_keys") or [])
