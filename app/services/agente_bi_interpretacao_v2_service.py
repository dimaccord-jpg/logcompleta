"""Interpretação conversacional `patch.definir.v2` do AgenteBI (SCRUM-269).

Produz e valida um plano. Não executa a DSL, não calcula e não publica
dashboard. O texto autorizado segue ao adaptador já existente, depois de
conferir a representação que essa governança autoriza de fato. O vínculo
de gráfico, coluna e parâmetro opaco é do servidor.

Dependência de política, antes de ligar o adaptador com dados reais:
o tratamento de placas em texto livre continua na governança compartilhada.
Esta etapa não cria classificador local nem categoria nova de identificador.
"""
from __future__ import annotations

import hashlib
import json
import logging
from uuid import uuid4

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiExecucao
from app.services.agente_bi_analisar_service import (
    GRAFICOS,
    TransferenciaRecusada,
    conferir_representacao,
    governar_representacao,
)
from app.services.agente_bi_contrato_v2_service import CONTRATO_PATCH_V2, validar_plano
from app.services.agente_bi_definir_service import (
    OPERACOES_EXECUCAO,
    DefinirErro,
    _ESCLARECIMENTOS,
    _FILTROS,
    _TIPOS_PARAMETRO,
    _id,
    _inteiro,
    _nome_parametro,
)
from app.services.agente_bi_evolucao_service import _carregar
from app.services.agente_bi_execucao_service import (
    LEASE_MAX_SEGUNDOS,
    EntregaProcessador,
    ExecucaoErro,
    enfileirar,
    executar_um_ciclo,
)
from app.services.agente_bi_integracao_service import _codigo, _soltar_efeito_para_falha, execucao_real_habilitada
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)
from app.services.cleiton_ai_data_governance import govern_outbound_content

logger = logging.getLogger(__name__)

CONTRATO_CONTEXTO_PLANO = "contexto.plano.v2"
_CHAVES_CANDIDATO = frozenset(
    {
        "contrato",
        "rotulos",
        "valores_de_celula",
        "amostras",
        "apto_anthropic",
        "autorizado_para_provedor",
        "transferencia",
        "intencao",
        "graficos",
        "colunas",
        "associacoes",
        "parametros_opacos",
    }
)
_CHAVES_PARAMETRO_PUBLICO = frozenset({"referencia", "tipo"})
_CHAVES_PARAMETRO_INTERNO = frozenset({"referencia", "grafico", "nome", "tipo"})
_CHAVES_INTERNO = frozenset(
    {
        "dashboard_id",
        "revisao_base",
        "definicao_anterior_id",
        "dataset",
        "identidade",
        "graficos",
        "parametros",
    }
)
_CHAVES_DATASET_INTERNO = frozenset({"id", "versao", "impressao"})
CODIGO_FILA = "plano_v2"
DEPENDENCIA_POLITICA_PLACAS = (
    "O tratamento de placas em texto livre permanece na governança compartilhada "
    "e precisa estar resolvido antes de ligar o adaptador com dados reais."
)
_SIGNIFICADO_NAO_CONFIRMADO = "nao_confirmado"
_CHAVES_PEDIDO = frozenset({"texto"})
_CHAVES_REF = ("arquivo", "aba", "regiao", "indice")
_LIMPEZA = frozenset({"intencao"})
_TENTATIVAS = 2


class InterpretacaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def interpretar_plano(escopo, dashboard_id: int, corpo: dict, *, transporte=None, lease_segundos: int | None = None) -> dict:
    """Fecha um plano conversacional. Não aplica o plano ao dashboard."""
    texto = _texto(corpo)
    carregado = _carregar(escopo, dashboard_id)
    pacote = _pacote_servidor(carregado, texto)
    payload = _exigir_contexto(pacote)
    if payload is None:
        raise InterpretacaoErro("intencao_nao_transportada")
    pacote = {**pacote, "texto": payload["intencao"]}
    if not execucao_real_habilitada():
        raise InterpretacaoErro("interpretacao_indisponivel")
    if transporte is not None and not callable(transporte):
        raise InterpretacaoErro("transporte_real_bloqueado", 403)
    identidade = _identidade(escopo, carregado, pacote["texto"])
    abertura = _abrir(escopo, carregado, pacote, identidade)
    ciclo = _operar(
        escopo,
        int(abertura["execucao"]["id"]),
        int(abertura["analise_id"]),
        transporte=transporte,
        lease_segundos=LEASE_MAX_SEGUNDOS if lease_segundos is None else lease_segundos,
    )
    plano, chamada = _fechar(escopo, abertura, ciclo)
    logger.info(
        "evento=agente_bi_plano_interpretado usuario_id=%s dashboard_id=%s situacao=%s chamada_anthropic=%s",
        escopo.usuario_id,
        int(dashboard_id),
        plano["situacao"],
        int(chamada),
    )
    return {
        "ok": True,
        "contrato": CONTRATO_PATCH_V2,
        "situacao": plano["situacao"],
        "plano": plano,
        "executado": False,
        "chamada_anthropic": chamada,
        "analise_id": int(abertura["analise_id"]),
    }


def revalidar_plano_duravel(analise: AgenteBiAnalise, documento) -> dict:
    """Confere o plano na confirmação. O vínculo sai da análise, não do modelo."""
    if analise.contrato_versao != CONTRATO_PATCH_V2:
        raise ExecucaoErro("contrato_saida_invalido")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_PATCH_V2:
        raise ExecucaoErro("contrato_saida_invalido")
    try:
        regras = json.loads(analise.regras_negocio_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecucaoErro("contrato_saida_invalido") from exc
    if not isinstance(regras, dict) or regras.get("contrato") != CONTRATO_PATCH_V2:
        raise ExecucaoErro("contrato_saida_invalido")
    try:
        plano = validar_plano(documento, regras.get("vinculo"))
    except DefinirErro as exc:
        raise ExecucaoErro(_codigo(exc.codigo)) from exc
    _recusar_opaco_externo(plano, _referencias_opacas(regras.get("parametros")))
    return plano


def _texto(corpo) -> str:
    if not isinstance(corpo, dict) or set(corpo) != _CHAVES_PEDIDO:
        raise InterpretacaoErro("payload_invalido")
    texto = corpo.get("texto")
    if not isinstance(texto, str) or not texto.strip() or len(texto) > 500 or "\x00" in texto:
        raise InterpretacaoErro("payload_invalido")
    return texto.strip()


def _pacote_servidor(carregado, texto: str) -> dict:
    vinculo = {
        "graficos": _graficos(carregado),
        "colunas": _colunas(carregado),
    }
    return {
        "vinculo": vinculo,
        "parametros": _parametros_opacos(carregado),
        "associacoes": _associacoes_comprovadas(carregado),
        "texto": texto,
    }


def _exigir_contexto(pacote: dict):
    return _governar(_candidato(pacote))


def _governar(candidato: dict):
    try:
        autorizado = governar_representacao(
            candidato,
            governar=govern_outbound_content,
            limpeza=_LIMPEZA,
            nulas=frozenset(),
        )
    except (TransferenciaRecusada, KeyError, TypeError) as exc:
        raise InterpretacaoErro("contexto_nao_autorizado") from exc
    payload = autorizado.get("payload") if isinstance(autorizado, dict) else None
    if not isinstance(payload, dict):
        raise InterpretacaoErro("contexto_nao_autorizado")
    original = candidato.get("intencao")
    obtida = payload.get("intencao")
    if isinstance(original, str) and original.strip() and not (isinstance(obtida, str) and obtida.strip()):
        return None
    return payload


def _candidato(pacote: dict) -> dict:
    """Envelope enviado ao adaptador. O contexto interno não entra aqui."""
    candidato = {
        "contrato": CONTRATO_CONTEXTO_PLANO,
        "rotulos": False,
        "valores_de_celula": False,
        "amostras": False,
        "apto_anthropic": False,
        "autorizado_para_provedor": False,
        "transferencia": "bloqueada",
        "intencao": pacote["texto"],
        "graficos": [{"id": item} for item in pacote["vinculo"]["graficos"]],
        "colunas": [_coluna_publica(item) for item in pacote["vinculo"]["colunas"]],
        "associacoes": list(pacote["associacoes"]),
        "parametros_opacos": [_parametro_publico(item) for item in pacote["parametros"]],
    }
    if set(candidato) != _CHAVES_CANDIDATO:
        raise InterpretacaoErro("contexto_nao_autorizado")
    return candidato


def _parametro_publico(item: dict) -> dict:
    if not isinstance(item, dict):
        raise InterpretacaoErro("parametro_inconsistente")
    chaves = frozenset(item)
    if chaves not in {_CHAVES_PARAMETRO_PUBLICO, _CHAVES_PARAMETRO_INTERNO}:
        raise InterpretacaoErro("parametro_inconsistente")
    return {"referencia": item["referencia"], "tipo": item["tipo"]}


def _coluna_publica(coluna: dict) -> dict:
    return {
        "referencia": {chave: coluna[chave] for chave in _CHAVES_REF},
        "papel": coluna.get("dimensao_principal"),
        "tipos": list(coluna.get("tipos") or []),
        "evidencias": list(coluna.get("evidencias") or []),
    }


def _graficos(carregado) -> list:
    saida = []
    for item in carregado.get("definicoes") or []:
        if not isinstance(item, dict):
            continue
        identificador = item.get("id")
        if _id(identificador, "g") and identificador not in saida:
            saida.append(identificador)
    return saida


def _colunas(carregado) -> list:
    saida = []
    vistos = set()
    for coluna in carregado.get("autorizadas") or []:
        if not isinstance(coluna, dict):
            continue
        ref = {}
        for chave in _CHAVES_REF:
            numero = _inteiro(coluna.get(chave))
            if numero is None:
                ref = None
                break
            ref[chave] = numero
        if ref is None:
            continue
        coordenada = tuple(ref[chave] for chave in _CHAVES_REF)
        if coordenada in vistos:
            continue
        vistos.add(coordenada)
        publica = dict(ref)
        tipos = _textos(coluna.get("tipos"))
        dimensoes = _textos(coluna.get("dimensoes"))
        evidencias = _textos(coluna.get("evidencias"))
        if tipos is not None:
            publica["tipos"] = tipos
        if dimensoes is not None:
            publica["dimensoes"] = dimensoes
        if evidencias is not None:
            publica["evidencias"] = evidencias
        papel = coluna.get("dimensao_principal")
        if isinstance(papel, str):
            publica["dimensao_principal"] = papel
        saida.append(publica)
    return saida


def _textos(valor):
    if not isinstance(valor, list) or any(not isinstance(item, str) for item in valor):
        return None
    return list(valor)


def _parametros_opacos(carregado) -> list:
    """Referência opaca só para parâmetro declarado na definição carregada."""
    return [_parametro_publico(item) for item in _vinculos_parametro(carregado.get("definicoes"))]


def _vinculos_parametro(definicoes) -> list:
    """Vínculo tipado já gravado na definição. Não lê o texto e não completa lacuna."""
    saida = []
    numero = 1
    for item in definicoes or []:
        if not isinstance(item, dict):
            continue
        grafico = item.get("id")
        if not _id(grafico, "g"):
            continue
        for parametro in item.get("parametros") or []:
            if not isinstance(parametro, dict):
                continue
            nome = parametro.get("nome")
            tipo = parametro.get("tipo")
            if not _nome_parametro(nome) or tipo not in _TIPOS_PARAMETRO:
                continue
            referencia = f"p{numero}"
            if not _id(referencia, "p"):
                return saida
            saida.append({"referencia": referencia, "grafico": grafico, "nome": nome, "tipo": tipo})
            numero += 1
    return saida


def _associacoes_comprovadas(carregado) -> list:
    """Só associação com significado confirmado. Alias técnico não entra.

    Sobreposição estrutural com significado não confirmado fica de fora.
    O texto do significado também fica de fora: ele não prova o negócio
    e pode repetir um rótulo.
    """
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    autorizadas = set()
    for coluna in carregado.get("autorizadas") or []:
        if isinstance(coluna, dict):
            coordenada = tuple(coluna.get(chave) for chave in _CHAVES_REF)
            if all(isinstance(item, int) and not isinstance(item, bool) for item in coordenada):
                autorizadas.add(coordenada)
    saida = []
    for relacao in perfil.get("relacoes") or []:
        if not isinstance(relacao, dict) or relacao.get("confirmacao_usuario") is not True:
            continue
        significado = relacao.get("significado_negocio")
        if not isinstance(significado, str) or not significado.strip() or significado == _SIGNIFICADO_NAO_CONFIRMADO:
            continue
        lados = []
        for chave in ("lado_unico", "lado_associado"):
            ref = _ref_associacao(relacao.get(chave))
            if ref is None or tuple(ref[item] for item in _CHAVES_REF) not in autorizadas:
                lados = None
                break
            lados.append(ref)
        if lados is None:
            continue
        saida.append({"lado_unico": lados[0], "lado_associado": lados[1]})
    return saida


def _ref_associacao(valor):
    if not isinstance(valor, dict):
        return None
    ref = {}
    for chave in _CHAVES_REF:
        numero = _inteiro(valor.get(chave if chave != "indice" else "indice"))
        if numero is None and chave == "indice":
            numero = _inteiro(valor.get("coluna"))
        if numero is None:
            return None
        ref[chave] = numero
    return ref


def _identidade(escopo, carregado, texto: str) -> str:
    dataset = carregado["dataset"]
    painel = carregado["painel"]
    corpo = {
        "conta_id": int(escopo.conta_id),
        "franquia_id": int(escopo.franquia_id),
        "usuario_id": int(escopo.usuario_id),
        "dashboard_id": int(painel["id"]),
        "revisao": int(painel["revisao"]),
        "dataset_id": int(dataset["id"]),
        "versao": int(dataset["versao"]),
        "impressao": dataset["impressao"],
        "texto": texto,
    }
    digest = hashlib.sha256(
        json.dumps(corpo, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    ).hexdigest()
    return "plano." + digest


def _contexto_interno(escopo, carregado, identidade: str) -> dict:
    """Dados para revalidar revisão e permissão. Não segue ao provedor."""
    dataset = carregado["dataset"]
    painel = carregado["painel"]
    definicao = carregado["definicao"]
    if (
        int(painel["usuario_id"]) != int(escopo.usuario_id)
        or int(painel["conta_id"]) != int(escopo.conta_id)
        or int(painel["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise InterpretacaoErro("autorizacao_indevida", 403)
    return {
        "dashboard_id": int(painel["id"]),
        "revisao_base": int(painel["revisao"]),
        "definicao_anterior_id": int(definicao["id"]),
        "dataset": {
            "id": int(dataset["id"]),
            "versao": int(dataset["versao"]),
            "impressao": dataset["impressao"],
        },
        "identidade": identidade,
        "graficos": _graficos(carregado),
        "parametros": _vinculos_parametro(carregado.get("definicoes")),
    }


def recuperar_contexto(escopo, analise_id: int) -> dict:
    """Relê o contexto interno e confere escopo, revisão e parâmetros. Não chama provedor."""
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or analise_id < 1:
        raise InterpretacaoErro("payload_invalido")
    try:
        registro = obter_analise(escopo, analise_id)
    except AgenteBiErro as exc:
        raise InterpretacaoErro(_codigo(exc.codigo), exc.status) from exc
    if registro.get("contrato_versao") != CONTRATO_PATCH_V2:
        raise InterpretacaoErro("contrato_saida_invalido")
    contexto = registro.get("contexto")
    regras = registro.get("regras_negocio")
    if not isinstance(contexto, dict) or not isinstance(regras, dict):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    interno = contexto.get("interno")
    if not _interno_integro(interno):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if registro.get("chave_recuperacao") != interno["identidade"]:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    painel = _painel_autorizado(escopo, int(interno["dashboard_id"]))
    if int(painel["revisao"]) != int(interno["revisao_base"]):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if (
        int(painel["usuario_id"]) != int(escopo.usuario_id)
        or int(painel["conta_id"]) != int(escopo.conta_id)
        or int(painel["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise InterpretacaoErro("autorizacao_indevida", 403)
    try:
        definicao = obter_analise(escopo, int(interno["definicao_anterior_id"]))
        dataset = obter_dataset(escopo, int(registro["dataset_id"]))
        carregado = _carregar(escopo, int(painel["id"]))
    except AgenteBiErro as exc:
        raise InterpretacaoErro("identidade_inconsistente", 409) from exc
    if int(definicao["id"]) != int(carregado["definicao"]["id"]):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if (
        int(dataset["id"]) != int(interno["dataset"]["id"])
        or int(dataset["versao"]) != int(interno["dataset"]["versao"])
        or dataset["impressao"] != interno["dataset"]["impressao"]
        or int(registro["dataset_id"]) != int(dataset["id"])
        or int(registro["versao"]) != int(dataset["versao"])
        or registro["impressao"] != dataset["impressao"]
        or int(carregado["dataset"]["id"]) != int(dataset["id"])
        or int(carregado["dataset"]["versao"]) != int(dataset["versao"])
        or carregado["dataset"]["impressao"] != dataset["impressao"]
    ):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    if perfil.get("vinculo_preparacao") != contexto.get("vinculo_preparacao"):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    texto = regras.get("texto")
    if not isinstance(texto, str) or _identidade(escopo, carregado, texto) != interno["identidade"]:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    graficos_regras = (regras.get("vinculo") or {}).get("graficos") if isinstance(regras.get("vinculo"), dict) else None
    if graficos_regras != interno["graficos"] or _graficos(carregado) != interno["graficos"]:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    _conferir_parametros_recuperados(interno["parametros"], carregado.get("definicoes"), regras.get("parametros"))
    return {
        "dashboard_id": int(interno["dashboard_id"]),
        "revisao_base": int(interno["revisao_base"]),
        "definicao_anterior_id": int(interno["definicao_anterior_id"]),
        "dataset": {
            "id": int(dataset["id"]),
            "versao": int(dataset["versao"]),
            "impressao": dataset["impressao"],
        },
        "identidade": interno["identidade"],
        "graficos": list(interno["graficos"]),
        "parametros": [dict(item) for item in interno["parametros"]],
    }


def _interno_integro(valor) -> bool:
    if not isinstance(valor, dict) or set(valor) != _CHAVES_INTERNO:
        return False
    if isinstance(valor.get("dashboard_id"), bool) or not isinstance(valor.get("dashboard_id"), int):
        return False
    if isinstance(valor.get("revisao_base"), bool) or not isinstance(valor.get("revisao_base"), int):
        return False
    if isinstance(valor.get("definicao_anterior_id"), bool) or not isinstance(valor.get("definicao_anterior_id"), int):
        return False
    if not isinstance(valor.get("identidade"), str) or not valor["identidade"].startswith("plano."):
        return False
    dataset = valor.get("dataset")
    if not isinstance(dataset, dict) or set(dataset) != _CHAVES_DATASET_INTERNO:
        return False
    if isinstance(dataset.get("id"), bool) or not isinstance(dataset.get("id"), int):
        return False
    if isinstance(dataset.get("versao"), bool) or not isinstance(dataset.get("versao"), int):
        return False
    if not isinstance(dataset.get("impressao"), str):
        return False
    graficos = valor.get("graficos")
    if not isinstance(graficos, list) or any(not _id(item, "g") for item in graficos):
        return False
    parametros = valor.get("parametros")
    if not isinstance(parametros, list):
        return False
    for item in parametros:
        if not isinstance(item, dict) or set(item) != _CHAVES_PARAMETRO_INTERNO:
            return False
        if not _id(item.get("referencia"), "p") or not _id(item.get("grafico"), "g"):
            return False
        if not _nome_parametro(item.get("nome")) or item.get("tipo") not in _TIPOS_PARAMETRO:
            return False
    return True


def _painel_autorizado(escopo, dashboard_id: int) -> dict:
    try:
        painel = obter_dashboard(escopo, dashboard_id)
    except AgenteBiErro as exc:
        raise InterpretacaoErro("identidade_inconsistente", 409) from exc
    if (
        int(painel["usuario_id"]) != int(escopo.usuario_id)
        or int(painel["conta_id"]) != int(escopo.conta_id)
        or int(painel["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise InterpretacaoErro("autorizacao_indevida", 403)
    return painel


def _conferir_parametros_recuperados(gravados, definicoes, publicos) -> None:
    atuais = _vinculos_parametro(definicoes)
    if not isinstance(publicos, list):
        raise InterpretacaoErro("parametro_inconsistente")
    referencias = {item["referencia"] for item in atuais if isinstance(item, dict)}
    for item in gravados:
        if item.get("referencia") not in referencias:
            raise InterpretacaoErro("referencia_inexistente")
    if gravados != atuais or publicos != [_parametro_publico(item) for item in atuais]:
        raise InterpretacaoErro("parametro_inconsistente")


def _abrir(escopo, carregado, pacote: dict, identidade: str) -> dict:
    ultimo = None
    for _ in range(_TENTATIVAS):
        try:
            return _abrir_uma(escopo, carregado, pacote, identidade)
        except (AgenteBiErro, ExecucaoErro) as exc:
            ultimo = exc
            if getattr(exc, "codigo", "") not in {"chave_recuperacao_existente", "rodada_existente", "execucao_existente"}:
                raise InterpretacaoErro(_codigo(getattr(exc, "codigo", "")), _status(getattr(exc, "codigo", ""))) from exc
    raise InterpretacaoErro(_codigo(getattr(ultimo, "codigo", "")), _status(getattr(ultimo, "codigo", "")))


def _abrir_uma(escopo, carregado, pacote: dict, identidade: str) -> dict:
    existente = (
        AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            chave_recuperacao=identidade,
        ).one_or_none()
    )
    if existente is not None:
        _exigir_mesmo_pedido(escopo, existente, carregado, pacote)
        return {
            "analise_id": int(existente.id),
            "execucao": enfileirar(escopo, int(existente.id), chave_idempotencia=identidade),
            "reusada": True,
        }
    dataset = carregado["dataset"]
    perfil = carregado.get("perfil") if isinstance(carregado.get("perfil"), dict) else {}
    try:
        analise = criar_analise(
            escopo,
            {
                "chave": identidade,
                "rodada": 1,
                "dataset_id": int(dataset["id"]),
                "versao": int(dataset["versao"]),
                "impressao": dataset["impressao"],
                "etapa": "definicao",
                "estado": "preparada",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_PLANO,
                    "vinculo_preparacao": perfil.get("vinculo_preparacao"),
                    "interno": _contexto_interno(escopo, carregado, identidade),
                },
                "regras_negocio": {
                    "contrato": CONTRATO_PATCH_V2,
                    "vinculo": pacote["vinculo"],
                    "parametros": pacote["parametros"],
                    "associacoes": pacote["associacoes"],
                    "texto": pacote["texto"],
                },
                "esclarecimentos": {},
                "contrato_versao": CONTRATO_PATCH_V2,
                "chave_recuperacao": identidade,
            },
        )
    except AgenteBiErro:
        raise
    return {
        "analise_id": int(analise["id"]),
        "execucao": enfileirar(escopo, int(analise["id"]), chave_idempotencia=identidade),
        "reusada": False,
    }


def _exigir_mesmo_pedido(escopo, linha: AgenteBiAnalise, carregado, pacote: dict) -> None:
    dataset = carregado["dataset"]
    if int(linha.dataset_id) != int(dataset["id"]) or int(linha.dataset_versao) != int(dataset["versao"]):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if linha.dataset_impressao != dataset["impressao"] or linha.contrato_versao != CONTRATO_PATCH_V2:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    try:
        regras = json.loads(linha.regras_negocio_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InterpretacaoErro("identidade_inconsistente", 409) from exc
    if not isinstance(regras, dict) or regras.get("texto") != pacote["texto"]:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if regras.get("vinculo") != pacote["vinculo"]:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    try:
        contexto = json.loads(linha.contexto_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InterpretacaoErro("identidade_inconsistente", 409) from exc
    if not isinstance(contexto, dict):
        raise InterpretacaoErro("identidade_inconsistente", 409)
    esperado = _contexto_interno(escopo, carregado, _identidade(escopo, carregado, pacote["texto"]))
    if contexto.get("interno") != esperado:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    if contexto.get("vinculo_preparacao") != (carregado.get("perfil") or {}).get("vinculo_preparacao"):
        raise InterpretacaoErro("identidade_inconsistente", 409)


def _operar(escopo, execucao_id: int, analise_id: int, *, transporte, lease_segundos: int) -> dict:
    def processador(referencias):
        if int(referencias.get("execucao_id") or 0) != execucao_id:
            raise ExecucaoErro("identidade_inconsistente")
        return _processar(referencias, transporte)

    return executar_um_ciclo(
        worker_id=f"plano-{int(escopo.usuario_id)}-{uuid4().hex[:12]}",
        processador=processador,
        execucao_id=execucao_id,
        lease_segundos=lease_segundos,
    )


def _processar(referencias: dict, transporte) -> EntregaProcessador:
    from app.services.agente_bi_anthropic_service import chamar_anthropic_governado

    linha = db.session.get(AgenteBiAnalise, int(referencias["analise_id"]))
    execucao = db.session.get(AgenteBiExecucao, int(referencias["execucao_id"]))
    if linha is None or execucao is None or linha.contrato_versao != CONTRATO_PATCH_V2:
        raise ExecucaoErro("identidade_inconsistente")
    if int(execucao.analise_id) != int(linha.id) or execucao.lease_worker is None or execucao.fencing is None:
        raise ExecucaoErro("fencing_recusado")
    try:
        regras = json.loads(linha.regras_negocio_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecucaoErro("contrato_saida_invalido") from exc
    if not isinstance(regras, dict):
        raise ExecucaoErro("contrato_saida_invalido")
    try:
        payload = _governar(_candidato(regras))
    except (InterpretacaoErro, KeyError, TypeError) as exc:
        codigo = exc.codigo if isinstance(exc, InterpretacaoErro) else "contrato_saida_invalido"
        raise ExecucaoErro(_codigo(codigo)) from exc
    if payload is None:
        raise ExecucaoErro("intencao_nao_transportada")
    mensagem = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    _conferir_transporte(payload, mensagem, usuario_id=int(referencias["usuario_id"]))
    resultado = chamar_anthropic_governado(
        usuario_id=int(referencias["usuario_id"]),
        conta_id=int(referencias["conta_id"]),
        franquia_id=int(referencias["franquia_id"]),
        mensagem=mensagem,
        chave_idempotencia=execucao.chave_idempotencia,
        execucao_id=int(execucao.id),
        worker_id=execucao.lease_worker,
        fencing=int(execucao.fencing),
        transporte=transporte,
        system=_instrucao(),
        output_config=None,
    )
    if resultado.get("incerto"):
        raise ExecucaoErro("efeito_incerto")
    if not resultado.get("ok"):
        _soltar_efeito_para_falha(execucao)
        raise ExecucaoErro(_codigo(str(resultado.get("codigo") or "")))
    try:
        plano = _plano_do_texto(resultado.get("texto"), regras)
    except (DefinirErro, InterpretacaoErro) as exc:
        _soltar_efeito_para_falha(execucao)
        raise ExecucaoErro(_codigo(getattr(exc, "codigo", ""))) from exc
    return EntregaProcessador(CODIGO_FILA, plano)


def _conferir_transporte(payload: dict, mensagem: str, *, usuario_id: int) -> None:
    """Confere o envelope que o adaptador governa de novo, antes de transportar.

    A chamada usa `govern_outbound_content` com a mesma finalidade, o mesmo
    agente, o mesmo provedor e o mesmo tipo de conteúdo. O texto enviado ao
    adaptador continua sendo `mensagem`: uma sessão nova reproduz a mesma
    decisão. JSON inválido, referência alterada ou intenção vazia não segue.
    """
    from app.services.agente_bi_anthropic_service import AGENT, PROVIDER, _pedido_seguro
    from app.services.cleiton_ai_data_governance import (
        CONTENT_TYPE_DOCUMENT,
        DECISION_ALLOW,
        DECISION_MINIMIZE,
        PURPOSE_BI_LOGISTICO,
        governance_result_is_coherent,
    )
    from app.services.cleiton_ai_safe_context import CleitonAiAliasSession

    envelope = {"system": _instrucao(), "messages": [{"role": "user", "content": mensagem}]}
    try:
        resultado = govern_outbound_content(
            envelope,
            purpose=PURPOSE_BI_LOGISTICO,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent=AGENT,
            provider=PROVIDER,
            alias_session=CleitonAiAliasSession(usuario_id=usuario_id),
        )
    except Exception as exc:
        raise ExecucaoErro("contexto_nao_autorizado") from exc
    if (
        not getattr(resultado, "issued_by_cleiton", False)
        or not governance_result_is_coherent(resultado, original=envelope)
        or getattr(resultado, "decision", None) not in {DECISION_ALLOW, DECISION_MINIMIZE}
    ):
        raise ExecucaoErro("contexto_nao_autorizado")
    # Modelo e limite não alteram o texto; só extraem a mensagem autorizada.
    pedido = _pedido_seguro(resultado.safe_content, "conferencia", 1)
    if not isinstance(pedido, dict):
        raise ExecucaoErro("contexto_nao_autorizado")
    try:
        autorizado = json.loads(pedido["messages"][0]["content"])
    except (json.JSONDecodeError, KeyError, TypeError, IndexError) as exc:
        raise ExecucaoErro("contexto_nao_autorizado") from exc
    if not isinstance(autorizado, dict):
        raise ExecucaoErro("contexto_nao_autorizado")
    original = payload.get("intencao")
    obtida = autorizado.get("intencao")
    if isinstance(original, str) and original.strip() and not (isinstance(obtida, str) and obtida.strip()):
        raise ExecucaoErro("intencao_nao_transportada")
    try:
        conferir_representacao(payload, autorizado, limpeza=_LIMPEZA, nulas=frozenset())
    except TransferenciaRecusada as exc:
        raise ExecucaoErro("contexto_nao_autorizado") from exc


def _plano_do_texto(texto, regras: dict) -> dict:
    if not isinstance(texto, str) or not texto.strip():
        raise DefinirErro("contrato_saida_invalido")
    try:
        documento = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise DefinirErro("contrato_saida_invalido") from exc
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_PATCH_V2:
        raise DefinirErro("contrato_saida_invalido")
    plano = validar_plano(documento, regras.get("vinculo"))
    _recusar_opaco_externo(plano, _referencias_opacas(regras.get("parametros")))
    return plano


def _referencias_opacas(valor) -> set:
    if not isinstance(valor, list):
        return set()
    saida = set()
    for item in valor:
        if isinstance(item, dict) and isinstance(item.get("referencia"), str):
            saida.add(item["referencia"])
    return saida


def _recusar_opaco_externo(plano: dict, permitidos: set) -> None:
    for operacao in plano.get("operacoes") or []:
        if not isinstance(operacao, dict):
            raise DefinirErro("contrato_saida_invalido")
        for parametro in operacao.get("parametros") or []:
            if isinstance(parametro, dict) and parametro.get("forma") == "opaca":
                if parametro.get("referencia") not in permitidos:
                    raise DefinirErro("referencia_inexistente")


def _fechar(escopo, abertura: dict, ciclo: dict) -> tuple[dict, bool]:
    if not isinstance(ciclo, dict):
        raise InterpretacaoErro("provedor_indisponivel")
    if ciclo.get("em_andamento"):
        raise InterpretacaoErro("operacao_em_andamento", 409)
    execucao = ciclo.get("execucao") if isinstance(ciclo.get("execucao"), dict) else {}
    if ciclo.get("adquirida"):
        if execucao.get("estado") != "concluida":
            codigo = execucao.get("ultimo_erro") or "provedor_indisponivel"
            raise InterpretacaoErro(_codigo(str(codigo)), _status(str(codigo), execucao.get("estado")))
        return _plano_gravado(escopo, int(abertura["analise_id"])), True
    linha = AgenteBiExecucao.query.filter_by(
        id=int(abertura["execucao"]["id"]),
        analise_id=int(abertura["analise_id"]),
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
    ).one_or_none()
    if linha is None:
        raise InterpretacaoErro("identidade_inconsistente", 409)
    db.session.refresh(linha)
    if linha.estado == "adquirida":
        raise InterpretacaoErro("operacao_em_andamento", 409)
    if linha.estado == "concluida":
        return _plano_gravado(escopo, int(abertura["analise_id"])), False
    raise InterpretacaoErro(_codigo(str(linha.ultimo_erro or "")), _status(str(linha.ultimo_erro or ""), linha.estado))


def _plano_gravado(escopo, analise_id: int) -> dict:
    linha = db.session.get(AgenteBiAnalise, int(analise_id))
    if linha is not None:
        db.session.refresh(linha)
    registro = obter_analise(escopo, int(analise_id))
    if registro.get("estado") != "concluida" or registro.get("contrato_versao") != CONTRATO_PATCH_V2:
        raise InterpretacaoErro("provedor_indisponivel")
    documento = registro.get("resultado")
    if linha is None or not isinstance(documento, dict):
        raise InterpretacaoErro("provedor_indisponivel")
    try:
        return revalidar_plano_duravel(linha, documento)
    except ExecucaoErro as exc:
        raise InterpretacaoErro(_codigo(exc.codigo)) from exc


def _status(codigo: str, estado=None) -> int:
    if estado in {"incerta", "adquirida"} or codigo in {"operacao_em_andamento", "efeito_incerto"}:
        return 409
    if codigo in {"autorizacao_indevida", "provedor_nao_autorizado", "admissao_recusada", "isolamento", "transporte_real_bloqueado"}:
        return 403
    if codigo == "nao_encontrado":
        return 404
    return 422


def _instrucao() -> str:
    return "\n".join(
        [
            "Responda somente com um JSON do contrato patch.definir.v2, versao 2, sem markdown.",
            "Situacao: proposto ou esclarecimento.",
            "Acoes: consultar, criar_grafico, alterar_indicador, alterar_agrupamento, alterar_apresentacao, definir_filtro, alterar_periodo.",
            "Preserve a intencao inteira, inclusive negacao e mais de uma instrucao.",
            "Use somente o identificador de grafico e a referencia tecnica recebidos.",
            "Nao invente grafico, coluna, dataset, arquivo, aba ou parametro.",
            "Tipo, evidencia e papel estrutural nao comprovam significado de negocio.",
            "Alias tecnico nao comprova significado de negocio.",
            "Sem associacao na lista recebida, nao atribua significado de negocio.",
            "Se a intencao depender de significado ausente, a situacao e esclarecimento e o motivo e confirmar_rotulos.",
            "Nao escreva valor de celula. Parametro opaco so com referencia recebida. Parametro tipado nao carrega valor.",
            "Referencia temporaria nasce em criar_grafico e so e usada por quem depende dessa criacao.",
            "Nao produza SQL, Python, formula ou codigo.",
            "Operacoes: " + ", ".join(sorted(OPERACOES_EXECUCAO)) + ".",
            "Apresentacoes: " + ", ".join(sorted(GRAFICOS)) + ".",
            "Filtros: " + ", ".join(sorted(_FILTROS)) + ".",
            "Esclarecimentos: " + ", ".join(sorted(_ESCLARECIMENTOS)) + ".",
        ]
    )
