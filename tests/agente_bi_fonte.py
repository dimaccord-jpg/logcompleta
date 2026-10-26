"""Leitura de código-fonte para asserções, sem a docstring do módulo."""
from __future__ import annotations

import ast
from pathlib import Path


def fonte_sem_docstring_de_modulo(caminho) -> str:
    arquivo = Path(caminho)
    texto = arquivo.read_text(encoding="utf-8")
    if arquivo.suffix != ".py":
        return texto
    try:
        modulo = ast.parse(texto)
    except SyntaxError:
        return texto
    if not modulo.body:
        return texto
    primeiro = modulo.body[0]
    if not isinstance(primeiro, ast.Expr):
        return texto
    valor = primeiro.value
    if not isinstance(valor, ast.Constant) or not isinstance(valor.value, str):
        return texto
    if not primeiro.end_lineno:
        return texto
    return "".join(texto.splitlines(keepends=True)[primeiro.end_lineno :])
