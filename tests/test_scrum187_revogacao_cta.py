"""SCRUM-187: CTA de continuidade após revogação Multiuser (texto + canal existente)."""
from __future__ import annotations

from pathlib import Path

import pytest
from flask import url_for

from app.extensions import db
from app.models import NotificacaoInterna
from app.services.conta_multiuser_notificacao_service import (
    CTA_INTERNOS_VALIDOS,
    criar_notificacao,
    listar_notificacoes_do_user,
    tentar_enviar_email_notificacao,
)
from app.services.conta_multiuser_revogacao_service import revogar_membro
from tests.test_fase7_multiuser_lifecycle import (
    _adicionar_membros,
    _build_client,
    _mock_stripe,
    _preparar_conta_multiuser,
)
from tests.test_user_area_notificacoes import (
    _bloco_notificacoes,
    _build_perfil_client,
    _html_perfil,
    _item_html,
    _src_template,
)

MENSAGEM_APROVADA = (
    "Seu acesso Multiuser nesta Conta foi encerrado. "
    "Você pode continuar usando o Agente Frete com seu próprio plano. "
    "Clique aqui para conhecer as opções e continuar com acesso aos recursos da plataforma."
)
ASSUNTO_APROVADO = "Seu acesso Multiuser foi encerrado — continue no Agente Frete"
CORPO_EMAIL_APROVADO = (
    "Seu acesso Multiuser nesta Conta foi encerrado. "
    "Isso não impede que você continue utilizando o Agente Frete de forma independente. "
    "Contrate seu próprio plano e continue aproveitando os recursos da plataforma."
)
CTA_EMAIL_LABEL = "Continuar usando o Agente Frete"
MENSAGEM_HISTORICA = "Seu acesso Multiuser nesta Conta foi encerrado."


def _criar(
    user,
    conta,
    *,
    tipo: str,
    mensagem: str,
    dedup: str,
    cta: str | None,
):
    return criar_notificacao(
        user_id=user.id,
        conta_id=conta.id,
        tipo=tipo,
        mensagem=mensagem,
        dedup_key=dedup,
        cta_interno=cta,
        commit=True,
    )


def test_allowlist_inclui_contrate_plano_e_rejeita_endpoint_estranho(app):
    assert "user.contrate_plano" in CTA_INTERNOS_VALIDOS
    assert "user.perfil" in CTA_INTERNOS_VALIDOS
    assert "multiuser_painel.gestao_multiuser" in CTA_INTERNOS_VALIDOS
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-allow", qtd=5, email="187.allow@test.com"
        )
        ok = _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_MEMBERSHIP_REVOGADO,
            mensagem=MENSAGEM_APROVADA,
            dedup="allow-ok",
            cta="user.contrate_plano",
        )
        assert ok.cta_interno == "user.contrate_plano"
        with pytest.raises(ValueError, match="CTA interno inválido"):
            _criar(
                user,
                conta,
                tipo=NotificacaoInterna.TIPO_CONVITE_RELEVANTE,
                mensagem="x",
                dedup="allow-bad",
                cta="evil.endpoint",
            )


def test_revogacao_persiste_membership_revogado_com_texto_e_cta(app, monkeypatch):
    with app.app_context():
        _build_client(app)
        conta, contratante = _preparar_conta_multiuser(
            "187-rv", qtd=10, email="187rv@test.com"
        )
        alvo = _adicionar_membros(conta, 1, "187rvm")[0]
        _mock_stripe(monkeypatch, quantity=10)
        with app.test_request_context("/"):
            revogar_membro(ator=contratante, alvo_user_id=alvo.id, commit=True)

            row = (
                NotificacaoInterna.query.filter_by(
                    user_id=alvo.id, tipo="membership_revogado"
                )
                .order_by(NotificacaoInterna.id.desc())
                .first()
            )
            assert row is not None
            assert row.mensagem == MENSAGEM_APROVADA
            assert row.cta_interno == "user.contrate_plano"
            assert row.tipo == NotificacaoInterna.TIPO_MEMBERSHIP_REVOGADO

            itens = listar_notificacoes_do_user(alvo)
            assert itens
            assert itens[0].tipo == "membership_revogado"
            assert itens[0].mensagem == MENSAGEM_APROVADA
            assert itens[0].cta_interno == "user.contrate_plano"
            assert itens[0].cta_url == url_for("user.contrate_plano")
            assert itens[0].cta_url == "/contrate-um-plano"


def test_tela_revogacao_renderiza_clique_aqui_como_link(app, monkeypatch):
    with app.app_context():
        conta, contratante = _preparar_conta_multiuser(
            "187-ui", qtd=10, email="187ui@test.com"
        )
        alvo = _adicionar_membros(conta, 1, "187uim")[0]
        _mock_stripe(monkeypatch, quantity=10)
        revogar_membro(ator=contratante, alvo_user_id=alvo.id, commit=True)
        db.session.refresh(alvo)

        html = _html_perfil(_build_perfil_client(app), alvo)
        bloco = _item_html(html, "Seu acesso Multiuser nesta Conta foi encerrado.")
        assert (
            'href="/contrate-um-plano">Clique aqui</a>' in bloco
            or '<a href="/contrate-um-plano">Clique aqui</a>' in bloco
        )
        assert "Você pode continuar usando o Agente Frete com seu próprio plano." in bloco
        assert (
            "para conhecer as opções e continuar com acesso aos recursos da plataforma."
            in bloco
        )
        assert "Abrir" not in bloco
        assert "<script" not in bloco.lower()
        assert "|safe" not in bloco


def test_template_sem_safe_e_sem_html_persistido():
    src = _src_template()
    inicio = src.index('id="notificacoes"')
    bloco = src[inicio : src.index("{% if eh_contratante_multiuser", inicio)]
    assert "|safe" not in bloco
    assert "membership_revogado" in bloco
    assert 'item.mensagem.split("Clique aqui", 1)' in bloco
    assert 'href="{{ item.cta_url }}">Clique aqui</a>' in bloco
    assert "/contrate-um-plano" not in bloco
    assert "url_for('user.contrate_plano')" in src
    assert "{{ item.mensagem }}" in bloco
    assert ">Abrir</a>" in bloco


def test_html_arbitrario_na_mensagem_nao_e_executado(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-xss", qtd=5, email="187.xss@test.com"
        )
        payload = (
            'Antes <script>alert("xss")</script> Clique aqui depois '
            '<img src=x onerror=alert(1)>'
        )
        _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_MEMBERSHIP_REVOGADO,
            mensagem=payload,
            dedup="xss-rev",
            cta="user.contrate_plano",
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco = _item_html(html, "Antes")
        assert "<script>alert" not in bloco
        assert "<img src=x" not in bloco
        assert "&lt;script&gt;alert" in bloco
        assert "&lt;img src=x" in bloco
        assert '<a href="/contrate-um-plano">Clique aqui</a>' in bloco
        assert "|safe" not in bloco


def test_historico_sem_clique_aqui_nao_quebra(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-old", qtd=5, email="187.old@test.com"
        )
        _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_MEMBERSHIP_REVOGADO,
            mensagem=MENSAGEM_HISTORICA,
            dedup="hist-rev",
            cta="user.perfil",
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco = _item_html(html, MENSAGEM_HISTORICA)
        assert MENSAGEM_HISTORICA in bloco
        assert "Clique aqui" not in bloco
        assert "Abrir" not in bloco
        assert 'href="/contrate-um-plano"' not in bloco
        assert 'id="notificacoes"' in html


def test_historico_sem_clique_aqui_com_cta_contrate_usa_abrir(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-old2", qtd=5, email="187.old2@test.com"
        )
        _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_MEMBERSHIP_REVOGADO,
            mensagem=MENSAGEM_HISTORICA,
            dedup="hist-rev-cta",
            cta="user.contrate_plano",
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco = _item_html(html, MENSAGEM_HISTORICA)
        assert MENSAGEM_HISTORICA in bloco
        assert ">Clique aqui</a>" not in bloco
        assert "Abrir" in bloco
        assert 'href="/contrate-um-plano"' in bloco


def test_demais_notificacoes_preservam_renderizacao(app):
    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-other", qtd=5, email="187.other@test.com"
        )
        _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_CONVITE_RELEVANTE,
            mensagem='Aviso <b>equipe</b> sem Clique aqui',
            dedup="other-mu",
            cta="multiuser_painel.gestao_multiuser",
        )
        _criar(
            user,
            conta,
            tipo=NotificacaoInterna.TIPO_REDUCAO_SOLICITADA,
            mensagem="Aviso do perfil",
            dedup="other-perfil",
            cta="user.perfil",
        )
        html = _html_perfil(_build_perfil_client(app), user)
        bloco_mu = _item_html(html, "equipe")
        bloco_perfil = _item_html(html, "Aviso do perfil")
        assert "&lt;b&gt;equipe&lt;/b&gt;" in bloco_mu
        assert "<b>equipe</b>" not in bloco_mu
        assert "Abrir" in bloco_mu
        assert 'href="/gestao-multiuser"' in bloco_mu
        assert "Abrir" not in bloco_perfil
        assert 'href="/perfil"' not in bloco_perfil
        assert ">Clique aqui</a>" not in _bloco_notificacoes(html)


def test_email_revogacao_assunto_corpo_cta_e_destinatario(app, monkeypatch):
    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    with app.app_context():
        _build_client(app)
        conta, contratante = _preparar_conta_multiuser(
            "187-mail", qtd=10, email="187mail@test.com"
        )
        alvo = _adicionar_membros(conta, 1, "187mailm")[0]
        _mock_stripe(monkeypatch, quantity=10)
        monkeypatch.setattr("app.auth_services.send_email", _capture)
        with app.test_request_context("/"):
            revogar_membro(ator=contratante, alvo_user_id=alvo.id, commit=True)
            db.session.refresh(alvo)
            esperado_url = url_for("user.contrate_plano", _external=True)

        assert len(captured) == 1
        envio = captured[0]
        assert envio["to_email"] == alvo.email
        assert envio["subject"] == ASSUNTO_APROVADO
        assert CORPO_EMAIL_APROVADO in envio["html"]
        assert CORPO_EMAIL_APROVADO in envio["text"]
        assert CTA_EMAIL_LABEL in envio["html"]
        assert f'<a href="{esperado_url}">{CTA_EMAIL_LABEL}</a>' in envio["html"]
        assert f"{CTA_EMAIL_LABEL}: {esperado_url}" in envio["text"]
        assert esperado_url.startswith("http")
        assert esperado_url.endswith("/contrate-um-plano")
        assert alvo.email != contratante.email


def test_helper_sem_cta_preserva_formato_anterior(app, monkeypatch):
    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    with app.app_context():
        conta, user = _preparar_conta_multiuser(
            "187-helper", qtd=5, email="187.helper@test.com"
        )
        monkeypatch.setattr("app.auth_services.send_email", _capture)
        tentar_enviar_email_notificacao(
            user,
            "Redução de assentos solicitada",
            "A quantity futura 8 entra no próximo corte. A quantity atual permanece 10.",
        )
        assert len(captured) == 1
        envio = captured[0]
        assert envio["to_email"] == user.email
        assert envio["subject"] == "Redução de assentos solicitada"
        assert (
            envio["html"]
            == "<p>A quantity futura 8 entra no próximo corte. A quantity atual permanece 10.</p>"
        )
        assert (
            envio["text"]
            == "A quantity futura 8 entra no próximo corte. A quantity atual permanece 10."
        )
        assert "<a href=" not in envio["html"]


def test_helper_com_cta_escapa_texto_e_url(app, monkeypatch):
    captured: list[dict] = []

    def _capture(**kwargs):
        captured.append(kwargs)

    with app.app_context():
        _conta, user = _preparar_conta_multiuser(
            "187-esc", qtd=5, email="187.esc@test.com"
        )
        monkeypatch.setattr("app.auth_services.send_email", _capture)
        tentar_enviar_email_notificacao(
            user,
            "Assunto",
            'Corpo <script>alert(1)</script>',
            cta_label='Continuar <b>agora</b>',
            cta_url='https://example.test/contrate-um-plano?a=1&b=2',
        )
        envio = captured[0]
        assert "<script>" not in envio["html"]
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in envio["html"]
        assert "&lt;b&gt;agora&lt;/b&gt;" in envio["html"]
        assert "a=1&amp;b=2" in envio["html"]
        assert envio["text"] == (
            "Corpo <script>alert(1)</script>\n\n"
            "Continuar <b>agora</b>: https://example.test/contrate-um-plano?a=1&b=2"
        )


def test_arquivos_de_produto_restritos():
    root = Path(__file__).resolve().parents[1]
    models = (root / "app" / "models.py").read_text(encoding="utf-8")
    user_area = (root / "app" / "user_area.py").read_text(encoding="utf-8")
    assert "Clique aqui para conhecer as opções" not in models
    assert "Clique aqui para conhecer as opções" not in user_area
    assert "cta_interno=\"user.contrate_plano\"" not in user_area
