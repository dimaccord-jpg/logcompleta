"""
Júlia - Agente Redação: geração de texto (notícia curta ou artigo completo) via Gemini.
Modelo configurável por GEMINI_MODEL_TEXT. Saída normalizada para o pipeline (sem list/dict em colunas).
"""
import json
import logging
import os
import re
from google import genai
from google.genai import types as genai_types
from app.prompts import PERSONA, GERAR_NOTICIA_CURTA, GERAR_ARTIGO_COMPLETO
from app.run_cleiton_gemini_governance import cleiton_governed_generate_content

logger = logging.getLogger(__name__)


def _api_key_label_for_tipo(tipo: str) -> str:
    if (tipo or "").lower() == "artigo":
        if os.getenv("GEMINI_API_KEY_2"):
            return "GEMINI_API_KEY_2"
        if os.getenv("GEMINI_API_KEY"):
            return "GEMINI_API_KEY"
    else:
        if os.getenv("GEMINI_API_KEY_1"):
            return "GEMINI_API_KEY_1"
        if os.getenv("GEMINI_API_KEY"):
            return "GEMINI_API_KEY"
    return "unknown"

# Modelos textuais em ordem de fallback
def _get_model_text_candidates() -> list[str]:
    candidates = [
        os.getenv("GEMINI_MODEL_TEXT", "").strip(),
        "gemini-2.5-flash",
        "gemini-1.5-flash",
    ]
    # Remove vazios e duplicados mantendo ordem
    seen = set()
    ordered = []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            ordered.append(c)
    return ordered


def _client_for_tipo(tipo: str):
    """
    Retorna cliente Gemini: artigos usam GEMINI_API_KEY_2, notícias GEMINI_API_KEY_1.
    Usa http_options com timeout configurável para evitar travar o worker em chamadas externas.
    """
    key = os.getenv("GEMINI_API_KEY_2") if tipo == "artigo" else os.getenv("GEMINI_API_KEY_1")
    if not key:
        key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        timeout_ms_env = os.getenv("GEMINI_HTTP_TIMEOUT_MS", "").strip()
        try:
            timeout_ms = max(1_000, int(timeout_ms_env)) if timeout_ms_env else 20_000
        except ValueError:
            timeout_ms = 20_000
        http_options = genai_types.HttpOptions(timeout=timeout_ms)
        return genai.Client(api_key=key, http_options=http_options)
    except Exception as e:
        logger.error("Falha ao inicializar cliente Gemini (%s): %s", tipo, e)
        return None


def gerar_noticia_curta(titulo_original: str, fonte: str, link: str) -> dict | None:
    """
    Gera notícia no padrão executivo curto.
    Retorna dict com: titulo_julia, resumo_julia (insight_curto 3-5 linhas), prompt_imagem.
    """
    client = _client_for_tipo("noticia")
    if not client:
        logger.error("Nenhuma chave Gemini configurada para notícias. Usando fallback local de redação.")
        return _fallback_noticia_curta(titulo_original, fonte, link)
    instrucao = GERAR_NOTICIA_CURTA.format(
        titulo_original=titulo_original,
        fonte=fonte,
        link=link,
    )
    prompt = f"{PERSONA}\n{instrucao}"
    data = _chamar_modelo(client, prompt, "noticia")
    if not data:
        logger.warning("Redação de notícia retornou vazio/inválido. Usando fallback local de redação.")
        return _fallback_noticia_curta(titulo_original, fonte, link)
    data["resumo_julia"] = _garantir_insight_3_5_linhas(data.get("resumo_julia", ""))
    return data


def gerar_artigo_completo(titulo_original: str, fonte: str, link: str) -> dict | None:
    """
    Gera artigo no padrão estendido (marketing + lead).
    Retorna dict com: titulo_julia, subtitulo, resumo_julia, conteudo_completo (HTML seguro),
    prompt_imagem, cta, objetivo_lead (ex.: newsletter, diagnóstico, contato comercial).
    """
    client = _client_for_tipo("artigo")
    if not client:
        logger.error("Nenhuma chave Gemini configurada para artigos. Usando fallback local de redação.")
        return _fallback_artigo_completo(titulo_original, fonte, link)
    instrucao = GERAR_ARTIGO_COMPLETO.format(
        titulo_original=titulo_original,
        fonte=fonte,
        link=link,
    )
    prompt = f"{PERSONA}\n{instrucao}"
    data = _chamar_modelo(client, prompt, "artigo")
    if not data:
        logger.warning("Redação de artigo retornou vazio/inválido. Usando fallback local de redação.")
        return _fallback_artigo_completo(titulo_original, fonte, link)
    return data


def _chamar_modelo(client, prompt: str, tipo: str) -> dict | None:
    last_error = None
    flow = "julia_redacao_artigo" if (tipo or "").lower() == "artigo" else "julia_redacao_noticia"
    label = _api_key_label_for_tipo(tipo)
    for model in _get_model_text_candidates():
        try:
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=prompt,
                agent="julia",
                flow_type=flow,
                api_key_label=label,
            )
            txt = (response.text or "").strip()
            inicio = txt.find("{")
            fim = txt.rfind("}") + 1
            if inicio < 0 or fim <= inicio:
                raise ValueError("Resposta sem JSON válido")
            return json.loads(txt[inicio:fim])
        except Exception as e:
            last_error = e
            logger.warning("Modelo textual indisponível (%s) para %s: %s", model, tipo, e)
    if last_error is not None:
        logger.exception("Falha na redação Júlia (%s) após fallback: %s", tipo, last_error)
    else:
        logger.error("Falha na redação Júlia (%s): sem erro detalhado retornado pelo cliente Gemini.", tipo)
    return None


def _garantir_insight_3_5_linhas(texto: str) -> str:
    """Normaliza insight para 3-5 linhas executivas, preservando objetividade."""
    if not texto:
        return ""
    limpo = _limpar_marcacao_markdown(str(texto))
    limpo = re.sub(r"\s+", " ", limpo).strip()
    if not limpo:
        return ""

    frases = [f.strip() for f in re.split(r"(?<=[.!?])\s+", limpo) if f.strip()]

    linhas = []
    if len(frases) >= 3:
        linhas = frases[:5]
    else:
        # Fallback por blocos de tamanho, garantindo entre 3 e 5 linhas.
        chunk_size = max(70, len(limpo) // 3)
        for i in range(0, len(limpo), chunk_size):
            parte = limpo[i:i + chunk_size].strip()
            if parte:
                linhas.append(parte)
            if len(linhas) >= 5:
                break
        if len(linhas) < 3:
            while len(linhas) < 3:
                linhas.append(linhas[-1] if linhas else limpo)

    return "\n".join(linhas[:5])


def _limpar_marcacao_markdown(texto: str) -> str:
    """Remove marcações markdown comuns para evitar exibição de asteriscos no insight."""
    if not texto:
        return ""
    out = str(texto)
    out = re.sub(r"\*{1,2}([^*\n]+)\*{1,2}", r"\1", out)
    out = out.replace("`", "")
    out = out.replace("*", "")
    return out


def gerar_conteudo(pauta_titulo: str, pauta_fonte: str, pauta_link: str, tipo_missao: str) -> dict | None:
    """
    Entrada única do agente de redação: delega para notícia curta ou artigo completo.
    tipo_missao: 'noticia' | 'artigo'
    """
    if (tipo_missao or "").lower() == "artigo":
        return gerar_artigo_completo(pauta_titulo, pauta_fonte, pauta_link)
    return gerar_noticia_curta(pauta_titulo, pauta_fonte, pauta_link)


def _fallback_noticia_curta(titulo_original: str, fonte: str, link: str) -> dict:
    """Fallback determinístico para notícia curta quando o provedor de IA falhar."""
    titulo_base = (titulo_original or "Atualização logística").strip()
    titulo_base = re.sub(r"\s+", " ", titulo_base)
    if len(titulo_base) > 110:
        titulo_base = titulo_base[:107].rstrip() + "..."

    resumo = (
        "O movimento reportado reforça atenção imediata sobre custo, prazo e previsibilidade operacional. "
        "A recomendação é validar impacto por rota e priorizar ajustes de capacidade nas próximas janelas de decisão. "
        "Com acompanhamento diário de indicadores, o time reduz risco de ruptura e melhora o nível de serviço."
    )
    return {
        "titulo_julia": titulo_base,
        "resumo_julia": _garantir_insight_3_5_linhas(resumo),
        "prompt_imagem": "Modern logistics control tower, containers, trucks and data dashboards, realistic photo style",
    }


def _fallback_artigo_completo(titulo_original: str, fonte: str, link: str) -> dict:
    """Fallback determinístico para artigo completo quando o provedor de IA falhar."""
    titulo_base = (titulo_original or "Estratégia logística para ganho operacional").strip()
    titulo_base = re.sub(r"\s+", " ", titulo_base)
    if len(titulo_base) > 118:
        titulo_base = titulo_base[:115].rstrip() + "..."

    subtitulo = "Como transformar variações do mercado em decisões operacionais mais previsíveis e rentáveis."
    resumo = _garantir_insight_3_5_linhas(
        "A pauta indica uma oportunidade de revisão tática com impacto direto em custo e nível de serviço. "
        "O caminho mais seguro é combinar leitura de cenário, priorização de rotas críticas e revisão de capacidade. "
        "Com governança simples e cadência semanal, a operação tende a ganhar previsibilidade e margem."
    )

    fonte_txt = (fonte or "Fonte informada").strip()
    link_txt = (link or "").strip()
    conteudo = (
        "<p><strong>Contexto executivo.</strong> A pauta aponta um cenário que exige resposta coordenada entre planejamento e operação. "
        "Quando custo, prazo e disponibilidade variam ao mesmo tempo, a decisão mais eficiente depende de priorização objetiva e dados de execução confiáveis.</p>"
        "<p><strong>Leitura de impacto.</strong> O primeiro passo é mapear onde a variação afeta receita e nível de serviço. "
        "Em geral, rotas com maior concentração de volume e maior sensibilidade a atraso devem receber prioridade de ajuste no curto prazo.</p>"
        "<p><strong>Plano de ação.</strong> Recomenda-se revisar capacidade por janela, atualizar parâmetros de alocação e definir gatilhos de contingência. "
        "A rotina deve incluir ritos curtos de acompanhamento, com métricas de cumprimento de prazo, custo por operação e taxa de retrabalho.</p>"
        f"<p><strong>Referência operacional.</strong> Fonte analisada: {fonte_txt}. "
        f"Link original: {link_txt}. "
        "A execução disciplinada dessas ações tende a elevar previsibilidade, reduzir desperdícios e sustentar crescimento com risco controlado.</p>"
    )

    return {
        "titulo_julia": titulo_base,
        "subtitulo": subtitulo,
        "resumo_julia": resumo,
        "conteudo_completo": conteudo,
        "prompt_imagem": "Executive logistics strategy meeting with digital supply chain dashboard, realistic corporate style",
        "cta": "Fale com um especialista e receba um plano prático para aumentar previsibilidade operacional.",
        "objetivo_lead": "contato_comercial",
        "referencias": f"Fonte: {fonte_txt} | Link: {link_txt}",
    }
