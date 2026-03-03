# 🏃 Guia de Execução Local

Este projeto utiliza variáveis de ambiente para alternar entre configurações de Desenvolvimento e Homologação.

## Pré-requisitos

1. Instale as dependências:
   ```bash
   pip install -r ../requirements.txt
   ```
2. Garanta que os arquivos `.env.dev` e `.env.homolog` existam na pasta `app/`.

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