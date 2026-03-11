#!/usr/bin/env python3
"""
Rotacao de segredos para ambientes .env.

Automatiza o que e possivel localmente:
- Gera segredos internos novos (SECRET_KEY, OPS_TOKEN, CRON_SECRET)
- Permite injetar segredos externos ja renovados no provedor via --set KEY=VALUE
- Atualiza um ou mais arquivos .env
- Emite relatorio sem expor valores completos
"""

from __future__ import annotations

import argparse
import json
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Tuple

AUTO_ROTATED_KEYS = ("SECRET_KEY", "OPS_TOKEN", "CRON_SECRET")
MANUAL_PROVIDER_KEYS = (
    "GOOGLE_OAUTH_CLIENT_ID",
    "GOOGLE_OAUTH_CLIENT_SECRET",
    "MAIL_PASSWORD",
    "GEMINI_API_KEY",
    "GEMINI_API_KEY_1",
    "GEMINI_API_KEY_2",
    "GEMINI_API_KEY_ROBERTO",
)

ENV_ASSIGNMENT_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")


@dataclass
class RotationResult:
    file_path: str
    updated_keys: List[str]
    inserted_keys: List[str]


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "*" * len(value)
    return f"{value[:4]}...{value[-4:]}"


def _generate_auto_values() -> Dict[str, str]:
    return {
        "SECRET_KEY": secrets.token_hex(32),
        "OPS_TOKEN": secrets.token_hex(32),
        "CRON_SECRET": secrets.token_hex(32),
    }


def _parse_set_args(set_args: List[str]) -> Dict[str, str]:
    values: Dict[str, str] = {}
    for item in set_args:
        if "=" not in item:
            raise ValueError(f"Formato invalido em --set: {item}. Use KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Chave vazia em --set: {item}")
        values[key] = value
    return values


def _update_env_content(content: str, updates: Dict[str, str], insert_missing: bool) -> Tuple[str, List[str], List[str]]:
    lines = content.splitlines()
    seen = set()
    updated_keys: List[str] = []

    for index, line in enumerate(lines):
        m = ENV_ASSIGNMENT_RE.match(line.strip())
        if not m:
            continue
        key = m.group(1)
        if key in updates:
            lines[index] = f"{key}={updates[key]}"
            seen.add(key)
            updated_keys.append(key)

    inserted_keys: List[str] = []
    missing = [k for k in updates.keys() if k not in seen]
    if insert_missing and missing:
        if lines and lines[-1].strip() != "":
            lines.append("")
        lines.append("# --- ROTACAO AUTOMATICA DE SEGREDOS ---")
        for key in missing:
            lines.append(f"{key}={updates[key]}")
            inserted_keys.append(key)

    return "\n".join(lines) + "\n", sorted(set(updated_keys)), sorted(inserted_keys)


def _load_file(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(f"Arquivo nao encontrado: {path}")
    return path.read_text(encoding="utf-8")


def _write_file(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8")


def _build_report(results: List[RotationResult], updates: Dict[str, str], missing_manual: List[str]) -> Dict[str, object]:
    return {
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "rotated_keys_masked": {k: _mask(v) for k, v in updates.items()},
        "files": [
            {
                "path": item.file_path,
                "updated_keys": item.updated_keys,
                "inserted_keys": item.inserted_keys,
            }
            for item in results
        ],
        "manual_provider_keys_pending": missing_manual,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Rotacao automatizada de segredos em arquivos .env")
    parser.add_argument(
        "--env-file",
        action="append",
        required=True,
        help="Arquivo .env alvo. Pode repetir o argumento para varios arquivos.",
    )
    parser.add_argument(
        "--set",
        dest="set_values",
        action="append",
        default=[],
        help="Define valor manual KEY=VALUE para segredos externos ja renovados no provedor.",
    )
    parser.add_argument(
        "--auto-only",
        action="store_true",
        help="Rotaciona somente segredos internos gerados localmente.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Nao grava arquivos; apenas mostra relatorio.",
    )
    parser.add_argument(
        "--report-file",
        default="scripts/security/rotation-report.json",
        help="Caminho do relatorio JSON (sempre sem valores completos).",
    )
    parser.add_argument(
        "--insert-missing",
        action="store_true",
        help="Insere chaves ausentes no arquivo .env. Padrao: apenas atualiza chaves existentes.",
    )

    args = parser.parse_args()

    auto_values = _generate_auto_values()
    manual_values = _parse_set_args(args.set_values)

    updates: Dict[str, str] = dict(auto_values)
    if not args.auto_only:
        updates.update(manual_values)

    missing_manual = []
    if not args.auto_only:
        missing_manual = sorted([k for k in MANUAL_PROVIDER_KEYS if k not in manual_values])

    results: List[RotationResult] = []
    for env_file in args.env_file:
        path = Path(env_file)
        content = _load_file(path)
        new_content, updated_keys, inserted_keys = _update_env_content(
            content,
            updates,
            insert_missing=args.insert_missing,
        )
        if not args.dry_run:
            _write_file(path, new_content)
        results.append(
            RotationResult(
                file_path=str(path),
                updated_keys=updated_keys,
                inserted_keys=inserted_keys,
            )
        )

    report = _build_report(results, updates, missing_manual)
    report_path = Path(args.report_file)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding="utf-8")

    print("Rotacao executada com sucesso.")
    print(f"Relatorio: {report_path}")
    if args.dry_run:
        print("Modo dry-run ativo: nenhum arquivo foi alterado.")
    if missing_manual:
        print("Pendencias de segredos externos (rotacione no provedor e rode novamente com --set):")
        for key in missing_manual:
            print(f"- {key}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
