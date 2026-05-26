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
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import quote

from app.run_cleiton_gemini_governance import (
    cleiton_governed_generate_content,
    cleiton_governed_generate_images,
)

logger = logging.getLogger(__name__)


def _api_key_label_imagem() -> str:
    if os.getenv("GEMINI_API_KEY_2"):
        return "GEMINI_API_KEY_2"
    if os.getenv("GEMINI_API_KEY_1"):
        return "GEMINI_API_KEY_1"
    if os.getenv("GEMINI_API_KEY"):
        return "GEMINI_API_KEY"
    return "unknown"


# URL opcional de fallback configurada por ambiente.
# Se ausente, usamos placeholder contextual para manter aderência ao conteúdo.
IMAGEM_FALLBACK_URL = (os.getenv("IMAGEM_FALLBACK_URL", "") or "").strip()
FALLBACK_ASSET_LOCAL = "/static/img/fallback-capa-v1.svg"
FALLBACK_ASSET_SECUNDARIO = "/static/img/logo.png"


def get_image_runtime_config() -> dict[str, str | int | bool]:
    """Expoe configuracao efetiva de runtime de imagem sem vazar segredos."""
    provider = (os.getenv("IMAGE_PROVIDER", "").strip() or "auto").lower()
    return {
        "image_provider": provider or "auto",
        "gemini_model_image": _get_model_image(),
        "gemini_model_image_fallback": _get_model_image_fallback() or "<disabled>",
        "gemini_http_timeout_ms": os.getenv("GEMINI_HTTP_TIMEOUT_MS", "").strip() or "<default:20000>",
        "gemini_image_http_timeout_ms": (
            os.getenv("GEMINI_IMAGE_HTTP_TIMEOUT_MS", "").strip() or "<inherits GEMINI_HTTP_TIMEOUT_MS/default>"
        ),
        "provider_efetivo": "gemini" if provider in ("auto", "gemini") else "fallback",
        "modelo_efetivo_principal": _get_model_image(),
        "modelo_efetivo_fallback": _get_model_image_fallback() or "<disabled>",
        "timeout_efetivo_ms": _get_gemini_timeout_ms(),
        "gemini_api_key_present": bool(
            os.getenv("GEMINI_API_KEY_2") or os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
        ),
        "google_api_key_present": bool(os.getenv("GOOGLE_API_KEY")),
    }


def log_image_runtime_config() -> None:
    cfg = get_image_runtime_config()
    logger.info(
        "Julia imagem runtime | IMAGE_PROVIDER=%s | GEMINI_MODEL_IMAGE=%s | "
        "GEMINI_MODEL_IMAGE_FALLBACK=%s | GEMINI_HTTP_TIMEOUT_MS=%s | "
        "GEMINI_IMAGE_HTTP_TIMEOUT_MS=%s | provider_efetivo=%s | modelo_efetivo=%s | "
        "fallback_modelo_efetivo=%s | timeout_efetivo_ms=%s | GEMINI_API_KEY=%s | GOOGLE_API_KEY=%s",
        cfg["image_provider"],
        cfg["gemini_model_image"],
        cfg["gemini_model_image_fallback"],
        cfg["gemini_http_timeout_ms"],
        cfg["gemini_image_http_timeout_ms"],
        cfg["provider_efetivo"],
        cfg["modelo_efetivo_principal"],
        cfg["modelo_efetivo_fallback"],
        cfg["timeout_efetivo_ms"],
        "present" if cfg["gemini_api_key_present"] else "absent",
        "present" if cfg["google_api_key_present"] else "absent",
    )


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
    return (os.getenv("IMAGE_STOCK_FALLBACK_ENABLED", "false") or "false").strip().lower() in (
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


def _modelo_imagem_usa_generate_images(model: str) -> bool:
    return (model or "").strip().lower().startswith("imagen")


def _log_image_attempt_failure(
    *,
    provider: str,
    model: str,
    operation: str,
    attempt: int,
    duration_ms: int,
    error_summary: str,
) -> None:
    logger.warning(
        "Julia imagem falhou | provider=%s | model=%s | operation=%s | timeout_ms=%s | tentativa=%s | duracao_ms=%s | error_summary=%s",
        provider,
        model,
        operation,
        _get_gemini_timeout_ms(),
        attempt,
        duration_ms,
        error_summary,
    )


def gerar_url_imagem(prompt_imagem: str) -> str | None:
    """
    Gera url_imagem a partir do prompt. Retorna URL string ou fallback; nunca list/dict.
    Se IMAGE_PROVIDER ou Gemini Imagen não estiver configurado, retorna IMAGEM_FALLBACK_URL.
    """
    return gerar_imagem_publicavel(prompt_imagem).get("url")


def gerar_imagem_publicavel(prompt_imagem: str) -> dict[str, str]:
    """
    Gera uma imagem e retorna metadados de observabilidade para a pipeline.
    Nunca bloqueia a publicação: sempre tenta entregar uma URL final estável.
    """
    prompt_imagem = (prompt_imagem or "").strip()[:500]
    provider = (os.getenv("IMAGE_PROVIDER", "").strip() or "auto").lower()

    if provider in ("gemini", "auto"):
        try:
            gemini_url = _gerar_via_gemini(prompt_imagem)
            if gemini_url:
                return {
                    "url": gemini_url,
                    "status": "sucesso",
                    "origem": classificar_origem_url_imagem(gemini_url),
                    "motivo": "gemini_ok",
                    "provider": "gemini",
                }
        except Exception as e:
            logger.exception("Falha inesperada ao gerar imagem Gemini: %s", e)
            motivo = f"gemini_exception_{e.__class__.__name__.lower()}"
        else:
            motivo = "gemini_sem_resultado"
        fallback_url = _fallback_url(prompt_imagem)
        return {
            "url": fallback_url,
            "status": "fallback",
            "origem": classificar_origem_url_imagem(fallback_url),
            "motivo": motivo,
            "provider": "fallback",
        }

    fallback_url = _fallback_url(prompt_imagem)
    return {
        "url": fallback_url,
        "status": "fallback",
        "origem": classificar_origem_url_imagem(fallback_url),
        "motivo": f"provider_{provider}_nao_gemini",
        "provider": "fallback",
    }


def _gerar_via_gemini(prompt_imagem: str) -> str | None:
    """Tenta gerar imagem via Gemini por duas estratégias; em falha, retorna None."""
    key = os.getenv("GEMINI_API_KEY_2") or os.getenv("GEMINI_API_KEY_1") or os.getenv("GEMINI_API_KEY")
    if not key:
        return None

    model_principal = _get_model_image()
    if _modelo_imagem_usa_generate_images(model_principal):
        url = _gerar_via_gemini_imagen(prompt_imagem, key)
        if url:
            return url
        return _gerar_via_gemini_multimodal(prompt_imagem, key)
    return _gerar_via_gemini_multimodal(prompt_imagem, key, model_override=model_principal)


def _gerar_via_gemini_imagen(prompt_imagem: str, key: str) -> str | None:
    """Tenta gerar imagem via API Imagen do Gemini."""
    tentativas = _image_retry_attempts()
    backoff_ms = _image_retry_backoff_ms()
    for tentativa in range(1, tentativas + 1):
        started = time.monotonic()
        try:
            from google.genai import types
            client = _build_gemini_client(key)
            model = _get_model_image()
            config = getattr(types, "GenerateImagesConfig", None)
            kwargs = {"model": model, "prompt": prompt_imagem or "global supply chain operations"}
            label = _api_key_label_imagem()
            try:
                if config:
                    kwargs["config"] = config(number_of_images=1)
                response = cleiton_governed_generate_images(
                    client,
                    agent="julia",
                    flow_type="julia_imagem_imagen",
                    api_key_label=label,
                    **kwargs,
                )
            except TypeError:
                # Compatibilidade com versões de SDK que não aceitam config no formato esperado.
                kwargs.pop("config", None)
                response = cleiton_governed_generate_images(
                    client,
                    agent="julia",
                    flow_type="julia_imagem_imagen",
                    api_key_label=label,
                    **kwargs,
                )
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
            _log_image_attempt_failure(
                provider="gemini",
                model=_get_model_image(),
                operation="generate_images",
                attempt=tentativa,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary=str(e),
            )
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


def _gerar_via_gemini_multimodal(prompt_imagem: str, key: str, model_override: str | None = None) -> str | None:
    """Fallback Gemini multimodal para extrair inline_data de imagem em bytes."""
    model = (model_override or _get_model_image_fallback()).strip()
    if not model:
        return None
    tentativas = _image_retry_attempts()
    backoff_ms = _image_retry_backoff_ms()
    for tentativa in range(1, tentativas + 1):
        started = time.monotonic()
        try:
            client = _build_gemini_client(key)
            prompt_final = (
                "Create a realistic editorial illustration, no text overlay, no watermark, "
                "high detail, cinematic lighting, logistics/supply chain context: "
                f"{prompt_imagem or 'global supply chain operations'}"
            )
            response = cleiton_governed_generate_content(
                client,
                model=model,
                contents=prompt_final,
                agent="julia",
                flow_type="julia_imagem_multimodal",
                api_key_label=_api_key_label_imagem(),
            )
            raw_bytes = _extrair_bytes_response_multimodal(response)
            if raw_bytes:
                return _salvar_imagem_local(raw_bytes)
            return None
        except Exception as e:
            _log_image_attempt_failure(
                provider="gemini",
                model=_get_model_image_fallback() or "<disabled>",
                operation="generate_content",
                attempt=tentativa,
                duration_ms=int((time.monotonic() - started) * 1000),
                error_summary=str(e),
            )
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
    if _fallback_asset_local_existe(FALLBACK_ASSET_LOCAL):
        return FALLBACK_ASSET_LOCAL
    if _fallback_asset_local_existe(FALLBACK_ASSET_SECUNDARIO):
        return FALLBACK_ASSET_SECUNDARIO
    return FALLBACK_ASSET_LOCAL


def gerar_fallback_imagem_estatica(prompt_imagem: str | None = None) -> str:
    """Retorna fallback de imagem estático para uso em outros agentes (ex.: Designer)."""
    return _fallback_url((prompt_imagem or "").strip())


def _fallback_asset_local_existe(asset_path: str) -> bool:
    """Verifica se o asset estático versionado existe no diretório app/static/img."""
    try:
        app_dir = os.path.dirname(os.path.abspath(__file__))
        rel = asset_path.replace("/static/", "", 1).replace("/", os.sep)
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
    """Baixa uma imagem de stock contextual e salva no storage persistente."""
    url = _stock_image_url(prompt_imagem)
    if not url:
        return None
    try:
        tema = (prompt_imagem or "logistics supply chain").strip().lower()
        tema = re.sub(r"[^a-z0-9\s]", " ", tema)
        tema = re.sub(r"\s+", " ", tema).strip()[:120] or "logistics supply chain"
        digest = hashlib.sha1(tema.encode("utf-8")).hexdigest()[:8]
        nome = f"julia_stock_{digest}_{uuid.uuid4().hex[:12]}.jpg"
        out_path = _path_arquivo_gerado(nome)

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
        return _url_publica_arquivo_gerado(nome)
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
    """Salva imagem gerada no storage persistente e retorna URL pública estável."""
    if not raw_bytes:
        return None
    try:
        nome = f"julia_{uuid.uuid4().hex}.png"
        out_path = _path_arquivo_gerado(nome)
        with open(out_path, "wb") as f:
            f.write(raw_bytes)
        return _url_publica_arquivo_gerado(nome)
    except Exception as e:
        logger.warning("Falha ao salvar imagem local gerada pela IA: %s", e)
        return None


def _dir_imagens_persistente() -> Path:
    """
    Diretório persistente para imagens geradas.
    Usa settings.data_dir quando disponível; fallback local apenas se necessário.
    """
    try:
        from app.settings import settings

        base_dir = Path(settings.data_dir)
    except Exception:
        app_dir = Path(os.path.dirname(os.path.abspath(__file__)))
        base_dir = app_dir
    out_dir = base_dir / "generated"
    out_dir.mkdir(parents=True, exist_ok=True)
    return out_dir


def _path_arquivo_gerado(nome: str) -> Path:
    return _dir_imagens_persistente() / nome


def _url_publica_arquivo_gerado(nome: str) -> str:
    return f"/media/generated/{nome}"


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
    if val == FALLBACK_ASSET_SECUNDARIO.lower():
        return "contingencia_fixa"
    if "/media/generated/julia_stock_" in val:
        return "fallback_contextual_stock"
    if "/media/generated/julia_" in val:
        return "gerada_local_gemini"
    # Compatibilidade retroativa com imagens legadas em static/generated.
    if "/static/generated/julia_stock_" in val:
        return "fallback_contextual_stock_legado"
    if "/static/generated/julia_" in val:
        return "gerada_local_gemini_legado"
    if "placehold.co" in val:
        return "placeholder_remoto"
    if "loremflickr.com" in val:
        return "stock_remoto"
    if val.startswith("http://") or val.startswith("https://"):
        return "url_remota"
    if val.startswith("/static/"):
        return "asset_local"
    return "desconhecida"
