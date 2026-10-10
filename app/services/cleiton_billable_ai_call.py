"""
Fachada obrigatória de chamada externa de IA (SCRUM-146).

Payload final → validação de transporte (allowlist) → config canônica →
governança → count_tokens oficial → teto → identidade → admissão →
reserva atômica → generate_content da mesma config → IaConsumoEvento →
liquidação idempotente.

Transporte billable é allowlist fechada. extra_body não vazio, client_args
ou async_client_args não vazios, cliente HTTP próprio e qualquer campo de
http_options fora da allowlist são rejeitados antes da contagem e da reserva.
A config canônica não recoloca essas extensões.

count_tokens é metering externo, não geração: não cria IaConsumoEvento,
não debita token de geração e não vira ProcessingEvent faturável.
A estimativa local não autoriza admissão financeira.

Não substitui a governança de dados e não cria régua comercial própria.
Não guarda prompt nem conteúdo.

Files API (upload, get, polling, delete) fica fora deste lote: não gera crédito
e a remoção continua permitida com franquia bloqueada.
"""
from __future__ import annotations

import copy
import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from uuid import uuid4

from flask import has_app_context
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.consumo_identidade import (
    TIPO_ORIGEM_HTTP_USUARIO,
    clear_consumo_identidade,
    get_consumo_identidade,
    identidade_sistema_interna,
    set_consumo_identidade,
)
from app.extensions import db
from app.models import Franquia, IaChamadaTentativa, utcnow_naive
from app.run_cleiton_gemini_governance import (
    PERSIST_OK,
    STATUS_FAILURE,
    STATUS_SUCCESS,
    STATUS_SUCCESS_NO_METRICS,
    cleiton_governed_generate_content_observed,
)
from app.services.cleiton_ai_data_governance import (
    CONTENT_TYPE_GENERATE_CONTENTS,
    CleitonAiGovernanceBlockedError,
    govern_or_raise,
    govern_provider_config,
    http_options_extra_body_nao_vazio,
    purpose_from_flow_type,
)
from app.services.cleiton_ciclo_franquia_service import garantir_ciclo_operacional_franquia
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_franquia_operacional_service import (
    _quantize_credit,
    _to_decimal,
    aplicar_motor_apos_ia_consumo_evento,
    converter_tokens_para_creditos,
    decidir_admissao_chamada_ia,
    ensure_franquia_operacional_inicializada,
)
from app.services.cleiton_mensageria_operacao_service import montar_mensagem_operacao
from app.services.cleiton_plano_resolver import resolver_plano_operacional_para_franquia
from app.services.conta_franquia_service import get_sistema_interno_ids

logger = logging.getLogger(__name__)

BILLABLE_AI_MAX_OUTPUT_TOKENS = 8192
BILLABLE_AI_MAX_THINKING_TOKENS = 8192
BILLABLE_AI_STRUCTURAL_TOKENS = 32
SDK_RETRY_ATTEMPTS = 1

MOTIVO_IDENTIDADE_AUSENTE = "identidade_cliente_ausente"
MOTIVO_IDENTIDADE_INCOERENTE = "identidade_incoerente"
MOTIVO_SDK_RETRY = "sdk_retry_nao_contido"
MOTIVO_AFC = "tool_calling_nao_contido"
MOTIVO_REGUA = "regua_indisponivel"
MOTIVO_TETO_CREDITOS_NAO_VERIFICAVEL = "teto_creditos_nao_verificavel"
MOTIVO_TETO = "teto_saida_nao_garantido"
MOTIVO_TETO_ENTRADA = "teto_entrada_nao_verificavel"
MOTIVO_PERSISTENCIA_RESERVA = "persistencia_reserva_falhou"
MOTIVO_SUCCESS_NO_METRICS = "success_no_metrics"
MOTIVO_PROVIDER_SEM_USAGE = "provider_sem_usage"
MOTIVO_PERSISTENCIA_EVENTO = "persistencia_evento_falhou"
MOTIVO_VINCULO_EVENTO = "vinculo_evento_falhou"
MOTIVO_LIQUIDACAO = "liquidacao_interrompida"
MOTIVO_USO_EXCEDE = "uso_excede_reserva"
MOTIVO_TETO_REQUEST_MUTAVEL = "teto_request_mutavel"
MOTIVO_TETO_RESERVA_JOB = "teto_excede_reserva_job"

# google-genai 1.63.0 HttpOptions. Aceitos na fachada billable, e só estes:
# timeout int; base_url e api_version str; headers dict[str, str] puro;
# retry_options com attempts efetivo 1 (checagem própria); extra_body None ou {};
# client_args e async_client_args ausentes, None ou {}.
# Cliente HTTP próprio (httpx_client, httpx_async_client, aiohttp_client) com
# valor, e qualquer outro campo com valor, bloqueiam o transporte inteiro.
# proxy, verify, auth, event_hooks, transport e mounts só cabem em client_args:
# container não vazio não é aceito. O SDK injeta o SSL padrão quando o
# container vem vazio; isso não é opção do chamador.
_CHAVES_HTTP_PERMITIDAS = frozenset(
    {
        "timeout",
        "baseurl",
        "apiversion",
        "headers",
        "retryoptions",
        "extrabody",
        "clientargs",
        "asyncclientargs",
        "httpxclient",
        "httpxasyncclient",
        "aiohttpclient",
    }
)
_CHAVES_ARGS_HTTP = frozenset({"clientargs", "asyncclientargs"})
_CHAVES_CLIENTE_HTTP = frozenset({"httpxclient", "httpxasyncclient", "aiohttpclient"})
_CHAVES_CORPO_HTTP = frozenset({"extrabody"})
_CHAVES_DESTINO_HTTP = frozenset({"baseurl", "apiversion"})

_CHAVES_MIDIA = frozenset(
    {
        "inline_data",
        "file_data",
        "inlinedata",
        "filedata",
        "blob",
        "video_metadata",
    }
)
_CHAVES_IGNORADAS_NO_TETO = frozenset(
    {
        "http_options",
        "httpoptions",
        "headers",
        "retry_options",
        "retryoptions",
    }
)


class BillableAiCallError(Exception):
    terminal = False

    def __init__(
        self,
        message: str,
        *,
        motivo: str,
        attempt_key: str | None = None,
        mensagem_usuario: str | None = None,
    ) -> None:
        super().__init__(message)
        self.motivo = motivo
        self.attempt_key = attempt_key
        self.mensagem_usuario = mensagem_usuario


class BillableAiAdmissionBlocked(BillableAiCallError):
    """Bloqueio comercial. Terminal: não há fallback de modelo."""

    terminal = True


BillableAiCommercialBlocked = BillableAiAdmissionBlocked


class BillableAiGovernanceBlocked(BillableAiCallError):
    """Bloqueio de governança antes do provider. Terminal."""

    terminal = True


class BillableAiUncertainError(BillableAiCallError):
    """Consumo não confirmado. A reserva permanece até reconciliação."""

    terminal = True

    def __init__(
        self,
        message: str,
        *,
        motivo: str,
        attempt_key: str | None = None,
        mensagem_usuario: str | None = None,
        provider_called: bool = False,
        response: Any = None,
    ) -> None:
        super().__init__(
            message,
            motivo=motivo,
            attempt_key=attempt_key,
            mensagem_usuario=mensagem_usuario,
        )
        self.provider_called = provider_called
        self.response = response


@dataclass
class BillableAiCallResult:
    response: Any
    evento_id: int | None
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    persist_status: str
    attempt_id: int | None
    attempt_key: str
    status: str
    complete: bool
    reserved_credits: Decimal
    actual_credits: Decimal | None


def gemini_http_options(*, timeout_ms: int):
    """Cliente Gemini com no máximo uma tentativa HTTP. Retry fica fora da reserva."""
    from google.genai import types as genai_types

    return genai_types.HttpOptions(
        timeout=int(timeout_ms),
        retry_options=genai_types.HttpRetryOptions(attempts=SDK_RETRY_ATTEMPTS),
    )


def _tentativas_padrao_quando_retry_nao_fixa_attempts() -> int:
    """google-genai: `options.attempts or _RETRY_ATTEMPTS` quando a opção existe sem attempts."""
    try:
        from google.genai import _api_client as api_client

        return int(api_client._RETRY_ATTEMPTS)
    except Exception:
        return 5


def tentativas_efetivas_retry(retry: Any) -> int:
    """
    Tentativas HTTP que o SDK instalado realmente executa.

    retry None no cliente: stop_after_attempt(1).
    HttpRetryOptions com attempts ausente, 0 ou inválido: cai em _RETRY_ATTEMPTS (5).
    """
    if retry is None:
        return 1
    if isinstance(retry, dict):
        attempts = retry.get("attempts")
    else:
        attempts = getattr(retry, "attempts", None)
    if attempts is None:
        return _tentativas_padrao_quando_retry_nao_fixa_attempts()
    try:
        numero = int(attempts)
    except (TypeError, ValueError):
        return _tentativas_padrao_quando_retry_nao_fixa_attempts()
    if numero <= 0:
        return _tentativas_padrao_quando_retry_nao_fixa_attempts()
    return numero


def _retry_options_do_cliente(client: Any) -> tuple[Any, bool]:
    """Lê o retry no caminho real do google-genai: client._api_client._http_options."""
    api = getattr(client, "_api_client", None)
    if api is not None:
        options = getattr(api, "_http_options", None)
        if options is not None:
            return getattr(options, "retry_options", None), True
    for attr in ("http_options", "_http_options"):
        options = getattr(client, attr, None)
        if options is None:
            continue
        if isinstance(options, dict):
            return options.get("retry_options") or options.get("retryOptions"), True
        return getattr(options, "retry_options", None), True
    return None, False


def tentativas_retry_sdk(client: Any) -> int | None:
    """None quando o cliente não expõe retry inspecionável. Não lê atributo decorativo se o SDK real existe."""
    retry, encontrado = _retry_options_do_cliente(client)
    if not encontrado:
        return None
    return tentativas_efetivas_retry(retry)


def _retry_da_config(config: Any) -> Any | None:
    """Retry por request. None quando a config não substitui o retry do cliente."""
    if config is None:
        return None
    http_opts = _valor_config(config, "http_options", "httpOptions")
    if http_opts is None:
        return None
    if isinstance(http_opts, dict):
        if "retry_options" not in http_opts and "retryOptions" not in http_opts:
            return None
        return http_opts.get("retry_options", http_opts.get("retryOptions"))
    campos = getattr(http_opts, "model_fields_set", None)
    retry = getattr(http_opts, "retry_options", None)
    if campos is not None and "retry_options" not in campos:
        return None
    if retry is None:
        return None
    return retry


def tentativas_retry_config(config: Any) -> int | None:
    retry = _retry_da_config(config)
    if retry is None and _valor_config(config, "http_options", "httpOptions") is None:
        return None
    if retry is None:
        return None
    return tentativas_efetivas_retry(retry)


def usuario_operacional_da_chamada(usuario: Any = None) -> Any:
    if usuario is not None:
        return usuario
    try:
        from flask import has_request_context
        from flask_login import current_user

        if has_request_context() and getattr(current_user, "is_authenticated", False):
            return current_user._get_current_object()
    except Exception:
        return None
    return None


def _valor_config(config: Any, *nomes: str) -> Any:
    if config is None:
        return None
    if isinstance(config, dict):
        for nome in nomes:
            if nome in config:
                return config[nome]
        return None
    for nome in nomes:
        if hasattr(config, nome):
            valor = getattr(config, nome)
            if valor is not None:
                return valor
    return None


class _ContaTexto:
    def __init__(self) -> None:
        self.bytes = 0
        self.nos = 0
        self.erro: str | None = None


def _acumular_texto(node: Any, acc: _ContaTexto, vistos: set[int]) -> None:
    """Estimativa local auxiliar. Percorre valores, chaves e schemas. Não autoriza admissão."""
    if acc.erro is not None or node is None or isinstance(node, (int, float, bool, Decimal)):
        return
    if isinstance(node, str):
        acc.bytes += len(node.encode("utf-8"))
        acc.nos += 1
        return
    if isinstance(node, (bytes, bytearray, memoryview)):
        acc.erro = MOTIVO_TETO_ENTRADA
        return
    if isinstance(node, dict):
        for chave, valor in node.items():
            nome = chave.lower() if isinstance(chave, str) else ""
            if nome in _CHAVES_IGNORADAS_NO_TETO:
                continue
            if nome in _CHAVES_MIDIA and valor is not None:
                acc.erro = MOTIVO_TETO_ENTRADA
                return
            if isinstance(chave, str):
                acc.bytes += len(chave.encode("utf-8"))
                acc.nos += 1
            _acumular_texto(valor, acc, vistos)
            if acc.erro is not None:
                return
        return
    if isinstance(node, (list, tuple)):
        for item in node:
            _acumular_texto(item, acc, vistos)
            if acc.erro is not None:
                return
        return
    ident = id(node)
    if ident in vistos:
        return
    vistos.add(ident)
    for attr in ("inline_data", "file_data"):
        if getattr(node, attr, None) is not None:
            acc.erro = MOTIVO_TETO_ENTRADA
            return
    if hasattr(node, "model_dump"):
        try:
            dumped = node.model_dump(exclude_none=True)
        except Exception:
            acc.erro = MOTIVO_TETO_ENTRADA
            return
        _acumular_texto(dumped, acc, vistos)
        return
    acc.erro = MOTIVO_TETO_ENTRADA


def estimar_tokens_entrada(contents: Any, config: Any = None) -> tuple[int, str | None]:
    """Estimativa local para observabilidade. Não é teto financeiro nem prova de token Gemini."""
    acc = _ContaTexto()
    vistos: set[int] = set()
    _acumular_texto(contents, acc, vistos)
    _acumular_texto(config, acc, vistos)
    if acc.erro:
        return 0, acc.erro
    tokens = acc.bytes + acc.nos * BILLABLE_AI_STRUCTURAL_TOKENS
    return max(int(tokens), 1), None


def _midia_nao_contavel(node: Any, vistos: set[int]) -> bool:
    """Bytes e mídia não têm contagem verificável nesta fachada."""
    if node is None or isinstance(node, (int, float, bool, Decimal, str)):
        return False
    if isinstance(node, (bytes, bytearray, memoryview)):
        return True
    if isinstance(node, dict):
        for chave, valor in node.items():
            nome = chave.lower() if isinstance(chave, str) else ""
            if nome in _CHAVES_IGNORADAS_NO_TETO:
                continue
            if nome in _CHAVES_MIDIA and valor is not None:
                return True
            if _midia_nao_contavel(valor, vistos):
                return True
        return False
    if isinstance(node, (list, tuple)):
        return any(_midia_nao_contavel(item, vistos) for item in node)
    ident = id(node)
    if ident in vistos:
        return False
    vistos.add(ident)
    for attr in ("inline_data", "file_data"):
        if getattr(node, attr, None) is not None:
            return True
    if hasattr(node, "model_dump"):
        try:
            dumped = node.model_dump(exclude_none=True)
        except Exception:
            return False
        return _midia_nao_contavel(dumped, vistos)
    return False


def _http_options_retry_unico() -> Any:
    try:
        from google.genai import types as genai_types

        return genai_types.HttpOptions(
            retry_options=genai_types.HttpRetryOptions(attempts=SDK_RETRY_ATTEMPTS),
            extra_body={},
        )
    except Exception:
        return {"retry_options": {"attempts": SDK_RETRY_ATTEMPTS}, "extra_body": {}}


def _partes_prompt(config: Any) -> dict[str, Any]:
    return {
        "system_instruction": _valor_config(config, "system_instruction", "systemInstruction"),
        "tools": _valor_config(config, "tools"),
        "cached_content": _valor_config(config, "cached_content", "cachedContent"),
        "response_schema": _valor_config(config, "response_schema", "responseSchema"),
        "response_json_schema": _valor_config(config, "response_json_schema", "responseJsonSchema"),
    }


def _pedido_count_tokens(config: Any) -> dict[str, Any]:
    """Projeção de metering da config canônica: o que entra no prompt e retry contido."""
    partes = _partes_prompt(config)
    pedido: dict[str, Any] = {"http_options": _http_options_retry_unico()}
    instrucao = partes["system_instruction"]
    if instrucao is not None:
        pedido["system_instruction"] = instrucao
    tools = partes["tools"]
    if tools:
        pedido["tools"] = tools
    cached = partes["cached_content"]
    if cached is not None:
        pedido["cached_content"] = cached
    # countTokens da Gemini API rejeita generation_config. O schema estruturado
    # permanece na config de generateContent; incluí-lo aqui impede a contagem.
    return pedido


def _metrica(resposta: Any, *nomes: str) -> Any:
    if resposta is None:
        return None
    if isinstance(resposta, dict):
        for nome in nomes:
            if nome in resposta:
                return resposta[nome]
        return None
    for nome in nomes:
        if hasattr(resposta, nome):
            valor = getattr(resposta, nome)
            if valor is not None:
                return valor
    return None


def _inteiro_nao_negativo(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
        return None
    return valor


def contar_tokens_oficiais(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: Any = None,
    attempt_key: str = "",
    agent: str = "",
    flow_type: str = "",
) -> tuple[int, str | None]:
    """
    count_tokens do mesmo model da geração futura.

    Falha, configuração não suportada ou métrica inválida: teto não verificável.
    Não usa a estimativa local como fallback e não chama generate_content.
    """
    pedido = _pedido_count_tokens(config)
    tem_cache = "cached_content" in pedido
    modelos = getattr(client, "models", None)
    contar = getattr(modelos, "count_tokens", None) if modelos is not None else None
    if not callable(contar):
        logger.warning(
            "Metering count_tokens indisponivel model=%s agent=%s flow_type=%s attempt_key=%s",
            model,
            agent,
            flow_type,
            attempt_key,
        )
        return 0, MOTIVO_TETO_ENTRADA
    logger.info(
        "Metering count_tokens start model=%s agent=%s flow_type=%s attempt_key=%s",
        model,
        agent,
        flow_type,
        attempt_key,
    )
    try:
        resposta = contar(model=model, contents=contents, config=pedido)
    except Exception as exc:
        logger.warning(
            "Metering count_tokens failed model=%s agent=%s flow_type=%s attempt_key=%s type=%s",
            model,
            agent,
            flow_type,
            attempt_key,
            exc.__class__.__name__,
        )
        return 0, MOTIVO_TETO_ENTRADA
    total = _inteiro_nao_negativo(_metrica(resposta, "total_tokens", "totalTokens"))
    if total is None:
        logger.warning(
            "Metering count_tokens metric invalid model=%s attempt_key=%s",
            model,
            attempt_key,
        )
        return 0, MOTIVO_TETO_ENTRADA
    if tem_cache:
        cache = _inteiro_nao_negativo(
            _metrica(resposta, "cached_content_token_count", "cachedContentTokenCount")
        )
        if cache is None or total < cache:
            logger.warning(
                "Metering count_tokens cache nao verificado model=%s attempt_key=%s",
                model,
                attempt_key,
            )
            return 0, MOTIVO_TETO_ENTRADA
    logger.info(
        "Metering count_tokens done model=%s attempt_key=%s total_tokens=%s",
        model,
        attempt_key,
        total,
    )
    return total, None


def _payload_seguro_para_metering(
    contents: Any,
    config: Any,
    *,
    purpose: str,
    agent: str,
) -> tuple[Any, Any]:
    """Mesma governança de saída externa. Não envia conteúdo bruto só para contar."""
    from app.services.cleiton_ai_safe_context import CleitonAiAliasSession

    session = CleitonAiAliasSession()
    governed = govern_or_raise(
        contents,
        purpose=purpose,
        content_type=CONTENT_TYPE_GENERATE_CONTENTS,
        agent=agent,
        provider="gemini",
        alias_session=session,
    )
    safe_config = govern_provider_config(
        config,
        purpose=purpose,
        agent=agent,
        provider="gemini",
        alias_session=session,
    )
    return governed.safe_content, safe_config


def _campo_explicito(config: Any, *nomes: str) -> tuple[bool, Any]:
    """(True, valor) só quando o campo foi definido. Ausência não vira default aqui."""
    if config is None:
        return False, None
    if isinstance(config, dict):
        for nome in nomes:
            if nome in config:
                return True, config[nome]
        return False, None
    definidos = getattr(config, "model_fields_set", None)
    if isinstance(definidos, (set, frozenset)):
        for nome in nomes:
            if nome in definidos:
                return True, getattr(config, nome, None)
        return False, None
    for nome in nomes:
        if hasattr(config, nome):
            valor = getattr(config, nome)
            if valor is not None:
                return True, valor
    return False, None


def _inteiro_estrito(valor: Any) -> int | None:
    if isinstance(valor, bool) or not isinstance(valor, int):
        return None
    return valor


def _thinking_budget_explicito(thinking_raw: Any) -> tuple[bool, Any]:
    if isinstance(thinking_raw, dict):
        if "thinking_budget" in thinking_raw:
            return True, thinking_raw.get("thinking_budget")
        if "thinkingBudget" in thinking_raw:
            return True, thinking_raw.get("thinkingBudget")
        return False, None
    campos = getattr(thinking_raw, "model_fields_set", None)
    if isinstance(campos, (set, frozenset)):
        if "thinking_budget" in campos:
            return True, getattr(thinking_raw, "thinking_budget", None)
        if "thinkingBudget" in campos:
            return True, getattr(thinking_raw, "thinkingBudget", None)
        return False, None
    budget = getattr(thinking_raw, "thinking_budget", None)
    if budget is None:
        budget = getattr(thinking_raw, "thinkingBudget", None)
    if budget is None:
        return False, None
    return True, budget


def resolver_limites_config(
    config: Any,
    max_output_tokens: int | None = None,
) -> tuple[int, int, int, str | None]:
    """Saída por candidato, candidatos e thinking que a config enviada torna verificáveis."""
    presente_saida, bruto_saida = _campo_explicito(config, "max_output_tokens", "maxOutputTokens")
    if presente_saida:
        saida_pedido = _inteiro_estrito(bruto_saida)
        if saida_pedido is None or saida_pedido < 1:
            return 0, 0, 0, MOTIVO_TETO
    else:
        saida_pedido = None
    if max_output_tokens is not None:
        explicito = _inteiro_estrito(max_output_tokens)
        if explicito is None or explicito < 1:
            return 0, 0, 0, MOTIVO_TETO
        saida = explicito if saida_pedido is None else min(saida_pedido, explicito)
    else:
        saida = BILLABLE_AI_MAX_OUTPUT_TOKENS if saida_pedido is None else saida_pedido
    if saida < 1:
        return 0, 0, 0, MOTIVO_TETO
    saida = min(saida, BILLABLE_AI_MAX_OUTPUT_TOKENS)

    presente_cand, bruto_cand = _campo_explicito(config, "candidate_count", "candidateCount")
    if not presente_cand:
        candidatos = 1
    else:
        candidatos = _inteiro_estrito(bruto_cand)
        if candidatos is None or candidatos < 1:
            return 0, 0, 0, MOTIVO_TETO

    presente_think, bruto_think = _campo_explicito(config, "thinking_config", "thinkingConfig")
    if not presente_think or bruto_think is None:
        budget: int | None = BILLABLE_AI_MAX_THINKING_TOKENS
    elif isinstance(bruto_think, (str, int, float, bool, bytes, bytearray)):
        return 0, 0, 0, MOTIVO_TETO
    else:
        presente_budget, bruto_budget = _thinking_budget_explicito(bruto_think)
        if not presente_budget:
            budget = BILLABLE_AI_MAX_THINKING_TOKENS
        else:
            budget = _inteiro_estrito(bruto_budget)
            if budget is None or budget < 0:
                return 0, 0, 0, MOTIVO_TETO
    budget = min(int(budget), BILLABLE_AI_MAX_THINKING_TOKENS)
    return saida, candidatos, budget, None


def tokens_teto_total(entrada: int, saida: int, thinking: int, candidatos: int) -> int:
    """Entrada verificada mais o máximo de saída e thinking por candidato."""
    return int(entrada) + (int(saida) + int(thinking)) * int(candidatos)


def calcular_teto_chamada(
    contents: Any,
    *,
    config: Any = None,
    max_output_tokens: int | None = None,
) -> tuple[int, int, Decimal | None, str | None]:
    """Estimativa local auxiliar. A admissão financeira usa contar_tokens_oficiais."""
    entrada, erro_entrada = estimar_tokens_entrada(contents, config)
    if erro_entrada:
        return 0, 0, None, erro_entrada
    saida, candidatos, thinking, erro_limite = resolver_limites_config(
        config,
        max_output_tokens=max_output_tokens,
    )
    if erro_limite:
        return 0, 0, None, erro_limite
    total = tokens_teto_total(entrada, saida, thinking, candidatos)
    cfg = get_or_create_config()
    creditos, erro = converter_tokens_para_creditos(total, cfg)
    return entrada, saida, creditos, erro


def _afc_desligado() -> Any:
    try:
        from google.genai import types as genai_types

        return genai_types.AutomaticFunctionCallingConfig(disable=True)
    except Exception:
        return {"disable": True}


def _chave_http(nome: str) -> str:
    return "".join(ch for ch in nome.lower() if ch.isalnum())


def _args_http_vazio(valor: Any) -> bool:
    """Ausente, None ou dict puro vazio. Subclasse e qualquer conteúdo bloqueiam."""
    return valor is None or (type(valor) is dict and len(valor) == 0)


def _headers_estaticos(valor: Any) -> bool:
    if valor is None:
        return True
    if type(valor) is not dict:
        return False
    return all(type(chave) is str and type(item) is str for chave, item in valor.items())


def _timeout_estatico(valor: Any) -> bool:
    return valor is None or type(valor) is int


def _retry_estatico(valor: Any) -> bool:
    if valor is None:
        return True
    if callable(valor) or isinstance(valor, (str, bytes, bytearray, memoryview, list, tuple, set, int, float)):
        return False
    return True


def _campos_http_explicitos(http_opts: Any) -> list[tuple[str, Any]] | None:
    """Campos definidos. None quando a forma não é enumerável: fail closed."""
    if isinstance(http_opts, dict):
        saida: list[tuple[str, Any]] = []
        for chave, valor in http_opts.items():
            saida.append((chave if isinstance(chave, str) else "", valor))
        return saida
    definidos = getattr(http_opts, "model_fields_set", None)
    if isinstance(definidos, (set, frozenset)):
        try:
            return [(str(nome), getattr(http_opts, str(nome), None)) for nome in definidos]
        except Exception:
            return None
    dados = getattr(http_opts, "__dict__", None)
    if isinstance(dados, dict):
        return [
            (chave, valor)
            for chave, valor in dados.items()
            if isinstance(chave, str) and not chave.startswith("_")
        ]
    return None


def _http_options_inadmissivel(http_opts: Any) -> bool:
    """Allowlist fechada do transporte. Não inspeciona o interior de auth."""
    if http_opts is None:
        return False
    if http_options_extra_body_nao_vazio(http_opts):
        return True
    campos = _campos_http_explicitos(http_opts)
    if campos is None:
        return True
    for chave, valor in campos:
        if valor is None:
            continue
        nome = _chave_http(chave)
        if nome not in _CHAVES_HTTP_PERMITIDAS:
            return True
        if nome in _CHAVES_ARGS_HTTP and not _args_http_vazio(valor):
            return True
        if nome in _CHAVES_CLIENTE_HTTP:
            return True
        if nome in _CHAVES_CORPO_HTTP and not _args_http_vazio(valor):
            return True
        if nome == "headers" and not _headers_estaticos(valor):
            return True
        if nome in _CHAVES_DESTINO_HTTP and type(valor) is not str:
            return True
        if nome == "timeout" and not _timeout_estatico(valor):
            return True
        if nome == "retryoptions" and not _retry_estatico(valor):
            return True
    return False


def _http_options_do_cliente(client: Any) -> Any:
    api = getattr(client, "_api_client", None)
    if api is not None:
        options = getattr(api, "_http_options", None)
        if options is not None:
            return options
    for attr in ("http_options", "_http_options"):
        options = getattr(client, attr, None)
        if options is not None:
            return options
    return None


def _payload_mutavel_depois_da_contagem(config: Any, client: Any) -> bool:
    """Config e cliente. O httpx já construído com auth não pode ser usado."""
    for origem in (
        _valor_config(config, "http_options", "httpOptions"),
        _http_options_do_cliente(client),
    ):
        if _http_options_inadmissivel(origem):
            return True
    return False


def _campo_http_copiado(origem: Any, *nomes: str) -> Any:
    presente, valor = _campo_explicito(origem, *nomes)
    if not presente or valor is None:
        return None
    try:
        return copy.deepcopy(valor)
    except Exception:
        return valor


def _http_options_canonico(config: Any) -> Any:
    """Só campos estáticos da allowlist. Não recoloca args, auth nem cliente próprio."""
    origem = _valor_config(config, "http_options", "httpOptions")
    transporte: dict[str, Any] = {}
    if origem is not None and not _http_options_inadmissivel(origem):
        timeout = _campo_http_copiado(origem, "timeout")
        if _timeout_estatico(timeout) and timeout is not None:
            transporte["timeout"] = timeout
        headers = _campo_http_copiado(origem, "headers")
        if _headers_estaticos(headers) and headers:
            transporte["headers"] = headers
        for destino, nomes in (
            ("base_url", ("base_url", "baseUrl")),
            ("api_version", ("api_version", "apiVersion")),
        ):
            valor = _campo_http_copiado(origem, *nomes)
            if type(valor) is str:
                transporte[destino] = valor
    base = _http_options_retry_unico()
    if not transporte:
        return base
    if isinstance(base, dict):
        base.update(transporte)
        return base
    try:
        return base.model_copy(update=transporte)
    except Exception:
        return base


def _thinking_explicito(budget: int) -> Any:
    try:
        from google.genai import types as genai_types

        return genai_types.ThinkingConfig(thinking_budget=int(budget))
    except Exception:
        return {"thinking_budget": int(budget)}


def _preparar_config_limitada(
    config: Any,
    *,
    saida: int,
    candidatos: int,
    thinking: int,
) -> tuple[Any, bool, str | None]:
    """Cópia da config com o teto reservado. Não altera o objeto do chamador."""
    if _http_options_inadmissivel(_valor_config(config, "http_options", "httpOptions")):
        return None, False, MOTIVO_TETO_REQUEST_MUTAVEL
    afc = _afc_desligado()
    pensamento = _thinking_explicito(thinking)
    http_opts = _http_options_canonico(config)
    if config is None:
        try:
            from google.genai import types as genai_types

            afc_obj = afc if not isinstance(afc, dict) else genai_types.AutomaticFunctionCallingConfig(disable=True)
            thinking_obj = (
                pensamento
                if not isinstance(pensamento, dict)
                else genai_types.ThinkingConfig(thinking_budget=int(thinking))
            )
            return (
                genai_types.GenerateContentConfig(
                    max_output_tokens=int(saida),
                    candidate_count=int(candidatos),
                    automatic_function_calling=afc_obj,
                    thinking_config=thinking_obj,
                    http_options=http_opts if not isinstance(http_opts, dict) else None,
                ),
                True,
                None,
            )
        except Exception:
            return (
                {
                    "max_output_tokens": int(saida),
                    "candidate_count": int(candidatos),
                    "automatic_function_calling": {"disable": True},
                    "thinking_config": {"thinking_budget": int(thinking)},
                    "http_options": http_opts if isinstance(http_opts, dict) else _http_options_retry_unico(),
                },
                True,
                None,
            )
    limites = {
        "max_output_tokens": int(saida),
        "candidate_count": int(candidatos),
        "http_options": http_opts,
    }
    if isinstance(config, dict):
        try:
            copied = copy.deepcopy(config)
        except Exception:
            return None, False, MOTIVO_TETO_REQUEST_MUTAVEL
        copied.update(limites)
        copied["automatic_function_calling"] = {"disable": True}
        copied["thinking_config"] = {"thinking_budget": int(thinking)}
        return copied, True, None
    updates = {
        **limites,
        "automatic_function_calling": afc,
        "thinking_config": pensamento,
    }
    if hasattr(config, "model_copy"):
        try:
            return config.model_copy(deep=True, update=updates), True, None
        except Exception:
            try:
                return config.model_copy(update=updates), True, None
            except Exception:
                return None, False, MOTIVO_TETO_REQUEST_MUTAVEL
    return None, False, MOTIVO_TETO_REQUEST_MUTAVEL


def _commit_session() -> None:
    db.session.commit()


def _checkpoint_pos_debito() -> None:
    """Ponto estável entre o commit do débito e a liberação da reserva."""
    return None


def _begin_write_lock() -> None:
    _commit_session()
    bind = db.session.get_bind()
    if bind is not None and bind.dialect.name == "sqlite":
        db.session.execute(text("BEGIN IMMEDIATE"))


def _reason(value: str) -> str:
    return (value or "")[:200]


def _id_inteiro(valor: Any) -> int | None:
    if valor is None or isinstance(valor, bool):
        return None
    try:
        return int(valor)
    except (TypeError, ValueError):
        return None


def _tripla_coerente(conta_id: int, franquia_id: int, usuario_id: int) -> str | None:
    from app.models import Conta, User

    usuario = db.session.get(User, int(usuario_id))
    conta = db.session.get(Conta, int(conta_id))
    franquia = db.session.get(Franquia, int(franquia_id))
    if usuario is None or conta is None or franquia is None:
        return MOTIVO_IDENTIDADE_INCOERENTE
    if int(usuario.conta_id) != int(conta_id) or int(usuario.franquia_id) != int(franquia_id):
        return MOTIVO_IDENTIDADE_INCOERENTE
    if int(franquia.conta_id) != int(conta_id):
        return MOTIVO_IDENTIDADE_INCOERENTE
    return None


def _contradiz_contexto(conta_id: int, franquia_id: int, usuario_id: int) -> bool:
    atual = get_consumo_identidade()
    if not atual:
        return False
    for chave, valor in (
        ("conta_id", conta_id),
        ("franquia_id", franquia_id),
        ("usuario_id", usuario_id),
    ):
        existente = atual.get(chave)
        if existente is not None and int(existente) != int(valor):
            return True
    if atual.get("origem_sistema") is True:
        return True
    return False


def _identidade_cliente(conta_id: int, franquia_id: int, usuario_id: int) -> tuple[dict[str, Any] | None, str | None]:
    motivo = _tripla_coerente(conta_id, franquia_id, usuario_id)
    if motivo:
        return None, motivo
    if _contradiz_contexto(conta_id, franquia_id, usuario_id):
        return None, MOTIVO_IDENTIDADE_INCOERENTE
    return {
        "conta_id": int(conta_id),
        "franquia_id": int(franquia_id),
        "usuario_id": int(usuario_id),
        "tipo_origem": TIPO_ORIGEM_HTTP_USUARIO,
        "origem_sistema": False,
    }, None


def _resolver_identidade(
    *,
    usuario: Any,
    conta_id: int | None,
    franquia_id: int | None,
    usuario_id: int | None,
    origem_sistema: bool,
) -> tuple[dict[str, Any] | None, str | None]:
    if origem_sistema:
        return identidade_sistema_interna("system_origin"), None
    if usuario is not None and getattr(usuario, "is_authenticated", True):
        from app.models import User

        uid = _id_inteiro(getattr(usuario, "id", None))
        if uid is None:
            return None, MOTIVO_IDENTIDADE_INCOERENTE
        row = db.session.get(User, uid)
        if row is None or row.conta_id is None or row.franquia_id is None:
            return None, MOTIVO_IDENTIDADE_INCOERENTE
        cid = int(row.conta_id)
        fid = int(row.franquia_id)
        if conta_id is not None and _id_inteiro(conta_id) != cid:
            return None, MOTIVO_IDENTIDADE_INCOERENTE
        if franquia_id is not None and _id_inteiro(franquia_id) != fid:
            return None, MOTIVO_IDENTIDADE_INCOERENTE
        if usuario_id is not None and _id_inteiro(usuario_id) != uid:
            return None, MOTIVO_IDENTIDADE_INCOERENTE
        return _identidade_cliente(cid, fid, uid)
    cid = _id_inteiro(conta_id)
    fid = _id_inteiro(franquia_id)
    uid = _id_inteiro(usuario_id)
    if cid is None and fid is None and uid is None:
        return None, MOTIVO_IDENTIDADE_AUSENTE
    if cid is None or fid is None or uid is None:
        return None, MOTIVO_IDENTIDADE_INCOERENTE
    return _identidade_cliente(cid, fid, uid)


def _nova_tentativa(
    *,
    attempt_key: str,
    ident: dict[str, Any] | None,
    agent: str,
    flow_type: str,
    operation: str,
    provider: str,
    model: str,
    status: str,
    reserved: Decimal,
    reason: str | None,
    reserva_ativa: bool,
    debita_cliente: bool,
    ciclo_inicio: Any = None,
    ciclo_fim: Any = None,
) -> IaChamadaTentativa:
    ident = ident or {}
    row = IaChamadaTentativa(
        attempt_key=attempt_key[:160],
        conta_id=ident.get("conta_id"),
        franquia_id=ident.get("franquia_id"),
        usuario_id=ident.get("usuario_id"),
        origem_sistema=bool(ident.get("origem_sistema")),
        agent=(agent or "")[:80],
        flow_type=(flow_type or "")[:80],
        operation=(operation or "")[:40],
        provider=(provider or "")[:40],
        model=(model or "")[:255],
        ciclo_inicio=ciclo_inicio,
        ciclo_fim=ciclo_fim,
        reserved_credits=reserved,
        status=status,
        failure_reason=_reason(reason) if reason else None,
        reserva_ativa=reserva_ativa,
        debita_cliente=debita_cliente,
        created_at=utcnow_naive(),
    )
    db.session.add(row)
    return row


def _mensagem(status_franquia: str, motivo: str, plano: str | None) -> str | None:
    try:
        return montar_mensagem_operacao(
            status_franquia=status_franquia,
            motivo=motivo,
            plano_resolvido=plano,
            sugerir_upgrade=status_franquia in (
                Franquia.STATUS_DEGRADED,
                Franquia.STATUS_BLOCKED,
                Franquia.STATUS_EXPIRED,
            ),
        )
    except Exception:
        return None


def _bloquear_sem_reserva(
    *,
    attempt_key: str,
    ident: dict[str, Any] | None,
    agent: str,
    flow_type: str,
    operation: str,
    provider: str,
    model: str,
    motivo: str,
    mensagem_usuario: str | None = None,
) -> None:
    if has_app_context():
        try:
            _nova_tentativa(
                attempt_key=attempt_key,
                ident=ident,
                agent=agent,
                flow_type=flow_type,
                operation=operation,
                provider=provider,
                model=model,
                status=IaChamadaTentativa.STATUS_BLOCKED,
                reserved=Decimal("0"),
                reason=motivo,
                reserva_ativa=False,
                debita_cliente=False,
            )
            _commit_session()
        except IntegrityError:
            db.session.rollback()
        except Exception:
            db.session.rollback()
            logger.warning("Falha ao persistir tentativa bloqueada %s", attempt_key)
    raise BillableAiAdmissionBlocked(
        motivo,
        motivo=motivo,
        attempt_key=attempt_key,
        mensagem_usuario=mensagem_usuario,
    )


def _bloquear_governanca(
    *,
    attempt_key: str,
    ident: dict[str, Any] | None,
    agent: str,
    flow_type: str,
    operation: str,
    provider: str,
    model: str,
    motivo: str,
    mensagem_usuario: str | None = None,
) -> None:
    if has_app_context():
        try:
            _nova_tentativa(
                attempt_key=attempt_key,
                ident=ident,
                agent=agent,
                flow_type=flow_type,
                operation=operation,
                provider=provider,
                model=model,
                status=IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
                reserved=Decimal("0"),
                reason=motivo,
                reserva_ativa=False,
                debita_cliente=False,
            )
            _commit_session()
        except IntegrityError:
            db.session.rollback()
        except Exception:
            db.session.rollback()
            logger.warning("Falha ao persistir bloqueio de governança %s", attempt_key)
    raise BillableAiGovernanceBlocked(
        motivo,
        motivo=motivo,
        attempt_key=attempt_key,
        mensagem_usuario=mensagem_usuario,
    )


def _liberar_reserva(attempt_id: int, *, status: str, reason: str) -> None:
    _begin_write_lock()
    attempt = (
        db.session.query(IaChamadaTentativa)
        .filter(IaChamadaTentativa.id == int(attempt_id))
        .with_for_update()
        .one()
    )
    if attempt.reserva_ativa and attempt.franquia_id is not None:
        franquia = (
            db.session.query(Franquia)
            .filter(Franquia.id == int(attempt.franquia_id))
            .with_for_update()
            .one_or_none()
        )
        if franquia is not None:
            ensure_franquia_operacional_inicializada(franquia)
            atual = _to_decimal(franquia.reserva_pendente)
            liberar = _to_decimal(attempt.reserved_credits)
            franquia.reserva_pendente = _quantize_credit(max(Decimal("0"), atual - liberar))
        attempt.reserva_ativa = False
    attempt.status = status
    attempt.failure_reason = _reason(reason)
    if status != IaChamadaTentativa.STATUS_UNCERTAIN:
        attempt.settled_at = utcnow_naive()
    _commit_session()


def liquidar_tentativa_ia(attempt_id: int, *, status_final: str = IaChamadaTentativa.STATUS_SETTLED) -> IaChamadaTentativa | None:
    """Liquida no máximo uma vez. Reexecução não gera segundo débito."""
    if not has_app_context():
        return None
    try:
        return _liquidar_tentativa_ia(int(attempt_id), status_final=status_final)
    except BillableAiUncertainError:
        raise
    except Exception as exc:
        _marcar_incerta(int(attempt_id), MOTIVO_LIQUIDACAO)
        raise BillableAiUncertainError(
            MOTIVO_LIQUIDACAO,
            motivo=MOTIVO_LIQUIDACAO,
            provider_called=True,
        ) from exc


def _liquidar_tentativa_ia(attempt_id: int, *, status_final: str) -> IaChamadaTentativa | None:
    attempt = db.session.get(IaChamadaTentativa, int(attempt_id))
    if attempt is None:
        return None
    if attempt.status in (
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
    ) and attempt.settled_at is not None:
        return attempt
    if attempt.status in (
        IaChamadaTentativa.STATUS_BLOCKED,
        IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
        IaChamadaTentativa.STATUS_RELEASED,
    ):
        return attempt
    if attempt.status == IaChamadaTentativa.STATUS_UNCERTAIN and attempt.ia_consumo_evento_id is None:
        return attempt
    if attempt.ia_consumo_evento_id is None:
        _marcar_incerta(attempt.id, MOTIVO_PERSISTENCIA_EVENTO)
        return db.session.get(IaChamadaTentativa, int(attempt_id))

    from app.models import IaConsumoEvento

    evento = db.session.get(IaConsumoEvento, int(attempt.ia_consumo_evento_id))
    if evento is None:
        _marcar_incerta(attempt.id, MOTIVO_PERSISTENCIA_EVENTO)
        return db.session.get(IaChamadaTentativa, int(attempt_id))
    if evento.status == STATUS_SUCCESS_NO_METRICS and _usage_ausente(evento):
        _marcar_incerta(attempt.id, MOTIVO_SUCCESS_NO_METRICS)
        return db.session.get(IaChamadaTentativa, int(attempt_id))

    get_or_create_config()
    from app.services.cleiton_franquia_operacional_service import creditos_totais_de_evento_ia

    cfg = get_or_create_config()
    creditos, erro = creditos_totais_de_evento_ia(evento, cfg)
    if erro:
        _marcar_incerta(attempt.id, "falha_conversao_creditos")
        return db.session.get(IaChamadaTentativa, int(attempt_id))
    creditos = _quantize_credit(_to_decimal(creditos))
    reservado = _quantize_credit(_to_decimal(attempt.reserved_credits))
    if attempt.debita_cliente and reservado > 0 and creditos > reservado:
        _marcar_incerta(attempt.id, MOTIVO_USO_EXCEDE)
        tentativa = db.session.get(IaChamadaTentativa, int(attempt_id))
        if tentativa is not None:
            tentativa.actual_credits = creditos
            _commit_session()
        return db.session.get(IaChamadaTentativa, int(attempt_id))

    if attempt.debita_cliente and creditos > 0:
        aplicar_motor_apos_ia_consumo_evento(int(evento.id), commit=True)

    _checkpoint_pos_debito()
    _begin_write_lock()
    locked = (
        db.session.query(IaChamadaTentativa)
        .filter(IaChamadaTentativa.id == int(attempt_id))
        .with_for_update()
        .one()
    )
    if locked.settled_at is not None and locked.status in (
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
    ):
        _commit_session()
        return locked
    if locked.reserva_ativa and locked.franquia_id is not None:
        franquia = (
            db.session.query(Franquia)
            .filter(Franquia.id == int(locked.franquia_id))
            .with_for_update()
            .one_or_none()
        )
        if franquia is not None:
            ensure_franquia_operacional_inicializada(franquia)
            atual = _to_decimal(franquia.reserva_pendente)
            franquia.reserva_pendente = _quantize_credit(
                max(Decimal("0"), atual - _to_decimal(locked.reserved_credits))
            )
        locked.reserva_ativa = False
    locked.actual_credits = creditos
    locked.status = status_final
    locked.settled_at = utcnow_naive()
    locked.failure_reason = None if status_final == IaChamadaTentativa.STATUS_SETTLED else locked.failure_reason
    _commit_session()
    return db.session.get(IaChamadaTentativa, int(attempt_id))


def _usage_ausente(evento: Any) -> bool:
    return evento.input_tokens is None and evento.output_tokens is None and evento.total_tokens is None


def _marcar_incerta(attempt_id: int, reason: str) -> None:
    try:
        db.session.rollback()
    except Exception:
        pass
    _begin_write_lock()
    attempt = (
        db.session.query(IaChamadaTentativa)
        .filter(IaChamadaTentativa.id == int(attempt_id))
        .with_for_update()
        .one()
    )
    if attempt.status == IaChamadaTentativa.STATUS_UNCERTAIN:
        _commit_session()
        return
    if attempt.settled_at is not None:
        _commit_session()
        return
    attempt.status = IaChamadaTentativa.STATUS_UNCERTAIN
    attempt.failure_reason = _reason(reason)
    _commit_session()


def _persistir_vinculo_evento(attempt_id: int, evento_id: int) -> None:
    attempt = db.session.get(IaChamadaTentativa, int(attempt_id))
    if attempt is None:
        raise RuntimeError("tentativa_ausente")
    attempt.ia_consumo_evento_id = int(evento_id)
    _commit_session()


def _vincular_evento(attempt_id: int, evento_id: int | None) -> None:
    if evento_id is None:
        return
    try:
        _persistir_vinculo_evento(int(attempt_id), int(evento_id))
    except BillableAiUncertainError:
        raise
    except Exception as exc:
        _marcar_incerta(int(attempt_id), MOTIVO_VINCULO_EVENTO)
        raise BillableAiUncertainError(
            MOTIVO_VINCULO_EVENTO,
            motivo=MOTIVO_VINCULO_EVENTO,
            provider_called=True,
        ) from exc


def cleiton_governed_billable_ai_call(
    client: Any,
    *,
    model: str,
    contents: Any,
    config: Any = None,
    agent: str,
    flow_type: str,
    api_key_label: str,
    operation: str = "generate_content",
    provider: str = "gemini",
    usuario: Any = None,
    conta_id: int | None = None,
    franquia_id: int | None = None,
    usuario_id: int | None = None,
    origem_sistema: bool = False,
    attempt_key: str | None = None,
    purpose: str | None = None,
    max_output_tokens: int | None = None,
    max_reserved_credits: Decimal | int | str | None = None,
) -> BillableAiCallResult:
    """Único entry point novo de geração externa governada e faturável."""
    key = (attempt_key or uuid4().hex)[:160]
    if not has_app_context():
        raise BillableAiAdmissionBlocked(
            MOTIVO_PERSISTENCIA_RESERVA,
            motivo=MOTIVO_PERSISTENCIA_RESERVA,
            attempt_key=key,
        )

    retries = tentativas_retry_sdk(client)
    if retries is None or retries != SDK_RETRY_ATTEMPTS:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_SDK_RETRY,
        )
    retries_config = tentativas_retry_config(config)
    if retries_config is not None and retries_config != SDK_RETRY_ATTEMPTS:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_SDK_RETRY,
        )

    saida, candidatos, thinking, erro_limite = resolver_limites_config(
        config,
        max_output_tokens=max_output_tokens,
    )
    if erro_limite:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=erro_limite,
        )
    if _midia_nao_contavel(contents, set()) or _midia_nao_contavel(config, set()):
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_TETO_ENTRADA,
        )
    if _payload_mutavel_depois_da_contagem(config, client):
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_TETO_REQUEST_MUTAVEL,
        )

    entrada_local, erro_local = estimar_tokens_entrada(contents, config)
    if erro_local is None:
        logger.info(
            "Estimativa local de entrada sem efeito financeiro attempt_key=%s tokens=%s",
            key,
            entrada_local,
        )

    resolved_purpose = purpose or purpose_from_flow_type(flow_type, agent)
    try:
        safe_contents, safe_config = _payload_seguro_para_metering(
            contents,
            config,
            purpose=resolved_purpose,
            agent=agent,
        )
    except CleitonAiGovernanceBlockedError as exc:
        _bloquear_governanca(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo="governance_blocked",
            mensagem_usuario=getattr(exc, "user_message", None),
        )

    config_cap, teto_ok, motivo_cfg = _preparar_config_limitada(
        safe_config,
        saida=saida,
        candidatos=candidatos,
        thinking=thinking,
    )
    if not teto_ok:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=motivo_cfg or MOTIVO_TETO_REQUEST_MUTAVEL,
        )
    try:
        conteudo_contado = copy.deepcopy(safe_contents)
        conteudo_gerado = copy.deepcopy(safe_contents)
        config_contagem = copy.deepcopy(config_cap)
        config_geracao = copy.deepcopy(config_cap)
    except Exception:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_TETO_REQUEST_MUTAVEL,
        )

    entrada, erro_count = contar_tokens_oficiais(
        client,
        model=model,
        contents=conteudo_contado,
        config=config_contagem,
        attempt_key=key,
        agent=agent,
        flow_type=flow_type,
    )
    if erro_count:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=erro_count,
        )

    total_tokens = tokens_teto_total(entrada, saida, thinking, candidatos)
    try:
        convertido, erro_regua = converter_tokens_para_creditos(total_tokens, get_or_create_config())
    except Exception:
        convertido, erro_regua = None, MOTIVO_REGUA
    # Ausência explícita. Decimal("0") só segue quando a conversão calculou zero.
    teto_conhecido = None if erro_regua or convertido is None else convertido
    if teto_conhecido is None and max_reserved_credits is not None:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_TETO_CREDITOS_NAO_VERIFICAVEL,
        )
    if teto_conhecido is None and not origem_sistema:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=MOTIVO_REGUA,
        )
    logger.info(
        "Teto financeiro verificado attempt_key=%s model=%s entrada=%s saida=%s thinking=%s candidatos=%s",
        key,
        model,
        entrada,
        saida,
        thinking,
        candidatos,
    )

    usuario_efetivo = usuario_operacional_da_chamada(usuario)
    ident, motivo_ident = _resolver_identidade(
        usuario=usuario_efetivo,
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        origem_sistema=origem_sistema,
    )
    if motivo_ident:
        _bloquear_sem_reserva(
            attempt_key=key,
            ident=None,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            motivo=motivo_ident,
        )

    assert ident is not None
    if max_reserved_credits is not None:
        if teto_conhecido is None:
            _bloquear_sem_reserva(
                attempt_key=key,
                ident=ident,
                agent=agent,
                flow_type=flow_type,
                operation=operation,
                provider=provider,
                model=model,
                motivo=MOTIVO_TETO_CREDITOS_NAO_VERIFICAVEL,
            )
        else:
            try:
                limite_job = _quantize_credit(_to_decimal(max_reserved_credits))
            except Exception:
                _bloquear_sem_reserva(
                    attempt_key=key,
                    ident=ident,
                    agent=agent,
                    flow_type=flow_type,
                    operation=operation,
                    provider=provider,
                    model=model,
                    motivo=MOTIVO_TETO_RESERVA_JOB,
                )
            else:
                if _quantize_credit(teto_conhecido) > limite_job:
                    _bloquear_sem_reserva(
                        attempt_key=key,
                        ident=ident,
                        agent=agent,
                        flow_type=flow_type,
                        operation=operation,
                        provider=provider,
                        model=model,
                        motivo=MOTIVO_TETO_RESERVA_JOB,
                    )
    # Origem sem hard cap e sem régua conserva a reserva histórica, sem débito.
    teto_reserva = teto_conhecido if teto_conhecido is not None else Decimal("0")
    try:
        attempt = _reservar(
            attempt_key=key,
            ident=ident,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            teto=_quantize_credit(teto_reserva),
        )
    except IntegrityError:
        db.session.rollback()
        existente = db.session.query(IaChamadaTentativa).filter_by(attempt_key=key).one_or_none()
        if existente is not None and existente.status in (
            IaChamadaTentativa.STATUS_BLOCKED,
            IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
        ):
            raise BillableAiAdmissionBlocked(
                existente.failure_reason or "bloqueada",
                motivo=existente.failure_reason or "bloqueada",
                attempt_key=key,
            ) from None
        raise BillableAiAdmissionBlocked(
            MOTIVO_PERSISTENCIA_RESERVA,
            motivo=MOTIVO_PERSISTENCIA_RESERVA,
            attempt_key=key,
        ) from None
    except BillableAiAdmissionBlocked:
        raise
    except Exception as exc:
        db.session.rollback()
        logger.warning("Reserva de IA não persistiu: %s", exc.__class__.__name__)
        raise BillableAiAdmissionBlocked(
            MOTIVO_PERSISTENCIA_RESERVA,
            motivo=MOTIVO_PERSISTENCIA_RESERVA,
            attempt_key=key,
        ) from exc

    if attempt.status == IaChamadaTentativa.STATUS_BLOCKED:
        raise BillableAiAdmissionBlocked(
            attempt.failure_reason or "bloqueada",
            motivo=attempt.failure_reason or "bloqueada",
            attempt_key=key,
            mensagem_usuario=_mensagem(
                Franquia.STATUS_BLOCKED,
                attempt.failure_reason or "",
                None,
            ),
        )

    previous_ident = get_consumo_identidade()
    set_consumo_identidade(ident)
    try:
        calling = db.session.get(IaChamadaTentativa, attempt.id)
        if calling is not None and calling.status == IaChamadaTentativa.STATUS_RESERVED:
            calling.status = IaChamadaTentativa.STATUS_CALLING
            _commit_session()
        observation = cleiton_governed_generate_content_observed(
            client,
            model=model,
            contents=conteudo_gerado,
            config=config_geracao,
            agent=agent,
            flow_type=flow_type,
            api_key_label=api_key_label,
            purpose=purpose,
            identidade=ident,
            apply_motor=False,
        )
    except CleitonAiGovernanceBlockedError as exc:
        _liberar_reserva(
            attempt.id,
            status=IaChamadaTentativa.STATUS_GOVERNANCE_BLOCKED,
            reason="governance_blocked",
        )
        raise BillableAiGovernanceBlocked(
            "governance_blocked",
            motivo="governance_blocked",
            attempt_key=key,
            mensagem_usuario=getattr(exc, "user_message", None),
        ) from exc
    finally:
        if previous_ident is None:
            try:
                clear_consumo_identidade()
            except Exception:
                pass
        else:
            set_consumo_identidade(previous_ident)

    try:
        return _concluir_chamada_observada(
            attempt_id=attempt.id,
            attempt_key=key,
            observation=observation,
        )
    except BillableAiUncertainError:
        raise
    except Exception as exc:
        if observation.provider_exception is not None and exc is observation.provider_exception:
            raise
        _marcar_incerta(attempt.id, MOTIVO_LIQUIDACAO)
        raise BillableAiUncertainError(
            MOTIVO_LIQUIDACAO,
            motivo=MOTIVO_LIQUIDACAO,
            attempt_key=key,
            provider_called=True,
            response=observation.response,
        ) from exc


def _concluir_chamada_observada(*, attempt_id: int, attempt_key: str, observation: Any) -> BillableAiCallResult:
    _vincular_evento(attempt_id, observation.evento_id)
    if observation.persist_status != PERSIST_OK or observation.evento_id is None:
        _marcar_incerta(attempt_id, MOTIVO_PERSISTENCIA_EVENTO)
        raise BillableAiUncertainError(
            MOTIVO_PERSISTENCIA_EVENTO,
            motivo=MOTIVO_PERSISTENCIA_EVENTO,
            attempt_key=attempt_key,
            provider_called=observation.provider_called,
            response=observation.response,
        )
    if observation.status == STATUS_SUCCESS_NO_METRICS:
        _marcar_incerta(attempt_id, MOTIVO_SUCCESS_NO_METRICS)
        raise BillableAiUncertainError(
            MOTIVO_SUCCESS_NO_METRICS,
            motivo=MOTIVO_SUCCESS_NO_METRICS,
            attempt_key=attempt_key,
            provider_called=True,
            response=observation.response,
        )
    if observation.status == STATUS_FAILURE and _usage_observation_ausente(observation):
        _marcar_incerta(attempt_id, MOTIVO_PROVIDER_SEM_USAGE)
        raise BillableAiUncertainError(
            MOTIVO_PROVIDER_SEM_USAGE,
            motivo=MOTIVO_PROVIDER_SEM_USAGE,
            attempt_key=attempt_key,
            provider_called=True,
        ) from observation.provider_exception

    status_final = (
        IaChamadaTentativa.STATUS_PROVIDER_FAILED
        if observation.status == STATUS_FAILURE
        else IaChamadaTentativa.STATUS_SETTLED
    )
    if observation.status == STATUS_FAILURE:
        row = db.session.get(IaChamadaTentativa, attempt_id)
        if row is not None:
            row.failure_reason = _reason("provider_failure")
            _commit_session()
    liquidada = liquidar_tentativa_ia(attempt_id, status_final=status_final)
    if liquidada is not None and liquidada.status == IaChamadaTentativa.STATUS_UNCERTAIN:
        raise BillableAiUncertainError(
            liquidada.failure_reason or MOTIVO_USO_EXCEDE,
            motivo=liquidada.failure_reason or MOTIVO_USO_EXCEDE,
            attempt_key=attempt_key,
            provider_called=True,
            response=observation.response,
        )
    if observation.provider_exception is not None:
        raise observation.provider_exception
    if liquidada is None or liquidada.status not in (
        IaChamadaTentativa.STATUS_SETTLED,
        IaChamadaTentativa.STATUS_PROVIDER_FAILED,
    ):
        raise BillableAiUncertainError(
            MOTIVO_PERSISTENCIA_EVENTO,
            motivo=MOTIVO_PERSISTENCIA_EVENTO,
            attempt_key=attempt_key,
            provider_called=True,
            response=observation.response,
        )
    return BillableAiCallResult(
        response=observation.response,
        evento_id=observation.evento_id,
        input_tokens=observation.input_tokens,
        output_tokens=observation.output_tokens,
        total_tokens=observation.total_tokens,
        persist_status=observation.persist_status,
        attempt_id=liquidada.id,
        attempt_key=attempt_key,
        status=liquidada.status,
        complete=liquidada.status == IaChamadaTentativa.STATUS_SETTLED,
        reserved_credits=_to_decimal(liquidada.reserved_credits),
        actual_credits=None if liquidada.actual_credits is None else _to_decimal(liquidada.actual_credits),
    )


def cleiton_governed_generate_content(
    client: Any,
    *,
    model: str,
    contents: Any,
    agent: str,
    flow_type: str,
    api_key_label: str,
    config: Any = None,
    purpose: str | None = None,
    usuario: Any = None,
    origem_sistema: bool = False,
    attempt_key: str | None = None,
    operation: str = "generate_content",
    provider: str = "gemini",
    conta_id: int | None = None,
    franquia_id: int | None = None,
    usuario_id: int | None = None,
    max_output_tokens: int | None = None,
) -> Any:
    """Assinatura histórica dos runners: a fachada é o entry point e devolve a resposta."""
    result = cleiton_governed_billable_ai_call(
        client,
        model=model,
        contents=contents,
        config=config,
        agent=agent,
        flow_type=flow_type,
        api_key_label=api_key_label,
        operation=operation,
        provider=provider,
        usuario=usuario,
        conta_id=conta_id,
        franquia_id=franquia_id,
        usuario_id=usuario_id,
        origem_sistema=origem_sistema,
        attempt_key=attempt_key,
        purpose=purpose,
        max_output_tokens=max_output_tokens,
    )
    if not result.complete:
        raise BillableAiUncertainError(
            "consumo_incerto",
            motivo="consumo_incerto",
            attempt_key=result.attempt_key,
            response=result.response,
        )
    return result.response


def _usage_observation_ausente(observation: Any) -> bool:
    return (
        observation.input_tokens is None
        and observation.output_tokens is None
        and observation.total_tokens is None
    )


def _reservar(
    *,
    attempt_key: str,
    ident: dict[str, Any],
    agent: str,
    flow_type: str,
    operation: str,
    provider: str,
    model: str,
    teto: Decimal,
) -> IaChamadaTentativa:
    _begin_write_lock()
    origem = bool(ident.get("origem_sistema"))
    franquia = None
    if ident.get("franquia_id") is not None:
        franquia = (
            db.session.query(Franquia)
            .filter(Franquia.id == int(ident["franquia_id"]))
            .with_for_update()
            .one_or_none()
        )
    if origem:
        _cid, sid = get_sistema_interno_ids()
        if sid is not None:
            franquia = (
                db.session.query(Franquia)
                .filter(Franquia.id == int(sid))
                .with_for_update()
                .one_or_none()
            )
            ident = dict(ident)
            ident["franquia_id"] = sid
            ident["conta_id"] = _cid
    if not origem and franquia is None:
        row = _nova_tentativa(
            attempt_key=attempt_key,
            ident=ident,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            status=IaChamadaTentativa.STATUS_BLOCKED,
            reserved=Decimal("0"),
            reason="franquia_nao_encontrada",
            reserva_ativa=False,
            debita_cliente=False,
        )
        _commit_session()
        return row

    ciclo_inicio = None
    ciclo_fim = None
    if franquia is not None:
        ensure_franquia_operacional_inicializada(franquia)
        garantir_ciclo_operacional_franquia(franquia.id, commit=False)
        franquia = db.session.get(Franquia, franquia.id)
        ciclo_inicio = franquia.inicio_ciclo if franquia is not None else None
        ciclo_fim = franquia.fim_ciclo if franquia is not None else None

    plano = resolver_plano_operacional_para_franquia(int(ident["franquia_id"])) if ident.get("franquia_id") else None
    if franquia is None or plano is None:
        decisao_admitida = False
        motivo = "franquia_nao_encontrada"
        debita = False
        classe_status = Franquia.STATUS_BLOCKED
    else:
        decisao = decidir_admissao_chamada_ia(
            franquia,
            plano,
            creditos_necessarios=teto,
            origem_sistema=origem,
        )
        decisao_admitida = decisao.admitida
        motivo = decisao.motivo
        debita = decisao.debita_cliente
        classe_status = decisao.classe

    if not decisao_admitida:
        row = _nova_tentativa(
            attempt_key=attempt_key,
            ident=ident,
            agent=agent,
            flow_type=flow_type,
            operation=operation,
            provider=provider,
            model=model,
            status=IaChamadaTentativa.STATUS_BLOCKED,
            reserved=Decimal("0"),
            reason=motivo,
            reserva_ativa=False,
            debita_cliente=False,
            ciclo_inicio=ciclo_inicio,
            ciclo_fim=ciclo_fim,
        )
        _commit_session()
        row.failure_reason = motivo
        return row

    reservar_saldo = debita and franquia is not None and classe_status not in ("internal", "unlimited")
    reserved = teto if reservar_saldo else Decimal("0")
    if reservar_saldo and franquia is not None:
        ensure_franquia_operacional_inicializada(franquia)
        franquia.reserva_pendente = _quantize_credit(_to_decimal(franquia.reserva_pendente) + reserved)
    row = _nova_tentativa(
        attempt_key=attempt_key,
        ident=ident,
        agent=agent,
        flow_type=flow_type,
        operation=operation,
        provider=provider,
        model=model,
        status=IaChamadaTentativa.STATUS_RESERVED,
        reserved=reserved,
        reason=None,
        reserva_ativa=reservar_saldo,
        debita_cliente=debita,
        ciclo_inicio=ciclo_inicio,
        ciclo_fim=ciclo_fim,
    )
    _commit_session()
    return row
