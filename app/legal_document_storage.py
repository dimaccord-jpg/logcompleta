"""
Camada central de armazenamento persistente para documentos legais.

Todos os caminhos usam settings.data_dir (storage persistente).
"""
from __future__ import annotations

from pathlib import Path

from app.settings import settings


LEGAL_ROOT_DIRNAME = "legal"
TERMS_DIRNAME = "terms"
PRIVACY_POLICIES_DIRNAME = "privacy_policies"


def get_legal_base_dir() -> Path:
    """Retorna a base persistente de documentos legais."""
    return Path(settings.data_dir) / LEGAL_ROOT_DIRNAME


def get_terms_storage_dir() -> Path:
    """Retorna o diretório persistente de Termos de Uso."""
    return get_legal_base_dir() / TERMS_DIRNAME


def get_privacy_policies_storage_dir() -> Path:
    """Retorna o diretorio persistente de Politicas de Privacidade."""
    return get_legal_base_dir() / PRIVACY_POLICIES_DIRNAME


def ensure_storage_dir(path: Path) -> Path:
    """Cria diretório de forma segura/idempotente e devolve o path absoluto."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def ensure_terms_storage_dir() -> Path:
    """Garante existência do diretório persistente de Termos de Uso."""
    return ensure_storage_dir(get_terms_storage_dir())


def ensure_privacy_policies_storage_dir() -> Path:
    """Garante existencia do diretorio persistente de Politicas de Privacidade."""
    return ensure_storage_dir(get_privacy_policies_storage_dir())


def validate_legal_filename(filename: str | None) -> str:
    """
    Valida basename para evitar path traversal.

    Regras:
    - nao vazio;
    - sem separadores de diretório;
    - exatamente basename (sem componentes pai/filho).
    """
    normalized = (filename or "").strip()
    if not normalized:
        raise ValueError("Nome de arquivo legal inválido: vazio.")
    basename = Path(normalized).name
    if basename != normalized or basename in {".", ".."}:
        raise ValueError("Nome de arquivo legal inválido.")
    if any(sep in normalized for sep in ("/", "\\")):
        raise ValueError("Nome de arquivo legal inválido.")
    return basename


def build_safe_storage_path(directory: Path, filename: str | None) -> Path:
    """
    Monta caminho absoluto seguro dentro do diretório informado.
    """
    safe_name = validate_legal_filename(filename)
    absolute_dir = directory.resolve()
    candidate = (absolute_dir / safe_name).resolve()
    if absolute_dir != candidate.parent:
        raise ValueError("Path inválido para armazenamento de documento legal.")
    return candidate
