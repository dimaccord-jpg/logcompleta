#!/usr/bin/env bash

# Diagnose script for Log Completa Flask app on Ubuntu 24.04
# Read-only, idempotent, no service restarts.

PROJECT_DIR="/srv/logcompleta/logcompleta"
VENV_DIR="/srv/logcompleta/logcompleta/.venv"
WSGI_APP="app.web:app"
GUNICORN_BIND="127.0.0.1:8000"

sep() {
  echo
  echo "============================================================"
  echo "$1"
  echo "============================================================"
}

echo
echo "Log Completa - Diagnóstico de Ambiente (somente leitura)"
echo "Timestamp: $(date -Iseconds)"
echo "Host: $(hostname)"
echo "User: $(whoami)"
echo

########################################
# 1) Validação de OS, Python, pip, venv
########################################
sep "1) Informações de SO e versão"

echo "[OS] uname -a:"
uname -a 2>&1 || echo "Falha ao executar uname -a"

echo
echo "[OS] Detalhes (Ubuntu):"
if command -v lsb_release >/dev/null 2>&1; then
  lsb_release -a 2>&1 || echo "Falha ao executar lsb_release -a"
elif [ -f /etc/os-release ]; then
  cat /etc/os-release 2>&1
else
  echo "/etc/os-release não encontrado e lsb_release não disponível."
fi

sep "2) Python e pip (sistema e venv)"

echo "[Python - sistema] python3 --version:"
python3 --version 2>&1 || echo "python3 não encontrado no PATH"

echo
echo "[pip - sistema] pip3 --version:"
pip3 --version 2>&1 || echo "pip3 não encontrado no PATH"

echo
echo "[Venv] Verificando diretório da venv em: $VENV_DIR"
if [ -d "$VENV_DIR" ]; then
  echo "Venv encontrada em $VENV_DIR"
else
  echo "ATENÇÃO: Diretório de venv NÃO encontrado em $VENV_DIR"
fi

VENV_PYTHON="$VENV_DIR/bin/python"
VENV_PIP="$VENV_DIR/bin/pip"
VENV_GUNICORN="$VENV_DIR/bin/gunicorn"

echo
echo "[Venv Python] $VENV_PYTHON --version:"
if [ -x "$VENV_PYTHON" ]; then
  "$VENV_PYTHON" --version 2>&1 || echo "Falha ao executar Python da venv"
else
  echo "ATENÇÃO: Binário Python da venv não encontrado ou não executável em $VENV_PYTHON"
fi

echo
echo "[Venv pip] $VENV_PIP --version:"
if [ -x "$VENV_PIP" ]; then
  "$VENV_PIP" --version 2>&1 || echo "Falha ao executar pip da venv"
else
  echo "ATENÇÃO: Binário pip da venv não encontrado ou não executável em $VENV_PIP"
fi

echo
echo "[gunicorn] Checando binário em: $VENV_GUNICORN"
if [ -x "$VENV_GUNICORN" ]; then
  echo "gunicorn encontrado em $VENV_GUNICORN"
  "$VENV_GUNICORN" --version 2>&1 || echo "Falha ao obter versão do gunicorn"
else
  echo "ATENÇÃO: Binário gunicorn NÃO encontrado ou não executável em $VENV_GUNICORN"
fi

echo
echo "[Projeto] Verificando diretório do projeto em: $PROJECT_DIR"
if [ -d "$PROJECT_DIR" ]; then
  echo "Diretório do projeto encontrado em $PROJECT_DIR"
else
  echo "ATENÇÃO: Diretório do projeto NÃO encontrado em $PROJECT_DIR"
fi

echo
echo "[WSGI] Configuração esperada:"
echo "  App WSGI: $WSGI_APP"
echo "  Bind Gunicorn: $GUNICORN_BIND"

########################################
# 2) Processos e portas
########################################
sep "3) Processos e portas (Gunicorn / porta 8000)"

echo "[ss] Verificando porta $GUNICORN_BIND"
echo "Comando: ss -ltnp | grep 8000"
ss -ltnp 2>&1 | grep 8000 || echo "Nenhuma linha contendo 8000 encontrada pelo ss (ou ss indisponível)."

echo
echo "[ps] Processos gunicorn"
echo "Comando: ps aux | grep gunicorn | grep -v grep"
ps aux 2>&1 | grep gunicorn | grep -v grep || echo "Nenhum processo gunicorn visível pelo ps (ou ps indisponível)."

########################################
# 3) Testes HTTP locais via Gunicorn
########################################
sep "4) Testes HTTP locais em http://127.0.0.1:8000"

if command -v curl >/dev/null 2>&1; then
  echo "[curl] Testando raiz /"
  echo "Comando: curl -v -m 10 http://127.0.0.1:8000/"
  curl -v -m 10 http://127.0.0.1:8000/ 2>&1 || echo "curl para / falhou com status $?"

  echo
  echo "[curl] Testando /health"
  echo "Comando: curl -v -m 10 http://127.0.0.1:8000/health"
  curl -v -m 10 http://127.0.0.1:8000/health 2>&1 || echo "curl para /health falhou com status $?"
else
  echo "ATENÇÃO: curl não encontrado no PATH. Testes HTTP não executados."
fi

########################################
# 4) Logs recentes e status do Nginx
########################################
sep '5) Logs recentes relacionados a "gunicorn|flask|traceback|error" (últimos 10 minutos)'

if command -v journalctl >/dev/null 2>&1; then
  echo "Comando: journalctl --since \"10 minutes ago\" --no-pager | grep -Ei \"gunicorn|flask|traceback|error\" || true"
  journalctl --since "10 minutes ago" --no-pager 2>&1 | grep -Ei "gunicorn|flask|traceback|error" || true
else
  echo "journalctl não disponível neste sistema."
fi

sep "6) systemctl status nginx (sem pager)"

if command -v systemctl >/dev/null 2>&1; then
  echo "Comando: systemctl status nginx --no-pager || true"
  systemctl status nginx --no-pager || true
else
  echo "systemctl não disponível neste sistema."
fi

sep "Fim do diagnóstico"

echo "Diagnóstico concluído. Nenhuma ação de alteração de estado foi executada."
echo
