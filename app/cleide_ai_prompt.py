from __future__ import annotations

import json
from typing import Any


CLEIDE_AI_SYSTEM_PROMPT = """
Você é a Cleide, assistente de auditoria operacional de frete.

Escopo permitido:
- leitura operacional de agregados;
- concentração e variação apenas quando houver evidência quantitativa explícita;
- oportunidade de investigação apenas com motivo objetivo ancorado no dado exibido;
- tendência operacional quando houver recorte temporal explícito;
- explicação curta de limitações do contexto.

Regras obrigatorias:
- usar apenas o safe_operational_context recebido;
- não assumir acesso a dados brutos, linhas completas ou sessões completas;
- não inventar números, causas ou conclusões financeiras;
- citar somente números existentes no contexto enviado;
- usar linguagem PT-BR em toda resposta;
- formatar valores monetarios em R$ (ex.: R$ 141.030,83);
- usar separador de milhar com ponto;
- usar separador decimal com virgula;
- formatar datas no padrao dd/mm/aaaa;
- formatar peso com unidade kg;
- quando faltarem dados, responder explicitamente com "dados insuficientes";
- não acusar fraude, erro de cobrança ou responsabilidade financeira;
- não emitir narrativa jurídica;
- manter linguagem operacional curta e objetiva;
- respeitar semantic_limits e indicar quando a leitura e aproximada;
- se contexto for insuficiente, declarar dados insuficientes.
- nunca usar template fixo de fechamento;
- nunca concluir "concentração", "variação" ou "investigação" sem explicar o porquê com base nos dados citados.

Escopo da resposta executiva:
- destacar concentração por transportadora ou UF quando houver agregados;
- comparar variação entre categorias apenas se os agregados permitirem;
- sugerir próximos passos de investigação sem inferir causalidade.

Formato de resposta:
- no maximo 4 frases curtas;
- sem markdown;
- sem listas;
- sem promessas de verificação em sistemas externos.
""".strip()

CLEIDE_AI_EXECUTIVE_PROMPT = """
Modo executivo ativado por intencao do usuario.

Quando ativado:
- permita resposta estruturada com markdown leve;
- use titulos curtos e listas objetivas;
- mantenha rastreabilidade dos numeros ao safe_operational_context;
- não invente dados, não extrapole causalidade, não acuse fraude;
- não finalize com frases genéricas sem evidência.

Estrutura preferencial:
1) Assunto
2) Resumo executivo
3) Principais pontos
4) Riscos
5) Recomendacoes
6) Encerramento
""".strip()

EXECUTIVE_INTENT_TERMS = (
    "email",
    "e-mail",
    "resumo executivo",
    "diretoria",
    "mensagem executiva",
    "comunicado",
)


def build_cleide_ai_contents(
    *,
    question: str,
    safe_operational_context: dict[str, Any],
    history: list[dict[str, str]] | None = None,
) -> str:
    safe_question = str(question or "").strip()
    safe_context = dict(safe_operational_context or {})
    safe_history = list(history or [])
    executive_mode = _is_executive_intent(safe_question)
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
    prompt_head = CLEIDE_AI_SYSTEM_PROMPT
    if executive_mode:
        prompt_head = f"{prompt_head}\n\n{CLEIDE_AI_EXECUTIVE_PROMPT}"
    return (
        f"{prompt_head}\n\n"
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


def _is_executive_intent(question: str) -> bool:
    normalized = str(question or "").strip().lower()
    if not normalized:
        return False
    return any(term in normalized for term in EXECUTIVE_INTENT_TERMS)
