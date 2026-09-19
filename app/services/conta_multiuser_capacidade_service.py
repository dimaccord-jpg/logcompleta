"""
Capacidade Multiuser (Fase 2): lock, quantity, assentos, Franquias e vínculo governado.

Ordem de lock (única nesta fase):
  Conta → vínculo/User → Franquia

Não chama Stripe. Não preenche assento ocioso com User. Não apaga Franquia extra.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy.orm import Query

from app.extensions import db
from app.models import (
    Conta,
    ContaMonetizacaoVinculo,
    ContaMultiuserConvite,
    ContaVinculoOrganizacional,
    Franquia,
    User,
    utcnow_naive,
)
from app.services.conta_multiuser_ciclo_service import (
    alinhar_franquia_ao_ciclo_conta,
    periodo_completo_invalido,
    resolver_ciclo_canonico_conta,
)
from app.services.conta_multiuser_errors import (
    CapacidadeEsgotadaError,
    DivergenciaImpeditivaError,
    VinculoInconsistenteError,
)
from app.services.conta_organizacional_rules import (
    CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY,
    CODIGO_DIV_CICLO_DIVERGENTE,
    CODIGO_DIV_FRANQUIAS_MAIOR_QUE_QUANTITY,
    CODIGO_DIV_FRANQUIAS_MENOR_QUE_CAPACITY,
    CODIGO_DIV_QUANTITY_EXTERNA_LOCAL,
    CODIGO_DIV_USER_CONTA_FRANQUIA_INCOERENTE,
    ESTADO_ATIVO,
    ORDEM_LOCK_CAPACIDADE,
    ORIGEM_ADMIN_PLANO,
    ORIGEM_CONVITE,
    PAPEL_CONTRATANTE,
    PAPEL_MEMBRO,
    SLUG_CONTA_SISTEMA,
)
from app.services.conta_organizacional_service import (
    VinculoOrganizacionalConflitoError,
    criar_vinculo_organizacional,
    marcar_conta_multiuser_ativa,
    persistir_quantidade_assentos_contratados,
)

logger = logging.getLogger(__name__)

# Ordem documentada para todos os writes de capacidade desta fase.
ORDEM_LOCK = ORDEM_LOCK_CAPACIDADE


@dataclass(frozen=True)
class DivergenciaCapacidade:
    codigo: str
    conta_id: int
    detalhe: str
    franquia_id: int | None = None
    user_id: int | None = None


@dataclass(frozen=True)
class SnapshotCapacidade:
    conta_id: int
    quantidade_contratada: int | None
    vinculos_ativos: int
    capacidade_livre: int | None
    franquias_total: int
    franquias_ocupadas: int
    franquias_disponiveis: int
    divergencias: tuple[DivergenciaCapacidade, ...]
    quantity_externa_observada: int | None = None


@dataclass
class ResultadoAtribuicaoMultiuserAdmin:
    conta_id: int
    user_id: int
    franquia_id: int
    quantidade_assentos: int
    franquias_criadas: list[Franquia] = field(default_factory=list)
    vinculo_id: int | None = None
    papel: str = PAPEL_CONTRATANTE
    idempotente: bool = False


def _query_conta_lock(conta_id: int) -> Query:
    return db.session.query(Conta).filter(Conta.id == int(conta_id)).with_for_update()


def _query_user_lock(user_id: int) -> Query:
    return db.session.query(User).filter(User.id == int(user_id)).with_for_update()


def _query_franquia_lock(franquia_id: int) -> Query:
    return db.session.query(Franquia).filter(Franquia.id == int(franquia_id)).with_for_update()


def bloquear_conta_para_capacidade(conta_id: int) -> Conta:
    """Raiz de serialização: SELECT ... FOR UPDATE na Conta (PostgreSQL)."""
    conta = _query_conta_lock(conta_id).one_or_none()
    if conta is None:
        raise ValueError("Conta não encontrada.")
    if (conta.slug or "") == SLUG_CONTA_SISTEMA:
        raise VinculoInconsistenteError("Conta sistema interno não participa da capacidade Multiuser.")
    return conta


def _bloquear_user(user_id: int) -> User:
    user = _query_user_lock(user_id).one_or_none()
    if user is None:
        raise ValueError("User não encontrado.")
    return user


def _bloquear_franquia(franquia_id: int) -> Franquia:
    franquia = _query_franquia_lock(franquia_id).one_or_none()
    if franquia is None:
        raise ValueError("Franquia não encontrada.")
    return franquia


def _vinculos_ativos_conta(conta_id: int) -> list[ContaVinculoOrganizacional]:
    return (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            estado=ESTADO_ATIVO,
        )
        .order_by(ContaVinculoOrganizacional.id.asc())
        .all()
    )


def _franquias_conta(conta_id: int) -> list[Franquia]:
    return (
        Franquia.query.filter_by(conta_id=int(conta_id))
        .order_by(Franquia.id.asc())
        .all()
    )


def _ids_franquias_ocupadas(conta_id: int) -> set[int]:
    return {int(v.franquia_id) for v in _vinculos_ativos_conta(conta_id)}


def _contar_ativos(conta_id: int) -> int:
    return (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            estado=ESTADO_ATIVO,
        )
        .count()
    )


def liberar_reservas_expiradas_conta(conta_id: int, agora=None) -> int:
    """Persiste expiração de convites pendentes cujo TTL venceu (padrão F4)."""
    momento = agora or utcnow_naive()
    rows = (
        ContaMultiuserConvite.query.filter(
            ContaMultiuserConvite.conta_id == int(conta_id),
            ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
            ContaMultiuserConvite.expires_at <= momento,
        )
        .all()
    )
    for row in rows:
        row.estado = ContaMultiuserConvite.ESTADO_EXPIRADO
        row.updated_at = momento
        db.session.add(row)
        logger.info(
            "evento=convite_expirado convite_id=%s conta_id=%s franquia_id=%s",
            row.id,
            row.conta_id,
            row.franquia_id,
        )
    if rows:
        db.session.flush()
    return len(rows)


def ids_franquias_reservadas_validas(
    conta_id: int,
    agora=None,
    *,
    excluir_convite_id: int | None = None,
) -> set[int]:
    """Franquias com convite pendente ainda dentro do TTL."""
    momento = agora or utcnow_naive()
    rows = (
        ContaMultiuserConvite.query.filter(
            ContaMultiuserConvite.conta_id == int(conta_id),
            ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
            ContaMultiuserConvite.expires_at > momento,
        )
        .all()
    )
    out: set[int] = set()
    excluir = int(excluir_convite_id) if excluir_convite_id is not None else None
    for row in rows:
        if excluir is not None and int(row.id) == excluir:
            continue
        out.add(int(row.franquia_id))
    return out


def contar_reservas_pendentes_validas(
    conta_id: int,
    agora=None,
    *,
    excluir_convite_id: int | None = None,
) -> int:
    momento = agora or utcnow_naive()
    query = ContaMultiuserConvite.query.filter(
        ContaMultiuserConvite.conta_id == int(conta_id),
        ContaMultiuserConvite.estado == ContaMultiuserConvite.ESTADO_PENDENTE,
        ContaMultiuserConvite.expires_at > momento,
    )
    if excluir_convite_id is not None:
        query = query.filter(ContaMultiuserConvite.id != int(excluir_convite_id))
    return query.count()


def contar_capacidade_comprometida(
    conta_id: int,
    agora=None,
    *,
    excluir_convite_id: int | None = None,
) -> int:
    """Ativos + reservas F4 pendentes e ainda válidas."""
    return _contar_ativos(conta_id) + contar_reservas_pendentes_validas(
        conta_id, agora, excluir_convite_id=excluir_convite_id
    )


def validar_quantity_nao_inferior_ao_comprometido(
    conta: Conta,
    nova_quantity: int,
    *,
    agora=None,
) -> None:
    """Recusa write local de quantity abaixo de ativos + reservas válidas."""
    momento = agora or utcnow_naive()
    comprometido = contar_capacidade_comprometida(int(conta.id), momento)
    if int(nova_quantity) < int(comprometido):
        raise DivergenciaImpeditivaError(
            "Não é possível persistir quantity menor que a capacidade já comprometida."
        )


def validar_convite_em_conversao(
    *,
    conta_id: int,
    convite_id: int,
    user_id: int,
    franquia_id: int | None = None,
    agora=None,
) -> ContaMultiuserConvite:
    """
    Confirma que o convite informado é a reserva do User destinatário nesta operação.
    Não aceita flags arbitrárias do caller (ignore_reservation / subtract_one).
    """
    momento = agora or utcnow_naive()
    row = db.session.get(ContaMultiuserConvite, int(convite_id))
    if row is None:
        raise VinculoInconsistenteError("Convite em conversão não encontrado.")
    if int(row.conta_id) != int(conta_id):
        raise VinculoInconsistenteError("Convite em conversão não pertence à Conta.")
    if (row.estado or "") != ContaMultiuserConvite.ESTADO_PENDENTE:
        raise VinculoInconsistenteError("Convite em conversão não está pendente.")
    if row.expires_at is None or row.expires_at <= momento:
        raise VinculoInconsistenteError("Convite em conversão não está mais válido.")
    if franquia_id is not None and int(row.franquia_id) != int(franquia_id):
        raise VinculoInconsistenteError(
            "Convite em conversão não corresponde à Franquia informada."
        )
    user = db.session.get(User, int(user_id))
    if user is None:
        raise VinculoInconsistenteError("User da conversão não encontrado.")
    email_user = (user.email or "").strip().lower()
    email_convite = (row.email_destino or "").strip().lower()
    if not email_user or email_user != email_convite:
        raise VinculoInconsistenteError(
            "Convite em conversão não pertence a este User."
        )
    return row


def _extrair_quantity_dict(data: object) -> int | None:
    if not isinstance(data, dict):
        return None
    for key in ("quantity", "quantidade", "quantidade_assentos"):
        valor = data.get(key)
        if isinstance(valor, int) and not isinstance(valor, bool) and valor >= 1:
            return valor
    items = data.get("items")
    if isinstance(items, dict):
        arr = items.get("data")
        if isinstance(arr, list) and arr and isinstance(arr[0], dict):
            q = arr[0].get("quantity")
            if isinstance(q, int) and not isinstance(q, bool) and q >= 1:
                return q
    nested = data.get("subscription")
    if isinstance(nested, dict):
        return _extrair_quantity_dict(nested)
    return None


def observar_quantity_externa_persistida(conta_id: int) -> int | None:
    """Lê quantity já persistida no vínculo comercial. Não chama Stripe."""
    vinculos = (
        ContaMonetizacaoVinculo.query.filter_by(conta_id=int(conta_id), ativo=True)
        .order_by(ContaMonetizacaoVinculo.id.asc())
        .all()
    )
    for vinculo in vinculos:
        for raw in (
            vinculo.snapshot_normalizado_json,
            vinculo.payload_bruto_sanitizado_json,
        ):
            if not raw:
                continue
            try:
                parsed = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            qtd = _extrair_quantity_dict(parsed)
            if qtd is not None:
                return qtd
    return None


def listar_divergencias_conta(conta_id: int) -> list[DivergenciaCapacidade]:
    conta = db.session.get(Conta, int(conta_id))
    if conta is None or (conta.slug or "") == SLUG_CONTA_SISTEMA:
        return []
    cid = int(conta.id)
    qtd = conta.quantidade_assentos_contratados
    ativos = _contar_ativos(cid)
    franquias = _franquias_conta(cid)
    ocupadas = _ids_franquias_ocupadas(cid)
    out: list[DivergenciaCapacidade] = []

    if qtd is not None and ativos > qtd:
        out.append(
            DivergenciaCapacidade(
                codigo=CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY,
                conta_id=cid,
                detalhe="vinculos_ativos_superam_quantity_contratada",
            )
        )
    if qtd is not None and len(franquias) < qtd:
        out.append(
            DivergenciaCapacidade(
                codigo=CODIGO_DIV_FRANQUIAS_MENOR_QUE_CAPACITY,
                conta_id=cid,
                detalhe="franquias_abaixo_da_quantity_contratada",
            )
        )
    if qtd is not None and len(franquias) > qtd:
        out.append(
            DivergenciaCapacidade(
                codigo=CODIGO_DIV_FRANQUIAS_MAIOR_QUE_QUANTITY,
                conta_id=cid,
                detalhe="franquias_acima_da_quantity_contratada",
            )
        )

    ciclo = resolver_ciclo_canonico_conta(cid)
    if ciclo.divergente:
        out.append(
            DivergenciaCapacidade(
                codigo=CODIGO_DIV_CICLO_DIVERGENTE,
                conta_id=cid,
                detalhe="ciclo_canonico_divergente_ou_cronologicamente_invalido",
            )
        )
    elif ciclo.determinavel:
        for fr in franquias:
            if fr.inicio_ciclo is None and fr.fim_ciclo is None:
                continue
            if (
                periodo_completo_invalido(fr.inicio_ciclo, fr.fim_ciclo)
                or fr.inicio_ciclo != ciclo.inicio
                or fr.fim_ciclo != ciclo.fim
            ):
                out.append(
                    DivergenciaCapacidade(
                        codigo=CODIGO_DIV_CICLO_DIVERGENTE,
                        conta_id=cid,
                        detalhe="franquia_fora_do_ciclo_canonico_da_conta",
                        franquia_id=int(fr.id),
                    )
                )
                break
    else:
        for fr in franquias:
            if periodo_completo_invalido(fr.inicio_ciclo, fr.fim_ciclo):
                out.append(
                    DivergenciaCapacidade(
                        codigo=CODIGO_DIV_CICLO_DIVERGENTE,
                        conta_id=cid,
                        detalhe="periodo_completo_cronologicamente_invalido",
                        franquia_id=int(fr.id),
                    )
                )
                break

    for vinculo in _vinculos_ativos_conta(cid):
        user = db.session.get(User, int(vinculo.user_id))
        franquia = db.session.get(Franquia, int(vinculo.franquia_id))
        incoerente = False
        if user is None or franquia is None:
            incoerente = True
        elif int(user.conta_id) != cid or int(franquia.conta_id) != cid:
            incoerente = True
        elif int(user.franquia_id) != int(vinculo.franquia_id):
            incoerente = True
        elif int(user.id) != int(vinculo.user_id):
            incoerente = True
        if incoerente:
            out.append(
                DivergenciaCapacidade(
                    codigo=CODIGO_DIV_USER_CONTA_FRANQUIA_INCOERENTE,
                    conta_id=cid,
                    detalhe="fk_user_conta_franquia_diverge_do_vinculo_ativo",
                    franquia_id=int(vinculo.franquia_id),
                    user_id=int(vinculo.user_id),
                )
            )

    qtd_ext = observar_quantity_externa_persistida(cid)
    if qtd is not None and qtd_ext is not None and qtd != qtd_ext:
        out.append(
            DivergenciaCapacidade(
                codigo=CODIGO_DIV_QUANTITY_EXTERNA_LOCAL,
                conta_id=cid,
                detalhe="quantity_local_diverge_da_quantity_externa_persistida",
            )
        )

    _ = ocupadas  # ocupação entra no snapshot; divergência de extras já coberta acima
    return out


def snapshot_capacidade(conta_id: int) -> SnapshotCapacidade:
    conta = db.session.get(Conta, int(conta_id))
    if conta is None:
        raise ValueError("Conta não encontrada.")
    cid = int(conta.id)
    qtd = conta.quantidade_assentos_contratados
    ativos = _contar_ativos(cid)
    franquias = _franquias_conta(cid)
    ocupadas = _ids_franquias_ocupadas(cid)
    livres: int | None
    if qtd is None:
        livres = None
    else:
        livres = max(0, int(qtd) - int(ativos))
    divergencias = tuple(listar_divergencias_conta(cid))
    return SnapshotCapacidade(
        conta_id=cid,
        quantidade_contratada=qtd,
        vinculos_ativos=ativos,
        capacidade_livre=livres,
        franquias_total=len(franquias),
        franquias_ocupadas=len(ocupadas),
        franquias_disponiveis=max(0, len(franquias) - len(ocupadas)),
        divergencias=divergencias,
        quantity_externa_observada=observar_quantity_externa_persistida(cid),
    )


def limite_ocupacao_conta(conta: Conta) -> int | None:
    """
    Teto operacional do ciclo vigente: a quantity atual contratada.

    Uma redução futura pendente é alvo do próximo corte e não reduz
    antecipadamente os assentos que o cliente já contratou e está pagando.
    """
    qtd = conta.quantidade_assentos_contratados
    if qtd is None:
        return None
    return int(qtd)


def _exigir_capacidade_livre(
    conta: Conta,
    *,
    convite_em_conversao: ContaMultiuserConvite | None = None,
    agora=None,
) -> None:
    qtd = limite_ocupacao_conta(conta)
    if qtd is None:
        raise DivergenciaImpeditivaError(
            "Quantity local ausente; não é possível ocupar assento Multiuser."
        )
    momento = agora or utcnow_naive()
    excluir_id = int(convite_em_conversao.id) if convite_em_conversao is not None else None
    comprometido = contar_capacidade_comprometida(
        conta.id, momento, excluir_convite_id=excluir_id
    )
    if comprometido >= int(qtd):
        raise CapacidadeEsgotadaError(
            "Capacidade Multiuser esgotada: vínculos ativos e reservas válidas "
            "atingiram a quantity contratada."
        )


def _escolher_franquia_livre(
    conta_id: int,
    *,
    agora=None,
    excluir_convite_id: int | None = None,
) -> Franquia | None:
    ocupadas = _ids_franquias_ocupadas(conta_id)
    reservadas = ids_franquias_reservadas_validas(
        conta_id, agora, excluir_convite_id=excluir_convite_id
    )
    indisponiveis = ocupadas | reservadas
    for fr in _franquias_conta(conta_id):
        if int(fr.id) not in indisponiveis:
            return fr
    return None


def materializar_franquias_faltantes(
    conta: Conta,
    quantidade_alvo: int,
    *,
    limite_referencia: Decimal | None,
    aplicar_limite_nas_existentes: bool = False,
) -> list[Franquia]:
    """
    Cria somente Franquias faltantes até a quantity. Não apaga excesso.
    Nova Franquia entra no ciclo canônico corrente, com consumo inicial padrão.
    """
    franquias = _franquias_conta(conta.id)
    criadas: list[Franquia] = []
    if len(franquias) < int(quantidade_alvo):
        faltantes = int(quantidade_alvo) - len(franquias)
        idx_base = len(franquias)
        ciclo = resolver_ciclo_canonico_conta(conta.id)
        for i in range(faltantes):
            idx = idx_base + i + 1
            slug = f"multiuser-{idx}"
            if Franquia.query.filter_by(conta_id=conta.id, slug=slug).first():
                slug = f"multiuser-{idx}-{uuid.uuid4().hex[:6]}"[:80]
            fr = Franquia(
                conta_id=conta.id,
                nome=f"Franquia {idx}",
                slug=slug,
                status=Franquia.STATUS_ACTIVE,
            )
            if limite_referencia is not None:
                fr.limite_total = limite_referencia
            db.session.add(fr)
            db.session.flush()
            alinhar_franquia_ao_ciclo_conta(fr, ciclo)
            criadas.append(fr)
            logger.info(
                "Franquia Multiuser materializada conta_id=%s franquia_id=%s idx=%s",
                conta.id,
                fr.id,
                idx,
            )

    if aplicar_limite_nas_existentes and limite_referencia is not None:
        for fr in _franquias_conta(conta.id):
            fr.limite_total = limite_referencia
            db.session.add(fr)

    return criadas


def _papel_para_novo_vinculo(conta_id: int) -> str:
    existe_contratante = (
        ContaVinculoOrganizacional.query.filter_by(
            conta_id=int(conta_id),
            papel=PAPEL_CONTRATANTE,
            estado=ESTADO_ATIVO,
        )
        .first()
        is not None
    )
    return PAPEL_MEMBRO if existe_contratante else PAPEL_CONTRATANTE


def converter_reserva_convite_em_ocupacao(
    *,
    convite_id: int,
    user_id: int,
    papel: str | None = None,
    origem: str = ORIGEM_CONVITE,
    criado_por_user_id: int | None = None,
    commit: bool = True,
) -> ContaVinculoOrganizacional:
    """
    Operação de domínio: reserva pendente → vínculo ativo → convite aceito.
    Valida destinatário, Conta, Franquia, validade e capacidade na mesma transação.
    """
    row = db.session.get(ContaMultiuserConvite, int(convite_id))
    if row is None:
        raise VinculoInconsistenteError("Convite em conversão não encontrado.")
    return ocupar_assento(
        conta_id=int(row.conta_id),
        user_id=int(user_id),
        franquia_id=int(row.franquia_id),
        papel=papel or PAPEL_MEMBRO,
        origem=origem,
        criado_por_user_id=criado_por_user_id,
        commit=commit,
        convite_id_em_conversao=int(convite_id),
    )


def ocupar_assento(
    *,
    conta_id: int,
    user_id: int,
    franquia_id: int | None = None,
    papel: str | None = None,
    origem: str = ORIGEM_ADMIN_PLANO,
    criado_por_user_id: int | None = None,
    limite_referencia: Decimal | None = None,
    commit: bool = True,
    convite_id_em_conversao: int | None = None,
) -> ContaVinculoOrganizacional:
    """
    Ocupa um assento com lock da Conta, revalidação de quantity e vínculo atômico.
    Não preenche assento ocioso automaticamente: só ocupa o User informado.
    Capacidade comprometida = ativos + reservas F4 válidas.
    """
    conta = bloquear_conta_para_capacidade(conta_id)
    user = _bloquear_user(user_id)
    agora = utcnow_naive()
    liberar_reservas_expiradas_conta(conta.id, agora)

    ativo_user = (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=int(user.id),
            estado=ESTADO_ATIVO,
        )
        .first()
    )
    if ativo_user is not None:
        if int(ativo_user.conta_id) != int(conta.id):
            raise VinculoInconsistenteError(
                "User já possui vínculo organizacional ativo em outra Conta."
            )
        if franquia_id is not None and int(ativo_user.franquia_id) != int(franquia_id):
            raise VinculoInconsistenteError(
                "User já possui vínculo organizacional ativo em outra Franquia."
            )
        if commit:
            db.session.commit()
        return ativo_user

    convite_conversao = None
    if convite_id_em_conversao is not None:
        convite_conversao = validar_convite_em_conversao(
            conta_id=conta.id,
            convite_id=int(convite_id_em_conversao),
            user_id=int(user.id),
            franquia_id=franquia_id,
            agora=agora,
        )
        if franquia_id is None:
            franquia_id = int(convite_conversao.franquia_id)

    _exigir_capacidade_livre(conta, convite_em_conversao=convite_conversao, agora=agora)

    alvo: Franquia | None
    excluir_reserva_id = int(convite_conversao.id) if convite_conversao is not None else None
    if franquia_id is not None:
        alvo = _bloquear_franquia(franquia_id)
        if int(alvo.conta_id) != int(conta.id):
            raise VinculoInconsistenteError("Franquia não pertence à Conta informada.")
        ocupada = (
            ContaVinculoOrganizacional.query.filter_by(
                franquia_id=int(alvo.id),
                estado=ESTADO_ATIVO,
            )
            .first()
        )
        if ocupada is not None:
            raise VinculoInconsistenteError(
                "Já existe vínculo organizacional ativo para esta Franquia."
            )
        reservadas_alheias = ids_franquias_reservadas_validas(
            conta.id, agora, excluir_convite_id=excluir_reserva_id
        )
        if int(alvo.id) in reservadas_alheias:
            raise VinculoInconsistenteError(
                "Franquia reservada por convite pendente válido."
            )
    else:
        alvo = _escolher_franquia_livre(
            conta.id, agora=agora, excluir_convite_id=excluir_reserva_id
        )
        if alvo is None:
            qtd = conta.quantidade_assentos_contratados or 0
            if len(_franquias_conta(conta.id)) < int(qtd):
                novas = materializar_franquias_faltantes(
                    conta,
                    int(qtd),
                    limite_referencia=limite_referencia,
                    aplicar_limite_nas_existentes=False,
                )
                alvo = novas[0] if novas else _escolher_franquia_livre(
                    conta.id, agora=agora, excluir_convite_id=excluir_reserva_id
                )
            if alvo is None:
                raise CapacidadeEsgotadaError(
                    "Não há Franquia disponível para ocupar o assento contratado."
                )
        alvo = _bloquear_franquia(alvo.id)
        reservadas_alheias = ids_franquias_reservadas_validas(
            conta.id, agora, excluir_convite_id=excluir_reserva_id
        )
        if int(alvo.id) in reservadas_alheias:
            raise VinculoInconsistenteError(
                "Franquia reservada por convite pendente válido."
            )

    if int(user.conta_id) != int(conta.id):
        raise VinculoInconsistenteError("User.conta_id não corresponde à Conta do assento.")

    user.conta_id = conta.id
    user.franquia_id = alvo.id
    db.session.add(user)
    db.session.flush()

    papel_n = papel or _papel_para_novo_vinculo(conta.id)
    try:
        vinculo = criar_vinculo_organizacional(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=alvo.id,
            papel=papel_n,
            origem=origem,
            criado_por_user_id=criado_por_user_id,
            commit=False,
        )
    except VinculoOrganizacionalConflitoError as exc:
        raise VinculoInconsistenteError(str(exc)) from exc
    logger.info(
        "Assento Multiuser ocupado conta_id=%s user_id=%s franquia_id=%s papel=%s vinculo_id=%s",
        conta.id,
        user.id,
        alvo.id,
        papel_n,
        vinculo.id,
    )
    if convite_conversao is not None:
        convite_conversao.estado = ContaMultiuserConvite.ESTADO_ACEITO
        convite_conversao.accepted_at = agora
        convite_conversao.accepted_user_id = int(user.id)
        convite_conversao.updated_at = agora
        db.session.add(convite_conversao)
        db.session.flush()
        if (convite_conversao.estado or "") != ContaMultiuserConvite.ESTADO_ACEITO:
            raise VinculoInconsistenteError(
                "Conversão de reserva exige aceite persistido do convite."
            )
    if commit:
        db.session.commit()
    return vinculo


def aplicar_atribuicao_plano_multiuser_admin(
    *,
    user: User,
    quantidade_assentos: int,
    limite_referencia: Decimal | None,
    admin_user_id: int | None,
    commit: bool = True,
) -> ResultadoAtribuicaoMultiuserAdmin:
    """
    Write administrativo legado governado.

    Nova ativação Multiuser exige dados empresariais do FSD (incluindo CNPJ).
    Conta já Multiuser ativa (legado incompleto) pode ser mantida sem
    converter NULL histórico em inválido.
    """
    from app.services.conta_multiuser_reconciliacao_service import (
        executar_reconciliacao_multiuser_antes_enforcement,
    )

    executar_reconciliacao_multiuser_antes_enforcement(commit=False)
    db.session.expire_all()
    user = db.session.get(User, int(user.id))
    if user is None:
        raise ValueError("User não encontrado.")
    if not user.conta_id:
        raise VinculoInconsistenteError("User sem Conta operacional para atribuição Multiuser.")

    conta = bloquear_conta_para_capacidade(user.conta_id)
    user = _bloquear_user(user.id)
    liberar_reservas_expiradas_conta(conta.id)

    validar_quantity_nao_inferior_ao_comprometido(conta, int(quantidade_assentos))

    persistir_quantidade_assentos_contratados(
        conta.id,
        int(quantidade_assentos),
        commit=False,
    )
    nova_ativacao = not bool(conta.multiuser_ativa)
    marcar_conta_multiuser_ativa(
        conta.id,
        exigir_cnpj=nova_ativacao,
        commit=False,
    )
    db.session.refresh(conta)

    criadas = materializar_franquias_faltantes(
        conta,
        int(quantidade_assentos),
        limite_referencia=limite_referencia,
        aplicar_limite_nas_existentes=True,
    )

    franquias = _franquias_conta(conta.id)
    if not franquias:
        raise VinculoInconsistenteError("Conta Multiuser sem Franquia operacional.")

    vinculo_existente = (
        ContaVinculoOrganizacional.query.filter_by(
            user_id=int(user.id),
            estado=ESTADO_ATIVO,
        )
        .first()
    )
    idempotente = False
    if vinculo_existente is not None:
        if int(vinculo_existente.conta_id) != int(conta.id):
            raise VinculoInconsistenteError(
                "User já possui vínculo organizacional ativo em outra Conta."
            )
        user.categoria = "multiuser"
        user.conta_id = conta.id
        user.franquia_id = vinculo_existente.franquia_id
        db.session.add(user)
        db.session.flush()
        vinculo = vinculo_existente
        idempotente = True
        papel = vinculo.papel
    else:
        papel = _papel_para_novo_vinculo(conta.id)
        if papel == PAPEL_CONTRATANTE:
            alvo = franquias[0]
            ocupada = (
                ContaVinculoOrganizacional.query.filter_by(
                    franquia_id=int(alvo.id),
                    estado=ESTADO_ATIVO,
                )
                .first()
            )
            if ocupada is not None:
                alvo = _escolher_franquia_livre(conta.id) or alvo
        else:
            alvo = _escolher_franquia_livre(conta.id)
            if alvo is None:
                raise CapacidadeEsgotadaError(
                    "Capacidade Multiuser esgotada: não há Franquia livre para o novo membro."
                )

        user.categoria = "multiuser"
        user.conta_id = conta.id
        user.franquia_id = alvo.id
        db.session.add(user)
        db.session.flush()

        vinculo = ocupar_assento(
            conta_id=conta.id,
            user_id=user.id,
            franquia_id=alvo.id,
            papel=papel,
            origem=ORIGEM_ADMIN_PLANO,
            criado_por_user_id=admin_user_id,
            limite_referencia=limite_referencia,
            commit=False,
        )

    snap = snapshot_capacidade(conta.id)
    if snap.divergencias:
        logger.info(
            "Divergencias Multiuser apos write admin conta_id=%s tipos=%s resultado=persistido",
            conta.id,
            ",".join(sorted({d.codigo for d in snap.divergencias})),
        )
    logger.info(
        "Governanca Multiuser admin conta_id=%s user_id=%s quantity=%s vinculo_id=%s criadas=%s idempotente=%s",
        conta.id,
        user.id,
        quantidade_assentos,
        vinculo.id,
        len(criadas),
        idempotente,
    )
    resultado = ResultadoAtribuicaoMultiuserAdmin(
        conta_id=conta.id,
        user_id=user.id,
        franquia_id=int(user.franquia_id),
        quantidade_assentos=int(quantidade_assentos),
        franquias_criadas=criadas,
        vinculo_id=vinculo.id,
        papel=papel,
        idempotente=idempotente,
    )
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return resultado
