#!/usr/bin/env python3
"""
Sonda diagnóstica LOCAL da conectividade Gemini/Imagen da Júlia (SCRUM-214).

Isolada do pipeline de produção:
- não é importada pelo app;
- não chama gerar_imagem_publicavel;
- não usa fallback/stock/modelo secundário;
- não persiste imagem, notícia, banco nem eventos de governança;
- no máximo UMA invocação SDK, com HttpRetryOptions(attempts=1).

Uso (local, após falha observada):
  set APP_ENV=dev
  python scripts/diagnose_julia_image_provider.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

# Garante import de `app.*` ao rodar o script diretamente.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DIAGNOSTIC_PROMPT = "diagnostic connectivity probe for julia image provider"


def resolve_image_api_key(getenv: Callable[[str], str | None] = os.getenv) -> str | None:
    """Mesma ordem de chave do pipeline de imagem da Júlia."""
    for label in ("GEMINI_API_KEY_2", "GEMINI_API_KEY_1", "GEMINI_API_KEY"):
        value = (getenv(label) or "").strip()
        if value:
            return value
    return None


def resolve_image_model(getenv: Callable[[str], str | None] = os.getenv) -> str:
    """Mesmo default de modelo do pipeline (`GEMINI_MODEL_IMAGE`)."""
    return (getenv("GEMINI_MODEL_IMAGE") or "").strip() or "gemini-3.1-flash-image"


def modelo_imagem_usa_generate_images(model: str) -> bool:
    """Espelha `app.run_julia_agente_imagem._modelo_imagem_usa_generate_images`."""
    return (model or "").strip().lower().startswith("imagen")


def choose_method(model: str) -> str:
    return "generate_images" if modelo_imagem_usa_generate_images(model) else "generate_content"


def extract_http_status(exc: BaseException | None) -> str:
    """Extrai só o status HTTP, sem mensagem/payload."""
    if exc is None:
        return ""
    for attr in ("code", "status_code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int) and 100 <= value <= 599:
            return str(value)
        if isinstance(value, str) and value.isdigit():
            code = int(value)
            if 100 <= code <= 599:
                return str(code)
    response = getattr(exc, "response", None)
    if response is not None:
        for attr in ("status_code", "status", "code"):
            value = getattr(response, attr, None)
            if isinstance(value, int) and 100 <= value <= 599:
                return str(value)
    return ""


def format_probe_output(
    *,
    model: str,
    method: str,
    exception: BaseException | None,
    http_status: str = "",
) -> str:
    exc_label = "None" if exception is None else type(exception).__name__
    status = http_status if http_status else (extract_http_status(exception) if exception else "")
    return "\n".join(
        [
            "provider=gemini",
            f"model={model}",
            f"method={method}",
            f"exception={exc_label}",
            f"http_status={status}",
        ]
    )


def build_gemini_client(api_key: str) -> Any:
    """Cliente SDK com retry HTTP desabilitado além de 1 tentativa."""
    from google import genai
    from google.genai import types as genai_types

    http_options = genai_types.HttpOptions(
        retry_options=genai_types.HttpRetryOptions(attempts=1),
    )
    return genai.Client(api_key=api_key, http_options=http_options)


def invoke_provider_once(client: Any, *, model: str, method: str) -> Any:
    """Uma única chamada SDK: generate_images OU generate_content."""
    if method == "generate_images":
        return client.models.generate_images(model=model, prompt=DIAGNOSTIC_PROMPT)
    return client.models.generate_content(model=model, contents=DIAGNOSTIC_PROMPT)


def require_explicit_app_env(getenv: Callable[[str], str | None] = os.getenv) -> str:
    raw = getenv("APP_ENV")
    if raw is None or not str(raw).strip():
        raise SystemExit("APP_ENV é obrigatório para a sonda diagnóstica (defina explicitamente).")
    return str(raw).strip().lower()


def run_probe(
    *,
    getenv: Callable[[str], str | None] = os.getenv,
    load_env: Callable[[], bool] | None = None,
    client_factory: Callable[[str], Any] = build_gemini_client,
    invoker: Callable[..., Any] = invoke_provider_once,
) -> str:
    """
    Executa a sonda e devolve o texto de saída (sem imprimir segredos).

    Call graph máximo:
      run_probe → client.models.generate_images|generate_content  (1x)
    """
    require_explicit_app_env(getenv)

    if load_env is None:
        from app.env_loader import load_app_env

        load_env = load_app_env
    load_env()

    model = resolve_image_model(getenv)
    method = choose_method(model)
    api_key = resolve_image_api_key(getenv)
    if not api_key:
        return format_probe_output(
            model=model,
            method=method,
            exception=RuntimeError("missing_api_key"),
            http_status="",
        )

    client = client_factory(api_key)
    try:
        # Resposta descartada de propósito: conectividade/modelo apenas.
        _ = invoker(client, model=model, method=method)
    except BaseException as exc:  # noqa: BLE001 — sonda precisa classificar qualquer falha
        return format_probe_output(model=model, method=method, exception=exc)

    return format_probe_output(model=model, method=method, exception=None)


def main() -> int:
    print(run_probe())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
