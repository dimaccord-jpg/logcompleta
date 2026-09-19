"""
Fundação organizacional Multiuser (Fase 1): CNPJ da Conta, vínculo e quantity local.
Não inicia Checkout, não chama Stripe e não altera autorização pública existente.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Conta,
    ContaVinculoOrganizacional,
    Franquia,
    User,
    utcnow_naive,
)
from app.services.cnpj_service import (
    CnpjInvalidoError,
    exigir_cnpj_normalizado_valido,
    normalizar_cnpj_opcional,
)
from app.services.conta_organizacional_rules import (
    ESTADO_ATIVO,
    ESTADO_ENCERRADO,
    ORIGEM_DOMINIO,
    PAPEL_CONTRATANTE,
    UQ_CONTA_CNPJ_MULTIUSER_ATIVA,
    UQ_VINCULO_CONTRATANTE_ATIVO,
    UQ_VINCULO_FRANQUIA_ATIVO,
    UQ_VINCULO_USER_ATIVO,
    normalizar_papel,
    quantidade_assentos_valida,
    titular_obrigatorio_de_papel,
)

logger = logging.getLogger(__name__)

_CEP_DIGITS = re.compile(r"\D+")
_UFS_BR = frozenset(
    {
        "AC", "AL", "AP", "AM", "BA", "CE", "DF", "ES", "GO", "MA",
        "MT", "MS", "MG", "PA", "PB", "PR", "PE", "PI", "RJ", "RN",
        "RS", "RO", "RR", "SC", "SP", "SE", "TO",
    }
)


class CnpjMultiuserDuplicadoError(ValueError):
    """CNPJ já utilizado por outra Conta Multiusuário ativa."""


class DadosEmpresariaisMultiuserIncompletosError(ValueError):
    """Nova ativação Multiuser exige os dados empresariais mínimos do FSD."""


class VinculoOrganizacionalConflitoError(ValueError):
    """Violação de unicidade de vínculo organizacional ativo."""


_CONSTRAINTS_CNPJ = frozenset({UQ_CONTA_CNPJ_MULTIUSER_ATIVA})
_CONSTRAINTS_VINCULO = frozenset(
    {
        UQ_VINCULO_USER_ATIVO,
        UQ_VINCULO_FRANQUIA_ATIVO,
        UQ_VINCULO_CONTRATANTE_ATIVO,
    }
)
_SQLITE_UNIQUE_PARA_CONSTRAINT = (
    ("UNIQUE constraint failed: conta.cnpj", UQ_CONTA_CNPJ_MULTIUSER_ATIVA),
    (
        f"UNIQUE constraint failed: {UQ_CONTA_CNPJ_MULTIUSER_ATIVA}",
        UQ_CONTA_CNPJ_MULTIUSER_ATIVA,
    ),
    (
        "UNIQUE constraint failed: conta_vinculo_organizacional.user_id",
        UQ_VINCULO_USER_ATIVO,
    ),
    (
        f"UNIQUE constraint failed: {UQ_VINCULO_USER_ATIVO}",
        UQ_VINCULO_USER_ATIVO,
    ),
    (
        "UNIQUE constraint failed: conta_vinculo_organizacional.franquia_id",
        UQ_VINCULO_FRANQUIA_ATIVO,
    ),
    (
        f"UNIQUE constraint failed: {UQ_VINCULO_FRANQUIA_ATIVO}",
        UQ_VINCULO_FRANQUIA_ATIVO,
    ),
    (
        "UNIQUE constraint failed: conta_vinculo_organizacional.conta_id",
        UQ_VINCULO_CONTRATANTE_ATIVO,
    ),
    (
        f"UNIQUE constraint failed: {UQ_VINCULO_CONTRATANTE_ATIVO}",
        UQ_VINCULO_CONTRATANTE_ATIVO,
    ),
)


def identificar_constraint_integrity_error(exc: IntegrityError) -> str | None:
    """
    Identifica somente constraints/índices reconhecidos desta fundação.
    PostgreSQL: orig.diag.constraint_name quando disponível.
    SQLite: mensagem exata de UNIQUE constraint failed do driver.
    Retorna None para violação desconhecida (caller deve repropagar).
    """
    orig = getattr(exc, "orig", None)
    diag = getattr(orig, "diag", None) if orig is not None else None
    nome_pg = getattr(diag, "constraint_name", None) if diag is not None else None
    if nome_pg:
        nome = str(nome_pg)
        reconhecidas = _CONSTRAINTS_CNPJ | _CONSTRAINTS_VINCULO
        if nome in reconhecidas:
            return nome
        return None
    msg = str(orig if orig is not None else exc)
    for fragmento, nome in _SQLITE_UNIQUE_PARA_CONSTRAINT:
        if fragmento in msg:
            return nome
    return None


def _erro_dominio_para_constraint(nome: str) -> Exception | None:
    if nome == UQ_CONTA_CNPJ_MULTIUSER_ATIVA:
        return CnpjMultiuserDuplicadoError(
            "CNPJ já utilizado por outra Conta Multiusuário ativa."
        )
    if nome == UQ_VINCULO_USER_ATIVO:
        return VinculoOrganizacionalConflitoError(
            "Já existe vínculo organizacional ativo para este User."
        )
    if nome == UQ_VINCULO_FRANQUIA_ATIVO:
        return VinculoOrganizacionalConflitoError(
            "Já existe vínculo organizacional ativo para esta Franquia."
        )
    if nome == UQ_VINCULO_CONTRATANTE_ATIVO:
        return VinculoOrganizacionalConflitoError(
            "Já existe Contratante ativo nesta Conta."
        )
    return None


def converter_ou_relancar_integrity_error(exc: IntegrityError) -> None:
    """Nunca retorna: converte constraint reconhecida ou relança o IntegrityError original."""
    nome = identificar_constraint_integrity_error(exc)
    dominio = _erro_dominio_para_constraint(nome) if nome else None
    if dominio is None:
        raise exc
    raise dominio from exc


def _strip_opt(raw: str | None, max_len: int) -> str | None:
    if raw is None:
        return None
    txt = str(raw).strip()
    if not txt:
        return None
    return txt[:max_len]


def _normalizar_uf(raw: str | None) -> str | None:
    txt = _strip_opt(raw, 2)
    if txt is None:
        return None
    uf = txt.upper()
    if len(uf) != 2 or uf not in _UFS_BR:
        raise ValueError("UF inválida.")
    return uf


def _normalizar_cep(raw: str | None) -> str | None:
    if raw is None or not str(raw).strip():
        return None
    digits = _CEP_DIGITS.sub("", str(raw))
    if not digits:
        return None
    if len(digits) != 8:
        raise ValueError("CEP deve conter 8 dígitos.")
    return digits


def _normalizar_email_empresarial(raw: str | None) -> str | None:
    txt = _strip_opt(raw, 150)
    if txt is None:
        return None
    return txt.lower()


def _conta_ou_erro(conta_id: int) -> Conta:
    conta = db.session.get(Conta, int(conta_id))
    if conta is None:
        raise ValueError("Conta não encontrada.")
    return conta


def conta_multiuser_ativa_elegivel_unicidade(conta: Conta) -> bool:
    return bool(conta.multiuser_ativa) and (conta.status or "") == Conta.STATUS_ATIVA


def buscar_conta_multiuser_ativa_por_cnpj(cnpj: str, *, excluir_conta_id: int | None = None) -> Conta | None:
    q = Conta.query.filter(
        Conta.cnpj == cnpj,
        Conta.multiuser_ativa.is_(True),
        Conta.status == Conta.STATUS_ATIVA,
    )
    if excluir_conta_id is not None:
        q = q.filter(Conta.id != int(excluir_conta_id))
    return q.order_by(Conta.id.asc()).first()


def _assert_cnpj_unico_multiuser_ativa(cnpj: str, *, conta_id: int | None) -> None:
    existente = buscar_conta_multiuser_ativa_por_cnpj(cnpj, excluir_conta_id=conta_id)
    if existente is not None:
        raise CnpjMultiuserDuplicadoError(
            "CNPJ já utilizado por outra Conta Multiusuário ativa."
        )


def exigir_cnpj_para_ativacao_multiuser(conta: Conta) -> str:
    """Governança de ativação: Conta Multiuser ativa exige CNPJ válido. Legado sem flag não exige."""
    if not conta.multiuser_ativa:
        raise ValueError("Governança de CNPJ aplicável apenas à Conta marcada como Multiuser.")
    try:
        return exigir_cnpj_normalizado_valido(conta.cnpj)
    except CnpjInvalidoError as exc:
        raise CnpjInvalidoError(
            "CNPJ é obrigatório e deve ser válido para Conta Multiusuário."
        ) from exc


_CAMPOS_EMPRESARIAIS_OBRIGATORIOS_CONTA = (
    ("razao_social", "Razão Social"),
    ("nome_fantasia", "Nome Fantasia"),
    ("email_empresarial", "e-mail empresarial"),
    ("endereco_logradouro", "logradouro"),
    ("endereco_numero", "número"),
    ("endereco_cidade", "cidade"),
    ("endereco_uf", "UF"),
    ("endereco_cep", "CEP"),
)


def exigir_dados_empresariais_para_ativacao_multiuser(conta: Conta) -> str:
    """
    FSD §5 / CA-52: nova ativação Multiuser exige dados empresariais mínimos
    e CNPJ válido/único. Não aplica retroativamente a Conta legado já ativa.
    """
    faltantes = [
        rotulo
        for campo, rotulo in _CAMPOS_EMPRESARIAIS_OBRIGATORIOS_CONTA
        if not str(getattr(conta, campo, None) or "").strip()
    ]
    if faltantes:
        raise DadosEmpresariaisMultiuserIncompletosError(
            "Dados empresariais incompletos para ativar Conta Multiusuário: "
            + ", ".join(faltantes)
            + "."
        )
    cnpj_norm = exigir_cnpj_normalizado_valido(conta.cnpj)
    _assert_cnpj_unico_multiuser_ativa(cnpj_norm, conta_id=int(conta.id))
    return cnpj_norm


def persistir_dados_empresariais_conta(
    conta_id: int,
    *,
    razao_social: str | None = None,
    nome_fantasia: str | None = None,
    cnpj: str | None = None,
    email_empresarial: str | None = None,
    endereco_logradouro: str | None = None,
    endereco_numero: str | None = None,
    endereco_complemento: str | None = None,
    endereco_bairro: str | None = None,
    endereco_cidade: str | None = None,
    endereco_uf: str | None = None,
    endereco_cep: str | None = None,
    commit: bool = True,
) -> Conta:
    conta = _conta_ou_erro(conta_id)
    cnpj_norm = normalizar_cnpj_opcional(cnpj)
    if cnpj_norm and conta_multiuser_ativa_elegivel_unicidade(conta):
        _assert_cnpj_unico_multiuser_ativa(cnpj_norm, conta_id=conta.id)

    conta.razao_social = _strip_opt(razao_social, 255)
    conta.nome_fantasia = _strip_opt(nome_fantasia, 255)
    conta.cnpj = cnpj_norm
    conta.email_empresarial = _normalizar_email_empresarial(email_empresarial)
    conta.endereco_logradouro = _strip_opt(endereco_logradouro, 255)
    conta.endereco_numero = _strip_opt(endereco_numero, 20)
    conta.endereco_complemento = _strip_opt(endereco_complemento, 120)
    conta.endereco_bairro = _strip_opt(endereco_bairro, 120)
    conta.endereco_cidade = _strip_opt(endereco_cidade, 120)
    conta.endereco_uf = _normalizar_uf(endereco_uf)
    conta.endereco_cep = _normalizar_cep(endereco_cep)
    conta_id_n = conta.id
    try:
        with db.session.begin_nested():
            db.session.add(conta)
            db.session.flush()
    except IntegrityError as exc:
        logger.warning(
            "IntegrityError ao persistir CNPJ da Conta id=%s.",
            conta_id_n,
        )
        converter_ou_relancar_integrity_error(exc)
    if commit:
        db.session.commit()
    return conta


def persistir_quantidade_assentos_contratados(
    conta_id: int,
    quantidade: int,
    *,
    commit: bool = True,
) -> Conta:
    """
    Persiste quantity comercial local na raiz Conta.
    Não altera Stripe, Subscription, Franquia.limite_total nem ciclo.

    Última barreira de integridade: recusa nova_quantity < comprometido
    (ativos + reservas pendentes válidas). Não há redução comercial nesta fase.
    """
    qtd = quantidade_assentos_valida(quantidade)
    from app.services.conta_multiuser_capacidade_service import (
        bloquear_conta_para_capacidade,
        liberar_reservas_expiradas_conta,
        validar_quantity_nao_inferior_ao_comprometido,
    )

    conta = bloquear_conta_para_capacidade(int(conta_id))
    liberar_reservas_expiradas_conta(conta.id)
    validar_quantity_nao_inferior_ao_comprometido(conta, qtd)
    conta.quantidade_assentos_contratados = qtd
    db.session.add(conta)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    logger.info(
        "Quantity local Multiuser persistida conta_id=%s quantidade=%s",
        conta.id,
        qtd,
    )
    return conta


def marcar_conta_multiuser_ativa(
    conta_id: int,
    *,
    exigir_cnpj: bool = True,
    commit: bool = True,
) -> Conta:
    """Ativação estrutural local. Não cria jornada pública nem chamada Stripe."""
    conta = _conta_ou_erro(conta_id)
    if exigir_cnpj:
        try:
            exigir_dados_empresariais_para_ativacao_multiuser(conta)
        except CnpjInvalidoError as exc:
            raise CnpjInvalidoError(
                "CNPJ é obrigatório e deve ser válido para Conta Multiusuário."
            ) from exc
    conta.multiuser_ativa = True
    conta_id_n = conta.id
    try:
        with db.session.begin_nested():
            db.session.add(conta)
            db.session.flush()
    except IntegrityError as exc:
        logger.warning(
            "IntegrityError ao ativar Multiuser da Conta id=%s.",
            conta_id_n,
        )
        converter_ou_relancar_integrity_error(exc)
    if commit:
        db.session.commit()
    logger.info("Estrutura Multiuser marcada na Conta id=%s", conta.id)
    return conta


def _assert_relacao_user_conta_franquia(user: User, conta: Conta, franquia: Franquia) -> None:
    if franquia.conta_id != conta.id:
        raise ValueError("Franquia não pertence à Conta informada.")
    if user.conta_id != conta.id:
        raise ValueError("User.conta_id não corresponde à Conta do vínculo.")
    if user.franquia_id != franquia.id:
        raise ValueError("User.franquia_id não corresponde à Franquia do vínculo.")


def criar_vinculo_organizacional(
    *,
    conta_id: int,
    user_id: int,
    franquia_id: int,
    papel: str,
    origem: str = ORIGEM_DOMINIO,
    criado_por_user_id: int | None = None,
    titular: bool | None = None,
    commit: bool = True,
) -> ContaVinculoOrganizacional:
    papel_n = normalizar_papel(papel)
    conta = _conta_ou_erro(conta_id)
    user = db.session.get(User, int(user_id))
    if user is None:
        raise ValueError("User não encontrado.")
    franquia = db.session.get(Franquia, int(franquia_id))
    if franquia is None:
        raise ValueError("Franquia não encontrada.")
    _assert_relacao_user_conta_franquia(user, conta, franquia)

    titular_n = titular_obrigatorio_de_papel(papel_n)
    if titular is not None and bool(titular) != titular_n:
        raise ValueError("Titularidade é derivada do papel e não pode contradizê-lo.")

    if papel_n == PAPEL_CONTRATANTE:
        contratante_ativo = (
            ContaVinculoOrganizacional.query.filter_by(
                conta_id=conta.id,
                papel=PAPEL_CONTRATANTE,
                estado=ESTADO_ATIVO,
            )
            .first()
        )
        if contratante_ativo is not None:
            raise VinculoOrganizacionalConflitoError(
                "Já existe Contratante ativo nesta Conta."
            )

    agora = utcnow_naive()
    user_id_n = int(user.id)
    franquia_id_n = int(franquia.id)
    row = ContaVinculoOrganizacional(
        conta_id=conta.id,
        user_id=user_id_n,
        franquia_id=franquia_id_n,
        papel=papel_n,
        estado=ESTADO_ATIVO,
        titular=titular_n,
        origem=(origem or ORIGEM_DOMINIO).strip()[:40] or ORIGEM_DOMINIO,
        criado_por_user_id=criado_por_user_id,
        iniciado_em=agora,
        encerrado_em=None,
        created_at=agora,
        updated_at=agora,
    )
    try:
        with db.session.begin_nested():
            db.session.add(row)
            db.session.flush()
    except IntegrityError as exc:
        logger.warning(
            "IntegrityError ao criar vínculo organizacional user_id=%s franquia_id=%s",
            user_id_n,
            franquia_id_n,
        )
        converter_ou_relancar_integrity_error(exc)
    if commit:
        db.session.commit()
    return row


def encerrar_vinculo_organizacional(
    vinculo_id: int,
    *,
    commit: bool = True,
) -> ContaVinculoOrganizacional:
    row = db.session.get(ContaVinculoOrganizacional, int(vinculo_id))
    if row is None:
        raise ValueError("Vínculo organizacional não encontrado.")
    if row.estado == ESTADO_ENCERRADO:
        return row
    row.estado = ESTADO_ENCERRADO
    row.encerrado_em = utcnow_naive()
    row.updated_at = utcnow_naive()
    db.session.add(row)
    if commit:
        db.session.commit()
    else:
        db.session.flush()
    return row


def listar_historico_vinculos_conta(conta_id: int) -> list[ContaVinculoOrganizacional]:
    return (
        ContaVinculoOrganizacional.query.filter_by(conta_id=int(conta_id))
        .order_by(ContaVinculoOrganizacional.id.asc())
        .all()
    )


def snapshot_dados_empresariais(conta: Conta) -> dict[str, Any]:
    return {
        "razao_social": conta.razao_social,
        "nome_fantasia": conta.nome_fantasia,
        "cnpj": conta.cnpj,
        "email_empresarial": conta.email_empresarial,
        "endereco_logradouro": conta.endereco_logradouro,
        "endereco_numero": conta.endereco_numero,
        "endereco_complemento": conta.endereco_complemento,
        "endereco_bairro": conta.endereco_bairro,
        "endereco_cidade": conta.endereco_cidade,
        "endereco_uf": conta.endereco_uf,
        "endereco_cep": conta.endereco_cep,
        "quantidade_assentos_contratados": conta.quantidade_assentos_contratados,
        "multiuser_ativa": bool(conta.multiuser_ativa),
    }
