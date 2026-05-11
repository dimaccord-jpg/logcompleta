from types import SimpleNamespace

from app.models import Franquia
from app.services import cleiton_operacao_autorizacao_service as svc


def _user(*, authenticated=True, franquia_id=10):
    return SimpleNamespace(
        is_authenticated=authenticated,
        franquia_id=franquia_id,
    )


def _leitura(status: str, motivo: str = "operacional_ok", plano_resolvido: str = "starter"):
    return SimpleNamespace(
        franquia_id=10,
        limite_total=None,
        consumo_acumulado=0,
        saldo_disponivel=None,
        inicio_ciclo=None,
        fim_ciclo=None,
        status=status,
        plano_resolvido=plano_resolvido,
        motivo_status=motivo,
        pendencias=(),
    )


def test_active_permite_operacao_normal(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(Franquia.STATUS_ACTIVE),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is True
    assert out["modo_operacao"] == "normal"
    assert out["status_franquia"] == Franquia.STATUS_ACTIVE


def test_degraded_permite_operacao_degradada(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(Franquia.STATUS_DEGRADED),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is True
    assert out["modo_operacao"] == "degraded"
    assert out["sugerir_upgrade"] is True
    assert out["mensagem_usuario"] == (
        "Você atingiu o limite de uso do plano Starter. Não pare agora! "
        "[Faça o upgrade](/contrate-um-plano) e continue criando sem interrupções."
    )


def test_blocked_bloqueia_antes_da_operacao(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(Franquia.STATUS_BLOCKED),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is False
    assert out["modo_operacao"] == "blocked"
    assert out["mensagem_usuario"] == (
        "Você atingiu o limite de uso do plano Starter. Não pare agora! "
        "[Faça o upgrade](/contrate-um-plano) e continue criando sem interrupções."
    )


def test_expired_bloqueia_antes_da_operacao(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(Franquia.STATUS_EXPIRED),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is False
    assert out["modo_operacao"] == "blocked"
    assert out["status_franquia"] == Franquia.STATUS_EXPIRED
    assert out["mensagem_usuario"] == (
        "Você atingiu o limite de uso do plano Starter. Não pare agora! "
        "[Faça o upgrade](/contrate-um-plano) e continue criando sem interrupções."
    )


def test_sem_franquia_trata_sem_erro_500():
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user(franquia_id=None))
    assert out["permitido"] is False
    assert out["status_franquia"] == "missing"
    assert out["motivo"] == "usuario_sem_franquia"


def test_bloqueio_manual_nao_sugere_upgrade(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(
            Franquia.STATUS_BLOCKED, motivo="bloqueio_manual"
        ),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is False
    assert out["sugerir_upgrade"] is False
    assert out["mensagem_usuario"] == (
        "Você atingiu o limite de uso do plano Starter. Não pare agora! "
        "[Faça o upgrade](/contrate-um-plano) e continue criando sem interrupções."
    )


def test_avulso_recebe_cta_padrao_mvp(monkeypatch):
    monkeypatch.setattr(
        svc,
        "ler_franquia_operacional_cleiton",
        lambda _fid, sincronizar_ciclo=True: _leitura(
            Franquia.STATUS_BLOCKED,
            plano_resolvido="avulso",
        ),
    )
    out = svc.avaliar_autorizacao_operacao_por_franquia(_user())
    assert out["permitido"] is False
    assert out["mensagem_usuario"] == (
        "Você atingiu o limite de uso do plano Avulso. Não pare agora! "
        "[Faça o upgrade](/contrate-um-plano) e continue criando sem interrupções."
    )
