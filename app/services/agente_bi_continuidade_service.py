"""Continuidade mínima do AgenteBI entre recargas (SCRUM-271).

Relê plano, esclarecimento e vínculo de revisão que a interpretação e a
aplicação já gravaram. Não cria chat, não copia resposta do modelo e não
grava valor de filtro: a política vigente guarda nome, tipo e operador na
definição, e o agregado na memória de cálculo.

Um plano só volta se ainda for o deste dashboard e desta revisão. Revisão
que deixou de ser a ponta da cadeia não reabre esclarecimento antigo.
"""
from __future__ import annotations

import json
import logging

from app.models import AgenteBiAnalise, AgenteBiAplicacao
from app.services.agente_bi_dashboard_service import _proxima_revisao
from app.services.agente_bi_interpretacao_v2_service import (
    CONTRATO_PATCH_V2,
    InterpretacaoErro,
    recuperar_contexto,
)
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    obter_analise,
    obter_dashboard,
)

logger = logging.getLogger(__name__)

_LIMITE_PLANOS = 200
_CHAVES = (
    "dashboard_id",
    "revisao",
    "situacao",
    "plano_id",
    "aplicacao_id",
    "revisao_base",
    "revisao_publicada",
    "definicao_id",
    "calculo_id",
    "esclarecimento",
)


def recuperar_continuidade(escopo, dashboard_id: int, consulta: dict | None = None) -> dict:
    """Devolve o estado mínimo compatível com a revisão aberta. Não chama provedor."""
    if consulta is None:
        from app.services.agente_bi_dashboard_service import consultar_dashboard

        consulta = consultar_dashboard(escopo, dashboard_id)
    painel = consulta.get("dashboard") if isinstance(consulta, dict) else None
    if not isinstance(painel, dict) or int(painel.get("id") or 0) != int(dashboard_id):
        raise AgenteBiErro("nao_encontrado", 404)
    if (
        int(painel.get("usuario_id") or 0) != int(escopo.usuario_id)
        or int(painel.get("conta_id") or 0) != int(escopo.conta_id)
        or int(painel.get("franquia_id") or 0) != int(escopo.franquia_id)
    ):
        raise AgenteBiErro("nao_encontrado", 404)
    pendente = _plano_pendente(escopo, painel, consulta) if _e_ponta(escopo, painel) else None
    if pendente is not None:
        _registrar(escopo, pendente)
        return pendente
    publicada = _aplicacao_publicada(escopo, painel, consulta)
    if publicada is not None:
        _registrar(escopo, publicada)
        return publicada
    ausente = _ausente(painel)
    _registrar(escopo, ausente)
    return ausente


def texto_do_plano_compativel(escopo, dashboard_id: int, plano_id: int) -> str:
    """Relê a intenção já gravada no plano, só se o dashboard e a revisão forem os mesmos.

    O texto não é devolvido na continuidade. Sai daqui para a publicação
    reutilizar a idempotência do plano, sem o cliente repetir a pergunta.
    """
    from app.services.agente_bi_publicacao_v2_service import PublicacaoErro

    if isinstance(plano_id, bool) or not isinstance(plano_id, int) or plano_id < 1:
        raise PublicacaoErro("payload_invalido")
    if isinstance(dashboard_id, bool) or not isinstance(dashboard_id, int) or dashboard_id < 1:
        raise PublicacaoErro("payload_invalido")
    try:
        registro = obter_analise(escopo, plano_id)
    except AgenteBiErro as exc:
        raise PublicacaoErro(exc.codigo, exc.status) from exc
    try:
        contexto = recuperar_contexto(escopo, plano_id)
    except InterpretacaoErro as exc:
        codigo = "conflito_versao" if exc.codigo == "identidade_inconsistente" else exc.codigo
        raise PublicacaoErro(codigo, exc.status) from exc
    if int(contexto["dashboard_id"]) != int(dashboard_id):
        raise PublicacaoErro("conflito_versao", 409)
    painel = obter_dashboard(escopo, dashboard_id)
    if int(painel["revisao"]) != int(contexto["revisao_base"]):
        raise PublicacaoErro("conflito_versao", 409)
    if (
        int(painel["usuario_id"]) != int(escopo.usuario_id)
        or int(painel["conta_id"]) != int(escopo.conta_id)
        or int(painel["franquia_id"]) != int(escopo.franquia_id)
    ):
        raise PublicacaoErro("nao_encontrado", 404)
    regras = registro.get("regras_negocio")
    texto = regras.get("texto") if isinstance(regras, dict) else None
    if not isinstance(texto, str) or not texto.strip() or len(texto) > 500 or "\x00" in texto:
        raise PublicacaoErro("identidade_inconsistente", 409)
    return texto.strip()


def _registrar(escopo, estado: dict) -> None:
    logger.info(
        "evento=agente_bi_continuidade_recuperada usuario_id=%s dashboard_id=%s revisao=%s situacao=%s plano_id=%s",
        escopo.usuario_id,
        estado["dashboard_id"],
        estado["revisao"],
        estado["situacao"],
        estado["plano_id"],
    )


def _e_ponta(escopo, painel: dict) -> bool:
    chave = painel.get("chave")
    revisao = painel.get("revisao")
    if not isinstance(chave, str) or isinstance(revisao, bool) or not isinstance(revisao, int):
        return False
    return _proxima_revisao(escopo, chave) == revisao + 1


def _aplicacao_publicada(escopo, painel: dict, consulta: dict):
    linhas = (
        AgenteBiAplicacao.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            dashboard_publicado_id=int(painel["id"]),
            estado="concluida",
        )
        .order_by(AgenteBiAplicacao.id.desc())
        .limit(2)
        .all()
    )
    if len(linhas) != 1:
        return None
    aplicacao = linhas[0]
    corpo = consulta.get("consulta") if isinstance(consulta.get("consulta"), dict) else {}
    if int(aplicacao.revisao_publicada or 0) != int(painel["revisao"]):
        return None
    if not _mesmo_dataset(aplicacao, corpo):
        return None
    try:
        contexto = recuperar_contexto(escopo, int(aplicacao.plano_id))
    except InterpretacaoErro:
        return None
    if int(contexto["dashboard_id"]) != int(aplicacao.dashboard_id):
        return None
    if int(contexto["revisao_base"]) != int(aplicacao.revisao_base):
        return None
    return _estado(
        painel,
        situacao="validado",
        plano_id=int(aplicacao.plano_id),
        aplicacao_id=int(aplicacao.id),
        revisao_base=int(aplicacao.revisao_base),
        revisao_publicada=int(aplicacao.revisao_publicada),
        definicao_id=int(aplicacao.definicao_id) if aplicacao.definicao_id else None,
        calculo_id=int(aplicacao.calculo_id) if aplicacao.calculo_id else None,
        esclarecimento=None,
    )


def _plano_pendente(escopo, painel: dict, consulta: dict):
    corpo = consulta.get("consulta") if isinstance(consulta.get("consulta"), dict) else {}
    dataset_id = corpo.get("dataset_id")
    versao = corpo.get("versao")
    impressao = corpo.get("impressao")
    if isinstance(dataset_id, bool) or not isinstance(dataset_id, int):
        return None
    if isinstance(versao, bool) or not isinstance(versao, int) or not isinstance(impressao, str):
        return None
    linhas = (
        AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            contrato_versao=CONTRATO_PATCH_V2,
            estado="concluida",
            dataset_id=dataset_id,
            dataset_versao=versao,
            dataset_impressao=impressao,
        )
        .filter(AgenteBiAnalise.chave_recuperacao.isnot(None))
        .order_by(AgenteBiAnalise.id.desc())
        .limit(_LIMITE_PLANOS)
        .all()
    )
    for linha in linhas:
        if not isinstance(linha.chave_recuperacao, str) or not linha.chave_recuperacao.startswith("plano."):
            continue
        interno = _interno(linha)
        if interno is None:
            continue
        if int(interno.get("dashboard_id") or 0) != int(painel["id"]):
            continue
        if int(interno.get("revisao_base") or 0) != int(painel["revisao"]):
            continue
        if _aplicacao_concluida(escopo, int(linha.id)) is not None:
            continue
        if _so_consulta(linha):
            continue
        try:
            contexto = recuperar_contexto(escopo, int(linha.id))
        except InterpretacaoErro:
            return None
        if int(contexto["dashboard_id"]) != int(painel["id"]) or int(contexto["revisao_base"]) != int(painel["revisao"]):
            return None
        resultado = _resultado(linha)
        motivo, nomes = _motivo_e_nomes(resultado, contexto.get("parametros"))
        if motivo is not None:
            return _estado(
                painel,
                situacao="esclarecimento",
                plano_id=int(linha.id),
                aplicacao_id=None,
                revisao_base=int(painel["revisao"]),
                revisao_publicada=None,
                definicao_id=None,
                calculo_id=None,
                esclarecimento=_esclarecimento(motivo, nomes),
            )
        if _mutante(resultado):
            return _estado(
                painel,
                situacao="validado",
                plano_id=int(linha.id),
                aplicacao_id=None,
                revisao_base=int(painel["revisao"]),
                revisao_publicada=None,
                definicao_id=None,
                calculo_id=None,
                esclarecimento=None,
            )
        return None
    return None


def _aplicacao_concluida(escopo, plano_id: int):
    linha = AgenteBiAplicacao.query.filter_by(plano_id=int(plano_id), estado="concluida").one_or_none()
    if linha is None:
        return None
    if (
        int(linha.usuario_id) != int(escopo.usuario_id)
        or int(linha.conta_id) != int(escopo.conta_id)
        or int(linha.franquia_id) != int(escopo.franquia_id)
    ):
        return None
    return linha


def _mesmo_dataset(aplicacao, consulta: dict) -> bool:
    return (
        int(aplicacao.dataset_id) == int(consulta.get("dataset_id") or 0)
        and int(aplicacao.dataset_versao) == int(consulta.get("versao") or 0)
        and aplicacao.dataset_impressao == consulta.get("impressao")
    )


def _interno(linha: AgenteBiAnalise):
    try:
        contexto = json.loads(linha.contexto_json)
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(contexto, dict):
        return None
    interno = contexto.get("interno")
    return interno if isinstance(interno, dict) else None


def _resultado(linha: AgenteBiAnalise):
    if not linha.resultado_json:
        return None
    try:
        valor = json.loads(linha.resultado_json)
    except (TypeError, json.JSONDecodeError):
        return None
    return valor if isinstance(valor, dict) else None


def _so_consulta(linha: AgenteBiAnalise) -> bool:
    resultado = _resultado(linha)
    operacoes = resultado.get("operacoes") if isinstance(resultado, dict) else None
    if not isinstance(operacoes, list) or not operacoes:
        return False
    return all(isinstance(item, dict) and item.get("acao") == "consultar" for item in operacoes)


def _mutante(resultado) -> bool:
    operacoes = resultado.get("operacoes") if isinstance(resultado, dict) else None
    if not isinstance(operacoes, list):
        return False
    return any(isinstance(item, dict) and item.get("acao") not in {None, "consultar"} for item in operacoes)


def _motivo_e_nomes(resultado, vinculos):
    if not isinstance(resultado, dict):
        return None, []
    nomes = _nomes(resultado, vinculos)
    if resultado.get("situacao") == "esclarecimento":
        motivo = None
        for operacao in resultado.get("operacoes") or []:
            if not isinstance(operacao, dict):
                continue
            esclarecimento = operacao.get("esclarecimento")
            if isinstance(esclarecimento, dict) and isinstance(esclarecimento.get("motivo"), str):
                motivo = esclarecimento["motivo"]
                break
        return motivo or "pedido_sem_alteracao", nomes
    if resultado.get("situacao") == "proposto" and nomes:
        return "informar_parametro", nomes
    return None, nomes


def _nomes(resultado, vinculos) -> list:
    por_referencia = {}
    if isinstance(vinculos, list):
        for item in vinculos:
            if isinstance(item, dict) and isinstance(item.get("referencia"), str) and isinstance(item.get("nome"), str):
                por_referencia[item["referencia"]] = item["nome"]
    nomes = []
    for operacao in resultado.get("operacoes") or []:
        if not isinstance(operacao, dict):
            continue
        for parametro in operacao.get("parametros") or []:
            if not isinstance(parametro, dict):
                continue
            nome = None
            if parametro.get("forma") == "tipado" and isinstance(parametro.get("nome"), str):
                nome = parametro["nome"]
            elif parametro.get("forma") == "opaca":
                nome = por_referencia.get(parametro.get("referencia"))
            if isinstance(nome, str) and nome and nome not in nomes:
                nomes.append(nome)
    return nomes


def _esclarecimento(motivo: str, nomes: list) -> dict:
    from app.services.agente_bi_publicacao_v2_service import _pergunta

    return {
        "motivo": motivo,
        "pergunta": _pergunta(motivo, nomes),
        "parametros": list(nomes),
    }


def _ausente(painel: dict) -> dict:
    return _estado(
        painel,
        situacao="ausente",
        plano_id=None,
        aplicacao_id=None,
        revisao_base=None,
        revisao_publicada=None,
        definicao_id=None,
        calculo_id=None,
        esclarecimento=None,
    )


def _estado(
    painel: dict,
    *,
    situacao: str,
    plano_id,
    aplicacao_id,
    revisao_base,
    revisao_publicada,
    definicao_id,
    calculo_id,
    esclarecimento,
) -> dict:
    estado = {
        "dashboard_id": int(painel["id"]),
        "revisao": int(painel["revisao"]),
        "situacao": situacao,
        "plano_id": plano_id,
        "aplicacao_id": aplicacao_id,
        "revisao_base": revisao_base,
        "revisao_publicada": revisao_publicada,
        "definicao_id": definicao_id,
        "calculo_id": calculo_id,
        "esclarecimento": esclarecimento,
    }
    if tuple(estado) != _CHAVES:
        raise AgenteBiErro("identidade_invalida", 409)
    return estado
