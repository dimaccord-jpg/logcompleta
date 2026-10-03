"""
Definição versionada das entrevistas condicionais de onboarding.

A definição vive em código. As respostas declaradas ficam em
OnboardingRespostaDeclarada. Novas perguntas entram aqui; não exigem coluna
nova nem migration, desde que caibam nas chaves já previstas na tabela.

User.job_role permanece a classificação principal. Esta taxonomia visual não
reclassifica valores já gravados (inclusive grafias antigas fora da lista).

Ramos previstos e inativos — não implementar neste lote:
- gerente → segmento_atuacao
- proprietario → tipo_empresa
- analista → area_atuacao

WhatsApp, edição em /perfil e sugestão por IA reutilizam esta definição depois.
Inferência automática não é origem de declaração.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

TAXONOMIA_VERSAO = "onboarding_entrevista_v1"

ORIGEM_CADASTRO_WEB = "cadastro_web"
ORIGEM_ONBOARDING_WHATSAPP = "onboarding_whatsapp"
ORIGEM_PERFIL_USUARIO = "perfil_usuario"
ORIGENS = (
    ORIGEM_CADASTRO_WEB,
    ORIGEM_ONBOARDING_WHATSAPP,
    ORIGEM_PERFIL_USUARIO,
)

CAMPO_FORMULARIO_PREFIX = "entrevista_"

JOB_ROLE_ANALISTA = "analista"
JOB_ROLE_COORDENADOR = "coordenador"
JOB_ROLE_GERENTE = "gerente"
JOB_ROLE_DIRETOR = "diretor"
JOB_ROLE_PROPRIETARIO = "proprietario"
JOB_ROLE_MOTORISTA_ENTREGADOR = "motorista_entregador"
JOB_ROLE_OUTRO = "outro"

JOB_ROLES: tuple[tuple[str, str], ...] = (
    (JOB_ROLE_ANALISTA, "Analista"),
    (JOB_ROLE_COORDENADOR, "Coordenador"),
    (JOB_ROLE_GERENTE, "Gerente"),
    (JOB_ROLE_DIRETOR, "Diretor"),
    (JOB_ROLE_PROPRIETARIO, "Proprietário / Sócio"),
    (JOB_ROLE_MOTORISTA_ENTREGADOR, "Motorista / Entregador"),
    (JOB_ROLE_OUTRO, "Outro"),
)

# Chaves futuras. entrevista_ativa() ignora esta lista de propósito.
RAMOS_FUTUROS_NAO_ATIVOS: tuple[tuple[str, str], ...] = (
    (JOB_ROLE_GERENTE, "segmento_atuacao"),
    (JOB_ROLE_PROPRIETARIO, "tipo_empresa"),
    (JOB_ROLE_ANALISTA, "area_atuacao"),
)


@dataclass(frozen=True)
class OpcaoEntrevista:
    key: str
    label: str


@dataclass(frozen=True)
class PerguntaEntrevista:
    key: str
    texto: str
    opcoes: tuple[OpcaoEntrevista, ...]
    depende_de: tuple[str, str] | None = None

    def aceita(self, answer_key: str) -> bool:
        return answer_key in {opcao.key for opcao in self.opcoes}


@dataclass(frozen=True)
class EntrevistaCargo:
    job_role: str
    perguntas: tuple[PerguntaEntrevista, ...]


@dataclass(frozen=True)
class ResultadoValidacao:
    ok: bool
    mensagem: str | None = None
    codigo: str | None = None


def _opcoes(*pares: tuple[str, str]) -> tuple[OpcaoEntrevista, ...]:
    return tuple(OpcaoEntrevista(key, label) for key, label in pares)


_TIPO_ATUACAO = PerguntaEntrevista(
    key="tipo_atuacao",
    texto="Como você atua principalmente?",
    opcoes=_opcoes(
        ("motorista_app", "Motorista de aplicativo"),
        ("entregador_app", "Entregador de aplicativo"),
        ("tac", "TAC / Caminhoneiro autônomo"),
        ("motorista_profissional", "Motorista profissional"),
        ("outro", "Outro"),
    ),
)

_VEICULO_PRINCIPAL = PerguntaEntrevista(
    key="veiculo_principal",
    texto="Qual veículo você utiliza principalmente?",
    opcoes=_opcoes(
        ("moto", "Moto"),
        ("carro", "Carro"),
        ("bicicleta", "Bicicleta"),
        ("outro", "Outro"),
    ),
    depende_de=("tipo_atuacao", "entregador_app"),
)

ENTREVISTAS: dict[str, EntrevistaCargo] = {
    JOB_ROLE_MOTORISTA_ENTREGADOR: EntrevistaCargo(
        job_role=JOB_ROLE_MOTORISTA_ENTREGADOR,
        perguntas=(_TIPO_ATUACAO, _VEICULO_PRINCIPAL),
    ),
}


def _validar_definicao() -> None:
    cargos = {key for key, _label in JOB_ROLES}
    chaves: list[str] = []
    for role, entrevista in ENTREVISTAS.items():
        if role != entrevista.job_role or role not in cargos:
            raise RuntimeError(f"entrevista ativa incoerente para cargo {role}")
        if not entrevista.perguntas:
            raise RuntimeError(f"entrevista ativa sem perguntas: {role}")
        vistas: set[str] = set()
        for pergunta in entrevista.perguntas:
            if not pergunta.key or pergunta.key in vistas or not pergunta.opcoes:
                raise RuntimeError(f"pergunta inválida: {pergunta.key}")
            vistas.add(pergunta.key)
            chaves.append(pergunta.key)
            if pergunta.depende_de is None:
                continue
            dep_key, dep_answer = pergunta.depende_de
            if dep_key not in vistas:
                raise RuntimeError(f"dependência fora de ordem: {pergunta.key}")
            parent = next(item for item in entrevista.perguntas if item.key == dep_key)
            if not parent.aceita(dep_answer):
                raise RuntimeError(f"dependência sem opção: {pergunta.key}")
    if len(chaves) != len(set(chaves)):
        raise RuntimeError("chave de pergunta duplicada entre entrevistas")
    for role, question_key in RAMOS_FUTUROS_NAO_ATIVOS:
        if role in ENTREVISTAS or question_key in chaves:
            raise RuntimeError(f"ramo futuro foi ativado sem lote próprio: {question_key}")


_validar_definicao()

PERGUNTAS: dict[str, PerguntaEntrevista] = {
    pergunta.key: pergunta
    for entrevista in ENTREVISTAS.values()
    for pergunta in entrevista.perguntas
}


def _ok() -> ResultadoValidacao:
    return ResultadoValidacao(True)


def _falha(codigo: str, mensagem: str) -> ResultadoValidacao:
    return ResultadoValidacao(False, mensagem, codigo)


def entrevista_ativa(job_role: str | None) -> EntrevistaCargo | None:
    """Entrevista com perguntas vigentes para o job_role exato. Sem inferência."""
    role = (job_role or "").strip()
    entrevista = ENTREVISTAS.get(role)
    if entrevista is None or not entrevista.perguntas:
        return None
    return entrevista


def normalizar_respostas(respostas: Mapping[str, str] | None) -> dict[str, str]:
    """Descarta vazios. Resposta vazia não é declaração."""
    if not respostas:
        return {}
    normalizadas: dict[str, str] = {}
    for key, value in respostas.items():
        question_key = (key or "").strip() if isinstance(key, str) else ""
        answer_key = (value or "").strip() if isinstance(value, str) else ""
        if question_key and answer_key:
            normalizadas[question_key] = answer_key
    return normalizadas


def perguntas_visiveis(
    job_role: str | None,
    respostas: Mapping[str, str] | None,
) -> tuple[PerguntaEntrevista, ...]:
    entrevista = entrevista_ativa(job_role)
    if entrevista is None:
        return ()
    declaradas = normalizar_respostas(respostas)
    visiveis: list[PerguntaEntrevista] = []
    for pergunta in entrevista.perguntas:
        if pergunta.depende_de is None:
            visiveis.append(pergunta)
            continue
        dep_key, dep_answer = pergunta.depende_de
        parent_visivel = any(item.key == dep_key for item in visiveis)
        if parent_visivel and declaradas.get(dep_key) == dep_answer:
            visiveis.append(pergunta)
    return tuple(visiveis)


def proxima_pergunta(
    job_role: str | None,
    respostas: Mapping[str, str] | None,
) -> PerguntaEntrevista | None:
    """Primeira pergunta visível ainda sem resposta válida. None se não houver."""
    declaradas = normalizar_respostas(respostas)
    for pergunta in perguntas_visiveis(job_role, declaradas):
        valor = declaradas.get(pergunta.key) or ""
        if not pergunta.aceita(valor):
            return pergunta
    return None


def validar_combinacao(
    job_role: str | None,
    respostas: Mapping[str, str] | None,
) -> ResultadoValidacao:
    """
    Contrato de escrita: resposta não aplicável é rejeitada, não descartada.

    Cargo sem entrevista ativa aceita somente o conjunto vazio. Valor legado
    de job_role que não tem entrevista (inclusive grafia antiga) também aceita
    conjunto vazio e não é reclassificado.
    """
    if respostas is not None and not isinstance(respostas, Mapping):
        return _falha("resposta_invalida", "A resposta informada não é válida para esta pergunta.")
    if respostas:
        for key, value in respostas.items():
            if not isinstance(key, str) or not isinstance(value, str):
                return _falha(
                    "resposta_invalida",
                    "A resposta informada não é válida para esta pergunta.",
                )
    declaradas = normalizar_respostas(respostas)
    role = (job_role or "").strip()
    entrevista = entrevista_ativa(role)

    for question_key, answer_key in declaradas.items():
        pergunta = PERGUNTAS.get(question_key)
        if pergunta is None:
            return _falha(
                "pergunta_inexistente",
                "A pergunta complementar informada não existe.",
            )
        if entrevista is None or all(item.key != question_key for item in entrevista.perguntas):
            return _falha(
                "pergunta_inaplicavel",
                "A pergunta complementar não se aplica ao cargo informado.",
            )
        if not pergunta.aceita(answer_key):
            return _falha(
                "resposta_invalida",
                "A resposta informada não é válida para esta pergunta.",
            )

    visiveis = perguntas_visiveis(role, declaradas)
    visiveis_keys = {pergunta.key for pergunta in visiveis}
    for question_key in declaradas:
        if question_key not in visiveis_keys:
            return _falha(
                "combinacao_incoerente",
                "A resposta complementar não se aplica a esta combinação de cargo e respostas.",
            )
    for pergunta in visiveis:
        if pergunta.key not in declaradas:
            return _falha("resposta_obrigatoria", f"Informe: {pergunta.texto}")
    return _ok()


def validar_declaracao(
    job_role: str | None,
    respostas: Mapping[str, str] | None,
    origem: str | None,
) -> ResultadoValidacao:
    if origem not in ORIGENS:
        return _falha("origem_invalida", "A origem da declaração não é válida.")
    return validar_combinacao(job_role, respostas)


def entrevista_encerrada(
    job_role: str | None,
    respostas: Mapping[str, str] | None,
) -> bool:
    """
    True quando não há próxima pergunta e a combinação é coerente.

    Cargo sem entrevista ativa e sem respostas complementares já terminou:
    o próprio job_role é o cargo. Combinação inválida não termina a entrevista.
    """
    if not validar_combinacao(job_role, respostas).ok:
        return False
    return proxima_pergunta(job_role, respostas) is None


def contexto_template() -> dict[str, object]:
    """Taxonomia única consumida pelos templates de cadastro e complete-profile."""
    perguntas = []
    for role, entrevista in ENTREVISTAS.items():
        for pergunta in entrevista.perguntas:
            depende = None
            if pergunta.depende_de is not None:
                depende = {
                    "pergunta": pergunta.depende_de[0],
                    "resposta": pergunta.depende_de[1],
                }
            perguntas.append(
                {
                    "key": pergunta.key,
                    "texto": pergunta.texto,
                    "job_roles": (role,),
                    "depende_de": depende,
                    "opcoes": [
                        {"key": opcao.key, "label": opcao.label}
                        for opcao in pergunta.opcoes
                    ],
                }
            )
    return {
        "onboarding_cargos": JOB_ROLES,
        "onboarding_perguntas": perguntas,
        "onboarding_campo_prefix": CAMPO_FORMULARIO_PREFIX,
    }
