import logging
from flask import session
from sqlalchemy.exc import SQLAlchemyError
from app.extensions import db
from app.agente_compara_doc_service import apply_temp_table_extraction_from_model_payload, load_temp_table_record
from app.services.cleiton_franquia_reconciliacao_service import reconciliar_franquia_cleiton
from tests.cleiton_doc_fixtures import patch_cleiton_doc_store
from tests.test_scrum146_extracao_cobranca import JSON_UTIL, _preparar, _seed, _resposta, _abatimentos, _franquia, _commit_confirma_o_abatimento, temp_mod

def test_commit_real_store(app, monkeypatch, tmp_path, caplog):
    patch_cleiton_doc_store(tmp_path, monkeypatch)
    app.config['SECRET_KEY'] = 'isolated'
    with app.test_request_context('/agente-compara'):
        usuario, franquia = _seed(limite='100', email='audit@test.com')
        fid = franquia.id
        client = _preparar(monkeypatch, usuario=usuario, gerar=lambda model, contents, config: _resposta(JSON_UTIL, prompt=100, resposta=50, pensamento=10), models=['gemini-2.5-flash', 'gemini-2.5-flash-lite'])
        monkeypatch.setattr(temp_mod, 'apply_temp_table_extraction_from_model_payload', apply_temp_table_extraction_from_model_payload)
        session['agente_compara_doc_ids'] = ['doc-1']
        real_commit = db.session.commit
        failures = []
        def commit():
            if _commit_confirma_o_abatimento():
                failures.append(True)
                raise SQLAlchemyError('SEGREDO_SQL_REAUDITORIA')
            return real_commit()
        monkeypatch.setattr(db.session, 'commit', commit)
        caplog.set_level(logging.INFO)
        result = temp_mod.run_agente_compara_temp_table_extraction(['doc-1'], session_obj=session, comparison_id='cmp-audit', table_id='tbl-audit')
        assert failures == [True]
        assert len(client.models.calls) == 1
        assert result['status'] == 'needs_review'
        assert temp_mod.entrega_faturavel(result)
        saved = load_temp_table_record(result['temp_table_id'], ttl_hours=24)
        assert saved['freight_tables'] == result['freight_tables']
        assert saved['status'] == 'needs_review'
        assert saved['error_code'] == temp_mod.ERROR_EXTRACAO_CONSUMO_INCERTO
        assert saved['credit_disposition'] == 'unconfirmed'
        for _ in range(2):
            assert reconciliar_franquia_cleiton(fid, aplicar_correcao=True).total_recalculado == 0
            assert _abatimentos() == 0
            assert _franquia(fid).consumo_acumulado == 0
            assert _franquia(fid).reserva_pendente == 0
        assert 'SEGREDO_SQL_REAUDITORIA' not in caplog.text
