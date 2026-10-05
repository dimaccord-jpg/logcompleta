"""Link absoluto e alias curto da conclusão enviada no WhatsApp."""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import timedelta

import pytest

from app.extensions import db
from app.models import (
    EventoCanalSaida,
    OnboardingCanalConclusao,
    utcnow_naive,
)
from app.services import canal_saida_link_seguro_service as entrega
from app.services import onboarding_canal_conclusao_service as conclusao
from app.services.onboarding_canal_conclusao_service import (
    ALIAS_ENTROPIA_BYTES,
    ALIAS_TAMANHO,
    associar_alias_conclusao,
)
from tests.test_whatsapp_canal_lote3d2a import (
    BASE_PUBLICA,
    DESTINATARIO,
    EMAIL_NOVA,
    FRASE_SENHA,
    _configurar,
    _link_do_body,
    _mock,
    _ok,
    _preparar_nova,
)
from tests.test_whatsapp_onboarding_lote2 import (
    SENHA,
    _ate_senha,
    _emitir,
    _patch_limite,
    _preparar_cliente,
)

_ALIAS = re.compile(r"^[A-Za-z0-9_-]{22}$")
_HOST_PRODUCAO = "www.agentefrete.com.br"


def _alias_limpo(alias: str, *proibidos: str) -> None:
    assert _ALIAS.fullmatch(alias)
    assert len(alias) == ALIAS_TAMANHO
    assert ALIAS_ENTROPIA_BYTES == 16
    assert not alias.isdigit()
    for proibido in proibidos:
        assert proibido not in alias


def _sem_bruto(alias: str, link: str) -> None:
    for row in OnboardingCanalConclusao.query.all():
        for coluna in row.__table__.columns:
            valor = getattr(row, coluna.name)
            assert valor != alias
            assert valor != link
            if isinstance(valor, str):
                assert alias not in valor
                assert link not in valor


def test_url_enviada_e_absoluta_e_usa_public_base_url(ctx, app, monkeypatch, caplog):
    _configurar(monkeypatch)
    monkeypatch.setenv("APP_ENV", "homolog")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://homolog0514.agentefrete.com.br")
    app.config["PUBLIC_BASE_URL"] = "https://www.agentefrete.com.br"
    chamadas = _mock(monkeypatch, _ok())
    _preparar_nova(app)
    saida = EventoCanalSaida.query.one()
    with caplog.at_level(logging.DEBUG):
        resultado = entrega.entregar_link_seguro(saida.id)
    corpo = chamadas[0][1]["json"]["text"]["body"]
    link = _link_do_body(chamadas)
    alias = link.rsplit("/c/", 1)[1]
    assert resultado.status_envio == EventoCanalSaida.STATUS_ACEITO
    assert corpo == (
        "Seu cadastro está quase pronto. Por segurança, crie sua senha neste link:\n"
        + link
    )
    assert link == f"https://homolog0514.agentefrete.com.br/c/{alias}"
    assert link.startswith("https://homolog0514.agentefrete.com.br/c/")
    assert _HOST_PRODUCAO not in link
    assert "/onboarding/canal/concluir/" not in corpo
    assert not link.startswith("/")
    _alias_limpo(alias, EMAIL_NOVA, DESTINATARIO)
    row = db.session.get(OnboardingCanalConclusao, resultado.conclusao_id)
    assert row.alias_hash == hashlib.sha256(alias.encode("utf-8")).hexdigest()
    _sem_bruto(alias, link)
    assert alias not in caplog.text
    assert link not in caplog.text


def test_homologacao_nao_cai_no_host_de_producao(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    monkeypatch.setenv("APP_ENV", "homolog")
    app.config["PUBLIC_BASE_URL"] = "https://www.agentefrete.com.br"
    chamadas = _mock(monkeypatch, _ok())
    _preparar_nova(app)
    entrega.entregar_link_seguro(EventoCanalSaida.query.one().id)
    link = _link_do_body(chamadas)
    assert link.startswith(f"{BASE_PUBLICA}/c/")
    assert _HOST_PRODUCAO not in link


@pytest.mark.parametrize(
    "base",
    ["", "   ", "ftp://homolog.exemplo.test/extra", "https://homolog.exemplo.test/extra", "https://user:senha@homolog.exemplo.test"],
)
def test_public_base_url_invalida_falha_fechado(ctx, app, monkeypatch, base):
    _configurar(monkeypatch)
    monkeypatch.setenv("APP_ENV", "homolog")
    app.config["PUBLIC_BASE_URL"] = "https://www.agentefrete.com.br"
    if base:
        monkeypatch.setenv("PUBLIC_BASE_URL", base)
    else:
        monkeypatch.delenv("PUBLIC_BASE_URL", raising=False)
    chamadas = _mock(monkeypatch, _ok())
    antiga, _token, saida = _preparar_nova(app)
    resultado = entrega.entregar_link_seguro(saida.id)
    assert resultado.codigo == entrega.CODIGO_URL_PUBLICA_INVALIDA
    assert resultado.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert chamadas == []
    row = db.session.get(EventoCanalSaida, saida.id)
    assert row.status_envio == EventoCanalSaida.STATUS_AGUARDANDO_LINK
    assert row.provider_message_id is None
    db.session.refresh(antiga)
    assert antiga.estado == OnboardingCanalConclusao.ESTADO_EMITIDO


def test_caminho_relativo_nunca_e_enviado(ctx, app, monkeypatch):
    _configurar(monkeypatch)
    chamadas = _mock(monkeypatch, _ok())
    _preparar_nova(app)
    entrega.entregar_link_seguro(EventoCanalSaida.query.one().id)
    corpo = chamadas[0][1]["json"]["text"]["body"]
    assert corpo == FRASE_SENHA + _link_do_body(chamadas)
    assert "/onboarding/canal/concluir/" not in corpo
    assert "\n/" not in corpo
    assert _link_do_body(chamadas).startswith("https://")


def test_alias_valido_abre_a_conclusao_e_o_token_antigo_continua(ctx, app, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-curto", "curto.canal@example.com", nome="Marina Canal")
    antigo = _emitir(ident.id)
    client = _preparar_cliente(app)
    legado = client.get(f"/onboarding/canal/concluir/{antigo.token}", follow_redirects=False)
    assert legado.status_code == 200
    assert 'type="password"' in legado.get_data(as_text=True)
    assert "/c/" not in legado.headers.get("Location", "")

    alias = associar_alias_conclusao(int(antigo.conclusao_id))
    db.session.commit()
    assert alias
    _alias_limpo(alias, "curto.canal@example.com", "subj-curto")
    aberto = client.get(f"/c/{alias}", follow_redirects=False)
    html = aberto.get_data(as_text=True)
    assert aberto.status_code == 200
    assert aberto.headers.get("Location") is None
    assert 'type="password"' in html
    assert "c***@example.com" in html
    assert "/onboarding/canal/concluir/" not in html
    assert antigo.token not in html
    estado = conclusao.inspecionar_alias_conclusao(alias)
    assert estado.codigo == conclusao.CODIGO_LINK_EMITIDO
    assert estado.onboarding_id == antigo.onboarding_id
    assert estado.formulario == "senha"


def test_alias_expirado_revogado_e_de_outra_conclusao_falham(ctx, app, monkeypatch, caplog):
    _patch_limite(monkeypatch)
    um = _ate_senha("subj-um", "um.canal@example.com", nome="Ana Um")
    dois = _ate_senha("subj-dois", "dois.canal@example.com", nome="Bia Dois")
    emissao_um = _emitir(um.id)
    emissao_dois = _emitir(dois.id)
    alias_um = associar_alias_conclusao(int(emissao_um.conclusao_id))
    alias_dois = associar_alias_conclusao(int(emissao_dois.conclusao_id))
    db.session.commit()
    assert alias_um and alias_dois and alias_um != alias_dois
    assert not alias_um.isdigit() and not alias_dois.isdigit()
    client = _preparar_cliente(app)

    outro = conclusao.inspecionar_alias_conclusao(alias_um)
    assert outro.onboarding_id == emissao_um.onboarding_id
    assert outro.onboarding_id != emissao_dois.onboarding_id
    assert conclusao.inspecionar_alias_conclusao(alias_dois).onboarding_id == emissao_dois.onboarding_id
    assert conclusao.inspecionar_alias_conclusao("a" * ALIAS_TAMANHO).codigo == (
        conclusao.CODIGO_TOKEN_INVALIDO
    )

    row_um = db.session.get(OnboardingCanalConclusao, emissao_um.conclusao_id)
    row_um.expira_em = utcnow_naive() - timedelta(seconds=5)
    db.session.commit()
    with caplog.at_level(logging.DEBUG):
        expirado = client.get(f"/c/{alias_um}")
    assert "expirou" in expirado.get_data(as_text=True).lower()
    assert alias_um not in caplog.text

    row_dois = db.session.get(OnboardingCanalConclusao, emissao_dois.conclusao_id)
    row_dois.estado = OnboardingCanalConclusao.ESTADO_REVOGADO
    db.session.commit()
    revogado = client.get(f"/c/{alias_dois}")
    assert "não pode mais ser usado" in revogado.get_data(as_text=True).lower()


def test_alias_consumido_falha_no_segundo_uso(ctx, app, monkeypatch):
    _patch_limite(monkeypatch)
    ident = _ate_senha("subj-uso", "uso.canal@example.com", nome="Caio Uso")
    emissao = _emitir(ident.id)
    alias = associar_alias_conclusao(int(emissao.conclusao_id))
    db.session.commit()
    client = _preparar_cliente(app)
    primeiro = client.post(
        f"/c/{alias}",
        data={"acao": "definir_senha", "password": SENHA, "confirm_password": SENHA},
    )
    assert "Cadastro concluído" in primeiro.get_data(as_text=True)
    assert emissao.token not in primeiro.get_data(as_text=True)
    segundo = client.get(f"/c/{alias}")
    texto = segundo.get_data(as_text=True).lower()
    assert "já foi utilizado" in texto or "já foi concluído" in texto
    assert 'type="password"' not in segundo.get_data(as_text=True)
    de_novo = conclusao.concluir_definicao_senha_por_alias(alias, SENHA, SENHA)
    assert de_novo.codigo != conclusao.CODIGO_CONTA_CRIADA
    from app.models import User

    assert User.query.count() == 1
