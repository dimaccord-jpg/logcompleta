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
    OPERACOES,
    _LACUNAS,
    _STATUS as _STATUS_SUGESTAO,
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
from app.services.agente_bi_perfil_semantico_service import PerfilErro, _CATEGORIAS, perfilar_dataset
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
    def __init__(self, codigo: str, status: int = 422, evidencias: list | None = None, diagnostico: dict | None = None):
        self.codigo = codigo
        self.status = status
        self.evidencias = list(evidencias) if evidencias else None
        self.diagnostico = diagnostico_publico(diagnostico)
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
        "diagnosticos": diagnosticar_preparacao(perfil if persistido else {}, preparacao.get("metadados_preparacao")),
        "achados": achados_estruturais(
            perfil if persistido else {}, preparacao.get("metadados_preparacao"), estrutura
        ),
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
            raise DescobertaErro(
                exc.codigo,
                exc.status,
                evidencias=exc.evidencias,
                diagnostico=getattr(exc, "diagnostico", None),
            ) from exc
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
    except IntegracaoErro as exc:
        raise DescobertaErro(
            _codigo_publico(exc.codigo),
            _status_codigo(exc.codigo),
            diagnostico=diagnostico_de_integracao(exc),
        ) from exc
    except (AnalisarErro, PerfilErro) as exc:
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
        raise DescobertaErro(
            _codigo_publico(exc.codigo),
            _status_codigo(exc.codigo),
            diagnostico=diagnostico_de_integracao(exc),
        ) from exc
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
        "diagnosticos": diagnosticar_resultado(resultado),
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


_CATEGORIAS_DIAGNOSTICO = frozenset({"protecao", "correcao", "confirmacao", "dados_adicionais"})
_FATOS = frozenset(
    {
        "cabecalho_ambiguo",
        "divisao_lateral_ambigua",
        "valores_ausentes",
        "relacao_nao_confirmada",
        "protecao_aplicada",
        "dominio_inconsistente",
        "formula_sem_cache",
        "dados_incompletos",
        "dado_necessario",
        "quantidade_acima_do_limite",
        "sugestao_nao_executavel",
        "sugestao_descartada",
    }
)
_HIPOTESES = frozenset(
    {
        "indicadores_da_regiao_sem_titulo",
        "indicadores_dependem_da_coluna",
        "indicadores_dependem_do_dado",
    }
)
_IMPACTOS = frozenset(
    {
        "indicadores_da_regiao",
        "indicadores_da_coluna",
        "juncao_nao_executada",
        "protecao_aplicada",
        "transferencia_bloqueada",
        "dado_necessario",
        "sugestao_condicional",
        "cobertura_parcial",
        "avaliacao_pendente",
        "sem_impacto_comprovado",
    }
)
# Achado registrado sem pedido de intervenção: antes da intenção ou sem
# indicador desta análise que dependa dele.
_IMPACTOS_INFORMATIVOS = frozenset({"avaliacao_pendente", "sem_impacto_comprovado"})
_ACHADOS_ESTRUTURAIS = frozenset(
    {
        "cabecalho_ambiguo",
        "divisao_lateral_ambigua",
        "coluna_parcialmente_vazia",
        "dominio_inconsistente",
        "formula_sem_cache",
        "dados_incompletos",
        "relacao_candidata_nao_confirmada",
    }
)
# Motivo de descarte da colaboração, como o usuário lê. Lista fechada: o
# aviso não carrega posição de célula nem valor da proposta.
_TEXTO_MOTIVO = {
    "evidencia_ausente": "evidência ausente",
    "referencia_inexistente": "referência inexistente",
    "juncao_nao_confirmada": "junção não confirmada",
    "metrica_inventada": "métrica fora do catálogo",
    "grafico_incompativel": "gráfico incompatível",
    "operacao_incompativel": "operação incompatível",
    "valor_fora_do_catalogo": "valor fora do catálogo",
    "viabilidade_nao_comprovada": "a região não cumpre as condições de uma sugestão viável",
}
# Pergunta do analisar.v1 que liga uma sugestão ao achado da mesma região.
_PERGUNTAS_DO_ACHADO = {
    "cabecalho_ambiguo": frozenset({"confirmar_rotulos"}),
    "divisao_lateral_ambigua": frozenset({"resolver_divisao_lateral"}),
    "formula_sem_cache": frozenset({"resolver_formula_sem_cache"}),
    "dominio_inconsistente": frozenset({"resolver_tipo_misto"}),
    "dados_incompletos": frozenset({"informar_dados_ausentes"}),
}
_ACOES = frozenset(
    {
        "revisar_titulos",
        "revisar_regiao",
        "completar_valores",
        "confirmar_relacao",
        "nenhuma",
        "informar_dado",
        "reduzir_estrutura",
    }
)
_TETO_DIAGNOSTICOS = 12
_TEXTO_DIAGNOSTICO = 400


def _inteiro_diagnostico(valor, *, positivo=False):
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    if valor < 0 or valor > 1_000_000:
        return None
    if positivo and valor < 1:
        return None
    return valor


def _codigo_em(valor, permitidos: frozenset):
    if not isinstance(valor, str) or valor not in permitidos or not 1 <= len(valor) <= 40:
        return None
    for caractere in valor:
        if caractere == "_":
            continue
        if not caractere.isalnum() or caractere.isupper():
            return None
    return valor


def _regiao_de(item):
    if not isinstance(item, dict):
        return None
    bruto = item
    if "arquivo" not in item and isinstance(item.get("regiao"), dict):
        bruto = item["regiao"]
    if not isinstance(bruto, dict):
        return None
    local = {}
    for chave in ("arquivo", "aba", "regiao"):
        numero = _inteiro_diagnostico(bruto.get(chave))
        if numero is None:
            return None
        local[chave] = numero
    return local


def _coluna_de(item):
    if not isinstance(item, dict):
        return None
    local = _regiao_de(item)
    if local is None:
        return None
    planilha = _inteiro_diagnostico(item.get("coluna_planilha"), positivo=True)
    if planilha is not None:
        local["coluna"] = planilha
    return local


def _medidas_uteis(perfil, *, ignorar=None) -> list[dict]:
    saida = []
    for coluna in perfil.get("colunas") or []:
        if not isinstance(coluna, dict):
            continue
        local = _coluna_de(coluna)
        if local is None or local == ignorar:
            continue
        if coluna.get("dominio_inconsistente") is not False:
            continue
        presentes = _inteiro_diagnostico(coluna.get("presentes"), positivo=True)
        ausentes = _inteiro_diagnostico(coluna.get("ausentes"))
        if presentes is None or ausentes != 0:
            continue
        saida.append(local)
    return saida


def _continua_sem(perfil, local) -> bool:
    if not isinstance(perfil, dict):
        return False
    for medida in _medidas_uteis(perfil):
        if local is None:
            return True
        if any(medida.get(chave) != local.get(chave) for chave in ("arquivo", "aba", "regiao", "coluna") if chave in local):
            return True
    return False


def _linhas_comprovadas(coluna) -> tuple[list[int] | None, dict | None]:
    ausentes = _inteiro_diagnostico(coluna.get("ausentes"))
    bruto = coluna.get("linhas_ausentes")
    if ausentes is None or ausentes < 1 or not isinstance(bruto, list) or len(bruto) != ausentes:
        return None, None
    linhas = []
    for item in bruto:
        numero = _inteiro_diagnostico(item)
        if numero is None:
            return None, None
        linhas.append(numero)
    if len(set(linhas)) != len(linhas):
        return None, None
    linhas.sort()
    if len(linhas) == 1:
        return linhas, None
    if linhas[-1] - linhas[0] + 1 == len(linhas):
        return None, {"inicio": linhas[0], "fim": linhas[-1]}
    return linhas, None


def _texto_local(local) -> str:
    if not isinstance(local, dict):
        return ""
    partes = []
    if "coluna" in local:
        partes.append(f"coluna {local['coluna']}")
    if "regiao" in local:
        partes.append(f"região {local['regiao']}")
    if "aba" in local:
        partes.append(f"aba {local['aba']}")
    if "arquivo" in local:
        partes.append(f"arquivo {local['arquivo']}")
    if "linha" in local:
        partes.append(f"linha {local['linha']}")
    return ", ".join(partes)


def _texto_linhas(linhas, intervalo) -> str:
    if isinstance(intervalo, dict):
        return f"As linhas {intervalo['inicio']} a {intervalo['fim']} não têm valor."
    if isinstance(linhas, list) and len(linhas) == 1:
        return f"A linha {linhas[0]} não tem valor."
    if isinstance(linhas, list) and linhas:
        if len(linhas) > 8:
            return f"Há {len(linhas)} linhas comprovadas com valor ausente."
        lista = ", ".join(str(item) for item in linhas)
        return f"As linhas comprovadas são {lista}."
    return "A linha exata não foi comprovada."


def _montar(base: dict) -> dict | None:
    codigo = _codigo_em(base.get("codigo"), _FATOS | _LACUNAS | frozenset({"limite_estrutural_excedido", "dados_pessoais_protegidos", "coluna_parcialmente_vazia", "relacao_candidata_nao_confirmada", "cobertura_parcial", "proposta_descartada"}))
    categoria = _codigo_em(base.get("categoria"), _CATEGORIAS_DIAGNOSTICO)
    impacto = _codigo_em(base.get("impacto"), _IMPACTOS)
    acao = _codigo_em(base.get("acao"), _ACOES)
    fato = _codigo_em(base.get("fato"), _FATOS)
    if not codigo or not categoria or not impacto or not acao or not fato:
        return None
    if not isinstance(base.get("continuidade_independente"), bool):
        return None
    saida = {
        "codigo": codigo,
        "categoria": categoria,
        "impacto": impacto,
        "acao": acao,
        "continuidade_independente": base["continuidade_independente"],
        "fato": fato,
    }
    hipotese = base.get("hipotese")
    if hipotese is not None:
        hipotese = _codigo_em(hipotese, _HIPOTESES)
        if hipotese is None:
            return None
        saida["hipotese"] = hipotese
    local = base.get("local")
    if local is not None:
        if not isinstance(local, dict):
            return None
        limpo = {}
        for chave in ("arquivo", "aba", "regiao", "coluna", "linha"):
            if chave not in local:
                continue
            numero = _inteiro_diagnostico(local[chave])
            if numero is None:
                return None
            limpo[chave] = numero
        if not limpo:
            return None
        saida["local"] = limpo
    motivos = base.get("motivos")
    if motivos is not None:
        if not isinstance(motivos, list) or not motivos or any(motivo not in _TEXTO_MOTIVO for motivo in motivos):
            return None
        saida["motivos"] = list(motivos)
    for chave in ("quantidade", "limite", "consolidados", "consolidados_ausentes", "consolidados_dominio"):
        if chave not in base:
            continue
        numero = _inteiro_diagnostico(base[chave])
        if numero is None:
            return None
        saida[chave] = numero
    categoria_dado = base.get("categoria_dado")
    if categoria_dado is not None:
        if categoria_dado not in _CATEGORIAS:
            return None
        saida["categoria_dado"] = categoria_dado
    intervalo = base.get("intervalo")
    if intervalo is not None:
        if not isinstance(intervalo, dict):
            return None
        inicio = _inteiro_diagnostico(intervalo.get("inicio"))
        fim = _inteiro_diagnostico(intervalo.get("fim"))
        if inicio is None or fim is None or fim < inicio:
            return None
        saida["intervalo"] = {"inicio": inicio, "fim": fim}
    linhas = base.get("linhas")
    if linhas is not None:
        if not isinstance(linhas, list) or not linhas:
            return None
        saida_linhas = []
        for item in linhas:
            numero = _inteiro_diagnostico(item)
            if numero is None:
                return None
            saida_linhas.append(numero)
        saida["linhas"] = saida_linhas
    regioes = base.get("regioes")
    if regioes is not None:
        if not isinstance(regioes, list) or not regioes:
            return None
        copiadas = []
        for item in regioes:
            local_regiao = _regiao_de(item)
            if local_regiao is None:
                return None
            copiadas.append(local_regiao)
        saida["regioes"] = copiadas
    indicadores = base.get("indicadores")
    if indicadores is not None:
        if not isinstance(indicadores, list):
            return None
        copiados = []
        for item in indicadores:
            if not isinstance(item, dict):
                return None
            identificador = item.get("id")
            status = _codigo_em(item.get("status"), _STATUS_SUGESTAO)
            operacao = item.get("operacao")
            if not isinstance(identificador, str) or not _ID_SUGESTAO.fullmatch(identificador) or status is None:
                return None
            if operacao is not None and operacao not in OPERACOES:
                return None
            copiados.append({"id": identificador, "status": status, "operacao": operacao})
        saida["indicadores"] = copiados
    mensagem = _mensagem(saida)
    if mensagem is None:
        return None
    saida["mensagem"] = mensagem
    saida["nivel"] = _nivel(saida)
    return saida


def _nivel(item: dict) -> str:
    """Informação, esclarecimento, correção necessária ou bloqueio."""
    if item["impacto"] in _IMPACTOS_INFORMATIVOS:
        return "informacao"
    if item["categoria"] == "confirmacao":
        return "esclarecimento"
    if item["acao"] == "nenhuma":
        return "informacao"
    if item["continuidade_independente"]:
        return "correcao"
    return "bloqueio"


def _mensagem_informativa(item: dict) -> str | None:
    codigo = item["codigo"]
    local = _texto_local(item.get("local"))
    if codigo == "relacao_candidata_nao_confirmada":
        regioes = item.get("regioes") or []
        if len(regioes) >= 2:
            fato = f"há uma relação candidata entre {_texto_local(regioes[0])} e {_texto_local(regioes[1])}. A junção não foi executada"
        else:
            fato = "há uma relação candidata. A junção não foi executada"
    else:
        quantidade = item.get("quantidade")
        fato = {
            "cabecalho_ambiguo": "o cabeçalho está ambíguo",
            "divisao_lateral_ambigua": "a divisão da planilha está ambígua",
            "coluna_parcialmente_vazia": "há 1 valor ausente" if quantidade == 1 else f"há {quantidade} valores ausentes",
            "dominio_inconsistente": "a coluna mistura tipos de valor",
            "formula_sem_cache": "há fórmula sem valor em cache",
            "dados_incompletos": "a análise registrou dados incompletos",
        }[codigo]
        if local:
            fato = f"{fato} em {local}"
        elif codigo == "dados_incompletos":
            fato = f"{fato}. Não há coluna, linha ou região comprovada para este achado"
    if item["impacto"] == "avaliacao_pendente":
        efeito = "O impacto será avaliado quando houver um pedido de análise. Nenhuma ação é necessária agora."
    else:
        efeito = "Nenhum indicador desta análise depende desse achado. Nenhuma ação é necessária agora."
    return f"Fato: {fato}. {efeito}"


def _mensagem(item: dict) -> str | None:
    codigo = item["codigo"]
    local = _texto_local(item.get("local"))
    if item["impacto"] in _IMPACTOS_INFORMATIVOS:
        if codigo not in _ACHADOS_ESTRUTURAIS:
            return None
        return _limitar(_mensagem_informativa(item))
    continuidade = (
        "As análises que não dependem desta informação podem continuar."
        if item["continuidade_independente"]
        else "Não há outro indicador independente comprovado."
    )
    if codigo == "cabecalho_ambiguo":
        onde = f"em {local}" if local else "sem coordenada mais específica"
        texto = (
            f"Fato: o cabeçalho está ambíguo {onde}. "
            "Revise os títulos das colunas nessa aba e região. "
            "Hipótese analítica: os indicadores dessa região ficam sem uma leitura confiável dos títulos. "
            f"{continuidade}"
        )
    elif codigo == "divisao_lateral_ambigua":
        texto = (
            f"Fato: a divisão da planilha está ambígua em {local}. "
            "Revise essa região antes de tratar os blocos como tabelas separadas. "
            f"{continuidade}"
        )
    elif codigo == "coluna_parcialmente_vazia":
        linhas = _texto_linhas(item.get("linhas"), item.get("intervalo"))
        if item.get("local") and item["local"].get("linha") is not None:
            linhas = f"A linha {item['local']['linha']} não tem valor."
        quantidade = item.get("quantidade")
        contagem = "Há 1 valor ausente." if quantidade == 1 else f"Há {quantidade} valores ausentes."
        texto = (
            f"Fato: a {local} está parcialmente vazia. {linhas} "
            f"{contagem} "
            "Hipótese analítica: os indicadores que usam esta coluna como medida dependem desses valores. "
            "Complete os valores na planilha de origem e envie uma nova versão. "
            f"{continuidade}"
        )
    elif codigo == "relacao_candidata_nao_confirmada":
        regioes = item.get("regioes") or []
        if len(regioes) >= 2:
            esquerda = _texto_local(regioes[0])
            direita = _texto_local(regioes[1])
            onde = f"entre {esquerda} e {direita}"
        elif local:
            onde = f"em {local}"
        else:
            onde = "sem uma região comprovada"
        texto = (
            f"Fato: há uma relação candidata {onde}. A junção não foi executada. "
            "Confirme explicitamente se essas regiões se relacionam. A confirmação não executa a junção. "
            f"{continuidade}"
        )
    elif codigo == "dados_pessoais_protegidos":
        categoria = item.get("categoria_dado")
        categoria_txt = f" da categoria {categoria}" if isinstance(categoria, str) else ""
        tem_coluna = isinstance(item.get("local"), dict) and "coluna" in item["local"]
        if tem_coluna:
            onde = f" A localização autorizada é {local}."
        elif local:
            onde = f" A localização autorizada é {local}. A coluna exata não foi comprovada."
        else:
            onde = " A coluna exata não foi comprovada."
        texto = (
            f"Fato: um dado pessoal{categoria_txt} foi protegido.{onde} "
            "O valor não é mostrado. Nenhuma ação é necessária, porque a proteção já resolveu o problema."
        )
    elif codigo == "dominio_inconsistente":
        texto = (
            f"Fato: a {local} mistura tipos de valor. "
            "Hipótese analítica: indicadores dessa coluna precisam de esclarecimento antes de serem usados. "
            f"{continuidade}"
        )
    elif codigo == "formula_sem_cache":
        onde = f" em {local}" if local else ""
        consolidados = item.get("consolidados")
        derivados = ""
        if consolidados:
            # Só cita o tipo contado. Sem a contagem por tipo, não nomeia nenhum.
            tipos = []
            if item.get("consolidados_ausentes"):
                tipos.append(f"valores ausentes em {item['consolidados_ausentes']}")
            if item.get("consolidados_dominio"):
                tipos.append(f"mistura de tipos em {item['consolidados_dominio']}")
            detalhe = f" ({' e '.join(tipos)})" if tipos else ""
            derivados = f"{consolidados} achado(s) de coluna vêm dessas fórmulas{detalhe}. "
        texto = (
            f"Fato: há fórmula sem valor em cache nos registros{onde}. "
            "Hipótese analítica: o indicador que depende dessa fórmula não pode usar um resultado ausente. "
            f"{derivados}{continuidade}"
        )
    elif codigo == "cobertura_parcial":
        quantidade = item.get("quantidade")
        itens = "1 item da proposta foi descartado" if quantidade == 1 else f"{quantidade} itens da proposta foram descartados"
        texto = (
            f"Fato: esta análise cobre só parte da proposta. {itens} por não passar na validação oficial. "
            "As sugestões apresentadas foram validadas uma a uma e em conjunto. "
            "Nenhum item descartado virou indicador."
        )
    elif codigo == "proposta_descartada":
        quantidade = item.get("quantidade")
        itens = "1 item da proposta foi descartado" if quantidade == 1 else f"{quantidade} itens da proposta foram descartados"
        motivos = "; ".join(_TEXTO_MOTIVO[motivo] for motivo in item.get("motivos") or [])
        texto = (
            f"Fato: nenhuma sugestão da proposta passou na validação oficial. {itens}. "
            f"Motivos: {motivos}. "
            "Nenhum indicador foi publicado."
        )
    elif codigo == "dados_incompletos":
        onde = f" em {local}" if local else " Não há coluna, linha ou região comprovada para este achado."
        texto = (
            f"Fato: a análise registrou dados incompletos.{onde} "
            "Hipótese analítica: indicadores que dependem desses valores podem ficar condicionais. "
            f"{continuidade}"
        )
    elif codigo == "limite_estrutural_excedido":
        texto = (
            f"Fato: foram identificadas {item.get('quantidade')} colunas. "
            f"O limite aplicável é {item.get('limite')}. "
            "Impacto: a análise guiada não pode transferir essa lista. "
            "Isso não é uma falha genérica de autorização. "
            "Ajuste a planilha de origem para caber no limite já suportado e envie uma nova versão. "
            "As colunas não foram cortadas nem renumeradas."
        )
    elif codigo in _LACUNAS:
        necessario = {
            "dimensao_temporal_ausente": "uma data ou período",
            "medida_numerica_ausente": "uma medida numérica",
            "relacao_ausente": "a relação entre as regiões",
            "sem_registros": "pelo menos um registro",
        }[codigo]
        texto = (
            f"Fato: falta {necessario} para produzir o indicador pedido. "
            "Inclua esse dado na planilha de origem e envie uma nova versão. "
            f"{continuidade}"
        )
    elif codigo == "sugestao_nao_executavel":
        texto = (
            "Fato: uma sugestão permanece condicional ou dependente de dados adicionais. "
            "Ela não entra como análise executável. "
            f"{continuidade}"
        )
    else:
        return None
    return _limitar(texto)


def _limitar(texto) -> str | None:
    if not isinstance(texto, str) or not texto or len(texto) > _TEXTO_DIAGNOSTICO:
        return None
    if any(ord(caractere) < 32 for caractere in texto):
        return None
    return texto


def diagnosticar_preparacao(perfil, metadados=None) -> list[dict]:
    """O que a etapa "Dados preparados" mostra antes da intenção.

    Só proteção de dados e impedimento global comprovado: nenhuma região com
    registro. Os demais achados seguem no perfil, na qualidade e em
    `achados_estruturais`, sem pedido de correção.
    """
    if not isinstance(perfil, dict):
        perfil = {}
    saida = []
    regioes = [item for item in perfil.get("regioes") or [] if isinstance(item, dict)]
    if regioes and all(_inteiro_diagnostico(item.get("linhas_observadas")) == 0 for item in regioes):
        fechado = _montar(
            {
                "codigo": "sem_registros",
                "categoria": "dados_adicionais",
                "impacto": "dado_necessario",
                "acao": "informar_dado",
                "continuidade_independente": False,
                "fato": "dado_necessario",
            }
        )
        if fechado is not None:
            saida.append(fechado)
    for item in _protecoes(perfil, metadados):
        if len(saida) >= _TETO_DIAGNOSTICOS:
            break
        fechado = _montar(item)
        if fechado is not None:
            saida.append(fechado)
    return saida


def achados_estruturais(perfil, metadados=None, estrutura=None) -> list[dict]:
    """Achados do perfil como informação, com a avaliação adiada para depois do pedido."""
    saida = []
    for item in diagnosticar_perfil(perfil, metadados, estrutura):
        if item["codigo"] not in _ACHADOS_ESTRUTURAIS:
            continue
        base = {chave: valor for chave, valor in item.items() if chave not in {"mensagem", "nivel"}}
        base["acao"] = "nenhuma"
        base["impacto"] = "avaliacao_pendente"
        fechado = _montar(base)
        if fechado is not None:
            saida.append(fechado)
    return saida


def diagnostico_de_cobertura(descartes, restantes: bool = True) -> dict | None:
    """Aviso de cobertura. Descartes já minimizados: lista, posição, campo, motivo.

    Sem sugestão restante, o aviso diz que nada foi publicado e cita só os
    códigos de motivo dos descartes.
    """
    if not isinstance(descartes, list) or not descartes:
        return None
    if restantes:
        return _montar(
            {
                "codigo": "cobertura_parcial",
                "categoria": "correcao",
                "impacto": "cobertura_parcial",
                "acao": "nenhuma",
                "continuidade_independente": True,
                "fato": "sugestao_descartada",
                "quantidade": len(descartes),
            }
        )
    motivos = []
    for item in descartes:
        motivo = item.get("motivo") if isinstance(item, dict) else None
        if motivo not in _TEXTO_MOTIVO:
            return None
        if motivo not in motivos:
            motivos.append(motivo)
    return _montar(
        {
            "codigo": "proposta_descartada",
            "categoria": "correcao",
            "impacto": "cobertura_parcial",
            "acao": "nenhuma",
            "continuidade_independente": False,
            "fato": "sugestao_descartada",
            "quantidade": len(descartes),
            "motivos": motivos,
        }
    )


def _formulas_em_registros(estrutura) -> dict:
    """Linhas de fórmula sem cache em registro elegível, por coluna da planilha."""
    saida: dict[tuple, set] = {}
    if not isinstance(estrutura, dict):
        return saida
    for regiao in estrutura.get("regioes") or []:
        if not isinstance(regiao, dict):
            continue
        base = tuple(_inteiro_diagnostico(regiao.get(chave)) for chave in ("arquivo", "aba", "indice"))
        if None in base:
            continue
        for formula in regiao.get("formulas_observadas") or []:
            if not isinstance(formula, dict) or formula.get("cache_estado") == "presente":
                continue
            if formula.get("registro") is not True:
                continue
            linha = _inteiro_diagnostico(formula.get("linha"))
            coluna = _inteiro_diagnostico(formula.get("coluna"), positivo=True)
            if linha is None or coluna is None:
                continue
            saida.setdefault(base + (coluna,), set()).add(linha)
    return saida


def _ambiguidade_de_tipo(estrutura, coluna) -> bool:
    """Data ou número ambíguo na coluna também mistura o domínio, sem fórmula."""
    if not isinstance(estrutura, dict):
        return True
    indice = _inteiro_diagnostico(coluna.get("indice"))
    if indice is None:
        return True
    for item in estrutura.get("ambiguidades") or []:
        if not isinstance(item, dict) or item.get("codigo") not in {"data_ambigua", "numero_ambiguo"}:
            continue
        if (
            item.get("arquivo") == coluna.get("arquivo")
            and item.get("aba") == coluna.get("aba")
            and item.get("regiao") == coluna.get("regiao")
            and item.get("coluna") == indice + 1
        ):
            return True
    return False


def _derivado_de_formula(coluna, formulas, estrutura, codigo) -> bool:
    """Prova que o achado da coluna vem só de fórmula sem cache em registro.

    Ausência: toda linha ausente comprovada tem fórmula sem cache nessa
    coluna. Domínio: o único tipo além do valor é `formula`, e não há data
    ou número ambíguo na coluna. Sem prova, o achado fica separado.
    """
    local = _coluna_de(coluna)
    if local is None or "coluna" not in local:
        return False
    linhas_formula = formulas.get((local["arquivo"], local["aba"], local["regiao"], local["coluna"]))
    if not linhas_formula:
        return False
    if codigo == "coluna_parcialmente_vazia":
        ausentes = _inteiro_diagnostico(coluna.get("ausentes"))
        linhas = coluna.get("linhas_ausentes")
        if ausentes is None or not isinstance(linhas, list) or len(linhas) != ausentes:
            return False
        return all(_inteiro_diagnostico(linha) in linhas_formula for linha in linhas)
    tipos = coluna.get("tipos")
    if not isinstance(tipos, list) or "formula" not in tipos:
        return False
    return len(set(tipos) - {"formula"}) <= 1 and not _ambiguidade_de_tipo(estrutura, coluna)


def diagnosticar_perfil(perfil, metadados=None, estrutura=None) -> list[dict]:
    """Projeta evidências já perfiladas. Não classifica célula nem inventa coordenada.

    Com a estrutura, ausência e mistura de tipo causadas só por fórmula sem
    cache entram no achado da fórmula, contadas em `consolidados`.
    """
    if not isinstance(perfil, dict):
        perfil = {}
    saida = []
    formulas = _formulas_em_registros(estrutura)
    consolidados: dict[tuple, dict[str, int]] = {}

    def acrescentar(item) -> None:
        if len(saida) >= _TETO_DIAGNOSTICOS:
            return
        fechado = _montar(item)
        if fechado is not None:
            saida.append(fechado)

    for regiao in perfil.get("regioes") or []:
        if not isinstance(regiao, dict):
            continue
        local = _regiao_de(regiao)
        if local is None:
            continue
        cabecalho = regiao.get("cabecalho") if isinstance(regiao.get("cabecalho"), dict) else {}
        ambiguidades = regiao.get("ambiguidades_estruturais") if isinstance(regiao.get("ambiguidades_estruturais"), list) else []
        if cabecalho.get("deteccao") == "ambigua" or "cabecalho_ambiguo" in ambiguidades:
            acrescentar(
                {
                    "codigo": "cabecalho_ambiguo",
                    "categoria": "correcao",
                    "impacto": "indicadores_da_regiao",
                    "acao": "revisar_titulos",
                    "continuidade_independente": _continua_sem(perfil, local),
                    "fato": "cabecalho_ambiguo",
                    "hipotese": "indicadores_da_regiao_sem_titulo",
                    "local": local,
                }
            )
        if "divisao_lateral_ambigua" in ambiguidades:
            acrescentar(
                {
                    "codigo": "divisao_lateral_ambigua",
                    "categoria": "correcao",
                    "impacto": "indicadores_da_regiao",
                    "acao": "revisar_regiao",
                    "continuidade_independente": _continua_sem(perfil, local),
                    "fato": "divisao_lateral_ambigua",
                    "local": local,
                }
            )
    for coluna in perfil.get("colunas") or []:
        if not isinstance(coluna, dict):
            continue
        local = _coluna_de(coluna)
        if local is None:
            continue
        presentes = _inteiro_diagnostico(coluna.get("presentes"))
        ausentes = _inteiro_diagnostico(coluna.get("ausentes"))
        regiao_coluna = (local["arquivo"], local["aba"], local["regiao"])
        if presentes is not None and ausentes is not None and presentes > 0 and ausentes > 0:
            if _derivado_de_formula(coluna, formulas, estrutura, "coluna_parcialmente_vazia"):
                contagem = consolidados.setdefault(regiao_coluna, {"ausentes": 0, "dominio": 0})
                contagem["ausentes"] += 1
                ausentes = None
        if presentes is not None and ausentes is not None and presentes > 0 and ausentes > 0:
            linhas, intervalo = _linhas_comprovadas(coluna)
            item = {
                "codigo": "coluna_parcialmente_vazia",
                "categoria": "correcao",
                "impacto": "indicadores_da_coluna",
                "acao": "completar_valores",
                "continuidade_independente": _continua_sem(perfil, local),
                "fato": "valores_ausentes",
                "hipotese": "indicadores_dependem_da_coluna",
                "local": local,
                "quantidade": ausentes,
            }
            if linhas is not None and len(linhas) == 1:
                item["local"] = {**local, "linha": linhas[0]}
            elif intervalo is not None:
                item["intervalo"] = intervalo
            elif linhas is not None:
                item["linhas"] = linhas
            acrescentar(item)
        if coluna.get("dominio_inconsistente") is True and _derivado_de_formula(coluna, formulas, estrutura, "dominio_inconsistente"):
            contagem = consolidados.setdefault(regiao_coluna, {"ausentes": 0, "dominio": 0})
            contagem["dominio"] += 1
        elif coluna.get("dominio_inconsistente") is True:
            acrescentar(
                {
                    "codigo": "dominio_inconsistente",
                    "categoria": "correcao",
                    "impacto": "indicadores_da_coluna",
                    "acao": "revisar_regiao",
                    "continuidade_independente": _continua_sem(perfil, local),
                    "fato": "dominio_inconsistente",
                    "hipotese": "indicadores_dependem_da_coluna",
                    "local": local,
                }
            )
    qualidade = perfil.get("qualidade") if isinstance(perfil.get("qualidade"), dict) else {}
    for regiao in qualidade.get("por_regiao") or []:
        if not isinstance(regiao, dict):
            continue
        bloco = regiao.get("formulas_sem_cache") if isinstance(regiao.get("formulas_sem_cache"), dict) else {}
        # Fórmula de linha pré-preenchida não alimenta registro e não bloqueia.
        bloqueantes = _inteiro_diagnostico(bloco["bloqueantes"] if "bloqueantes" in bloco else bloco.get("observadas"))
        if bloqueantes is None or bloqueantes < 1:
            continue
        local = _regiao_de(regiao)
        item = {
            "codigo": "formula_sem_cache",
            "categoria": "correcao",
            "impacto": "indicadores_da_regiao",
            "acao": "revisar_regiao",
            "continuidade_independente": _continua_sem(perfil, local),
            "fato": "formula_sem_cache",
        }
        if local is not None:
            item["local"] = local
            derivados = consolidados.get((local["arquivo"], local["aba"], local["regiao"]))
            if derivados:
                item["consolidados"] = derivados["ausentes"] + derivados["dominio"]
                if derivados["ausentes"]:
                    item["consolidados_ausentes"] = derivados["ausentes"]
                if derivados["dominio"]:
                    item["consolidados_dominio"] = derivados["dominio"]
        acrescentar(item)
    for relacao in perfil.get("relacoes") or []:
        if not isinstance(relacao, dict) or relacao.get("juncao") != _JUNCAO:
            continue
        lados = []
        for chave in ("lado_unico", "lado_associado"):
            lado = relacao.get(chave)
            regiao = _regiao_de(lado) if isinstance(lado, dict) else None
            if regiao is not None:
                lados.append(regiao)
        if len(lados) != 2:
            continue
        acrescentar(
            {
                "codigo": "relacao_candidata_nao_confirmada",
                "categoria": "confirmacao",
                "impacto": "juncao_nao_executada",
                "acao": "confirmar_relacao",
                "continuidade_independente": _continua_sem(perfil, None),
                "fato": "relacao_nao_confirmada",
                "regioes": lados,
            }
        )
    for item in _protecoes(perfil, metadados):
        acrescentar(item)
    return saida


def _protecoes(perfil, metadados) -> list[dict]:
    if not isinstance(metadados, dict):
        return []
    protecao = metadados.get("protecao")
    if not isinstance(protecao, dict) or protecao.get("decisao") not in {"minimizada", "isolada"}:
        return []
    governanca = perfil.get("governanca") if isinstance(perfil, dict) else {}
    categorias = governanca.get("categorias_detectadas") if isinstance(governanca, dict) else {}
    categoria = None
    if isinstance(categorias, dict) and len(categorias) == 1:
        nome = next(iter(categorias))
        if nome in _CATEGORIAS:
            categoria = nome
    colunas = protecao.get("colunas") if isinstance(protecao.get("colunas"), list) else []
    saida = []
    if not colunas:
        item = {
            "codigo": "dados_pessoais_protegidos",
            "categoria": "protecao",
            "impacto": "protecao_aplicada",
            "acao": "nenhuma",
            "continuidade_independente": True,
            "fato": "protecao_aplicada",
        }
        if categoria is not None:
            item["categoria_dado"] = categoria
        saida.append(item)
        return saida
    for coluna in colunas:
        if not isinstance(coluna, dict):
            continue
        correspondente = _coluna_correspondente(perfil, coluna)
        local = _coluna_de(correspondente if correspondente is not None else coluna)
        item = {
            "codigo": "dados_pessoais_protegidos",
            "categoria": "protecao",
            "impacto": "protecao_aplicada",
            "acao": "nenhuma",
            "continuidade_independente": True,
            "fato": "protecao_aplicada",
        }
        if local is not None:
            item["local"] = local
        if categoria is not None:
            item["categoria_dado"] = categoria
        saida.append(item)
    return saida


def _coluna_correspondente(perfil, registro):
    if not isinstance(perfil, dict) or not isinstance(registro, dict):
        return None
    chave = _tupla_coluna(registro)
    if chave is None:
        return None
    for coluna in perfil.get("colunas") or []:
        if isinstance(coluna, dict) and _tupla_coluna(coluna) == chave:
            return coluna
    return None


def _tupla_coluna(item):
    partes = []
    for nome in ("arquivo", "aba", "regiao", "indice"):
        numero = _inteiro_diagnostico(item.get(nome))
        if numero is None:
            return None
        partes.append(numero)
    return tuple(partes)


def diagnosticar_resultado(resultado) -> list[dict]:
    """Traduz o documento analisar.v1 já validado. Não promove sugestão inválida.

    O achado estrutural só pede intervenção quando uma sugestão não viável
    da mesma região cita a pergunta ligada a ele. Sem essa ligação, fica
    como informação. Sugestão já explicada por um achado não gera um
    segundo aviso, e achado repetido no mesmo lugar aparece uma vez.
    """
    if not isinstance(resultado, dict):
        return []
    sugestoes = resultado.get("sugestoes") if isinstance(resultado.get("sugestoes"), list) else []
    viaveis = [item for item in sugestoes if isinstance(item, dict) and item.get("status") == "viavel"]
    codigos_pergunta = {}
    for pergunta in resultado.get("perguntas") or []:
        if isinstance(pergunta, dict) and isinstance(pergunta.get("id"), str):
            codigos_pergunta[pergunta["id"]] = pergunta.get("codigo")
    saida = []
    vistos = set()

    def acrescentar(item) -> None:
        if len(saida) >= _TETO_DIAGNOSTICOS:
            return
        chave = (item.get("codigo"), json.dumps(item.get("local"), sort_keys=True))
        if item.get("codigo") != "sugestao_nao_executavel" and chave in vistos:
            return
        fechado = _montar(item)
        if fechado is not None:
            vistos.add(chave)
            saida.append(fechado)

    def indicador(item):
        identificador = item.get("id")
        status = item.get("status")
        operacao = item.get("operacao")
        if not isinstance(identificador, str) or _ID_SUGESTAO.fullmatch(identificador) is None:
            return None
        if status not in _STATUS_SUGESTAO:
            return None
        if operacao is not None and operacao not in OPERACOES:
            operacao = None
        return {"id": identificador, "status": status, "operacao": operacao}

    def dependentes(codigo, local) -> list:
        """Sugestões não viáveis da região cuja pergunta aponta para o achado."""
        if local is None:
            return []
        perguntas = _PERGUNTAS_DO_ACHADO[codigo]
        achadas = []
        for item in sugestoes:
            if not isinstance(item, dict) or item.get("status") == "viavel":
                continue
            regiao = _regiao_de(item.get("regiao")) if isinstance(item.get("regiao"), dict) else None
            if regiao != local:
                continue
            ligadas = {codigos_pergunta.get(valor) for valor in item.get("perguntas") or [] if isinstance(valor, str)}
            if ligadas & perguntas:
                achadas.append(item)
        return achadas

    explicadas = set()
    for item in resultado.get("descobertas") or []:
        if not isinstance(item, dict):
            continue
        codigo = item.get("codigo")
        local = _regiao_de(item.get("regiao")) if isinstance(item.get("regiao"), dict) else None
        if codigo == "relacao_candidata_nao_confirmada":
            acrescentar(
                {
                    "codigo": "relacao_candidata_nao_confirmada",
                    "categoria": "confirmacao",
                    "impacto": "juncao_nao_executada",
                    "acao": "confirmar_relacao",
                    "continuidade_independente": bool(viaveis),
                    "fato": "relacao_nao_confirmada",
                    **({} if local is None else {"local": local}),
                }
            )
            continue
        if codigo not in _PERGUNTAS_DO_ACHADO:
            continue
        afetadas = dependentes(codigo, local)
        base = {
            "codigo": codigo,
            "categoria": "correcao",
            "continuidade_independente": bool(viaveis),
            "fato": codigo,
            **({} if local is None else {"local": local}),
        }
        if not afetadas:
            base.update({"impacto": "sem_impacto_comprovado", "acao": "nenhuma"})
            acrescentar(base)
            continue
        explicadas.update(id(sugestao) for sugestao in afetadas)
        lista = [valor for valor in (indicador(sugestao) for sugestao in afetadas) if valor is not None]
        if codigo == "cabecalho_ambiguo":
            base.update(
                {
                    "impacto": "indicadores_da_regiao",
                    "acao": "revisar_titulos",
                    "hipotese": "indicadores_da_regiao_sem_titulo",
                }
            )
        elif codigo == "dados_incompletos":
            base.update(
                {
                    "impacto": "indicadores_da_coluna",
                    "acao": "completar_valores",
                    "hipotese": "indicadores_dependem_do_dado",
                }
            )
        elif codigo == "dominio_inconsistente":
            base.update(
                {
                    "impacto": "indicadores_da_coluna",
                    "acao": "revisar_regiao",
                    "hipotese": "indicadores_dependem_da_coluna",
                }
            )
        else:
            base.update({"impacto": "indicadores_da_regiao", "acao": "revisar_regiao"})
        base["indicadores"] = lista
        acrescentar(base)
    # Divisão lateral não é descoberta do analisar.v1. A pergunta
    # resolver_divisao_lateral, gerada só para a região com essa ambiguidade,
    # é a prova do achado.
    for pergunta in resultado.get("perguntas") or []:
        if not isinstance(pergunta, dict) or pergunta.get("codigo") not in _PERGUNTAS_DO_ACHADO["divisao_lateral_ambigua"]:
            continue
        local = _regiao_de(pergunta.get("regiao")) if isinstance(pergunta.get("regiao"), dict) else None
        afetadas = dependentes("divisao_lateral_ambigua", local)
        if not afetadas:
            continue
        explicadas.update(id(sugestao) for sugestao in afetadas)
        acrescentar(
            {
                "codigo": "divisao_lateral_ambigua",
                "categoria": "correcao",
                "impacto": "indicadores_da_regiao",
                "acao": "revisar_regiao",
                "continuidade_independente": bool(viaveis),
                "fato": "divisao_lateral_ambigua",
                "local": local,
                "indicadores": [valor for valor in (indicador(sugestao) for sugestao in afetadas) if valor is not None],
            }
        )
    for item in sugestoes:
        if not isinstance(item, dict) or item.get("status") == "viavel":
            continue
        lacuna = item.get("lacuna")
        if lacuna in _LACUNAS:
            local = _regiao_de(item.get("regiao")) if isinstance(item.get("regiao"), dict) else None
            acrescentar(
                {
                    "codigo": lacuna,
                    "categoria": "dados_adicionais",
                    "impacto": "dado_necessario",
                    "acao": "informar_dado",
                    "continuidade_independente": bool(viaveis),
                    "fato": "dado_necessario",
                    "hipotese": "indicadores_dependem_do_dado",
                    **({} if local is None else {"local": local}),
                }
            )
        elif id(item) in explicadas:
            continue
        elif item.get("status") in {"condicional", "dependente_de_novos_dados"}:
            proprio = indicador(item)
            acrescentar(
                {
                    "codigo": "sugestao_nao_executavel",
                    "categoria": "confirmacao" if item.get("status") == "condicional" else "dados_adicionais",
                    "impacto": "sugestao_condicional",
                    "acao": "nenhuma" if item.get("status") == "condicional" else "informar_dado",
                    "continuidade_independente": bool(viaveis),
                    "fato": "sugestao_nao_executavel",
                    **({} if proprio is None else {"indicadores": [proprio]}),
                }
            )
    return saida


def diagnostico_de_integracao(exc) -> dict | None:
    if getattr(exc, "codigo", None) != "limite_estrutural_excedido":
        return None
    detalhes = getattr(exc, "detalhes", None)
    if not isinstance(detalhes, dict) or detalhes.get("impacto") != "transferencia_bloqueada":
        return None
    quantidade = _inteiro_diagnostico(detalhes.get("quantidade"), positivo=True)
    limite = _inteiro_diagnostico(detalhes.get("limite"), positivo=True)
    if quantidade is None or limite is None or quantidade <= limite:
        return None
    return _montar(
        {
            "codigo": "limite_estrutural_excedido",
            "categoria": "correcao",
            "impacto": "transferencia_bloqueada",
            "acao": "reduzir_estrutura",
            "continuidade_independente": False,
            "fato": "quantidade_acima_do_limite",
            "quantidade": quantidade,
            "limite": limite,
        }
    )


def diagnostico_publico(valor) -> dict | None:
    if not isinstance(valor, dict):
        return None
    return _montar(valor)
