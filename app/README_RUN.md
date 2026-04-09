# Guia de Execução Local

Este documento complementa o `README.md` da raiz.  
A visão principal do projeto, do chat da Júlia e das regras críticas fica no documento raiz.

## Objetivo

Fornecer um checklist curto de execução local e validação funcional básica.
As regras de produto, governança e comportamento oficial permanecem centralizadas no `README.md` da raiz.

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
   - mensagem inicial exibida: `Faça uma pergunta sobre logística, fretes, supply chain ou indicadores. Ex.: "Como o dólar impacta o frete?"`;
   - ao bloquear por franquia, a mensagem visual de limite renderiza link markdown clicável de upgrade;
4. validar upload Roberto;
5. validar páginas `/noticia/<id>` com botão `Voltar Para Home`;
6. validar experiência da tela `/fretes` por perfil:
   - admin: consulta por `UF + Cidade` disponível e painel completo;
   - usuário comum: foco visual em upload/BI, sem blocos de qualidade da base, recomendações e custo médio;
   - no usuário comum, `Proporção por modal` no slot lateral e mapa no fim da página;
   - mensagens de erro de upload aceitam links markdown vindos do backend;
7. validar tela `/perfil`:
   - card `Pagamento` clicável;
   - redireciona para `/contrate-um-plano`;
   - página exibe `Estamos construindo essa funcionalidade.`;
8. validar telas admin:
   - `/admin/dashboard`
   - `/admin/agentes/julia`
   - `/admin/agentes/cleiton`
   - `/admin/controle-usuarios`
   - `/admin/planos`
9. validar endpoint admin de franquia:
   - `/admin/api/cleiton-franquia/<franquia_id>/validacao`
10. validar onboarding e governança do plano Free:
   - novo cadastro local nasce com `franquia.limite_total` numérico;
   - novo cadastro Google nasce com `franquia.limite_total` numérico;
   - novo usuário `free` não aparece como ilimitado por ausência de limite;
   - a checagem deve ser feita na `Franquia`, não em `User.creditos`;
11. validar mensageria operacional Cleiton:
   - status `degraded`, `blocked` e `expired` retornam CTA com nome amigável do plano;
   - URL do CTA respeita `PLANOS_UPGRADE_URL`.

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
- cadastro novo Free falhando: verificar se a referência administrativa do plano Free está configurada em `/admin/planos`.
- links úteis ausentes no chat: pode ser resultado de filtro de relevância ou falha segura da busca web.
- CTA de upgrade incorreto: verificar `PLANOS_UPGRADE_URL` no ambiente e em `app.config`.

## Referência Principal

Antes de ampliar este guia, atualize primeiro o `README.md` da raiz.
