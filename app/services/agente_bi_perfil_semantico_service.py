"""Perfil semântico determinístico do AgenteBI (SCRUM-255).

Lê um dataset já preparado (`preparar.v1`) e devolve um perfil novo.
Não relê XLSX/CSV, não grava, não altera o dataset, não chama provedor
e não executa junção.

Hipótese estrutural não é significado de negócio. Rótulos de coluna não
provam relação nem frete, motorista, transportadora ou custo.

Relações candidatas têm orçamento. O teto de pares examinados e o teto
de relações publicadas seguem o mesmo padrão da ingestão: configuração
pode reduzir, não ultrapassa o limite. A amostra prioriza chave com
chave e espalha regiões e faixas da grade. Análise incompleta fica
explícita e não é conclusão sobre pares que não foram examinados.

Dataset recuperado só é perfilado se a impressão integral, recalculada
com o algoritmo da gravação, coincidir com a impressão declarada e com
a impressão persistida. Preparação ainda em memória não exige esse hash.

A representação para uma futura IA é separada, sem valores de célula.
Ela fica armazenada só como artefato local. A finalidade `bi_logistico`
é a da governança oficial. Cadastrá-la não valida o contrato de saída
nem declara este contexto apto para a Anthropic: a transferência do
perfil permanece bloqueada.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
from collections import Counter

from app.models import AgenteBiDataset
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    EscopoAgenteBi,
    _obter_no_escopo,
    calcular_impressao_dataset,
    obter_dataset,
)
from app.services.cleiton_ai_data_governance import (
    CLASSIFIER_NAME,
    CONTENT_TYPE_DOCUMENT,
    DECISION_ALLOW,
    DECISION_BLOCK,
    PURPOSE_REDACAO,
    SUPPORTED_PURPOSES,
    govern_outbound_content,
    governance_result_is_coherent,
)
from app.services.cleiton_ai_privacy_classifier import (
    CATEGORY_API_KEY,
    CATEGORY_BANK,
    CATEGORY_CARD,
    CATEGORY_CNPJ,
    CATEGORY_CPF,
    CATEGORY_EMAIL,
    CATEGORY_FILENAME,
    CATEGORY_FISCAL_KEY,
    CATEGORY_PERSON_NAME,
    CATEGORY_PHONE,
    CATEGORY_SECRET,
    CATEGORY_STRIPE_ID,
    CATEGORY_TECHNICAL_ID,
    CATEGORY_TOKEN,
    DECISION_MINIMIZE,
    classification_invariants_ok,
    classify_text,
)

logger = logging.getLogger(__name__)

CONTRATO_PERFIL = "perfil.semantico.v1"
CONTRATO_CONTEXTO = "contexto.ia.candidato.v1"
CONTRATO_PREPARACAO = "preparar.v1"
FINALIDADE_TRANSFERENCIA = "bi_logistico"
CONTRATO_SAIDA_VALIDADO = False

_STATUS = "hipotese_estrutural"
_SIGNIFICADO = "nao_confirmado"
_JUNCAO = "nao_executada"

_PAPEL_TRANSACIONAL = "registro_transacional"
_PAPEL_AUXILIAR = "tabela_auxiliar"
_PAPEL_CADASTRO = "cadastro"
_PAPEL_LISTA = "lista_referencia"
_PAPEL_APRESENTACAO = "planilha_apresentacao"
_INDEFINIDA = "indefinida"

_DIM_DATA = "data"
_DIM_TEMPO = "tempo"
_DIM_QUANTIDADE = "quantidade"
_DIM_VALOR = "valor"
_DIM_IDENTIFICADOR = "identificador"

_CARD_UM_UM = "1:1"
_CARD_UM_N = "1:N"
_CARD_DESCONHECIDA = "desconhecida"

# Mesmo desenho de `resolver_limites`: o ambiente só reduz o teto.
TETO_PARES_EXAMINADOS = 2_000
TETO_RELACOES_PUBLICADAS = 64
_FAIXAS_RELACAO = 8
CRITERIO_SELECAO_RELACOES = "evidencia_estrutural_cobertura_equilibrada"

_DETECCAO = frozenset({"inequivoca", "ambigua", "ausente"})
_CONFIANCA = frozenset({"alta", "media", "nenhuma"})
_TIPOS = frozenset(
    {
        "ausente",
        "texto",
        "numero",
        "data",
        "hora",
        "booleano",
        "erro",
        "formula",
        "numero_pendente",
        "data_pendente",
        "nao_suportado",
        "misto",
        "vazio",
    }
)
_TIPOS_CHAVE = frozenset({"texto", "numero", "data", "hora"})
_CODIGOS_AMBIGUIDADE = frozenset(
    {
        "separador_ambiguo",
        "divisao_lateral_ambigua",
        "cabecalho_ambiguo",
        "data_ambigua",
        "numero_ambiguo",
    }
)
_ALERTAS = frozenset(
    {
        "divisao_lateral_ambigua",
        "cabecalho_ambiguo",
        "cabecalho_ausente",
        "linha_pre_preenchida",
        "linha_invalida",
        "cabecalho_repetido",
        "tipo_misto",
        "coluna_parcialmente_vazia",
        "linha_parcialmente_vazia",
        "formula_sem_cache",
        "sem_regiao_tabular",
        "regioes_multiplas",
        "aba_oculta",
        "indicio_apresentacao",
        "celulas_mescladas",
        "separador_ambiguo",
        "data_ambigua",
        "numero_ambiguo",
    }
)
_CATEGORIAS = frozenset(
    {
        CATEGORY_CPF,
        CATEGORY_CNPJ,
        CATEGORY_EMAIL,
        CATEGORY_PHONE,
        CATEGORY_FISCAL_KEY,
        CATEGORY_SECRET,
        CATEGORY_API_KEY,
        CATEGORY_TOKEN,
        CATEGORY_CARD,
        CATEGORY_BANK,
        CATEGORY_STRIPE_ID,
        CATEGORY_PERSON_NAME,
        CATEGORY_FILENAME,
        CATEGORY_TECHNICAL_ID,
    }
)
_MOTIVOS = frozenset(
    {
        "unsupported_purpose",
        "classifier_unavailable",
        "classifier_exception",
        "inconsistent_governance_result",
        "invalid_decision",
        "missing_safe_content",
        "minimizacao_nao_comprovada",
        "representacao_alterada",
        "governanca_indisponivel",
        "opaco",
    }
)
_TOKENS = frozenset(
    {
        CONTRATO_PERFIL,
        CONTRATO_CONTEXTO,
        CONTRATO_PREPARACAO,
        FINALIDADE_TRANSFERENCIA,
        CLASSIFIER_NAME,
        PURPOSE_REDACAO,
        _STATUS,
        _SIGNIFICADO,
        _JUNCAO,
        _PAPEL_TRANSACIONAL,
        _PAPEL_AUXILIAR,
        _PAPEL_CADASTRO,
        _PAPEL_LISTA,
        _PAPEL_APRESENTACAO,
        _INDEFINIDA,
        _DIM_DATA,
        _DIM_TEMPO,
        _DIM_QUANTIDADE,
        _DIM_VALOR,
        _DIM_IDENTIFICADOR,
        _CARD_UM_UM,
        _CARD_UM_N,
        _CARD_DESCONHECIDA,
        "opaco",
        "opaca",
        "local",
        "bloqueada",
        "comprovada",
        "nao_comprovada",
        "local_nao_autorizada",
        "impedida",
        "allowed",
        "minimized",
        "blocked",
        "perfil_json_integra_a_impressao_imutavel",
        "texto_nao_literal",
        "codigo_de_ambiguidade_opaco",
        "classificador_existente_nao_cobre_todos_os_identificadores",
        "placa",
        "rotulos_nao_confirmados",
        "divisao_lateral_nao_resolvida",
        "chave_incompleta",
        "contagem_divergente",
        "tipo_data",
        "tipo_hora",
        "tipo_numero_inteiro",
        "tipo_numero_fracionado",
        "tipo_texto",
        "componente_horario",
        "unicidade_observada",
        "valores_repetidos",
        "sobreposicao_de_valores",
        "rotulo_nao_utilizado",
        "varios_registros",
        "varias_colunas",
        "largura_estreita",
        "aba_oculta",
        "indicio_auxiliar",
        "indicio_apresentacao",
        "contem_grafico",
        "celulas_mescladas",
        "coluna_unica",
        "sem_registros_elegiveis",
        "com_valor_sobre_celulas_elegiveis",
        "tupla_identica_de_celulas_preparadas",
        "tipo_estavel_sem_ausencia_e_valores_distintos",
        "sobreposicao_minima_sem_usar_rotulo",
        "codigo_publicado_pela_preparacao",
        "contagem_publicada_linhas_pre_preenchidas",
        "cache_ausente_nas_formulas_observadas",
        "mais_de_um_tipo_efetivo",
        "contrato",
        "amostras",
        "regioes",
        "arquivo",
        "aba",
        "regiao",
        "papel",
        "linhas",
        "cabecalho",
        "qualidade",
        "com_valor",
        "celulas",
        "duplicidades",
        "datas_ambiguas",
        "formulas_sem_cache",
        "pre_preenchidas",
        "relacoes",
        "orcamento_relacoes",
        "analise_relacoes_parcial",
        "limite_relacoes_atingido",
        "pares_examinados",
        "relacoes_publicadas",
        "criterio_selecao",
        CRITERIO_SELECAO_RELACOES,
        "cardinalidade",
        "compartilhados",
        "risco_duplicacao",
        "confirmacao_usuario",
        "lado_unico",
        "lado_associado",
        "coluna",
        "dimensoes",
        "categorias",
        "armazenamento",
        "amostras_de_linhas",
        "valores_de_celula",
        "representacao",
        "autorizacao",
        "transferencia",
        "autorizado_para_provedor",
        "apto_anthropic",
        "contrato_saida_validado",
        "finalidade_aprovada",
        "decisao",
        "cadastrar_finalidade_nao_autoriza",
        "inequivoca",
        "ambigua",
        "ausente",
        "alta",
        "media",
        "nenhuma",
        *_TIPOS,
        *_CODIGOS_AMBIGUIDADE,
        *_ALERTAS,
        *_CATEGORIAS,
        *_MOTIVOS,
        DECISION_ALLOW,
        DECISION_MINIMIZE,
        DECISION_BLOCK,
    }
)
_HEX = frozenset("0123456789abcdef")
_CODIGO = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_")
_CHAVES_AMBIGUIDADE = ("arquivo", "aba", "regiao", "coluna", "codigo")


class PerfilErro(Exception):
    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(codigo)


def perfilar_dataset(escopo: EscopoAgenteBi, dataset_id: int) -> dict:
    """Recupera o dataset no escopo, confere a impressão integral e perfila sem gravar."""
    corpo = obter_dataset(escopo, dataset_id)
    if (
        corpo.get("conta_id") != escopo.conta_id
        or corpo.get("franquia_id") != escopo.franquia_id
        or corpo.get("usuario_id") != escopo.usuario_id
    ):
        raise AgenteBiErro("nao_encontrado", 404)
    _exigir_impressao_integral(escopo, dataset_id, corpo)
    return perfilar_preparacao(corpo, escopo=escopo)


def _exigir_impressao_integral(escopo: EscopoAgenteBi, dataset_id: int, corpo: dict) -> None:
    """Recalcula a impressão da gravação e recusa corpo divergente.

    Uma serialização canônica basta. A consulta seguinte lê só a versão
    e a impressão já persistidas; não regrava nem cria versão.
    """
    if not isinstance(corpo, dict):
        raise AgenteBiErro("integridade_dados", 409)
    try:
        calculada = calcular_impressao_dataset(
            chave=corpo["chave"],
            origem=corpo["origem"],
            origem_ref=corpo["origem_ref"],
            versao=corpo["versao"],
            estrutura=corpo["estrutura"],
            perfil=corpo["perfil"],
            dados_tipados=corpo["dados_tipados"],
            metadados_preparacao=corpo["metadados_preparacao"],
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise AgenteBiErro("integridade_dados", 409) from exc
    declarada = corpo.get("impressao")
    if not isinstance(declarada, str) or not _hex64(declarada) or declarada != calculada:
        raise AgenteBiErro("integridade_dados", 409)
    linha = _obter_no_escopo(AgenteBiDataset, escopo, dataset_id)
    if linha is None or linha.versao != corpo.get("versao") or linha.impressao != calculada:
        raise AgenteBiErro("integridade_dados", 409)


def perfilar_preparacao(preparacao: dict, *, escopo: EscopoAgenteBi | None = None) -> dict:
    """Monta o perfil a partir da preparação. Não muta o dicionário recebido."""
    if not isinstance(preparacao, dict):
        raise PerfilErro("contrato_preparacao_ausente")
    _conferir_escopo(preparacao, escopo)
    estrutura = preparacao.get("estrutura")
    dados = preparacao.get("dados_tipados")
    metadados = preparacao.get("metadados_preparacao")
    if not isinstance(estrutura, dict) or not isinstance(dados, list):
        raise PerfilErro("contrato_preparacao_ausente")
    if estrutura.get("contrato") != CONTRATO_PREPARACAO:
        raise PerfilErro("contrato_preparacao_ausente")
    if isinstance(metadados, dict) and metadados.get("versao_preparacao") not in (None, CONTRATO_PREPARACAO):
        raise PerfilErro("contrato_preparacao_ausente")
    if not isinstance(estrutura.get("regioes"), list):
        raise PerfilErro("dados_invalidos")

    ambiguidades, opacas = _copiar_ambiguidades(estrutura.get("ambiguidades"))
    analise = _analisar(estrutura, dados, ambiguidades)
    transformacoes = []
    if opacas:
        transformacoes.append(
            {"tipo": "codigo_de_ambiguidade_opaco", "motivo": "texto_nao_literal", "ocorrencias": opacas}
        )
    perfil = {
        "contrato": CONTRATO_PERFIL,
        "vinculo_preparacao": _vinculo(estrutura, dados, metadados if isinstance(metadados, dict) else {}),
        "escopo": _escopo_publico(escopo),
        "dataset": _referencia_dataset(preparacao),
        "persistencia": {"gravada": False, "motivo": "perfil_json_integra_a_impressao_imutavel"},
        "dependencias": {
            "armazenamento": "SCRUM-252",
            "perfil_persistido": False,
            "motivo": "perfil_json_integra_a_impressao_imutavel",
        },
        "substituicao_dos_dados": False,
        "registros_excluidos": 0,
        "registros_alterados": 0,
        "juncao": _JUNCAO,
        "transformacoes": transformacoes,
        "arquivos": len(estrutura.get("arquivos") or []),
        "abas": len(estrutura.get("abas") or []),
        "regioes": analise["regioes"],
        "colunas": analise["colunas"],
        "qualidade": analise["qualidade"],
        "relacoes": analise["relacoes"],
        "orcamento_relacoes": analise["orcamento_relacoes"],
        "ambiguidades": ambiguidades,
        "governanca": _governanca(preparacao, analise, escopo, bloquear_contexto=bool(transformacoes)),
    }
    _registrar(escopo, perfil)
    return perfil


def _conferir_escopo(preparacao: dict, escopo: EscopoAgenteBi | None) -> None:
    tem_dono = any(chave in preparacao for chave in ("conta_id", "franquia_id", "usuario_id"))
    if tem_dono and escopo is None:
        raise PerfilErro("escopo_divergente")
    if escopo is None or not tem_dono:
        return
    if (
        preparacao.get("conta_id") != escopo.conta_id
        or preparacao.get("franquia_id") != escopo.franquia_id
        or preparacao.get("usuario_id") != escopo.usuario_id
    ):
        raise PerfilErro("escopo_divergente")


def _escopo_publico(escopo: EscopoAgenteBi | None):
    if escopo is None:
        return None
    return {
        "conta_id": escopo.conta_id,
        "franquia_id": escopo.franquia_id,
        "usuario_id": escopo.usuario_id,
    }


def _referencia_dataset(preparacao: dict) -> dict:
    referencia = {}
    identificador = preparacao.get("id")
    versao = preparacao.get("versao")
    impressao = preparacao.get("impressao")
    if _inteiro_positivo(identificador):
        referencia["id"] = identificador
    if _inteiro_positivo(versao):
        referencia["versao"] = versao
    if isinstance(impressao, str) and _hex64(impressao):
        referencia["impressao"] = impressao
    return referencia


def _copiar_ambiguidades(bruto) -> tuple[list, int]:
    if bruto is None:
        return [], 0
    if not isinstance(bruto, list):
        raise PerfilErro("dados_invalidos")
    saida = []
    opacas = 0
    for item in bruto:
        if not isinstance(item, dict):
            raise PerfilErro("dados_invalidos")
        copia = {}
        for chave in _CHAVES_AMBIGUIDADE:
            if chave not in item:
                continue
            valor = item[chave]
            if chave == "codigo":
                if valor in _CODIGOS_AMBIGUIDADE:
                    copia["codigo"] = valor
                else:
                    copia["codigo"] = "opaco"
                    opacas += 1
                continue
            if valor is not None and not _inteiro_nao_bool(valor):
                raise PerfilErro("dados_invalidos")
            copia[chave] = valor
        saida.append(copia)
    return saida, opacas


def _analisar(estrutura: dict, dados: list, ambiguidades: list) -> dict:
    abas = {}
    for aba in estrutura.get("abas") or []:
        if isinstance(aba, dict):
            abas[(aba.get("arquivo"), aba.get("indice"))] = aba
    grupos: dict[tuple, list] = {}
    for registro in dados:
        if not isinstance(registro, dict) or not isinstance(registro.get("valores"), list):
            raise PerfilErro("dados_invalidos")
        chave = (registro.get("arquivo"), registro.get("aba"), registro.get("regiao"))
        grupos.setdefault(chave, []).append(registro)

    regioes = []
    colunas = []
    qualidade_regioes = []
    for regiao in estrutura["regioes"]:
        if not isinstance(regiao, dict):
            raise PerfilErro("dados_invalidos")
        chave = (regiao.get("arquivo"), regiao.get("aba"), regiao.get("indice"))
        linhas = list(grupos.get(chave, []))
        aba = abas.get((regiao.get("arquivo"), regiao.get("aba")), {})
        montada, cols, qualidade = _regiao(regiao, aba, linhas, ambiguidades)
        regioes.append(montada)
        colunas.extend(cols)
        qualidade_regioes.append(qualidade)

    relacoes, orcamento = _relacoes(colunas, regioes)
    for coluna in colunas:
        coluna.pop("_identidades", None)
        coluna.pop("_tipo_chave", None)
        coluna.pop("_codigos", None)
    return {
        "regioes": regioes,
        "colunas": colunas,
        "qualidade": {
            "criterios": _criterios(),
            "por_regiao": qualidade_regioes,
            "totais": _totais(qualidade_regioes),
        },
        "relacoes": relacoes,
        "orcamento_relacoes": orcamento,
    }


def _criterios() -> dict:
    return {
        "completude": "com_valor_sobre_celulas_elegiveis",
        "duplicidade": "tupla_identica_de_celulas_preparadas",
        "chave": "tipo_estavel_sem_ausencia_e_valores_distintos",
        "relacao": "sobreposicao_minima_sem_usar_rotulo",
        "data_ambigua": "codigo_publicado_pela_preparacao",
        "linha_pre_preenchida": "contagem_publicada_linhas_pre_preenchidas",
        "formula_sem_cache": "cache_ausente_nas_formulas_observadas",
        "dominio": "mais_de_um_tipo_efetivo",
    }


def _regiao(regiao: dict, aba: dict, linhas: list, ambiguidades: list) -> tuple[dict, list, dict]:
    arquivo = regiao.get("arquivo")
    aba_indice = regiao.get("aba")
    indice = regiao.get("indice")
    largura = _largura(regiao, linhas)
    coluna_inicio = regiao.get("coluna_inicio") if _inteiro_nao_bool(regiao.get("coluna_inicio")) else 1
    stats = [_coluna(linhas, posicao, coluna_inicio + posicao) for posicao in range(largura)]
    colunas = []
    for posicao, item in enumerate(stats):
        codigos = _codigos_da_coluna(ambiguidades, arquivo, aba_indice, indice, posicao)
        montada = _hipotese_coluna(arquivo, aba_indice, indice, posicao, item, codigos)
        colunas.append(montada)

    deteccao = regiao.get("cabecalho_deteccao")
    deteccao_publica = deteccao if deteccao in _DETECCAO else "opaco"
    confianca = regiao.get("cabecalho_confianca")
    confianca_publica = confianca if confianca in _CONFIANCA else "opaca"
    papel, candidatos, evidencias = _hipotese_regiao(aba, regiao, linhas, colunas)
    estruturais = _codigos_da_regiao(ambiguidades, arquivo, aba_indice, indice)
    if deteccao_publica == "ambigua" and "cabecalho_ambiguo" not in estruturais:
        estruturais.append("cabecalho_ambiguo")
    if _divisao_ambigua(regiao) and "divisao_lateral_ambigua" not in estruturais:
        estruturais.append("divisao_lateral_ambigua")

    linhas_preparadas = regiao.get("linhas_elegiveis")
    if not _inteiro_nao_bool(linhas_preparadas):
        linhas_preparadas = None
    pre_preenchidas = regiao.get("linhas_pre_preenchidas")
    if not _inteiro_nao_bool(pre_preenchidas):
        pre_preenchidas = None
    qualidade = _qualidade(regiao, linhas, stats, colunas, largura, estruturais, pre_preenchidas)
    publica = {
        "arquivo": arquivo,
        "aba": aba_indice,
        "regiao": indice,
        "papel": papel,
        "candidatos": candidatos,
        "status": _STATUS,
        "significado_negocio": _SIGNIFICADO,
        "evidencias": evidencias,
        "confirmacao_usuario": True,
        "cabecalho": {
            "deteccao": deteccao_publica,
            "confianca": confianca_publica,
            "linha": regiao.get("cabecalho_linha") if _inteiro_nao_bool(regiao.get("cabecalho_linha")) else None,
            "candidatos_total": regiao.get("candidatos_total") if _inteiro_nao_bool(regiao.get("candidatos_total")) else 0,
            "candidatos_amostra": bool(regiao.get("candidatos_amostra")),
            "rotulos_confirmados": False,
        },
        "ambiguidades_estruturais": estruturais,
        "linhas_observadas": len(linhas),
        "linhas_elegiveis_preparadas": linhas_preparadas,
        "divergencia": linhas_preparadas is not None and linhas_preparadas != len(linhas),
    }
    return publica, colunas, qualidade


def _largura(regiao: dict, linhas: list) -> int:
    inicio = regiao.get("coluna_inicio")
    fim = regiao.get("coluna_fim")
    if _inteiro_nao_bool(inicio) and _inteiro_nao_bool(fim) and fim >= inicio:
        largura = fim - inicio + 1
    else:
        tipos = regiao.get("tipos_fisicos")
        largura = len(tipos) if isinstance(tipos, list) else 0
    for registro in linhas:
        largura = max(largura, len(registro["valores"]))
    return largura


def _coluna(linhas: list, posicao: int, coluna_planilha: int) -> dict:
    tipos = []
    presentes = 0
    ausentes = 0
    com_valor = 0
    inteiros = True
    fracionados = False
    com_hora = False
    identidades = {}
    for registro in linhas:
        valores = registro["valores"]
        celula = valores[posicao] if posicao < len(valores) else {"tipo": "ausente"}
        if not isinstance(celula, dict):
            raise PerfilErro("dados_invalidos")
        efetivo = _tipo_efetivo(celula)
        if efetivo == "ausente":
            ausentes += 1
            continue
        if efetivo not in tipos:
            tipos.append(efetivo)
        if not _tem_valor(celula):
            ausentes += 1
            continue
        presentes += 1
        com_valor += 1
        valor = celula.get("valor")
        if efetivo == "numero":
            if isinstance(valor, bool) or not isinstance(valor, int):
                inteiros = False
            if isinstance(valor, float):
                fracionados = True
        if efetivo == "data" and isinstance(valor, str) and " " in valor and ":" in valor:
            com_hora = True
        chave = _chave_valor(efetivo, valor)
        identidades[chave] = identidades.get(chave, 0) + 1
    return {
        "posicao": posicao,
        "coluna_planilha": coluna_planilha,
        "tipos": tipos,
        "presentes": presentes,
        "ausentes": ausentes,
        "com_valor": com_valor,
        "distintos": len(identidades),
        "inteiros": inteiros and presentes > 0 and tipos == ["numero"],
        "fracionados": fracionados,
        "com_hora": com_hora,
        "identidades": identidades,
        "linhas": len(linhas),
    }


def _hipotese_coluna(arquivo, aba, regiao, posicao, item: dict, codigos: list) -> dict:
    tipos = [tipo for tipo in item["tipos"] if tipo != "ausente"]
    dimensoes = []
    evidencias = []
    if tipos == ["data"]:
        dimensoes.append(_DIM_DATA)
        evidencias.append("tipo_data")
        if item["com_hora"]:
            dimensoes.append(_DIM_TEMPO)
            evidencias.append("componente_horario")
    elif tipos == ["hora"]:
        dimensoes.append(_DIM_TEMPO)
        evidencias.append("tipo_hora")
    elif tipos == ["numero"] and item["fracionados"]:
        dimensoes.append(_DIM_VALOR)
        evidencias.append("tipo_numero_fracionado")
    elif tipos == ["numero"] and item["inteiros"]:
        dimensoes.append(_DIM_QUANTIDADE)
        evidencias.append("tipo_numero_inteiro")
    elif tipos == ["texto"]:
        evidencias.append("tipo_texto")
    elif tipos:
        dimensoes.append(_INDEFINIDA)

    chave = (
        len(tipos) == 1
        and tipos[0] in _TIPOS_CHAVE
        and item["presentes"] >= 2
        and item["ausentes"] == 0
        and item["distintos"] == item["presentes"]
    )
    if chave:
        if _DIM_IDENTIFICADOR not in dimensoes:
            dimensoes.append(_DIM_IDENTIFICADOR)
        evidencias.append("unicidade_observada")
    elif tipos == ["numero"] and item["inteiros"] and item["distintos"] < item["presentes"]:
        evidencias.append("valores_repetidos")
    if ("data_ambigua" in codigos or "numero_ambiguo" in codigos) and _INDEFINIDA not in dimensoes:
        dimensoes.append(_INDEFINIDA)
    principal = _DIM_IDENTIFICADOR if chave else (dimensoes[0] if dimensoes else _INDEFINIDA)
    if not dimensoes:
        dimensoes = [_INDEFINIDA]
        principal = _INDEFINIDA
    return {
        "arquivo": arquivo,
        "aba": aba,
        "regiao": regiao,
        "indice": posicao,
        "coluna_planilha": item["coluna_planilha"],
        "dimensoes": dimensoes,
        "dimensao_principal": principal,
        "status": _STATUS,
        "significado_negocio": _SIGNIFICADO,
        "evidencias": evidencias,
        "chave_candidata": chave,
        "tipos": sorted(tipos) if tipos else ["vazio"],
        "presentes": item["presentes"],
        "ausentes": item["ausentes"],
        "distintos": item["distintos"],
        "dominio_inconsistente": len(tipos) > 1 or "data_ambigua" in codigos or "numero_ambiguo" in codigos,
        "completude": {"com_valor": item["com_valor"], "linhas": item["linhas"], "ausentes": item["ausentes"]},
        "_identidades": item["identidades"],
        "_tipo_chave": tipos[0] if len(tipos) == 1 else None,
        "_codigos": codigos,
    }


def _hipotese_regiao(aba: dict, regiao: dict, linhas: list, colunas: list) -> tuple[str, list, list]:
    candidatos = []
    evidencias = []
    n = len(linhas)
    cols = len(colunas)
    if aba.get("indicio_apresentacao") is True:
        candidatos.append(_PAPEL_APRESENTACAO)
        evidencias.append("indicio_apresentacao")
    if aba.get("contem_grafico") is True:
        evidencias.append("contem_grafico")
    mescladas = regiao.get("celulas_mescladas")
    if not _inteiro_nao_bool(mescladas):
        mescladas = aba.get("celulas_mescladas")
    if _inteiro_nao_bool(mescladas) and mescladas > 0:
        evidencias.append("celulas_mescladas")
    if aba.get("oculta") is True or aba.get("indicio_auxiliar") is True:
        candidatos.append(_PAPEL_AUXILIAR)
        if aba.get("oculta") is True:
            evidencias.append("aba_oculta")
        if aba.get("indicio_auxiliar") is True:
            evidencias.append("indicio_auxiliar")
    if cols <= 2 and n >= 1 and aba.get("indicio_apresentacao") is not True:
        candidatos.append(_PAPEL_LISTA)
        evidencias.append("largura_estreita")
    if cols >= 3 and n >= 2:
        candidatos.append(_PAPEL_TRANSACIONAL)
        evidencias.append("varios_registros")
        evidencias.append("varias_colunas")
    if n >= 2 and 2 <= cols <= 6 and any(item["chave_candidata"] for item in colunas):
        candidatos.append(_PAPEL_CADASTRO)
        evidencias.append("coluna_unica")
    if n == 0:
        evidencias.append("sem_registros_elegiveis")
    if not candidatos:
        candidatos.append(_INDEFINIDA)
    papel = _principal(candidatos, aba=aba, colunas=cols, linhas=n)
    return papel, candidatos, evidencias


def _principal(candidatos: list, *, aba: dict, colunas: int, linhas: int) -> str:
    if aba.get("indicio_apresentacao") is True and _PAPEL_APRESENTACAO in candidatos:
        return _PAPEL_APRESENTACAO
    if (aba.get("oculta") is True or aba.get("indicio_auxiliar") is True) and _PAPEL_AUXILIAR in candidatos:
        return _PAPEL_AUXILIAR
    if linhas >= 2 and colunas >= 3 and _PAPEL_TRANSACIONAL in candidatos:
        return _PAPEL_TRANSACIONAL
    if colunas <= 2 and linhas >= 1 and _PAPEL_LISTA in candidatos:
        return _PAPEL_LISTA
    if _PAPEL_CADASTRO in candidatos:
        return _PAPEL_CADASTRO
    if _PAPEL_AUXILIAR in candidatos:
        return _PAPEL_AUXILIAR
    return candidatos[0]


def _qualidade(regiao, linhas, stats, colunas, largura, estruturais, pre_preenchidas) -> dict:
    com_valor = sum(item["com_valor"] for item in stats)
    celulas = len(linhas) * largura
    ausentes = sum(item["ausentes"] for item in stats)
    duplicidades = _duplicidades(linhas)
    datas_pendentes = sum(1 for item in stats for tipo in item["tipos"] if tipo == "data_pendente")
    formulas_observadas = regiao.get("formulas_observadas") if isinstance(regiao.get("formulas_observadas"), list) else []
    sem_cache = 0
    for formula in formulas_observadas:
        if isinstance(formula, dict) and formula.get("cache_estado") != "presente":
            sem_cache += 1
    publicadas = regiao.get("formulas_sem_cache")
    if not _inteiro_nao_bool(publicadas):
        publicadas = None
    tipos_publicados = []
    bruto = regiao.get("tipos_fisicos") if isinstance(regiao.get("tipos_fisicos"), list) else []
    for tipo in bruto:
        tipos_publicados.append(tipo if tipo in _TIPOS else "opaco")
    tipos_observados = []
    for item in stats:
        tipos_observados.append(_rotulo_tipo(item["tipos"], item["presentes"]))
    preambulo = 0
    for preservada in regiao.get("celulas_preservadas") or []:
        if isinstance(preservada, dict) and preservada.get("papel") == "preambulo":
            preambulo += 1
    return {
        "arquivo": regiao.get("arquivo"),
        "aba": regiao.get("aba"),
        "regiao": regiao.get("indice"),
        "completude": {"com_valor": com_valor, "celulas": celulas, "valores_ausentes": ausentes},
        "tipos_misturados": sum(1 for item in colunas if item["dominio_inconsistente"]),
        "duplicidades": duplicidades,
        "datas": {
            "ambiguas": sum(1 for codigo in estruturais if codigo == "data_ambigua"),
            "pendentes": datas_pendentes,
            "colunas": [
                item["indice"]
                for item in colunas
                if "data_ambigua" in item["_codigos"] or "data_pendente" in item["tipos"]
            ],
        },
        "formulas_sem_cache": {
            "observadas": sem_cache,
            "publicadas": publicadas,
            "divergencia": publicadas is not None and publicadas != sem_cache,
        },
        "cabecalho_ambiguo": "cabecalho_ambiguo" in estruturais or regiao.get("cabecalho_deteccao") == "ambigua",
        "linhas_pre_preenchidas": pre_preenchidas,
        "preambulo_celulas": preambulo,
        "dominios_inconsistentes": sum(1 for item in colunas if item["dominio_inconsistente"]),
        "chaves_candidatas": sum(1 for item in colunas if item["chave_candidata"]),
        "tipos_publicados": tipos_publicados,
        "tipos_observados": tipos_observados,
        "divergencia_tipos": tipos_publicados != tipos_observados,
        "alertas": [codigo for codigo in (regiao.get("alertas") or []) if codigo in _ALERTAS],
    }


def _duplicidades(linhas: list) -> dict:
    grupos: dict[str, list] = {}
    for registro in linhas:
        assinatura = json.dumps(
            registro["valores"],
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_escalar,
        )
        linha = registro.get("linha") if _inteiro_nao_bool(registro.get("linha")) else None
        grupos.setdefault(assinatura, []).append(linha)
    repetidos = [item for item in grupos.values() if len(item) > 1]
    linhas_grupo = sorted(linha for grupo in repetidos for linha in grupo if linha is not None)
    return {
        "grupos": len(repetidos),
        "linhas_excedentes": sum(len(grupo) - 1 for grupo in repetidos),
        "linhas": linhas_grupo,
    }


def _rotulo_tipo(tipos: list, presentes: int) -> str:
    if presentes == 0:
        return "vazio"
    if len(tipos) == 1 and tipos[0] in _TIPOS:
        return tipos[0]
    if len(tipos) > 1:
        return "misto"
    return "opaco"


def _relacoes(colunas: list, regioes: list) -> tuple[list, dict]:
    """Examina pares sob orçamento, sem montar o produto cartesiano.

    Primeiro as duas colunas são chave candidata; depois exatamente uma.
    Dentro de cada classe, as regiões revezam e o índice do par percorre
    a grade com passo coprimo, então as primeiras comparações não ficam
    presas ao começo da planilha. Só o que foi examinado pode ser publicado.
    """
    por_regiao = {(item["arquivo"], item["aba"], item["regiao"]): item for item in regioes}
    teto_pares, teto_publicadas = _tetos_relacoes()
    ordem, mapa, totais = _agrupar_colunas(colunas)
    fontes_chave = _fontes_relacao(ordem, mapa, "chaves")
    fontes_mistas = _fontes_relacao(ordem, mapa, "mistas")
    total = _tamanho_fontes(fontes_chave) + _tamanho_fontes(fontes_mistas)
    encontradas = []
    examinados = 0
    for fontes in (fontes_chave, fontes_mistas):
        if examinados >= teto_pares:
            break
        achadas, usados = _examinar_fontes(fontes, teto_pares - examinados, por_regiao)
        encontradas.extend(achadas)
        examinados += usados
    publicadas = _publicar_relacoes(encontradas, teto_publicadas, totais)
    return publicadas, {
        "analise_relacoes_parcial": examinados < total,
        "limite_relacoes_atingido": len(publicadas) < len(encontradas),
        "pares_examinados": examinados,
        "relacoes_publicadas": len(publicadas),
        "criterio_selecao": CRITERIO_SELECAO_RELACOES,
    }


def _tetos_relacoes() -> tuple[int, int]:
    return (
        _teto_env("AGENTE_BI_RELACOES_MAX_PARES", TETO_PARES_EXAMINADOS),
        _teto_env("AGENTE_BI_RELACOES_MAX_PUBLICADAS", TETO_RELACOES_PUBLICADAS),
    )


def _teto_env(nome: str, teto: int) -> int:
    bruto = os.getenv(nome)
    if bruto is None or not bruto.strip() or not bruto.strip().isdigit():
        return teto
    valor = int(bruto.strip())
    if valor < 1:
        return teto
    return min(valor, teto)


def _agrupar_colunas(colunas: list) -> tuple[list, dict, dict]:
    ordem = []
    mapa: dict[tuple, dict] = {}
    totais: dict[tuple, int] = {}
    for coluna in colunas:
        chave = (coluna["arquivo"], coluna["aba"], coluna["regiao"])
        totais[chave] = totais.get(chave, 0) + 1
        tipo = coluna.get("_tipo_chave")
        if tipo not in _TIPOS_CHAVE:
            continue
        grupo = mapa.get(chave)
        if grupo is None:
            grupo = {"chaves": {}, "outras": {}}
            mapa[chave] = grupo
            ordem.append(chave)
        destino = grupo["chaves"] if coluna["chave_candidata"] else grupo["outras"]
        destino.setdefault(tipo, []).append(coluna)
    ordem.sort(key=_chave_regiao)
    for grupo in mapa.values():
        for balde in (grupo["chaves"], grupo["outras"]):
            for tipo in balde:
                balde[tipo].sort(key=lambda item: item["indice"] if _inteiro_nao_bool(item["indice"]) else -1)
    return ordem, mapa, totais


def _chave_regiao(chave: tuple) -> tuple:
    return tuple(item if _inteiro_nao_bool(item) else -1 for item in chave)


def _fontes_relacao(ordem: list, mapa: dict, classe: str) -> list[tuple[list, list]]:
    fontes = []
    for esquerda in range(len(ordem)):
        for direita in range(esquerda + 1, len(ordem)):
            grupo_a = mapa[ordem[esquerda]]
            grupo_b = mapa[ordem[direita]]
            tipos = sorted(set(grupo_a["chaves"]) | set(grupo_a["outras"]))
            for tipo in tipos:
                if tipo not in grupo_b["chaves"] and tipo not in grupo_b["outras"]:
                    continue
                chaves_a = grupo_a["chaves"].get(tipo, [])
                outras_a = grupo_a["outras"].get(tipo, [])
                chaves_b = grupo_b["chaves"].get(tipo, [])
                outras_b = grupo_b["outras"].get(tipo, [])
                if classe == "chaves":
                    if chaves_a and chaves_b:
                        fontes.append((chaves_a, chaves_b))
                elif chaves_a and outras_b:
                    fontes.append((chaves_a, outras_b))
                if classe != "chaves" and chaves_b and outras_a:
                    fontes.append((chaves_b, outras_a))
    return fontes


def _tamanho_fontes(fontes: list) -> int:
    return sum(len(esquerda) * len(direita) for esquerda, direita in fontes)


def _examinar_fontes(fontes: list, limite: int, regioes: dict) -> tuple[list, int]:
    if limite <= 0 or not fontes:
        return [], 0
    cursores = [0] * len(fontes)
    tamanhos = [len(esquerda) * len(direita) for esquerda, direita in fontes]
    encontradas = []
    examinados = 0
    while examinados < limite:
        avancou = False
        for indice, (esquerda, direita) in enumerate(fontes):
            if cursores[indice] >= tamanhos[indice]:
                continue
            primeira, segunda = _par_espalhado(esquerda, direita, cursores[indice])
            cursores[indice] += 1
            avancou = True
            examinados += 1
            relacao = _relacionar(primeira, segunda, regioes)
            if relacao is not None:
                encontradas.append(relacao)
            if examinados >= limite:
                break
        if not avancou:
            break
    return encontradas, examinados


def _par_espalhado(esquerda: list, direita: list, k: int) -> tuple[dict, dict]:
    largura = len(esquerda)
    altura = len(direita)
    resto = k % largura
    volta = k // largura
    linha = (resto * _passo_coprimo(largura)) % largura
    passo = _passo_coprimo(altura)
    coluna = (volta * passo + resto * passo) % altura
    return esquerda[linha], direita[coluna]


def _passo_coprimo(tamanho: int) -> int:
    if tamanho <= 1:
        return 1
    passo = (tamanho * 2 + 2) // 5
    if passo < 1 or passo >= tamanho:
        passo = 1
    while math.gcd(passo, tamanho) != 1:
        passo += 1
        if passo >= tamanho:
            return 1
    return passo


def _publicar_relacoes(encontradas: list, limite: int, totais: dict) -> list:
    if len(encontradas) <= limite:
        encontradas.sort(key=_ordem_relacao)
        return encontradas
    escolhidas = _selecionar_cobertura(encontradas, limite, totais)
    escolhidas.sort(key=_ordem_relacao)
    return escolhidas


def _selecionar_cobertura(encontradas: list, limite: int, totais: dict) -> list:
    baldes: dict[tuple, list] = {}
    for relacao in encontradas:
        baldes.setdefault(_slot_relacao(relacao, totais), []).append(relacao)
    for lista in baldes.values():
        lista.sort(key=_ordem_relacao)
    ordem = _intercalar_slots(list(baldes))
    ponteiro = {chave: 0 for chave in ordem}
    escolhidas = []
    while len(escolhidas) < limite:
        avancou = False
        for chave in ordem:
            indice = ponteiro[chave]
            lista = baldes[chave]
            if indice >= len(lista):
                continue
            escolhidas.append(lista[indice])
            ponteiro[chave] = indice + 1
            avancou = True
            if len(escolhidas) >= limite:
                break
        if not avancou:
            break
    return escolhidas


def _slot_relacao(relacao: dict, totais: dict) -> tuple:
    unico = relacao["lado_unico"]
    associado = relacao["lado_associado"]
    return (
        _prioridade_relacao(relacao),
        *_coordenada(unico),
        *_coordenada(associado),
        _faixa_coluna(unico["coluna"], totais.get(_coordenada(unico), 1)),
        _faixa_coluna(associado["coluna"], totais.get(_coordenada(associado), 1)),
    )


def _coordenada(ref: dict) -> tuple:
    return (
        ref["arquivo"] if _inteiro_nao_bool(ref["arquivo"]) else -1,
        ref["aba"] if _inteiro_nao_bool(ref["aba"]) else -1,
        ref["regiao"] if _inteiro_nao_bool(ref["regiao"]) else -1,
    )


def _prioridade_relacao(relacao: dict) -> tuple:
    ambiguidade = 1 if relacao["ambiguidades"] else 0
    card = relacao["cardinalidade_observada"]
    if card == _CARD_UM_UM:
        ordem = 0
    elif card == _CARD_UM_N:
        ordem = 1
    else:
        ordem = 2
    return (ambiguidade, ordem)


def _faixa_coluna(indice, total: int) -> int:
    if not _inteiro_nao_bool(indice) or total <= 0:
        return 0
    return min(_FAIXAS_RELACAO - 1, indice * _FAIXAS_RELACAO // total)


def _intercalar_slots(chaves: list) -> list:
    grupos: dict[tuple, list] = {}
    for chave in chaves:
        grupos.setdefault(chave[0], []).append(chave)
    ordem = []
    for prioridade in sorted(grupos):
        por_regiao: dict[tuple, list] = {}
        for chave in grupos[prioridade]:
            por_regiao.setdefault(chave[1:7], []).append(chave)
        regioes = sorted(por_regiao)
        for regiao in regioes:
            por_regiao[regiao].sort(key=lambda item: (item[7], item[8], item))
        maior = max(len(por_regiao[regiao]) for regiao in regioes)
        for indice in range(maior):
            for regiao in regioes:
                lista = por_regiao[regiao]
                if indice < len(lista):
                    ordem.append(lista[indice])
    return ordem


def _ordem_relacao(item: dict) -> tuple:
    unico = item["lado_unico"]
    return (
        unico["arquivo"] if _inteiro_nao_bool(unico["arquivo"]) else -1,
        unico["aba"] if _inteiro_nao_bool(unico["aba"]) else -1,
        unico["regiao"] if _inteiro_nao_bool(unico["regiao"]) else -1,
        unico["coluna"],
        item["lado_associado"]["coluna"],
    )


def _relacionar(primeira: dict, segunda: dict, regioes: dict):
    if (primeira["arquivo"], primeira["aba"], primeira["regiao"]) == (
        segunda["arquivo"],
        segunda["aba"],
        segunda["regiao"],
    ):
        return None
    if primeira["chave_candidata"] and not segunda["chave_candidata"]:
        unica, associada = primeira, segunda
    elif segunda["chave_candidata"] and not primeira["chave_candidata"]:
        unica, associada = segunda, primeira
    elif primeira["chave_candidata"] and segunda["chave_candidata"]:
        unica, associada = _ordenar_lados(primeira, segunda)
    else:
        return None
    if unica["_tipo_chave"] is None or unica["_tipo_chave"] != associada["_tipo_chave"]:
        return None
    if unica["_tipo_chave"] == "booleano":
        return None
    compartilhados, repetido = _sobreposicao(unica["_identidades"], associada["_identidades"])
    if compartilhados < 2:
        return None
    menor = min(unica["distintos"], associada["distintos"])
    if menor < 2 or compartilhados * 2 < menor:
        return None
    if unica["chave_candidata"] and associada["chave_candidata"] and not repetido:
        observada = _CARD_UM_UM
    elif unica["chave_candidata"] and repetido:
        observada = _CARD_UM_N
    else:
        observada = _CARD_DESCONHECIDA
    ambiguidades = []
    for coluna in (unica, associada):
        regiao = regioes.get((coluna["arquivo"], coluna["aba"], coluna["regiao"]))
        if regiao is None:
            continue
        if regiao["cabecalho"]["deteccao"] == "ambigua":
            ambiguidades.append("rotulos_nao_confirmados")
        if "divisao_lateral_ambigua" in regiao["ambiguidades_estruturais"]:
            ambiguidades.append("divisao_lateral_nao_resolvida")
        if regiao["divergencia"]:
            ambiguidades.append("contagem_divergente")
    ambiguidades = list(dict.fromkeys(ambiguidades))
    return {
        "lado_unico": _ref_coluna(unica),
        "lado_associado": _ref_coluna(associada),
        "status": _STATUS,
        "significado_negocio": _SIGNIFICADO,
        "evidencias": ["sobreposicao_de_valores", "unicidade_observada", "rotulo_nao_utilizado"],
        "valores_compartilhados": compartilhados,
        "cardinalidade_observada": observada,
        "cardinalidade": _CARD_DESCONHECIDA if ambiguidades else observada,
        "risco_duplicacao": repetido or observada == _CARD_UM_N,
        "ambiguidades": ambiguidades,
        "confirmacao_usuario": True,
        "juncao": _JUNCAO,
    }


def _sobreposicao(esquerda: dict, direita: dict) -> tuple[int, bool]:
    if len(esquerda) > len(direita):
        esquerda, direita = direita, esquerda
    compartilhados = 0
    repetido = False
    for chave, contagem in esquerda.items():
        contagem_outra = direita.get(chave)
        if not contagem_outra:
            continue
        compartilhados += 1
        if contagem > 1 or contagem_outra > 1:
            repetido = True
    return compartilhados, repetido


def _ordenar_lados(primeira: dict, segunda: dict):
    chave_a = (primeira["distintos"], primeira["arquivo"], primeira["aba"], primeira["regiao"], primeira["indice"])
    chave_b = (segunda["distintos"], segunda["arquivo"], segunda["aba"], segunda["regiao"], segunda["indice"])
    if chave_a >= chave_b:
        return primeira, segunda
    return segunda, primeira


def _ref_coluna(coluna: dict) -> dict:
    return {
        "arquivo": coluna["arquivo"],
        "aba": coluna["aba"],
        "regiao": coluna["regiao"],
        "coluna": coluna["indice"],
    }


def _totais(regioes: list) -> dict:
    return {
        "com_valor": sum(item["completude"]["com_valor"] for item in regioes),
        "celulas": sum(item["completude"]["celulas"] for item in regioes),
        "valores_ausentes": sum(item["completude"]["valores_ausentes"] for item in regioes),
        "linhas_excedentes": sum(item["duplicidades"]["linhas_excedentes"] for item in regioes),
        "datas_ambiguas": sum(item["datas"]["ambiguas"] for item in regioes),
        "formulas_sem_cache": sum(item["formulas_sem_cache"]["observadas"] for item in regioes),
        "pre_preenchidas": sum(item["linhas_pre_preenchidas"] or 0 for item in regioes),
        "chaves_candidatas": sum(item["chaves_candidatas"] for item in regioes),
        "dominios_inconsistentes": sum(item["dominios_inconsistentes"] for item in regioes),
    }


def _governanca(preparacao: dict, analise: dict, escopo, *, bloquear_contexto: bool) -> dict:
    varredura = _varrer(preparacao)
    representacao = None
    motivo_representacao = None
    if varredura is None or bloquear_contexto:
        motivo_representacao = "minimizacao_nao_comprovada"
    else:
        representacao = _representacao(analise, varredura)
        if not _arvore_controlada(representacao):
            representacao = None
            motivo_representacao = "minimizacao_nao_comprovada"
        else:
            minimizada = _minimizar_representacao(representacao)
            if minimizada is None:
                representacao = None
                motivo_representacao = "minimizacao_nao_comprovada"
            else:
                representacao = minimizada
    avaliacao = _avaliar_finalidade(representacao if representacao is not None else {"contrato": CONTRATO_CONTEXTO, "amostras": False})
    montagem = "local_nao_autorizada" if representacao is not None and avaliacao is not None else "impedida"
    if representacao is None:
        montagem = "impedida"
    contexto = None
    if montagem == "local_nao_autorizada":
        contexto = {
            "contrato": CONTRATO_CONTEXTO,
            "armazenamento": "local",
            "amostras_de_linhas": False,
            "valores_de_celula": False,
            "representacao": representacao,
            "autorizacao": _autorizacao(avaliacao),
        }
        if not _arvore_controlada(contexto):
            contexto = None
            montagem = "impedida"
            motivo_representacao = "minimizacao_nao_comprovada"
    motivos = []
    if motivo_representacao is not None:
        motivos.append(motivo_representacao)
    if avaliacao is None:
        motivos.append("governanca_indisponivel")
    elif avaliacao.get("motivo") and avaliacao["motivo"] not in motivos:
        motivos.append(avaliacao["motivo"])
    return {
        "classificador": CLASSIFIER_NAME,
        "finalidade_varredura": PURPOSE_REDACAO,
        "finalidade_transferencia": FINALIDADE_TRANSFERENCIA,
        "finalidade_aprovada": FINALIDADE_TRANSFERENCIA in SUPPORTED_PURPOSES,
        "decisao": None if avaliacao is None else avaliacao["decisao"],
        "motivos": motivos,
        "varredura": "comprovada" if varredura is not None and representacao is not None else "nao_comprovada",
        "categorias_detectadas": {} if varredura is None else varredura["categorias"],
        "bloqueios": 0 if varredura is None else varredura["bloqueios"],
        "amostras_de_linhas": False,
        "valores_de_celula": False,
        "texto_de_celula_excluido": True,
        "motivo_exclusao_de_texto": "classificador_existente_nao_cobre_todos_os_identificadores",
        "identificadores_fora_do_classificador": ["placa"],
        "armazenamento": "local",
        "transferencia": "bloqueada",
        "autorizado_para_provedor": False,
        "apto_anthropic": False,
        "contrato_saida_validado": CONTRATO_SAIDA_VALIDADO,
        "cadastrar_finalidade_nao_autoriza": True,
        "montagem_contexto": montagem,
        "contexto_ia": contexto,
    }


def _representacao(analise: dict, varredura: dict) -> dict:
    dimensoes = Counter()
    for coluna in analise["colunas"]:
        for dimensao in coluna["dimensoes"]:
            if dimensao in _TOKENS:
                dimensoes[dimensao] += 1
    return {
        "contrato": CONTRATO_CONTEXTO,
        "amostras": False,
        "regioes": [
            {
                "arquivo": item["arquivo"] if _inteiro_nao_bool(item["arquivo"]) else 0,
                "aba": item["aba"] if _inteiro_nao_bool(item["aba"]) else 0,
                "regiao": item["regiao"] if _inteiro_nao_bool(item["regiao"]) else 0,
                "papel": item["papel"],
                "linhas": item["linhas_observadas"],
                "cabecalho": item["cabecalho"]["deteccao"],
            }
            for item in analise["regioes"]
        ],
        "dimensoes": {nome: dimensoes[nome] for nome in sorted(dimensoes)},
        "qualidade": {
            "com_valor": analise["qualidade"]["totais"]["com_valor"],
            "celulas": analise["qualidade"]["totais"]["celulas"],
            "duplicidades": analise["qualidade"]["totais"]["linhas_excedentes"],
            "datas_ambiguas": analise["qualidade"]["totais"]["datas_ambiguas"],
            "formulas_sem_cache": analise["qualidade"]["totais"]["formulas_sem_cache"],
            "pre_preenchidas": analise["qualidade"]["totais"]["pre_preenchidas"],
        },
        "relacoes": [
            {
                "cardinalidade": item["cardinalidade"],
                "compartilhados": item["valores_compartilhados"],
                "risco_duplicacao": item["risco_duplicacao"],
                "confirmacao_usuario": True,
                "lado_unico": item["lado_unico"],
                "lado_associado": item["lado_associado"],
            }
            for item in analise["relacoes"]
        ],
        "analise_relacoes_parcial": analise["orcamento_relacoes"]["analise_relacoes_parcial"],
        "limite_relacoes_atingido": analise["orcamento_relacoes"]["limite_relacoes_atingido"],
        "categorias": varredura["categorias"],
    }


def _minimizar_representacao(representacao: dict):
    try:
        resultado = govern_outbound_content(
            representacao,
            purpose=PURPOSE_REDACAO,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent="agente_bi",
            provider="nao_enviado",
        )
    except Exception:
        return None
    if (
        not resultado.issued_by_cleiton
        or not governance_result_is_coherent(resultado, original=representacao)
        or resultado.decision != DECISION_ALLOW
        or resultado.safe_content != representacao
    ):
        return None
    return resultado.safe_content


def _avaliar_finalidade(documento: dict):
    try:
        resultado = govern_outbound_content(
            documento,
            purpose=FINALIDADE_TRANSFERENCIA,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent="agente_bi",
            provider="nao_enviado",
        )
    except Exception:
        return None
    motivo = resultado.reason_codes[0] if resultado.reason_codes else "opaco"
    if motivo not in _MOTIVOS:
        motivo = "opaco"
    decisao = resultado.decision if resultado.decision in {DECISION_ALLOW, DECISION_MINIMIZE, DECISION_BLOCK} else "opaco"
    return {"decisao": decisao, "motivo": motivo}


def _autorizacao(avaliacao) -> dict:
    return {
        "armazenamento": "local",
        "transferencia": "bloqueada",
        "autorizado_para_provedor": False,
        "apto_anthropic": False,
        "contrato_saida_validado": CONTRATO_SAIDA_VALIDADO,
        "finalidade_aprovada": FINALIDADE_TRANSFERENCIA in SUPPORTED_PURPOSES,
        "decisao": None if avaliacao is None else avaliacao["decisao"],
        "cadastrar_finalidade_nao_autoriza": True,
    }


def _varrer(preparacao: dict):
    categorias: dict[str, int] = {}
    bloqueios = 0

    def visitar(texto: str) -> bool:
        nonlocal bloqueios
        try:
            resultado = classify_text(texto, purpose=PURPOSE_REDACAO, content_type="text")
        except Exception:
            return False
        if not classification_invariants_ok(resultado):
            return False
        if resultado.decision == DECISION_BLOCK:
            if "ambiguous_high_risk_span" in resultado.reason_codes or "inconsistent_classifier_result" in resultado.reason_codes:
                return False
            bloqueios += 1
            return True
        for categoria in resultado.categories_detected:
            if categoria not in _CATEGORIAS:
                return False
            categorias[categoria] = categorias.get(categoria, 0) + 1
        return True

    if not _visitar_valores(preparacao, visitar):
        return None
    return {"categorias": {chave: categorias[chave] for chave in sorted(categorias)}, "bloqueios": bloqueios}


def _visitar_valores(valor, visitar) -> bool:
    if isinstance(valor, dict):
        for chave, item in valor.items():
            if isinstance(chave, str) and chave and not visitar(chave):
                return False
            if not _visitar_valores(item, visitar):
                return False
        return True
    if isinstance(valor, list):
        for item in valor:
            if not _visitar_valores(item, visitar):
                return False
        return True
    if isinstance(valor, str):
        return True if valor == "" else visitar(valor)
    if isinstance(valor, bool):
        return True
    if isinstance(valor, int):
        texto = str(abs(valor))
        if len(texto) >= 10 and not visitar(texto):
            return False
        return True
    if isinstance(valor, float):
        texto = format(valor, "f")
        if _muitos_digitos(texto) and not visitar(texto):
            return False
        return True
    if valor is None:
        return True
    return False


def _arvore_controlada(valor) -> bool:
    if valor is None or isinstance(valor, bool):
        return True
    if isinstance(valor, int) and not isinstance(valor, bool):
        return True
    if isinstance(valor, str):
        return valor in _TOKENS or _hex64(valor)
    if isinstance(valor, list):
        return all(_arvore_controlada(item) for item in valor)
    if isinstance(valor, dict):
        return all(isinstance(chave, str) and chave in _TOKENS and _arvore_controlada(item) for chave, item in valor.items())
    return False


def _codigos_da_coluna(ambiguidades, arquivo, aba, regiao, posicao) -> list:
    alvo = posicao + 1
    return [
        item["codigo"]
        for item in ambiguidades
        if item.get("arquivo") == arquivo
        and item.get("aba") == aba
        and item.get("regiao") == regiao
        and item.get("coluna") == alvo
        and item.get("codigo") in _CODIGOS_AMBIGUIDADE
    ]


def _codigos_da_regiao(ambiguidades, arquivo, aba, regiao) -> list:
    return [
        item["codigo"]
        for item in ambiguidades
        if item.get("arquivo") == arquivo and item.get("aba") == aba and item.get("regiao") == regiao and item.get("codigo") in _CODIGOS_AMBIGUIDADE
    ]


def _divisao_ambigua(regiao: dict) -> bool:
    return regiao.get("divisao_escolha") is None and bool(regiao.get("divisao_candidatos"))


def _tipo_efetivo(celula: dict) -> str:
    tipo = celula.get("tipo")
    if tipo == "formula" and celula.get("cache_estado") == "presente":
        cache = celula.get("tipo_cache")
        if cache in _TIPOS:
            return cache
        return "formula"
    if tipo in _TIPOS:
        return tipo
    return "desconhecido"


def _tem_valor(celula: dict) -> bool:
    tipo = celula.get("tipo")
    if tipo in {"ausente", "erro", "nao_suportado"}:
        return False
    if tipo == "formula" and celula.get("cache_estado") != "presente":
        return False
    return "valor" in celula and celula.get("valor") is not None


def _chave_valor(tipo: str, valor) -> str:
    return json.dumps([tipo, valor], ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_escalar)


def _escalar(valor):
    if isinstance(valor, (str, int, float, bool)) or valor is None:
        return valor
    return None


def _vinculo(estrutura, dados, metadados) -> str:
    material = {"dados_tipados": dados, "estrutura": estrutura, "metadados_preparacao": metadados}
    bruto = json.dumps(material, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=_escalar)
    return hashlib.sha256(bruto.encode("utf-8")).hexdigest()


def _registrar(escopo, perfil: dict) -> None:
    logger.info(
        "evento=agente_bi_perfil usuario_id=%s regioes=%s relacoes=%s contexto=%s",
        "-" if escopo is None else escopo.usuario_id,
        len(perfil["regioes"]),
        len(perfil["relacoes"]),
        perfil["governanca"]["montagem_contexto"],
    )


def _inteiro_nao_bool(valor) -> bool:
    return isinstance(valor, int) and not isinstance(valor, bool)


def _inteiro_positivo(valor) -> bool:
    return _inteiro_nao_bool(valor) and valor >= 1


def _hex64(valor: str) -> bool:
    return len(valor) == 64 and all(caractere in _HEX for caractere in valor)


def _muitos_digitos(texto: str) -> bool:
    total = 0
    for caractere in texto:
        if caractere.isdigit():
            total += 1
            if total >= 10:
                return True
    return False
