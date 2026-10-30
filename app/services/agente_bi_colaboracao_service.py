"""Colaboração Claude–Gemini na descoberta do AgenteBI.

Cada chamada externa é uma rodada com análise, execução, chave estável e
tentativa oficial. O checkpoint técnico não é `analisar.v1`. A aprovação
analítica continua no validador existente e só a revisão que passa por ele
vira o documento final, sem outra chamada.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
from decimal import Decimal
from uuid import uuid4

from app.extensions import db
from app.models import (
    AgenteBiAnalise,
    AgenteBiDataset,
    AgenteBiExecucao,
    AgenteBiExecucaoEvento,
    IaChamadaTentativa,
    utcnow_naive,
)
from app.services.agente_bi_analisar_service import (
    CATALOGO_CONTRATO,
    CONTRATO_ANALISAR,
    CONTRATO_CONTEXTO_ANALISAR,
    PROVEDOR_ANTHROPIC,
    AnalisarErro,
    _DESCOBERTAS,
    _EVIDENCIAS,
    _JUNCAO,
    _LACUNAS,
    _LEITURAS,
    _ORIGENS,
    _PERGUNTAS,
    _STATUS,
    GRAFICOS,
    OPERACOES,
    _EVIDENCIAS,
    analisar,
    esquema_rascunho_analisar,
    instrucao_de_rascunho,
    montar_contexto,
)
from app.services.agente_bi_anthropic_service import (
    _liberar_efeito,
    chamada_liquidada,
    chamar_anthropic_governado,
    preparar_saida_estruturada,
)
from app.services.agente_bi_execucao_service import (
    LEASE_MAX_SEGUNDOS,
    MAX_TENTATIVAS,
    EntregaProcessador,
    ExecucaoErro,
    _atualizar_condicional,
    _publicar_transicao,
    enfileirar,
    execucao_para_dict,
    executar_um_ciclo,
    falhar,
    promover_esgotadas,
    recuperar_leases,
)
from app.services.agente_bi_gemini_service import chamar_gemini_governado
from app.services.agente_bi_integracao_service import (
    IntegracaoErro,
    autorizar_transferencia,
    intencao_c_ausente,
    intencao_c_removida,
)
from app.services.agente_bi_perfil_semantico_service import PerfilErro, perfilar_dataset
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    EscopoAgenteBi,
    analise_para_dict,
    criar_analise,
    obter_analise,
)

logger = logging.getLogger(__name__)

CONTRATO_TECNICO = "colaboracao.tecnica.v1"
CONTRATO_CONTEXTO_TECNICO = "contexto.colaboracao.tecnica.v1"
_ACAO_PROPOR = "propor"
_ACAO_CRITICAR = "criticar"
_ACAO_REVISAR = "revisar"
_ACAO_MATERIALIZAR = "materializar"
_PAPEL_CLAUDE = "claude"
_PAPEL_GEMINI = "gemini"
_PAPEL_SERVIDOR = "agente_bi"
_FASE_CLAUDE = "claude_analisando"
_FASE_GEMINI = "gemini_revisando"
_FASE_REVISAO = "claude_consolidando"
_FASE_PRONTO = "pronto"
_CODIGO_CONFIRMACAO = "colaboracao"
_CHECKPOINT = frozenset(
    {
        "evidencia_ausente",
        "referencia_inexistente",
        "juncao_nao_confirmada",
        "metrica_inventada",
        "grafico_incompativel",
        "operacao_incompativel",
    }
)
_CHAVES_TECNICAS = frozenset(
    {
        "contrato",
        "colaboracao_id",
        "pai_id",
        "pai_hash",
        "papel",
        "acao",
        "referencias",
        "proposta",
        "critica",
        "verificacao",
        "hash",
        "tentativa_id",
    }
)
_CHAVES_VERIFICACAO = frozenset({"aprovado", "codigo"})
_CHAVES_DESCARTE = frozenset({"lista", "posicao", "campo", "motivo"})
_LISTAS_DESCARTE = frozenset({"sugestoes", "descobertas"})
_MOTIVO_CATALOGO = "valor_fora_do_catalogo"
_MOTIVOS_DESCARTE = _CHECKPOINT | {_MOTIVO_CATALOGO}
_CHAVES_SUGESTAO_RASCUNHO = frozenset(
    {
        "status",
        "leitura",
        "grafico",
        "operacao",
        "origem",
        "regiao",
        "medida",
        "agrupamento",
        "segunda_medida",
        "evidencias",
        "juncao",
        "lacuna",
        "duvidas",
    }
)
_CHAVES_DESCOBERTA_RASCUNHO = frozenset({"codigo", "regiao", "relacao"})
_CAMPOS_DESCARTE = {
    "sugestoes": _CHAVES_SUGESTAO_RASCUNHO | {"item", "limite"},
    "descobertas": _CHAVES_DESCOBERTA_RASCUNHO | {"item", "limite"},
}
_TETO_LISTA = {"descobertas": 12, "sugestoes": 8}
_TETO_BRUTO = {"descobertas": 24, "sugestoes": 16}
_TETO_DESCARTES = 40
_POSICAO_MAXIMA = 31
_CHAVES_CRITICA = frozenset({"referencias", "medidas", "operacoes", "evidencias", "objecoes"})
_CHAVES_OBJECAO = frozenset({"codigo", "referencia"})
_CHAVES_CONTEXTO = frozenset(
    {
        "contrato",
        "colaboracao_id",
        "pai_id",
        "pai_hash",
        "papel",
        "acao",
        "vinculo_preparacao",
        "dataset_id",
        "dataset_versao",
        "dataset_impressao",
    }
)
_ABERTOS = frozenset({"preparada", "aguardando_execucao", "em_execucao"})
_FILA_LIVRE = frozenset({"aguardando", "falha_recuperavel"})
_ERROS_PRE_CHAMADA = frozenset({"modelo_ausente", "credencial_ausente"})
_CODIGO_RECUPERACAO_PRE_CHAMADA = "recuperacao_pre_chamada"


class ColaboracaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422, evidencias: list | None = None):
        self.codigo = codigo
        self.status = status
        self.evidencias = list(evidencias) if evidencias else None
        super().__init__(codigo)


def executar_descoberta(
    escopo,
    dataset_id: int,
    pedido: dict,
    *,
    transporte=None,
    cliente_gemini=None,
    controle=None,
) -> dict:
    """Avança no máximo uma chamada externa e devolve o estado da colaboração."""
    dataset = _dataset(escopo, dataset_id)
    try:
        from app.services.agente_bi_analisar_service import _validar_pedido

        pedido_ok = _validar_pedido(pedido)
    except AnalisarErro as exc:
        raise ColaboracaoErro(_codigo(exc.codigo)) from exc
    if intencao_c_ausente(pedido_ok):
        raise ColaboracaoErro("pedido_invalido", 422)
    try:
        perfil = perfilar_dataset(escopo, dataset_id)
        contexto = montar_contexto(perfil, pedido_ok)
        from app.services.agente_bi_persistencia_service import obter_dataset

        estrutura = obter_dataset(escopo, dataset_id).get("estrutura")
        liberacao = autorizar_transferencia(perfil, contexto, estrutura=estrutura)
    except (IntegracaoErro, AnalisarErro, PerfilErro, AgenteBiErro) as exc:
        raise ColaboracaoErro(_codigo(getattr(exc, "codigo", "")), _status(getattr(exc, "codigo", ""))) from exc
    if intencao_c_removida(pedido_ok, liberacao):
        raise ColaboracaoErro("intencao_nao_transportada", 422, evidencias=["intencao_nao_transportada"])
    return _avancar(
        escopo,
        dataset,
        pedido_ok,
        perfil,
        contexto,
        liberacao["payload"],
        identidade_colaboracao(escopo, dataset, pedido_ok),
        transporte=transporte,
        cliente_gemini=cliente_gemini,
        controle=controle or {},
    )


def identidade_colaboracao(escopo, dataset: AgenteBiDataset, pedido: dict) -> str:
    return _identidade(
        "cb.",
        {
            "conta_id": int(escopo.conta_id),
            "franquia_id": int(escopo.franquia_id),
            "usuario_id": int(escopo.usuario_id),
            "dataset_id": int(dataset.id),
            "versao": int(dataset.versao),
            "impressao": dataset.impressao,
            "pedido": pedido,
        },
    )


def identidade_rodada(colaboracao_id: str, pai_id: int | None, acao: str, papel: str) -> str:
    """A identidade lógica não inclui fencing."""
    return _identidade(
        "rd.",
        {
            "colaboracao": colaboracao_id,
            "pai": pai_id,
            "acao": acao,
            "papel": papel,
        },
    )


def revalidar_contrato_tecnico(analise: AgenteBiAnalise, referencias: dict, documento) -> dict:
    """Reconfere o envelope gravável. Não chama `validar_saida`.

    O diagnóstico é opcional. Envelope antigo, sem descartes, continua válido.
    """
    if not isinstance(documento, dict) or set(documento) != _chaves_envelope(documento):
        raise ExecucaoErro("contrato_saida_invalido")
    contexto = _json(analise.contexto_json)
    if not isinstance(contexto, dict) or set(contexto) != _CHAVES_CONTEXTO:
        raise ExecucaoErro("identidade_inconsistente")
    if contexto.get("contrato") != CONTRATO_CONTEXTO_TECNICO or documento.get("contrato") != CONTRATO_TECNICO:
        raise ExecucaoErro("contrato_saida_invalido")
    if (
        documento.get("colaboracao_id") != contexto.get("colaboracao_id")
        or documento.get("pai_id") != contexto.get("pai_id")
        or documento.get("pai_hash") != contexto.get("pai_hash")
        or documento.get("papel") != contexto.get("papel")
        or documento.get("acao") != contexto.get("acao")
    ):
        raise ExecucaoErro("identidade_inconsistente")
    _exigir_cadeia(analise, contexto, referencias)
    proposta = documento.get("proposta")
    critica = documento.get("critica")
    if contexto["papel"] == _PAPEL_CLAUDE:
        if critica is not None or minimizar_proposta(proposta) != proposta:
            raise ExecucaoErro("contrato_saida_invalido")
    elif contexto["papel"] == _PAPEL_GEMINI:
        if proposta is not None or minimizar_critica(critica, _contexto_analitico(analise)) != critica:
            raise ExecucaoErro("contrato_saida_invalido")
    else:
        raise ExecucaoErro("contrato_saida_invalido")
    descartes = None
    if "descartes" in documento:
        if contexto["papel"] != _PAPEL_CLAUDE:
            raise ExecucaoErro("contrato_saida_invalido")
        descartes = _minimizar_descartes(documento.get("descartes"))
        if descartes != documento.get("descartes"):
            raise ExecucaoErro("contrato_saida_invalido")
    verificacao = documento.get("verificacao")
    if not isinstance(verificacao, dict) or set(verificacao) != _CHAVES_VERIFICACAO:
        raise ExecucaoErro("contrato_saida_invalido")
    if verificacao.get("aprovado") is True:
        if descartes is not None or verificacao.get("codigo") is not None or contexto["papel"] != _PAPEL_CLAUDE:
            raise ExecucaoErro("contrato_saida_invalido")
    elif verificacao.get("aprovado") is not False:
        raise ExecucaoErro("contrato_saida_invalido")
    elif descartes is None:
        if verificacao.get("codigo") not in _CHECKPOINT:
            raise ExecucaoErro("contrato_saida_invalido")
    elif verificacao.get("codigo") is not None and verificacao.get("codigo") not in _CHECKPOINT:
        raise ExecucaoErro("contrato_saida_invalido")
    if not _referencias_validas(documento.get("referencias")):
        raise ExecucaoErro("contrato_saida_invalido")
    tentativa_id = documento.get("tentativa_id")
    if isinstance(tentativa_id, bool) or not isinstance(tentativa_id, int):
        raise ExecucaoErro("contrato_saida_invalido")
    execucao = db.session.get(AgenteBiExecucao, int(referencias["execucao_id"]))
    if execucao is None or execucao.ia_chamada_tentativa_id != tentativa_id:
        raise ExecucaoErro("identidade_inconsistente")
    tentativa = db.session.get(IaChamadaTentativa, tentativa_id)
    if (
        tentativa is None
        or tentativa.usuario_id != analise.usuario_id
        or tentativa.conta_id != analise.conta_id
        or tentativa.franquia_id != analise.franquia_id
        or tentativa.status != IaChamadaTentativa.STATUS_SETTLED
    ):
        raise ExecucaoErro("identidade_inconsistente")
    if documento.get("hash") != _hash(_corpo_assinado(documento)):
        raise ExecucaoErro("contrato_saida_invalido")
    if not chamada_liquidada(int(execucao.id)):
        raise ExecucaoErro("efeito_incerto")
    return documento


def minimizar_proposta(valor):
    if not isinstance(valor, dict) or set(valor) != {"descobertas", "sugestoes"}:
        return None
    descobertas = _lista_de(valor.get("descobertas"), _descoberta, 12)
    sugestoes = _lista_de(valor.get("sugestoes"), _sugestao, 8)
    if descobertas is None or sugestoes is None:
        return None
    return {"descobertas": descobertas, "sugestoes": sugestoes}


def minimizar_critica(valor, contexto: dict):
    if not isinstance(valor, dict) or set(valor) != _CHAVES_CRITICA:
        return None
    referencias = _lista_de(valor.get("referencias"), _regiao, 8)
    medidas = _lista_de(valor.get("medidas"), _coluna, 8)
    if referencias is None or medidas is None or not isinstance(valor.get("operacoes"), list):
        return None
    if len(valor["operacoes"]) > 8 or any(item not in OPERACOES for item in valor["operacoes"]):
        return None
    if not isinstance(valor.get("evidencias"), list) or len(valor["evidencias"]) > 8:
        return None
    pool = _pool(contexto, referencias, medidas)
    if any(item not in pool for item in valor["evidencias"]):
        return None
    objecoes = []
    bruto = valor.get("objecoes")
    if not isinstance(bruto, list) or len(bruto) > 8:
        return None
    for item in bruto:
        if not isinstance(item, dict) or set(item) != _CHAVES_OBJECAO:
            return None
        if item.get("codigo") not in _CHECKPOINT:
            return None
        referencia = item.get("referencia")
        if referencia is not None:
            referencia = _coluna(referencia)
            if referencia is None or not _coordenada_autorizada(contexto, referencia):
                return None
        objecoes.append({"codigo": item["codigo"], "referencia": referencia})
    if any(not _coordenada_autorizada(contexto, item, regiao=True) for item in referencias):
        return None
    if any(not _coordenada_autorizada(contexto, item) for item in medidas):
        return None
    return {
        "referencias": referencias,
        "medidas": medidas,
        "operacoes": list(valor["operacoes"]),
        "evidencias": list(valor["evidencias"]),
        "objecoes": objecoes,
    }


def instrucao_de_critica() -> str:
    """Instrução estática do revisor. Não inclui dado de planilha."""
    return "\n".join(
        [
            "Voce e o revisor critico da proposta recebida.",
            "Voce nao e o autor da proposta final.",
            "Use somente o contexto autorizado, a proposta, a verificacao e os descartes.",
            "Descartes, quando existirem, trazem somente lista, posicao, campo e motivo.",
            "Referencias, medidas e evidencias precisam existir nesse contexto.",
            "Nao invente coordenada, metrica, valor, rotulo, formula ou cliente.",
            "Nao copie celula nem conteudo bruto.",
            "Responda apenas com JSON, sem texto ao redor e sem markdown.",
            "As chaves sao exatamente referencias, medidas, operacoes, evidencias e objecoes.",
            "referencias: no maximo 8 objetos com arquivo, aba e regiao, inteiros nao negativos.",
            "medidas: no maximo 8 objetos com arquivo, aba, regiao e indice, inteiros nao negativos.",
            "operacoes: no maximo 8 valores dentre " + ", ".join(sorted(OPERACOES)) + ".",
            "evidencias: no maximo 8 codigos presentes na referencia ou na medida citada: "
            + ", ".join(sorted(_EVIDENCIAS))
            + ".",
            "objecoes: no maximo 8 objetos com codigo e referencia.",
            "codigo da objecao: " + ", ".join(sorted(_CHECKPOINT)) + ".",
            "referencia da objecao e nula ou uma coluna autorizada.",
            "Nao inclua outras chaves.",
        ]
    )


def instrucao_de_revisao() -> str:
    """Como o Claude revisa a proposta anterior com a crítica. Não inclui dado de planilha."""
    return "\n".join(
        [
            "Revise a proposta anterior usando somente a critica autorizada.",
            "Corrija o que as objecoes indicam e conserve o restante.",
            "A verificacao anterior pode trazer descartes com lista, posicao, campo e motivo.",
            "Reconstrua a sugestao com o contexto autorizado, sem o valor removido.",
            "Referencias e evidencias precisam existir no contexto autorizado.",
            "A critica nao substitui a proposta nem o contrato de saida.",
        ]
    )


def esquema_critica() -> dict:
    """JSON Schema da crítica. A minimização local continua obrigatória."""
    inteiro = {"type": "integer", "minimum": 0}
    regiao = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "arquivo": inteiro,
            "aba": inteiro,
            "regiao": inteiro,
        },
        "required": ["arquivo", "aba", "regiao"],
    }
    coluna = {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "arquivo": inteiro,
            "aba": inteiro,
            "regiao": inteiro,
            "indice": inteiro,
        },
        "required": ["arquivo", "aba", "regiao", "indice"],
    }
    chaves = ["referencias", "medidas", "operacoes", "evidencias", "objecoes"]
    return {
        "type": "object",
        "additionalProperties": False,
        "propertyOrdering": chaves,
        "required": chaves,
        "properties": {
            "referencias": {"type": "array", "maxItems": 8, "items": regiao},
            "medidas": {"type": "array", "maxItems": 8, "items": coluna},
            "operacoes": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "enum": sorted(OPERACOES)},
            },
            "evidencias": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "enum": sorted(_EVIDENCIAS)},
            },
            "objecoes": {
                "type": "array",
                "maxItems": 8,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "codigo": {"type": "string", "enum": sorted(_CHECKPOINT)},
                        "referencia": {"anyOf": [coluna, {"type": "null"}]},
                    },
                    "required": ["codigo", "referencia"],
                },
            },
        },
    }


def _config_critica() -> dict:
    """Transporte response_json_schema. response_schema do SDK não é aceito pela API."""
    return {
        "system_instruction": instrucao_de_critica(),
        "response_mime_type": "application/json",
        "response_json_schema": esquema_critica(),
    }


def _avancar(escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id, *, transporte, cliente_gemini, controle):
    recuperar_leases()
    promover_esgotadas()
    tecnicas = _tecnicas(escopo, dataset, colaboracao_id)
    final = _materializada(escopo, dataset, colaboracao_id, tecnicas)
    if final is not None:
        return _pronto(escopo, final)
    aberta = next((item for item in tecnicas if item.estado in _ABERTOS), None)
    if aberta is not None:
        return _retomar(
            escopo,
            dataset,
            pedido,
            perfil,
            contexto,
            payload,
            colaboracao_id,
            aberta,
            transporte=transporte,
            cliente_gemini=cliente_gemini,
            controle=controle,
        )
    if _bloqueio_financeiro(tecnicas):
        return _estado("incerta", None, False)
    if not tecnicas:
        return _abrir(
            escopo,
            dataset,
            pedido,
            perfil,
            contexto,
            payload,
            colaboracao_id,
            pai=None,
            acao=_ACAO_PROPOR,
            papel=_PAPEL_CLAUDE,
            transporte=transporte,
            cliente_gemini=cliente_gemini,
            controle=controle,
        )
    ultima = tecnicas[-1]
    if ultima.estado != "concluida" or not ultima.resultado_json:
        return _estado("recusada", None, False)
    documento = _json(ultima.resultado_json)
    if documento.get("acao") == _ACAO_REVISAR and _aprovado(documento):
        return _materializar(escopo, dataset, pedido, perfil, colaboracao_id, ultima, documento, controle)
    if documento.get("acao") == _ACAO_REVISAR and not _progresso(contexto, tecnicas):
        return _preservar(escopo, dataset, pedido, perfil, colaboracao_id, tecnicas, controle)
    acao, papel = _seguinte(documento)
    if acao is None:
        return _estado("encerrada", None, False)
    return _abrir(
        escopo,
        dataset,
        pedido,
        perfil,
        contexto,
        payload,
        colaboracao_id,
        pai=ultima,
        acao=acao,
        papel=papel,
        transporte=transporte,
        cliente_gemini=cliente_gemini,
        controle=controle,
    )


def _retomar(escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id, analise, *, transporte, cliente_gemini, controle):
    execucao = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one_or_none()
    if execucao is None:
        enfileirar(escopo, int(analise.id), chave_idempotencia=analise.chave_recuperacao)
        execucao = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one()
    db.session.refresh(execucao)
    if execucao.estado == "concluida":
        return _avancar(
            escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id,
            transporte=transporte, cliente_gemini=cliente_gemini, controle=controle,
        )
    if execucao.estado == "incerta" or (
        execucao.efeito_externo == "incerto" and execucao.estado != "adquirida"
    ):
        return _estado("incerta", _fase_da_analise(analise), False)
    if execucao.estado == "adquirida":
        if execucao.lease_ate is not None and execucao.lease_ate > utcnow_naive():
            return _estado("em_andamento", _fase_da_analise(analise), True)
        return _estado("incerta", _fase_da_analise(analise), False)
    if execucao.estado == "falha_definitiva":
        return _estado("recusada", None, False)
    if _tentativa_pede_reconciliacao(execucao):
        return _estado("incerta", _fase_da_analise(analise), False)
    if execucao.estado not in _FILA_LIVRE or execucao.efeito_externo != "nenhum":
        return _estado("em_andamento", _fase_da_analise(analise), True)
    return _rodar(
        escopo,
        dataset,
        pedido,
        perfil,
        contexto,
        payload,
        colaboracao_id,
        analise,
        transporte=transporte,
        cliente_gemini=cliente_gemini,
        controle=controle,
    )


def _abrir(escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id, *, pai, acao, papel, transporte, cliente_gemini, controle):
    if _bloqueio_financeiro(_tecnicas(escopo, dataset, colaboracao_id)):
        return _estado("incerta", None, False)
    pai_id = None if pai is None else int(pai.id)
    pai_hash = None if pai is None else _json(pai.resultado_json).get("hash")
    chave = identidade_rodada(colaboracao_id, pai_id, acao, papel)
    analise = (
        AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            chave_recuperacao=chave,
        ).one_or_none()
    )
    if analise is None:
        analise = _inserir_rodada(escopo, dataset, pedido, contexto, colaboracao_id, pai_id, pai_hash, acao, papel, chave)
    return _retomar(
        escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id, analise,
        transporte=transporte, cliente_gemini=cliente_gemini, controle=controle,
    )


def _inserir_rodada(escopo, dataset, pedido, contexto, colaboracao_id, pai_id, pai_hash, acao, papel, chave):
    gravacao = {
        "chave": dataset.chave,
        "rodada": _proxima_rodada(escopo, dataset.chave),
        "dataset_id": int(dataset.id),
        "versao": int(dataset.versao),
        "impressao": dataset.impressao,
        "etapa": "descoberta",
        "estado": "preparada",
        "contexto": {
            "contrato": CONTRATO_CONTEXTO_TECNICO,
            "colaboracao_id": colaboracao_id,
            "pai_id": pai_id,
            "pai_hash": pai_hash,
            "papel": papel,
            "acao": acao,
            "vinculo_preparacao": contexto["vinculo_preparacao"],
            "dataset_id": int(dataset.id),
            "dataset_versao": int(dataset.versao),
            "dataset_impressao": dataset.impressao,
        },
        "regras_negocio": {"catalogo": CATALOGO_CONTRATO, "pedido": pedido},
        "esclarecimentos": {},
        "contrato_versao": CONTRATO_TECNICO,
        "chave_recuperacao": chave,
    }
    try:
        criada = criar_analise(escopo, gravacao)
    except AgenteBiErro as exc:
        if exc.codigo not in {"rodada_existente", "chave_recuperacao_existente", "conflito_versao"}:
            raise ColaboracaoErro(_codigo(exc.codigo), _status(exc.codigo)) from exc
        if exc.codigo == "rodada_existente":
            gravacao["rodada"] = _proxima_rodada(escopo, dataset.chave)
            try:
                criada = criar_analise(escopo, gravacao)
            except AgenteBiErro as repetida:
                if repetida.codigo not in {"chave_recuperacao_existente", "conflito_versao"}:
                    raise ColaboracaoErro(_codigo(repetida.codigo), _status(repetida.codigo)) from repetida
                criada = None
        else:
            criada = None
    if criada is None:
        existente = AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            chave_recuperacao=chave,
        ).one()
        return existente
    return db.session.get(AgenteBiAnalise, int(criada["id"]))


def _rodar(escopo, dataset, pedido, perfil, contexto, payload, colaboracao_id, analise, *, transporte, cliente_gemini, controle):
    worker = f"colab-{uuid4().hex[:20]}"
    execucao = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one()
    contexto_analise = _json(analise.contexto_json)
    pai = None if contexto_analise.get("pai_id") is None else db.session.get(AgenteBiAnalise, int(contexto_analise["pai_id"]))
    documento_pai = None if pai is None or not pai.resultado_json else _json(pai.resultado_json)

    def processador(referencias):
        if int(referencias.get("execucao_id") or 0) != int(execucao.id):
            raise ExecucaoErro("identidade_inconsistente")
        linha = db.session.get(AgenteBiExecucao, int(execucao.id))
        db.session.refresh(linha)
        if controle.get("antes_da_chamada"):
            controle["antes_da_chamada"]()
        if contexto_analise["papel"] == _PAPEL_CLAUDE and contexto_analise.get("acao") == _ACAO_REVISAR:
            try:
                proposta_anterior, critica, verificacao_anterior = _proposta_revisavel(
                    analise, contexto_analise, referencias, contexto
                )
            except ExecucaoErro as exc:
                _recusar(linha, exc.codigo)
                raise
            mensagem = _canonico(
                {
                    "contexto": payload,
                    "proposta_anterior": proposta_anterior,
                    "critica_autorizada": critica,
                    "verificacao_anterior": verificacao_anterior,
                }
            )
        else:
            mensagem = _mensagem(payload, contexto_analise, documento_pai)
            if (
                contexto_analise["papel"] == _PAPEL_CLAUDE
                and isinstance(documento_pai, dict)
                and isinstance(documento_pai.get("critica"), dict)
                and minimizar_critica(documento_pai["critica"], contexto) != documento_pai["critica"]
            ):
                _recusar(linha, "contexto_nao_autorizado")
                raise ExecucaoErro("contexto_nao_autorizado")
        if contexto_analise["papel"] == _PAPEL_GEMINI:
            resultado = chamar_gemini_governado(
                usuario_id=escopo.usuario_id,
                conta_id=escopo.conta_id,
                franquia_id=escopo.franquia_id,
                mensagem=mensagem,
                chave_idempotencia=linha.chave_idempotencia,
                execucao_id=int(linha.id),
                worker_id=linha.lease_worker,
                fencing=int(linha.fencing),
                cliente=cliente_gemini,
                config=_config_critica(),
            )
        else:
            system = instrucao_de_rascunho()
            if contexto_analise.get("acao") == _ACAO_REVISAR:
                system = system + "\n" + instrucao_de_revisao()
            resultado = chamar_anthropic_governado(
                usuario_id=escopo.usuario_id,
                conta_id=escopo.conta_id,
                franquia_id=escopo.franquia_id,
                mensagem=mensagem,
                chave_idempotencia=linha.chave_idempotencia,
                execucao_id=int(linha.id),
                worker_id=linha.lease_worker,
                fencing=int(linha.fencing),
                transporte=transporte,
                system=system,
                output_config=preparar_saida_estruturada(
                    (os.getenv("AGENTE_BI_ANTHROPIC_MODEL") or "").strip(),
                    esquema_rascunho_analisar(),
                ),
            )
        db.session.refresh(linha)
        if resultado.get("incerto"):
            raise ExecucaoErro("efeito_incerto")
        if not resultado.get("ok"):
            codigo = _codigo(str(resultado.get("codigo") or ""))
            if not _pre_chamada_sem_recusa(linha, resultado, codigo):
                _recusar(linha, codigo)
            raise ExecucaoErro(codigo)
        if controle.get("depois_da_resposta"):
            controle["depois_da_resposta"]()
        try:
            tecnico = _envelope(analise, contexto_analise, resultado, perfil, pedido, contexto)
        except ExecucaoErro as exc:
            if exc.codigo in {"efeito_incerto", "identidade_inconsistente"}:
                raise
            ident = {
                "usuario_id": int(linha.usuario_id),
                "conta_id": int(linha.conta_id),
                "franquia_id": int(linha.franquia_id),
            }
            _liberar_efeito(int(linha.id), ident, linha.lease_worker, int(linha.fencing))
            db.session.refresh(linha)
            _recusar(linha, exc.codigo)
            raise
        if tecnico is None:
            ident = {
                "usuario_id": int(linha.usuario_id),
                "conta_id": int(linha.conta_id),
                "franquia_id": int(linha.franquia_id),
            }
            _liberar_efeito(int(linha.id), ident, linha.lease_worker, int(linha.fencing))
            db.session.refresh(linha)
            _recusar(linha, "contrato_saida_invalido")
            raise ExecucaoErro("contrato_saida_invalido")
        return EntregaProcessador(_CODIGO_CONFIRMACAO, tecnico)

    ciclo = executar_um_ciclo(
        worker_id=worker,
        processador=processador,
        execucao_id=int(execucao.id),
        lease_segundos=LEASE_MAX_SEGUNDOS,
    )
    atual = db.session.get(AgenteBiExecucao, int(execucao.id))
    db.session.refresh(atual)
    db.session.refresh(analise)
    _observar(analise, atual)
    if atual.estado == "concluida" and analise.resultado_json:
        documento = _json(analise.resultado_json)
        if documento.get("acao") == _ACAO_REVISAR and _aprovado(documento):
            return _materializar(escopo, dataset, pedido, perfil, colaboracao_id, analise, documento, controle)
        tecnicas = _tecnicas(escopo, dataset, colaboracao_id)
        if documento.get("acao") == _ACAO_REVISAR and not _progresso(contexto, tecnicas):
            return _preservar(escopo, dataset, pedido, perfil, colaboracao_id, tecnicas, controle)
        return _estado("em_andamento", _fase_seguinte(documento), True)
    if atual.estado == "incerta" or atual.efeito_externo == "incerto":
        return _estado("incerta", _fase_da_analise(analise), False)
    if atual.estado in {"falha_definitiva", "falha_recuperavel"} or ciclo.get("adquirida") is False:
        if atual.estado == "falha_recuperavel":
            return _estado("em_andamento", _fase_da_analise(analise), True)
        if atual.estado == "falha_definitiva":
            return _estado("recusada", None, False)
        return _estado("em_andamento", _fase_da_analise(analise), True)
    return _estado("em_andamento", _fase_da_analise(analise), True)


def _materializar(escopo, dataset, pedido, perfil, colaboracao_id, revisao, documento, controle):
    if controle.get("antes_de_materializar"):
        controle["antes_de_materializar"]()
    chave = identidade_rodada(colaboracao_id, int(revisao.id), _ACAO_MATERIALIZAR, _PAPEL_SERVIDOR)
    existente = AgenteBiAnalise.query.filter_by(
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        chave_recuperacao=chave,
    ).one_or_none()
    if existente is not None:
        if existente.contrato_versao != CONTRATO_ANALISAR or existente.estado != "concluida":
            raise ColaboracaoErro("identidade_inconsistente", 409)
        return _pronto(escopo, existente)
    proposta = documento.get("proposta")
    try:
        saida = analisar(perfil, pedido, provedor=lambda _contexto: proposta, proveniencia=PROVEDOR_ANTHROPIC)
    except AnalisarErro:
        return _estado("encerrada", None, False)
    if saida.get("contrato") != CONTRATO_ANALISAR or saida.get("contrato_saida_validado") is not True:
        return _estado("encerrada", None, False)
    try:
        criada = criar_analise(
            escopo,
            {
                "chave": dataset.chave,
                "rodada": _proxima_rodada(escopo, dataset.chave),
                "dataset_id": int(dataset.id),
                "versao": int(dataset.versao),
                "impressao": dataset.impressao,
                "etapa": "descoberta",
                "estado": "concluida",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_ANALISAR,
                    "vinculo_preparacao": saida["vinculo_preparacao"],
                    "dataset_id": int(dataset.id),
                    "colaboracao_id": colaboracao_id,
                    "pai_id": int(revisao.id),
                    "pai_hash": documento.get("hash"),
                },
                "regras_negocio": {"catalogo": CATALOGO_CONTRATO, "pedido": pedido},
                "esclarecimentos": {},
                "contrato_versao": CONTRATO_ANALISAR,
                "resultado": saida,
                "chave_recuperacao": chave,
            },
        )
    except AgenteBiErro as exc:
        if exc.codigo == "chave_recuperacao_existente":
            existente = AgenteBiAnalise.query.filter_by(
                usuario_id=escopo.usuario_id,
                chave_recuperacao=chave,
            ).one()
            return _pronto(escopo, existente)
        if exc.codigo == "rodada_existente":
            return _materializar(escopo, dataset, pedido, perfil, colaboracao_id, revisao, documento, {})
        raise ColaboracaoErro(_codigo(exc.codigo), _status(exc.codigo)) from exc
    linha = db.session.get(AgenteBiAnalise, int(criada["id"]))
    logger.info(
        "evento=agente_bi_colaboracao_materializada analise_id=%s pai_id=%s usuario_id=%s",
        linha.id,
        revisao.id,
        escopo.usuario_id,
    )
    return _pronto(escopo, linha)


def _preservar(escopo, dataset, pedido, perfil, colaboracao_id, tecnicas, controle):
    aprovada = None
    for item in tecnicas:
        if not item.resultado_json:
            continue
        documento = _json(item.resultado_json)
        if item.estado == "concluida" and _aprovado(documento):
            aprovada = (item, documento)
    if aprovada is None:
        return _estado("encerrada", None, False)
    return _materializar(escopo, dataset, pedido, perfil, colaboracao_id, aprovada[0], aprovada[1], controle)


_BLOQUEIO_DURO = frozenset(
    {
        "autorizacao_indevida",
        "contexto_nao_autorizado",
        "provedor_nao_autorizado",
        "provedor_indisponivel",
    }
)


def _envelope(analise, contexto_analise, resultado, perfil, pedido, contexto):
    texto = resultado.get("texto")
    papel = contexto_analise["papel"]
    proposta = None
    critica = None
    verificacao = {"aprovado": False, "codigo": "contrato_saida_invalido"}
    descartes = []
    if papel == _PAPEL_CLAUDE:
        bruto, formato = _objeto_da_resposta(texto)
        if bruto is None:
            _diagnosticar(analise, "json_nao_interpretavel")
            return None
        proposta, descartes = _representar_proposta(bruto)
        if proposta is None or descartes is None:
            _diagnosticar(analise, "estrutura_nao_representavel")
            return None
        try:
            analisar(perfil, pedido, provedor=lambda _contexto: proposta, proveniencia=PROVEDOR_ANTHROPIC)
        except AnalisarErro as exc:
            if exc.codigo in _BLOQUEIO_DURO:
                raise ExecucaoErro(exc.codigo)
            if exc.codigo not in _CHECKPOINT and not _vazia_recuperavel(proposta, descartes):
                _diagnosticar(analise, "verificacao_nao_recuperavel")
                return None
            codigo = exc.codigo if exc.codigo in _CHECKPOINT else _codigo_de_descartes(descartes)
            verificacao = {"aprovado": False, "codigo": codigo}
        else:
            verificacao = {"aprovado": True, "codigo": None}
        if descartes:
            verificacao = {"aprovado": False, "codigo": verificacao["codigo"] or _codigo_de_descartes(descartes)}
        if formato:
            logger.info(
                "evento=agente_bi_colaboracao_formato_recuperado analise_id=%s",
                getattr(analise, "id", None),
            )
    else:
        try:
            bruto = json.loads(texto) if isinstance(texto, str) else None
        except (TypeError, json.JSONDecodeError):
            bruto = None
        critica = minimizar_critica(bruto, contexto)
        if critica is None:
            _diagnosticar(analise, "critica_nao_representavel")
            return None
        codigo_critica = critica["objecoes"][0]["codigo"] if critica["objecoes"] else "evidencia_ausente"
        verificacao = {"aprovado": False, "codigo": codigo_critica}
    db.session.refresh(analise)
    execucao = AgenteBiExecucao.query.filter_by(analise_id=analise.id).one()
    db.session.refresh(execucao)
    tentativa_id = execucao.ia_chamada_tentativa_id or resultado.get("tentativa_id")
    if isinstance(tentativa_id, bool) or not isinstance(tentativa_id, int):
        raise ExecucaoErro("identidade_inconsistente")
    if not chamada_liquidada(int(execucao.id)):
        raise ExecucaoErro("efeito_incerto")
    documento = {
        "contrato": CONTRATO_TECNICO,
        "colaboracao_id": contexto_analise["colaboracao_id"],
        "pai_id": contexto_analise["pai_id"],
        "pai_hash": contexto_analise["pai_hash"],
        "papel": papel,
        "acao": contexto_analise["acao"],
        "referencias": _citar(proposta, critica),
        "proposta": proposta,
        "critica": critica,
        "verificacao": verificacao,
        "tentativa_id": int(tentativa_id),
    }
    if descartes:
        documento["descartes"] = descartes
    documento["hash"] = _hash(documento)
    return documento


def _diagnosticar(analise, motivo: str) -> None:
    logger.info(
        "evento=agente_bi_colaboracao_irrecuperavel motivo=%s analise_id=%s",
        motivo,
        getattr(analise, "id", None),
    )


def _objeto_da_resposta(texto):
    """Lê o JSON da resposta. Cerca de markdown não cria campo analítico."""
    if not isinstance(texto, str) or not texto.strip():
        return None, False
    candidato = texto.strip()
    recuperado = False
    if candidato.startswith("```"):
        linhas = candidato.splitlines()
        if linhas and linhas[0].startswith("```"):
            linhas = linhas[1:]
        if linhas and linhas[-1].strip().startswith("```"):
            linhas = linhas[:-1]
        candidato = "\n".join(linhas).strip()
        recuperado = True
    bruto = _carregar_json(candidato)
    if not isinstance(bruto, dict):
        inicio = candidato.find("{")
        fim = candidato.rfind("}")
        if inicio < 0 or fim <= inicio:
            return None, False
        bruto = _carregar_json(candidato[inicio : fim + 1])
        recuperado = True
    if not isinstance(bruto, dict):
        return None, False
    return bruto, recuperado or candidato != texto.strip()


def _carregar_json(texto):
    try:
        return json.loads(texto)
    except (TypeError, json.JSONDecodeError):
        return None


def _representar_proposta(valor):
    """Devolve a proposta segura e os descartes classificáveis.

    Forma superior irrecuperável, item que não é objeto ou campo
    desconhecido não produz aprovação. Lista vazia com diagnóstico
    continua representável.
    """
    if not isinstance(valor, dict) or set(valor) != {"descobertas", "sugestoes"}:
        return None, None
    descobertas_brutas = valor.get("descobertas")
    sugestoes_brutas = valor.get("sugestoes")
    if not isinstance(descobertas_brutas, list) or not isinstance(sugestoes_brutas, list):
        return None, None
    if len(descobertas_brutas) > _TETO_BRUTO["descobertas"] or len(sugestoes_brutas) > _TETO_BRUTO["sugestoes"]:
        return None, None
    estrita = minimizar_proposta(valor)
    if estrita is not None:
        return estrita, []
    descobertas = []
    sugestoes = []
    descartes = []
    if not _separar("descobertas", descobertas_brutas, _descoberta, _falha_descoberta, descobertas, descartes):
        return None, None
    if not _separar("sugestoes", sugestoes_brutas, _sugestao, _falha_sugestao, sugestoes, descartes):
        return None, None
    if not descartes:
        return None, None
    proposta = {"descobertas": descobertas, "sugestoes": sugestoes}
    if minimizar_proposta(proposta) != proposta or _minimizar_descartes(descartes) != descartes:
        return None, None
    return proposta, descartes


def _separar(lista, brutos, conversor, classificar, destino, descartes) -> bool:
    teto = _TETO_LISTA[lista]
    for posicao, item in enumerate(brutos):
        if not isinstance(item, dict):
            return False
        if posicao < teto:
            convertido = conversor(item)
            if convertido is not None:
                destino.append(convertido)
                continue
            falha = classificar(item)
        else:
            falha = classificar(item)
            if falha is None and conversor(item) is not None:
                falha = ("limite", _MOTIVO_CATALOGO)
        if falha is None:
            return False
        diagnostico = _diagnostico(lista, posicao, falha[0], falha[1])
        if diagnostico is None:
            return False
        descartes.append(diagnostico)
    return True


def _falha_sugestao(item):
    if not isinstance(item, dict) or set(item) - _CHAVES_SUGESTAO_RASCUNHO:
        return None
    if "juncao" in item and item.get("juncao") != _JUNCAO:
        return ("juncao", "juncao_nao_confirmada")
    if "grafico" in item and item.get("grafico") is not None and item.get("grafico") not in GRAFICOS:
        return ("grafico", "grafico_incompativel")
    if "operacao" in item and item.get("operacao") is not None and item.get("operacao") not in OPERACOES:
        return ("operacao", "metrica_inventada")
    if "evidencias" in item:
        evidencias = item.get("evidencias")
        if not isinstance(evidencias, list) or len(evidencias) > 8:
            return ("evidencias", _MOTIVO_CATALOGO)
        if any(codigo not in _EVIDENCIAS for codigo in evidencias):
            return ("evidencias", "evidencia_ausente")
    for chave in ("medida", "agrupamento", "segunda_medida"):
        if chave not in item or item.get(chave) is None:
            continue
        if _coluna(item.get(chave)) is None:
            return (chave, "referencia_inexistente")
    if "regiao" in item and item.get("regiao") is not None and _regiao(item.get("regiao")) is None:
        return ("regiao", "referencia_inexistente")
    if "status" in item and item.get("status") not in _STATUS:
        return ("status", _MOTIVO_CATALOGO)
    if "leitura" in item and item.get("leitura") not in _LEITURAS:
        return ("leitura", _MOTIVO_CATALOGO)
    if "origem" in item and item.get("origem") not in _ORIGENS:
        return ("origem", _MOTIVO_CATALOGO)
    if "lacuna" in item and item.get("lacuna") is not None and item.get("lacuna") not in _LACUNAS:
        return ("lacuna", _MOTIVO_CATALOGO)
    if "duvidas" in item:
        duvidas = item.get("duvidas")
        if not isinstance(duvidas, list) or len(duvidas) > 8:
            return ("duvidas", _MOTIVO_CATALOGO)
        if any(codigo not in _PERGUNTAS for codigo in duvidas):
            return ("duvidas", _MOTIVO_CATALOGO)
    if set(item) != _CHAVES_SUGESTAO_RASCUNHO:
        return ("item", _MOTIVO_CATALOGO)
    return None


def _falha_descoberta(item):
    if not isinstance(item, dict) or set(item) - _CHAVES_DESCOBERTA_RASCUNHO:
        return None
    if "codigo" in item and item.get("codigo") not in _DESCOBERTAS:
        return ("codigo", _MOTIVO_CATALOGO)
    if "regiao" in item and item.get("regiao") is not None and _regiao(item.get("regiao")) is None:
        return ("regiao", "referencia_inexistente")
    if "relacao" in item and item.get("relacao") is not None:
        relacao = item.get("relacao")
        if isinstance(relacao, bool) or not isinstance(relacao, int) or relacao < 0:
            return ("relacao", "referencia_inexistente")
    if set(item) != _CHAVES_DESCOBERTA_RASCUNHO:
        return ("item", _MOTIVO_CATALOGO)
    return None


def _diagnostico(lista, posicao, campo, motivo):
    if lista not in _LISTAS_DESCARTE or campo not in _CAMPOS_DESCARTE[lista] or motivo not in _MOTIVOS_DESCARTE:
        return None
    if isinstance(posicao, bool) or not isinstance(posicao, int) or not 0 <= posicao <= _POSICAO_MAXIMA:
        return None
    if campo == "limite" and (motivo != _MOTIVO_CATALOGO or posicao < _TETO_LISTA[lista]):
        return None
    return {"lista": lista, "posicao": int(posicao), "campo": campo, "motivo": motivo}


def _minimizar_descartes(valor):
    if not isinstance(valor, list) or not valor or len(valor) > _TETO_DESCARTES:
        return None
    saida = []
    for item in valor:
        if not isinstance(item, dict) or set(item) != _CHAVES_DESCARTE:
            return None
        diagnostico = _diagnostico(item.get("lista"), item.get("posicao"), item.get("campo"), item.get("motivo"))
        if diagnostico is None:
            return None
        saida.append(diagnostico)
    return saida


def _codigo_de_descartes(descartes) -> str | None:
    for item in descartes:
        if item.get("motivo") in _CHECKPOINT:
            return item["motivo"]
    return None


def _vazia_recuperavel(proposta, descartes) -> bool:
    return (
        isinstance(proposta, dict)
        and proposta.get("descobertas") == []
        and proposta.get("sugestoes") == []
        and bool(descartes)
    )


def _chaves_envelope(documento: dict) -> frozenset:
    if isinstance(documento, dict) and "descartes" in documento:
        return _CHAVES_TECNICAS | {"descartes"}
    return _CHAVES_TECNICAS


def _corpo_assinado(documento: dict) -> dict:
    return {
        chave: documento[chave]
        for chave in _chaves_envelope(documento)
        if chave != "hash" and chave in documento
    }


def _mensagem(payload, contexto_analise, documento_pai) -> str:
    corpo = dict(payload)
    if not isinstance(documento_pai, dict):
        return _canonico(corpo)
    if contexto_analise["papel"] == _PAPEL_GEMINI and isinstance(documento_pai.get("proposta"), dict):
        corpo = {
            "contexto": payload,
            "proposta": documento_pai["proposta"],
            "verificacao": documento_pai.get("verificacao"),
        }
        if "descartes" in documento_pai:
            seguro = _minimizar_descartes(documento_pai.get("descartes"))
            if seguro != documento_pai.get("descartes"):
                raise ExecucaoErro("contexto_nao_autorizado")
            corpo["descartes"] = seguro
    elif contexto_analise["papel"] == _PAPEL_CLAUDE and isinstance(documento_pai.get("critica"), dict):
        corpo = {
            "contexto": payload,
            "proposta_anterior": documento_pai.get("proposta"),
            "critica_autorizada": documento_pai["critica"],
        }
    return _canonico(corpo)


def _progresso(contexto, tecnicas) -> bool:
    propostas = []
    for item in tecnicas:
        if item.estado != "concluida" or not item.resultado_json:
            continue
        documento = _json(item.resultado_json)
        if documento.get("papel") != _PAPEL_CLAUDE or not isinstance(documento.get("proposta"), dict):
            continue
        propostas.append(documento)
    if len(propostas) < 2:
        return True
    atual = propostas[-1]
    if _aprovado(atual):
        return True
    anteriores = propostas[:-1]
    descartes = atual.get("descartes") or []
    if descartes and all((item.get("descartes") or []) != descartes for item in anteriores):
        return True
    assinatura = _assinatura(atual["proposta"])
    if any(_assinatura(item["proposta"]) == assinatura for item in anteriores):
        return False
    if len(propostas) >= 3 and _assinatura(propostas[-3]["proposta"]) == assinatura:
        return False
    codigo = (atual.get("verificacao") or {}).get("codigo")
    if codigo not in {((item.get("verificacao") or {}).get("codigo")) for item in anteriores}:
        return True
    agora = _nao_autorizadas(contexto, atual["proposta"])
    antes = _nao_autorizadas(contexto, anteriores[-1]["proposta"])
    return agora < antes


def _seguinte(documento):
    acao = documento.get("acao")
    if acao == _ACAO_PROPOR:
        return _ACAO_CRITICAR, _PAPEL_GEMINI
    if acao == _ACAO_CRITICAR:
        return _ACAO_REVISAR, _PAPEL_CLAUDE
    if acao == _ACAO_REVISAR:
        return _ACAO_CRITICAR, _PAPEL_GEMINI
    return None, None


def _fase_seguinte(documento) -> str:
    acao, _papel = _seguinte(documento)
    return _fase_da_acao(acao)


def _fase_da_acao(acao) -> str:
    if acao == _ACAO_CRITICAR:
        return _FASE_GEMINI
    if acao == _ACAO_REVISAR:
        return _FASE_REVISAO
    return _FASE_CLAUDE


def _fase_da_analise(analise) -> str:
    contexto = _json(analise.contexto_json)
    return _fase_da_acao(contexto.get("acao") if isinstance(contexto, dict) else None)


def _pronto(escopo, analise: AgenteBiAnalise) -> dict:
    from app.services.agente_bi_descoberta_service import _publicar_descoberta

    corpo = _publicar_descoberta(analise_para_dict(analise), _json(analise.resultado_json))
    corpo["colaboracao"] = _colaboracao("concluida", _FASE_PRONTO, False)
    return corpo


def _estado(estado: str, fase: str | None, continuar: bool) -> dict:
    return {"ok": True, "colaboracao": _colaboracao(estado, fase, continuar)}


def _colaboracao(estado: str, fase: str | None, continuar: bool) -> dict:
    return {"estado": estado, "fase": fase, "continuar": continuar}


def _tecnicas(escopo, dataset, colaboracao_id) -> list[AgenteBiAnalise]:
    linhas = (
        AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            dataset_id=int(dataset.id),
            dataset_versao=int(dataset.versao),
            dataset_impressao=dataset.impressao,
            contrato_versao=CONTRATO_TECNICO,
        )
        .order_by(AgenteBiAnalise.id.asc())
        .all()
    )
    saida = []
    for linha in linhas:
        contexto = _json(linha.contexto_json)
        if isinstance(contexto, dict) and contexto.get("colaboracao_id") == colaboracao_id:
            saida.append(linha)
    return saida


def _materializada(escopo, dataset, colaboracao_id, tecnicas):
    for item in reversed(tecnicas):
        if item.estado != "concluida" or not item.resultado_json:
            continue
        documento = _json(item.resultado_json)
        if not _aprovado(documento) or documento.get("acao") not in {_ACAO_PROPOR, _ACAO_REVISAR}:
            continue
        chave = identidade_rodada(colaboracao_id, int(item.id), _ACAO_MATERIALIZAR, _PAPEL_SERVIDOR)
        achada = AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            chave_recuperacao=chave,
            contrato_versao=CONTRATO_ANALISAR,
        ).one_or_none()
        if achada is not None and achada.dataset_id == dataset.id:
            return achada
    return None


def _bloqueio_financeiro(tecnicas) -> bool:
    for item in tecnicas:
        execucao = AgenteBiExecucao.query.filter_by(analise_id=item.id).one_or_none()
        if execucao is None:
            continue
        if execucao.estado == "incerta":
            return True
        if execucao.efeito_externo == "incerto" and execucao.estado != "adquirida":
            return True
        if _tentativa_pede_reconciliacao(execucao):
            return True
    return False


def _tentativa_pede_reconciliacao(execucao: AgenteBiExecucao) -> bool:
    if execucao.ia_chamada_tentativa_id is None:
        return False
    tentativa = db.session.get(IaChamadaTentativa, int(execucao.ia_chamada_tentativa_id))
    if tentativa is None:
        return False
    if tentativa.status in {IaChamadaTentativa.STATUS_UNCERTAIN, IaChamadaTentativa.STATUS_CALLING}:
        return True
    if tentativa.reserva_ativa and tentativa.status != IaChamadaTentativa.STATUS_SETTLED:
        return True
    return False


def _recusar(linha: AgenteBiExecucao, codigo: str) -> None:
    if linha.estado in {"falha_definitiva", "incerta", "concluida"}:
        return
    falhar(
        int(linha.id),
        worker_id=linha.lease_worker,
        fencing=int(linha.fencing),
        codigo=codigo,
        recuperavel=False,
    )


def _pre_chamada_sem_recusa(linha: AgenteBiExecucao, resultado: dict, codigo: str) -> bool:
    """Deixa o executor classificar a falha que ocorreu antes de qualquer chamada.

    As demais recusas continuam definitivas por ``_recusar``.
    """
    return (
        resultado.get("provedor_chamado") is False
        and resultado.get("tentativa_id") is None
        and codigo in _ERROS_PRE_CHAMADA
        and linha.ia_chamada_tentativa_id is None
        and linha.efeito_externo == "nenhum"
    )


def recuperar_falha_pre_chamada(escopo, execucao_id: int) -> dict:
    """Reabre só ``falha_definitiva`` de pré-chamada para ``falha_recuperavel``.

    Não entra na continuação da colaboração, não chama provedor e não altera
    contador, fencing, identidade ou consumo. A segunda chamada devolve o
    mesmo registro.
    """
    if not isinstance(escopo, EscopoAgenteBi):
        raise ExecucaoErro("identidade_inconsistente")
    if isinstance(execucao_id, bool) or not isinstance(execucao_id, int) or execucao_id < 1:
        raise ExecucaoErro("nao_encontrado")
    try:
        linha = _bloquear_execucao(int(execucao_id))
        if not _execucao_no_escopo(linha, escopo):
            db.session.rollback()
            raise ExecucaoErro("nao_encontrado")
        analise = _bloquear_analise(linha)
        if _recuperacao_ja_aplicada(linha, analise, escopo):
            foto = execucao_para_dict(linha)
            db.session.rollback()
            return foto
        motivo = _motivo_recusa_pre_chamada(linha, analise, escopo)
        if motivo is not None:
            db.session.rollback()
            raise ExecucaoErro(motivo)
        return _aplicar_recuperacao_pre_chamada(linha, analise)
    except ExecucaoErro:
        db.session.rollback()
        raise
    except Exception:
        db.session.rollback()
        raise


def _aplicar_recuperacao_pre_chamada(linha: AgenteBiExecucao, analise: AgenteBiAnalise) -> dict:
    momento = utcnow_naive()
    execucao_id = int(linha.id)
    erro = linha.ultimo_erro
    if not _atualizar_condicional(
        [
            AgenteBiExecucao.id == execucao_id,
            AgenteBiExecucao.estado == "falha_definitiva",
            AgenteBiExecucao.ultimo_erro == erro,
            AgenteBiExecucao.efeito_externo == "nenhum",
            AgenteBiExecucao.ia_chamada_tentativa_id.is_(None),
            AgenteBiExecucao.tentativas == int(linha.tentativas),
            AgenteBiExecucao.recuperacoes == int(linha.recuperacoes),
            AgenteBiExecucao.fencing == int(linha.fencing),
            AgenteBiExecucao.lease_worker.is_(None),
            AgenteBiExecucao.lease_ate.is_(None),
            AgenteBiExecucao.usuario_id == int(linha.usuario_id),
            AgenteBiExecucao.conta_id == int(linha.conta_id),
            AgenteBiExecucao.franquia_id == int(linha.franquia_id),
            AgenteBiExecucao.analise_id == int(analise.id),
            AgenteBiExecucao.chave_idempotencia == linha.chave_idempotencia,
            AgenteBiExecucao.tentativas < MAX_TENTATIVAS,
        ],
        {
            "estado": "falha_recuperavel",
            "lease_worker": None,
            "lease_ate": None,
            "updated_at": momento,
        },
    ):
        db.session.rollback()
        raise ExecucaoErro("estado_invalido")
    _publicar_transicao(
        execucao_id,
        origem="falha_definitiva",
        destino="falha_recuperavel",
        codigo=_CODIGO_RECUPERACAO_PRE_CHAMADA,
        agora=momento,
    )
    logger.info(
        "evento=agente_bi_recuperacao_pre_chamada execucao_id=%s analise_id=%s codigo=%s",
        execucao_id,
        analise.id,
        erro,
    )
    return execucao_para_dict(db.session.get(AgenteBiExecucao, execucao_id))


def _bloquear_execucao(execucao_id: int) -> AgenteBiExecucao | None:
    consulta = AgenteBiExecucao.query.filter(AgenteBiExecucao.id == int(execucao_id))
    if db.session.get_bind().dialect.name == "postgresql":
        consulta = consulta.with_for_update()
    return consulta.execution_options(populate_existing=True).one_or_none()


def _bloquear_analise(linha: AgenteBiExecucao) -> AgenteBiAnalise | None:
    consulta = AgenteBiAnalise.query.filter_by(
        id=linha.analise_id,
        usuario_id=linha.usuario_id,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
    )
    if db.session.get_bind().dialect.name == "postgresql":
        consulta = consulta.with_for_update()
    return consulta.execution_options(populate_existing=True).one_or_none()


def _execucao_no_escopo(linha: AgenteBiExecucao | None, escopo: EscopoAgenteBi) -> bool:
    return (
        linha is not None
        and linha.usuario_id == escopo.usuario_id
        and linha.conta_id == escopo.conta_id
        and linha.franquia_id == escopo.franquia_id
    )


def _recuperacao_ja_aplicada(linha: AgenteBiExecucao, analise: AgenteBiAnalise | None, escopo: EscopoAgenteBi) -> bool:
    if linha.estado != "falha_recuperavel" or linha.ultimo_erro not in _ERROS_PRE_CHAMADA:
        return False
    if not _invariantes_pre_chamada(linha, analise, escopo):
        return False
    if analise is None or analise.estado != "em_execucao":
        return False
    return (
        AgenteBiExecucaoEvento.query.filter_by(
            execucao_id=linha.id,
            estado_anterior="falha_definitiva",
            estado_novo="falha_recuperavel",
            codigo=_CODIGO_RECUPERACAO_PRE_CHAMADA,
            fencing=int(linha.fencing),
        ).count()
        >= 1
    )


def _motivo_recusa_pre_chamada(linha: AgenteBiExecucao, analise: AgenteBiAnalise | None, escopo: EscopoAgenteBi) -> str | None:
    if linha.estado != "falha_definitiva":
        return "estado_invalido"
    if linha.ultimo_erro not in _ERROS_PRE_CHAMADA:
        return "erro_nao_recuperavel"
    if linha.efeito_externo != "nenhum":
        return "efeito_nao_recuperavel"
    if linha.lease_worker is not None or linha.lease_ate is not None:
        return "lease_existente"
    if int(linha.tentativas) >= MAX_TENTATIVAS:
        return "tentativas_esgotadas"
    if not _invariantes_pre_chamada(linha, analise, escopo):
        if analise is not None and analise.resultado_json is not None:
            return "historico_preservado"
        tentativa = _tentativa_da_chave(linha.chave_idempotencia)
        if _reserva_correspondente(linha, tentativa):
            return "reserva_existente"
        if linha.ia_chamada_tentativa_id is not None or tentativa is not None:
            return "tentativa_existente"
        return "identidade_inconsistente"
    if analise.estado != "falha_validacao":
        return "estado_invalido"
    if not _configuracao_operacional_valida(_json(analise.contexto_json).get("papel"), linha.ultimo_erro):
        return "configuracao_invalida"
    return None


def _invariantes_pre_chamada(linha: AgenteBiExecucao, analise: AgenteBiAnalise | None, escopo: EscopoAgenteBi) -> bool:
    if linha.efeito_externo != "nenhum" or linha.ia_chamada_tentativa_id is not None:
        return False
    if linha.lease_worker is not None or linha.lease_ate is not None:
        return False
    if int(linha.tentativas) >= MAX_TENTATIVAS:
        return False
    if not _execucao_no_escopo(linha, escopo):
        return False
    tentativa = _tentativa_da_chave(linha.chave_idempotencia)
    if tentativa is not None or _reserva_correspondente(linha, tentativa):
        return False
    if not _vinculos_da_analise(linha, analise):
        return False
    return analise.resultado_json is None and analise.estado in {"falha_validacao", "em_execucao"}


def _vinculos_da_analise(linha: AgenteBiExecucao, analise: AgenteBiAnalise | None) -> bool:
    if analise is None or int(analise.id) != int(linha.analise_id):
        return False
    if (
        analise.usuario_id != linha.usuario_id
        or analise.conta_id != linha.conta_id
        or analise.franquia_id != linha.franquia_id
        or analise.chave_recuperacao != linha.chave_idempotencia
    ):
        return False
    dataset = AgenteBiDataset.query.filter_by(
        id=analise.dataset_id,
        usuario_id=analise.usuario_id,
        conta_id=analise.conta_id,
        franquia_id=analise.franquia_id,
        versao=analise.dataset_versao,
        impressao=analise.dataset_impressao,
    ).one_or_none()
    if dataset is None:
        return False
    contexto = _json(analise.contexto_json)
    if not isinstance(contexto, dict) or contexto.get("papel") not in {_PAPEL_GEMINI, _PAPEL_CLAUDE}:
        return False
    return (
        contexto.get("dataset_id") == int(dataset.id)
        and contexto.get("dataset_versao") == int(dataset.versao)
        and contexto.get("dataset_impressao") == dataset.impressao
    )


def _tentativa_da_chave(chave: str) -> IaChamadaTentativa | None:
    return db.session.query(IaChamadaTentativa).filter_by(attempt_key=chave).one_or_none()


def _reserva_correspondente(linha: AgenteBiExecucao, tentativa: IaChamadaTentativa | None) -> bool:
    if linha.reserva_em_andamento or linha.desfecho_financeiro == "reservado":
        return True
    if tentativa is None:
        return False
    if tentativa.reserva_ativa or tentativa.status == IaChamadaTentativa.STATUS_RESERVED:
        return True
    creditos = tentativa.reserved_credits
    return creditos is not None and Decimal(creditos) > 0


def _configuracao_operacional_valida(papel, codigo: str) -> bool:
    """A configuração que faltava na pré-chamada, sem montar cliente nem gerar."""
    if papel == _PAPEL_GEMINI and codigo == "modelo_ausente":
        modelo = (os.getenv("AGENTE_BI_GEMINI_MODEL") or "").strip()
        return bool(modelo) and len(modelo) <= 255
    if papel == _PAPEL_GEMINI and codigo == "credencial_ausente":
        chave = (os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY_1") or "").strip()
        return bool(chave)
    if papel == _PAPEL_CLAUDE and codigo == "modelo_ausente":
        modelo = (os.getenv("AGENTE_BI_ANTHROPIC_MODEL") or "").strip()
        return bool(modelo) and len(modelo) <= 255
    if papel == _PAPEL_CLAUDE and codigo == "credencial_ausente":
        return bool((os.getenv("ANTHROPIC_API_KEY") or "").strip())
    return False


def _observar(analise: AgenteBiAnalise, execucao: AgenteBiExecucao) -> None:
    tentativa = None
    if execucao.ia_chamada_tentativa_id is not None:
        tentativa = db.session.get(IaChamadaTentativa, int(execucao.ia_chamada_tentativa_id))
    contexto = _json(analise.contexto_json)
    logger.info(
        "evento=agente_bi_colaboracao_rodada provedor=%s acao=%s papel=%s analise_id=%s execucao_id=%s tentativa_id=%s",
        None if tentativa is None else tentativa.provider,
        None if not isinstance(contexto, dict) else contexto.get("acao"),
        None if not isinstance(contexto, dict) else contexto.get("papel"),
        analise.id,
        execucao.id,
        None if tentativa is None else tentativa.id,
    )


def _exigir_cadeia(analise: AgenteBiAnalise, contexto: dict, referencias: dict) -> AgenteBiAnalise | None:
    dataset = AgenteBiDataset.query.filter_by(
        id=analise.dataset_id,
        usuario_id=analise.usuario_id,
        conta_id=analise.conta_id,
        franquia_id=analise.franquia_id,
        versao=analise.dataset_versao,
        impressao=analise.dataset_impressao,
    ).one_or_none()
    if dataset is None:
        raise ExecucaoErro("identidade_inconsistente")
    if (
        referencias.get("analise_id") != int(analise.id)
        or referencias.get("dataset_id") != int(dataset.id)
        or referencias.get("usuario_id") != int(analise.usuario_id)
        or referencias.get("conta_id") != int(analise.conta_id)
        or referencias.get("franquia_id") != int(analise.franquia_id)
        or contexto.get("dataset_id") != int(dataset.id)
        or contexto.get("dataset_versao") != int(dataset.versao)
        or contexto.get("dataset_impressao") != dataset.impressao
        or contexto.get("vinculo_preparacao") != _vinculo(analise)
    ):
        raise ExecucaoErro("identidade_inconsistente")
    return _pai_vinculado(analise, contexto)


def _pai_vinculado(analise: AgenteBiAnalise, contexto: dict) -> AgenteBiAnalise | None:
    pai_id = contexto.get("pai_id")
    if pai_id is None:
        if contexto.get("pai_hash") is not None or contexto.get("acao") != _ACAO_PROPOR:
            raise ExecucaoErro("identidade_inconsistente")
        return None
    if isinstance(pai_id, bool) or not isinstance(pai_id, int) or not isinstance(contexto.get("pai_hash"), str):
        raise ExecucaoErro("identidade_inconsistente")
    pai = AgenteBiAnalise.query.filter_by(
        id=pai_id,
        usuario_id=analise.usuario_id,
        conta_id=analise.conta_id,
        franquia_id=analise.franquia_id,
        dataset_id=analise.dataset_id,
        dataset_versao=analise.dataset_versao,
        dataset_impressao=analise.dataset_impressao,
        contrato_versao=CONTRATO_TECNICO,
    ).one_or_none()
    if pai is None or not pai.resultado_json:
        raise ExecucaoErro("identidade_inconsistente")
    anterior = _json(pai.resultado_json)
    if anterior.get("hash") != contexto.get("pai_hash") or anterior.get("colaboracao_id") != contexto.get("colaboracao_id"):
        raise ExecucaoErro("identidade_inconsistente")
    return pai


def _proposta_revisavel(analise: AgenteBiAnalise, contexto_analise: dict, referencias: dict, contexto_analitico: dict):
    gemini = _exigir_cadeia(analise, contexto_analise, referencias)
    if gemini is None or not gemini.resultado_json:
        raise ExecucaoErro("identidade_inconsistente")
    contexto_gemini = _json(gemini.contexto_json)
    if not isinstance(contexto_gemini, dict):
        raise ExecucaoErro("identidade_inconsistente")
    claude = _exigir_cadeia(
        gemini,
        contexto_gemini,
        {
            "analise_id": int(gemini.id),
            "dataset_id": int(gemini.dataset_id),
            "usuario_id": int(gemini.usuario_id),
            "conta_id": int(gemini.conta_id),
            "franquia_id": int(gemini.franquia_id),
        },
    )
    if claude is None or not claude.resultado_json:
        raise ExecucaoErro("identidade_inconsistente")
    documento_gemini = _json(gemini.resultado_json)
    documento_claude = _json(claude.resultado_json)
    if documento_gemini.get("papel") != _PAPEL_GEMINI or documento_claude.get("papel") != _PAPEL_CLAUDE:
        raise ExecucaoErro("identidade_inconsistente")
    proposta = documento_claude.get("proposta")
    if minimizar_proposta(proposta) != proposta:
        raise ExecucaoErro("identidade_inconsistente")
    critica = documento_gemini.get("critica")
    if minimizar_critica(critica, contexto_analitico) != critica:
        raise ExecucaoErro("contexto_nao_autorizado")
    execucao = AgenteBiExecucao.query.filter_by(analise_id=int(claude.id)).one_or_none()
    if execucao is None:
        raise ExecucaoErro("identidade_inconsistente")
    revalidar_contrato_tecnico(
        claude,
        {
            "execucao_id": int(execucao.id),
            "analise_id": int(claude.id),
            "dataset_id": int(claude.dataset_id),
            "usuario_id": int(claude.usuario_id),
            "conta_id": int(claude.conta_id),
            "franquia_id": int(claude.franquia_id),
        },
        documento_claude,
    )
    return proposta, critica, _verificacao_anterior(documento_claude)


def _verificacao_anterior(documento: dict) -> dict:
    verificacao = documento.get("verificacao")
    if not isinstance(verificacao, dict):
        raise ExecucaoErro("identidade_inconsistente")
    saida = {"aprovado": verificacao.get("aprovado"), "codigo": verificacao.get("codigo")}
    if "descartes" in documento:
        saida["descartes"] = documento["descartes"]
    return saida


def _vinculo(analise: AgenteBiAnalise) -> str:
    return _contexto_analitico(analise)["vinculo_preparacao"]


def _contexto_analitico(analise: AgenteBiAnalise) -> dict:
    from app.services.agente_bi_persistencia_service import EscopoAgenteBi

    try:
        perfil = perfilar_dataset(
            EscopoAgenteBi(
                conta_id=int(analise.conta_id),
                franquia_id=int(analise.franquia_id),
                usuario_id=int(analise.usuario_id),
            ),
            int(analise.dataset_id),
        )
        return montar_contexto(perfil, _pedido(analise))
    except (AnalisarErro, PerfilErro, AgenteBiErro) as exc:
        raise ExecucaoErro("identidade_inconsistente") from exc


def _pedido(analise: AgenteBiAnalise) -> dict:
    regras = _json(analise.regras_negocio_json)
    pedido = regras.get("pedido") if isinstance(regras, dict) else None
    if not isinstance(pedido, dict):
        raise ExecucaoErro("identidade_inconsistente")
    return pedido


def _citar(proposta, critica) -> list:
    saida = []

    def por(valor):
        if isinstance(valor, dict) and valor not in saida:
            saida.append(valor)

    if isinstance(proposta, dict):
        for item in proposta.get("sugestoes") or []:
            por(item.get("regiao"))
            por(item.get("medida"))
            por(item.get("agrupamento"))
            por(item.get("segunda_medida"))
        for item in proposta.get("descobertas") or []:
            por(item.get("regiao"))
    if isinstance(critica, dict):
        for item in critica.get("referencias") or []:
            por(item)
        for item in critica.get("medidas") or []:
            por(item)
        for item in critica.get("objecoes") or []:
            por(item.get("referencia"))
    return [item for item in saida if isinstance(item, dict)]


def _aprovado(documento: dict) -> bool:
    if not isinstance(documento, dict) or documento.get("descartes"):
        return False
    verificacao = documento.get("verificacao")
    return isinstance(verificacao, dict) and verificacao.get("aprovado") is True and verificacao.get("codigo") is None


def _assinatura(proposta: dict) -> str:
    itens = []
    for item in proposta.get("sugestoes") or []:
        itens.append(
            {
                "status": item.get("status"),
                "operacao": item.get("operacao"),
                "grafico": item.get("grafico"),
                "medida": item.get("medida"),
                "agrupamento": item.get("agrupamento"),
                "segunda_medida": item.get("segunda_medida"),
                "evidencias": sorted(item.get("evidencias") or []),
                "juncao": item.get("juncao"),
                "lacuna": item.get("lacuna"),
                "duvidas": sorted(item.get("duvidas") or []),
            }
        )
    return _canonico(itens)


def _nao_autorizadas(contexto, proposta: dict) -> set:
    pool = set()
    for coluna in contexto.get("colunas") or []:
        pool.update(coluna.get("evidencias") or [])
    for regiao in contexto.get("regioes") or []:
        pool.update(regiao.get("evidencias") or [])
    usadas = set()
    for item in proposta.get("sugestoes") or []:
        for codigo in item.get("evidencias") or []:
            if codigo not in pool:
                usadas.add(codigo)
    return usadas


def _pool(contexto, referencias, medidas) -> set:
    pool = set()
    for alvo in list(referencias) + list(medidas):
        for coluna in contexto.get("colunas") or []:
            if _mesma(coluna, alvo):
                pool.update(coluna.get("evidencias") or [])
        for regiao in contexto.get("regioes") or []:
            if _mesma(regiao, alvo, regiao=True):
                pool.update(regiao.get("evidencias") or [])
    return pool


def _coordenada_autorizada(contexto, ref, regiao=False) -> bool:
    origem = contexto.get("regioes") if regiao else contexto.get("colunas")
    return any(_mesma(item, ref, regiao=regiao) for item in origem or [])


def _mesma(origem, ref, regiao=False) -> bool:
    chaves = ("arquivo", "aba", "regiao") if regiao or "indice" not in ref else ("arquivo", "aba", "regiao", "indice")
    return all(origem.get(chave) == ref.get(chave) for chave in chaves)


def _referencias_validas(valor) -> bool:
    if not isinstance(valor, list) or len(valor) > 32:
        return False
    for item in valor:
        if _coluna(item) is None and _regiao(item) is None:
            return False
    return True


def _lista_de(valor, item, teto):
    if not isinstance(valor, list) or len(valor) > teto:
        return None
    saida = []
    for bruto in valor:
        convertido = item(bruto)
        if convertido is None:
            return None
        saida.append(convertido)
    return saida


def _sugestao(item):
    chaves = {
        "status",
        "leitura",
        "grafico",
        "operacao",
        "origem",
        "regiao",
        "medida",
        "agrupamento",
        "segunda_medida",
        "evidencias",
        "juncao",
        "lacuna",
        "duvidas",
    }
    if not isinstance(item, dict) or set(item) != chaves:
        return None
    if item.get("status") not in _STATUS or item.get("leitura") not in _LEITURAS:
        return None
    if item.get("origem") not in _ORIGENS or item.get("juncao") != _JUNCAO:
        return None
    if item.get("grafico") is not None and item.get("grafico") not in GRAFICOS:
        return None
    if item.get("operacao") is not None and item.get("operacao") not in OPERACOES:
        return None
    if item.get("lacuna") is not None and item.get("lacuna") not in _LACUNAS:
        return None
    regiao = item.get("regiao")
    if regiao is not None:
        regiao = _regiao(regiao)
        if regiao is None:
            return None
    medida = _opcional(item.get("medida"), _coluna)
    agrupamento = _opcional(item.get("agrupamento"), _coluna)
    segunda = _opcional(item.get("segunda_medida"), _coluna)
    if medida is False or agrupamento is False or segunda is False:
        return None
    evidencias = item.get("evidencias")
    duvidas = item.get("duvidas")
    if not isinstance(evidencias, list) or not isinstance(duvidas, list):
        return None
    if len(evidencias) > 8 or len(duvidas) > 8:
        return None
    if any(codigo not in _EVIDENCIAS for codigo in evidencias) or any(codigo not in _PERGUNTAS for codigo in duvidas):
        return None
    return {
        "status": item["status"],
        "leitura": item["leitura"],
        "grafico": item.get("grafico"),
        "operacao": item.get("operacao"),
        "origem": item["origem"],
        "regiao": regiao,
        "medida": medida,
        "agrupamento": agrupamento,
        "segunda_medida": segunda,
        "evidencias": list(evidencias),
        "juncao": _JUNCAO,
        "lacuna": item.get("lacuna"),
        "duvidas": list(duvidas),
    }


def _descoberta(item):
    if not isinstance(item, dict) or set(item) != {"codigo", "regiao", "relacao"}:
        return None
    if item.get("codigo") not in _DESCOBERTAS:
        return None
    regiao = item.get("regiao")
    if regiao is not None:
        regiao = _regiao(regiao)
        if regiao is None:
            return None
    relacao = item.get("relacao")
    if relacao is not None and (isinstance(relacao, bool) or not isinstance(relacao, int) or relacao < 0):
        return None
    return {"codigo": item["codigo"], "regiao": regiao, "relacao": relacao}


def _opcional(valor, conversor):
    if valor is None:
        return None
    convertido = conversor(valor)
    if convertido is None:
        return False
    return convertido


def _coluna(valor):
    if not isinstance(valor, dict) or set(valor) != {"arquivo", "aba", "regiao", "indice"}:
        return None
    if any(_inteiro(valor[chave]) is None for chave in valor):
        return None
    return {chave: int(valor[chave]) for chave in ("arquivo", "aba", "regiao", "indice")}


def _regiao(valor):
    if not isinstance(valor, dict) or set(valor) != {"arquivo", "aba", "regiao"}:
        return None
    if any(_inteiro(valor[chave]) is None for chave in valor):
        return None
    return {chave: int(valor[chave]) for chave in ("arquivo", "aba", "regiao")}


def _inteiro(valor):
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        return None
    return valor


def _dataset(escopo, dataset_id: int) -> AgenteBiDataset:
    linha = AgenteBiDataset.query.filter_by(
        id=int(dataset_id),
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        usuario_id=escopo.usuario_id,
    ).one_or_none()
    if linha is None:
        raise ColaboracaoErro("nao_encontrado", 404)
    return linha


def _proxima_rodada(escopo, chave: str) -> int:
    from sqlalchemy import func

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


def _identidade(prefixo: str, corpo: dict) -> str:
    return prefixo + hashlib.sha256(_canonico(corpo).encode("utf-8")).hexdigest()


def _hash(corpo: dict) -> str:
    return hashlib.sha256(_canonico(corpo).encode("utf-8")).hexdigest()


def _canonico(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json(valor):
    if isinstance(valor, dict):
        return valor
    return json.loads(valor)


def _codigo(codigo: str) -> str:
    if isinstance(codigo, str) and codigo.isidentifier() is False:
        texto = codigo
    else:
        texto = codigo if isinstance(codigo, str) else ""
    if isinstance(texto, str) and texto.replace("_", "").isalnum() and texto.islower() and 1 <= len(texto) <= 40 and texto.isidentifier():
        return texto
    if isinstance(codigo, str) and 1 <= len(codigo) <= 40 and all(c.islower() or c.isdigit() or c == "_" for c in codigo) and codigo and codigo[0].isalpha():
        return codigo
    return "provedor_indisponivel"


def _status(codigo: str) -> int:
    if codigo in {"autorizacao_indevida", "provedor_nao_autorizado", "admissao_recusada", "isolamento"}:
        return 403
    if codigo == "nao_encontrado":
        return 404
    return 422
