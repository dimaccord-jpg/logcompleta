# Guia de MKT

Revisão documental: `2026-09-23` (Sprint 12)

Este documento resume posicionamento institucional, SEO e compartilhamento social do AgenteFrete. Complementa o `README.md`, mas não substitui a documentação técnica principal.

## 1. Visão geral

**AgenteFrete** é o assistente virtual especializado em logística da LogCompleta — identidade pública única. Nomes internos (Júlia, Roberto, Cleide, Cleiton, AgenteCompara) não devem ser apresentados como marcas/produtos separados.

Capacidades (habilidades do AgenteFrete):

- Conversar / Consultar o AgenteFrete — chat operacional autenticado
- Analisar fretes — BI Roberto em `/fretes`
- Auditar cobranças de frete — AgenteAudita
- Comparar tabelas — AgenteCompara
- Feed editorial — informação + aquisição + recorrência

Cleiton governa discovery, franquias, billing e orquestração. Multiuser V1 está em produção; não anunciar memória compartilhada entre membros nem API pública Multiuser. Ver [AGENTS](AGENTS.md) e [Multiuser V1](multiuser_v1.md).

O produto **não** é TMS, WMS nem ERP.

## 2. Proposta de valor

- inteligência de dados para logística e transporte
- conteúdo especializado com superfície pública rastreável
- apoio operacional governado, sem promessas genéricas de IA

## 3. Tom recomendado

- claro, técnico sem excesso, confiável, orientado a decisão
- evitar promessas exageradas e discurso desconectado da operação real

## 4. Contrato de SEO do conteúdo editorial

Base: `PUBLIC_BASE_URL`; template usa `share_url_abs`. `index.html` ainda pode fixar domínio de produção em seu bloco canonical — isso não é garantia universal para todas as páginas.

Ambientes:

- produção: `https://www.agentefrete.com.br`
- homolog: host homolog configurado no ambiente

Contrato obrigatório em URLs editoriais:

- `canonical == og:url == share_url_abs`

## 5. Pipeline editorial e intenções

Intenções explícitas (não apenas heurística):

| Intenção | Uso |
|---|---|
| `news` | notícia rápida / atualidade |
| `analysis` | análise / aplicação prática |
| `evergreen` | conteúdo perene orientado a intenção de busca |

Elementos do pipeline:

- metadata editorial
- SEO / meta description
- alt de imagem
- JSON-LD (`NewsArticle` vs `Article` conforme apresentação)
- CTA contextual quando há habilidade real mapeada
- URLs públicas: `/noticia/<id>`
- Feed `/feed`: informação + aquisição + recorrência

### Evergreen (operação)

- admin editorial (Júlia) permite Analysis / Evergreen e pauta explícita
- `pauta_id` explícito preservado; inválido = fail-closed
- `evergreen_automatico_habilitado` e `evergreen_frequencia_minutos` em `ConfigRegras`
- cadência independente do ciclo legado; ver [estado de produção](estado_producao.md)

### Imagem editorial

- modelo principal: `gemini-3.1-flash-image` via `generate_content`
- não documentar `imagen-3.0-generate-002` como default atual
- fallback permanece disponível; valor remoto no Render é a fonte de verdade

## 6. Superfície pública atual

Páginas públicas relevantes:

- `/`
- `/feed`
- `/noticia/<id>`
- `/acesso-desktop` (campanha opcional/legada; **não** fluxo principal nem gate)

`/fretes` é autenticada — fora da superfície SEO pública.

Fora da superfície pública de SEO:

- `/fretes`, `/gestao-multiuser`, `/admin/...`, `/perfil`, `/contrate-um-plano`
- `/api/...`, `/cron/...`, `/ops/...`, `/login`, `/logout`, `/register`

## 7. Compartilhamento social

- conteúdo público em `app/templates/noticia_interna.html`
- partial `app/templates/partials/social_share.html`
- redes: Facebook, Threads, X, LinkedIn, WhatsApp (`https://api.whatsapp.com/send`)
- share público não usa IA, não gera `IaConsumoEvento`, não faz billing e não dispara pipeline

A persona editorial “Júlia, Editora Virtual” pode aparecer no artigo; isso não redefine a identidade do chat operacional (AgenteFrete).

## 8. Checklist de validação

- `PUBLIC_BASE_URL` correto no ambiente
- canonical / `og:url` / `share_url_abs` alinhados
- links de share funcionando
- conteúdo público `200`; despublicado `404`
- intenções news/analysis/evergreen coerentes com o conteúdo
- CTA de habilidade só quando o destino existe

## 9. Resumo executivo

AgenteFrete é a marca pública; o Feed editorial (news/analysis/evergreen) sustenta SEO e recorrência; habilidades operacionais exigem autenticação; `/acesso-desktop` é campanha opcional.
