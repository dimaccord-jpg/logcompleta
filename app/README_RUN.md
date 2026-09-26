# Execução Local

Referência funcional da árvore de produção `fa98317` (Sprint 12). A visão oficial está em [arquitetura](../docs/arquitetura_oficial.md) e [estado de produção](../docs/estado_producao.md).

## Pré-requisitos

- executar a partir da raiz do projeto;
- ativar a `.venv` local quando aplicável;
- usar `APP_ENV=dev`, banco de desenvolvimento e definir `DATABASE_URL`, `SECRET_KEY`, `APP_DATA_DIR`, `INDICES_FILE_PATH` e `PUBLIC_BASE_URL`;
- `load_app_env()` carrega `app/.env.dev` com `override=False` — variáveis já definidas no processo prevalecem;
- homologação usa `homolog`/`APP_ENV=homolog`; produção usa `producao`/`APP_ENV=prod`, em serviços Render separados;
- instalar `requirements.txt` para executar o runtime; `pypdf` permanece dependência do conversor local de PDF.

## Comandos principais

```powershell
.\.venv\Scripts\python.exe -m flask --app app.web db upgrade
.\.venv\Scripts\python.exe -m flask --app app.web run
```

```powershell
.\.venv\Scripts\python.exe -m pytest -q
```

Há cobertura automatizada relevante para tema global, identidade AgenteFrete, gate de `/fretes`, mobile Home/Login, evergreen, modelo de imagem e checkout Multiusuário. Não trate contagens globais de testes como verdade permanente em guias vivos.

## Pontos de validação manual

1. home pública: discovery AgenteFrete, cards de habilidade e CTA `home_chat_cta_v1`;
2. tema global (`system`/`dark`/`light`) em páginas do shell;
3. autenticação e redirecionamento seguro (`next` interno; rejeição de externo);
4. `/chat_julia?mode=operational` com identidade AgenteFrete;
5. `/auditoria-frete`;
6. `/agente-compara` com jornada completa;
7. `/fretes` (exige login);
8. `/feed` e `/noticia/<id>` (SEO / intenções);
9. `/contrate-um-plano` (Starter/Pro embedded; Multiusuário limpa checkout anterior);
10. `/perfil` (segurança, conta, notificações, tema);
11. `/acesso-desktop` (opcional; não é gate);
12. `/politica-de-privacidade` e `/termos-de-uso`;
13. `/health`, `/health/liveness` e `/health/readiness`;
14. `/admin/*` autorizado (incl. evergreen editorial);
15. `/cron/*` apenas com segredo válido;
16. Multiuser conforme [guia vigente](../docs/multiuser_v1.md).

`tests/conftest.py` força SQLite em memória. Para validar Multiuser, selecionar testes relacionados e conferir mocks Stripe, sem credenciais de produção.

## Observações importantes

- o banco oficial continua sendo PostgreSQL via `DATABASE_URL`;
- o schema é governado pelas migrations Alembic existentes;
- o head versionado atual é `g8h9i0j1k2l3`, com `down_revision=f7g8h9i0j1k2`;
- a migration `z0a1b2c3d4e5_home_cta_experiment_event.py` adiciona `home_cta_experiment_event`;
- `playwright` existe apenas em `requirements-dev.txt`;
- os testes versionados não são executados automaticamente pelo `build.sh` do deploy;
- divergências antigas de `flask db check` em constraints/índices são drift histórico;
- `.db` locais, caches JSON, `app/indices.json` e artefatos em `app/cleiton_doc_tmp/` não fazem parte do deploy;
- templates oficiais `.xlsx` permanecem versionados por exceção do `.gitignore`.
