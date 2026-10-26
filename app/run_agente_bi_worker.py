"""Processo separado da fila do AgenteBI.

Não é thread do Flask, não entra no gunicorn e não deve ser declarado no
Render nesta entrega. O web (`start.sh`) continua só com a API.

Quando a operação autorizar, num serviço worker próprio, ainda sem provedor:

    AGENTE_BI_WORKER_HABILITADO=1 python -m app.run_agente_bi_worker --once
    AGENTE_BI_WORKER_HABILITADO=1 python -m app.run_agente_bi_worker --max-ciclos 20 --pausa-segundos 2

O default é recusar. Sem a variável, o processo sai antes de abrir o banco.
Não encaminha dataset, prompt ou resposta à Anthropic, ao Gemini ou a outro provedor.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

from app.services.agente_bi_execucao_service import LEASE_PADRAO_SEGUNDOS, executar_um_ciclo


def processo_habilitado() -> bool:
    return os.environ.get("AGENTE_BI_WORKER_HABILITADO") == "1"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Worker durável do AgenteBI (simulado)")
    parser.add_argument("--once", action="store_true", help="Processa no máximo uma operação e sai")
    parser.add_argument("--max-ciclos", type=int, default=1)
    parser.add_argument("--pausa-segundos", type=float, default=2.0)
    parser.add_argument("--lease-segundos", type=int, default=LEASE_PADRAO_SEGUNDOS)
    parser.add_argument("--worker-id", default="")
    args = parser.parse_args(argv)
    if not processo_habilitado():
        print(
            "Worker do AgenteBI recusado. Defina AGENTE_BI_WORKER_HABILITADO=1 "
            "somente no processo separado, quando a operação autorizar.",
            file=sys.stderr,
        )
        return 2
    if args.max_ciclos < 1 or args.pausa_segundos < 0:
        return 2
    worker_id = args.worker_id or f"worker.{os.getpid()}"
    from app.web import app

    with app.app_context():
        limite = 1 if args.once else args.max_ciclos
        for ciclo in range(limite):
            resultado = executar_um_ciclo(worker_id=worker_id, lease_segundos=args.lease_segundos)
            if not resultado.get("adquirida"):
                if args.once:
                    return 0
                if ciclo + 1 < limite and args.pausa_segundos:
                    time.sleep(args.pausa_segundos)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
