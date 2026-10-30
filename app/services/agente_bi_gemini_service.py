"""Adaptador fino do Gemini para o AgenteBI.

A geração passa pela fachada faturável oficial. O SDK não é chamado daqui
quando o chamador entrega um cliente. Contagem, admissão, liquidação e
falha permanecem nas estruturas oficiais. Não há teto novo de token.
"""
from __future__ import annotations

import logging
import os
from typing import Any

from flask import has_app_context

from app.extensions import db
from app.models import AgenteBiExecucao, IaChamadaTentativa, User
from app.services.agente_bi_anthropic_service import (
    _liberar_efeito,
    _segurar_efeito,
    _vincular_execucao,
)
from app.services.cleiton_ai_data_governance import PURPOSE_BI_LOGISTICO
from app.services.cleiton_billable_ai_call import (
    BillableAiAdmissionBlocked,
    BillableAiGovernanceBlocked,
    BillableAiUncertainError,
    cleiton_governed_billable_ai_call,
)

logger = logging.getLogger(__name__)

PROVIDER = "gemini"
AGENT = "agente_bi"
FLOW_TYPE = PURPOSE_BI_LOGISTICO
API_KEY_LABEL = "AGENTE_BI_GEMINI"
FLAG_MODELO = "AGENTE_BI_GEMINI_MODEL"
TIMEOUT_MS = 30000


def montar_cliente_gemini() -> Any:
    """Cliente oficial. Testes substituem esta função e não chegam ao SDK."""
    chave = (os.getenv("GEMINI_API_KEY") or os.getenv("GEMINI_API_KEY_1") or "").strip()
    if not chave:
        return None
    from google import genai

    from app.services.cleiton_billable_ai_call import gemini_http_options

    return genai.Client(api_key=chave, http_options=gemini_http_options(timeout_ms=TIMEOUT_MS))


def chamar_gemini_governado(
    *,
    usuario_id: int,
    conta_id: int,
    franquia_id: int,
    mensagem: str,
    chave_idempotencia: str,
    execucao_id: int,
    worker_id: str,
    fencing: int,
    cliente=None,
    config: Any = None,
) -> dict[str, Any]:
    """Uma tentativa. A mesma chave não dispara outra geração."""
    if not has_app_context():
        return _saida("sem_contexto", chamado=False)
    if not isinstance(mensagem, str) or not mensagem.strip():
        return _saida("mensagem_ausente", chamado=False)
    modelo = (os.getenv(FLAG_MODELO) or "").strip()
    if not modelo or len(modelo) > 255:
        return _saida("modelo_ausente", chamado=False)
    usuario = db.session.get(User, int(usuario_id))
    if usuario is None:
        return _saida("identidade_incoerente", chamado=False)
    envio = cliente if cliente is not None else montar_cliente_gemini()
    if envio is None:
        return _saida("credencial_ausente", chamado=False)
    ident = {
        "usuario_id": int(usuario_id),
        "conta_id": int(conta_id),
        "franquia_id": int(franquia_id),
    }
    if not _segurar_efeito(int(execucao_id), ident, worker_id, fencing):
        return _saida("efeito_incerto", chamado=False, incerto=True)
    try:
        resultado = cleiton_governed_billable_ai_call(
            envio,
            model=modelo,
            contents=mensagem,
            config=config,
            agent=AGENT,
            flow_type=FLOW_TYPE,
            api_key_label=API_KEY_LABEL,
            provider=PROVIDER,
            usuario=usuario,
            conta_id=int(conta_id),
            franquia_id=int(franquia_id),
            usuario_id=int(usuario_id),
            origem_sistema=False,
            attempt_key=chave_idempotencia,
            purpose=PURPOSE_BI_LOGISTICO,
        )
    except BillableAiAdmissionBlocked:
        tentativa_id = _vincular_tentativa(chave_idempotencia, execucao_id, ident, worker_id, fencing)
        _liberar_efeito(int(execucao_id), ident, worker_id, fencing)
        return _saida("admissao_recusada", chamado=False, tentativa_id=tentativa_id)
    except BillableAiGovernanceBlocked:
        tentativa_id = _vincular_tentativa(chave_idempotencia, execucao_id, ident, worker_id, fencing)
        _liberar_efeito(int(execucao_id), ident, worker_id, fencing)
        return _saida("contexto_nao_autorizado", chamado=False, tentativa_id=tentativa_id)
    except BillableAiUncertainError:
        tentativa_id = _vincular_tentativa(chave_idempotencia, execucao_id, ident, worker_id, fencing)
        return _saida("efeito_incerto", chamado=True, incerto=True, tentativa_id=tentativa_id)
    except Exception:
        tentativa_id = _vincular_tentativa(chave_idempotencia, execucao_id, ident, worker_id, fencing)
        logger.info(
            "evento=agente_bi_gemini_falha execucao_id=%s tentativa_id=%s",
            execucao_id,
            tentativa_id,
        )
        return _saida("efeito_incerto", chamado=True, incerto=True, tentativa_id=tentativa_id)
    tentativa_id = getattr(resultado, "attempt_id", None)
    if not _vincular_id(tentativa_id, execucao_id, ident, worker_id, fencing):
        return _saida("efeito_incerto", chamado=True, incerto=True, tentativa_id=tentativa_id)
    if not getattr(resultado, "complete", False):
        return _saida("efeito_incerto", chamado=True, incerto=True, tentativa_id=tentativa_id)
    texto = _texto(getattr(resultado, "response", None))
    logger.info(
        "evento=agente_bi_gemini codigo=ok execucao_id=%s tentativa_id=%s",
        execucao_id,
        tentativa_id,
    )
    return _saida("ok", ok=True, chamado=True, texto=texto, tentativa_id=tentativa_id)


def _vincular_tentativa(chave: str, execucao_id: int, ident: dict, worker_id: str, fencing: int):
    tentativa = db.session.query(IaChamadaTentativa).filter_by(attempt_key=chave).one_or_none()
    if tentativa is None:
        return None
    if not _vincular_id(tentativa.id, execucao_id, ident, worker_id, fencing):
        return int(tentativa.id)
    return int(tentativa.id)


def _vincular_id(tentativa_id, execucao_id: int, ident: dict, worker_id: str, fencing: int) -> bool:
    if isinstance(tentativa_id, bool) or not isinstance(tentativa_id, int):
        return False
    if not _vincular_execucao(int(execucao_id), int(tentativa_id), ident, worker_id, fencing):
        atual = db.session.get(IaChamadaTentativa, int(tentativa_id))
        linha = db.session.get(AgenteBiExecucao, int(execucao_id))
        if linha is None or atual is None:
            return False
        db.session.refresh(linha)
        return linha.ia_chamada_tentativa_id == int(tentativa_id)
    db.session.commit()
    return True


def _texto(resposta) -> str | None:
    if resposta is None:
        return None
    texto = resposta.get("text") if isinstance(resposta, dict) else getattr(resposta, "text", None)
    if isinstance(texto, str):
        return texto
    return None


def _saida(
    codigo: str,
    *,
    chamado: bool,
    ok: bool = False,
    incerto: bool = False,
    texto: str | None = None,
    tentativa_id: int | None = None,
) -> dict[str, Any]:
    return {
        "ok": ok,
        "codigo": codigo,
        "incerto": incerto,
        "provedor": PROVIDER,
        "provedor_chamado": chamado,
        "texto": texto,
        "tentativa_id": None if tentativa_id is None else int(tentativa_id),
    }
