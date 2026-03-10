"""
Júlia - Agente Imagem: geração de URL de imagem a partir do prompt (IA ou fallback).
Configurável por GEMINI_MODEL_IMAGE / IMAGE_PROVIDER / IMAGE_API_KEY.
Nunca salvar list/dict bruto em colunas; retornar sempre string (URL) ou None.
"""
import logging
import os
import re
import base64
import uuid
from urllib.parse import quote

logger = logging.getLogger(__name__)

# URL opcional de fallback configurada por ambiente.
# Se ausente, usamos placeholder contextual para manter aderência ao conteúdo.
IMAGEM_FALLBACK_URL = (os.getenv("IMAGEM_FALLBACK_URL", "") or "").strip()


def _get_gemini_timeout_ms() -> int:
    """Timeout HTTP para chamadas Gemini de imagem (ms), com fallback seguro."""
    raw = (
        os.getenv("GEMINI_IMAGE_HTTP_TIMEOUT_MS", "").strip()
        or os.getenv("GEMINI_HTTP_TIMEOUT_MS", "").strip()
    )
    try:
        return max(1_000, int(raw)) if raw else 20_000
    except ValueError:
        return 20_000


def _build_gemini_client(key: str):
    """Cria cliente Gemini com timeout configurável; cai para cliente padrão se necessário."""
    from google import genai
    try:
        from google.genai import types as genai_types
        http_options = genai_types.HttpOptions(timeout=_get_gemini_timeout_ms())
        return genai.Client(api_key=key, http_options=http_options)
    except Exception as e:
        logger.warning("Gemini imagem: falha ao aplicar http timeout, usando cliente padrão: %s", e)
        return genai.Client(api_key=key)


def _get_model_image() -> str:
    return (os.getenv("GEMINI_MODEL_IMAGE", "").strip() or "imagen-3.0-generate-002").strip()


def _get_model_image_fallback() -> str:
    # Modelo Gemini multimodal que pode retornar inline_data (imagem em bytes)
    return (os.getenv("GEMINI_MODEL_IMAGE_FALLBACK", "").strip() or "gemini-2.0-flash-preview-image-generation").strip()


def gerar_url_imagem(prompt_imagem: str) -> str | None:
    """
    Gera url_imagem a partir do prompt. Retorna URL string ou fallback; nunca list/dict.
    Se IMAGE_PROVIDER ou Gemini Imagen não estiver configurado, retorna IMAGEM_FALLBACK_URL.
    """
    prompt_imagem = (prompt_imagem or "").strip()[:500]
    provider = (os.getenv("IMAGE_PROVIDER", "").strip() or "auto").lower()

    if provider in ("gemini", "auto"):
        gemini_url = _gerar_via_gemini(prompt_imagem)
        if gemini_url:
            return gemini_url

    if provider in ("placeholder", "fallback", "auto"):
        return _placeholder_url(prompt_imagem)

    return _fallback_url(prompt_imagem)


def _gerar_via_gemini(prompt_imagem: str) -> str | None:
    """Tenta gerar imagem via Gemini por duas estratégias; em falha, retorna None."""
    key = os.getenv("GEMINI_API_KEY_2") or os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None

    # 1) Imagen endpoint (prioridade)
    url = _gerar_via_gemini_imagen(prompt_imagem, key)
    if url:
        return url

    # 2) Fallback Gemini multimodal (ainda Gemini)
    return _gerar_via_gemini_multimodal(prompt_imagem, key)


def _gerar_via_gemini_imagen(prompt_imagem: str, key: str) -> str | None:
    """Tenta gerar imagem via API Imagen do Gemini."""
    try:
        from google.genai import types
        client = _build_gemini_client(key)
        model = _get_model_image()
        config = getattr(types, "GenerateImagesConfig", None)
        kwargs = {"model": model, "prompt": prompt_imagem or "global supply chain operations", "number_of_images": 1}
        if config:
            kwargs["config"] = config(number_of_images=1)
        response = client.models.generate_images(**kwargs)
        if response and getattr(response, "generated_images", None):
            img = response.generated_images[0]
            if getattr(img, "url", None):
                return str(img.url)
            raw_bytes = _extrair_bytes_imagem(img)
            if raw_bytes:
                local_url = _salvar_imagem_local(raw_bytes)
                if local_url:
                    return local_url
        return None
    except Exception as e:
        logger.warning("Imagen indisponivel (%s): %s", _get_model_image(), e)
        return None


def _gerar_via_gemini_multimodal(prompt_imagem: str, key: str) -> str | None:
    """Fallback Gemini multimodal para extrair inline_data de imagem em bytes."""
    try:
        client = _build_gemini_client(key)
        model = _get_model_image_fallback()
        prompt_final = (
            "Create a realistic editorial illustration, no text overlay, no watermark, "
            "high detail, cinematic lighting, logistics/supply chain context: "
            f"{prompt_imagem or 'global supply chain operations'}"
        )
        response = client.models.generate_content(model=model, contents=prompt_final)
        raw_bytes = _extrair_bytes_response_multimodal(response)
        if raw_bytes:
            return _salvar_imagem_local(raw_bytes)
        return None
    except Exception as e:
        logger.warning("Gemini multimodal imagem indisponivel (%s): %s", _get_model_image_fallback(), e)
        return None


def _placeholder_url(prompt_imagem: str) -> str:
    """Gera URL placeholder codificando o texto (para testes/demo)."""
    texto = _texto_placeholder(prompt_imagem)
    t = re.sub(r"\s+", "+", texto[:80])
    return f"https://placehold.co/800x450/0d6efd/fff?text={quote(t)}"


def _texto_placeholder(prompt_imagem: str) -> str:
    base = (prompt_imagem or "").strip()
    if not base:
        return "Supply chain strategic insight"
    # Texto limpo e curto para URL de placeholder.
    base = re.sub(r"\s+", " ", base)
    return base[:80]


def _fallback_url(prompt_imagem: str) -> str:
    if IMAGEM_FALLBACK_URL:
        return IMAGEM_FALLBACK_URL
    foto = _stock_image_url(prompt_imagem)
    if foto:
        return foto
    return _placeholder_url(prompt_imagem)


def _stock_image_url(prompt_imagem: str) -> str | None:
    """Fallback visual sem texto azul: usa serviço de foto temática quando IA falha."""
    try:
        base = (prompt_imagem or "logistics supply chain control tower").lower()
        termos = []
        if "oil" in base or "petrole" in base or "energia" in base:
            termos.extend(["oil", "logistics", "port"])
        if "geopolit" in base or "middle east" in base or "oriente" in base:
            termos.extend(["cargo", "shipping", "trade"])
        if not termos:
            termos.extend(["logistics", "supplychain", "warehouse"])
        query = ",".join(termos[:3])
        return f"https://loremflickr.com/1200/675/{query}/all"
    except Exception:
        return None


def _extrair_bytes_imagem(img) -> bytes | None:
    """Extrai bytes da imagem do objeto retornado pela SDK, suportando variações de payload."""
    try:
        if hasattr(img, "image") and hasattr(img.image, "image_bytes") and img.image.image_bytes:
            return img.image.image_bytes
        if hasattr(img, "image_bytes") and getattr(img, "image_bytes"):
            return img.image_bytes
        if isinstance(img, dict):
            nested = img.get("image") if isinstance(img.get("image"), dict) else None
            if nested and nested.get("image_bytes"):
                return nested.get("image_bytes")
            if img.get("image_bytes"):
                return img.get("image_bytes")
            if img.get("b64_json"):
                return base64.b64decode(img.get("b64_json"))
    except Exception:
        return None
    return None


def _salvar_imagem_local(raw_bytes: bytes) -> str | None:
    """Salva imagem gerada localmente em static/generated e retorna URL pública do Flask."""
    if not raw_bytes:
        return None
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        out_dir = os.path.join(app_dir, "static", "generated")
        os.makedirs(out_dir, exist_ok=True)
        nome = f"julia_{uuid.uuid4().hex}.png"
        out_path = os.path.join(out_dir, nome)
        with open(out_path, "wb") as f:
            f.write(raw_bytes)
        return f"/static/generated/{nome}"
    except Exception as e:
        logger.warning("Falha ao salvar imagem local gerada pela IA: %s", e)
        return None


def _extrair_bytes_response_multimodal(response) -> bytes | None:
    """Extrai bytes de imagem de respostas multimodais (inline_data) do Gemini."""
    try:
        candidates = getattr(response, "candidates", None) or []
        for cand in candidates:
            content = getattr(cand, "content", None)
            parts = getattr(content, "parts", None) or []
            for part in parts:
                inline = getattr(part, "inline_data", None)
                if not inline:
                    continue
                data = getattr(inline, "data", None)
                if not data:
                    continue
                if isinstance(data, bytes):
                    return data
                if isinstance(data, str):
                    return base64.b64decode(data)
    except Exception:
        return None
    return None


def normalizar_url_imagem(valor) -> str | None:
    """
    Garante que valor seja string URL ou None; nunca list/dict para não quebrar templates.
    """
    if valor is None:
        return None
    if isinstance(valor, str):
        return valor.strip() or None
    if isinstance(valor, (list, dict)):
        logger.warning("url_imagem veio como list/dict; ignorando para evitar quebra de template.")
        return None
    return str(valor).strip() or None
