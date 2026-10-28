"""Leitura de definições do AgenteBI (SCRUM-269, etapa 3.1).

Reconhece `definir.v1` pela validação já usada no cálculo e `definir.v2`
pelo validador evolutivo. A cadeia é percorrida da raiz até a definição
pedida. A identidade do dataset, o perfil técnico e o vínculo de preparação
saem do registro persistido no servidor. A definição anterior precisa ser a
origem do cálculo da revisão base. Não executa fórmula, não agrega, não
publica dashboard e não chama provedor.
"""
from __future__ import annotations

import copy

from app.models import AgenteBiDashboard, AgenteBiDashboardDataset
from app.services.agente_bi_analisar_service import AnalisarErro, montar_contexto
from app.services.agente_bi_contrato_v2_service import (
    CONTRATO_DEFINIR_V2,
    validar_definicao_evolutiva,
)
from app.services.agente_bi_definir_service import (
    CONTRATO_DEFINIR,
    CONTRATO_SELECAO,
    DSL_CONTRATO,
    EXECUCAO,
    DefinirErro,
    montar_contexto_definir,
    validar_saida,
)
from app.services.agente_bi_perfil_semantico_service import PerfilErro, perfilar_dataset
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    obter_analise,
    obter_dataset,
)

CONTRATO_CONTEXTO_DEFINICAO_V2 = "contexto.definicao.v2"
_CHAVES_IDENTIDADE = frozenset(
    {
        "usuario_id",
        "conta_id",
        "franquia_id",
        "dashboard_id",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
    }
)
_PROIBIDAS = frozenset(
    {"sql", "python", "codigo", "expressao", "formula", "script", "query", "exec", "eval", "select", "import"}
)
_PREFIXOS_EXECUTAVEIS = ("select ", "import ", "def ", "exec(", "eval(")
_PROJECOES_CONTEXTO = ("colunas", "regioes", "relacoes", "qualidade")
_CONTRATO_CALCULO = "calculo.v1"


class LeituraErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def ler_definicao(escopo, analise_id: int) -> dict:
    """Lê a definição persistida. Não calcula e não publica."""
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or analise_id < 1:
        raise LeituraErro("payload_invalido")
    registros = _coletar_ancestrais(escopo, analise_id)
    return _validar_da_raiz(escopo, registros, {})


def _coletar_ancestrais(escopo, analise_id: int) -> list:
    """Segue o vínculo persistido até a raiz v1. Não valida o conteúdo encontrado."""
    vistos = set()
    coletados = []
    atual = analise_id
    primeiro = True
    while True:
        if atual in vistos:
            raise LeituraErro("proveniencia_inconsistente")
        if not primeiro and (isinstance(atual, bool) or not isinstance(atual, int) or atual < 1):
            raise LeituraErro("proveniencia_inconsistente")
        vistos.add(atual)
        try:
            registro = obter_analise(escopo, atual)
        except AgenteBiErro as exc:
            if primeiro:
                raise
            raise LeituraErro("proveniencia_inconsistente") from exc
        coletados.append(registro)
        contrato = registro.get("contrato_versao")
        if contrato == CONTRATO_DEFINIR:
            break
        if contrato != CONTRATO_DEFINIR_V2:
            raise LeituraErro("contrato_saida_invalido")
        atual = _referencia_anterior(registro)
        primeiro = False
    coletados.reverse()
    return coletados


def _referencia_anterior(registro: dict) -> int:
    contexto = registro.get("contexto")
    vinculo = contexto.get("vinculo") if isinstance(contexto, dict) else None
    if not isinstance(vinculo, dict):
        raise LeituraErro("proveniencia_inconsistente")
    anterior = vinculo.get("definicao_anterior_id")
    if isinstance(anterior, bool) or not isinstance(anterior, int) or anterior < 1:
        raise LeituraErro("proveniencia_inconsistente")
    return int(anterior)


def _validar_da_raiz(escopo, registros: list, perfis: dict) -> dict:
    if not registros or registros[0].get("contrato_versao") != CONTRATO_DEFINIR:
        raise LeituraErro("proveniencia_inconsistente")
    pedido_id = int(registros[-1]["id"])
    anterior = None
    for registro in registros:
        try:
            if registro.get("contrato_versao") == CONTRATO_DEFINIR:
                atual = _ler_v1(escopo, registro, perfis)
            else:
                atual = _ler_v2(escopo, registro, perfis, anterior)
        except LeituraErro as exc:
            if int(registro["id"]) != pedido_id:
                raise LeituraErro("proveniencia_inconsistente") from exc
            raise
        anterior = atual
    return anterior


def reconhecer_definicao_v2(escopo, analise: dict) -> None:
    """Valida `definir.v2` e interrompe antes de fórmula, agregação ou publicação."""
    if not isinstance(analise, dict) or analise.get("contrato_versao") != CONTRATO_DEFINIR_V2:
        return
    identificador = analise.get("id")
    if isinstance(identificador, bool) or not isinstance(identificador, int) or identificador < 1:
        raise LeituraErro("analise_indisponivel")
    ler_definicao(escopo, identificador)
    raise LeituraErro("analise_indisponivel")


def _ler_v1(escopo, origem: dict, perfis: dict) -> dict:
    if origem.get("etapa") != "definicao" or origem.get("estado") != "concluida":
        raise LeituraErro("analise_indisponivel")
    documento = origem.get("resultado")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR:
        raise LeituraErro("analise_indisponivel")
    _recusar_executavel(documento)
    if documento.get("dsl") != DSL_CONTRATO or documento.get("execucao") != EXECUCAO:
        raise LeituraErro("analise_indisponivel")
    dataset = _dataset_do_servidor(escopo, origem)
    if (
        documento.get("dataset_id") != dataset["id"]
        or documento.get("dataset_versao") != dataset["versao"]
        or documento.get("dataset_impressao") != dataset["impressao"]
    ):
        raise LeituraErro("identidade_invalida", 409)
    perfil = _perfil_da_leitura(escopo, int(dataset["id"]), perfis)
    embarcado = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    if (
        embarcado.get("id") != dataset["id"]
        or embarcado.get("versao") != dataset["versao"]
        or embarcado.get("impressao") != dataset["impressao"]
        or perfil.get("vinculo_preparacao") != documento.get("vinculo_preparacao")
    ):
        raise LeituraErro("identidade_invalida", 409)
    definicoes = documento.get("definicoes")
    if not isinstance(definicoes, list) or not definicoes:
        raise LeituraErro("analise_indisponivel")
    _validar_estrutura_v1(escopo, documento, dataset, perfil)
    return {
        "contrato": CONTRATO_DEFINIR,
        "analise_id": int(origem["id"]),
        "definicoes": copy.deepcopy(definicoes),
        "dataset": _identidade_dataset(dataset),
        "vinculo_preparacao": perfil.get("vinculo_preparacao"),
    }


def _ler_v2(escopo, origem: dict, perfis: dict, anterior: dict) -> dict:
    if origem.get("etapa") != "definicao" or origem.get("estado") != "concluida":
        raise LeituraErro("analise_indisponivel")
    documento = origem.get("resultado")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR_V2:
        raise LeituraErro("contrato_saida_invalido")
    contexto = origem.get("contexto")
    if not isinstance(contexto, dict) or contexto.get("contrato") != CONTRATO_CONTEXTO_DEFINICAO_V2:
        raise LeituraErro("proveniencia_inconsistente")
    vinculo = contexto.get("vinculo")
    if not isinstance(vinculo, dict):
        raise LeituraErro("proveniencia_inconsistente")
    if _CHAVES_IDENTIDADE & set(vinculo):
        raise LeituraErro("autorizacao_indevida", 403)
    try:
        validado = validar_definicao_evolutiva(documento, vinculo)
    except DefinirErro as exc:
        raise LeituraErro(exc.codigo, exc.status) from exc
    dataset = _dataset_do_servidor(escopo, origem)
    _recusar_dataset_substituido(vinculo.get("contexto"), dataset)
    perfil = _perfil_da_leitura(escopo, int(dataset["id"]), perfis)
    preparacao = contexto.get("vinculo_preparacao")
    embarcado = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    if (
        not isinstance(preparacao, str)
        or preparacao != perfil.get("vinculo_preparacao")
        or embarcado.get("id") != dataset["id"]
        or embarcado.get("versao") != dataset["versao"]
        or embarcado.get("impressao") != dataset["impressao"]
    ):
        raise LeituraErro("identidade_inconsistente", 409)
    _exigir_contexto_tecnico(vinculo.get("contexto"), perfil)
    _exigir_ancestral_validado(validado, vinculo, dataset, anterior)
    _exigir_revisao_da_linhagem(escopo, validado["revisao_base"], dataset, validado["definicao_anterior_id"])
    return {
        "contrato": CONTRATO_DEFINIR_V2,
        "analise_id": int(origem["id"]),
        "definicoes": copy.deepcopy(validado["definicoes"]),
        "dataset": _identidade_dataset(dataset),
        "vinculo_preparacao": preparacao,
        "definicao_anterior_id": validado["definicao_anterior_id"],
        "revisao_base": validado["revisao_base"],
        "origem_contrato": validado["origem_contrato"],
    }


def _dataset_do_servidor(escopo, origem: dict) -> dict:
    try:
        dataset = obter_dataset(escopo, int(origem["dataset_id"]))
    except (AgenteBiErro, TypeError, ValueError) as exc:
        raise LeituraErro("identidade_inconsistente", 409) from exc
    if dataset["versao"] != origem["versao"] or dataset["impressao"] != origem["impressao"]:
        raise LeituraErro("identidade_invalida", 409)
    return dataset


def _perfil_da_leitura(escopo, dataset_id: int, perfis: dict) -> dict:
    """Um perfil por dataset nesta leitura. O mapa vive só na chamada atual."""
    if dataset_id not in perfis:
        perfis[dataset_id] = _perfil(escopo, dataset_id)
    return perfis[dataset_id]


def _perfil(escopo, dataset_id: int) -> dict:
    try:
        perfil = perfilar_dataset(escopo, int(dataset_id))
    except (PerfilErro, AgenteBiErro) as exc:
        raise LeituraErro("identidade_inconsistente", 409) from exc
    if not isinstance(perfil, dict):
        raise LeituraErro("identidade_inconsistente", 409)
    return perfil


def _identidade_dataset(dataset: dict) -> dict:
    return {
        "id": int(dataset["id"]),
        "versao": int(dataset["versao"]),
        "impressao": dataset["impressao"],
    }


def _recusar_dataset_substituido(contexto, dataset: dict) -> None:
    if not isinstance(contexto, dict) or "dataset" not in contexto:
        return
    embarcado = contexto.get("dataset")
    if not isinstance(embarcado, dict):
        raise LeituraErro("identidade_inconsistente", 409)
    if (
        embarcado.get("id") != dataset["id"]
        or embarcado.get("versao") != dataset["versao"]
        or embarcado.get("impressao") != dataset["impressao"]
    ):
        raise LeituraErro("identidade_inconsistente", 409)


def _exigir_contexto_tecnico(contexto, perfil: dict) -> None:
    """As projeções do vínculo têm de ser as do perfil real, não as do documento."""
    if not isinstance(contexto, dict):
        raise LeituraErro("identidade_inconsistente", 409)
    try:
        real = montar_contexto(perfil, {"modalidades": ["A"]})
    except AnalisarErro as exc:
        raise LeituraErro("identidade_inconsistente", 409) from exc
    for chave in _PROJECOES_CONTEXTO:
        if contexto.get(chave) != real.get(chave):
            raise LeituraErro("identidade_inconsistente", 409)


def _validar_estrutura_v1(escopo, documento: dict, dataset: dict, perfil: dict) -> None:
    """Reconstrói o pacote de `definir_selecao` e submete o documento a `validar_saida`."""
    selecao_id = documento.get("origem_selecao_id")
    if isinstance(selecao_id, bool) or not isinstance(selecao_id, int) or selecao_id < 1:
        raise LeituraErro("proveniencia_inconsistente")
    try:
        selecao = obter_analise(escopo, selecao_id)
    except AgenteBiErro as exc:
        raise LeituraErro("proveniencia_inconsistente") from exc
    if selecao.get("etapa") != "selecao" or selecao.get("estado") != "concluida":
        raise LeituraErro("proveniencia_inconsistente")
    if selecao.get("contrato_versao") != CONTRATO_SELECAO:
        raise LeituraErro("proveniencia_inconsistente")
    resultado = selecao.get("resultado")
    if not isinstance(resultado, dict) or resultado.get("contrato") != CONTRATO_SELECAO:
        raise LeituraErro("proveniencia_inconsistente")
    if (
        selecao.get("dataset_id") != dataset["id"]
        or selecao.get("versao") != dataset["versao"]
        or selecao.get("impressao") != dataset["impressao"]
        or resultado.get("dataset_id") != dataset["id"]
        or resultado.get("dataset_versao") != dataset["versao"]
        or resultado.get("dataset_impressao") != dataset["impressao"]
        or resultado.get("vinculo_preparacao") != perfil.get("vinculo_preparacao")
    ):
        raise LeituraErro("proveniencia_inconsistente")
    pacote = dict(resultado)
    pacote["id"] = int(selecao["id"])
    try:
        contexto = montar_contexto_definir(perfil, pacote)
        validar_saida(contexto, documento)
    except DefinirErro as exc:
        raise LeituraErro(exc.codigo, exc.status) from exc


def _exigir_ancestral_validado(validado: dict, vinculo: dict, dataset: dict, anterior: dict) -> None:
    if not isinstance(anterior, dict) or int(anterior.get("analise_id") or 0) != int(validado["definicao_anterior_id"]):
        raise LeituraErro("proveniencia_inconsistente")
    if anterior.get("contrato") != validado["origem_contrato"]:
        raise LeituraErro("proveniencia_inconsistente")
    identidade = anterior.get("dataset") if isinstance(anterior.get("dataset"), dict) else {}
    if (
        identidade.get("id") != dataset["id"]
        or identidade.get("versao") != dataset["versao"]
        or identidade.get("impressao") != dataset["impressao"]
    ):
        raise LeituraErro("proveniencia_inconsistente")
    if _projecao_graficos(vinculo.get("definicoes")) != _projecao_graficos(anterior.get("definicoes")):
        raise LeituraErro("proveniencia_inconsistente")


def _projecao_graficos(definicoes) -> tuple:
    if not isinstance(definicoes, list):
        raise LeituraErro("proveniencia_inconsistente")
    saida = []
    for item in definicoes:
        if not isinstance(item, dict):
            raise LeituraErro("proveniencia_inconsistente")
        saida.append((item.get("id"), item.get("sugestao_id"), item.get("origem")))
    return tuple(saida)


def _exigir_revisao_da_linhagem(escopo, revisao: int, dataset: dict, definicao_anterior_id: int) -> None:
    """A revisão vale quando o cálculo persistido daquele dashboard nasce desta definição."""
    paineis = (
        AgenteBiDashboard.query.filter_by(
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
            revisao=int(revisao),
        ).all()
    )
    for painel in paineis:
        if (
            int(painel.conta_id) != int(escopo.conta_id)
            or int(painel.franquia_id) != int(escopo.franquia_id)
            or int(painel.usuario_id) != int(escopo.usuario_id)
        ):
            continue
        if not _dashboard_no_dataset(painel, escopo, dataset):
            continue
        if isinstance(painel.analise_id, bool) or not isinstance(painel.analise_id, int) or painel.analise_id < 1:
            continue
        try:
            calculo = obter_analise(escopo, int(painel.analise_id))
        except (AgenteBiErro, TypeError, ValueError):
            continue
        if calculo.get("etapa") != "execucao" or calculo.get("estado") != "concluida":
            continue
        if calculo.get("contrato_versao") != _CONTRATO_CALCULO:
            continue
        if (
            int(calculo.get("dataset_id") or 0) != int(dataset["id"])
            or int(calculo.get("versao") or 0) != int(dataset["versao"])
            or calculo.get("impressao") != dataset["impressao"]
        ):
            continue
        memoria = calculo.get("resultado")
        origem = memoria.get("origem_definicao_id") if isinstance(memoria, dict) else None
        if isinstance(origem, bool) or not isinstance(origem, int):
            continue
        if int(origem) == int(definicao_anterior_id):
            return
    raise LeituraErro("proveniencia_inconsistente")


def _dashboard_no_dataset(painel, escopo, dataset: dict) -> bool:
    vinculos = AgenteBiDashboardDataset.query.filter_by(dashboard_id=int(painel.id)).all()
    for item in vinculos:
        if (
            int(item.conta_id) == int(escopo.conta_id)
            and int(item.franquia_id) == int(escopo.franquia_id)
            and int(item.usuario_id) == int(escopo.usuario_id)
            and int(item.dataset_id) == int(dataset["id"])
            and int(item.dataset_versao) == int(dataset["versao"])
            and item.dataset_impressao == dataset["impressao"]
        ):
            return True
    return False


def _recusar_executavel(valor, profundidade: int = 0) -> None:
    if profundidade > 12:
        raise LeituraErro("expressao_nao_permitida")
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if isinstance(chave, str) and chave.lower() in _PROIBIDAS:
                raise LeituraErro("expressao_nao_permitida")
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, list):
        for item in valor:
            _recusar_executavel(item, profundidade + 1)
        return
    if isinstance(valor, str) and valor.strip().lower().startswith(_PREFIXOS_EXECUTAVEIS):
        raise LeituraErro("expressao_nao_permitida")
