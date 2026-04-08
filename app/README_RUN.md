# Guia de Execução Local

Este documento complementa o `README.md` da raiz.  
A visão principal do projeto, do chat da Júlia e das regras críticas fica no documento raiz.

## Objetivo

Fornecer um checklist curto de execução local e validação funcional básica.

## Pré-requisitos

- `pip install -r requirements.txt`
- `DATABASE_URL` válido
- `APP_ENV` definido explicitamente como `dev`, `homolog` ou `prod`
- ambiente carregado a partir de `app/.env.example`

## Subida Local

PowerShell:

```powershell
$env:APP_ENV="dev"
python -m app.web
```

## Validação Funcional Recomendada

1. autenticar no sistema;
2. validar o chat da Júlia com usuário permitido;
3. validar no chat:
   - `Enter` envia;
   - `Shift+Enter` quebra linha;
   - markdown básico renderiza sem vazamento de `*`;
   - sugestão clicável executa a intenção;
   - links úteis só aparecem quando fizerem sentido;
4. validar upload Roberto;
5. validar páginas `/noticia/<id>` com botão `Voltar Para Home`;
6. validar telas admin:
   - `/admin/dashboard`
   - `/admin/agentes/julia`
   - `/admin/agentes/cleiton`
   - `/admin/controle-usuarios`
   - `/admin/planos`
7. validar endpoint admin de franquia:
   - `/admin/api/cleiton-franquia/<franquia_id>/validacao`

## Comandos Úteis

- web: `python -m app.web`
- índices financeiros: `python -m app.finance`
- ciclo Cleiton: `python -m app.run_cleiton`
- testes focados Fase 2:
  - `pytest tests/test_cleiton_classificacao_status.py`
  - `pytest tests/test_cleiton_motor_reconciliacao.py`
  - `pytest tests/test_cleiton_upload_billing_service.py`
  - `pytest tests/test_franquia_operacao_autorizacao_service.py`

## Troubleshooting Objetivo

- `No module named alembic`: ambiente sem Alembic; revisar estratégia de migrations do projeto.
- `relation does not exist`: schema desalinhado; revisar `../migrations/README`.
- chat/upload bloqueado: verificar status operacional da franquia.
- links úteis ausentes no chat: pode ser resultado de filtro de relevância ou falha segura da busca web.

## Referência Principal

Antes de ampliar este guia, atualize primeiro o `README.md` da raiz.
