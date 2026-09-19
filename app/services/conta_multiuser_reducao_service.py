"""Redução futura de quantity Multiuser (Fase 7). Sem pró-rata, sem refund, sem revogar membros."""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserReducaoQuantity,
    ContaVinculoOrganizacional,
    MonetizacaoFato,
    User,
    utcnow_naive,
)
from app.services.cleiton_monetizacao_service import registrar_fato_monetizacao
from app.services.conta_multiuser_aumento_service import (
    _atualizar_quantity_item_stripe,
    _atualizar_snapshot_quantity_vinculo,
    _carregar_assinatura_stripe,
    _extrair_ids_vinculo,
    _ler_quantity_stripe_assinatura,
    exigir_contratante_ativo_gestao,
)
from app.services.conta_multiuser_capacidade_service import (
    bloquear_conta_para_capacidade,
    contar_capacidade_comprometida,
    liberar_reservas_expiradas_conta,
)
from app.services.conta_multiuser_ciclo_service import resolver_ciclo_canonico_conta
from app.services.conta_multiuser_errors import (
    GestaoMultiuserNaoAutorizadaError,
    ReducaoMultiuserConflitoError,
    ReducaoMultiuserInvalidaError,
)
from app.services.conta_multiuser_notificacao_service import (
    criar_notificacao,
    tentar_enviar_email_notificacao,
)
from app.services.conta_organizacional_rules import ESTADO_ATIVO
from app.services.conta_organizacional_service import persistir_quantidade_assentos_contratados
from app.services.plano_service import obter_quantidade_minima_multiuser_admin

logger = logging.getLogger(__name__)

PRORATION_BEHAVIOR_NONE = "none"
ESTADO_RECONCILIACAO_NECESSARIA = "reconciliacao_necessaria"
_CHAVES_EFEITO_STRIPE_REDUCAO = (
    "mu_reducao_stripe_prep:{correlation_id}",
    "mu_reducao_stripe_qty:{correlation_id}",
)


@dataclass
class ResultadoReducaoQuantity:
    estado: str
    replay: bool
    conta_id: int
    quantity_atual: int
    quantity_futura: int
    efetivar_em: str
    correlation_id: str
    reducao_id: int
    proration_behavior: str | None = None
    stripe_escrito: bool = False
    mensagem: str = ""


def reducao_pendente_da_conta(conta_id: int) -> ContaMultiuserReducaoQuantity | None:
    return (
        ContaMultiuserReducaoQuantity.query.filter_by(
            conta_id=int(conta_id),
            estado=ContaMultiuserReducaoQuantity.ESTADO_PENDENTE,
        )
        .order_by(ContaMultiuserReducaoQuantity.id.asc())
        .first()
    )


def quantity_futura_pendente(conta_id: int) -> int | None:
    row = reducao_pendente_da_conta(conta_id)
    if row is None:
        return None
    return int(row.quantity_futura)


def snapshot_reducao_para_painel(conta_id: int) -> dict | None:
    row = reducao_pendente_da_conta(conta_id)
    if row is None:
        return None
    from app.services.conta_multiuser_aumento_service import formatar_data_br

    data_limite = None
    if row.efetivar_em is not None:
        data_limite = row.efetivar_em - timedelta(days=1)
    return {
        "quantity_futura": int(row.quantity_futura),
        "quantity_atual_no_pedido": int(row.quantity_atual_no_pedido),
        "efetivar_em": row.efetivar_em.isoformat() if row.efetivar_em else None,
        "efetivar_em_rotulo": formatar_data_br(row.efetivar_em),
        "data_limite_adequacao": data_limite.isoformat() if data_limite else None,
        "data_limite_adequacao_rotulo": formatar_data_br(data_limite),
        "estado": row.estado,
    }


def _cas_update(row: ContaMultiuserReducaoQuantity, values: dict) -> bool:
    expected = int(row.versao)
    values = dict(values)
    values["versao"] = expected + 1
    values["updated_at"] = utcnow_naive()
    result = db.session.execute(
        update(ContaMultiuserReducaoQuantity)
        .where(ContaMultiuserReducaoQuantity.id == int(row.id))
        .where(ContaMultiuserReducaoQuantity.versao == expected)
        .where(ContaMultiuserReducaoQuantity.estado == row.estado)
        .values(**values)
    )
    db.session.flush()
    if int(result.rowcount or 0) != 1:
        return False
    db.session.refresh(row)
    return True


def _to_int_quantity(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            txt = str(value or "").strip()
            value = int(txt)
        except (TypeError, ValueError) as exc:
            raise ReducaoMultiuserInvalidaError(
                "Informe uma quantity futura inteira positiva."
            ) from exc
    if int(value) < 1:
        raise ReducaoMultiuserInvalidaError(
            "A redução não pode ser usada para zerar ou cancelar o contrato."
        )
    return int(value)


def _quantity_do_snapshot_fato(fato: MonetizacaoFato) -> int | None:
    try:
        snap = json.loads(fato.snapshot_normalizado_json or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(snap, dict):
        return None
    raw = snap.get("quantity_futura")
    if raw is None:
        raw = snap.get("quantity")
    try:
        qtd = int(raw)
    except (TypeError, ValueError):
        return None
    return qtd if qtd >= 1 else None


def _efeito_stripe_desta_reducao(correlation_id: str) -> int | None:
    """Quantity Stripe persistida por fato da MESMA redução/correlation."""
    cid = (correlation_id or "").strip()
    if not cid:
        return None
    for template in _CHAVES_EFEITO_STRIPE_REDUCAO:
        fato = MonetizacaoFato.query.filter_by(
            idempotency_key=template.format(correlation_id=cid)
        ).first()
        if fato is None:
            continue
        qtd = _quantity_do_snapshot_fato(fato)
        if qtd is not None:
            return qtd
    return None


def _evidencia_stripe_desta_reducao(
    correlation_id: str,
    *,
    qtd_stripe: int | None,
    futura: int,
) -> bool:
    if qtd_stripe is None:
        return False
    efeito = _efeito_stripe_desta_reducao(correlation_id)
    if efeito is None:
        return False
    return int(efeito) == int(futura) and int(qtd_stripe) == int(efeito)


class EvidenciaCausalReducaoNaoPersistidaError(RuntimeError):
    """Stripe já pode ter sido alterado; a evidência causal não ficou durável."""


def _snapshot_json_fato(snapshot: dict) -> str:
    return json.dumps(snapshot or {}, ensure_ascii=True, sort_keys=True, default=str)


def _fato_por_idempotency(chave: str) -> MonetizacaoFato | None:
    return MonetizacaoFato.query.filter_by(idempotency_key=chave).first()


def _obter_engine():
    """Engine da aplicação. Nunca a Connection da transação corrente."""
    engine = getattr(db, "engine", None)
    if engine is not None and not hasattr(engine, "pool"):
        inner = getattr(engine, "engine", None)
        if inner is not None and hasattr(inner, "connect"):
            engine = inner
    if engine is None or not hasattr(engine, "connect"):
        raise EvidenciaCausalReducaoNaoPersistidaError(
            "Engine SQLAlchemy indisponível para evidência causal isolada."
        )
    return engine


def _engine_nao_oferece_transacao_independente(engine) -> bool:
    """
    StaticPool / SQLite :memory: devolve a mesma Connection da sessão
    principal. Commit nessa Connection commitaria mutações locais pendentes.
    Postgres (e SQLite em arquivo) admitem conexão/transação nova.
    """
    dialect = str(getattr(getattr(engine, "dialect", None), "name", "") or "")
    if dialect != "sqlite":
        return False
    pool = getattr(engine, "pool", None)
    if type(pool).__name__ == "StaticPool":
        return True
    url = str(getattr(engine, "url", "") or "")
    return ":memory:" in url and "cache=shared" not in url


def _novo_fato_efeito_stripe(
    *,
    tipo_fato: str,
    status_tecnico: str,
    conta_id: int,
    usuario_id: int,
    customer_id: str | None,
    subscription_id: str | None,
    idempotency_key: str,
    correlation_key: str,
    snapshot: dict,
) -> MonetizacaoFato:
    return MonetizacaoFato(
        tipo_fato=tipo_fato,
        status_tecnico=status_tecnico,
        idempotency_key=idempotency_key,
        correlation_key=correlation_key,
        timestamp_interno=utcnow_naive(),
        conta_id=int(conta_id),
        usuario_id=int(usuario_id),
        customer_id=(customer_id or "").strip() or None,
        subscription_id=(subscription_id or "").strip() or None,
        snapshot_normalizado_json=_snapshot_json_fato(snapshot),
    )


def _fato_snapshot_equivalente(fato: MonetizacaoFato, snapshot: dict) -> bool:
    qtd = _quantity_do_snapshot_fato(fato)
    if qtd is None:
        return False
    esperado = snapshot.get("quantity_futura")
    if esperado is None:
        esperado = snapshot.get("quantity")
    try:
        return int(qtd) == int(esperado)
    except (TypeError, ValueError):
        return False


def _confirmar_fato_equivalente_duravel(idempotency_key: str, snapshot: dict) -> bool:
    """Confirma o equivalente numa conexão nova; não reutiliza a transação principal."""
    engine = _obter_engine()
    if _engine_nao_oferece_transacao_independente(engine):
        # :memory:/StaticPool não prova persistência independente.
        # Fato visível só na sessão corrente não é durável.
        return False

    from sqlalchemy.orm import Session

    connection = engine.connect()
    isolada = None
    try:
        isolada = Session(bind=connection)
        fato = isolada.query(MonetizacaoFato).filter_by(
            idempotency_key=idempotency_key
        ).first()
        if fato is None:
            return False
        return _fato_snapshot_equivalente(fato, snapshot)
    finally:
        if isolada is not None:
            isolada.close()
        connection.close()


def _inserir_fato_efeito_stripe_unidade_corrente_sem_commit(
    *,
    tipo_fato: str,
    status_tecnico: str,
    conta_id: int,
    usuario_id: int,
    customer_id: str | None,
    subscription_id: str | None,
    idempotency_key: str,
    correlation_key: str,
    snapshot: dict,
) -> None:
    """
    Fallback só para SQLite :memory:/StaticPool de teste: insere na UoW
    corrente e NÃO commita a sessão principal. Não é a semântica de
    produção — Postgres persiste em transação independente.
    """
    existente = _fato_por_idempotency(idempotency_key)
    if existente is not None:
        if _fato_snapshot_equivalente(existente, snapshot):
            return
        raise EvidenciaCausalReducaoNaoPersistidaError(
            "Fato causal existente não é equivalente ao efeito Stripe desta redução."
        )
    with db.session.begin_nested():
        db.session.add(
            _novo_fato_efeito_stripe(
                tipo_fato=tipo_fato,
                status_tecnico=status_tecnico,
                conta_id=conta_id,
                usuario_id=usuario_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                idempotency_key=idempotency_key,
                correlation_key=correlation_key,
                snapshot=snapshot,
            )
        )
        db.session.flush()


def _persistir_fato_efeito_stripe_isolado(
    *,
    tipo_fato: str,
    status_tecnico: str,
    conta_id: int,
    usuario_id: int,
    customer_id: str | None,
    subscription_id: str | None,
    idempotency_key: str,
    correlation_key: str,
    snapshot: dict,
) -> None:
    """
    Produção (Postgres): Engine → conexão nova → transação nova →
    Session vinculada a essa conexão → commit independente.
    Não reutiliza a Connection da transação principal.
    """
    engine = _obter_engine()
    if _engine_nao_oferece_transacao_independente(engine):
        _inserir_fato_efeito_stripe_unidade_corrente_sem_commit(
            tipo_fato=tipo_fato,
            status_tecnico=status_tecnico,
            conta_id=conta_id,
            usuario_id=usuario_id,
            customer_id=customer_id,
            subscription_id=subscription_id,
            idempotency_key=idempotency_key,
            correlation_key=correlation_key,
            snapshot=snapshot,
        )
        return

    from sqlalchemy.orm import Session

    connection = engine.connect()
    isolada = None
    transacao = None
    try:
        transacao = connection.begin()
        isolada = Session(bind=connection)
        existente = isolada.query(MonetizacaoFato).filter_by(
            idempotency_key=idempotency_key
        ).first()
        if existente is not None:
            if _fato_snapshot_equivalente(existente, snapshot):
                return
            raise EvidenciaCausalReducaoNaoPersistidaError(
                "Fato causal existente não é equivalente ao efeito Stripe desta redução."
            )
        isolada.add(
            _novo_fato_efeito_stripe(
                tipo_fato=tipo_fato,
                status_tecnico=status_tecnico,
                conta_id=conta_id,
                usuario_id=usuario_id,
                customer_id=customer_id,
                subscription_id=subscription_id,
                idempotency_key=idempotency_key,
                correlation_key=correlation_key,
                snapshot=snapshot,
            )
        )
        isolada.flush()
        transacao.commit()
        transacao = None
    except Exception:
        if transacao is not None and transacao.is_active:
            transacao.rollback()
        raise
    finally:
        if isolada is not None:
            isolada.close()
        connection.close()


def _registrar_efeito_stripe_reducao(
    row: ContaMultiuserReducaoQuantity,
    *,
    futura: int,
    local: int,
    customer_id: str | None,
    subscription_id: str | None,
    item_id: str | None,
) -> None:
    """
    Evidência causal do update Stripe desta redução. Obrigatória após o
    POST Stripe e isolada da transação local que ainda vai concluir.
    """
    chave = f"mu_reducao_stripe_qty:{row.correlation_id}"
    snapshot = {
        "quantity": int(futura),
        "quantity_local": int(local),
        "quantity_futura": int(futura),
        "proration_behavior": PRORATION_BEHAVIOR_NONE,
        "subscription_item_id": item_id,
    }
    payload = {
        "tipo_fato": "reducao_quantity_stripe_atualizada",
        "status_tecnico": "stripe_ok",
        "conta_id": int(row.conta_id),
        "usuario_id": int(row.solicitado_por_user_id),
        "customer_id": customer_id,
        "subscription_id": subscription_id,
        "idempotency_key": chave,
        "correlation_key": row.correlation_id,
        "snapshot": snapshot,
    }
    try:
        _persistir_fato_efeito_stripe_isolado(**payload)
    except IntegrityError:
        if _confirmar_fato_equivalente_duravel(chave, snapshot):
            logger.info(
                "evento=evidencia_causal_reducao_replay_seguro correlation_id=%s",
                row.correlation_id,
            )
            return
        logger.exception(
            "evento=evidencia_causal_reducao_integrity_sem_fato_duravel correlation_id=%s",
            row.correlation_id,
        )
        raise EvidenciaCausalReducaoNaoPersistidaError(
            "A evidência causal do update Stripe não ficou durável."
        )
    except EvidenciaCausalReducaoNaoPersistidaError:
        raise
    except Exception as exc:
        logger.exception(
            "evento=evidencia_causal_reducao_persistencia_falhou correlation_id=%s",
            row.correlation_id,
        )
        raise EvidenciaCausalReducaoNaoPersistidaError(
            "Falha ao persistir a evidência causal após o update Stripe."
        ) from exc


def _vinculo_monetario_ativo(conta_id: int) -> ContaMonetizacaoVinculo:
    vinculo = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )
    if vinculo is None:
        raise ReducaoMultiuserInvalidaError(
            "Vínculo comercial ativo não encontrado para reduzir quantity."
        )
    return vinculo


def solicitar_reducao_quantity(
    *,
    ator: User,
    quantity_futura,
    idempotency_key: str | None = None,
    commit: bool = True,
) -> ResultadoReducaoQuantity:
    try:
        contratante = exigir_contratante_ativo_gestao(ator)
    except GestaoMultiuserNaoAutorizadaError as exc:
        raise ReducaoMultiuserInvalidaError(str(exc)) from exc

    futura = _to_int_quantity(quantity_futura)
    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=True)
    if minimo is None or futura < int(minimo):
        raise ReducaoMultiuserInvalidaError(
            "A quantity futura não pode ser inferior ao mínimo comercial Multiuser."
        )

    conta = bloquear_conta_para_capacidade(int(contratante.conta_id))
    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)

    atual = conta.quantidade_assentos_contratados
    if atual is None or int(atual) < 1:
        raise ReducaoMultiuserInvalidaError(
            "A Conta não possui quantity contratada para redução."
        )
    atual = int(atual)
    if futura >= atual:
        raise ReducaoMultiuserInvalidaError(
            "A quantity futura deve ser menor que a quantity atual."
        )

    ciclo = resolver_ciclo_canonico_conta(int(conta.id))
    if not ciclo.determinavel or ciclo.fim is None:
        raise ReducaoMultiuserInvalidaError(
            "O ciclo comercial da Conta não é determinável para definir o corte."
        )
    efetivar_em = ciclo.fim

    comprometido = contar_capacidade_comprometida(int(conta.id), agora)
    ativos = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id), estado=ESTADO_ATIVO
    ).count()
    if futura < int(ativos) or futura < int(comprometido):
        raise ReducaoMultiuserInvalidaError(
            "A quantity futura não pode ser inferior aos memberships ativos e reservas válidas."
        )

    chave = (idempotency_key or "").strip() or uuid.uuid4().hex
    existente = ContaMultiuserReducaoQuantity.query.filter_by(
        idempotency_key=chave
    ).first()
    if existente is not None:
        if int(existente.conta_id) != int(conta.id):
            raise ReducaoMultiuserConflitoError(
                "Chave de idempotência não pertence a esta Conta."
            )
        if int(existente.quantity_futura) != futura:
            raise ReducaoMultiuserConflitoError(
                "Chave de idempotência já identifica outra redução."
            )
        if commit:
            db.session.commit()
        return ResultadoReducaoQuantity(
            estado=existente.estado,
            replay=True,
            conta_id=int(conta.id),
            quantity_atual=atual,
            quantity_futura=int(existente.quantity_futura),
            efetivar_em=existente.efetivar_em.isoformat(),
            correlation_id=existente.correlation_id,
            reducao_id=int(existente.id),
            mensagem="Redução já registrada.",
        )

    pendente = reducao_pendente_da_conta(int(conta.id))
    if pendente is not None:
        if int(pendente.quantity_futura) == futura:
            if commit:
                db.session.commit()
            return ResultadoReducaoQuantity(
                estado=pendente.estado,
                replay=True,
                conta_id=int(conta.id),
                quantity_atual=atual,
                quantity_futura=int(pendente.quantity_futura),
                efetivar_em=pendente.efetivar_em.isoformat(),
                correlation_id=pendente.correlation_id,
                reducao_id=int(pendente.id),
                mensagem="Redução já registrada.",
            )
        raise ReducaoMultiuserConflitoError(
            "Já existe uma redução futura pendente para esta Conta."
        )

    vinculo = _vinculo_monetario_ativo(int(conta.id))
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo)
    row = ContaMultiuserReducaoQuantity(
        conta_id=int(conta.id),
        solicitado_por_user_id=int(ator.id),
        quantity_atual_no_pedido=atual,
        quantity_futura=futura,
        solicitado_em=agora,
        efetivar_em=efetivar_em,
        estado=ContaMultiuserReducaoQuantity.ESTADO_PENDENTE,
        idempotency_key=chave,
        correlation_id=uuid.uuid4().hex,
        versao=1,
        stripe_customer_id=customer_id,
        stripe_subscription_id=subscription_id,
        created_at=agora,
        updated_at=agora,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        pendente = reducao_pendente_da_conta(int(conta.id))
        if pendente is not None and int(pendente.quantity_futura) == futura:
            if commit:
                db.session.commit()
            return ResultadoReducaoQuantity(
                estado=pendente.estado,
                replay=True,
                conta_id=int(conta.id),
                quantity_atual=atual,
                quantity_futura=int(pendente.quantity_futura),
                efetivar_em=pendente.efetivar_em.isoformat(),
                correlation_id=pendente.correlation_id,
                reducao_id=int(pendente.id),
                mensagem="Redução já registrada.",
            )
        raise ReducaoMultiuserConflitoError(
            "Já existe uma redução futura pendente para esta Conta."
        ) from exc

    registrar_fato_monetizacao(
        tipo_fato="reducao_quantity_solicitada",
        status_tecnico="pendente",
        conta_id=int(conta.id),
        usuario_id=int(ator.id),
        customer_id=customer_id,
        subscription_id=subscription_id,
        idempotency_key=f"mu_reducao_solicitada:{row.correlation_id}",
        correlation_key=row.correlation_id,
        snapshot_normalizado={
            "quantity_atual": atual,
            "quantity_futura": futura,
            "efetivar_em": efetivar_em.isoformat(),
        },
    )
    criar_notificacao(
        user_id=int(ator.id),
        conta_id=int(conta.id),
        tipo="reducao_solicitada",
        mensagem=(
            f"Redução para {futura} assentos solicitada. "
            "A quantity atual permanece até o próximo corte."
        ),
        dedup_key=f"reducao_solicitada:{row.correlation_id}",
        cta_interno="multiuser_painel.gestao_multiuser",
        referencia_dominio=f"reducao:{row.id}",
        commit=False,
    )
    logger.info(
        "evento=reducao_quantity_solicitada conta_id=%s atual=%s futura=%s efetivar_em=%s",
        conta.id,
        atual,
        futura,
        efetivar_em.isoformat(),
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    tentar_enviar_email_notificacao(
        ator,
        "Redução de assentos solicitada",
        f"A quantity futura {futura} entra no próximo corte. A quantity atual permanece {atual}.",
    )
    db.session.refresh(conta)
    return ResultadoReducaoQuantity(
        estado=row.estado,
        replay=False,
        conta_id=int(conta.id),
        quantity_atual=int(conta.quantidade_assentos_contratados or atual),
        quantity_futura=futura,
        efetivar_em=efetivar_em.isoformat(),
        correlation_id=row.correlation_id,
        reducao_id=int(row.id),
        mensagem="Redução futura registrada. A quantity atual não foi alterada.",
    )


def tentar_efetivar_reducao_no_corte(
    conta_id: int,
    *,
    referencia=None,
    commit: bool = False,
) -> ResultadoReducaoQuantity | None:
    """
    Revalida e efetiva a redução pendente no corte canônico.
    Não revoga membros. Não reseta consumo. proration_behavior=none.
    """
    conta = bloquear_conta_para_capacidade(int(conta_id))
    row = reducao_pendente_da_conta(int(conta.id))
    if row is None:
        return None
    momento = referencia or utcnow_naive()
    if row.efetivar_em is not None and row.efetivar_em > momento:
        return None

    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)
    comprometido = contar_capacidade_comprometida(int(conta.id), agora)
    ativos = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id), estado=ESTADO_ATIVO
    ).count()
    futura = int(row.quantity_futura)
    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=False)
    bloqueada = False
    motivo = ""
    if minimo is not None and futura < int(minimo):
        bloqueada = True
        motivo = "abaixo_do_minimo_comercial"
    elif futura < int(ativos) or futura < int(comprometido):
        bloqueada = True
        motivo = "capacidade_incompativel_no_corte"

    contratante = (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta.id),
            estado=ESTADO_ATIVO,
            papel="contratante",
        )
        .first()
    )
    if bloqueada:
        _cas_update(
            row,
            {"estado": ContaMultiuserReducaoQuantity.ESTADO_BLOQUEADA_NO_CORTE},
        )
        registrar_fato_monetizacao(
            tipo_fato="reducao_quantity_bloqueada_no_corte",
            status_tecnico=motivo,
            conta_id=int(conta.id),
            usuario_id=int(row.solicitado_por_user_id),
            customer_id=row.stripe_customer_id,
            subscription_id=row.stripe_subscription_id,
            idempotency_key=f"mu_reducao_bloqueada:{row.correlation_id}",
            correlation_key=row.correlation_id,
            snapshot_normalizado={
                "quantity_local": conta.quantidade_assentos_contratados,
                "quantity_futura": futura,
                "ativos": ativos,
                "comprometido": comprometido,
                "motivo": motivo,
            },
        )
        if contratante is not None:
            criar_notificacao(
                user_id=int(contratante.user_id),
                conta_id=int(conta.id),
                tipo="reducao_nao_efetivada",
                mensagem=(
                    "A redução de assentos não foi efetivada no corte porque "
                    "a ocupação atual é incompatível."
                ),
                dedup_key=f"reducao_nao_efetivada:{row.correlation_id}",
                cta_interno="multiuser_painel.gestao_multiuser",
                referencia_dominio=f"reducao:{row.id}",
                commit=False,
            )
        logger.info(
            "evento=reducao_quantity_bloqueada_no_corte conta_id=%s motivo=%s",
            conta.id,
            motivo,
        )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        if contratante is not None:
            user_c = db.session.get(User, int(contratante.user_id))
            tentar_enviar_email_notificacao(
                user_c,
                "Redução de assentos não efetivada",
                "A ocupação atual impede a redução no corte. Quantity e Stripe permanecem iguais.",
            )
        return ResultadoReducaoQuantity(
            estado=row.estado,
            replay=False,
            conta_id=int(conta.id),
            quantity_atual=int(conta.quantidade_assentos_contratados or 0),
            quantity_futura=futura,
            efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
            correlation_id=row.correlation_id,
            reducao_id=int(row.id),
            mensagem="Redução não efetivada no corte.",
        )

    vinculo = _vinculo_monetario_ativo(int(conta.id))
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo)
    if not subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "Subscription ausente para efetivar a redução."
        )
    if row.stripe_subscription_id and row.stripe_subscription_id != subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar a mesma Subscription do pedido."
        )
    if row.stripe_customer_id and customer_id and row.stripe_customer_id != customer_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar o mesmo Customer do pedido."
        )

    assinatura = _carregar_assinatura_stripe(subscription_id)
    qtd_stripe, item_id = _ler_quantity_stripe_assinatura(assinatura)
    if not item_id:
        raise ReducaoMultiuserInvalidaError(
            "Subscription Item ausente para efetivar a redução."
        )
    local = int(conta.quantidade_assentos_contratados or 0)
    stripe_ja_na_futura = qtd_stripe is not None and int(qtd_stripe) == futura
    if (
        qtd_stripe is not None
        and int(qtd_stripe) != local
        and not stripe_ja_na_futura
    ):
        raise ReducaoMultiuserInvalidaError(
            "Quantity local e Stripe divergentes; redução não efetivada."
        )

    if stripe_ja_na_futura and local != futura:
        return _resolver_stripe_ja_na_futura(
            row,
            conta,
            vinculo=vinculo,
            futura=futura,
            local=local,
            customer_id=customer_id,
            subscription_id=subscription_id,
            item_id=item_id,
            contratante=contratante,
            agora=agora,
            commit=commit,
        )

    if not stripe_ja_na_futura:
        _atualizar_quantity_item_stripe(
            item_id=item_id,
            nova_quantity=futura,
            idempotency_key=f"mu_reducao_qty:{row.correlation_id}",
        )
        _registrar_efeito_stripe_reducao(
            row,
            futura=futura,
            local=local,
            customer_id=customer_id,
            subscription_id=subscription_id,
            item_id=item_id,
        )
    return _concluir_efetivacao_local_reducao(
        row,
        conta,
        vinculo=vinculo,
        futura=futura,
        customer_id=customer_id,
        subscription_id=subscription_id,
        item_id=item_id,
        contratante=contratante,
        agora=agora,
        commit=commit,
        recuperacao=False,
        stripe_escrito=not stripe_ja_na_futura,
        mensagem="Redução efetivada no corte.",
    )


def _resolver_stripe_ja_na_futura(
    row: ContaMultiuserReducaoQuantity,
    conta: Conta,
    *,
    vinculo: ContaMonetizacaoVinculo,
    futura: int,
    local: int,
    customer_id: str | None,
    subscription_id: str | None,
    item_id: str | None,
    contratante: ContaVinculoOrganizacional | None,
    agora,
    commit: bool,
) -> ResultadoReducaoQuantity:
    """
    Stripe já está na futura. Só conclui o local se a MESMA redução
    tiver evidência durável (correlation/idempotency). Senão, reconciliação.
    """
    if _evidencia_stripe_desta_reducao(
        row.correlation_id, qtd_stripe=futura, futura=futura
    ):
        return _concluir_efetivacao_local_reducao(
            row,
            conta,
            vinculo=vinculo,
            futura=futura,
            customer_id=customer_id,
            subscription_id=subscription_id,
            item_id=item_id,
            contratante=contratante,
            agora=agora,
            commit=commit,
            recuperacao=True,
            stripe_escrito=False,
            mensagem="Redução recuperada: Stripe desta operação já estava na quantity futura.",
        )
    return _marcar_reconciliacao_necessaria(
        row,
        conta,
        futura=futura,
        local=local,
        qtd_stripe=futura,
        motivo="stripe_na_futura_sem_evidencia_desta_reducao",
        commit=commit,
    )


def _concluir_efetivacao_local_reducao(
    row: ContaMultiuserReducaoQuantity,
    conta: Conta,
    *,
    vinculo: ContaMonetizacaoVinculo,
    futura: int,
    customer_id: str | None,
    subscription_id: str | None,
    item_id: str | None,
    contratante: ContaVinculoOrganizacional | None,
    agora,
    commit: bool,
    recuperacao: bool,
    stripe_escrito: bool,
    mensagem: str,
) -> ResultadoReducaoQuantity:
    persistir_quantidade_assentos_contratados(int(conta.id), futura, commit=False)
    db.session.refresh(conta)
    _atualizar_snapshot_quantity_vinculo(vinculo, futura)
    _cas_update(
        row,
        {
            "estado": ContaMultiuserReducaoQuantity.ESTADO_EFETIVADA,
            "efetivada_em": agora,
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "stripe_subscription_item_id": item_id,
        },
    )
    registrar_fato_monetizacao(
        tipo_fato="reducao_quantity_efetivada",
        status_tecnico="aplicado",
        conta_id=int(conta.id),
        usuario_id=int(row.solicitado_por_user_id),
        customer_id=customer_id,
        subscription_id=subscription_id,
        idempotency_key=f"mu_reducao_efetivada:{row.correlation_id}",
        correlation_key=row.correlation_id,
        snapshot_normalizado={
            "quantity_nova": futura,
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
            "subscription_item_id": item_id,
            "recuperacao": recuperacao,
        },
    )
    if recuperacao:
        registrar_fato_monetizacao(
            tipo_fato="reducao_quantity_recuperada",
            status_tecnico="aplicado",
            conta_id=int(conta.id),
            usuario_id=int(row.solicitado_por_user_id),
            customer_id=customer_id,
            subscription_id=subscription_id,
            idempotency_key=f"mu_reducao_recuperada:{row.correlation_id}",
            correlation_key=row.correlation_id,
            snapshot_normalizado={
                "quantity_nova": futura,
                "origem": "stripe_desta_reducao",
                "subscription_item_id": item_id,
            },
        )
    if contratante is not None:
        criar_notificacao(
            user_id=int(contratante.user_id),
            conta_id=int(conta.id),
            tipo="reducao_efetivada",
            mensagem=f"A quantity contratada passou a {futura} assentos.",
            dedup_key=f"reducao_efetivada:{row.correlation_id}",
            cta_interno="multiuser_painel.gestao_multiuser",
            referencia_dominio=f"reducao:{row.id}",
            commit=False,
        )
    logger.info(
        "evento=%s conta_id=%s quantity_nova=%s item=%s",
        "reducao_quantity_recuperada" if recuperacao else "reducao_quantity_efetivada",
        conta.id,
        futura,
        item_id,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    if contratante is not None:
        user_c = db.session.get(User, int(contratante.user_id))
        tentar_enviar_email_notificacao(
            user_c,
            "Redução de assentos efetivada",
            f"A quantity contratada passou a {futura} assentos.",
        )
    return ResultadoReducaoQuantity(
        estado=row.estado,
        replay=False,
        conta_id=int(conta.id),
        quantity_atual=int(conta.quantidade_assentos_contratados or futura),
        quantity_futura=futura,
        efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
        correlation_id=row.correlation_id,
        reducao_id=int(row.id),
        proration_behavior=PRORATION_BEHAVIOR_NONE,
        stripe_escrito=stripe_escrito,
        mensagem=mensagem,
    )


def _marcar_reconciliacao_necessaria(
    row: ContaMultiuserReducaoQuantity,
    conta: Conta,
    *,
    futura: int,
    local: int,
    qtd_stripe: int | None,
    motivo: str,
    commit: bool,
) -> ResultadoReducaoQuantity:
    """Estado acionável: Stripe na futura sem evidência desta redução. Não conclui local."""
    registrar_fato_monetizacao(
        tipo_fato="reducao_quantity_reconciliacao_necessaria",
        status_tecnico=motivo,
        conta_id=int(conta.id),
        usuario_id=int(row.solicitado_por_user_id),
        customer_id=row.stripe_customer_id,
        subscription_id=row.stripe_subscription_id,
        idempotency_key=f"mu_reducao_recon:{row.correlation_id}",
        correlation_key=row.correlation_id,
        snapshot_normalizado={
            "quantity_local": local,
            "quantity_futura": futura,
            "quantity_stripe": qtd_stripe,
            "motivo": motivo,
        },
    )
    logger.info(
        "evento=reducao_quantity_reconciliacao_necessaria conta_id=%s motivo=%s local=%s futura=%s",
        conta.id,
        motivo,
        local,
        futura,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return ResultadoReducaoQuantity(
        estado=ESTADO_RECONCILIACAO_NECESSARIA,
        replay=False,
        conta_id=int(conta.id),
        quantity_atual=local,
        quantity_futura=futura,
        efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
        correlation_id=row.correlation_id,
        reducao_id=int(row.id),
        proration_behavior=PRORATION_BEHAVIOR_NONE,
        stripe_escrito=False,
        mensagem="Reconciliação necessária: Stripe na quantity futura sem evidência desta redução.",
    )


def tentar_recuperar_reducao_parcial(
    conta_id: int,
    *,
    referencia=None,
    commit: bool = False,
) -> ResultadoReducaoQuantity | None:
    """
    Recupera redução pendente quando Stripe já está na futura por efeito
    comprovado da mesma redução. Não reenvia Stripe. Replay-safe.
    """
    conta = bloquear_conta_para_capacidade(int(conta_id))
    row = reducao_pendente_da_conta(int(conta.id))
    if row is None:
        recente = (
            ContaMultiuserReducaoQuantity.query.filter_by(conta_id=int(conta.id))
            .order_by(ContaMultiuserReducaoQuantity.id.desc())
            .first()
        )
        if recente is None:
            return None
        if recente.estado == ContaMultiuserReducaoQuantity.ESTADO_EFETIVADA:
            return ResultadoReducaoQuantity(
                estado=recente.estado,
                replay=True,
                conta_id=int(conta.id),
                quantity_atual=int(conta.quantidade_assentos_contratados or 0),
                quantity_futura=int(recente.quantity_futura),
                efetivar_em=recente.efetivar_em.isoformat() if recente.efetivar_em else "",
                correlation_id=recente.correlation_id,
                reducao_id=int(recente.id),
                proration_behavior=PRORATION_BEHAVIOR_NONE,
                stripe_escrito=False,
                mensagem="Redução já efetivada.",
            )
        return None

    momento = referencia or utcnow_naive()
    if row.efetivar_em is not None and row.efetivar_em > momento:
        return None

    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)
    comprometido = contar_capacidade_comprometida(int(conta.id), agora)
    ativos = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id), estado=ESTADO_ATIVO
    ).count()
    futura = int(row.quantity_futura)
    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=False)
    if minimo is not None and futura < int(minimo):
        return _marcar_reconciliacao_necessaria(
            row,
            conta,
            futura=futura,
            local=int(conta.quantidade_assentos_contratados or 0),
            qtd_stripe=None,
            motivo="capacidade_incompativel_na_recuperacao",
            commit=commit,
        )
    if futura < int(ativos) or futura < int(comprometido):
        return _marcar_reconciliacao_necessaria(
            row,
            conta,
            futura=futura,
            local=int(conta.quantidade_assentos_contratados or 0),
            qtd_stripe=None,
            motivo="capacidade_incompativel_na_recuperacao",
            commit=commit,
        )

    contratante = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id),
        estado=ESTADO_ATIVO,
        papel="contratante",
    ).first()
    vinculo = _vinculo_monetario_ativo(int(conta.id))
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo)
    if not subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "Subscription ausente para recuperar a redução."
        )
    if row.stripe_subscription_id and row.stripe_subscription_id != subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar a mesma Subscription do pedido."
        )
    if row.stripe_customer_id and customer_id and row.stripe_customer_id != customer_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar o mesmo Customer do pedido."
        )

    assinatura = _carregar_assinatura_stripe(subscription_id)
    qtd_stripe, item_id = _ler_quantity_stripe_assinatura(assinatura)
    local = int(conta.quantidade_assentos_contratados or 0)
    if qtd_stripe is None or int(qtd_stripe) != futura:
        return _marcar_reconciliacao_necessaria(
            row,
            conta,
            futura=futura,
            local=local,
            qtd_stripe=qtd_stripe,
            motivo="stripe_nao_esta_na_quantity_futura",
            commit=commit,
        )
    if local == futura:
        return _concluir_efetivacao_local_reducao(
            row,
            conta,
            vinculo=vinculo,
            futura=futura,
            customer_id=customer_id,
            subscription_id=subscription_id,
            item_id=item_id,
            contratante=contratante,
            agora=agora,
            commit=commit,
            recuperacao=True,
            stripe_escrito=False,
            mensagem="Redução recuperada: estado local já coincidia com a futura.",
        )
    return _resolver_stripe_ja_na_futura(
        row,
        conta,
        vinculo=vinculo,
        futura=futura,
        local=local,
        customer_id=customer_id,
        subscription_id=subscription_id,
        item_id=item_id,
        contratante=contratante,
        agora=agora,
        commit=commit,
    )


_BILLING_REASONS_RENOVACAO = frozenset(
    {"subscription_cycle", "subscription", "upcoming"}
)
_BILLING_REASONS_IGNORAR = frozenset(
    {"subscription_create", "manual", "subscription_update"}
)


def _subscription_id_do_invoice(object_data: dict | None) -> str | None:
    if not isinstance(object_data, dict):
        return None
    raw = object_data.get("subscription")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    if isinstance(raw, dict):
        sid = str(raw.get("id") or "").strip()
        if sid:
            return sid
    parent = object_data.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_details")
        if isinstance(details, dict):
            sid = str(details.get("subscription") or "").strip()
            if sid:
                return sid
    lines = object_data.get("lines")
    data = lines.get("data") if isinstance(lines, dict) else None
    if isinstance(data, list):
        from app.services.cleiton_monetizacao_service import (
            _subscription_id_from_invoice_line,
        )

        for line in data:
            sid = _subscription_id_from_invoice_line(line)
            if sid:
                return sid
    return None


def _subscription_item_da_linha_invoice(line: dict) -> str:
    line_item = str(line.get("subscription_item") or "").strip()
    if line_item:
        return line_item
    parent = line.get("parent")
    if isinstance(parent, dict):
        details = parent.get("subscription_item_details")
        if isinstance(details, dict):
            return str(details.get("subscription_item") or "").strip()
    return ""


def _linhas_invoice_payload(invoice_payload: dict) -> list[dict]:
    lines = invoice_payload.get("lines")
    data = lines.get("data") if isinstance(lines, dict) else None
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(lines, list):
        return [x for x in lines if isinstance(x, dict)]
    return []


def _quantity_rascunho_do_item(invoice_payload: dict, item_id: str) -> int | None:
    for line in _linhas_invoice_payload(invoice_payload):
        line_item = _subscription_item_da_linha_invoice(line)
        if line_item and line_item != item_id:
            continue
        raw = line.get("quantity")
        try:
            qtd = int(raw)
        except (TypeError, ValueError):
            return None
        return qtd if qtd >= 1 else None
    return None


def _alinhar_quantity_invoice_rascunho(
    *,
    invoice_payload: dict,
    item_id: str,
    nova_quantity: int,
    idempotency_key: str,
) -> bool:
    """
    Confirma que a invoice cobrável usa a quantity futura.
    True: preview sem invoice mutável, ou rascunho confirmado na futura.
    False: invoice cobrável existe e não pôde ser confirmada na futura.
    """
    if not isinstance(invoice_payload, dict):
        return True
    status = str(invoice_payload.get("status") or "").strip().lower()
    invoice_id = str(invoice_payload.get("id") or "").strip()
    if not invoice_id:
        return True
    qtd_atual = _quantity_rascunho_do_item(invoice_payload, item_id)
    if qtd_atual is not None and int(qtd_atual) == int(nova_quantity):
        return True
    if status not in {"draft", ""}:
        return False
    if qtd_atual is None:
        return False
    from app.services.cleiton_monetizacao_service import _stripe_post

    alinhou = False
    for line in _linhas_invoice_payload(invoice_payload):
        line_item = _subscription_item_da_linha_invoice(line)
        if line_item and line_item != item_id:
            continue
        line_id = str(line.get("id") or "").strip()
        if not line_id:
            return False
        try:
            resp = _stripe_post(
                f"/invoices/{invoice_id}/lines/{line_id}",
                {"quantity": str(int(nova_quantity))},
                idempotency_key=f"{idempotency_key}:{line_id}",
            )
        except Exception:
            logger.exception(
                "evento=reducao_invoice_linha_nao_alinhada invoice_id=%s line_id=%s",
                invoice_id,
                line_id,
            )
            return False
        got = resp.get("quantity") if isinstance(resp, dict) else None
        try:
            if int(got) != int(nova_quantity):
                return False
        except (TypeError, ValueError):
            return False
        alinhou = True
    return alinhou


def _bloquear_reducao_invoice_inconsistente(
    row: ContaMultiuserReducaoQuantity,
    conta: Conta,
    *,
    futura: int,
    local: int,
    motivo: str,
    commit: bool,
) -> ResultadoReducaoQuantity:
    """Fail-closed: invoice não representa a futura; local não avança."""
    _cas_update(
        row,
        {"estado": ContaMultiuserReducaoQuantity.ESTADO_BLOQUEADA_NO_CORTE},
    )
    registrar_fato_monetizacao(
        tipo_fato="reducao_quantity_invoice_nao_alinhada",
        status_tecnico=motivo,
        conta_id=int(conta.id),
        usuario_id=int(row.solicitado_por_user_id),
        customer_id=row.stripe_customer_id,
        subscription_id=row.stripe_subscription_id,
        idempotency_key=f"mu_reducao_inv_fail:{row.correlation_id}",
        correlation_key=row.correlation_id,
        snapshot_normalizado={
            "quantity_local": local,
            "quantity_futura": futura,
            "motivo": motivo,
        },
    )
    logger.info(
        "evento=reducao_invoice_nao_alinhada conta_id=%s motivo=%s local=%s futura=%s",
        conta.id,
        motivo,
        local,
        futura,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return ResultadoReducaoQuantity(
        estado=row.estado,
        replay=False,
        conta_id=int(conta.id),
        quantity_atual=local,
        quantity_futura=futura,
        efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
        correlation_id=row.correlation_id,
        reducao_id=int(row.id),
        proration_behavior=PRORATION_BEHAVIOR_NONE,
        stripe_escrito=False,
        mensagem="Redução não aplicada: a invoice não cobra a quantity futura.",
    )


def preparar_reducao_stripe_antes_da_cobranca(
    conta_id: int,
    *,
    invoice_payload: dict | None = None,
    commit: bool = False,
) -> ResultadoReducaoQuantity | None:
    """
    Atualiza a quantity Stripe (e o rascunho da invoice, se houver) ANTES
    da cobrança da virada. Não altera quantity local, ciclo nem consumo.
    proration_behavior=none. Fail-closed se ocupação > futura.
    """
    conta = bloquear_conta_para_capacidade(int(conta_id))
    row = reducao_pendente_da_conta(int(conta.id))
    if row is None:
        return None

    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)
    comprometido = contar_capacidade_comprometida(int(conta.id), agora)
    ativos = ContaVinculoOrganizacional.query.filter_by(
        conta_id=int(conta.id), estado=ESTADO_ATIVO
    ).count()
    futura = int(row.quantity_futura)
    minimo = obter_quantidade_minima_multiuser_admin(exigir_configurado=False)
    bloqueio_capacidade = (
        (minimo is not None and futura < int(minimo))
        or futura < int(ativos)
        or futura < int(comprometido)
    )
    if bloqueio_capacidade:
        if row.efetivar_em is not None and row.efetivar_em > agora:
            return None
        return tentar_efetivar_reducao_no_corte(
            int(conta.id), referencia=agora, commit=commit
        )

    vinculo = _vinculo_monetario_ativo(int(conta.id))
    customer_id, subscription_id = _extrair_ids_vinculo(vinculo)
    if not subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "Subscription ausente para preparar a redução."
        )
    if row.stripe_subscription_id and row.stripe_subscription_id != subscription_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar a mesma Subscription do pedido."
        )
    if row.stripe_customer_id and customer_id and row.stripe_customer_id != customer_id:
        raise ReducaoMultiuserInvalidaError(
            "A redução deve usar o mesmo Customer do pedido."
        )

    assinatura = _carregar_assinatura_stripe(subscription_id)
    qtd_stripe, item_id = _ler_quantity_stripe_assinatura(assinatura)
    if not item_id:
        raise ReducaoMultiuserInvalidaError(
            "Subscription Item ausente para preparar a redução."
        )
    local = int(conta.quantidade_assentos_contratados or 0)
    if qtd_stripe is not None and int(qtd_stripe) == futura:
        if isinstance(invoice_payload, dict):
            invoice_ok = _alinhar_quantity_invoice_rascunho(
                invoice_payload=invoice_payload,
                item_id=item_id,
                nova_quantity=futura,
                idempotency_key=f"mu_reducao_inv:{row.correlation_id}",
            )
            if not invoice_ok:
                return _bloquear_reducao_invoice_inconsistente(
                    row,
                    conta,
                    futura=futura,
                    local=local,
                    motivo="falha_update_linha_invoice",
                    commit=commit,
                )
        if commit:
            db.session.commit()
        else:
            db.session.flush()
        return ResultadoReducaoQuantity(
            estado=row.estado,
            replay=True,
            conta_id=int(conta.id),
            quantity_atual=local,
            quantity_futura=futura,
            efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
            correlation_id=row.correlation_id,
            reducao_id=int(row.id),
            proration_behavior=PRORATION_BEHAVIOR_NONE,
            stripe_escrito=False,
            mensagem="Stripe já está na quantity futura.",
        )
    if qtd_stripe is not None and int(qtd_stripe) != local:
        raise ReducaoMultiuserInvalidaError(
            "Quantity local e Stripe divergentes; redução não preparada."
        )

    _atualizar_quantity_item_stripe(
        item_id=item_id,
        nova_quantity=futura,
        idempotency_key=f"mu_reducao_qty:{row.correlation_id}",
    )
    _registrar_efeito_stripe_reducao(
        row,
        futura=futura,
        local=local,
        customer_id=customer_id,
        subscription_id=subscription_id,
        item_id=item_id,
    )
    if isinstance(invoice_payload, dict):
        invoice_ok = _alinhar_quantity_invoice_rascunho(
            invoice_payload=invoice_payload,
            item_id=item_id,
            nova_quantity=futura,
            idempotency_key=f"mu_reducao_inv:{row.correlation_id}",
        )
        if not invoice_ok:
            return _bloquear_reducao_invoice_inconsistente(
                row,
                conta,
                futura=futura,
                local=local,
                motivo="falha_update_linha_invoice",
                commit=commit,
            )
    registrar_fato_monetizacao(
        tipo_fato="reducao_quantity_stripe_preparada",
        status_tecnico="stripe_ok",
        conta_id=int(conta.id),
        usuario_id=int(row.solicitado_por_user_id),
        customer_id=customer_id,
        subscription_id=subscription_id,
        idempotency_key=f"mu_reducao_stripe_prep:{row.correlation_id}",
        correlation_key=row.correlation_id,
        snapshot_normalizado={
            "quantity_local": local,
            "quantity_futura": futura,
            "proration_behavior": PRORATION_BEHAVIOR_NONE,
            "subscription_item_id": item_id,
        },
    )
    logger.info(
        "evento=reducao_quantity_stripe_preparada conta_id=%s futura=%s item=%s",
        conta.id,
        futura,
        item_id,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return ResultadoReducaoQuantity(
        estado=row.estado,
        replay=False,
        conta_id=int(conta.id),
        quantity_atual=local,
        quantity_futura=futura,
        efetivar_em=row.efetivar_em.isoformat() if row.efetivar_em else "",
        correlation_id=row.correlation_id,
        reducao_id=int(row.id),
        proration_behavior=PRORATION_BEHAVIOR_NONE,
        stripe_escrito=True,
        mensagem="Stripe atualizada para a quantity futura antes da cobrança.",
    )


def tentar_preparar_reducao_antes_da_cobranca_por_evento(
    evento: dict,
    object_data: dict | None = None,
) -> ResultadoReducaoQuantity | None:
    """
    Hook de invoice.created / invoice.upcoming. Sem benefício operacional,
    sem reset de ciclo. Somente preparação financeira da redução pendente.
    """
    payload = object_data if isinstance(object_data, dict) else {}
    billing_reason = str(payload.get("billing_reason") or "").strip().lower()
    if billing_reason in _BILLING_REASONS_IGNORAR:
        return None
    if billing_reason and billing_reason not in _BILLING_REASONS_RENOVACAO:
        return None
    subscription_id = _subscription_id_do_invoice(payload)
    if not subscription_id:
        return None
    vinculo = (
        ContaMonetizacaoVinculo.query.filter_by(
            subscription_id=subscription_id,
            ativo=True,
        )
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .first()
    )
    if vinculo is None:
        return None
    conta = db.session.get(Conta, int(vinculo.conta_id))
    if conta is None or not bool(conta.multiuser_ativa):
        return None
    try:
        return preparar_reducao_stripe_antes_da_cobranca(
            int(conta.id),
            invoice_payload=payload,
            commit=True,
        )
    except Exception:
        logger.exception(
            "evento=reducao_pre_cobranca_falhou conta_id=%s event_id=%s",
            conta.id,
            (evento or {}).get("id") if isinstance(evento, dict) else None,
        )
        db.session.rollback()
        return None
