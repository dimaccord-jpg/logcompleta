"""Atualização de datasets compatíveis do AgenteBI (SCRUM-264).

Substitui ou acrescenta linhas de um dataset já ligado a um dashboard.
A compatibilidade é estrutural e semântica: posição, tipos, papéis,
unidades, granularidade e relações usadas pelas definições. Nome parecido
não identifica coluna.

Dado compatível gera uma versão nova pelo fingerprint já existente,
regrava a definição aprovada presa a essa versão e recalcula com o
executor determinístico. Não chama modelo. Dado incompatível, mudança de
significado ou ambiguidade relevante não grava versão nem revisão.

No modo acrescentar, o SHA-256 de cada arquivo entra na linhagem do
dataset. O mesmo lote não é aplicado de novo. O modo substituir segue
o versionamento já existente.
"""
from __future__ import annotations

import copy
import logging

from flask import request
from sqlalchemy import func

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_calculo_service import CalculoErro, calcular_definicao
from app.services.agente_bi_dashboard_service import consultar_dashboard, montar_dashboard
from app.services.agente_bi_definir_service import CONTRATO_DEFINIR, _granularidade_temporal, _unidade
from app.services.agente_bi_ingestao_service import IngestaoErro, _fora_do_contrato, ingerir, persistir_preparacao
from app.services.agente_bi_perfil_semantico_service import perfilar_dataset, perfilar_preparacao
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    obter_analise,
    obter_dashboard,
    obter_dataset,
)

logger = logging.getLogger(__name__)

CONTRATO_ATUALIZACAO = "atualizacao.v1"
ENCAMINHAMENTO = "reinterpretacao_autorizada"
_MODOS = frozenset({"substituir", "acrescentar"})
_BLOQUEIOS = frozenset({"atualizacao_incompativel", "mudanca_significado", "ambiguidade_relevante"})
_CHAVE_LOTES = "lotes_aplicados"
_HEX = frozenset("0123456789abcdef")
_AMBIGUAS = frozenset(
    {
        "separador_ambiguo",
        "divisao_lateral_ambigua",
        "cabecalho_ambiguo",
        "data_ambigua",
        "numero_ambiguo",
    }
)
_EVIDENCIAS_TIPO = frozenset(
    {
        "tipo_data",
        "tipo_hora",
        "componente_horario",
        "tipo_numero_fracionado",
        "tipo_numero_inteiro",
        "tipo_texto",
    }
)
_CONTADORES = (
    "linhas_vazias",
    "linhas_invalidas",
    "linhas_pre_preenchidas",
    "linhas_cabecalho_repetido",
    "formulas",
    "formulas_com_cache",
    "formulas_sem_cache",
    "celulas_preservadas",
)
_CHAVES_REF = ("arquivo", "aba", "regiao", "indice")


class AtualizacaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def atualizar_requisicao(escopo, dashboard_id: int) -> dict:
    """Lê o envio já aceito pela ingestão e atualiza o dashboard do escopo."""
    from app.services.agente_bi_ingestao_service import _ler_requisicao

    arquivos, _chave, limites = _ler_requisicao(campos_form=frozenset({"chave", "modo"}))
    return atualizar_dashboard(
        escopo,
        dashboard_id,
        modo=request.form.get("modo"),
        arquivos=arquivos,
        limites=limites,
    )


def atualizar_dashboard(escopo, dashboard_id: int, *, modo, arquivos, limites=None) -> dict:
    """Recalcula o dashboard se, e somente se, o dado novo permanece compatível."""
    if modo not in _MODOS:
        raise AtualizacaoErro("payload_invalido")
    painel = obter_dashboard(escopo, dashboard_id)
    calcular = _calculo_do_painel(escopo, painel)
    definicao = _definicao_aprovada(escopo, calcular)
    dataset = obter_dataset(escopo, calcular["dataset_id"])
    _exigir_vinculo(painel, calcular, definicao, dataset)
    documento = definicao["resultado"]
    definicoes = documento.get("definicoes")
    if not isinstance(definicoes, list) or not definicoes:
        raise AgenteBiErro("analise_indisponivel", 422)
    colunas, regioes, relacoes, metricas = _referencias(definicoes)
    perfil_base = perfilar_dataset(escopo, dataset["id"])
    try:
        preparada = ingerir(escopo, arquivos, chave=dataset["chave"], limites=limites, persistir=False)
    except IngestaoErro:
        raise
    preparacao = preparada.get("preparacao")
    if not isinstance(preparacao, dict):
        raise AtualizacaoErro("atualizacao_incompativel")
    shas = _shas_do_lote(preparacao) if modo == "acrescentar" else None
    if shas is not None:
        _recusar_lote_repetido(dataset, shas)
    _exigir_esqueleto(dataset["estrutura"], preparacao.get("estrutura"))
    if modo == "acrescentar":
        candidata = _acrescentar(dataset, preparacao, shas)
    else:
        candidata = {
            "estrutura": preparacao.get("estrutura"),
            "perfil": preparacao.get("perfil"),
            "dados_tipados": preparacao.get("dados_tipados"),
            "metadados_preparacao": preparacao.get("metadados_preparacao"),
        }
    perfil_candidato = perfilar_preparacao(candidata)
    _exigir_semantica(perfil_base, perfil_candidato, colunas, regioes, relacoes, metricas)
    _exigir_rotulos(dataset["estrutura"], preparacao.get("estrutura"), regioes)

    gravado = persistir_preparacao(escopo, dataset["chave"], candidata)
    novo = gravado["dataset"]
    if gravado["reutilizado"] and novo["id"] == dataset["id"]:
        aberto = consultar_dashboard(escopo, dashboard_id)
        logger.info(
            "evento=agente_bi_dataset_atualizado usuario_id=%s dashboard_id=%s modo=%s recalculado=0 versao=%s",
            escopo.usuario_id,
            dashboard_id,
            modo,
            dataset["versao"],
        )
        return _resposta(
            modo=modo,
            recalculado=False,
            reutilizado=True,
            definicao_origem_id=definicao["id"],
            definicao_id=definicao["id"],
            dataset_anterior_id=dataset["id"],
            dataset=dataset,
            painel=aberto["dashboard"],
            consulta=aberto["consulta"],
        )

    perfil_novo = perfilar_dataset(escopo, novo["id"])
    _exigir_semantica(perfil_base, perfil_novo, colunas, regioes, relacoes, metricas)
    definicao_nova = _regravar_definicao(escopo, definicao, perfil_novo)
    try:
        calculado = calcular_definicao(
            escopo,
            {"analise_id": definicao_nova["id"], "relacoes_autorizadas": sorted(relacoes)},
        )
    except CalculoErro as exc:
        raise AtualizacaoErro(exc.codigo, exc.status) from exc
    montado = montar_dashboard(escopo, {"analise_id": calculado["calculo"]["id"]})
    logger.info(
        "evento=agente_bi_dataset_atualizado usuario_id=%s dashboard_id=%s modo=%s recalculado=1 versao=%s",
        escopo.usuario_id,
        montado["dashboard"]["id"],
        modo,
        novo["versao"],
    )
    return _resposta(
        modo=modo,
        recalculado=True,
        reutilizado=False,
        definicao_origem_id=definicao["id"],
        definicao_id=definicao_nova["id"],
        dataset_anterior_id=dataset["id"],
        dataset=novo,
        painel=montado["dashboard"],
        consulta=montado["consulta"],
    )


def _resposta(*, modo, recalculado, reutilizado, definicao_origem_id, definicao_id, dataset_anterior_id, dataset, painel, consulta) -> dict:
    return {
        "ok": True,
        "contrato": CONTRATO_ATUALIZACAO,
        "modo": modo,
        "recalculado": recalculado,
        "reutilizado": reutilizado,
        "chamada_anthropic": False,
        "ia_calculo": False,
        "definicao_preservada": True,
        "definicao_origem_id": definicao_origem_id,
        "definicao_id": definicao_id,
        "dataset_anterior_id": dataset_anterior_id,
        "dataset_id": dataset["id"],
        "versao": dataset["versao"],
        "impressao": dataset["impressao"],
        "encaminhamento": None,
        "dashboard": painel,
        "consulta": consulta,
    }


def _calculo_do_painel(escopo, painel) -> dict:
    analise_id = painel.get("analise_id")
    if isinstance(analise_id, bool) or not isinstance(analise_id, int) or analise_id < 1:
        raise AgenteBiErro("analise_indisponivel", 422)
    return obter_analise(escopo, analise_id)


def _definicao_aprovada(escopo, calculo) -> dict:
    memoria = calculo.get("resultado")
    if not isinstance(memoria, dict):
        raise AgenteBiErro("analise_indisponivel", 422)
    definicao_id = memoria.get("origem_definicao_id")
    if isinstance(definicao_id, bool) or not isinstance(definicao_id, int) or definicao_id < 1:
        raise AgenteBiErro("analise_indisponivel", 422)
    definicao = obter_analise(escopo, definicao_id)
    if definicao.get("etapa") != "definicao" or definicao.get("estado") != "concluida":
        raise AgenteBiErro("analise_indisponivel", 422)
    if definicao.get("contrato_versao") != CONTRATO_DEFINIR:
        raise AgenteBiErro("analise_indisponivel", 422)
    documento = definicao.get("resultado")
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_DEFINIR:
        raise AgenteBiErro("analise_indisponivel", 422)
    return definicao


def _exigir_vinculo(painel, calculo, definicao, dataset) -> None:
    if painel.get("chave") != dataset["chave"] or calculo.get("chave") != dataset["chave"]:
        raise AgenteBiErro("identidade_invalida", 409)
    if definicao.get("chave") != dataset["chave"]:
        raise AgenteBiErro("identidade_invalida", 409)
    if calculo.get("dataset_id") != dataset["id"] or calculo.get("versao") != dataset["versao"]:
        raise AgenteBiErro("identidade_invalida", 409)
    if calculo.get("impressao") != dataset["impressao"]:
        raise AgenteBiErro("identidade_invalida", 409)
    datasets = painel.get("datasets")
    if not isinstance(datasets, list) or len(datasets) != 1:
        raise AtualizacaoErro("atualizacao_incompativel")
    vinculo = datasets[0]
    if not isinstance(vinculo, dict):
        raise AtualizacaoErro("atualizacao_incompativel")
    if (
        vinculo.get("dataset_id") != dataset["id"]
        or vinculo.get("versao") != dataset["versao"]
        or vinculo.get("impressao") != dataset["impressao"]
    ):
        raise AgenteBiErro("identidade_invalida", 409)


def _referencias(definicoes) -> tuple[set, set, set, list]:
    colunas = set()
    regioes = set()
    relacoes = set()
    metricas = []
    for item in definicoes:
        if not isinstance(item, dict):
            raise AgenteBiErro("analise_indisponivel", 422)
        metrica = item.get("metrica")
        if isinstance(metrica, dict):
            coord = _coord(metrica.get("coluna"))
            if coord is not None:
                colunas.add(coord)
                regioes.add(coord[:3])
                metricas.append((coord, metrica.get("operacao")))
        for grupo in (item.get("dimensoes"), item.get("filtros")):
            if not isinstance(grupo, list):
                continue
            for parte in grupo:
                if not isinstance(parte, dict):
                    continue
                coord = _coord(parte.get("coluna"))
                if coord is not None:
                    colunas.add(coord)
                    regioes.add(coord[:3])
        relacionamentos = item.get("relacionamentos")
        if not isinstance(relacionamentos, list):
            continue
        for relacao in relacionamentos:
            if not isinstance(relacao, dict):
                continue
            indice = relacao.get("indice")
            if isinstance(indice, bool) or not isinstance(indice, int) or indice < 0:
                raise AtualizacaoErro("atualizacao_incompativel")
            relacoes.add(indice)
    if not colunas:
        raise AtualizacaoErro("atualizacao_incompativel")
    return colunas, regioes, relacoes, metricas


def _exigir_esqueleto(base, novo) -> None:
    if not isinstance(base, dict) or not isinstance(novo, dict):
        _bloquear("atualizacao_incompativel")
    esquerda = _mapa_estrutura(base)
    direita = _mapa_estrutura(novo)
    if set(esquerda) != set(direita):
        _bloquear("atualizacao_incompativel")
    for chave, regiao in esquerda.items():
        outra = direita[chave]
        if regiao.get("coluna_inicio") != outra.get("coluna_inicio") or regiao.get("coluna_fim") != outra.get("coluna_fim"):
            _bloquear("atualizacao_incompativel")
        if regiao.get("tipos_fisicos") != outra.get("tipos_fisicos"):
            _bloquear("atualizacao_incompativel")


def _exigir_semantica(base, candidato, colunas, regioes, relacoes, metricas) -> None:
    antigas = _mapa_colunas(base)
    novas = _mapa_colunas(candidato)
    papeis_antigos = _mapa_papeis(base)
    papeis_novos = _mapa_papeis(candidato)
    for chave in regioes:
        if chave not in papeis_antigos or chave not in papeis_novos:
            _bloquear("atualizacao_incompativel")
        if papeis_antigos[chave] != papeis_novos[chave]:
            _bloquear("mudanca_significado")
    for chave in colunas:
        if chave not in antigas or chave not in novas:
            _bloquear("atualizacao_incompativel")
        if _assinatura_tipo(antigas[chave]) != _assinatura_tipo(novas[chave]):
            if antigas[chave].get("tipos") != novas[chave].get("tipos"):
                _bloquear("atualizacao_incompativel")
            _bloquear("mudanca_significado")
        if _granularidade_temporal(antigas[chave]) != _granularidade_temporal(novas[chave]):
            _bloquear("mudanca_significado")
    for chave, operacao in metricas:
        if _unidade(operacao, antigas.get(chave)) != _unidade(operacao, novas.get(chave)):
            _bloquear("mudanca_significado")
    _exigir_relacoes(base, candidato, relacoes)


def _exigir_relacoes(base, candidato, indices) -> None:
    if not indices:
        return
    antigas = base.get("relacoes") if isinstance(base.get("relacoes"), list) else []
    novas = candidato.get("relacoes") if isinstance(candidato.get("relacoes"), list) else []
    for indice in indices:
        if indice >= len(antigas) or indice >= len(novas):
            _bloquear("atualizacao_incompativel")
        if _assinatura_relacao(antigas[indice]) != _assinatura_relacao(novas[indice]):
            _bloquear("mudanca_significado")


def _exigir_rotulos(base, novo, regioes) -> None:
    if not isinstance(novo, dict):
        _bloquear("ambiguidade_relevante")
    if _tem_ambiguidade(novo, regioes):
        _bloquear("ambiguidade_relevante")
    esquerda = _mapa_estrutura(base)
    direita = _mapa_estrutura(novo)
    for chave in regioes:
        if chave not in esquerda or chave not in direita:
            _bloquear("atualizacao_incompativel")
        if esquerda[chave].get("cabecalho_deteccao") != "inequivoca" or direita[chave].get("cabecalho_deteccao") != "inequivoca":
            _bloquear("ambiguidade_relevante")
        rotulo_base = _rotulos(esquerda[chave])
        rotulo_novo = _rotulos(direita[chave])
        if rotulo_base is None or rotulo_novo is None or rotulo_base != rotulo_novo:
            _bloquear("ambiguidade_relevante")


def _tem_ambiguidade(estrutura, regioes) -> bool:
    itens = estrutura.get("ambiguidades")
    if not isinstance(itens, list):
        return False
    for item in itens:
        if not isinstance(item, dict) or item.get("codigo") not in _AMBIGUAS:
            continue
        regiao = item.get("regiao")
        chave = (item.get("arquivo"), item.get("aba"), regiao)
        if regiao is None or chave in regioes:
            return True
    return False


def _acrescentar(dataset, preparacao, shas: list[str]) -> dict:
    estrutura = copy.deepcopy(dataset["estrutura"])
    dados = copy.deepcopy(dataset["dados_tipados"])
    if not isinstance(estrutura, dict) or not isinstance(dados, list):
        _bloquear("atualizacao_incompativel")
    novos = preparacao.get("dados_tipados")
    nova_estrutura = preparacao.get("estrutura")
    if not isinstance(novos, list) or not isinstance(nova_estrutura, dict):
        _bloquear("atualizacao_incompativel")
    regioes = _mapa_estrutura(estrutura)
    novas_regioes = _mapa_estrutura(nova_estrutura)
    for chave, regiao in regioes.items():
        outra = novas_regioes[chave]
        atuais = [item for item in dados if _coord_linha(item) == chave]
        ultima = max((_inteiro(item.get("linha")) or 0 for item in atuais), default=_inteiro(regiao.get("linha_fim")) or 0)
        acrescentados = [item for item in novos if _coord_linha(item) == chave]
        acrescentados.sort(key=lambda item: _inteiro(item.get("linha")) or 0)
        for deslocamento, registro in enumerate(acrescentados, start=1):
            copia = copy.deepcopy(registro)
            copia["linha"] = ultima + deslocamento
            dados.append(copia)
        regiao["linhas_elegiveis"] = len(atuais) + len(acrescentados)
        regiao["linhas_ocupadas"] = _inteiro(regiao.get("linhas_ocupadas")) + _inteiro(outra.get("linhas_ocupadas"))
        regiao["linha_fim"] = max(_inteiro(regiao.get("linha_fim")), ultima + len(acrescentados))
        for campo in _CONTADORES:
            regiao[campo] = _inteiro(regiao.get(campo)) + _inteiro(outra.get(campo))
        if isinstance(regiao.get("formulas_observadas"), list) and isinstance(outra.get("formulas_observadas"), list):
            regiao["formulas_observadas"] = list(regiao["formulas_observadas"]) + list(outra["formulas_observadas"])
    perfil_base = dataset.get("perfil") if isinstance(dataset.get("perfil"), dict) else {}
    perfil_novo = preparacao.get("perfil") if isinstance(preparacao.get("perfil"), dict) else {}
    metadados = dataset.get("metadados_preparacao")
    if not isinstance(metadados, dict):
        _bloquear("atualizacao_incompativel")
    metadados = _registrar_lotes(metadados, shas)
    if _fora_do_contrato(metadados):
        _bloquear("atualizacao_incompativel")
    return {
        "estrutura": estrutura,
        "perfil": {
            "arquivos": len(estrutura.get("arquivos") or []),
            "abas": len(estrutura.get("abas") or []),
            "regioes": len(estrutura.get("regioes") or []),
            "celulas": _inteiro(perfil_base.get("celulas")) + _inteiro(perfil_novo.get("celulas")),
            "linhas_elegiveis": sum(_inteiro(item.get("linhas_elegiveis")) for item in estrutura.get("regioes") or []),
            "formulas": sum(_inteiro(item.get("formulas")) for item in estrutura.get("regioes") or []),
            "ambigua": bool(perfil_base.get("ambigua") or perfil_novo.get("ambigua") or estrutura.get("ambiguidades")),
        },
        "dados_tipados": dados,
        "metadados_preparacao": metadados,
    }


def _regravar_definicao(escopo, definicao, perfil) -> dict:
    dataset = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    documento = copy.deepcopy(definicao["resultado"])
    documento["dataset_id"] = dataset.get("id")
    documento["dataset_versao"] = dataset.get("versao")
    documento["dataset_impressao"] = dataset.get("impressao")
    documento["vinculo_preparacao"] = perfil.get("vinculo_preparacao")
    documento["chamada_anthropic"] = False
    return _com_rodada(
        escopo,
        definicao["chave"],
        lambda rodada: criar_analise(
            escopo,
            {
                "chave": definicao["chave"],
                "rodada": rodada,
                "dataset_id": dataset.get("id"),
                "versao": dataset.get("versao"),
                "impressao": dataset.get("impressao"),
                "etapa": "definicao",
                "estado": "concluida",
                "contexto": {
                    "contrato": "contexto.definir.v1",
                    "definicao_origem_id": definicao["id"],
                    "atualizacao": CONTRATO_ATUALIZACAO,
                    "chamada_anthropic": False,
                },
                "regras_negocio": {"dsl": documento.get("dsl"), "execucao": "nao_executada"},
                "esclarecimentos": {"bloqueios": []},
                "contrato_versao": CONTRATO_DEFINIR,
                "resultado": documento,
            },
        ),
    )


def _com_rodada(escopo, chave: str, montar):
    erro = None
    for _ in range(2):
        try:
            return montar(_proxima_rodada(escopo, chave))
        except AgenteBiErro as exc:
            erro = exc
            if exc.codigo not in {"rodada_existente", "conflito_versao"}:
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


def _mapa_estrutura(estrutura) -> dict:
    if not isinstance(estrutura, dict) or not isinstance(estrutura.get("regioes"), list):
        _bloquear("atualizacao_incompativel")
    saida = {}
    for regiao in estrutura["regioes"]:
        if not isinstance(regiao, dict):
            _bloquear("atualizacao_incompativel")
        chave = (regiao.get("arquivo"), regiao.get("aba"), regiao.get("indice"))
        if chave in saida:
            _bloquear("atualizacao_incompativel")
        saida[chave] = regiao
    return saida


def _mapa_colunas(perfil) -> dict:
    if not isinstance(perfil, dict) or not isinstance(perfil.get("colunas"), list):
        _bloquear("atualizacao_incompativel")
    saida = {}
    for coluna in perfil["colunas"]:
        if not isinstance(coluna, dict):
            _bloquear("atualizacao_incompativel")
        chave = (coluna.get("arquivo"), coluna.get("aba"), coluna.get("regiao"), coluna.get("indice"))
        saida[chave] = coluna
    return saida


def _mapa_papeis(perfil) -> dict:
    if not isinstance(perfil, dict) or not isinstance(perfil.get("regioes"), list):
        _bloquear("atualizacao_incompativel")
    saida = {}
    for regiao in perfil["regioes"]:
        if not isinstance(regiao, dict):
            _bloquear("atualizacao_incompativel")
        saida[(regiao.get("arquivo"), regiao.get("aba"), regiao.get("regiao"))] = regiao.get("papel")
    return saida


def _assinatura_tipo(coluna) -> tuple:
    """Tipo físico e classe analítica estável.

    A hipótese de identificador depende da unicidade da amostra. Acrescentar
    linhas pode repeti-la sem mudar data, quantidade, valor ou texto.
    """
    evidencias = set(coluna.get("evidencias") or [])
    return (
        tuple(coluna.get("tipos") or []),
        _classe_estavel(coluna),
        tuple(sorted(evidencias & _EVIDENCIAS_TIPO)),
    )


def _classe_estavel(coluna) -> str:
    principal = coluna.get("dimensao_principal")
    if principal != "identificador":
        return principal
    estaveis = [item for item in coluna.get("dimensoes") or [] if item != "identificador"]
    return estaveis[0] if estaveis else "indefinida"


def _assinatura_relacao(relacao) -> tuple:
    if not isinstance(relacao, dict):
        _bloquear("atualizacao_incompativel")
    return (
        _coord(relacao.get("lado_unico")),
        _coord(relacao.get("lado_associado")),
        relacao.get("cardinalidade"),
        relacao.get("cardinalidade_observada"),
    )


def _rotulos(regiao) -> tuple | None:
    linha = regiao.get("cabecalho_linha")
    for candidato in regiao.get("candidatos") or []:
        if not isinstance(candidato, dict) or candidato.get("linha") != linha:
            continue
        rotulos = candidato.get("rotulos")
        if isinstance(rotulos, list):
            return tuple(rotulos)
    return None


def _coord(ref):
    if not isinstance(ref, dict):
        return None
    saida = []
    for chave in _CHAVES_REF:
        numero = ref.get(chave)
        if isinstance(numero, bool) or not isinstance(numero, int):
            return None
        saida.append(numero)
    return tuple(saida)


def _coord_linha(registro) -> tuple:
    if not isinstance(registro, dict):
        return (None, None, None)
    return (registro.get("arquivo"), registro.get("aba"), registro.get("regiao"))


def _inteiro(valor) -> int:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return 0
    return valor


def _sha_valido(valor) -> bool:
    return isinstance(valor, str) and len(valor) == 64 and all(caractere in _HEX for caractere in valor)


def _shas_do_lote(preparacao) -> list[str]:
    """SHA-256 já publicado pela ingestão. A interface envia um arquivo."""
    estrutura = preparacao.get("estrutura") if isinstance(preparacao, dict) else None
    arquivos = estrutura.get("arquivos") if isinstance(estrutura, dict) else None
    if not isinstance(arquivos, list) or not arquivos:
        _bloquear("atualizacao_incompativel")
    vistos = []
    for arquivo in arquivos:
        digest = arquivo.get("sha256") if isinstance(arquivo, dict) else None
        if not _sha_valido(digest):
            _bloquear("atualizacao_incompativel")
        if digest not in vistos:
            vistos.append(digest)
    return vistos


def _linhagem_de_lotes(dataset) -> list[str]:
    """Lotes já acrescentados neste dataset. O arquivo da versão base não entra aqui."""
    metadados = dataset.get("metadados_preparacao") if isinstance(dataset, dict) else None
    if not isinstance(metadados, dict) or _CHAVE_LOTES not in metadados:
        return []
    lotes = metadados.get(_CHAVE_LOTES)
    if not isinstance(lotes, list) or not all(_sha_valido(item) for item in lotes):
        _bloquear("atualizacao_incompativel")
    return list(lotes)


def _recusar_lote_repetido(dataset, shas: list[str]) -> None:
    linhagem = set(_linhagem_de_lotes(dataset))
    if any(digest in linhagem for digest in shas):
        _bloquear("lote_ja_aplicado")


def _registrar_lotes(metadados: dict, shas: list[str]) -> dict:
    saida = copy.deepcopy(metadados)
    atuais = list(saida.get(_CHAVE_LOTES) or [])
    for digest in shas:
        if digest not in atuais:
            atuais.append(digest)
    saida[_CHAVE_LOTES] = atuais
    return saida


def _bloquear(codigo: str) -> None:
    logger.info("evento=agente_bi_atualizacao_bloqueada codigo=%s", codigo)
    raise AtualizacaoErro(codigo)
