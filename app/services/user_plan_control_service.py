"""
Orquestração transacional de atribuição de plano no Controle de Usuários.
"""
from __future__ import annotations

import re
import secrets
import uuid
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func

from app.extensions import db
from app.models import Conta, Franquia, MultiuserFranquiaCodigo, User
from app.services import plano_service
from app.services.cleiton_ciclo_franquia_service import (
    garantir_ciclo_operacional_franquia,
)
from app.services.cleiton_franquia_operacional_service import (
    aplicar_status_apos_mudanca_estrutural,
)
from app.services.conta_franquia_service import (
    criar_conta_franquia_para_cadastro,
    get_sistema_interno_ids,
)

PLANO_FREE = "free"
PLANO_STARTER = "starter"
PLANO_PRO = "pro"
PLANO_MULTIUSER = "multiuser"
PLANO_AVULSO = "avulso"
PLANO_USO_ADM = "uso_adm"

PLANOS_SUPORTADOS = {
    PLANO_FREE,
    PLANO_STARTER,
    PLANO_PRO,
    PLANO_MULTIUSER,
    PLANO_AVULSO,
    PLANO_USO_ADM,
}

_CODE_SANITIZER = re.compile(r"[^A-Z0-9]")


@dataclass(frozen=True)
class ResultadoAtribuicaoPlano:
    user_email: str
    plano_anterior: str
    plano_novo: str
    franquia_id: int | None
    conta_id: int | None
    franquias_multiuser_criadas: int
    codigos_gerados: tuple[str, ...]


def _normalize_email(email: str | None) -> str:
    return (email or "").strip().lower()


def _parse_plano(plano_raw: str | None) -> str:
    plano = (plano_raw or "").strip().lower()
    if plano not in PLANOS_SUPORTADOS:
        raise ValueError("Plano inválido para atribuição.")
    return plano


def _get_user_by_email(email: str | None) -> User:
    email_n = _normalize_email(email)
    if not email_n:
        raise ValueError("Informe um e-mail válido.")
    user = (
        User.query.filter(func.lower(User.email) == email_n)
        .order_by(User.id.asc())
        .first()
    )
    if user is None:
        raise ValueError("Usuário não encontrado para o e-mail informado.")
    return user


def _to_decimal_or_none(v) -> Decimal | None:
    if v is None:
        return None
    if isinstance(v, Decimal):
        return v
    return Decimal(str(v))


def _get_limite_referencia_plano(plano_codigo: str) -> Decimal | None:
    planos = {
        p["codigo"]: p for p in plano_service.listar_planos_saas_admin()
    }
    if plano_codigo not in planos:
        raise ValueError(f"Plano '{plano_codigo}' não encontrado na configuração administrativa.")
    limite = planos[plano_codigo].get("franquia_referencia")
    limite_d = _to_decimal_or_none(limite)
    if plano_codigo in (PLANO_STARTER, PLANO_PRO, PLANO_MULTIUSER, PLANO_AVULSO) and limite_d is None:
        raise ValueError(
            f"Franquia de referência do plano {plano_codigo} não está configurada em /admin/planos."
        )
    return limite_d


def _ensure_commercial_structure(user: User) -> tuple[Conta, Franquia]:
    conta, franquia = None, None
    if user.conta_id:
        conta = db.session.get(Conta, int(user.conta_id))
    if user.franquia_id:
        franquia = db.session.get(Franquia, int(user.franquia_id))

    sistema_conta_id, sistema_franquia_id = get_sistema_interno_ids()
    em_estrutura_interna = (
        (sistema_conta_id is not None and user.conta_id == sistema_conta_id)
        or (sistema_franquia_id is not None and user.franquia_id == sistema_franquia_id)
    )
    estrutura_invalida = (
        conta is None
        or franquia is None
        or franquia.conta_id != conta.id
        or em_estrutura_interna
    )

    if not estrutura_invalida:
        return conta, franquia

    nome = (user.full_name or user.email or "Usuário")[:255]
    conta_nova, franquia_nova = criar_conta_franquia_para_cadastro(user.email, nome)
    user.conta_id = conta_nova.id
    user.franquia_id = franquia_nova.id
    db.session.add(user)
    db.session.flush()
    return conta_nova, franquia_nova


def _new_multiuser_code() -> str:
    raw = f"MU-{secrets.token_urlsafe(10).upper()}"
    return _CODE_SANITIZER.sub("", raw)[:32]


def _create_code_for_franquia(
    *,
    conta_id: int,
    franquia_id: int,
    admin_user_id: int | None,
) -> str:
    for _ in range(5):
        code = _new_multiuser_code()
        exists = MultiuserFranquiaCodigo.query.filter_by(codigo=code).first()
        if exists:
            continue
        row = MultiuserFranquiaCodigo(
            conta_id=conta_id,
            franquia_id=franquia_id,
            codigo=code,
            ativo=True,
            criado_por_user_id=admin_user_id,
        )
        db.session.add(row)
        db.session.flush()
        return code
    raise ValueError("Não foi possível gerar código único de acesso para Multiuser.")


def _ensure_multiuser_franquias(
    *,
    user: User,
    quantidade_franquias: int,
    limite_referencia: Decimal | None,
    admin_user_id: int | None,
) -> tuple[list[Franquia], list[str]]:
    conta, _fr = _ensure_commercial_structure(user)
    franquias_conta = (
        Franquia.query.filter_by(conta_id=conta.id)
        .order_by(Franquia.id.asc())
        .all()
    )
    criadas: list[Franquia] = []
    codigos: list[str] = []

    if len(franquias_conta) < quantidade_franquias:
        faltantes = quantidade_franquias - len(franquias_conta)
        idx_base = len(franquias_conta)
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
            db.session.add(fr)
            db.session.flush()
            criadas.append(fr)
        franquias_conta = (
            Franquia.query.filter_by(conta_id=conta.id)
            .order_by(Franquia.id.asc())
            .all()
        )

    # Aplica limite de referência em todas as franquias operacionais da conta Multiuser.
    for fr in franquias_conta:
        fr.limite_total = limite_referencia
        db.session.add(fr)

    # Mantém usuário master apontando para a primeira franquia da conta.
    if franquias_conta:
        user.franquia_id = franquias_conta[0].id
    user.conta_id = conta.id
    user.categoria = PLANO_MULTIUSER
    db.session.add(user)
    db.session.flush()

    for fr in criadas:
        code = _create_code_for_franquia(
            conta_id=conta.id,
            franquia_id=fr.id,
            admin_user_id=admin_user_id,
        )
        codigos.append(code)

    return franquias_conta, codigos


def _alinhar_governanca_franquias(franquias: list[Franquia]) -> None:
    for fr in franquias:
        fr.inicio_ciclo = None
        fr.fim_ciclo = None
        db.session.add(fr)
    db.session.flush()
    for fr in franquias:
        garantir_ciclo_operacional_franquia(fr.id)
        aplicar_status_apos_mudanca_estrutural(fr.id)


def atribuir_plano_para_usuario(
    *,
    email: str,
    plano_raw: str,
    quantidade_franquias_raw: str | None = None,
    admin_user_id: int | None = None,
) -> ResultadoAtribuicaoPlano:
    user = _get_user_by_email(email)
    plano_novo = _parse_plano(plano_raw)
    plano_anterior = (user.categoria or "").strip().lower() or PLANO_FREE

    if plano_novo == PLANO_USO_ADM:
        conta_id, franquia_id = get_sistema_interno_ids()
        if conta_id is None or franquia_id is None:
            raise ValueError("Estrutura interna (Conta/Franquia sistema) não encontrada.")
        user.categoria = "interna"
        user.conta_id = int(conta_id)
        user.franquia_id = int(franquia_id)
        db.session.add(user)
        db.session.commit()
        return ResultadoAtribuicaoPlano(
            user_email=user.email,
            plano_anterior=plano_anterior,
            plano_novo=plano_novo,
            franquia_id=user.franquia_id,
            conta_id=user.conta_id,
            franquias_multiuser_criadas=0,
            codigos_gerados=(),
        )

    limite_referencia = _get_limite_referencia_plano(plano_novo)

    if plano_novo == PLANO_MULTIUSER:
        qtd_txt = (quantidade_franquias_raw or "").strip()
        if not qtd_txt:
            raise ValueError("Informe a quantidade de franquias para o plano Multiuser.")
        try:
            qtd = int(qtd_txt)
        except ValueError:
            raise ValueError("Quantidade de franquias inválida para Multiuser.")
        if qtd <= 0:
            raise ValueError("Quantidade de franquias deve ser maior que zero.")
        franquias, codigos = _ensure_multiuser_franquias(
            user=user,
            quantidade_franquias=qtd,
            limite_referencia=limite_referencia,
            admin_user_id=admin_user_id,
        )
        db.session.commit()
        _alinhar_governanca_franquias(franquias)
        user_atualizado = db.session.get(User, user.id)
        return ResultadoAtribuicaoPlano(
            user_email=user_atualizado.email,
            plano_anterior=plano_anterior,
            plano_novo=plano_novo,
            franquia_id=user_atualizado.franquia_id,
            conta_id=user_atualizado.conta_id,
            franquias_multiuser_criadas=max(0, len(codigos)),
            codigos_gerados=tuple(codigos),
        )

    conta, franquia = _ensure_commercial_structure(user)
    user.categoria = plano_novo
    user.conta_id = conta.id
    user.franquia_id = franquia.id
    franquia.limite_total = limite_referencia
    db.session.add(user)
    db.session.add(franquia)
    db.session.commit()

    _alinhar_governanca_franquias([franquia])

    user_atualizado = db.session.get(User, user.id)
    return ResultadoAtribuicaoPlano(
        user_email=user_atualizado.email,
        plano_anterior=plano_anterior,
        plano_novo=plano_novo,
        franquia_id=user_atualizado.franquia_id,
        conta_id=user_atualizado.conta_id,
        franquias_multiuser_criadas=0,
        codigos_gerados=(),
    )
