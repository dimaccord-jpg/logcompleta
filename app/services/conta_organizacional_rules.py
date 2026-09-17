"""
Regras puras da fundação organizacional Multiuser (Fase 1).
Sem I/O, sem Flask, para reuso em service e backfill.
"""
from __future__ import annotations

from dataclasses import dataclass, field

PAPEL_CONTRATANTE = "contratante"
PAPEL_MEMBRO = "membro"
PAPEIS_V1 = frozenset({PAPEL_CONTRATANTE, PAPEL_MEMBRO})

ESTADO_ATIVO = "ativo"
ESTADO_ENCERRADO = "encerrado"
ESTADOS_V1 = frozenset({ESTADO_ATIVO, ESTADO_ENCERRADO})

ORIGEM_BACKFILL_LEGADO = "backfill_legado"
ORIGEM_DOMINIO = "dominio"
ORIGEM_ADMIN_PLANO = "admin_plano"
ORIGEM_CONTRATACAO_STRIPE = "contratacao_stripe"
ORIGEM_CONVITE = "convite"

# Fase 4: convite reserva Franquia; aceite ocupa. Locks: Contas(id ordenado) → convite → User → Franquia.
ORDEM_LOCK_CONVITE = ("conta", "convite", "vinculo_user", "franquia")
UQ_CONVITE_FRANQUIA_PENDENTE = "uq_conta_convite_franquia_pendente"
UQ_CONVITE_CONTA_EMAIL_PENDENTE = "uq_conta_convite_conta_email_pendente"

CATEGORIAS_MULTIUSER = frozenset({"multiuser", "enterprise"})
SLUG_CONTA_SISTEMA = "sistema-interno"

CODIGO_MULTIPLOS_USERS_MESMA_FRANQUIA = "multiplos_users_mesma_franquia"
CODIGO_USER_FRANQUIA_CONTA_DIVERGENTE = "user_franquia_conta_divergente"
CODIGO_CONTA_SEM_CONTRATANTE = "conta_sem_contratante_deterministico"
CODIGO_USER_SEM_FRANQUIA_DA_CONTA = "user_sem_franquia_da_conta"

CODIGO_DIV_ATIVOS_MAIOR_QUE_QUANTITY = "ativos_maior_que_quantity"
CODIGO_DIV_FRANQUIAS_MENOR_QUE_CAPACITY = "franquias_menor_que_capacity"
CODIGO_DIV_FRANQUIAS_MAIOR_QUE_QUANTITY = "franquias_maior_que_quantity"
CODIGO_DIV_CICLO_DIVERGENTE = "ciclo_divergente"
CODIGO_DIV_USER_CONTA_FRANQUIA_INCOERENTE = "user_conta_franquia_incoerente"
CODIGO_DIV_QUANTITY_EXTERNA_LOCAL = "quantity_externa_local_divergente"

FONTE_CICLO_VINCULO_MONETIZACAO = "conta_monetizacao_vinculo"
FONTE_CICLO_FRANQUIAS_UNANIMES = "franquias_unanimidade"
FONTE_CICLO_INCONCLUSIVO = "inconclusivo"
FONTE_CICLO_DIVERGENTE = "divergente"

ORDEM_LOCK_CAPACIDADE = ("conta", "vinculo_user", "franquia")

# Fase 5: autoridade normativa V1 do aumento automático cumulativo por ciclo.
# Leitura operacional: plano_service.obter_limite_aumento_automatico_ciclo_multiuser()
# (ConfigRegras /admin/planos). Este constante só entra se a config ainda não existir.
LIMITE_AUMENTO_AUTOMATICO_CICLO_V1 = 5
CHAVE_LIMITE_AUMENTO_AUTOMATICO_CICLO = (
    "plano_limite_aumento_automatico_ciclo_admin_multiuser"
)
ESTADO_GERENCIAL_AGUARDANDO_VINCULO = "aguardando_vinculo"
ESTADO_GERENCIAL_PENDENTE_ATIVACAO = "pendente_ativacao"
ESTADO_GERENCIAL_ATIVO = "ativo"
ESTADO_GERENCIAL_REVOGADO = "revogado"

UQ_CONTA_CNPJ_MULTIUSER_ATIVA = "uq_conta_cnpj_multiuser_ativa"
UQ_VINCULO_USER_ATIVO = "uq_conta_vinculo_org_user_ativo"
UQ_VINCULO_FRANQUIA_ATIVO = "uq_conta_vinculo_org_franquia_ativo"
UQ_VINCULO_CONTRATANTE_ATIVO = "uq_conta_vinculo_org_contratante_ativo"

# Fase 8: classificação de diagnóstico. Não altera regras comerciais F1–F7.
STATUS_DIAG_OK = "ok"
STATUS_DIAG_DIVERGENTE = "divergente"
STATUS_DIAG_PENDENTE_ESPERADO = "pendente_esperado"
STATUS_DIAG_RECONCILIACAO_NECESSARIA = "reconciliacao_necessaria"
STATUS_DIAG_INCONCLUSIVO = "inconclusivo"

CODIGO_DIAG_QUANTITY_LOCAL_STRIPE = "quantity_local_stripe"
CODIGO_DIAG_CICLO = "ciclo_conta_franquia"
CODIGO_DIAG_MEMBERSHIP = "membership"
CODIGO_DIAG_CONVITES = "convites"
CODIGO_DIAG_F6_PARCIAL = "f6_parcial"
CODIGO_DIAG_REDUCAO_PENDENTE = "reducao_pendente"
CODIGO_DIAG_STRIPE_INDISPONIVEL = "stripe_indisponivel"
CODIGO_DIAG_CONTRATANTES = "contratantes_ativos"
CODIGO_DIAG_OCUPACAO = "ocupacao_vs_capacity"
CODIGO_DIAG_F5_EM_PROCESSAMENTO = "f5_em_processamento"

STRIPE_CONSULTA_OK = "ok"
STRIPE_CONSULTA_INDISPONIVEL = "indisponivel"
STRIPE_CONSULTA_NAO_CONSULTADO = "nao_consultado"
STRIPE_CONSULTA_NAO_APLICAVEL = "nao_aplicavel"


@dataclass(frozen=True)
class UserLegado:
    id: int
    conta_id: int
    franquia_id: int
    categoria: str


@dataclass(frozen=True)
class FranquiaLegada:
    id: int
    conta_id: int


@dataclass(frozen=True)
class ContaLegada:
    id: int
    slug: str
    status: str


@dataclass(frozen=True)
class VinculoPlanejado:
    conta_id: int
    user_id: int
    franquia_id: int
    papel: str
    titular: bool
    origem: str = ORIGEM_BACKFILL_LEGADO


@dataclass(frozen=True)
class InconsistenciaPlanejada:
    codigo: str
    detalhe: str
    conta_id: int | None = None
    franquia_id: int | None = None
    user_id: int | None = None


@dataclass
class PlanoBackfillMultiuser:
    contas_multiuser_ids: list[int] = field(default_factory=list)
    quantidade_por_conta: dict[int, int] = field(default_factory=dict)
    vinculos: list[VinculoPlanejado] = field(default_factory=list)
    inconsistencias: list[InconsistenciaPlanejada] = field(default_factory=list)


def categoria_eh_multiuser(categoria: str | None) -> bool:
    return (categoria or "").strip().lower() in CATEGORIAS_MULTIUSER


def papel_valido(papel: str | None) -> bool:
    return (papel or "").strip().lower() in PAPEIS_V1


def estado_valido(estado: str | None) -> bool:
    return (estado or "").strip().lower() in ESTADOS_V1


def normalizar_papel(papel: str | None) -> str:
    valor = (papel or "").strip().lower()
    if valor not in PAPEIS_V1:
        raise ValueError("Papel organizacional inválido. Valores permitidos: contratante, membro.")
    return valor


def titular_obrigatorio_de_papel(papel: str | None) -> bool:
    """Titularidade é derivada do papel: contratante=True, membro=False."""
    return normalizar_papel(papel) == PAPEL_CONTRATANTE


def quantidade_assentos_valida(quantidade) -> int:
    if isinstance(quantidade, bool) or not isinstance(quantidade, int):
        raise ValueError("Quantidade de assentos deve ser um inteiro positivo.")
    if quantidade < 1:
        raise ValueError("Quantidade de assentos deve ser maior ou igual a 1.")
    return quantidade


def classificar_backfill_multiuser_legado(
    *,
    contas: list[ContaLegada],
    franquias: list[FranquiaLegada],
    users: list[UserLegado],
    sistema_conta_id: int | None,
) -> PlanoBackfillMultiuser:
    """
    Determina vínculos iniciais sem inventar associação ambígua.

    Primeira Franquia da Conta (menor id): Contratante somente se houver exatamente
    1 User Multiuser elegível. 0 ou 2+ Users: inconsistência, nenhum vínculo inventado.
    Demais Franquias: Membro somente se houver exatamente 1 User elegível.
    """
    plano = PlanoBackfillMultiuser()
    franquias_por_conta: dict[int, list[FranquiaLegada]] = {}
    franquia_por_id = {fr.id: fr for fr in franquias}
    for fr in franquias:
        franquias_por_conta.setdefault(fr.conta_id, []).append(fr)
    for lista in franquias_por_conta.values():
        lista.sort(key=lambda item: item.id)

    users_multiuser_por_conta: dict[int, list[UserLegado]] = {}
    for user in users:
        if not categoria_eh_multiuser(user.categoria):
            continue
        if sistema_conta_id is not None and user.conta_id == sistema_conta_id:
            continue
        users_multiuser_por_conta.setdefault(user.conta_id, []).append(user)

    contas_por_id = {c.id: c for c in contas}

    for conta_id, users_conta in sorted(users_multiuser_por_conta.items()):
        conta = contas_por_id.get(conta_id)
        if conta is None or conta.slug == SLUG_CONTA_SISTEMA:
            continue
        if sistema_conta_id is not None and conta.id == sistema_conta_id:
            continue

        plano.contas_multiuser_ids.append(conta.id)
        franquias_conta = franquias_por_conta.get(conta.id, [])
        plano.quantidade_por_conta[conta.id] = max(1, len(franquias_conta)) if franquias_conta else 1

        if not franquias_conta:
            plano.inconsistencias.append(
                InconsistenciaPlanejada(
                    codigo=CODIGO_CONTA_SEM_CONTRATANTE,
                    detalhe="Conta Multiuser sem franquia para vínculo inicial.",
                    conta_id=conta.id,
                )
            )
            continue

        primeira_franquia_id = franquias_conta[0].id
        users_por_franquia: dict[int, list[UserLegado]] = {}
        for user in users_conta:
            fr = franquia_por_id.get(user.franquia_id)
            if fr is None or fr.conta_id != conta.id:
                plano.inconsistencias.append(
                    InconsistenciaPlanejada(
                        codigo=CODIGO_USER_FRANQUIA_CONTA_DIVERGENTE,
                        detalhe="User Multiuser com franquia fora da Conta operacional; vínculo não criado.",
                        conta_id=conta.id,
                        franquia_id=user.franquia_id,
                        user_id=user.id,
                    )
                )
                continue
            users_por_franquia.setdefault(user.franquia_id, []).append(user)

        contratante_definido = False
        candidatos_master = list(users_por_franquia.get(primeira_franquia_id, []))
        if len(candidatos_master) == 1:
            master = candidatos_master[0]
            plano.vinculos.append(
                VinculoPlanejado(
                    conta_id=conta.id,
                    user_id=master.id,
                    franquia_id=primeira_franquia_id,
                    papel=PAPEL_CONTRATANTE,
                    titular=titular_obrigatorio_de_papel(PAPEL_CONTRATANTE),
                )
            )
            contratante_definido = True
        elif len(candidatos_master) > 1:
            plano.inconsistencias.append(
                InconsistenciaPlanejada(
                    codigo=CODIGO_MULTIPLOS_USERS_MESMA_FRANQUIA,
                    detalhe=(
                        "Mais de um User Multiuser na primeira franquia; "
                        "contratante não inventado."
                    ),
                    conta_id=conta.id,
                    franquia_id=primeira_franquia_id,
                )
            )

        for fr in franquias_conta[1:]:
            ocupantes = list(users_por_franquia.get(fr.id, []))
            if len(ocupantes) == 1:
                plano.vinculos.append(
                    VinculoPlanejado(
                        conta_id=conta.id,
                        user_id=ocupantes[0].id,
                        franquia_id=fr.id,
                        papel=PAPEL_MEMBRO,
                        titular=titular_obrigatorio_de_papel(PAPEL_MEMBRO),
                    )
                )
            elif len(ocupantes) > 1:
                plano.inconsistencias.append(
                    InconsistenciaPlanejada(
                        codigo=CODIGO_MULTIPLOS_USERS_MESMA_FRANQUIA,
                        detalhe="Mais de um User Multiuser na mesma franquia; vínculo não inventado.",
                        conta_id=conta.id,
                        franquia_id=fr.id,
                    )
                )

        if not contratante_definido:
            detalhe_sem_contratante = (
                "Mais de um User Multiuser na primeira franquia; contratante não inventado."
                if len(candidatos_master) > 1
                else "Nenhum User Multiuser na primeira franquia; contratante não inventado."
            )
            plano.inconsistencias.append(
                InconsistenciaPlanejada(
                    codigo=CODIGO_CONTA_SEM_CONTRATANTE,
                    detalhe=detalhe_sem_contratante,
                    conta_id=conta.id,
                    franquia_id=primeira_franquia_id,
                )
            )

    return plano
