# Guia de MKT

Revisão documental: `2026-09-19`

Este documento resume posicionamento institucional, SEO e compartilhamento social do Agentefrete. Ele complementa o `README.md`, mas nao substitui a documentacao tecnica principal.

## 1. Visao geral

AgenteFrete reúne as superfícies descritas em [agentes e identidades](AGENTS.md):

- AgenteFrete/Júlia operacional: chat autenticado e contexto documental; Júlia também mantém funções editoriais.
- Cleiton: governança, discovery, franquias, billing e observabilidade.
- AgenteAudita (Cleide no código): auditoria de fretes, BI e chat analítico.
- AgenteCompara: comparação de duas ou três tabelas fornecidas pelo usuário.
- Roberto: BI de fretes e chat quantitativo em `/fretes`.

Multiuser V1 está em produção, com contratação Stripe e franquias individuais. Não anunciar compartilhamento de memória, arquivos ou resultados entre membros, API pública Multiuser ou contratação automática de transportadoras. Ver [Multiuser V1](multiuser_v1.md).

## 2. Proposta de valor

Mensagem central:

- inteligencia de dados para logistica e transporte
- conteudo especializado com superficie publica rastreavel
- apoio operacional governado, sem promessas genericas de IA

## 3. Tom recomendado

- claro
- tecnico sem excesso
- confiavel
- orientado a decisao

Evitar:

- promessas exageradas
- linguagem vaga de IA
- discurso desconectado da operacao real

## 4. Contrato de SEO do conteúdo editorial

Para URLs editoriais de notícia/artigo, a base é `PUBLIC_BASE_URL` e o template usa `share_url_abs`. Isso não é garantia universal para todas as páginas: `index.html` ainda fixa o domínio de produção em seu bloco canonical.

Ambientes:

- producao: `https://www.agentefrete.com.br`
- homolog: host homolog configurado no ambiente

Contrato obrigatorio:

- `canonical == og:url == share_url_abs`

Regras:

- canonical deve refletir a URL publica efetiva do ambiente
- `og:url` deve refletir a mesma URL
- qualquer URL absoluta usada em share deve seguir essa base
- paginas privadas e operacionais nao entram na superficie SEO publica

## 5. Superficie publica atual

Paginas publicas relevantes:

- `/`
- `/feed`
- `/noticia/<id>`

`/fretes` é uma superfície autenticada de BI, não uma página editorial pública.

Paginas que devem permanecer fora da superficie publica de SEO:

- `/fretes`
- `/gestao-multiuser`
- `/admin/...`
- `/perfil`
- `/contrate-um-plano`
- `/api/...`
- `/cron/...`
- `/ops/...`
- `/login`
- `/logout`
- `/register`

## 6. Compartilhamento social

Escopo atual:

- conteudo publico da Julia em `app/templates/noticia_interna.html`
- partial oficial `app/templates/partials/social_share.html`

Redes suportadas:

- Facebook
- Threads
- X
- LinkedIn
- WhatsApp

Contrato de WhatsApp:

- `https://api.whatsapp.com/send`

Regras de governanca:

- share publico nao usa IA
- share publico nao gera `IaConsumoEvento`
- share publico nao faz billing
- share publico nao dispara pipeline

## 7. Checklist de validacao

- `PUBLIC_BASE_URL` correto no ambiente
- canonical correto
- `og:url` correto
- `share_url_abs` igual ao canonical
- links de Facebook, Threads, X, LinkedIn e WhatsApp funcionando
- conteudo publico responde `200`
- conteudo despublicado responde `404`

## 8. Resumo executivo

O marketing do Agentefrete deve comunicar valor real de logistica e transporte, preservando o contrato tecnico de share e SEO por ambiente. A regra mais importante e simples: a superficie publica precisa ser canonica, rastreavel e separada do runtime de IA e billing.
