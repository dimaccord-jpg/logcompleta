# Changelog Consolidado

Data de consolidacao: `2026-05-26`

Este changelog consolida o estado funcional e operacional atualmente promovido no projeto. Ele nao substitui o historico de `git`, mas registra o que ja deve ser tratado como comportamento oficial.

## Consolidado vigente

### Arquitetura e governanca

- arquitetura oficial consolidada entre Roberto, Cleiton, Julia e Cleide
- reforco da regra de nao criar rotas paralelas, bypasss ou fallback mascarando erro
- runtime de ambiente formalizado com `APP_ENV` obrigatorio e trilha de observabilidade rastreavel

### Julia editorial

- artigos em fallback de redacao passaram a ser bloqueados antes de imagem e publicacao
- metadados oficiais de redacao consolidados: `redacao_status`, `redacao_fallback`, `redacao_motivo`
- observabilidade de imagem consolidada em `assets_canais_json`

### Imagem e retencao

- persistencia de imagem movida do legado `app/static/generated` para `settings.data_dir/generated`
- superficie publica consolidada em `/media/generated/`
- retencao corrigida para nao sobrescrever `NoticiaPortal.url_imagem`
- fallback visual read-only preserva patrimonio editorial publicado

### Editorial e publicacao

- TTL editorial oficial consolidado em 5 dias para `rss`, `api` e `import_legacy`
- pauta manual e serie editorial permanecem fora do bloqueio de TTL
- reprocessamento elegivel mantido em `status=pendente` e `arquivada=False`
- despublicacao editorial consolidada em `POST /admin/noticias/<id>/despublicar`

### Share e SEO

- compartilhamento social consolidado para Facebook, Threads, X, LinkedIn e WhatsApp
- contrato publico consolidado: `canonical == og:url == share_url_abs`
- `PUBLIC_BASE_URL` consolidado como fonte de verdade entre homolog e producao
- share publico explicitamente sem IA, sem billing e sem pipeline

### Cleide

- namespace operacional oficial consolidado
- upload, template, status, clear, filtro e chat controlado consolidados
- fallback governado documentado para erro de provider, fora de escopo e bloqueio semantico
- admin Cleide consolidado com controles de contexto e upload

### Admin e observabilidade

- bloco visual Customer Insight ocultado no dashboard
- backend do insight preservado
- separacao de metricas entre Roberto e Cleide consolidada no payload de IA

### Testes

- consolidacao validada com `434 passed`

## Documentacao consolidada neste marco

- `README.md`
- `docs/estado_oficial_consolidado.md`
- `docs/arquitetura_oficial.md`
- `docs/onboarding_tecnico.md`
- `docs/runtime_ia_e_observabilidade.md`
- `docs/troubleshooting_operacional.md`
- `docs/guia_de_mkt.md`
