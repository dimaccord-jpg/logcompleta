"""Adaptador Anthropic do AgenteBI.

Segue o caminho do AgenteAudita: autorização da finalidade, minimização,
chamada, IaConsumoEvento e motor oficial da franquia. Não reserva crédito
e não conclui a análise. A fila durável continua simulada.

A chamada faturável usa retries=0. Timeout, queda de conexão e 5xx não
são repetidos: a tentativa fica incerta. 429 encerra esta tentativa sem
nova chamada com a mesma chave.

O evento e o vínculo/estado da tentativa entram na mesma transação. O
motor oficial continua idempotente por evento e é reaplicado quando a
mesma chave impede outra chamada.

O timeout do SDK 2.3 é o do transporte httpx2, não um prazo total. Um
float vira pool, connect, write e read com o mesmo valor; a parede
possível é a soma dessas fases e pode passar do lease. Objeto de httpx
é recusado pelo SDK. O orçamento só reduz as fases configuradas para
caber no lease menos a margem. Isso não garante que a chamada termine
dentro do prazo. Sem orçamento, a tentativa e o evento não são criados.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from flask import has_app_context
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AgenteBiExecucao,
    Franquia,
    IaChamadaTentativa,
    IaConsumoEvento,
    User,
    utcnow_naive,
)
from app.services.cleiton_ai_data_governance import (
    CONTENT_TYPE_DOCUMENT,
    PURPOSE_BI_LOGISTICO,
    CleitonAiGovernanceBlockedError,
    govern_or_raise,
    project_safe_error,
)
from app.services.cleiton_ai_safe_context import CleitonAiAliasSession
from app.services.cleiton_franquia_operacional_service import (
    Q6,
    aplicar_motor_apos_ia_consumo_evento,
    decidir_admissao_chamada_ia,
    ensure_franquia_operacional_inicializada,
)
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia

logger = logging.getLogger(__name__)

PROVIDER = "anthropic"
PROVIDER_INTERNO = "internal"
AGENT = "agente_bi"
FLOW_TYPE = PURPOSE_BI_LOGISTICO
OPERATION = "messages"
TIPO_ORIGEM = "agente_bi_worker"
RETRIES = 0
TETO_MAX_TOKENS = 4096
TIMEOUT_PADRAO_SEGUNDOS = 30.0
TIMEOUT_TETO_SEGUNDOS = 120.0
# httpx2.Timeout(float) replica o valor nas quatro fases. Não é prazo total.
FASES_DO_FLOAT = 4
# O DEFAULT_TIMEOUT do SDK usa connect=5. Write segue o mesmo teto curto.
# Pool é aquisição local; o float do SDK o deixaria igual à leitura.
FASE_CONNECT_SEGUNDOS = 5.0
FASE_WRITE_SEGUNDOS = 5.0
FASE_POOL_SEGUNDOS = 1.0
LEITURA_MINIMA_SEGUNDOS = 1.0
# Evento, motor oficial, validação e commit de analisar.v1.
MARGEM_CONFIRMACAO_SEGUNDOS = 5.0
FLAG_HABILITADA = "AGENTE_BI_ANTHROPIC_ENABLED"
FLAG_CREDENCIAL = "ANTHROPIC_API_KEY"
FLAG_MODELO = "AGENTE_BI_ANTHROPIC_MODEL"
FLAG_MAX_TOKENS = "AGENTE_BI_ANTHROPIC_MAX_TOKENS"
FLAG_TIMEOUT = "AGENTE_BI_ANTHROPIC_TIMEOUT_SECONDS"
# Famílias com JSON Schema nativo documentado. Fora da lista, o prompt basta.
_FAMILIAS_SAIDA_ESTRUTURADA = (
    "claude-sonnet-4-5",
    "claude-opus-4-5",
    "claude-haiku-4-5",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-6",
)

_STATUS_OK = "success"
_STATUS_SEM_USO = "success_no_metrics"
_STATUS_FALHA = "failure"
_PAPEIS = frozenset({"user", "assistant"})
_CHAVE_PERMITIDA = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.:-")


class ErroTransporte(Exception):
    """Falha do transporte. O texto da exceção não entra em log nem em evento."""

    def __init__(
        self,
        codigo: str,
        *,
        incerto: bool,
        status: int | None = None,
        input_tokens: int | None = None,
        output_tokens: int | None = None,
    ) -> None:
        super().__init__(codigo)
        self.codigo = codigo
        self.incerto = incerto
        self.status = status
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


def fases_para_orcamento(limite_leitura: float, orcamento_parede: float) -> dict[str, float] | None:
    """Fases cuja soma cabe no orçamento. A leitura não passa do limite configurado."""
    if limite_leitura <= 0 or orcamento_parede <= 0:
        return None
    fixas = FASE_CONNECT_SEGUNDOS + FASE_WRITE_SEGUNDOS + FASE_POOL_SEGUNDOS
    leitura = min(float(limite_leitura), float(orcamento_parede) - fixas)
    if leitura < LEITURA_MINIMA_SEGUNDOS:
        return None
    return {
        "connect": FASE_CONNECT_SEGUNDOS,
        "read": leitura,
        "write": FASE_WRITE_SEGUNDOS,
        "pool": FASE_POOL_SEGUNDOS,
    }


def materializar_prazo(fases: dict[str, float]) -> Any:
    """Timeout aceito pelo SDK. Sem httpx2, o float conserva a parede na soma das fases."""
    try:
        import httpx2
    except ImportError:
        parede = fases["connect"] + fases["read"] + fases["write"] + fases["pool"]
        return parede / FASES_DO_FLOAT
    return httpx2.Timeout(
        fases["read"],
        connect=fases["connect"],
        write=fases["write"],
        pool=fases["pool"],
    )


def modelo_aceita_saida_estruturada(modelo: str) -> bool:
    nome = (modelo or "").strip().lower()
    return any(nome.startswith(familia) for familia in _FAMILIAS_SAIDA_ESTRUTURADA)


def _parametro_de_create(nome: str) -> bool:
    try:
        import inspect

        from anthropic.resources.messages import Messages

        return nome in inspect.signature(Messages.create).parameters
    except Exception:
        return False


def preparar_saida_estruturada(modelo: str, esquema: dict) -> dict | None:
    """Schema nativo só quando o SDK e o modelo aceitam. Senão, o prompt cobre o formato."""
    if not isinstance(esquema, dict) or not modelo_aceita_saida_estruturada(modelo):
        return None
    if not _parametro_de_create("output_config") and not _parametro_de_create("output_format"):
        return None
    return {"format": {"type": "json_schema", "schema": esquema}}


def montar_cliente_anthropic(api_key: str, timeout: Any) -> Any:
    """Cliente oficial. retries permanece 0 e não é parâmetro de configuração."""
    import anthropic

    return anthropic.Anthropic(api_key=api_key, max_retries=RETRIES, timeout=timeout)


def classificar_excecao_transporte(exc: BaseException) -> ErroTransporte:
    status = getattr(exc, "status_code", None)
    if isinstance(status, bool) or not isinstance(status, int):
        status = None
    nome = exc.__class__.__name__
    entrada, saida = _usage_de_objeto(exc)
    if nome in {"APITimeoutError", "TimeoutException"} or nome.endswith("TimeoutError"):
        return ErroTransporte("timeout", incerto=True, status=status, input_tokens=entrada, output_tokens=saida)
    if nome in {"APIConnectionError", "ConnectError"}:
        return ErroTransporte("conexao", incerto=True, status=status, input_tokens=entrada, output_tokens=saida)
    if status == 429 or nome == "RateLimitError":
        return ErroTransporte("rate_limit", incerto=False, status=429, input_tokens=entrada, output_tokens=saida)
    if status is not None and status >= 500:
        return ErroTransporte("servidor", incerto=True, status=status, input_tokens=entrada, output_tokens=saida)
    if status is not None and status >= 400:
        return ErroTransporte(
            "pedido_recusado",
            incerto=False,
            status=status,
            input_tokens=entrada,
            output_tokens=saida,
        )
    return ErroTransporte("transporte", incerto=True, status=status, input_tokens=entrada, output_tokens=saida)


def interpretar_mensagem(message: Any) -> dict[str, Any]:
    entrada, saida = _usage_de_objeto(message)
    parada = getattr(message, "stop_reason", None)
    if not isinstance(parada, str):
        parada = None
    blocos = getattr(message, "content", None) or []
    textos: list[str] = []
    recusa = parada == "refusal"
    if isinstance(blocos, list):
        for bloco in blocos:
            tipo = bloco.get("type") if isinstance(bloco, dict) else getattr(bloco, "type", None)
            if tipo == "refusal":
                recusa = True
            bruto = bloco.get("text") if isinstance(bloco, dict) else getattr(bloco, "text", None)
            if isinstance(bruto, str):
                textos.append(bruto)
    return {
        "texto": "".join(textos).strip(),
        "stop_reason": parada,
        "input_tokens": entrada,
        "output_tokens": saida,
        "recusa": recusa,
        "truncada": parada == "max_tokens",
    }


def chamar_anthropic_governado(
    *,
    usuario_id: int,
    conta_id: int,
    franquia_id: int,
    mensagem: str,
    chave_idempotencia: str,
    execucao_id: int | None = None,
    worker_id: str | None = None,
    fencing: int | None = None,
    transporte=None,
    model: str | None = None,
    max_tokens: int | None = None,
    system: str | None = None,
    output_config: dict | None = None,
) -> dict[str, Any]:
    """Uma tentativa física. A mesma chave não dispara outra chamada faturável."""
    if not has_app_context():
        return _resultado("sem_contexto", provedor_chamado=False)
    if not _habilitada():
        return _resultado("desabilitado", provedor_chamado=False)
    ident = _identidade(usuario_id, conta_id, franquia_id)
    if ident is None:
        return _resultado("identidade_incoerente", provedor_chamado=False)
    if not isinstance(mensagem, str) or not mensagem.strip():
        return _resultado("mensagem_ausente", provedor_chamado=False)
    if not _chave_valida(chave_idempotencia):
        return _resultado("chave_invalida", provedor_chamado=False)
    modelo = (model or os.getenv(FLAG_MODELO) or "").strip()
    if not modelo or len(modelo) > 255:
        return _resultado("modelo_ausente", provedor_chamado=False)
    limite = _max_tokens(max_tokens)
    if limite is None:
        return _resultado("limite_invalido", provedor_chamado=False)
    timeout = _timeout()
    if timeout is None:
        return _resultado("limite_invalido", provedor_chamado=False)

    execucao = None
    if execucao_id is not None:
        execucao, erro_execucao = _execucao_do_dono(execucao_id, ident, worker_id, fencing)
        if erro_execucao:
            return _resultado(erro_execucao, provedor_chamado=False)
        if execucao.estado == "incerta" or execucao.efeito_externo == "incerto":
            return _retorno_efeito_incerto_sem_nova_chamada(execucao, ident)

    existente = db.session.query(IaChamadaTentativa).filter_by(attempt_key=chave_idempotencia).one_or_none()
    if existente is not None:
        if not _mesma_identidade(existente, ident):
            return _resultado("isolamento", provedor_chamado=False)
        return _repetir(existente, execucao, worker_id, fencing)

    franquia = db.session.get(Franquia, ident["franquia_id"])
    if franquia is None or int(franquia.conta_id) != ident["conta_id"]:
        return _resultado("identidade_incoerente", provedor_chamado=False)
    ensure_franquia_operacional_inicializada(franquia)
    plano = resolver_plano_operacional_para_franquia(ident["franquia_id"])
    decisao = decidir_admissao_chamada_ia(
        franquia,
        plano,
        creditos_necessarios=Q6,
        origem_sistema=False,
    )
    if not decisao.admitida:
        tentativa, nova = _criar_tentativa(
            chave_idempotencia,
            ident,
            modelo,
            IaChamadaTentativa.STATUS_BLOCKED,
            decisao.motivo,
            debita_cliente=False,
            execucao=execucao,
            worker_id=worker_id,
            fencing=fencing,
        )
        if tentativa is None:
            return _resultado("isolamento", provedor_chamado=False)
        if not nova:
            return _repetir(tentativa, execucao, worker_id, fencing)
        return _resultado(
            "admissao_recusada",
            provedor_chamado=False,
            tentativa_id=tentativa.id,
            execucao_id=None if execucao is None else execucao.id,
            motivo=decisao.motivo,
        )

    try:
        governado = govern_or_raise(
            {"system": system or "", "messages": [{"role": "user", "content": mensagem}]},
            purpose=PURPOSE_BI_LOGISTICO,
            content_type=CONTENT_TYPE_DOCUMENT,
            agent=AGENT,
            provider=PROVIDER,
            alias_session=CleitonAiAliasSession(usuario_id=ident["usuario_id"]),
        )
    except CleitonAiGovernanceBlockedError as exc:
        return _recusar_conteudo(
            chave_idempotencia,
            ident,
            modelo,
            execucao,
            worker_id,
            fencing,
            exc,
        )
    pedido = _pedido_seguro(governado.safe_content, modelo, limite)
    if isinstance(pedido, dict) and isinstance(output_config, dict) and output_config:
        pedido["output_config"] = output_config
    if pedido is None:
        return _recusar_conteudo(
            chave_idempotencia,
            ident,
            modelo,
            execucao,
            worker_id,
            fencing,
            CleitonAiGovernanceBlockedError(reason_codes=["conteudo_nao_autorizado"]),
        )

    prazo = timeout
    if execucao is not None:
        fases, erro_prazo = _fases_se_couber(int(execucao.id), ident, worker_id, fencing, timeout)
        if erro_prazo:
            return _resultado(
                erro_prazo,
                provedor_chamado=False,
                incerto=erro_prazo == "efeito_incerto",
            )
        if transporte is None:
            prazo = materializar_prazo(fases)
    envio = transporte if transporte is not None else _transporte_sdk(prazo)
    if envio is None:
        return _resultado("credencial_ausente", provedor_chamado=False)

    tentativa, nova = _criar_tentativa(
        chave_idempotencia,
        ident,
        modelo,
        IaChamadaTentativa.STATUS_CALLING,
        None,
        debita_cliente=bool(decisao.debita_cliente),
        execucao=execucao,
        worker_id=worker_id,
        fencing=fencing,
    )
    if tentativa is None:
        return _resultado("isolamento", provedor_chamado=False)
    if not nova:
        return _repetir(tentativa, execucao, worker_id, fencing)
    if execucao is not None and not _segurar_efeito(int(execucao.id), ident, worker_id, fencing):
        _fechar_tentativa(tentativa, IaChamadaTentativa.STATUS_UNCERTAIN, None, "efeito_incerto")
        _efeito_incerto(int(execucao.id), worker_id, fencing)
        return _resultado(
            "efeito_incerto",
            provedor_chamado=False,
            incerto=True,
            tentativa_id=tentativa.id,
            execucao_id=execucao.id,
        )

    try:
        bruto = envio(pedido)
    except ErroTransporte as exc:
        return _encerrar_transporte(tentativa, ident, modelo, exc, execucao, worker_id, fencing)
    except Exception as exc:
        classificado = classificar_excecao_transporte(exc)
        return _encerrar_transporte(tentativa, ident, modelo, classificado, execucao, worker_id, fencing)

    if not isinstance(bruto, dict):
        bruto = interpretar_mensagem(bruto)
    return _encerrar_resposta(tentativa, ident, modelo, bruto, execucao, worker_id, fencing)


def _fases_se_couber(execucao_id: int, ident: dict[str, int], worker_id, fencing, limite: float):
    """Relê fencing e lease imediatamente antes da tentativa externa."""
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is not None:
        db.session.expire(linha)
    linha, erro = _execucao_do_dono(int(execucao_id), ident, worker_id, fencing)
    if erro:
        return None, erro
    if linha.estado != "adquirida" or linha.lease_ate is None:
        if linha.estado == "incerta" or linha.efeito_externo == "incerto":
            return None, "efeito_incerto"
        return None, "fencing_recusado"
    restante = (linha.lease_ate - utcnow_naive()).total_seconds()
    fases = fases_para_orcamento(float(limite), restante - MARGEM_CONFIRMACAO_SEGUNDOS)
    if fases is None:
        return None, "orcamento_temporal"
    return fases, None


def _transporte_sdk(timeout: Any):
    chave = (os.getenv(FLAG_CREDENCIAL) or "").strip()
    if not chave:
        return None

    def enviar(pedido: dict[str, Any]) -> dict[str, Any]:
        cliente = montar_cliente_anthropic(chave, timeout)
        argumentos = {
            "model": pedido["model"],
            "max_tokens": pedido["max_tokens"],
            "messages": pedido["messages"],
        }
        if pedido.get("system"):
            argumentos["system"] = pedido["system"]
        config = pedido.get("output_config")
        if isinstance(config, dict):
            if _parametro_de_create("output_config"):
                argumentos["output_config"] = config
            elif _parametro_de_create("output_format"):
                argumentos["output_format"] = config
        try:
            mensagem = cliente.messages.create(**argumentos)
        except Exception as exc:
            raise classificar_excecao_transporte(exc) from exc
        return interpretar_mensagem(mensagem)

    return enviar


def _encerrar_resposta(tentativa, ident, modelo, bruto, execucao, worker_id, fencing) -> dict[str, Any]:
    entrada = _token(bruto.get("input_tokens"))
    saida = _token(bruto.get("output_tokens"))
    texto = bruto.get("texto") if isinstance(bruto.get("texto"), str) else ""
    recusa = bool(bruto.get("recusa"))
    truncada = bool(bruto.get("truncada"))
    if entrada is None and saida is None:
        status = _STATUS_SEM_USO
        codigo = "sem_usage"
    elif recusa:
        status = _STATUS_OK
        codigo = "recusa"
    elif truncada:
        status = _STATUS_OK
        codigo = "truncada"
    else:
        status = _STATUS_OK
        codigo = "ok"
    evento_id = _consolidar_evento_tentativa(
        tentativa.id,
        ident,
        modelo,
        status,
        entrada,
        saida,
        None,
        status_tentativa=IaChamadaTentativa.STATUS_SETTLED,
        motivo=None if codigo == "ok" else codigo,
    )
    if evento_id is None:
        return _marcar_incerta(tentativa, execucao, worker_id, fencing, "persistencia_falhou")
    _aplicar_motor(evento_id)
    if execucao is not None and not _conferir_posse_do_efeito(int(execucao.id), ident, worker_id, fencing):
        _efeito_incerto(int(execucao.id), worker_id, fencing)
        return _resultado(
            "efeito_incerto",
            provedor_chamado=True,
            incerto=True,
            evento_id=evento_id,
            tentativa_id=tentativa.id,
            execucao_id=execucao.id,
            modelo=modelo,
            usage=_usage(entrada, saida),
        )
    return _resultado(
        codigo,
        ok=codigo in {"ok", "truncada", "sem_usage"},
        provedor_chamado=True,
        evento_id=evento_id,
        tentativa_id=tentativa.id,
        execucao_id=None if execucao is None else execucao.id,
        modelo=modelo,
        usage=_usage(entrada, saida),
        texto=texto,
        truncada=truncada,
        recusa=recusa,
    )


def _encerrar_transporte(tentativa, ident, modelo, exc: ErroTransporte, execucao, worker_id, fencing) -> dict[str, Any]:
    evento_id = _consolidar_evento_tentativa(
        tentativa.id,
        ident,
        modelo,
        _STATUS_FALHA,
        exc.input_tokens,
        exc.output_tokens,
        _resumo_transporte(exc),
        status_tentativa=(
            IaChamadaTentativa.STATUS_UNCERTAIN
            if exc.incerto
            else IaChamadaTentativa.STATUS_PROVIDER_FAILED
        ),
        motivo="efeito_incerto" if exc.incerto else exc.codigo,
    )
    if evento_id is not None:
        _aplicar_motor(evento_id)
    if not exc.incerto and execucao is not None and not _liberar_efeito(int(execucao.id), ident, worker_id, fencing):
        return _marcar_incerta(
            tentativa,
            execucao,
            worker_id,
            fencing,
            exc.codigo,
            evento_id=evento_id,
            usage=_usage(exc.input_tokens, exc.output_tokens),
        )
    if exc.incerto or evento_id is None:
        return _marcar_incerta(
            tentativa,
            execucao,
            worker_id,
            fencing,
            "persistencia_falhou" if evento_id is None else exc.codigo,
            evento_id=evento_id,
            usage=_usage(exc.input_tokens, exc.output_tokens),
        )
    return _resultado(
        exc.codigo,
        provedor_chamado=True,
        evento_id=evento_id,
        tentativa_id=tentativa.id,
        execucao_id=None if execucao is None else execucao.id,
        modelo=modelo,
        usage=_usage(exc.input_tokens, exc.output_tokens),
    )


def _marcar_incerta(
    tentativa,
    execucao,
    worker_id,
    fencing,
    codigo: str,
    evento_id: int | None = None,
    usage: dict[str, int | None] | None = None,
) -> dict[str, Any]:
    _fechar_tentativa(tentativa, IaChamadaTentativa.STATUS_UNCERTAIN, evento_id, "efeito_incerto")
    if execucao is not None and worker_id and fencing is not None:
        _efeito_incerto(execucao.id, worker_id, fencing)
    return _resultado(
        codigo,
        provedor_chamado=True,
        incerto=True,
        evento_id=evento_id,
        tentativa_id=tentativa.id,
        execucao_id=None if execucao is None else execucao.id,
        usage=usage,
    )


def _recusar_conteudo(chave, ident, modelo, execucao, worker_id, fencing, exc: CleitonAiGovernanceBlockedError) -> dict[str, Any]:
    tentativa, nova = _criar_tentativa(
        chave,
        ident,
        modelo,
        IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
        "conteudo_nao_autorizado",
        debita_cliente=False,
        execucao=execucao,
        worker_id=worker_id,
        fencing=fencing,
    )
    if tentativa is None:
        return _resultado("isolamento", provedor_chamado=False)
    if not nova:
        return _repetir(tentativa, execucao, worker_id, fencing)
    evento_id = _consolidar_evento_tentativa(
        tentativa.id,
        ident,
        modelo,
        _STATUS_FALHA,
        None,
        None,
        project_safe_error(exc, stage="pre_provider", provider=PROVIDER, retry=False),
        status_tentativa=IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
        motivo="conteudo_nao_autorizado",
        provider=PROVIDER_INTERNO,
        operation="ai_data_governance_block",
        encerrar=False,
    )
    if evento_id is not None:
        _aplicar_motor(evento_id)
    return _resultado(
        "conteudo_nao_autorizado",
        provedor_chamado=False,
        evento_id=evento_id,
        tentativa_id=tentativa.id,
        execucao_id=None if execucao is None else execucao.id,
    )


def _repetir(tentativa: IaChamadaTentativa, execucao, worker_id, fencing) -> dict[str, Any]:
    if tentativa.ia_consumo_evento_id is not None:
        _aplicar_motor(int(tentativa.ia_consumo_evento_id))
    if tentativa.status in {IaChamadaTentativa.STATUS_CALLING, IaChamadaTentativa.STATUS_UNCERTAIN}:
        if execucao is not None and worker_id and fencing is not None:
            _efeito_incerto(execucao.id, worker_id, fencing)
        return _resultado(
            "efeito_incerto",
            provedor_chamado=False,
            incerto=True,
            tentativa_id=tentativa.id,
            evento_id=tentativa.ia_consumo_evento_id,
            execucao_id=None if execucao is None else execucao.id,
            repetida=True,
        )
    evento = None
    if tentativa.ia_consumo_evento_id is not None:
        evento = db.session.get(IaConsumoEvento, int(tentativa.ia_consumo_evento_id))
    codigo = {
        IaChamadaTentativa.STATUS_BLOCKED: "admissao_recusada",
        IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED: "conteudo_nao_autorizado",
        IaChamadaTentativa.STATUS_PROVIDER_FAILED: tentativa.failure_reason or "transporte",
        IaChamadaTentativa.STATUS_SETTLED: tentativa.failure_reason or "ok",
    }.get(tentativa.status, "repetida")
    if codigo not in {"ok", "truncada", "recusa", "sem_usage", "admissao_recusada", "conteudo_nao_autorizado"}:
        if tentativa.status != IaChamadaTentativa.STATUS_PROVIDER_FAILED:
            codigo = "repetida"
    return _resultado(
        codigo,
        ok=tentativa.status == IaChamadaTentativa.STATUS_SETTLED and codigo in {"ok", "truncada", "sem_usage"},
        provedor_chamado=False,
        tentativa_id=tentativa.id,
        evento_id=None if evento is None else evento.id,
        execucao_id=None if execucao is None else execucao.id,
        modelo=tentativa.model,
        usage=None
        if evento is None
        else _usage(evento.input_tokens, evento.output_tokens, evento.total_tokens),
        repetida=True,
    )


def _criar_tentativa(
    chave: str,
    ident: dict[str, int],
    modelo: str,
    status: str,
    motivo: str | None,
    *,
    debita_cliente: bool,
    execucao,
    worker_id: str | None,
    fencing: int | None,
) -> tuple[IaChamadaTentativa | None, bool]:
    row = IaChamadaTentativa(
        attempt_key=chave,
        conta_id=ident["conta_id"],
        franquia_id=ident["franquia_id"],
        usuario_id=ident["usuario_id"],
        origem_sistema=False,
        agent=AGENT,
        flow_type=FLOW_TYPE,
        operation=OPERATION,
        provider=PROVIDER,
        model=modelo,
        reserved_credits=0,
        status=status,
        failure_reason=(motivo or None),
        reserva_ativa=False,
        debita_cliente=debita_cliente,
        created_at=utcnow_naive(),
    )
    try:
        db.session.add(row)
        db.session.flush()
        if execucao is not None:
            if not _vincular_execucao(execucao.id, row.id, ident, worker_id, fencing):
                db.session.rollback()
                return None, False
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        existente = db.session.query(IaChamadaTentativa).filter_by(attempt_key=chave).one_or_none()
        if existente is not None and _mesma_identidade(existente, ident):
            return existente, False
        return None, False
    return db.session.get(IaChamadaTentativa, row.id), True


def _vincular_execucao(execucao_id: int, tentativa_id: int, ident: dict[str, int], worker_id, fencing) -> bool:
    if not worker_id or fencing is None:
        return False
    sentenca = (
        update(AgenteBiExecucao)
        .where(
            AgenteBiExecucao.id == int(execucao_id),
            AgenteBiExecucao.usuario_id == ident["usuario_id"],
            AgenteBiExecucao.conta_id == ident["conta_id"],
            AgenteBiExecucao.franquia_id == ident["franquia_id"],
            AgenteBiExecucao.estado == "adquirida",
            AgenteBiExecucao.lease_worker == worker_id,
            AgenteBiExecucao.fencing == int(fencing),
            AgenteBiExecucao.ia_chamada_tentativa_id.is_(None),
        )
        .values(ia_chamada_tentativa_id=int(tentativa_id), updated_at=utcnow_naive())
        .returning(AgenteBiExecucao.id)
        .execution_options(synchronize_session=False)
    )
    return db.session.execute(sentenca).first() is not None


def _fechar_tentativa(tentativa: IaChamadaTentativa, status: str, evento_id: int | None, motivo: str | None) -> None:
    atual = db.session.get(IaChamadaTentativa, tentativa.id)
    if atual is None or atual.status != IaChamadaTentativa.STATUS_CALLING:
        return
    atual.status = status
    atual.failure_reason = motivo
    atual.reserva_ativa = False
    atual.reserved_credits = 0
    if evento_id is not None:
        atual.ia_consumo_evento_id = int(evento_id)
    if status in {
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
        IaChamadaTentativa.STATUS_UNCERTAIN,
    }:
        atual.settled_at = utcnow_naive()
    db.session.commit()


def _consolidar_evento_tentativa(
    tentativa_id: int,
    ident: dict[str, int],
    modelo: str,
    status_evento: str,
    entrada: int | None,
    saida: int | None,
    erro: str | None,
    *,
    status_tentativa: str,
    motivo: str | None,
    provider: str = PROVIDER,
    operation: str = OPERATION,
    encerrar: bool = True,
) -> int | None:
    """Grava o evento e o estado da tentativa no mesmo commit."""
    try:
        atual = (
            db.session.query(IaChamadaTentativa)
            .filter(
                IaChamadaTentativa.id == int(tentativa_id),
                IaChamadaTentativa.usuario_id == ident["usuario_id"],
                IaChamadaTentativa.conta_id == ident["conta_id"],
                IaChamadaTentativa.franquia_id == ident["franquia_id"],
            )
            .with_for_update()
            .execution_options(populate_existing=True)
            .one_or_none()
        )
        if atual is None:
            db.session.rollback()
            return None
        if atual.ia_consumo_evento_id is not None:
            evento_existente = int(atual.ia_consumo_evento_id)
            if encerrar and atual.status == IaChamadaTentativa.STATUS_CALLING:
                _aplicar_estado_tentativa(atual, status_tentativa, evento_existente, motivo, True)
                db.session.commit()
            else:
                db.session.rollback()
            return evento_existente
        if encerrar and atual.status != IaChamadaTentativa.STATUS_CALLING:
            db.session.rollback()
            return None
        evento_id = _persistir_evento(
            ident,
            modelo,
            status_evento,
            entrada,
            saida,
            erro,
            provider=provider,
            operation=operation,
        )
        if evento_id is None:
            db.session.rollback()
            return None
        _aplicar_estado_tentativa(atual, status_tentativa, evento_id, motivo, encerrar)
        db.session.commit()
        return int(evento_id)
    except Exception as exc:
        logger.warning(
            "evento=agente_bi_anthropic_persistencia_falhou usuario_id=%s erro=%s",
            ident.get("usuario_id"),
            exc.__class__.__name__,
        )
        try:
            db.session.rollback()
        except Exception:
            pass
        return _evento_ja_vinculado(tentativa_id, ident)


def _evento_ja_vinculado(tentativa_id: int, ident: dict[str, int]) -> int | None:
    try:
        db.session.expire_all()
        atual = db.session.get(IaChamadaTentativa, int(tentativa_id))
    except Exception:
        return None
    if atual is None or not _mesma_identidade(atual, ident) or atual.ia_consumo_evento_id is None:
        return None
    return int(atual.ia_consumo_evento_id)


def _aplicar_estado_tentativa(atual: IaChamadaTentativa, status: str, evento_id: int, motivo: str | None, encerrar: bool) -> None:
    atual.ia_consumo_evento_id = int(evento_id)
    atual.reserva_ativa = False
    atual.reserved_credits = 0
    if not encerrar:
        return
    atual.status = status
    atual.failure_reason = motivo
    if status in {
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
        IaChamadaTentativa.STATUS_UNCERTAIN,
    }:
        atual.settled_at = utcnow_naive()


def _persistir_evento(
    ident: dict[str, int],
    modelo: str,
    status: str,
    entrada: int | None,
    saida: int | None,
    erro: str | None,
    *,
    provider: str = PROVIDER,
    operation: str = OPERATION,
) -> int | None:
    """Insere o evento na transação corrente. Quem chama faz o commit."""
    total = None
    if entrada is not None or saida is not None:
        total = int(entrada or 0) + int(saida or 0)
    row = IaConsumoEvento(
        occurred_at=utcnow_naive(),
        provider=provider,
        operation=operation,
        model=(modelo or "")[:255],
        agent=AGENT,
        flow_type=FLOW_TYPE,
        api_key_label="anthropic",
        status=status,
        input_tokens=entrada,
        output_tokens=saida,
        total_tokens=total,
        error_summary=(erro or None),
        conta_id=ident["conta_id"],
        franquia_id=ident["franquia_id"],
        usuario_id=ident["usuario_id"],
        tipo_origem=TIPO_ORIGEM,
        origem_sistema=False,
        regime_abatimento=IaConsumoEvento.REGIME_NOVO,
    )
    db.session.add(row)
    db.session.flush()
    return int(row.id)


def _aplicar_motor(evento_id: int) -> None:
    try:
        aplicar_motor_apos_ia_consumo_evento(int(evento_id))
    except Exception as exc:
        logger.warning(
            "evento=agente_bi_anthropic_motor_falhou evento_id=%s erro=%s",
            evento_id,
            exc.__class__.__name__,
        )
        try:
            db.session.rollback()
        except Exception:
            pass


def _retorno_efeito_incerto_sem_nova_chamada(execucao, ident: dict[str, int]) -> dict[str, Any]:
    """A mesma chave não chama de novo. Se o evento já está vinculado, o motor oficial reentra."""
    tentativa_id = execucao.ia_chamada_tentativa_id
    evento_id = None
    if tentativa_id is not None:
        tentativa = db.session.get(IaChamadaTentativa, int(tentativa_id))
        if (
            tentativa is not None
            and _mesma_identidade(tentativa, ident)
            and tentativa.ia_consumo_evento_id is not None
        ):
            evento_id = int(tentativa.ia_consumo_evento_id)
            _aplicar_motor(evento_id)
    return _resultado(
        "efeito_incerto",
        provedor_chamado=False,
        incerto=True,
        evento_id=evento_id,
        tentativa_id=None if tentativa_id is None else int(tentativa_id),
        execucao_id=execucao.id,
    )


def _segurar_efeito(execucao_id: int, ident: dict[str, int], worker_id, fencing) -> bool:
    """Impede a recuperação de recolocar a linha enquanto a chamada está fora.

    O estado continua adquirida. O efeito permanece incerto até a confirmação
    gravar o documento na mesma atualização. Soltar para nenhum antes disso
    reabre a fila comum.
    """
    if not worker_id or fencing is None:
        return False
    sentenca = (
        update(AgenteBiExecucao)
        .where(
            AgenteBiExecucao.id == int(execucao_id),
            AgenteBiExecucao.usuario_id == ident["usuario_id"],
            AgenteBiExecucao.conta_id == ident["conta_id"],
            AgenteBiExecucao.franquia_id == ident["franquia_id"],
            AgenteBiExecucao.estado == "adquirida",
            AgenteBiExecucao.lease_worker == worker_id,
            AgenteBiExecucao.fencing == int(fencing),
            AgenteBiExecucao.lease_ate > utcnow_naive(),
            AgenteBiExecucao.efeito_externo == "nenhum",
        )
        .values(efeito_externo="incerto", updated_at=utcnow_naive())
        .returning(AgenteBiExecucao.id)
        .execution_options(synchronize_session=False)
    )
    segurou = db.session.execute(sentenca).first() is not None
    if segurou:
        db.session.commit()
    else:
        db.session.rollback()
    return segurou


def _tentativa_da_execucao(execucao_id: int) -> IaChamadaTentativa | None:
    if isinstance(execucao_id, bool) or not isinstance(execucao_id, int):
        return None
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None or linha.ia_chamada_tentativa_id is None:
        return None
    return db.session.get(IaChamadaTentativa, int(linha.ia_chamada_tentativa_id))


def chamada_liquidada(execucao_id: int) -> bool:
    """A resposta já foi contabilizada e pode entrar no documento."""
    tentativa = _tentativa_da_execucao(execucao_id)
    return tentativa is not None and tentativa.status == IaChamadaTentativa.STATUS_SETTLED


def chamada_externa_contabilizada(execucao_id: int) -> bool:
    """True quando a tentativa já saiu ou o resultado não pode ser reenviado.

    A fila usa isto sem ler crédito, evento ou abatimento.
    """
    tentativa = _tentativa_da_execucao(execucao_id)
    if tentativa is None:
        return False
    return tentativa.status in {
        IaChamadaTentativa.STATUS_CALLING,
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_UNCERTAIN,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
    }


def _conferir_posse_do_efeito(execucao_id: int, ident: dict[str, int], worker_id, fencing) -> bool:
    """Lease e fencing ainda válidos, com o efeito seguindo incerto.

    Não grava. A resposta liquidada só segue para o documento se o dono
    continua de posse. A confirmação consome o efeito incerto.
    """
    if not worker_id or fencing is None:
        return False
    linha = db.session.get(AgenteBiExecucao, int(execucao_id))
    if linha is None:
        return False
    db.session.expire(linha)
    db.session.refresh(linha)
    if (
        int(linha.usuario_id) != ident["usuario_id"]
        or int(linha.conta_id) != ident["conta_id"]
        or int(linha.franquia_id) != ident["franquia_id"]
    ):
        return False
    return (
        linha.estado == "adquirida"
        and linha.lease_worker == worker_id
        and int(linha.fencing) == int(fencing)
        and linha.lease_ate is not None
        and linha.lease_ate > utcnow_naive()
        and linha.efeito_externo == "incerto"
    )


def _liberar_efeito(execucao_id: int, ident: dict[str, int], worker_id, fencing) -> bool:
    """Devolve o efeito a nenhum só com lease e fencing ainda válidos.

    Cabe à falha já classificada, quando o documento não será gravado.
    A resposta que ainda confirma analisar.v1 não passa por aqui: soltar
    o efeito antes desse commit reabre a fila.
    """
    if not worker_id or fencing is None:
        return False
    sentenca = (
        update(AgenteBiExecucao)
        .where(
            AgenteBiExecucao.id == int(execucao_id),
            AgenteBiExecucao.usuario_id == ident["usuario_id"],
            AgenteBiExecucao.conta_id == ident["conta_id"],
            AgenteBiExecucao.franquia_id == ident["franquia_id"],
            AgenteBiExecucao.estado == "adquirida",
            AgenteBiExecucao.lease_worker == worker_id,
            AgenteBiExecucao.fencing == int(fencing),
            AgenteBiExecucao.lease_ate > utcnow_naive(),
            AgenteBiExecucao.efeito_externo == "incerto",
        )
        .values(efeito_externo="nenhum", updated_at=utcnow_naive())
        .returning(AgenteBiExecucao.id)
        .execution_options(synchronize_session=False)
    )
    liberou = db.session.execute(sentenca).first() is not None
    if liberou:
        db.session.commit()
    else:
        db.session.rollback()
    return liberou


def _efeito_incerto(execucao_id: int, worker_id: str, fencing: int) -> None:
    from app.services.agente_bi_execucao_service import ExecucaoErro, marcar_efeito_incerto

    try:
        marcar_efeito_incerto(
            int(execucao_id),
            worker_id=worker_id,
            fencing=int(fencing),
            lease_vivo=False,
        )
    except ExecucaoErro:
        return


def _execucao_do_dono(execucao_id, ident, worker_id, fencing) -> tuple[AgenteBiExecucao | None, str | None]:
    if not isinstance(execucao_id, int) or isinstance(execucao_id, bool):
        return None, "isolamento"
    if not isinstance(worker_id, str) or not worker_id.strip():
        return None, "fencing_recusado"
    if not isinstance(fencing, int) or isinstance(fencing, bool):
        return None, "fencing_recusado"
    linha = db.session.get(AgenteBiExecucao, execucao_id)
    if linha is None:
        return None, "isolamento"
    if (
        int(linha.usuario_id) != ident["usuario_id"]
        or int(linha.conta_id) != ident["conta_id"]
        or int(linha.franquia_id) != ident["franquia_id"]
    ):
        return None, "isolamento"
    if linha.estado == "incerta" or linha.efeito_externo == "incerto":
        return linha, None
    if linha.estado != "adquirida" or linha.lease_worker != worker_id or int(linha.fencing) != fencing:
        return None, "fencing_recusado"
    if linha.lease_ate is None or linha.lease_ate <= utcnow_naive():
        return None, "fencing_recusado"
    return linha, None


def _identidade(usuario_id, conta_id, franquia_id) -> dict[str, int] | None:
    uid = _id(usuario_id)
    cid = _id(conta_id)
    fid = _id(franquia_id)
    if uid is None or cid is None or fid is None:
        return None
    usuario = db.session.get(User, uid)
    if usuario is None or usuario.conta_id is None or usuario.franquia_id is None:
        return None
    if int(usuario.conta_id) != cid or int(usuario.franquia_id) != fid:
        return None
    return {"usuario_id": uid, "conta_id": cid, "franquia_id": fid}


def _mesma_identidade(tentativa: IaChamadaTentativa, ident: dict[str, int]) -> bool:
    return (
        tentativa.usuario_id is not None
        and tentativa.conta_id is not None
        and tentativa.franquia_id is not None
        and int(tentativa.usuario_id) == ident["usuario_id"]
        and int(tentativa.conta_id) == ident["conta_id"]
        and int(tentativa.franquia_id) == ident["franquia_id"]
    )


def _pedido_seguro(seguro: Any, modelo: str, limite: int) -> dict[str, Any] | None:
    if not isinstance(seguro, dict):
        return None
    mensagens = seguro.get("messages")
    if not isinstance(mensagens, list) or not mensagens:
        return None
    limpas = []
    for item in mensagens:
        if not isinstance(item, dict):
            return None
        papel = item.get("role")
        conteudo = item.get("content")
        if papel not in _PAPEIS or not isinstance(conteudo, str) or not conteudo.strip():
            return None
        limpas.append({"role": papel, "content": conteudo})
    sistema = seguro.get("system")
    if sistema is None:
        sistema = ""
    if not isinstance(sistema, str):
        return None
    return {
        "model": modelo,
        "max_tokens": limite,
        "system": sistema,
        "messages": limpas,
    }


def _usage_de_objeto(origem: Any) -> tuple[int | None, int | None]:
    usage = getattr(origem, "usage", None)
    if usage is None and isinstance(origem, dict):
        usage = origem.get("usage")
    if usage is None:
        resposta = getattr(origem, "response", None)
        if resposta is not None:
            return _usage_de_objeto(resposta)
        return None, None
    if isinstance(usage, dict):
        return _token(usage.get("input_tokens")), _token(usage.get("output_tokens"))
    return _token(getattr(usage, "input_tokens", None)), _token(getattr(usage, "output_tokens", None))


def _token(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        return None
    return valor


def _usage(entrada: int | None, saida: int | None, total: int | None = None) -> dict[str, int | None]:
    if total is None and (entrada is not None or saida is not None):
        total = int(entrada or 0) + int(saida or 0)
    return {"input_tokens": entrada, "output_tokens": saida, "total_tokens": total}


def _resumo_transporte(exc: ErroTransporte) -> str:
    status = exc.status if exc.status is not None else "n/a"
    return f"type={exc.__class__.__name__}|stage=messages|provider=anthropic|retry=no|code={exc.codigo}|status={status}"


def _habilitada() -> bool:
    valor = (os.getenv(FLAG_HABILITADA) or "").strip().lower()
    return valor in {"1", "true", "yes", "on"}


def _id(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 1:
        return None
    return valor


def _chave_valida(chave: Any) -> bool:
    if not isinstance(chave, str) or not 8 <= len(chave) <= 80:
        return False
    if chave[0] not in _CHAVE_PERMITIDA or chave[0] in ".:-_":
        return False
    return all(caractere in _CHAVE_PERMITIDA for caractere in chave)


def _max_tokens(pedido: int | None) -> int | None:
    teto = TETO_MAX_TOKENS
    bruto = (os.getenv(FLAG_MAX_TOKENS) or "").strip()
    if bruto:
        if not bruto.isdigit():
            return None
        valor = int(bruto)
        if valor < 1 or valor > TETO_MAX_TOKENS:
            return None
        teto = valor
    if pedido is None:
        return teto
    if isinstance(pedido, bool) or not isinstance(pedido, int) or pedido < 1 or pedido > teto:
        return None
    return pedido


def _timeout() -> float | None:
    bruto = (os.getenv(FLAG_TIMEOUT) or "").strip()
    if not bruto:
        return TIMEOUT_PADRAO_SEGUNDOS
    try:
        valor = float(bruto)
    except ValueError:
        return None
    if valor <= 0 or valor > TIMEOUT_TETO_SEGUNDOS:
        return None
    return valor


def _resultado(
    codigo: str,
    *,
    ok: bool = False,
    provedor_chamado: bool,
    incerto: bool = False,
    evento_id: int | None = None,
    tentativa_id: int | None = None,
    execucao_id: int | None = None,
    modelo: str = "",
    usage: dict[str, int | None] | None = None,
    texto: str | None = None,
    truncada: bool = False,
    recusa: bool = False,
    repetida: bool = False,
    motivo: str | None = None,
) -> dict[str, Any]:
    logger.info(
        "evento=agente_bi_anthropic codigo=%s evento_id=%s execucao_id=%s tentativa_id=%s incerto=%s",
        codigo,
        evento_id,
        execucao_id,
        tentativa_id,
        incerto,
    )
    return {
        "ok": ok,
        "codigo": codigo,
        "provedor": PROVIDER,
        "modelo": modelo,
        "evento_id": evento_id,
        "tentativa_id": tentativa_id,
        "execucao_id": execucao_id,
        "usage": usage or {"input_tokens": None, "output_tokens": None, "total_tokens": None},
        "texto": texto,
        "provedor_chamado": provedor_chamado,
        "truncada": truncada,
        "recusa": recusa,
        "incerto": incerto,
        "repetida": repetida,
        "motivo": motivo,
        "retries": RETRIES,
    }
