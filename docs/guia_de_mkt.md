# Guia de MKT

Este documento consolida, em um único lugar, o material de SEO do site e o resumo institucional/comercial do Agentefrete para campanhas, redes sociais e comunicação digital.

Ele complementa o `README.md`, mas não substitui o documento principal do projeto.

## 1. Visão Geral do Agentefrete

O Agentefrete é uma plataforma de inteligência de dados aplicada à logística e ao transporte. O projeto combina análise de fretes, leitura de tendências, conteúdo especializado e recursos com inteligência artificial para ajudar empresas e profissionais a tomarem decisões mais informadas no dia a dia da operação logística.

Na prática, o Agentefrete conecta três frentes principais:

- conteúdo e inteligência editorial com a Julia
- governança operacional e trilho técnico com o Cleiton
- análise de fretes, BI e contexto analítico com o Roberto

## 2. Proposta de Valor

O Agentefrete ajuda transportadores, embarcadores, operadores logísticos, times comerciais e gestores de supply chain a transformar dados em contexto prático para decisão.

Benefícios centrais:

- entender custos logísticos com mais clareza
- identificar padrões e tendências no transporte
- apoiar negociações com mais evidência
- ampliar a leitura estratégica sobre frete e operação
- unir conteúdo, dados e inteligência aplicada em um mesmo ambiente

## 3. Resumos Prontos de Posicionamento

### Resumo curto

O Agentefrete é uma plataforma de inteligência logística que usa dados, análise de fretes e IA para apoiar decisões no transporte e na gestão de custos.

### Resumo médio

O Agentefrete é uma plataforma voltada para logística e transporte que combina inteligência de dados, análise de fretes, conteúdo estratégico e recursos com IA. A proposta é ajudar empresas e profissionais a entender melhor custos, padrões operacionais e tendências do setor para tomar decisões mais seguras e eficientes.

### Resumo institucional

O Agentefrete nasceu para aproximar dados e decisão no setor logístico. Em vez de tratar o frete apenas como custo isolado, a plataforma organiza contexto analítico, conteúdo estratégico e inteligência aplicada para apoiar quem precisa operar, negociar e planejar com mais clareza. O resultado é uma experiência que conecta leitura de mercado, análise histórica, visão operacional e apoio inteligente para logística, transporte e supply chain.

## 4. Posicionamento para Campanhas

Mensagens que representam bem o projeto:

- inteligência de dados para logística
- análise de fretes com apoio de IA
- decisões logísticas com mais contexto
- dados históricos transformados em visão estratégica
- conteúdo e análise para o setor de transporte

## 5. Linhas de Comunicação

### Eficiência e decisão

Foco:

- redução de incerteza
- leitura de custos
- visão analítica da operação

Exemplo:

`Transforme dados de frete em decisões logísticas mais inteligentes.`

### Inteligência aplicada

Foco:

- uso prático de IA
- apoio à análise
- velocidade com contexto

Exemplo:

`Use inteligência artificial para entender padrões, custos e tendências no transporte.`

### Autoridade no setor

Foco:

- conteúdo especializado
- leitura de mercado
- posicionamento estratégico

Exemplo:

`Conteúdo, dados e análise para quem precisa decidir melhor na logística.`

## 6. Sugestões de Bio e Headlines

### Bio curta

`Inteligência de dados para logística, fretes e transporte.`

### Bio média

`Plataforma de inteligência logística com análise de fretes, conteúdo estratégico e IA aplicada ao transporte.`

### Bio comercial

`Dados, conteúdo e inteligência artificial para apoiar decisões em logística, transporte e supply chain.`

### Headlines

- `Análise de fretes com inteligência artificial`
- `Mais contexto para decisões logísticas`
- `Dados e IA para transporte e supply chain`
- `Entenda custos, padrões e tendências do frete`
- `Inteligência logística para quem decide no operacional`

## 7. Textos Prontos para Uso

### Post institucional

`O Agentefrete é uma plataforma de inteligência de dados para logística e transporte. Com análise de fretes, conteúdo estratégico e recursos com IA, ajudamos profissionais e empresas a entender custos, identificar padrões e acompanhar tendências para tomar decisões mais claras no dia a dia da operação.`

### Anúncio

`Analise fretes, entenda custos logísticos e visualize tendências com inteligência de dados aplicada ao transporte. Conheça o Agentefrete.`

### Apresentação comercial

`O Agentefrete reúne inteligência logística, análise de fretes, conteúdo estratégico e recursos com IA em uma plataforma criada para apoiar decisões mais eficientes no transporte e na gestão operacional.`

## 8. Tom Recomendado

Para campanhas e redes, o melhor tom para o Agentefrete é:

- claro
- técnico sem ser excessivamente denso
- confiável
- estratégico
- orientado a decisão

Evitar:

- promessas exageradas
- discurso genérico de IA
- linguagem vaga sem conexão com logística real

## 9. SEO do Site

O SEO do Agentefrete deve manter consistência entre rastreamento, indexação, canonicidade e conteúdo público indexável das páginas do site, com foco especial nas rotas públicas do domínio `https://www.agentefrete.com.br`.

### Domínio canônico

- domínio canônico oficial: `https://www.agentefrete.com.br`
- o Render redireciona `https://agentefrete.com.br/*` para `https://www.agentefrete.com.br/*`
- sinais públicos de SEO devem sempre usar a versão com `www`

Pontos alinhados:

- canonical
- `og:url`
- `og:image` fallback
- `robots.txt`
- `sitemap.xml`
- URLs públicas geradas por `_SEO_CANONICAL_ORIGIN`

### `robots.txt`

Origem:

- rota Flask em `app/web.py`

Estado esperado:

- sem bloqueio para `/fretes`
- bloqueios mantidos para rotas privadas, administrativas e operacionais
- linha `Sitemap:` apontando para `https://www.agentefrete.com.br/sitemap.xml`

### `sitemap.xml`

Origem:

- rota Flask em `app/web.py`
- template XML em `app/templates/sitemap.xml`

Estado esperado:

- home pública `/`
- página pública `/fretes`
- notícias publicadas `/noticia/<id>`

Páginas que não devem entrar no sitemap enquanto permanecerem como página genérica de pré-lançamento:

- `/auditoria-frete`
- `/controle-estoque`
- `/insights-frete`

### Canonical e Open Graph

Base pública:

- `app/templates/base.html`

Regras:

- canonical deve refletir a própria URL pública com `www`
- `og:url` deve usar a mesma URL canônica
- imagens públicas de fallback devem usar `https://www.agentefrete.com.br`

### JSON-LD

Hoje o JSON-LD está presente nas páginas de notícia em `app/templates/noticia_interna.html`.

Regra:

- qualquer URL ou imagem pública derivada do template deve usar a base canônica com `www`

## 10. Página Estratégica: `/fretes`

Importância:

- é a principal página pública de aquisição e descoberta orgânica do módulo Roberto
- conecta inteligência logística, histórico de fretes, análise de custos e tendência

Elementos esperados:

- `title` específico
- `meta description` específica
- `og:title` específico
- `og:description` específica
- canonical público com `www`
- exatamente um `H1`
- conteúdo textual SSR indexável no HTML inicial

Estado on-page esperado:

- `title`: `Análise de Fretes com Inteligência Artificial | Agentefrete`
- `meta description`: foco em custos, padrões logísticos, tendências e inteligência de dados
- bloco público introdutório explicando proposta de valor da análise de fretes

## 11. Regras Operacionais para SEO

Estas regras devem ser respeitadas em qualquer ajuste futuro:

- não criar rotas paralelas para SEO
- não mover `robots.txt` e `sitemap.xml` para outro trilho sem necessidade explícita
- não alterar lógica de autenticação por causa de indexação
- não alterar trilho de consumo por franquia
- não alterar billing, Stripe ou governança Cleiton por motivo de SEO
- não transformar páginas privadas em públicas para ganho de indexação
- não depender de JavaScript para entregar o conteúdo textual principal das páginas estratégicas

## 12. Checklist de Validação SEO

Antes de publicar mudanças relevantes de SEO, validar:

- `robots.txt` responde `200`
- `sitemap.xml` responde `200`
- canonical usa `https://www.agentefrete.com.br`
- `og:url` usa `https://www.agentefrete.com.br`
- a URL pública alvo responde `200` sem redirect inesperado no host canônico
- páginas públicas importantes estão linkadas internamente
- páginas privadas continuam fora do sitemap
- páginas públicas estratégicas têm conteúdo SSR legível no HTML inicial

## 13. Escopo Público Atual do Site

Páginas públicas relevantes:

- `/`
- `/fretes`
- `/noticia/<id>`

Páginas privadas ou operacionais que devem continuar fora da superfície SEO:

- `/admin/...`
- `/perfil`
- `/contrate-um-plano`
- `/api/...`
- `/cron/...`
- `/ops/...`
- `/login`
- `/logout`
- `/register`
- `/reset-password/...`
- `/request-password-reset`

## 14. Resumo Executivo Final

O marketing do Agentefrete deve trabalhar duas frentes em conjunto:

- posicionamento claro da plataforma como inteligência de dados para logística e transporte
- consistência técnica de SEO para garantir descoberta orgânica das páginas públicas estratégicas

Na prática, isso significa comunicar bem a proposta de valor do projeto sem perder a disciplina técnica de canonicidade, indexação, conteúdo SSR e preservação da arquitetura principal do sistema.
