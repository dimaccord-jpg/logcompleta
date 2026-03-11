"""
Configuracao do Gunicorn para reduzir abortos de worker em tarefas sincronas mais longas
(ex.: execucao manual do ciclo via painel admin).
"""

import os


def _int_env(name: str, default: int, min_value: int) -> int:
	raw = (os.getenv(name, "") or "").strip()
	try:
		value = int(raw) if raw else default
	except ValueError:
		value = default
	return max(min_value, value)


# Mantem configuravel por ambiente sem hardcode operacional em codigo.
timeout = _int_env("GUNICORN_TIMEOUT_SECONDS", 120, 30)
graceful_timeout = _int_env("GUNICORN_GRACEFUL_TIMEOUT_SECONDS", 30, 10)
keepalive = _int_env("GUNICORN_KEEPALIVE_SECONDS", 5, 1)
