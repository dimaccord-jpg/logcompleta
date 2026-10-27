"""Ligação operacional do AgenteBI (SCRUM-259).

Encadeia preparação já gravada, perfil semântico, contexto minimizado,
adaptador Anthropic governado e a fila durável. Sem `autorizado`, o
transporte é uma função injetada ou o gerador local. Com `autorizado`,
o mesmo ciclo usa o modelo configurado, a instrução do rascunho e o
SDK quando nenhum transporte é injetado. Este módulo não altera teto,
lease nem preço.

`apto_anthropic=False` permanece um invariante do contexto bloqueado.
A liberação usa a representação aprovada. `allowed` entrega esse conteúdo.
`minimized` só segue se a referência técnica continuar íntegra depois da
revalidação. Bloqueio técnico, governança indisponível ou resultado
incoerente impedem a transferência sem nova tentativa. Bloqueio por
conteúdo pode repetir uma vez, só sem as anotações opcionais; a segunda
decisão vale. A fila continua dona dos estados. O
processador entrega à confirmação o código e o documento `analisar.v1`
já validado, gravado na mesma transação. Quem devolve só o código
continua em `execucao_simulada.v1`. O coordenador devolve o documento
confirmado relido da análise, sem usar estado de contexto como retorno.
"""
from __future__ import annotations

import copy
import json
import os
import re

from app.extensions import db
from app.models import AgenteBiAnalise, AgenteBiExecucao, utcnow_naive
from app.services.agente_bi_analisar_service import (
    CONTRATO_ANALISAR,
    CONTRATO_CONTEXTO_ANALISAR,
    CATALOGO_CONTRATO,
    PROVEDOR_ANTHROPIC,
    AnalisarErro,
    TransferenciaRecusada,
    analisar,
    contexto_de_transferencia,
    esquema_rascunho_analisar,
    governar_representacao,
    instrucao_de_rascunho,
    montar_contexto,
    provedor_simulado,
    referencias_da_representacao,
    representacao_semantica,
    validar_saida,
)
from app.services.agente_bi_anthropic_service import (
    FLAG_MAX_TOKENS,
    FLAG_MODELO,
    TETO_MAX_TOKENS,
    ErroTransporte,
    chamar_anthropic_governado,
    preparar_saida_estruturada,
)
from app.services.agente_bi_execucao_service import (
    EntregaProcessador,
    ExecucaoErro,
    enfileirar,
    execucao_para_dict,
    executar_um_ciclo,
)
from app.services.agente_bi_perfil_semantico_service import (
    FINALIDADE_TRANSFERENCIA,
    PerfilErro,
    perfilar_dataset,
)
from app.services.agente_bi_persistencia_service import (
    AgenteBiErro,
    EscopoAgenteBi,
    criar_analise,
    obter_dataset,
)
from app.services.cleiton_ai_data_governance import (
    DECISION_ALLOW,
    DECISION_MINIMIZE,
    govern_outbound_content,
)
from app.services.cleiton_ai_privacy_classifier import SUPPORTED_PURPOSES

MODELO_SIMULADO = "claude-sonnet-simulado"
MAX_TOKENS_SIMULADO = 256
MOTIVO_LIBERACAO = "contexto_verificado"

_CODIGO_RE_FILA = re.compile(r"^[a-z0-9_]{1,40}$")
_CHAVES_PAYLOAD = frozenset(
    {
        "contrato",
        "amostras",
        "valores_de_celula",
        "rotulos",
        "apto_anthropic",
        "autorizado_para_provedor",
        "transferencia",
        "modalidades",
        "intencoes",
        "regioes",
        "colunas",
        "relacoes",
        "qualidade",
        "orcamento_relacoes",
    }
)
_CHAVES_CONTEUDO = frozenset(
    {
        "valor",
        "texto",
        "formula",
        "rotulo",
        "dados_tipados",
        "conteudo",
        "mensagem",
        "email",
        "cpf",
        "nome",
        "system",
    }
)
class IntegracaoErro(Exception):
    def __init__(self, codigo: str):
        self.codigo = codigo
        super().__init__(codigo)


def validar_resposta_simulada(contexto: dict, documento: dict) -> dict:
    """Reusa o contrato fechado. Não corrige a proposta e não chama provedor."""
    return validar_saida(contexto, documento)


def autorizar_transferencia(perfil: dict, contexto: dict, *, estrutura=None) -> dict:
    """Libera a representação semântica que a governança devolver.

    O flag `apto_anthropic=False` continua invariante. `allowed` usa o
    conteúdo aprovado. `minimized` passa só depois da revalidação das
    referências. Bloqueio técnico, falha ou resultado incoerente não
    transferem. Se a intenção foi removida na representação reduzida, a
    liberação não a trata como entregue.
    """
    if not isinstance(perfil, dict):
        raise IntegracaoErro("contexto_nao_autorizado")
    _governanca_explicita(perfil.get("governanca"))
    try:
        estrutural = contexto_de_transferencia(contexto)
        representacao = representacao_semantica(contexto, estrutura)
    except AnalisarErro as exc:
        raise IntegracaoErro(_codigo(exc.codigo)) from exc
    _estrutura_fechada(estrutural)
    if referencias_da_representacao(estrutural) != referencias_da_representacao(representacao):
        raise IntegracaoErro("contexto_nao_autorizado")
    _pertence_ao_perfil(perfil, representacao)
    try:
        autorizado = governar_representacao(representacao, governar=govern_outbound_content)
    except TransferenciaRecusada as exc:
        raise IntegracaoErro("contexto_nao_autorizado") from exc
    verificacoes = [
        "varredura_comprovada",
        "finalidade_nao_basta",
        "estrutura_fechada",
        "governanca_emitida",
        "referencias_preservadas",
    ]
    removidas = bool(autorizado.get("anotacoes_removidas"))
    if removidas:
        verificacoes.append("anotacoes_opcionais_removidas")
    intencao_original = representacao.get("intencao")
    intencao_final = autorizado["payload"].get("intencao") if isinstance(autorizado.get("payload"), dict) else None
    intencao_ausente = isinstance(intencao_original, str) and bool(intencao_original) and not isinstance(intencao_final, str)
    if intencao_ausente:
        verificacoes.append("intencao_nao_transportada")
    if autorizado["decisao"] == DECISION_ALLOW:
        verificacoes.append("decisao_allowed")
        if not removidas and not intencao_ausente:
            verificacoes.append("conteudo_identico")
    elif autorizado["decisao"] == DECISION_MINIMIZE:
        verificacoes.append("conteudo_minimizado_revalidado")
    else:
        raise IntegracaoErro("contexto_nao_autorizado")
    return {
        "liberado": True,
        "motivo": MOTIVO_LIBERACAO,
        "payload": autorizado["payload"],
        "verificacoes": verificacoes,
    }


def abrir_operacao(
    escopo: EscopoAgenteBi,
    dataset_id: int,
    pedido: dict,
    *,
    chave_analise: str,
    rodada: int = 1,
) -> dict:
    """Perfila, autoriza e enfileira uma rodada sem resultado analítico."""
    perfil = _perfil_ou_erro(escopo, dataset_id)
    estrutura = _estrutura_do_dataset(escopo, dataset_id)
    try:
        contexto = montar_contexto(perfil, pedido)
    except AnalisarErro as exc:
        raise IntegracaoErro(_codigo(exc.codigo)) from exc
    autorizar_transferencia(perfil, contexto, estrutura=estrutura)
    dataset = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    if dataset.get("id") != int(dataset_id):
        raise IntegracaoErro("identidade_inconsistente")
    existente = (
        AgenteBiAnalise.query.filter_by(
            usuario_id=escopo.usuario_id,
            conta_id=escopo.conta_id,
            franquia_id=escopo.franquia_id,
            chave=chave_analise,
            rodada=int(rodada),
        ).one_or_none()
    )
    if existente is not None:
        if int(existente.dataset_id) != int(dataset_id):
            raise IntegracaoErro("identidade_inconsistente")
        return {
            "analise_id": int(existente.id),
            "execucao": enfileirar(escopo, int(existente.id)),
            "reusada": True,
        }
    try:
        analise = criar_analise(
            escopo,
            {
                "chave": chave_analise,
                "rodada": int(rodada),
                "dataset_id": int(dataset_id),
                "versao": dataset["versao"],
                "impressao": dataset["impressao"],
                "etapa": "descoberta",
                "estado": "preparada",
                "contexto": {
                    "contrato": CONTRATO_CONTEXTO_ANALISAR,
                    "vinculo_preparacao": contexto["vinculo_preparacao"],
                    "dataset_id": int(dataset_id),
                },
                "regras_negocio": {"catalogo": CATALOGO_CONTRATO, "pedido": pedido},
                "esclarecimentos": {},
                "contrato_versao": CONTRATO_ANALISAR,
            },
        )
    except AgenteBiErro as exc:
        raise IntegracaoErro(_codigo(exc.codigo)) from exc
    return {
        "analise_id": int(analise["id"]),
        "execucao": enfileirar(escopo, int(analise["id"])),
        "reusada": False,
    }


def processar_transporte_simulado(
    referencias: dict,
    *,
    transporte=None,
    autorizado: bool = False,
) -> EntregaProcessador:
    """Um ciclo da fila. Sem autorização, recusa o SDK e grava proveniência simulada."""
    linha = _exigir_execucao(referencias)
    if linha.lease_worker is None or linha.lease_ate is None:
        raise ExecucaoErro("lease_expirado")
    if linha.lease_ate <= utcnow_naive():
        raise ExecucaoErro("lease_expirado")
    escopo = EscopoAgenteBi(
        conta_id=int(linha.conta_id),
        franquia_id=int(linha.franquia_id),
        usuario_id=int(linha.usuario_id),
    )
    if int(referencias.get("dataset_id")) != _dataset_da_analise(linha):
        raise ExecucaoErro("identidade_inconsistente")
    pedido = _pedido_gravado(linha)
    try:
        perfil = _perfil_ou_erro(escopo, int(referencias["dataset_id"]))
        contexto = montar_contexto(perfil, pedido)
        estrutura = _estrutura_do_dataset(escopo, int(referencias["dataset_id"]))
        liberacao = autorizar_transferencia(perfil, contexto, estrutura=estrutura)
    except IntegracaoErro as exc:
        raise ExecucaoErro(_codigo(exc.codigo)) from exc
    except AnalisarErro as exc:
        raise ExecucaoErro(_codigo(exc.codigo)) from exc
    payload = copy.deepcopy(liberacao["payload"])
    mensagem = _canonico(payload)
    if autorizado:
        if transporte is not None and not callable(transporte):
            raise ExecucaoErro("transporte_real_bloqueado")
        envio = transporte
        modelo = None
        limite = None
        sistema = instrucao_de_rascunho()
        config = preparar_saida_estruturada((os.getenv(FLAG_MODELO) or "").strip(), esquema_rascunho_analisar())
    else:
        envio = transporte or _transporte_padrao(payload, mensagem)
        if not callable(envio):
            raise ExecucaoErro("transporte_real_bloqueado")
        modelo = MODELO_SIMULADO
        limite = _max_tokens_simulado()
        sistema = ""
        config = None
    resultado = chamar_anthropic_governado(
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
        mensagem=mensagem,
        chave_idempotencia=linha.chave_idempotencia,
        execucao_id=int(linha.id),
        worker_id=linha.lease_worker,
        fencing=int(linha.fencing),
        transporte=envio,
        model=modelo,
        max_tokens=limite,
        system=sistema,
        output_config=config,
    )
    if resultado.get("incerto"):
        raise ExecucaoErro("efeito_incerto")
    if not resultado.get("ok"):
        _soltar_efeito_para_falha(linha)
        raise ExecucaoErro(_codigo(str(resultado.get("codigo") or "")))
    proveniencia = None
    if autorizado:
        if resultado.get("provedor") != PROVEDOR_ANTHROPIC or resultado.get("provedor_chamado") is not True:
            _soltar_efeito_para_falha(linha)
            raise ExecucaoErro("contrato_saida_invalido")
        proveniencia = PROVEDOR_ANTHROPIC
    try:
        proposta = _proposta_do_texto(resultado.get("texto"))
        saida = analisar(perfil, pedido, provedor=lambda _contexto: proposta, proveniencia=proveniencia)
    except AnalisarErro as exc:
        _soltar_efeito_para_falha(linha)
        raise ExecucaoErro(_codigo(exc.codigo)) from exc
    if saida.get("contrato") != CONTRATO_ANALISAR or saida.get("apto_anthropic") is not False:
        _soltar_efeito_para_falha(linha)
        raise ExecucaoErro("contrato_saida_invalido")
    if autorizado:
        if saida.get("provedor") != PROVEDOR_ANTHROPIC or saida.get("chamada_anthropic") is not True:
            _soltar_efeito_para_falha(linha)
            raise ExecucaoErro("contrato_saida_invalido")
        return EntregaProcessador(PROVEDOR_ANTHROPIC, saida)
    if saida.get("provedor") != "simulado" or saida.get("chamada_anthropic") is not False:
        _soltar_efeito_para_falha(linha)
        raise ExecucaoErro("autorizacao_indevida")
    return EntregaProcessador("simulado", saida)


def executar_operacao(
    escopo: EscopoAgenteBi,
    dataset_id: int,
    pedido: dict,
    *,
    worker_id: str,
    chave_analise: str,
    transporte=None,
    rodada: int = 1,
    autorizado: bool = False,
    lease_segundos: int | None = None,
) -> dict:
    """Abre a rodada e processa somente essa execução.

    A fila global do worker permanece. Este caminho adquire o id aberto
    para o escopo e não executa outro item no lugar. Se essa execução já
    estiver com outro worker, devolve o estado de processamento sem
    consumir a fila. O documento é o confirmado na análise, relido do banco.
    """
    abertura = abrir_operacao(
        escopo,
        dataset_id,
        pedido,
        chave_analise=chave_analise,
        rodada=rodada,
    )
    execucao_id = int(abertura["execucao"]["id"])
    analise_id = int(abertura["analise_id"])
    _exigir_execucao_pedida(escopo, execucao_id, analise_id)

    def processador(refs):
        if int(refs.get("execucao_id") or 0) != execucao_id:
            raise ExecucaoErro("identidade_inconsistente")
        return processar_transporte_simulado(refs, transporte=transporte, autorizado=autorizado)

    ciclo = executar_um_ciclo(
        worker_id=worker_id,
        processador=processador,
        execucao_id=execucao_id,
        **({} if lease_segundos is None else {"lease_segundos": lease_segundos}),
    )
    ciclo = _ciclo_da_operacao(escopo, execucao_id, analise_id, ciclo)
    saida = None if ciclo.get("em_andamento") else _documento_confirmado(escopo, analise_id)
    return {"abertura": abertura, "ciclo": ciclo, "saida": saida}


def _soltar_efeito_para_falha(linha: AgenteBiExecucao) -> None:
    """Devolve o efeito quando o documento não será confirmado.

    A resposta válida permanece incerta até o commit de analisar.v1.
    Soltar separa falha conhecida de efeito incerto. Se a chamada já foi
    contabilizada, a fila encerra em falha definitiva e não readquire.
    """
    from app.services.agente_bi_anthropic_service import _liberar_efeito

    if linha.lease_worker is None or linha.fencing is None:
        return
    _liberar_efeito(
        int(linha.id),
        {
            "usuario_id": int(linha.usuario_id),
            "conta_id": int(linha.conta_id),
            "franquia_id": int(linha.franquia_id),
        },
        linha.lease_worker,
        int(linha.fencing),
    )


def _governanca_explicita(governanca) -> None:
    if not isinstance(governanca, dict):
        raise IntegracaoErro("contexto_nao_autorizado")
    if FINALIDADE_TRANSFERENCIA not in SUPPORTED_PURPOSES:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("finalidade_aprovada") is not True:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("cadastrar_finalidade_nao_autoriza") is not True:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("varredura") != "comprovada":
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("montagem_contexto") != "local_nao_autorizada":
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("decisao") != DECISION_ALLOW:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("amostras_de_linhas") is not False or governanca.get("valores_de_celula") is not False:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("texto_de_celula_excluido") is not True:
        raise IntegracaoErro("contexto_nao_autorizado")
    if governanca.get("apto_anthropic") is not False or governanca.get("autorizado_para_provedor") is not False:
        raise IntegracaoErro("autorizacao_indevida")
    if governanca.get("transferencia") != "bloqueada":
        raise IntegracaoErro("autorizacao_indevida")
    contexto = governanca.get("contexto_ia")
    if not isinstance(contexto, dict):
        raise IntegracaoErro("contexto_nao_autorizado")
    if contexto.get("amostras_de_linhas") is not False or contexto.get("valores_de_celula") is not False:
        raise IntegracaoErro("contexto_nao_autorizado")
    autorizacao = contexto.get("autorizacao")
    if not isinstance(autorizacao, dict):
        raise IntegracaoErro("contexto_nao_autorizado")
    if autorizacao.get("decisao") != DECISION_ALLOW:
        raise IntegracaoErro("contexto_nao_autorizado")
    if autorizacao.get("cadastrar_finalidade_nao_autoriza") is not True:
        raise IntegracaoErro("contexto_nao_autorizado")
    if autorizacao.get("apto_anthropic") is not False or autorizacao.get("autorizado_para_provedor") is not False:
        raise IntegracaoErro("autorizacao_indevida")


def _estrutura_fechada(payload: dict) -> None:
    if not isinstance(payload, dict) or set(payload) != _CHAVES_PAYLOAD:
        raise IntegracaoErro("contexto_nao_autorizado")
    if payload.get("contrato") != CONTRATO_CONTEXTO_ANALISAR:
        raise IntegracaoErro("contexto_nao_autorizado")
    if payload.get("amostras") is not False or payload.get("valores_de_celula") is not False:
        raise IntegracaoErro("contexto_nao_autorizado")
    if payload.get("rotulos") is not False:
        raise IntegracaoErro("contexto_nao_autorizado")
    if payload.get("apto_anthropic") is not False or payload.get("autorizado_para_provedor") is not False:
        raise IntegracaoErro("autorizacao_indevida")
    if payload.get("transferencia") != "bloqueada":
        raise IntegracaoErro("autorizacao_indevida")
    _recusar_sujo(payload)


def _recusar_sujo(valor, profundidade: int = 0) -> None:
    if profundidade > 8:
        raise IntegracaoErro("contexto_nao_autorizado")
    if valor is None or isinstance(valor, bool):
        return
    if isinstance(valor, int):
        if valor < 0 or valor > 1_000_000:
            raise IntegracaoErro("contexto_nao_autorizado")
        return
    if isinstance(valor, str):
        if len(valor) > 64 or any(ord(caractere) < 32 for caractere in valor):
            raise IntegracaoErro("contexto_nao_autorizado")
        if any(marca in valor for marca in ("=", "@", "/", "\\")):
            raise IntegracaoErro("contexto_nao_autorizado")
        if sum(caractere.isdigit() for caractere in valor) >= 6:
            raise IntegracaoErro("contexto_nao_autorizado")
        return
    if isinstance(valor, list):
        if len(valor) > 64:
            raise IntegracaoErro("contexto_nao_autorizado")
        for item in valor:
            _recusar_sujo(item, profundidade + 1)
        return
    if isinstance(valor, dict):
        if len(valor) > 32:
            raise IntegracaoErro("contexto_nao_autorizado")
        for chave, item in valor.items():
            if not isinstance(chave, str) or chave in _CHAVES_CONTEUDO:
                raise IntegracaoErro("contexto_nao_autorizado")
            _recusar_sujo(item, profundidade + 1)
        return
    raise IntegracaoErro("contexto_nao_autorizado")


def _transporte_padrao(payload: dict, mensagem: str):
    def enviar(pedido: dict):
        mensagens = pedido.get("messages") if isinstance(pedido, dict) else None
        if not isinstance(mensagens, list) or not mensagens:
            raise ErroTransporte("pedido_recusado", incerto=False)
        conteudo = mensagens[0].get("content") if isinstance(mensagens[0], dict) else None
        if conteudo != mensagem:
            raise ErroTransporte("pedido_recusado", incerto=False)
        try:
            proposta = provedor_simulado(payload)
        except AnalisarErro as exc:
            raise ErroTransporte("pedido_recusado", incerto=False) from exc
        return {
            "texto": _canonico(proposta),
            "stop_reason": "end_turn",
            "input_tokens": None,
            "output_tokens": None,
            "recusa": False,
            "truncada": False,
        }

    return enviar


def _proposta_do_texto(texto) -> dict:
    if not isinstance(texto, str) or not texto.strip():
        raise AnalisarErro("contrato_saida_invalido")
    try:
        resposta = json.loads(texto)
    except json.JSONDecodeError as exc:
        raise AnalisarErro("contrato_saida_invalido") from exc
    if not isinstance(resposta, dict) or set(resposta) != {"descobertas", "sugestoes"}:
        raise AnalisarErro("contrato_saida_invalido")
    return resposta


def _estrutura_do_dataset(escopo: EscopoAgenteBi, dataset_id: int):
    try:
        corpo = obter_dataset(escopo, int(dataset_id))
    except AgenteBiErro as exc:
        raise IntegracaoErro(_codigo(exc.codigo)) from exc
    estrutura = corpo.get("estrutura") if isinstance(corpo, dict) else None
    if not isinstance(estrutura, dict):
        raise IntegracaoErro("contexto_nao_autorizado")
    return estrutura


def _pertence_ao_perfil(perfil: dict, representacao: dict) -> None:
    if representacao.get("vinculo_preparacao") != perfil.get("vinculo_preparacao"):
        raise IntegracaoErro("contexto_nao_autorizado")
    dataset = perfil.get("dataset") if isinstance(perfil.get("dataset"), dict) else {}
    obtido = representacao.get("dataset") if isinstance(representacao.get("dataset"), dict) else {}
    for chave in ("id", "versao", "impressao"):
        if chave in dataset and obtido.get(chave) != dataset.get(chave):
            raise IntegracaoErro("contexto_nao_autorizado")
    colunas = set()
    for item in perfil.get("colunas") or []:
        if isinstance(item, dict):
            colunas.add((item.get("arquivo"), item.get("aba"), item.get("regiao"), item.get("indice")))
    if any(coord not in colunas for coord in referencias_da_representacao(representacao)):
        raise IntegracaoErro("contexto_nao_autorizado")
    regioes = {
        (item.get("arquivo"), item.get("aba"), item.get("regiao"))
        for item in perfil.get("regioes") or []
        if isinstance(item, dict)
    }
    for item in representacao.get("regioes") or []:
        if not isinstance(item, dict):
            raise IntegracaoErro("contexto_nao_autorizado")
        if (item.get("arquivo"), item.get("aba"), item.get("regiao")) not in regioes:
            raise IntegracaoErro("contexto_nao_autorizado")
    for chave in ("usuario_id", "conta_id", "franquia_id", "escopo"):
        if chave in representacao:
            raise IntegracaoErro("contexto_nao_autorizado")


def _perfil_ou_erro(escopo: EscopoAgenteBi, dataset_id: int) -> dict:
    try:
        return perfilar_dataset(escopo, int(dataset_id))
    except (PerfilErro, AgenteBiErro) as exc:
        raise IntegracaoErro(_codigo(exc.codigo)) from exc


def _exigir_execucao(referencias: dict) -> AgenteBiExecucao:
    for campo in ("execucao_id", "analise_id", "dataset_id", "usuario_id", "conta_id", "franquia_id"):
        valor = referencias.get(campo)
        if isinstance(valor, bool) or not isinstance(valor, int):
            raise ExecucaoErro("identidade_inconsistente")
    linha = db.session.get(AgenteBiExecucao, int(referencias["execucao_id"]))
    if linha is None or linha.estado != "adquirida":
        raise ExecucaoErro("fencing_recusado")
    if (
        int(linha.analise_id) != int(referencias["analise_id"])
        or int(linha.usuario_id) != int(referencias["usuario_id"])
        or int(linha.conta_id) != int(referencias["conta_id"])
        or int(linha.franquia_id) != int(referencias["franquia_id"])
    ):
        raise ExecucaoErro("identidade_inconsistente")
    return linha


def _dataset_da_analise(linha: AgenteBiExecucao) -> int:
    analise = AgenteBiAnalise.query.filter_by(
        id=linha.analise_id,
        usuario_id=linha.usuario_id,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
    ).one_or_none()
    if analise is None or analise.dataset_id is None:
        raise ExecucaoErro("identidade_inconsistente")
    return int(analise.dataset_id)


def _pedido_gravado(linha: AgenteBiExecucao) -> dict:
    analise = AgenteBiAnalise.query.filter_by(
        id=linha.analise_id,
        usuario_id=linha.usuario_id,
        conta_id=linha.conta_id,
        franquia_id=linha.franquia_id,
    ).one_or_none()
    if analise is None:
        raise ExecucaoErro("identidade_inconsistente")
    try:
        regras = json.loads(analise.regras_negocio_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ExecucaoErro("pedido_invalido") from exc
    pedido = regras.get("pedido") if isinstance(regras, dict) else None
    if not isinstance(pedido, dict):
        raise ExecucaoErro("pedido_invalido")
    return pedido


def _max_tokens_simulado() -> int:
    """Usa o teto já configurado. Não eleva o limite do adaptador."""
    bruto = (os.getenv(FLAG_MAX_TOKENS) or "").strip()
    teto = TETO_MAX_TOKENS
    if bruto.isdigit():
        valor = int(bruto)
        if 1 <= valor <= TETO_MAX_TOKENS:
            teto = valor
    return min(MAX_TOKENS_SIMULADO, teto)


def _canonico(valor) -> str:
    return json.dumps(valor, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _codigo(codigo: str) -> str:
    if isinstance(codigo, str) and _CODIGO_RE_FILA.fullmatch(codigo):
        return codigo
    return "falha_processamento"


def _exigir_execucao_pedida(escopo: EscopoAgenteBi, execucao_id: int, analise_id: int) -> AgenteBiExecucao:
    linha = AgenteBiExecucao.query.filter_by(
        id=int(execucao_id),
        analise_id=int(analise_id),
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
    ).one_or_none()
    if linha is None:
        raise IntegracaoErro("identidade_inconsistente")
    return linha


def _ciclo_da_operacao(escopo: EscopoAgenteBi, execucao_id: int, analise_id: int, ciclo: dict) -> dict:
    if ciclo.get("adquirida"):
        execucao = ciclo.get("execucao")
        if not isinstance(execucao, dict):
            raise IntegracaoErro("identidade_inconsistente")
        _conferir_identidade(escopo, execucao_id, analise_id, execucao)
        return ciclo
    linha = _exigir_execucao_pedida(escopo, execucao_id, analise_id)
    db.session.refresh(linha)
    if linha.estado == "adquirida":
        return {
            "adquirida": False,
            "em_andamento": True,
            "execucao": execucao_para_dict(linha),
        }
    return ciclo


def _conferir_identidade(escopo: EscopoAgenteBi, execucao_id: int, analise_id: int, execucao: dict) -> None:
    try:
        mesma = (
            int(execucao["id"]) == int(execucao_id)
            and int(execucao["analise_id"]) == int(analise_id)
            and int(execucao["usuario_id"]) == int(escopo.usuario_id)
            and int(execucao["conta_id"]) == int(escopo.conta_id)
            and int(execucao["franquia_id"]) == int(escopo.franquia_id)
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise IntegracaoErro("identidade_inconsistente") from exc
    if not mesma:
        raise IntegracaoErro("identidade_inconsistente")


def _documento_confirmado(escopo: EscopoAgenteBi, analise_id: int) -> dict | None:
    """Relê a análise confirmada. O documento em memória do processador não volta."""
    analise = AgenteBiAnalise.query.filter_by(
        id=int(analise_id),
        usuario_id=escopo.usuario_id,
        conta_id=escopo.conta_id,
        franquia_id=escopo.franquia_id,
    ).one_or_none()
    if analise is None:
        raise IntegracaoErro("identidade_inconsistente")
    db.session.refresh(analise)
    if analise.resultado_json is None:
        return None
    try:
        documento = json.loads(analise.resultado_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise IntegracaoErro("identidade_inconsistente") from exc
    if not isinstance(documento, dict) or documento.get("contrato") != CONTRATO_ANALISAR:
        return None
    trilha = documento.get("trilha")
    if not isinstance(trilha, dict):
        raise IntegracaoErro("identidade_inconsistente")
    if (
        trilha.get("dataset_id") != int(analise.dataset_id)
        or trilha.get("dataset_versao") != int(analise.dataset_versao)
        or trilha.get("dataset_impressao") != analise.dataset_impressao
    ):
        raise IntegracaoErro("identidade_inconsistente")
    return documento
