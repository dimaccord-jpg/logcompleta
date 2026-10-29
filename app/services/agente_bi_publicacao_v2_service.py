"""Publicação transacional do pedido livre do AgenteBI (SCRUM-269).

Liga os componentes já aprovados, nesta ordem:

interpretação governada, revalidação do plano, composição, cálculo,
identidade da aplicação, definição, memória, revisão e conclusão.

A intenção do texto não é decidida neste módulo. A publicação confirma
uma vez. Falha, esclarecimento ou concorrência não deixam revisão nova.
Repetir o mesmo plano devolve a revisão já publicada.

Um `plano_id` já gravado retoma a intenção sem o cliente reenviar o texto,
somente quando dashboard e revisão ainda são os do plano. O valor de
filtro continua fora da continuidade: entra só no pedido desta chamada.

Plano que só consulta gráficos existentes não abre revisão: os agregados
autorizados seguem para a interpretação logística, e o dashboard permanece.
"""
from __future__ import annotations

import copy
import logging

from app.extensions import db
from app.models import AgenteBiAnalise
from app.services.agente_bi_aplicacao_service import concluir_aplicacao, obter_aplicacao_do_plano, preparar_aplicacao
from app.services.agente_bi_calculo_service import (
    EXECUTOR,
    CONTRATO_CALCULO_V2,
    CalculoErro,
    _alvos_do_plano,
    _contexto_do_perfil,
    _proxima_rodada,
    _vinculo_servidor,
    calcular_definicao_v2,
)
from app.services.agente_bi_composicao_service import ComposicaoErro, compor_definicao
from app.services.agente_bi_contrato_v2_service import CONTRATO_DEFINIR_V2
from app.services.agente_bi_dashboard_service import (
    CONTRATO_DASHBOARD,
    _definicao_persistida,
    _indicadores_autorizados,
    _proxima_revisao,
    consultar_dashboard,
)
from app.services.agente_bi_evolucao_service import _carregar
from app.services.agente_bi_explicacao_service import explicar_resultados
from app.services.agente_bi_interpretacao_v2_service import (
    InterpretacaoErro,
    interpretar_plano,
    recuperar_contexto,
    revalidar_plano_duravel,
)
from app.services.agente_bi_leitura_service import CONTRATO_CONTEXTO_DEFINICAO_V2
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    criar_analise,
    criar_dashboard,
    obter_analise,
    obter_dashboard,
)

logger = logging.getLogger(__name__)

_CHAVES_PEDIDO = frozenset({"texto", "parametros", "plano_id"})
_MOTIVOS = {
    "informar_parametro": "Informe o valor exigido. Não vou inventar esse valor.",
    "confirmar_rotulos": "Preciso da confirmação do significado antes de alterar o dashboard.",
    "informar_unidade": "Não identifiquei a unidade dessa métrica. Não vou publicar o indicador.",
    "pedido_sem_alteracao": "Não identifiquei alteração de gráfico neste pedido.",
    "alvo_bloqueado": "A alteração não produziu um resultado publicável.",
    "interpretacao_indisponivel": "A interpretação governada não está disponível para este pedido.",
}


class PublicacaoErro(Exception):
    def __init__(self, codigo: str, status: int = 422):
        self.codigo = codigo
        self.status = status
        super().__init__(codigo)


def aplicar_pedido(escopo, dashboard_id: int, corpo: dict, *, transporte=None) -> dict:
    """Aplica um pedido em texto ao dashboard do escopo, ou pede esclarecimento."""
    texto, parametros, plano_id = _pedido(corpo)
    if plano_id is not None:
        from app.services.agente_bi_continuidade_service import texto_do_plano_compativel

        guardado = texto_do_plano_compativel(escopo, int(dashboard_id), plano_id)
        if texto is None:
            texto = guardado
        elif texto != guardado:
            raise PublicacaoErro("conflito_versao", 409)
    interpretado = interpretar_plano(escopo, int(dashboard_id), {"texto": texto}, transporte=transporte)
    carregado = _carregar(escopo, int(dashboard_id))
    indicadores = _indicadores_do_pedido(interpretado)
    if interpretado.get("situacao") != "proposto":
        return _esclarecer(carregado, _motivo_plano(interpretado.get("plano")), indicadores, parametros=[])
    plano_id = int(interpretado["analise_id"])
    linha = db.session.get(AgenteBiAnalise, plano_id)
    if linha is None:
        raise PublicacaoErro("analise_indisponivel", 409)
    db.session.refresh(linha)
    registro = obter_analise(escopo, plano_id)
    resultado = registro.get("resultado")
    if not isinstance(resultado, dict):
        raise PublicacaoErro("analise_indisponivel", 409)
    plano = revalidar_plano_duravel(linha, resultado)
    if plano.get("situacao") != "proposto":
        return _esclarecer(carregado, _motivo_plano(plano), indicadores, parametros=[])
    refs = _consulta_de_existentes(plano)
    if refs:
        return explicar_resultados(
            escopo,
            carregado,
            refs,
            texto,
            indicadores_plano=indicadores,
            transporte=transporte,
        )
    pronta = _aplicacao_concluida(escopo, plano_id)
    if pronta is not None:
        return _resposta_de_aplicacao(escopo, carregado, pronta, indicadores, parametros, reusada=True)
    contexto = recuperar_contexto(escopo, plano_id)
    if int(contexto["definicao_anterior_id"]) != int(carregado["definicao"]["id"]):
        raise PublicacaoErro("proveniencia_inconsistente", 409)
    if int(contexto["revisao_base"]) != int(carregado["painel"]["revisao"]):
        raise PublicacaoErro("conflito_versao", 409)
    try:
        composta = compor_definicao(
            plano,
            {
                "contrato": carregado["definicao"]["contrato_versao"],
                "analise_id": int(carregado["definicao"]["id"]),
                "definicoes": copy.deepcopy(carregado["definicoes"]),
            },
            _contexto_do_perfil(carregado["perfil"]),
            [dict(item) for item in contexto.get("parametros") or []],
            {
                "definicao_anterior_id": int(carregado["definicao"]["id"]),
                "revisao_base": int(carregado["painel"]["revisao"]),
                "origem_contrato": carregado["definicao"]["contrato_versao"],
            },
        )
    except ComposicaoErro as exc:
        raise PublicacaoErro(exc.codigo, exc.status) from exc
    if composta.get("situacao") == "esclarecimento":
        return _esclarecer(carregado, str(composta.get("motivo") or ""), indicadores, parametros=[])
    if composta.get("contrato") != CONTRATO_DEFINIR_V2:
        raise PublicacaoErro("contrato_saida_invalido")
    alvos = _alvos_do_plano(composta)
    if not alvos:
        return _esclarecer(carregado, "pedido_sem_alteracao", indicadores, parametros=[])
    calculado = calcular_definicao_v2(
        escopo,
        {
            "definicao": composta,
            "alvos": alvos,
            "plano_id": plano_id,
            "parametros": parametros,
        },
    )
    if calculado.get("situacao") == "esclarecimento" or calculado.get("ok") is not True:
        nomes = _nomes_esclarecidos(calculado.get("esclarecimentos"))
        return _esclarecer(carregado, str(calculado.get("motivo") or "informar_parametro"), indicadores, parametros=nomes)
    memoria = calculado.get("calculo")
    if not isinstance(memoria, dict) or memoria.get("contrato") != CONTRATO_CALCULO_V2:
        raise PublicacaoErro("analise_indisponivel", 409)
    return _publicar(escopo, carregado, plano_id, composta, memoria, indicadores, parametros)


def _publicar(escopo, carregado, plano_id: int, composta: dict, memoria: dict, indicadores: dict, parametros: dict) -> dict:
    projecao = indicadores
    painel = carregado["painel"]
    dataset = carregado["dataset"]
    definicao_anterior = carregado["definicao"]
    revisao_base = int(painel["revisao"])
    try:
        aplicacao = preparar_aplicacao(escopo, plano_id)
        if aplicacao.get("estado") == "concluida" and aplicacao.get("dashboard_publicado_id"):
            return _resposta_de_aplicacao(escopo, carregado, aplicacao, projecao, parametros, reusada=True)
        if int(aplicacao.get("revisao_base") or 0) != revisao_base:
            raise AgenteBiErro("conflito_versao", 409)
        if _proxima_revisao(escopo, painel["chave"]) != revisao_base + 1:
            raise AgenteBiErro("conflito_versao", 409)
        vinculo = _vinculo_servidor(
            definicao_anterior,
            revisao_base,
            carregado["definicoes"],
            _contexto_do_perfil(carregado["perfil"]),
            composta.get("plano"),
        )
        definicao = _gravar(
            escopo,
            definicao_anterior["chave"],
            {
                "dataset_id": dataset["id"],
                "versao": dataset["versao"],
                "impressao": dataset["impressao"],
                "etapa": "definicao",
                "estado": "concluida",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_DEFINICAO_V2,
                    "vinculo_preparacao": carregado["perfil"].get("vinculo_preparacao"),
                    "vinculo": vinculo,
                },
                "regras_negocio": {"contrato": CONTRATO_DEFINIR_V2},
                "esclarecimentos": {},
                "contrato_versao": CONTRATO_DEFINIR_V2,
                "resultado": composta,
            },
        )
        guardada = copy.deepcopy(memoria)
        guardada["origem_definicao_id"] = int(definicao["id"])
        guardada["origem_contrato"] = CONTRATO_DEFINIR_V2
        guardada["persistido"] = True
        autorizados = _indicadores_autorizados(
            {"dataset_id": dataset["id"], "versao": dataset["versao"], "impressao": dataset["impressao"]},
            guardada,
        )
        bloqueios = [
            {"id": item["id"], "motivo": item.get("motivo")}
            for item in autorizados
            if item.get("status") == "bloqueado"
        ]
        calculo = _gravar(
            escopo,
            definicao_anterior["chave"],
            {
                "dataset_id": dataset["id"],
                "versao": dataset["versao"],
                "impressao": dataset["impressao"],
                "etapa": "execucao",
                "estado": "concluida",
                "contexto": {
                    "definicao_id": int(definicao["id"]),
                    "plano_id": int(plano_id),
                    "dataset_id": dataset["id"],
                    "versao": dataset["versao"],
                    "impressao": dataset["impressao"],
                },
                "regras_negocio": {"executor": EXECUTOR, "ia_calculo": False, "plano_id": int(plano_id)},
                "esclarecimentos": {"bloqueios": bloqueios},
                "contrato_versao": CONTRATO_CALCULO_V2,
                "resultado": guardada,
            },
        )
        publicado = criar_dashboard(
            escopo,
            {
                "chave": painel["chave"],
                "revisao": revisao_base + 1,
                "estado_publicacao": "publicado",
                "contrato_versao": CONTRATO_DASHBOARD,
                "executor_versao": EXECUTOR,
                "definicoes_graficos": [_definicao_persistida(item) for item in autorizados],
                "datasets": [
                    {
                        "dataset_id": dataset["id"],
                        "versao": dataset["versao"],
                        "impressao": dataset["impressao"],
                    }
                ],
                "analise_id": int(calculo["id"]),
            },
            confirmar=False,
        )
        concluir_aplicacao(
            escopo,
            int(aplicacao["id"]),
            definicao_id=int(definicao["id"]),
            calculo_id=int(calculo["id"]),
            dashboard_publicado_id=int(publicado["id"]),
        )
        aberto = consultar_dashboard(escopo, int(publicado["id"]))
        db.session.commit()
    except AgenteBiErro as exc:
        db.session.rollback()
        if exc.codigo == "conflito_versao":
            pronta = _aplicacao_concluida(escopo, plano_id)
            if pronta is not None:
                return _resposta_de_aplicacao(escopo, carregado, pronta, projecao, parametros, reusada=True)
        raise PublicacaoErro(exc.codigo, exc.status) from exc
    except CalculoErro as exc:
        db.session.rollback()
        raise PublicacaoErro(exc.codigo, exc.status) from exc
    except Exception:
        db.session.rollback()
        raise
    logger.info(
        "evento=agente_bi_publicacao_v2 usuario_id=%s dashboard_id=%s plano_id=%s revisao=%s chamada_anthropic=%s",
        escopo.usuario_id,
        aberto["dashboard"]["id"],
        plano_id,
        aberto["dashboard"]["revisao"],
        int(_chamada_contada(projecao)),
    )
    return _resposta_aplicada(carregado, aberto, projecao, parametros, reusada=False, plano_id=plano_id)


def _gravar(escopo, chave: str, corpo: dict) -> dict:
    completo = dict(corpo)
    completo["chave"] = chave
    completo["rodada"] = _proxima_rodada(escopo, chave)
    return criar_analise(escopo, completo, confirmar=False)


def _aplicacao_concluida(escopo, plano_id: int):
    try:
        aplicacao = obter_aplicacao_do_plano(escopo, int(plano_id))
    except AgenteBiErro as exc:
        if exc.codigo == "nao_encontrado":
            return None
        raise
    if aplicacao.get("estado") == "concluida" and aplicacao.get("dashboard_publicado_id"):
        return aplicacao
    return None


def _resposta_de_aplicacao(escopo, carregado, aplicacao: dict, indicadores: dict, parametros: dict, *, reusada: bool) -> dict:
    identificador = int(aplicacao["dashboard_publicado_id"])
    obter_dashboard(escopo, identificador)
    aberto = consultar_dashboard(escopo, identificador)
    return _resposta_aplicada(carregado, aberto, indicadores, parametros, reusada=reusada, plano_id=int(aplicacao["plano_id"]))


def _resposta_aplicada(carregado, aberto: dict, indicadores: dict, parametros: dict, *, reusada: bool, plano_id: int) -> dict:
    campos = _campos_ia(indicadores)
    return {
        "ok": True,
        "situacao": "aplicada",
        "reusada": reusada,
        "chamada_anthropic": campos["chamada_anthropic"] is True,
        "cobranca": campos["cobranca"] is True,
        "indicadores_ia": campos["indicadores_ia"],
        "plano_id": int(plano_id),
        "parametros": _parametros_publicos(parametros),
        "esclarecimento": None,
        "dashboard": aberto["dashboard"],
        "consulta": aberto["consulta"],
        "revisao_anterior_id": carregado["painel"]["id"],
    }


def _esclarecer(carregado, motivo: str, indicadores: dict, *, parametros: list) -> dict:
    codigo = motivo if motivo in _MOTIVOS or _codigo_publico(motivo) else "pedido_sem_alteracao"
    consulta = carregado.get("consulta")
    campos = _campos_ia(indicadores)
    return {
        "ok": True,
        "situacao": "esclarecimento",
        "reusada": False,
        "chamada_anthropic": campos["chamada_anthropic"] is True,
        "cobranca": campos["cobranca"] is True,
        "indicadores_ia": campos["indicadores_ia"],
        "parametros": {},
        "esclarecimento": {
            "motivo": codigo,
            "pergunta": _pergunta(codigo, parametros),
            "parametros": [item for item in parametros if isinstance(item, str)],
        },
        "dashboard": carregado["painel"],
        "consulta": consulta,
        "revisao_anterior_id": None,
    }


def _indicadores_do_pedido(interpretado: dict) -> dict:
    from app.services.agente_bi_anthropic_service import indicadores_ia_vazios

    bruto = interpretado.get("indicadores_ia") if isinstance(interpretado, dict) else None
    if isinstance(bruto, dict):
        return bruto
    return indicadores_ia_vazios()


def _campos_ia(indicadores: dict) -> dict:
    from app.services.agente_bi_anthropic_service import campos_publicos_ia

    return campos_publicos_ia(indicadores)


def _chamada_contada(indicadores: dict) -> bool:
    return _campos_ia(indicadores)["chamada_anthropic"] is True


def _pergunta(motivo: str, parametros: list) -> str:
    if motivo == "informar_parametro" and parametros:
        return "Informe o valor de " + ", ".join(parametros) + ". Não vou inventar esse valor."
    return _MOTIVOS.get(motivo, "Não apliquei a alteração. O pedido precisa de um esclarecimento explícito.")


def _pedido(corpo) -> tuple[str | None, dict, int | None]:
    if not isinstance(corpo, dict) or not set(corpo) <= _CHAVES_PEDIDO:
        raise InterpretacaoErro("payload_invalido")
    if "texto" not in corpo and "plano_id" not in corpo:
        raise InterpretacaoErro("payload_invalido")
    plano_id = None
    if "plano_id" in corpo:
        plano_id = corpo.get("plano_id")
        if isinstance(plano_id, bool) or not isinstance(plano_id, int) or plano_id < 1:
            raise InterpretacaoErro("payload_invalido")
    texto = None
    if "texto" in corpo:
        texto = corpo.get("texto")
        if not isinstance(texto, str) or not texto.strip() or len(texto) > 500 or "\x00" in texto:
            raise InterpretacaoErro("payload_invalido")
        texto = texto.strip()
    if "parametros" not in corpo:
        return texto, {}, plano_id
    parametros = corpo.get("parametros")
    if not isinstance(parametros, dict) or any(not isinstance(nome, str) or not nome for nome in parametros):
        raise CalculoErro("parametro_inconsistente")
    return texto, dict(parametros), plano_id


def _consulta_de_existentes(plano) -> list | None:
    """Gráficos já publicados que o plano só consulta. Alteração segue o fluxo atual."""
    operacoes = plano.get("operacoes") if isinstance(plano, dict) else None
    if not isinstance(operacoes, list) or not operacoes:
        return None
    refs = []
    for operacao in operacoes:
        if not isinstance(operacao, dict) or operacao.get("acao") != "consultar":
            return None
        grafico = operacao.get("grafico")
        if not isinstance(grafico, str) or not grafico:
            return None
        if grafico not in refs:
            refs.append(grafico)
    return refs or None


def _motivo_plano(plano) -> str:
    if not isinstance(plano, dict):
        return "pedido_sem_alteracao"
    for operacao in plano.get("operacoes") or []:
        if not isinstance(operacao, dict):
            continue
        esclarecimento = operacao.get("esclarecimento")
        if isinstance(esclarecimento, dict) and isinstance(esclarecimento.get("motivo"), str):
            return esclarecimento["motivo"]
    return "pedido_sem_alteracao"


def _nomes_esclarecidos(itens) -> list:
    if not isinstance(itens, list):
        return []
    nomes = []
    for item in itens:
        if not isinstance(item, dict):
            continue
        nome = item.get("parametro")
        if isinstance(nome, str) and nome and nome not in nomes:
            nomes.append(nome)
    return nomes


def _parametros_publicos(parametros: dict) -> dict:
    if not isinstance(parametros, dict):
        return {}
    return {nome: valor for nome, valor in parametros.items() if isinstance(nome, str)}


def _codigo_publico(motivo: str) -> bool:
    return isinstance(motivo, str) and motivo.isidentifier() and motivo.islower() and len(motivo) <= 40
