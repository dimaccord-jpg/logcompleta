# Execução Local

Referência revisada em 2026-09-19. A visão funcional oficial está em `docs/arquitetura_oficial.md` e `docs/estado_producao.md`.

## Pré-requisitos

- executar a partir da raiz do projeto;
- ativar a `.venv` local quando aplicável;
- usar `APP_ENV=dev`, banco de desenvolvimento e definir `DATABASE_URL`, `SECRET_KEY`, `APP_DATA_DIR`, `INDICES_FILE_PATH` e `PUBLIC_BASE_URL`;
- homologação usa `homolog`/`APP_ENV=homolog`; produção usa `producao`/`APP_ENV=prod`, em serviços Render separados.

## Comandos principais

```powershell
.\.venv\Scripts\python.exe -m flask --app app.web db upgrade
.\.venv\Scripts\python.exe -m flask --app app.web run
```

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

## Pontos de validação manual

1. home pública e discovery do AgenteFrete, incluindo o CTA do experimento `home_chat_cta_v1`;
2. autenticação e redirecionamento seguro;
3. `/chat_julia?mode=operational`;
4. `/auditoria-frete`;
5. `/agente-compara` com jornada completa: start, upload das duas tabelas obrigatórias, terceira opcional, revisão por tabela, impostos globais, coverage opcional, arquivo operacional, confirmação explícita do cálculo, leitura do resultado, analytics comparativo, modal de memória de cálculo e chat contextual pós-READY;
6. `/fretes`;
7. `/politica-de-privacidade` e `/termos-de-uso`;
8. `/health`, `/health/liveness` e `/health/readiness`;
9. `/admin/*` em contexto autorizado, incluindo métricas, billing e leitura do experimento da home;
10. `/cron/*` apenas com segredo válido e em contexto controlado;
11. Multiuser conforme [guia vigente](../docs/multiuser_v1.md): contratação, convites/aceite, gestão e isolamento entre membros.

`tests/conftest.py` força SQLite em memória e substitui a `DATABASE_URL` herdada por destino inválido de isolamento. Para validar Multiuser, selecionar testes relacionados à mudança e conferir os mocks Stripe, sem usar credenciais de produção.

## Observações importantes

- o banco oficial continua sendo PostgreSQL via `DATABASE_URL`;
- o schema é governado pelas migrations Alembic existentes;
- o head atual é `f7g8h9i0j1k2`; cadeia em [Banco e Migrations](../docs/DATABASE_AND_MIGRATIONS.md);
- a migration `z0a1b2c3d4e5_home_cta_experiment_event.py` adiciona a tabela `home_cta_experiment_event` e não altera o schema isolado do AgenteCompara;
- `playwright>=1.40.0` existe apenas em `requirements-dev.txt` para testes e não deve ser tratado como dependência de execução nem do runtime de produção;
- os testes versionados são suporte de regressão e documentação executável, mas não são executados automaticamente pelo `build.sh` do deploy;
- divergências antigas de `flask db check` em constraints/índices de `cleiton_billing_apropriacao`, `franquia` e `multiuser_franquia_codigo` são preexistentes e devem ser tratadas como drift histórico, não como parte desta entrega documental;
- `.db` locais, caches JSON, `app/indices.json` e artefatos temporários em `app/cleiton_doc_tmp/` não fazem parte do deploy;
- templates oficiais `.xlsx` permanecem versionados por exceção do `.gitignore`.
