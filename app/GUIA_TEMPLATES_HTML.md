# Guia de Templates HTML

Este guia complementa o `README.md` principal com foco em estrutura visual e padrões de templates. Referência: Sprint 12 / produção `fa98317`.

## Princípios

- usar `base.html` como base única de navegação do shell principal;
- reutilizar o design system em `static/css/agentefrete-theme.css`;
- evitar estilos fora da paleta `--af-*`;
- manter contraste, legibilidade e responsividade;
- não replicar lógica de negócio complexa em template;
- não duplicar listas de navegação: o shell consome `app/shell_navigation.py`.

## Tema global (contrato atual)

Preferências: `system` | `dark` | `light`.

- persistência: `localStorage` chave `af-theme`
- aplicação: templates que herdam `base.html` (Login, Chat, Perfil, Planos, Fretes, Auditoria, Comparação, Feed, etc.)
- `system` acompanha `prefers-color-scheme`; mudança do SO reflete enquanto a preferência for `system`
- **não** existe opt-in por página (`af_theme_enabled` / `data-af-theme-enabled` removidos)
- controle de tema: partial `partials/af_theme_control.html` (sidebar + menu mobile)
- **fora do contrato:** `painel_admin/template_admin/base_admin.html` e páginas standalone como `/acesso-desktop`

## Responsividade

- Home e Login possuem correções mobile cobertas por teste
- referência operacional aproximada: 360–430 px de largura
- expectativa: sem overflow horizontal; menu mobile funcional; desktop/tablet preservados
- não inventar suporte a devices específicos além do validado

## Templates Principais

### `index.html`

- home de discovery público + cards de habilidade (Analisar / Auditar / Comparar)
- definição pública: “Assistente virtual especializado em logística”
- inclui `partials/app_global_header.html` e componente de chat discovery
- CTA público participa de `home_chat_cta_v1`
- conteúdo editorial está no `/feed`

### `chat_julia.html` / `julia_chat_operational.html`

Componente e superfície do chat operacional **AgenteFrete** (nome de arquivo técnico `julia`).

Estado atual esperado:

- labels, alt e placeholders: **AgenteFrete**
- campo de entrada em `textarea`; `Shift+Enter` quebra linha; `Enter` envia
- texto inicial depende do contexto (discovery vs operacional / CTA)
- histórico montado no cliente a partir do DOM; sem threads persistentes de produto
- sugestões clicáveis e markdown básico seguro nas respostas do assistente

### `fretes.html` + `roberto_bi.html`

Superfície autenticada (`/fretes`). Estado visual por perfil:

- admin autenticado: formulário de consulta por rota + BI completo
- usuário comum: foco em upload/BI; blocos de qualidade/recomendações/custo médio ocultos conforme regras existentes; mapa no final

### `chat_roberto_fretes.html`

Chat do Roberto em `/fretes` (balao próprio, separado do chat AgenteFrete).

### `cleide_auditoria.html` / `agente_compara.html`

Superfícies de habilidades sob o shell/`base.html` — respeitam o tema global.

### `user_area.html` + `contrate_plano.html`

- `/perfil`: Central de Segurança e Conta, proteção, credenciais, alteração de senha (fluxo de recuperação por e-mail), Conta, notificações, Multiuser e contratação — tema global
- `contrate_plano.html`: Free / Starter / Pro / Multiusuário
- Starter/Pro: checkout embedded
- ao selecionar Multiusuário: destrói/limpa checkout anterior, colapsa container, mostra formulário; checkout só após **Ir para o checkout**
- ViaCEP auxilia endereço Multiuser com fallback manual

### Gestão Multiuser

`/gestao-multiuser` do contratante ativo. Regras em [Multiuser V1](../docs/multiuser_v1.md).

### `noticia_interna.html` / `feed.html`

- detalhe em `/noticia/<id>` com SEO, meta description, alt, JSON-LD e CTA contextual de habilidade quando aplicável
- intenções editoriais: news / analysis / evergreen
- bloco editorial da persona Júlia (Editora) permanece no conteúdo público; isso não redefine a identidade do chat operacional
- Feed: coluna única, misturado, cronológico, limite de 5 itens

### `acesso_desktop.html`

Landing de campanha opcional/legada. Standalone (não herda `base.html`). **Não** é gate técnico nem requisito de uso do AgenteFrete.

### `login.html`

Autenticação com preservação de `next` interno seguro; tema global via `base.html`; layout mobile corrigido.

## Checklist de Frontend

- o template do shell estende `base.html` e respeita `af-theme`
- layout não replica sidebar/navbar manualmente
- chat operacional exibe identidade AgenteFrete
- páginas de notícia/artigo mantêm navegação clara de retorno
- Multiusuário não deixa iframe residual do checkout anterior

## Referência Principal

O [README](../README.md) e o [estado de produção](../docs/estado_producao.md) são a entrada; este arquivo foca padrão visual e estrutural.
