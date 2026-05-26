from app.extensions import db
from app.models import AuditoriaGerencial, NoticiaPortal, Pauta
from app.run_julia_agente_pipeline import executar_pipeline
from app.run_cleiton_agente_orquestrador import _detalhe_falha_dispatch_julia


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


def test_pipeline_bloqueia_artigo_em_fallback_sem_publicar(app, monkeypatch):
    with app.app_context():
        pauta = _criar_pauta(tipo="artigo", suffix="fallback")
        conteudo_fallback = {
            "titulo_julia": "Fallback de artigo",
            "conteudo_completo": "<p>Conteúdo de contingência</p>",
            "redacao_status": "fallback",
            "redacao_fallback": True,
            "redacao_motivo": "json_parse_error",
        }
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_conteudo",
            lambda *args, **kwargs: conteudo_fallback,
        )

        ok = executar_pipeline(
            {"mission_id": "mission-fallback", "tipo_missao": "artigo"},
            app,
        )

        pauta_atualizada = db.session.get(Pauta, pauta.id)
        noticia = NoticiaPortal.query.filter_by(link=pauta.link).first()
        auditoria_bloqueio = (
            AuditoriaGerencial.query.filter_by(
                tipo_decisao="julia",
                decisao="Fallback de redação bloqueado antes da publicação",
            )
            .order_by(AuditoriaGerencial.id.desc())
            .first()
        )

        assert ok is False
        assert pauta_atualizada.status == "falha"
        assert noticia is None
        assert auditoria_bloqueio is not None
        assert auditoria_bloqueio.resultado == "falha"
        assert '"redacao_motivo": "json_parse_error"' in (auditoria_bloqueio.contexto_json or "")


def test_pipeline_noticia_curta_permanece_publicando(app, monkeypatch):
    with app.app_context():
        pauta = _criar_pauta(tipo="noticia", suffix="ok-noticia")
        conteudo_ok = {
            "titulo_julia": "Notícia validada",
            "resumo_julia": "Resumo operacional",
            "prompt_imagem": "Prompt de imagem realista para logística com operação em porto",
        }
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_conteudo",
            lambda *args, **kwargs: conteudo_ok,
        )
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_url_imagem",
            lambda _prompt: "https://example.com/img.png",
        )
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.classificar_origem_url_imagem",
            lambda _url: "externa",
        )
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.validar_conteudo",
            lambda conteudo, tipo: (True, []),
        )
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.gerar_assets_por_canal",
            lambda *_args, **_kwargs: {
                "url_imagem_master": "https://example.com/img.png",
                "assets_por_canal": {},
                "provider_utilizado": "mock",
            },
        )
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.normalizar_assets_json",
            lambda _assets: "{}",
        )

        def _publicar_stub(**kwargs):
            noticia = NoticiaPortal(
                tipo=kwargs.get("tipo", "noticia"),
                titulo_julia=kwargs.get("titulo_julia", "Sem título"),
                titulo_original=kwargs.get("titulo_original", "Título original"),
                link=kwargs.get("link"),
                fonte=kwargs.get("fonte", ""),
                resumo_julia=kwargs.get("resumo_julia"),
                conteudo_completo=kwargs.get("conteudo_completo"),
                status_publicacao=kwargs.get("status_publicacao", "pendente"),
            )
            db.session.add(noticia)
            db.session.commit()
            return noticia

        monkeypatch.setattr("app.run_julia_agente_pipeline.publicar", _publicar_stub)
        monkeypatch.setattr(
            "app.run_julia_agente_pipeline.publicar_multicanal",
            lambda noticia, mission_id, assets_por_canal=None: {"resultado": "sucesso_total"},
        )

        ok = executar_pipeline(
            {"mission_id": "mission-noticia-ok", "tipo_missao": "noticia"},
            app,
        )

        pauta_atualizada = db.session.get(Pauta, pauta.id)
        noticia = NoticiaPortal.query.filter_by(link=pauta.link).first()
        assert ok is True
        assert pauta_atualizada.status == "publicada"
        assert noticia is not None


def test_orquestrador_exibe_motivo_tecnico_da_falha_para_admin(app):
    with app.app_context():
        auditoria = AuditoriaGerencial(
            tipo_decisao="julia",
            decisao="Fallback de redação bloqueado antes da publicação",
            contexto_json='{"mission_id":"mission-admin-msg","pauta_id":123,"redacao_motivo":"json_parse_error"}',
            resultado="falha",
            detalhe="Pipeline bloqueou contingência.",
        )
        db.session.add(auditoria)
        db.session.commit()

        detalhe = _detalhe_falha_dispatch_julia("mission-admin-msg")
        assert detalhe == "Falha de redação Júlia: json_parse_error."
