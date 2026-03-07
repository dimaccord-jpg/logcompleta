# 🏃 Guia de Execução Local

Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento e Homologação. A lógica de autenticação está em `app/auth_services.py`; a infraestrutura em `app/infra.py`; as rotas operacionais (diagnóstico OAuth, auditoria de usuários, promote-admin, health) estão em `app/ops_routes.py` (Blueprint). O `web.py` apenas expõe as rotas e registra os blueprints.

## Pré-requisitos

1. Instale as dependências:
   ```bash
   pip install -r ../requirements.txt
   ```
2. Garanta que os arquivos `.env.dev` e `.env.homolog` existam na pasta `app/`.
   - O arquivo `.env` simples é legado e **não deve ser usado**.
   - Use `app/.env.example` como base, copiando para `.env.dev` e `.env.homolog` e ajustando apenas os valores.
   - Para login com Google, defina `GOOGLE_OAUTH_REDIRECT_URI` (ex.: `http://127.0.0.1:5000/login/google/callback`) e, em dev, `OAUTHLIB_INSECURE_TRANSPORT=1`.

---

## 1. Ambiente de Desenvolvimento (DEV)

*Características:* Debug ATIVO, Reload automático, Logs no console.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="dev"; python web.py
```

**Comando (Linux/Mac):**
```bash
APP_ENV=dev python web.py
```

---

## 2. Ambiente de Homologação (HOMOLOG)

*Características:* Debug OFF, Simulação de Produção, Logs INFO.

**Comando (Windows PowerShell):**
```powershell
$env:APP_ENV="homolog"; python web.py
```

**Comando (Linux/Mac - Via Gunicorn - Recomendado):**
```bash
APP_ENV=homolog gunicorn -w 2 -b 0.0.0.0:8000 web:app
```

---

## 3. Diagnóstico OAuth (opcional)

As rotas de ops (`app/ops_routes.py`) incluem:
- `GET /health` — health check (não exige token).
- `GET /oauth-diagnostics` — estado do OAuth; exige header `X-Ops-Token`.
- `POST /ops/user-audit` e `POST /ops/promote-admin` — exigem `X-Ops-Token`.

Exemplo de diagnóstico OAuth:
```bash
curl -H "X-Ops-Token: SEU_OPS_TOKEN" http://127.0.0.1:5000/oauth-diagnostics
```