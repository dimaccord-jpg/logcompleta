"""
Júlia - Agente Redação: geração de texto (notícia curta ou artigo completo) via Gemini.
Modelo configurável por GEMINI_MODEL_TEXT. Saída normalizada para o pipeline (sem list/dict em colunas).
"""
import json
import logging
import os
import re
from google import genai

logger = logging.getLogger(__name__)

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
    """Retorna cliente Gemini: artigos usam GEMINI_API_KEY_2, notícias GEMINI_API_KEY_1."""
    key = os.getenv("GEMINI_API_KEY_2") if tipo == "artigo" else os.getenv("GEMINI_API_KEY_1")
    if not key:
        key = os.getenv("GEMINI_API_KEY")
    if not key:
        return None
    try:
        return genai.Client(api_key=key)
    except Exception as e:
        logger.error("Falha ao inicializar cliente Gemini (%s): %s", tipo, e)
        return None


PERSONA = """
Você é Júlia, Editora-Chefe de 32 anos, executiva premium e assertiva.
Tom elegante, profissional e estratégico. Foco em eficiência real e resultados.
Use termos técnicos de Logística 4.0 corretamente. Evite linguagem genérica ou agressiva.
Seja realista e orientada a dor/oportunidade/plano de ação (B2B logística/supply chain).
"""


def gerar_noticia_curta(titulo_original: str, fonte: str, link: str) -> dict | None:
    """
    Gera notícia no padrão executivo curto.
    Retorna dict com: titulo_julia, resumo_julia (insight_curto 3-5 linhas), prompt_imagem.
    """
    client = _client_for_tipo("noticia")
    if not client:
        logger.error("Nenhuma chave Gemini configurada para notícias.")
        return None
    instrucao = f'''
TAREFA: Transforme esta pauta em NOTÍCIA CURTA no padrão executivo.

PAUTA: "{titulo_original}"
FONTE: {fonte}
LINK ORIGINAL: {link}

REQUISITOS:
1. titulo_julia: título refraseado, premium e direto (máx. 120 caracteres).
2. resumo_julia (insight_curto): 3 a 5 linhas destacando impacto prático (risco/oportunidade), objetivo e acionável. Sem texto genérico.
3. prompt_imagem: uma frase em inglês para gerar imagem IA (logística/supply chain, cenário realista).

Retorne APENAS um JSON válido com as chaves: "titulo_julia", "resumo_julia", "prompt_imagem".
'''
    prompt = f"{PERSONA}\n{instrucao}"
    data = _chamar_modelo(client, prompt, "noticia")
    if not data:
        return None
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
        logger.error("Nenhuma chave Gemini configurada para artigos.")
        return None
    instrucao = f'''
TAREFA: Escreva um ARTIGO ESTRATÉGICO PREMIUM com foco em lead generation.

PAUTA: "{titulo_original}"
FONTE: {fonte}
LINK ORIGINAL: {link}

REQUISITOS:
1. titulo_julia: premium, forte, alta gestão (máx. 120 caracteres).
2. subtitulo: uma frase de impacto que resuma a oportunidade estratégica.
3. resumo_julia: insight executivo de 3 a 5 linhas para o card.
4. conteudo_completo: texto técnico e fluido (mínimo 4 parágrafos) em HTML. Use APENAS <p>, <strong>, <ul>, <li>. Sem scripts ou estilos inline.
5. prompt_imagem: frase em inglês para IA gerar imagem realista de logística high-tech.
6. cta: chamada para ação explícita e profissional (ex.: "Receba o diagnóstico gratuito", "Fale com um especialista").
7. objetivo_lead: um de: "newsletter", "diagnóstico", "contato_comercial", "material_rico".
8. referencias: links e fontes de insights (texto curto).

Retorne APENAS um JSON válido com as chaves: "titulo_julia", "subtitulo", "resumo_julia", "conteudo_completo", "prompt_imagem", "cta", "objetivo_lead", "referencias".
'''
    prompt = f"{PERSONA}\n{instrucao}"
    return _chamar_modelo(client, prompt, "artigo")


def _chamar_modelo(client, prompt: str, tipo: str) -> dict | None:
    last_error = None
    for model in _get_model_text_candidates():
        try:
            response = client.models.generate_content(model=model, contents=prompt)
            txt = (response.text or "").strip()
            inicio = txt.find("{")
            fim = txt.rfind("}") + 1
            if inicio < 0 or fim <= inicio:
                raise ValueError("Resposta sem JSON válido")
            return json.loads(txt[inicio:fim])
        except Exception as e:
            last_error = e
            logger.warning("Modelo textual indisponível (%s) para %s: %s", model, tipo, e)
    logger.exception("Falha na redação Júlia (%s) após fallback: %s", tipo, last_error)
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
