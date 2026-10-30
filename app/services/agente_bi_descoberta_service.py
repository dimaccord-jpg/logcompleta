"""Jornada visível de descoberta do AgenteBI (SCRUM-258, SCRUM-268).

Reutiliza o contrato `analisar.v1`. Sem execução real habilitada, o provedor
simulado permanece. Com a execução real, a rodada usa a operação durável já
existente e só publica o documento persistido e validado. Não recalcula
viabilidade, não abre worker de infraestrutura e não grava dashboard.
A seleção aprovada é uma rodada nova, presa ao usuário e à versão do dataset.
Só entra gráfico que a rodada de descoberta já marcou como viável.
A seleção local não herda a proveniência da descoberta de origem.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from uuid import uuid4

from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiDataset
from app.services.agente_bi_analisar_service import (
    CATALOGO_CONTRATO,
    CONTRATO_ANALISAR,
    PROVEDOR_ANTHROPIC,
    PROVEDOR_SIMULADO,
    AnalisarErro,
    _validar_pedido,
    analisar_dataset,
    montar_contexto,
    provedor_simulado,
)
from app.agente_bi_flags import agente_bi_colaboracao_habilitada
from app.services.agente_bi_execucao_service import LEASE_MAX_SEGUNDOS, ExecucaoErro
from app.services.agente_bi_integracao_service import (
    IntegracaoErro,
    autorizar_transferencia,
    execucao_real_habilitada,
    executar_operacao,
    intencao_c_ausente,
    intencao_c_removida,
)
from app.services.agente_bi_ingestao_service import ingerir_requisicao
from app.services.agente_bi_perfil_semantico_service import PerfilErro, perfilar_dataset
from app.services.agente_bi_persistencia_service import (
    LIMITE_GRAFICOS,
    TIPOS_GRAFICO,
    AgenteBiErro,
    criar_analise,
    obter_analise,
    obter_dataset,
)

logger = logging.getLogger(__name__)

CONTRATO_SELECAO = "selecao.descoberta.v1"
CONTRATO_CONTEXTO_SELECAO = "contexto.selecao.v1"
_JUNCAO = "nao_executada"
_ORIGENS = frozenset({"dados", "referencia"})
_CODIGO = re.compile(r"^[a-z0-9_]{1,40}$")
_ID_SUGESTAO = re.compile(r"^s[1-9][0-9]{0,2}$")
_ID_PERGUNTA = re.compile(r"^p[1-9][0-9]{0,2}$")
_CONFLITOS_RODADA = frozenset({"rodada_existente", "conflito_versao"})
_CHAVES_PEDIDO = frozenset({"dataset_id", "pedido"})
_CHAVES_SELECAO = frozenset({"analise_id", "sugestoes", "perguntas", "confirmacao"})
_CHAVES_CONFIRMACAO = frozenset({"id", "confirmada"})
_TOTAIS = (
    "com_valor",
    "celulas",
    "valores_ausentes",
    "linhas_excedentes",
    "datas_ambiguas",
    "formulas_sem_cache",
    "pre_preenchidas",
    "chaves_candidatas",
    "dominios_inconsistentes",
)


_REUSO_OPERACAO = frozenset({"conflito_versao", "rodada_existente", "chave_recuperacao_existente"})
_TENTATIVAS_RODADA = 2
_CODIGO_PUBLICO = re.compile(r"^[a-z0-9_]{1,40}$")
_VERIFICACAO_PUBLICA = re.compile(r"^[a-z0-9_]{1,40}$")


class DescobertaErro(Exception):
    def __init__(self, codigo: str, status: int = 422, evidencias: list | None = None):
        self.codigo = codigo
        self.status = status
        self.evidencias = list(evidencias) if evidencias else None
        super().__init__(codigo)


def preparar_requisicao(escopo) -> dict:
    """Prepara o arquivo já aceito pela ingestão e devolve só o estado estrutural."""
    return resumir_preparacao(escopo, ingerir_requisicao(escopo))


def resumir_preparacao(escopo, bruto: dict) -> dict:
    """Tira célula, fórmula e rótulo do retorno. O catálogo vem do perfil já existente."""
    if not isinstance(bruto, dict):
        raise DescobertaErro("payload_invalido")
    preparacao = bruto.get("preparacao") if isinstance(bruto.get("preparacao"), dict) else {}
    estrutura = preparacao.get("estrutura") if isinstance(preparacao.get("estrutura"), dict) else {}
    perfil_bruto = preparacao.get("perfil") if isinstance(preparacao.get("perfil"), dict) else {}
    dataset = bruto.get("dataset") if isinstance(bruto.get("dataset"), dict) else None
    identificador = _identificador(dataset.get("id")) if dataset else None
    persistido = bruto.get("persistido") is True and identificador is not None
    colunas: list[dict] = []
    regioes: list[dict] = []
    qualidade: dict = {}
    if persistido:
        try:
            perfil = perfilar_dataset(escopo, int(dataset["id"]))
        except PerfilErro as exc:
            raise DescobertaErro(exc.codigo) from exc
        colunas = _colunas_publicas(perfil)
        regioes = _regioes_publicas(perfil)
        qualidade = _qualidade_publica(perfil)
    publico = _dataset_publico(escopo, dataset) if persistido else None
    logger.info(
        "evento=agente_bi_descoberta_preparacao usuario_id=%s persistido=%s versao=%s",
        escopo.usuario_id,
        int(persistido),
        None if publico is None else publico["versao"],
    )
    resposta = {
        "ok": True,
        "estado": "preparado" if persistido else "nao_persistido",
        "persistido": persistido,
        "reutilizado": bruto.get("reutilizado") is True,
        "motivo": bruto.get("motivo") if isinstance(bruto.get("motivo"), str) else None,
        "dataset": publico,
        "resumo": _resumo(perfil_bruto),
        "arquivos": _arquivos_publicos(estrutura),
        "abas": _abas_publicas(estrutura),
        "ambiguidades": _ambiguidades_publicas(estrutura.get("ambiguidades")),
        "alertas": _codigos_texto(estrutura.get("alertas")),
        "colunas": colunas,
        "regioes": regioes,
        "qualidade": qualidade,
    }
    protecao = _protecao_publica(preparacao.get("metadados_preparacao"))
    if protecao is not None:
        resposta["protecao"] = protecao
    return resposta


def descobrir(escopo, corpo: dict, *, transporte=None, cliente_gemini=None, controle=None) -> dict:
    """Uma rodada A, B, C ou combinada.

    A flag desligada preserva o simulador. A flag ligada usa a operação
    durável e o adaptador governado, com transporte injetável nos testes.
    A colaboração, quando habilitada, também usa uma chamada externa por
    requisição e não altera o simulador.
    """
    if not isinstance(corpo, dict) or set(corpo) != _CHAVES_PEDIDO:
        raise DescobertaErro("pedido_invalido")
    dataset_id = _identificador(corpo.get("dataset_id"))
    pedido = corpo.get("pedido")
    if dataset_id is None or not isinstance(pedido, dict):
        raise DescobertaErro("pedido_invalido")
    if not execucao_real_habilitada():
        return _descobrir_simulado(escopo, dataset_id, pedido)
    if agente_bi_colaboracao_habilitada():
        from app.services.agente_bi_colaboracao_service import ColaboracaoErro, executar_descoberta

        try:
            return executar_descoberta(
                escopo,
                dataset_id,
                pedido,
                transporte=transporte,
                cliente_gemini=cliente_gemini,
                controle=controle,
            )
        except ColaboracaoErro as exc:
            raise DescobertaErro(exc.codigo, exc.status, evidencias=exc.evidencias) from exc
    return _descobrir_real(escopo, dataset_id, pedido, transporte=transporte)


def _descobrir_simulado(escopo, dataset_id: int, pedido: dict) -> dict:
    """Rodada local. Não enfileira e não consulta o adaptador."""
    linha = _dataset_no_escopo(escopo, dataset_id)
    gravado = _com_rodada(
        escopo,
        linha.chave,
        lambda rodada: analisar_dataset(
            escopo,
            dataset_id,
            chave=linha.chave,
            rodada=rodada,
            pedido=pedido,
            provedor=provedor_simulado,
        ),
    )
    analise = gravado["analise"]
    resultado = gravado["resultado"]
    if resultado.get("contrato") != CONTRATO_ANALISAR or resultado.get("chamada_anthropic") is not False:
        raise DescobertaErro("autorizacao_indevida", 403)
    logger.info(
        "evento=agente_bi_descoberta usuario_id=%s analise_id=%s dataset_id=%s versao=%s sugestoes=%s",
        escopo.usuario_id,
        analise["id"],
        analise["dataset_id"],
        analise["versao"],
        len(resultado.get("sugestoes") or []),
    )
    return _publicar_descoberta(analise, resultado)


def _descobrir_real(escopo, dataset_id: int, pedido: dict, *, transporte=None) -> dict:
    """Opera dentro da requisição pelo coordenador já existente.

    O ciclo adquire lease, confirma o documento na mesma transação e, se a
    requisição cair no meio, a recuperação da fila não reabre chamada já
    contabilizada. Pendência não é publicada como análise concluída.
    """
    linha = _dataset_no_escopo(escopo, dataset_id)
    try:
        pedido_ok = _validar_pedido(pedido)
    except AnalisarErro as exc:
        raise DescobertaErro(_codigo_publico(exc.codigo)) from exc
    if intencao_c_ausente(pedido_ok):
        raise DescobertaErro("pedido_invalido", 422)
    try:
        perfil = perfilar_dataset(escopo, dataset_id)
        contexto = montar_contexto(perfil, pedido_ok)
        estrutura = obter_dataset(escopo, dataset_id).get("estrutura")
        liberacao = autorizar_transferencia(perfil, contexto, estrutura=estrutura)
    except (IntegracaoErro, AnalisarErro, PerfilErro) as exc:
        raise DescobertaErro(_codigo_publico(getattr(exc, "codigo", "")), _status_codigo(getattr(exc, "codigo", ""))) from exc
    if intencao_c_removida(pedido_ok, liberacao):
        evidencias = _evidencias_publicas(liberacao.get("verificacoes"))
        if "intencao_nao_transportada" not in evidencias:
            evidencias.append("intencao_nao_transportada")
        raise DescobertaErro("intencao_nao_transportada", 422, evidencias=evidencias)
    identidade = _identidade_operacao(escopo, linha, pedido_ok)
    try:
        resultado = _operacao_duravel(
            escopo,
            dataset_id,
            pedido_ok,
            chave_analise=linha.chave,
            transporte=transporte,
            identidade=identidade,
        )
    except IntegracaoErro as exc:
        raise DescobertaErro(_codigo_publico(exc.codigo), _status_codigo(exc.codigo)) from exc
    except ExecucaoErro as exc:
        raise DescobertaErro(_codigo_publico(exc.codigo), _status_codigo(exc.codigo)) from exc
    return _publicar_ciclo(escopo, resultado)


def _operacao_duravel(escopo, dataset_id, pedido, *, chave_analise, transporte, identidade):
    """Tenta a próxima rodada de novo quando outra requisição ocupou o número.

    O limite é o mesmo de `_com_rodada`. Esgotado, o conflito sobe sem
    devolver a análise do outro pedido.
    """
    ultimo = None
    for _ in range(_TENTATIVAS_RODADA):
        try:
            return executar_operacao(
                escopo,
                dataset_id,
                pedido,
                worker_id=f"descoberta-{escopo.usuario_id}-{uuid4().hex[:12]}",
                chave_analise=chave_analise,
                transporte=transporte,
                rodada=_proxima_rodada(escopo, chave_analise),
                autorizado=True,
                lease_segundos=LEASE_MAX_SEGUNDOS,
                chave_idempotencia=identidade,
                chave_recuperacao=identidade,
            )
        except IntegracaoErro as exc:
            ultimo = exc
            if exc.codigo not in _REUSO_OPERACAO:
                raise
    raise ultimo


def _publicar_ciclo(escopo, resultado: dict) -> dict:
    ciclo = resultado.get("ciclo") if isinstance(resultado, dict) else None
    if not isinstance(ciclo, dict):
        raise DescobertaErro("provedor_indisponivel")
    if ciclo.get("em_andamento"):
        raise DescobertaErro("operacao_em_andamento", 409)
    saida = resultado.get("saida")
    abertura = resultado.get("abertura") if isinstance(resultado.get("abertura"), dict) else {}
    if not isinstance(saida, dict):
        execucao = ciclo.get("execucao") if isinstance(ciclo.get("execucao"), dict) else {}
        if not execucao and isinstance(abertura.get("execucao"), dict):
            execucao = abertura["execucao"]
        codigo = _codigo_publico(execucao.get("ultimo_erro") or "provedor_indisponivel")
        raise DescobertaErro(codigo, _status_operacao(codigo, execucao.get("estado")))
    analise_id = abertura.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int):
        raise DescobertaErro("provedor_indisponivel")
    analise = obter_analise(escopo, int(analise_id))
    if analise.get("estado") != "concluida":
        raise DescobertaErro("operacao_em_andamento", 409)
    if (
        saida.get("contrato") != CONTRATO_ANALISAR
        or saida.get("provedor") != PROVEDOR_ANTHROPIC
        or saida.get("chamada_anthropic") is not True
        or saida.get("apto_anthropic") is not False
        or saida.get("contrato_saida_validado") is not True
    ):
        raise DescobertaErro("autorizacao_indevida", 403)
    logger.info(
        "evento=agente_bi_descoberta usuario_id=%s analise_id=%s dataset_id=%s versao=%s sugestoes=%s provedor=%s",
        escopo.usuario_id,
        analise["id"],
        analise["dataset_id"],
        analise["versao"],
        len(saida.get("sugestoes") or []),
        PROVEDOR_ANTHROPIC,
    )
    return _publicar_descoberta(analise, saida)


def _identidade_operacao(escopo, dataset: AgenteBiDataset, pedido: dict) -> str:
    corpo = {
        "conta_id": int(escopo.conta_id),
        "franquia_id": int(escopo.franquia_id),
        "usuario_id": int(escopo.usuario_id),
        "dataset_id": int(dataset.id),
        "versao": int(dataset.versao),
        "impressao": dataset.impressao,
        "pedido": pedido,
    }
    digest = hashlib.sha256(
        json.dumps(corpo, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return "bi." + digest


def _codigo_publico(codigo) -> str:
    if isinstance(codigo, str) and _CODIGO_PUBLICO.fullmatch(codigo):
        return codigo
    return "provedor_indisponivel"


def _status_codigo(codigo: str) -> int:
    publico = _codigo_publico(codigo)
    if publico in {"autorizacao_indevida", "provedor_nao_autorizado", "admissao_recusada", "isolamento"}:
        return 403
    if publico == "nao_encontrado":
        return 404
    if publico in _REUSO_OPERACAO:
        return 409
    return 422


def _status_operacao(codigo: str, estado) -> int:
    if estado in {"incerta", "adquirida"} or codigo in {"operacao_em_andamento", "efeito_incerto"}:
        return 409
    return _status_codigo(codigo)


def _evidencias_publicas(valor) -> list[str]:
    if not isinstance(valor, list):
        return []
    saida = []
    for item in valor:
        if isinstance(item, str) and _VERIFICACAO_PUBLICA.fullmatch(item) and item not in saida:
            saida.append(item)
        if len(saida) >= 16:
            break
    return saida


def aprovar_selecao(escopo, corpo: dict) -> dict:
    """Grava até seis gráficos viáveis. Condicional e lacuna não viram análise executável."""
    if not isinstance(corpo, dict) or set(corpo) != _CHAVES_SELECAO:
        raise DescobertaErro("selecao_invalida")
    if corpo.get("confirmacao") is not True:
        raise DescobertaErro("confirmacao_ausente")
    analise_id = _identificador(corpo.get("analise_id"))
    if analise_id is None:
        raise DescobertaErro("selecao_invalida")
    identifcadores = _identificadores(corpo.get("sugestoes"))
    origem = obter_analise(escopo, analise_id)
    resultado = _resultado_de_descoberta(origem)
    escolhidas = _graficos_viaveis(resultado, identifcadores)
    registro = _confirmacoes(resultado, identifcadores, corpo.get("perguntas"))
    _conferir_trilha(origem, resultado)
    documento = {
        "contrato": CONTRATO_SELECAO,
        "origem_analise_id": origem["id"],
        "origem_contrato": CONTRATO_ANALISAR,
        "vinculo_preparacao": resultado["vinculo_preparacao"],
        "dataset_id": origem["dataset_id"],
        "dataset_versao": origem["versao"],
        "dataset_impressao": origem["impressao"],
        "modalidades": list(resultado["modalidades"]),
        "limite_graficos": LIMITE_GRAFICOS,
        "graficos": escolhidas,
        "perguntas_confirmadas": registro["confirmadas"],
        "executavel": "somente_viavel",
        "juncao": _JUNCAO,
        "chamada_anthropic": False,
        "apto_anthropic": False,
    }
    gravada = _com_rodada(
        escopo,
        origem["chave"],
        lambda rodada: criar_analise(
            escopo,
            {
                "chave": origem["chave"],
                "rodada": rodada,
                "dataset_id": origem["dataset_id"],
                "versao": origem["versao"],
                "impressao": origem["impressao"],
                "etapa": "selecao",
                "estado": "concluida",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_SELECAO,
                    "origem_analise_id": origem["id"],
                    "modalidades": list(resultado["modalidades"]),
                    "vinculo_preparacao": resultado["vinculo_preparacao"],
                },
                "regras_negocio": {
                    "catalogo": CATALOGO_CONTRATO,
                    "limite_graficos": LIMITE_GRAFICOS,
                    "executavel": "somente_viavel",
                },
                "esclarecimentos": {
                    "perguntas": registro["confirmadas"],
                    "pendentes": registro["pendentes"],
                    "confirmadas": not registro["pendentes"],
                },
                "contrato_versao": CONTRATO_SELECAO,
                "resultado": documento,
            },
        ),
    )
    logger.info(
        "evento=agente_bi_selecao_aprovada usuario_id=%s analise_id=%s origem=%s graficos=%s versao=%s",
        escopo.usuario_id,
        gravada["id"],
        origem["id"],
        len(escolhidas),
        gravada["versao"],
    )
    return _publicar_selecao(gravada, documento)


def _com_rodada(escopo, chave: str, montar):
    erro = None
    for _ in range(_TENTATIVAS_RODADA):
        try:
            return montar(_proxima_rodada(escopo, chave))
        except AgenteBiErro as exc:
            erro = exc
            if exc.codigo not in _CONFLITOS_RODADA:
                raise
    raise erro


def _proxima_rodada(escopo, chave: str) -> int:
    atual = (
        db.session.query(func.max(AgenteBiAnalise.rodada))
        .filter(
            AgenteBiAnalise.conta_id == escopo.conta_id,
            AgenteBiAnalise.franquia_id == escopo.franquia_id,
            AgenteBiAnalise.usuario_id == escopo.usuario_id,
            AgenteBiAnalise.chave == chave,
        )
        .scalar()
    )
    if atual is None:
        return 1
    return int(atual) + 1


def _dataset_no_escopo(escopo, dataset_id: int) -> AgenteBiDataset:
    linha = (
        AgenteBiDataset.query.filter_by(
            id=dataset_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            usuario_id=escopo.usuario_id,
        ).one_or_none()
    )
    if linha is None:
        raise AgenteBiErro("nao_encontrado", 404)
    return linha


def _proveniencia_de_descoberta(resultado: dict) -> None:
    """Aceita descoberta simulada ou Anthropic já validada. Recusa o restante."""
    if resultado.get("apto_anthropic") is not False or resultado.get("contrato_saida_validado") is not True:
        raise DescobertaErro("autorizacao_indevida", 403)
    provedor = resultado.get("provedor")
    chamada = resultado.get("chamada_anthropic")
    if provedor == PROVEDOR_SIMULADO and chamada is False:
        return
    if provedor == PROVEDOR_ANTHROPIC and chamada is True:
        return
    raise DescobertaErro("autorizacao_indevida", 403)


def _resultado_de_descoberta(analise: dict) -> dict:
    """Lê uma descoberta concluída.

    O simulador permanece na etapa descoberta. A fila durável marca a mesma
    análise como execucao ao adquirir o lease. O contrato gravado continua
    sendo analisar.v1; cálculo e definição usam outro contrato.
    """
    if analise.get("contrato_versao") != CONTRATO_ANALISAR:
        raise DescobertaErro("analise_indisponivel")
    if analise.get("etapa") not in {"descoberta", "execucao"}:
        raise DescobertaErro("analise_indisponivel")
    if analise.get("estado") != "concluida":
        raise DescobertaErro("analise_indisponivel")
    resultado = analise.get("resultado")
    if not isinstance(resultado, dict) or resultado.get("contrato") != CONTRATO_ANALISAR:
        raise DescobertaErro("analise_indisponivel")
    _proveniencia_de_descoberta(resultado)
    if not isinstance(resultado.get("sugestoes"), list) or not isinstance(resultado.get("perguntas"), list):
        raise DescobertaErro("analise_indisponivel")
    if not isinstance(resultado.get("modalidades"), list) or not isinstance(resultado.get("vinculo_preparacao"), str):
        raise DescobertaErro("analise_indisponivel")
    return resultado


def _conferir_trilha(analise: dict, resultado: dict) -> None:
    trilha = resultado.get("trilha")
    if not isinstance(trilha, dict):
        raise DescobertaErro("analise_indisponivel")
    if (
        trilha.get("dataset_id") != analise["dataset_id"]
        or trilha.get("dataset_versao") != analise["versao"]
        or trilha.get("dataset_impressao") != analise["impressao"]
    ):
        raise DescobertaErro("analise_indisponivel")


def _identificadores(valor) -> list[str]:
    if not isinstance(valor, list) or any(not isinstance(item, str) for item in valor):
        raise DescobertaErro("selecao_invalida")
    if len(valor) > LIMITE_GRAFICOS:
        raise DescobertaErro("limite_graficos")
    if not valor or len(set(valor)) != len(valor) or any(_ID_SUGESTAO.fullmatch(item) is None for item in valor):
        raise DescobertaErro("selecao_invalida")
    return list(valor)


def _graficos_viaveis(resultado: dict, identificadores: list[str]) -> list[dict]:
    por_id = {}
    for item in resultado["sugestoes"]:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            por_id[item["id"]] = item
    escolhidas = []
    for identificador in identificadores:
        item = por_id.get(identificador)
        if item is None:
            raise DescobertaErro("sugestao_inexistente")
        if item.get("status") != "viavel" or item.get("origem") not in _ORIGENS:
            raise DescobertaErro("sugestao_nao_executavel")
        grafico = item.get("grafico")
        if not isinstance(grafico, str) or grafico not in TIPOS_GRAFICO or item.get("juncao") != _JUNCAO:
            raise DescobertaErro("sugestao_nao_executavel")
        evidencias = item.get("evidencias")
        if not isinstance(evidencias, list) or not evidencias:
            raise DescobertaErro("sugestao_nao_executavel")
        escolhidas.append(
            {
                "id": item["id"],
                "status": "viavel",
                "leitura": item.get("leitura"),
                "grafico": grafico,
                "operacao": item.get("operacao"),
                "origem": item["origem"],
                "regiao": item.get("regiao"),
                "medida": item.get("medida"),
                "agrupamento": item.get("agrupamento"),
                "segunda_medida": item.get("segunda_medida"),
                "evidencias": list(evidencias),
                "juncao": _JUNCAO,
            }
        )
    return escolhidas


def _confirmacoes(resultado, identificadores, bruto) -> dict:
    """Espelha as perguntas da rodada e só cobra as que a seleção depende."""
    perguntas = resultado.get("perguntas") if isinstance(resultado, dict) else None
    if not isinstance(perguntas, list) or not isinstance(bruto, list):
        raise DescobertaErro("selecao_invalida")
    esperadas = []
    for item in perguntas:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str) or _ID_PERGUNTA.fullmatch(item["id"]) is None:
            raise DescobertaErro("analise_indisponivel")
        esperadas.append(item["id"])
    if len(set(esperadas)) != len(esperadas):
        raise DescobertaErro("analise_indisponivel")
    if len(bruto) != len(esperadas):
        raise DescobertaErro("selecao_invalida")
    estados = {}
    for item in bruto:
        if not isinstance(item, dict) or set(item) != _CHAVES_CONFIRMACAO:
            raise DescobertaErro("selecao_invalida")
        identificador = item.get("id")
        confirmada = item.get("confirmada")
        if not isinstance(identificador, str) or not isinstance(confirmada, bool):
            raise DescobertaErro("selecao_invalida")
        if identificador in estados:
            raise DescobertaErro("selecao_invalida")
        estados[identificador] = confirmada
    if set(estados) != set(esperadas):
        raise DescobertaErro("selecao_invalida")
    obrigatorias = _perguntas_obrigatorias(resultado, identificadores, set(esperadas))
    if any(estados[identificador] is not True for identificador in obrigatorias):
        raise DescobertaErro("perguntas_pendentes")
    return {
        "confirmadas": [identificador for identificador in esperadas if estados[identificador] is True],
        "pendentes": [identificador for identificador in esperadas if estados[identificador] is False],
    }


def _perguntas_obrigatorias(resultado, identificadores, conhecidas: set[str]) -> list[str]:
    """Perguntas ligadas às sugestões escolhidas. Viável não carrega pergunta."""
    sugestoes = resultado.get("sugestoes")
    if not isinstance(sugestoes, list):
        raise DescobertaErro("analise_indisponivel")
    por_id = {}
    for item in sugestoes:
        if isinstance(item, dict) and isinstance(item.get("id"), str):
            por_id[item["id"]] = item
    obrigatorias = []
    for identificador in identificadores:
        item = por_id.get(identificador)
        if item is None:
            raise DescobertaErro("sugestao_inexistente")
        vinculadas = item.get("perguntas")
        if not isinstance(vinculadas, list) or any(not isinstance(valor, str) for valor in vinculadas):
            raise DescobertaErro("analise_indisponivel")
        if len(set(vinculadas)) != len(vinculadas) or any(valor not in conhecidas for valor in vinculadas):
            raise DescobertaErro("analise_indisponivel")
        if item.get("status") == "viavel":
            if vinculadas:
                raise DescobertaErro("analise_indisponivel")
            continue
        for valor in vinculadas:
            if valor not in obrigatorias:
                obrigatorias.append(valor)
    return obrigatorias


def _publicar_descoberta(analise: dict, resultado: dict) -> dict:
    return {
        "ok": True,
        "analise_id": analise["id"],
        "usuario_id": analise["usuario_id"],
        "dataset_id": analise["dataset_id"],
        "versao": analise["versao"],
        "impressao": analise["impressao"],
        "rodada": analise["rodada"],
        "etapa": analise["etapa"],
        "contrato_versao": analise["contrato_versao"],
        "resultado": resultado,
    }


def _publicar_selecao(analise: dict, documento: dict) -> dict:
    return {
        "ok": True,
        "selecao": {
            "id": analise["id"],
            "usuario_id": analise["usuario_id"],
            "dataset_id": analise["dataset_id"],
            "versao": analise["versao"],
            "impressao": analise["impressao"],
            "rodada": analise["rodada"],
            "etapa": analise["etapa"],
            "estado": analise["estado"],
            "contrato_versao": analise["contrato_versao"],
            "graficos": len(documento["graficos"]),
            "resultado": documento,
        },
    }


def _dataset_publico(escopo, dataset: dict) -> dict:
    return {
        "id": int(dataset["id"]),
        "usuario_id": escopo.usuario_id,
        "chave": dataset.get("chave"),
        "versao": dataset.get("versao"),
        "impressao": dataset.get("impressao"),
    }


def _protecao_publica(metadados):
    if not isinstance(metadados, dict):
        return None
    protecao = metadados.get("protecao")
    if not isinstance(protecao, dict):
        return None
    decisao = protecao.get("decisao")
    if decisao not in {"minimizada", "isolada"}:
        return None
    return decisao


def _resumo(perfil: dict) -> dict:
    return {
        "arquivos": _contagem(perfil.get("arquivos")),
        "abas": _contagem(perfil.get("abas")),
        "regioes": _contagem(perfil.get("regioes")),
        "celulas": _contagem(perfil.get("celulas")),
        "linhas_elegiveis": _contagem(perfil.get("linhas_elegiveis")),
        "formulas": _contagem(perfil.get("formulas")),
        "ambigua": perfil.get("ambigua") is True,
    }


def _arquivos_publicos(estrutura: dict) -> list[dict]:
    saida = []
    for item in _lista(estrutura.get("arquivos"))[:8]:
        if not isinstance(item, dict):
            continue
        indice = _inteiro(item.get("indice"))
        if indice is None:
            continue
        saida.append(
            {
                "indice": indice,
                "nome": _nome(item.get("nome")),
                "alertas": _codigos_texto(item.get("alertas")),
            }
        )
    return saida


def _abas_publicas(estrutura: dict) -> list[dict]:
    saida = []
    for item in _lista(estrutura.get("abas"))[:30]:
        if not isinstance(item, dict):
            continue
        arquivo = _inteiro(item.get("arquivo"))
        indice = _inteiro(item.get("indice"))
        if arquivo is None or indice is None:
            continue
        estado = item.get("estado")
        saida.append(
            {
                "arquivo": arquivo,
                "indice": indice,
                "nome": _nome(item.get("nome")),
                "estado": estado if isinstance(estado, str) and _CODIGO.fullmatch(estado) else "desconhecido",
                "linhas": _contagem(item.get("linhas")),
                "colunas": _contagem(item.get("colunas")),
                "alertas": _codigos_texto(item.get("alertas")),
            }
        )
    return saida


def _colunas_publicas(perfil: dict) -> list[dict]:
    saida = []
    for item in _lista(perfil.get("colunas")):
        if not isinstance(item, dict):
            continue
        coordenadas = {chave: _inteiro(item.get(chave)) for chave in ("arquivo", "aba", "regiao", "indice")}
        if any(valor is None for valor in coordenadas.values()):
            continue
        planilha = _inteiro(item.get("coluna_planilha"))
        dimensao = item.get("dimensao_principal")
        if not isinstance(dimensao, str) or _CODIGO.fullmatch(dimensao) is None:
            continue
        saida.append(
            {
                **coordenadas,
                "coluna_planilha": planilha,
                "dimensao_principal": dimensao,
                "tipos": _codigos_texto(item.get("tipos")),
                "evidencias": _codigos_texto(item.get("evidencias")),
                "dominio_inconsistente": item.get("dominio_inconsistente") is True,
            }
        )
    return saida


def _regioes_publicas(perfil: dict) -> list[dict]:
    saida = []
    for item in _lista(perfil.get("regioes")):
        if not isinstance(item, dict):
            continue
        coordenadas = {chave: _inteiro(item.get(chave)) for chave in ("arquivo", "aba", "regiao")}
        if any(valor is None for valor in coordenadas.values()):
            continue
        papel = item.get("papel")
        cabecalho = item.get("cabecalho") if isinstance(item.get("cabecalho"), dict) else {}
        deteccao = cabecalho.get("deteccao")
        saida.append(
            {
                **coordenadas,
                "papel": papel if isinstance(papel, str) and _CODIGO.fullmatch(papel) else "indefinida",
                "cabecalho": deteccao if isinstance(deteccao, str) and _CODIGO.fullmatch(deteccao) else "opaco",
                "ambiguidades": _codigos_texto(item.get("ambiguidades_estruturais")),
                "linhas": _contagem(item.get("linhas_observadas")),
                "evidencias": _codigos_texto(item.get("evidencias")),
            }
        )
    return saida


def _qualidade_publica(perfil: dict) -> dict:
    bloco = perfil.get("qualidade") if isinstance(perfil.get("qualidade"), dict) else {}
    totais = bloco.get("totais") if isinstance(bloco.get("totais"), dict) else {}
    return {chave: _contagem(totais.get(chave)) for chave in _TOTAIS}


def _ambiguidades_publicas(valor) -> list[dict]:
    saida = []
    for item in _lista(valor)[:40]:
        if not isinstance(item, dict):
            continue
        codigo = item.get("codigo")
        if not isinstance(codigo, str) or _CODIGO.fullmatch(codigo) is None:
            continue
        publico = {"codigo": codigo}
        for chave in ("arquivo", "aba", "regiao", "coluna"):
            if chave not in item or item[chave] is None:
                continue
            numero = _inteiro(item[chave])
            if numero is None:
                continue
            publico[chave] = numero
        saida.append(publico)
    return saida


def _codigos_texto(valor) -> list[str]:
    saida = []
    for item in _lista(valor)[:20]:
        if isinstance(item, str) and _CODIGO.fullmatch(item):
            saida.append(item)
    return saida


def _lista(valor) -> list:
    return valor if isinstance(valor, list) else []


def _identificador(valor):
    numero = _inteiro(valor)
    if numero is None or numero < 1:
        return None
    return numero


def _inteiro(valor):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        return None
    return valor


def _contagem(valor) -> int:
    numero = _inteiro(valor)
    return 0 if numero is None else numero


def _nome(valor) -> str:
    if not isinstance(valor, str):
        return "arquivo"
    limpo = "".join(caractere for caractere in valor if caractere.isprintable()).strip()
    return (limpo or "arquivo")[:80]
