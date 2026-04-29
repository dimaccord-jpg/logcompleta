from app.extensions import db
from app.models import AuditoriaGerencial, Pauta
from app.run_julia_agente_pipeline import executar_pipeline


def _criar_pauta(tipo: str = "noticia", suffix: str = "1") -> Pauta:
    pauta = Pauta(
        titulo_original="Teste de pauta",
        fonte="Portal Teste",
        link=f"https://example.com/{tipo}/pauta-{suffix}",
        tipo=tipo,
        status="pendente",
        status_verificacao="aprovado",
    )
    db.session.add(pauta)
    db.session.commit()
    return pauta


def test_pipeline_marca_pauta_como_falha_e_audita_excecao(app, monkeypatch):
    with app.app_context():
        pauta = _criar_pauta(suffix="erro")

        def _boom(*args, **kwargs):
            raise RuntimeError("falha forçada na redação")

        monkeypatch.setattr("app.run_julia_agente_pipeline.gerar_conteudo", _boom)

        ok = executar_pipeline({"mission_id": "mission-erro", "tipo_missao": "noticia"}, app)

        pauta_atualizada = db.session.get(Pauta, pauta.id)
        auditorias = (
            AuditoriaGerencial.query.filter_by(tipo_decisao="julia")
            .order_by(AuditoriaGerencial.id.asc())
            .all()
        )

        assert ok is False
        assert pauta_atualizada.status == "falha"
        assert [a.decisao for a in auditorias] == [
            "Pauta selecionada para pipeline",
            "Início da redação",
            "Erro inesperado no pipeline",
        ]
        assert auditorias[-1].resultado == "falha"
        assert "mission-erro" in (auditorias[-1].contexto_json or "")
        assert "falha forçada na redação" in (auditorias[-1].detalhe or "")


def test_pipeline_rejeita_retorno_nao_dict_do_llm(app, monkeypatch):
    with app.app_context():
        pauta = _criar_pauta(suffix="tipo")

        monkeypatch.setattr("app.run_julia_agente_pipeline.gerar_conteudo", lambda *args, **kwargs: ["invalido"])

        ok = executar_pipeline({"mission_id": "mission-tipo", "tipo_missao": "noticia"}, app)

        pauta_atualizada = db.session.get(Pauta, pauta.id)
        falha_redacao = (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="julia",
                decisao="Falha na redação",
            )
            .order_by(AuditoriaGerencial.id.desc())
            .first()
        )

        assert ok is False
        assert pauta_atualizada.status == "falha"
        assert falha_redacao is not None
        assert falha_redacao.resultado == "falha"
        assert '"tipo_retorno": "list"' in (falha_redacao.contexto_json or "")
