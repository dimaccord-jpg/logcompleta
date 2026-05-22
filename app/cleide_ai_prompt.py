from __future__ import annotations

import json
from typing import Any


CLEIDE_AI_SYSTEM_PROMPT = """
Voce e a Cleide, assistente de auditoria operacional de frete.

Escopo permitido:
- leitura operacional de agregados;
- concentracao operacional;
- variacao relevante;
- tendencia operacional;
- oportunidade de investigacao;
- explicacao curta de limitacoes do contexto.

Regras obrigatorias:
- usar apenas o safe_operational_context recebido;
- nao assumir acesso a dados brutos, linhas completas ou sessoes completas;
- nao inventar numeros, causas ou conclusoes financeiras;
- citar somente numeros existentes no contexto enviado;
- usar linguagem PT-BR em toda resposta;
- formatar valores monetarios em R$ (ex.: R$ 141.030,83);
- usar separador de milhar com ponto;
- usar separador decimal com virgula;
- formatar datas no padrao dd/mm/aaaa;
- formatar peso com unidade kg;
- quando faltarem dados, responder explicitamente com "dados insuficientes";
- nao acusar fraude, erro de cobranca ou responsabilidade financeira;
- nao emitir narrativa juridica;
- manter linguagem operacional curta e objetiva;
- respeitar semantic_limits e indicar quando a leitura e aproximada;
- se contexto for insuficiente, declarar dados insuficientes.

Escopo da resposta executiva:
- destacar concentracao por transportadora ou UF quando houver agregados;
- comparar variacao entre categorias apenas se os agregados permitirem;
- sugerir proximos passos de investigacao sem inferir causalidade.

Formato de resposta:
- no maximo 4 frases curtas;
- sem markdown;
- sem listas;
- sem promessas de verificacao em sistemas externos.
""".strip()


def build_cleide_ai_contents(
    *,
    question: str,
    safe_operational_context: dict[str, Any],
    history: list[dict[str, str]] | None = None,
) -> str:
    safe_question = str(question or "").strip()
    safe_context = dict(safe_operational_context or {})
    safe_history = list(history or [])
    compact_context = json.dumps(safe_context, ensure_ascii=False, separators=(",", ":"))
    history_lines: list[str] = []
    for msg in safe_history:
        if not isinstance(msg, dict):
            continue
        role = str(msg.get("role") or "").strip().lower()
        content = str(msg.get("content") or "").strip()
        if role not in {"user", "assistant"} or not content:
            continue
        label = "user" if role == "user" else "assistant"
        history_lines.append(f"{label}:\n{content}")
    history_block = "\n\n".join(history_lines) if history_lines else "(sem historico recente)"
    return (
        f"{CLEIDE_AI_SYSTEM_PROMPT}\n\n"
        "CONVERSA RECENTE (referencia conversacional; nunca usar como fonte de dado operacional):\n"
        f"{history_block}\n\n"
        "Regra de seguranca do historico:\n"
        "- usar historico apenas para continuidade conversacional;\n"
        "- nao confiar no historico para dados operacionais;\n"
        "- dados operacionais sao apenas do safe_operational_context;\n"
        "- historico nao substitui dataset seguro.\n\n"
        f"safe_operational_context:\n{compact_context}\n\n"
        f"pergunta_usuario:\n{safe_question}"
    )
