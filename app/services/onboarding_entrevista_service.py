"""
Persistência das respostas declaradas na entrevista condicional de onboarding.

A definição (quais perguntas existem, para qual cargo e em qual ordem) está em
onboarding_entrevista_definicao. Este serviço grava só o estado atual, com
origem fechada e data da declaração.

Preparado para o cadastro web agora e, depois, para /perfil
(origem perfil_usuario) e WhatsApp (origem onboarding_whatsapp) sem tabela nova.
Não grava inferência de IA, não altera billing e não emite evento de Growth.
"""
from __future__ import annotations

from collections.abc import Mapping

from app.extensions import db
from app.models import OnboardingRespostaDeclarada, User, utcnow_naive
from app.services.onboarding_entrevista_definicao import (
    CAMPO_FORMULARIO_PREFIX,
    TAXONOMIA_VERSAO,
    ResultadoValidacao,
    entrevista_ativa,
    normalizar_respostas,
    validar_combinacao,
    validar_declaracao,
)

__all__ = (
    "aplicar_declaracao_entrevista",
    "carregar_respostas_atuais",
    "declarar_cargo_e_entrevista",
    "entrevista_complementar_satisfeita",
    "extrair_respostas_formulario",
    "remover_respostas_declaradas",
)


def extrair_respostas_formulario(form) -> dict[str, str]:
    """Lê somente campos entrevista_<question_key> não vazios."""
    if form is None:
        return {}
    respostas: dict[str, str] = {}
    keys = form.keys() if hasattr(form, "keys") else ()
    for key in keys:
        if not isinstance(key, str) or not key.startswith(CAMPO_FORMULARIO_PREFIX):
            continue
        question_key = key[len(CAMPO_FORMULARIO_PREFIX) :].strip()
        raw = form.get(key)
        answer_key = raw.strip() if isinstance(raw, str) else ""
        if question_key and answer_key:
            respostas[question_key] = answer_key
    return respostas


def carregar_respostas_atuais(user_id: int) -> dict[str, str]:
    rows = OnboardingRespostaDeclarada.query.filter_by(user_id=int(user_id)).all()
    return {row.question_key: row.answer_key for row in rows}


def entrevista_complementar_satisfeita(user) -> bool:
    """
    Completude da entrevista para o job_role já gravado.

    Sem entrevista ativa, a ausência de respostas é válida. Isso preserva
    usuários legados e cargos corporativos. Não preenche respostas sozinho
    e não reclassifica job_role.
    """
    role = (getattr(user, "job_role", None) or "").strip()
    if entrevista_ativa(role) is None:
        return True
    user_id = getattr(user, "id", None)
    if user_id is None:
        return False
    return validar_combinacao(role, carregar_respostas_atuais(int(user_id))).ok


def remover_respostas_declaradas(user: User) -> int:
    """
    Remove as declarações deste User na sessão atual. Não commita.

    Usado no encerramento/desidentificação para não deixar resposta de perfil
    identificável depois que o User perde a identidade operacional.
    """
    if getattr(user, "id", None) is None:
        return 0
    rows = OnboardingRespostaDeclarada.query.filter_by(user_id=int(user.id)).all()
    for row in rows:
        db.session.delete(row)
    return len(rows)


def aplicar_declaracao_entrevista(
    user: User,
    *,
    job_role: str | None,
    respostas: Mapping[str, str] | None,
    origem: str,
) -> ResultadoValidacao:
    """
    Substitui as respostas compatíveis com o cargo que o User já possui.

    job_role é mantido por compatibilidade e deve ser igual a user.job_role.
    Para alterar cargo e respostas juntos, use declarar_cargo_e_entrevista.
    Não altera User.job_role, plano, créditos, franquia nem faz commit.
    Exige user.id. Resposta não aplicável rejeita a declaração inteira e
    mantém o estado anterior. Declaração válida remove perguntas que deixaram
    de fazer parte do ramo.
    """
    role = user.job_role
    if job_role != role:
        return ResultadoValidacao(
            False,
            "O cargo informado não corresponde ao cargo atual do usuário.",
            "cargo_divergente",
        )
    resultado = validar_declaracao(role, respostas, origem)
    if not resultado.ok:
        return resultado
    if getattr(user, "id", None) is None:
        return ResultadoValidacao(
            False,
            "Não foi possível registrar a entrevista complementar.",
            "usuario_invalido",
        )
    _substituir_respostas(user, normalizar_respostas(respostas), origem)
    return resultado


def declarar_cargo_e_entrevista(
    user: User,
    *,
    job_role: str | None,
    respostas: Mapping[str, str] | None,
    origem: str,
) -> ResultadoValidacao:
    """
    Ponto de edição futura de /perfil: grava job_role e as respostas juntos.

    Não commita, não altera usage_purpose, billing, feed, permissões nem plugin.
    Combinação inválida não muda cargo nem respostas já gravadas.
    """
    role = (job_role or "").strip()
    resultado = validar_declaracao(role, respostas, origem)
    if not resultado.ok:
        return resultado
    if getattr(user, "id", None) is None:
        return ResultadoValidacao(
            False,
            "Não foi possível registrar a entrevista complementar.",
            "usuario_invalido",
        )
    user.job_role = role or None
    _substituir_respostas(user, normalizar_respostas(respostas), origem)
    return resultado


def _substituir_respostas(user: User, respostas: dict[str, str], origem: str) -> None:
    now = utcnow_naive()
    existentes = {
        row.question_key: row
        for row in OnboardingRespostaDeclarada.query.filter_by(user_id=int(user.id)).all()
    }
    for question_key, answer_key in respostas.items():
        row = existentes.get(question_key)
        if row is None:
            db.session.add(
                OnboardingRespostaDeclarada(
                    user_id=int(user.id),
                    question_key=question_key,
                    answer_key=answer_key,
                    origem=origem,
                    taxonomia_versao=TAXONOMIA_VERSAO,
                    declarada_em=now,
                    atualizada_em=now,
                )
            )
            continue
        if (
            row.answer_key != answer_key
            or row.origem != origem
            or row.taxonomia_versao != TAXONOMIA_VERSAO
        ):
            row.answer_key = answer_key
            row.origem = origem
            row.taxonomia_versao = TAXONOMIA_VERSAO
            row.atualizada_em = now
    for question_key, row in existentes.items():
        if question_key not in respostas:
            db.session.delete(row)
