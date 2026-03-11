"""
Júlia - Agente Imagem: geração de URL de imagem a partir do prompt (IA ou fallback).
Configurável por GEMINI_MODEL_IMAGE / IMAGE_PROVIDER / IMAGE_API_KEY.
Nunca salvar list/dict bruto em colunas; retornar sempre string (URL) ou None.
Fallback padrão prioriza asset estático versionado em /static/img para manter consistência visual.
"""
import logging
import os
import re
import base64
import uuid
import time
import hashlib
from urllib.request import Request, urlopen
from urllib.parse import quote

logger = logging.getLogger(__name__)

# URL opcional de fallback configurada por ambiente.
# Se ausente, usamos placeholder contextual para manter aderência ao conteúdo.
IMAGEM_FALLBACK_URL = (os.getenv("IMAGEM_FALLBACK_URL", "") or "").strip()
FALLBACK_ASSET_LOCAL = "/static/img/fallback-capa-v1.svg"


def _allow_remote_fallback() -> bool:
    """Permite fallback remoto apenas quando explicitamente habilitado por ambiente."""
    return (os.getenv("IMAGE_ALLOW_REMOTE_FALLBACK", "false") or "false").strip().lower() in (
        "1", "true", "t", "yes"
    )


def _image_retry_attempts() -> int:
    """Quantidade de tentativas para chamadas de geração de imagem."""
    raw = (os.getenv("IMAGE_RETRY_ATTEMPTS", "3") or "3").strip()
    try:
        return max(1, min(5, int(raw)))
    except ValueError:
        return 3


def _image_retry_backoff_ms() -> int:
    """Backoff base (ms) entre tentativas de geração de imagem."""
    raw = (os.getenv("IMAGE_RETRY_BACKOFF_MS", "800") or "800").strip()
    try:
        return max(100, min(5_000, int(raw)))
    except ValueError:
        return 800


def _stock_fallback_enabled() -> bool:
    """Habilita fallback fotográfico contextual salvo localmente quando IA falhar."""
    return (os.getenv("IMAGE_STOCK_FALLBACK_ENABLED", "true") or "true").strip().lower() in (
        "1", "true", "t", "yes"
    )


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
    # Modelo Gemini multimodal opcional que pode retornar inline_data (imagem em bytes)
    return (os.getenv("GEMINI_MODEL_IMAGE_FALLBACK", "").strip() or "").strip()


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

    if provider == "placeholder":
        return _placeholder_url(prompt_imagem)

    if provider in ("fallback", "auto", "gemini"):
        return _fallback_url(prompt_imagem)

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
    tentativas = _image_retry_attempts()
    backoff_ms = _image_retry_backoff_ms()
    for tentativa in range(1, tentativas + 1):
        try:
            from google.genai import types
            client = _build_gemini_client(key)
            model = _get_model_image()
            config = getattr(types, "GenerateImagesConfig", None)
            kwargs = {"model": model, "prompt": prompt_imagem or "global supply chain operations"}
            try:
                if config:
                    kwargs["config"] = config(number_of_images=1)
                response = client.models.generate_images(**kwargs)
            except TypeError:
                # Compatibilidade com versões de SDK que não aceitam config no formato esperado.
                kwargs.pop("config", None)
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
            logger.warning(
                "Imagen indisponivel (%s) tentativa %d/%d: %s",
                _get_model_image(),
                tentativa,
                tentativas,
                e,
            )
            if tentativa < tentativas:
                time.sleep((backoff_ms * tentativa) / 1000.0)
    return None


def _gerar_via_gemini_multimodal(prompt_imagem: str, key: str) -> str | None:
    """Fallback Gemini multimodal para extrair inline_data de imagem em bytes."""
    model = _get_model_image_fallback()
    if not model:
        return None
    tentativas = _image_retry_attempts()
    backoff_ms = _image_retry_backoff_ms()
    for tentativa in range(1, tentativas + 1):
        try:
            client = _build_gemini_client(key)
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
            logger.warning(
                "Gemini multimodal imagem indisponivel (%s) tentativa %d/%d: %s",
                _get_model_image_fallback(),
                tentativa,
                tentativas,
                e,
            )
            if tentativa < tentativas:
                time.sleep((backoff_ms * tentativa) / 1000.0)
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
    if _stock_fallback_enabled():
        foto_local = _stock_image_local(prompt_imagem)
        if foto_local:
            return foto_local
    if _fallback_asset_local_existe():
        return FALLBACK_ASSET_LOCAL
    if _allow_remote_fallback():
        foto = _stock_image_url(prompt_imagem)
        if foto:
            return foto
    return _placeholder_url(prompt_imagem)


def gerar_fallback_imagem_estatica(prompt_imagem: str | None = None) -> str:
    """Retorna fallback de imagem estático para uso em outros agentes (ex.: Designer)."""
    return _fallback_url((prompt_imagem or "").strip())


def _fallback_asset_local_existe() -> bool:
    """Verifica se o asset estático versionado existe no diretório app/static/img."""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        rel = FALLBACK_ASSET_LOCAL.replace("/static/", "", 1).replace("/", os.sep)
        p = os.path.join(app_dir, "static", rel)
        return os.path.exists(p)
    except Exception:
        return False


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


def _stock_image_local(prompt_imagem: str) -> str | None:
    """Baixa uma imagem de stock contextual e salva em static/generated de forma estável por tema."""
    url = _stock_image_url(prompt_imagem)
    if not url:
        return None
    try:
        tema = (prompt_imagem or "logistics supply chain").strip().lower()
        tema = re.sub(r"[^a-z0-9\s]", " ", tema)
        tema = re.sub(r"\s+", " ", tema).strip()[:120] or "logistics supply chain"
        digest = hashlib.sha1(tema.encode("utf-8")).hexdigest()[:16]

        app_dir = os.path.dirname(os.path.abspath(__file__))
        out_dir = os.path.join(app_dir, "static", "generated")
        os.makedirs(out_dir, exist_ok=True)
        nome = f"julia_stock_{digest}.jpg"
        out_path = os.path.join(out_dir, nome)

        if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
            return f"/static/generated/{nome}"

        req = Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Accept": "image/*",
            },
        )
        timeout_sec = max(3.0, _get_gemini_timeout_ms() / 1000.0)
        with urlopen(req, timeout=timeout_sec) as resp:
            data = resp.read()
        if not data:
            return None
        with open(out_path, "wb") as f:
            f.write(data)
        return f"/static/generated/{nome}"
    except Exception as e:
        logger.warning("Fallback contextual de stock indisponivel: %s", e)
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


def classificar_origem_url_imagem(url: str | None) -> str:
    """Classifica origem da URL da imagem para auditoria do pipeline."""
    val = (url or "").strip().lower()
    if not val:
        return "vazio"
    if val == FALLBACK_ASSET_LOCAL.lower():
        return "contingencia_fixa"
    if "/static/generated/julia_stock_" in val:
        return "fallback_contextual_stock"
    if "/static/generated/julia_" in val:
        return "gerada_local_gemini"
    if "placehold.co" in val:
        return "placeholder_remoto"
    if "loremflickr.com" in val:
        return "stock_remoto"
    if val.startswith("http://") or val.startswith("https://"):
        return "url_remota"
    if val.startswith("/static/"):
        return "asset_local"
    return "desconhecida"
