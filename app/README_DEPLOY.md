# Deploy e Promoção

Referência: `homolog@6701a53` promovido em `producao@0c3a133` em 2026-07-16. Backup: `backup/producao-antes-cleide-insights-20260716`.

## Branches

- `homolog`: desenvolvimento, push e validação de homologação;
- `producao`: branch operacional de produção;
- `main`: branch padrão remota; não é produção operacional nesta fotografia.

O `render.yaml` versionado ainda declara produção em `main` e `autoDeploy: false`. Trate isso como divergência a validar no painel, não como autorização para promover em `main`.

## Promoção

1. Em `homolog`, confirmar worktree, revisar diff e limpar resíduos após inspeção.
2. Verificar se mudanças de schema têm migrations; esta entrega não teve migration, tabela ou coluna.
3. Executar a suíte completa e validação manual mínima.
4. Fazer commit e push para `homolog` somente após aprovação do trabalho.
5. Validar deploy/health e fluxos no Render de homologação.
6. Trocar para `producao`, atualizar referências remotas e criar `backup/producao-antes-<entrega>-<data>`.
7. Fazer merge de `homolog` com `--no-ff`; resolver conflitos por arquivo, sem escolher um lado em massa.
8. Executar novamente a suíte completa após o merge.
9. Fazer push de `producao`, validar build, migration chain, `/health`, login, agentes, billing e observabilidade.
10. Voltar para `homolog`.

Não execute commit, push, merge ou deploy durante uma atualização apenas documental sem autorização explícita.

## Limpeza e schema

Não publique `.venv`, `.tmp_pytest_fixture`, `app/cleiton_doc_tmp`, `tt_*.json`, caches ou bancos locais. Inspecione qualquer JSON antes de excluir. Mudança futura de schema deve ter migration no mesmo deploy, aplicada antes do código dependente e verificada depois.

Fotografia validada: 1.896 testes aprovados em `6701a53`; execute `python -m pytest -q` e registre a contagem real do commit promovido.
