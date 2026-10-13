"""
Orçamento técnico da extração de tabela do AgenteCompara.

O teto de 30.000 tokens vale para a operação inteira, em qualquer plano:
soma de entrada, thinking e resposta de todas as tentativas, inclusive fallback.
O valor autorizado é o menor entre esse teto, os tokens cobertos pelo saldo
disponível e a capacidade contabilizável do modelo selecionado.

O saldo já desconta créditos consumidos e reservas pendentes. A conversão usa
CleitonCostConfig. A reserva, a liquidação e o ciclo mensal continuam na fachada
billable, com a mesma fórmula de teto por chamada.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Any

from flask import has_app_context

from app.extensions import db
from app.models import Franquia
from app.services.cleiton_cost_service import get_or_create_config
from app.services.cleiton_franquia_operacional_service import (
    converter_tokens_para_creditos,
    saldo_disponivel_chamada_ia,
)

EXTRACAO_TETO_TECNICO_TOKENS = 30_000
MIN_JSON_OUTPUT_TOKENS = 4_096

# Mínimo positivo publicado e se thinking_budget=0 cabe no modelo.
# Flash 2.5 e a família 2.0 seguem o encaixe atual.
_THINKING_MINIMO: tuple[tuple[str, int, bool], ...] = (
    ("gemini-2.5-flash-lite", 512, True),
    ("gemini-2.5-pro", 128, False),
)

# Limites publicados do modelo. A capacidade contabilizável soma saída e thinking
# porque o teto da operação e a reserva da fachada somam os dois.
_MODELOS: tuple[tuple[str, int, int], ...] = (
    ("gemini-2.5-flash-lite", 65_536, 24_576),
    ("gemini-2.5-flash", 65_536, 24_576),
    ("gemini-2.5-pro", 65_536, 32_768),
    ("gemini-2.0-flash-lite", 8_192, 0),
    ("gemini-2.0-flash", 8_192, 0),
)
_SAIDA_CONSERVADORA = 8_192
_THINKING_CONSERVADOR = 8_192


class ReguaExtracaoIndisponivel(Exception):
    """A régua de CleitonCostConfig não converte tokens em créditos."""


@dataclass(frozen=True)
class CapacidadeModelo:
    max_output_tokens: int
    max_thinking_tokens: int

    @property
    def capacidade_contabil(self) -> int:
        return int(self.max_output_tokens) + int(self.max_thinking_tokens)


@dataclass(frozen=True)
class OrcamentoExtracao:
    autorizado: int
    teto_tecnico: int
    tokens_saldo: int | None
    capacidade_modelo: int
    recusado: str | None = None


def capacidade_do_modelo(model: str) -> CapacidadeModelo:
    """Limites do modelo selecionado. Modelo desconhecido fica no teto global."""
    nome = (model or "").strip().lower()
    for prefixo, saida, thinking in _MODELOS:
        if nome == prefixo or nome.startswith(prefixo + "-"):
            return CapacidadeModelo(saida, thinking)
    return CapacidadeModelo(_SAIDA_CONSERVADORA, _THINKING_CONSERVADOR)


def tokens_cobertos_pelo_saldo(saldo: Decimal | None, cfg: Any) -> int | None:
    """
    Maior quantidade de tokens cuja conversão não ultrapassa o saldo.

    None significa franquia sem teto comercial. A taxa vem só da config.
    """
    if saldo is None:
        return None
    rate = getattr(cfg, "credit_tokens_per_credit", None)
    if isinstance(rate, bool) or not isinstance(rate, (int, float)) or rate <= 0:
        raise ReguaExtracaoIndisponivel("regua_indisponivel")
    if saldo <= 0:
        return 0
    rate_d = Decimal(str(rate))
    bruto = int((saldo * rate_d).to_integral_value(rounding=ROUND_DOWN))
    for _ in range(8):
        if bruto <= 0:
            return 0
        creditos, erro = converter_tokens_para_creditos(bruto, cfg)
        if erro or creditos is None:
            raise ReguaExtracaoIndisponivel("regua_indisponivel")
        if creditos <= saldo:
            return bruto
        bruto -= 1
    return 0


def consultar_tokens_saldo(usuario: Any) -> tuple[int | None, str | None]:
    """
    Tokens ainda autorizáveis para a franquia do usuário.

    Sem usuário ou sem contexto, devolve (None, None): a fachada continua
    responsável pela identidade. None no saldo da franquia é ausência de teto.
    """
    if usuario is None or not has_app_context():
        return None, None
    fid = getattr(usuario, "franquia_id", None)
    if fid is None or isinstance(fid, bool):
        return None, None
    try:
        franquia_id = int(fid)
    except (TypeError, ValueError):
        return 0, "franquia_indisponivel"
    franquia = db.session.get(Franquia, franquia_id)
    if franquia is None:
        return 0, "franquia_indisponivel"
    saldo = saldo_disponivel_chamada_ia(franquia)
    try:
        return tokens_cobertos_pelo_saldo(saldo, get_or_create_config()), None
    except ReguaExtracaoIndisponivel:
        return 0, "regua_indisponivel"


def orcamento_autorizado(
    *,
    teto_restante: int,
    modelo: CapacidadeModelo,
    tokens_saldo: int | None,
    recusado: str | None = None,
) -> OrcamentoExtracao:
    """Menor valor entre teto técnico restante, saldo e capacidade do modelo."""
    restante = max(0, min(EXTRACAO_TETO_TECNICO_TOKENS, int(teto_restante)))
    capacidade = int(modelo.capacidade_contabil)
    if recusado:
        autorizado = 0
    else:
        partes = [restante, capacidade]
        if tokens_saldo is not None:
            partes.append(max(0, int(tokens_saldo)))
        autorizado = min(partes)
    return OrcamentoExtracao(
        autorizado=autorizado,
        teto_tecnico=EXTRACAO_TETO_TECNICO_TOKENS,
        tokens_saldo=tokens_saldo,
        capacidade_modelo=capacidade,
        recusado=recusado,
    )


def montar_orcamento_tentativa(*, usuario: Any, model: str, consumido: int) -> OrcamentoExtracao:
    """Orçamento da próxima tentativa, já descontado o consumo técnico anterior."""
    modelo = capacidade_do_modelo(model)
    tokens_saldo, recusado = consultar_tokens_saldo(usuario)
    return orcamento_autorizado(
        teto_restante=EXTRACAO_TETO_TECNICO_TOKENS - max(0, int(consumido)),
        modelo=modelo,
        tokens_saldo=tokens_saldo,
        recusado=recusado,
    )


def tentativa_viavel(autorizado: int, *, min_json: int = MIN_JSON_OUTPUT_TOKENS) -> bool:
    """Há tokens suficientes para uma resposta JSON mesmo com entrada vazia."""
    return isinstance(autorizado, int) and not isinstance(autorizado, bool) and autorizado >= min_json


def _regra_thinking(model: str | None) -> tuple[int, bool] | None:
    """Mínimo positivo e permissão de zero. None preserva o encaixe do modelo."""
    nome = (model or "").strip().lower()
    for prefixo, minimo, zero_permitido in _THINKING_MINIMO:
        if nome == prefixo or nome.startswith(prefixo + "-"):
            return minimo, zero_permitido
    return None


def _thinking_compativel(
    entrada: int,
    saida_final: int,
    thinking_final: int,
    orcamento: int,
    *,
    candidatos: int,
    min_json: int,
) -> bool:
    if saida_final < min_json or thinking_final < 0:
        return False
    if saida_final - thinking_final < min_json:
        return False
    if entrada + (saida_final + thinking_final) * candidatos > orcamento:
        return False
    return True


def encaixar_limites_na_operacao(
    entrada: int,
    saida: int,
    thinking: int,
    orcamento: int,
    *,
    candidatos: int = 1,
    min_json: int = MIN_JSON_OUTPUT_TOKENS,
    model: str | None = None,
) -> tuple[int, int] | None:
    """
    Ajusta saída e thinking para caber no orçamento da tentativa.

    O thinking fica estritamente abaixo da saída reservada ao JSON: no Gemini 2.5
    os tokens de thinking contam dentro de max_output_tokens. A soma contabilizada
    continua entrada + saída + thinking, a mesma da reserva financeira.
    Thinking positivo abaixo do mínimo do modelo não é enviado: Flash-Lite cai
    para zero quando isso cabe; Pro só segue se o mínimo e o JSON couberem.
    """
    if (
        isinstance(entrada, bool)
        or isinstance(saida, bool)
        or isinstance(thinking, bool)
        or isinstance(orcamento, bool)
        or isinstance(candidatos, bool)
    ):
        return None
    if candidatos < 1 or entrada < 0 or orcamento < 1 or saida < 1 or thinking < 0:
        return None
    if entrada >= orcamento:
        return None
    room = (orcamento - entrada) // candidatos
    if room < min_json:
        return None
    folga_json = saida - min_json if saida > min_json else 0
    thinking_teto = min(
        thinking,
        room // 4,
        max(0, (room - min_json) // 2),
        folga_json,
    )
    if thinking_teto < 0:
        thinking_teto = 0
    saida_final = min(saida, room - thinking_teto)
    if saida_final < min_json:
        return None
    if saida_final - thinking_teto < min_json:
        thinking_teto = saida_final - min_json
    if thinking_teto < 0:
        return None
    if saida_final + thinking_teto > room:
        saida_final = room - thinking_teto
    if saida_final < min_json or saida_final - thinking_teto < min_json:
        return None
    if entrada + (saida_final + thinking_teto) * candidatos > orcamento:
        return None
    regra = _regra_thinking(model)
    if regra is None:
        return saida_final, thinking_teto
    minimo, zero_permitido = regra
    if thinking_teto >= minimo or (thinking_teto == 0 and zero_permitido):
        return saida_final, thinking_teto
    if zero_permitido:
        thinking_final = 0
    elif minimo > thinking:
        return None
    else:
        thinking_final = minimo
    saida_final = min(saida, room - thinking_final)
    if not _thinking_compativel(
        entrada,
        saida_final,
        thinking_final,
        orcamento,
        candidatos=candidatos,
        min_json=min_json,
    ):
        return None
    return saida_final, thinking_final


def _inteiro_usage(fonte: Any, *nomes: str) -> int | None:
    for nome in nomes:
        if isinstance(fonte, dict):
            valor = fonte.get(nome)
        else:
            valor = getattr(fonte, nome, None)
        if isinstance(valor, bool) or not isinstance(valor, int) or valor < 0:
            continue
        return valor
    return None


def _fonte_usage(origem: Any) -> Any:
    if origem is None:
        return None
    metadado = getattr(origem, "usage_metadata", None)
    if metadado is None and isinstance(origem, dict):
        metadado = origem.get("usage_metadata") or origem.get("usageMetadata")
    if metadado is not None:
        return metadado
    if any(
        hasattr(origem, nome)
        for nome in ("prompt_token_count", "candidates_token_count", "total_token_count", "thoughts_token_count")
    ):
        return origem
    return None


def _candidatos_consumo(origem: Any) -> list[Any]:
    """A própria origem e, uma vez, response/result. Não soma as duas."""
    fontes = [origem]
    for nome in ("response", "result"):
        if isinstance(origem, dict):
            extra = origem.get(nome)
        else:
            extra = getattr(origem, nome, None)
        if extra is not None and all(extra is not item for item in fontes):
            fontes.append(extra)
    return fontes


def _consumo_da_fonte(fonte: Any) -> int | None:
    prompt = _inteiro_usage(fonte, "prompt_token_count", "promptTokenCount")
    resposta = _inteiro_usage(fonte, "candidates_token_count", "candidatesTokenCount")
    pensamento = _inteiro_usage(fonte, "thoughts_token_count", "thoughtsTokenCount")
    total = _inteiro_usage(fonte, "total_token_count", "totalTokenCount")
    if prompt is not None and resposta is not None:
        soma = prompt + resposta + (pensamento or 0)
        if total is None or total < soma:
            return soma
        return total
    if total is not None:
        return total
    return None


def consumo_tecnico(origem: Any) -> int | None:
    """Entrada + resposta + thinking observados. Não lê texto nem documento."""
    if origem is None:
        return None
    for candidato in _candidatos_consumo(origem):
        fonte = _fonte_usage(candidato)
        if fonte is None:
            continue
        valor = _consumo_da_fonte(fonte)
        if valor is not None:
            return valor
    return None
