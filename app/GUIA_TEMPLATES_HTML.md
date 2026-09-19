# Guia de Templates HTML

Este guia complementa o `README.md` principal com foco apenas em estrutura visual e padrões de templates.

## Princípios

- usar `base.html` como base única de navegação;
- reutilizar o design system em `static/css/agentefrete-theme.css`;
- evitar estilos fora da paleta `--af-*`;
- manter contraste, legibilidade e responsividade;
- não replicar lógica de negócio complexa em template.

## Templates Principais

### `index.html`

- home de discovery público ou superfície operacional autenticada conforme contexto;
- inclui `partials/app_global_header.html` e `chat_julia.html`;
- CTA público participa de `home_chat_cta_v1`;
- conteúdo editorial está no `/feed`, não em uma lista de notícias deste template.

### `chat_julia.html`

Componente oficial do chat da Júlia.

Estado atual esperado:

- campo de entrada em `textarea`;
- `Shift+Enter` para quebra de linha e `Enter` para envio, controlados no JS;
- não exibe dica visual de teclado;
- texto inicial depende da superfície autenticada/discovery e de `home_cta_text`; não fixar uma única mensagem para todos os contextos
- suporta sugestões clicáveis;
- renderização visual preparada para markdown básico seguro da Júlia;
- mensagens do usuário continuam simples e puras.

### `fretes.html` + `roberto_bi.html`

Estado visual por perfil:

- admin autenticado:
  - vê formulário de consulta por rota (`UF + Cidade`);
  - vê o módulo BI completo;
- usuário comum autenticado:
  - não vê o formulário de consulta por rota;
  - experiência foca no upload/BI;
  - blocos `Qualidade da base analisada`, `Recomendações` e `Custo médio (período)` ficam ocultos;
  - `Proporção por modal` ocupa o slot lateral;
  - `Mapa Brasil` aparece no final em card dedicado e maior.

Comportamento visual complementar:

- mensagens de erro de upload aceitam links markdown simples vindos do backend;
- links exibidos devem abrir em nova aba com `rel="noopener noreferrer"`.

### `chat_roberto_fretes.html`

Componente oficial do chat do Roberto na tela `/fretes`.

Estado visual atual esperado:

- balao flutuante proprio, separado visualmente da Julia;
- mensagem inicial orientativa antes do upload:
  - `Realize o upload do arquivo para que possamos analisa-lo juntos.`
- mensagens do Roberto exibem acao discreta de `Copiar`;
- feedback visual curto apos copia:
  - `Copiado`
- a copia deve atuar apenas sobre o texto ja renderizado da resposta;
- acoes visuais deste componente nao devem introduzir chamada extra de rede nem telemetria paralela.

### `user_area.html` + `contrate_plano.html`

- card `Pagamento` em `/perfil` é clicável;
- redireciona para `/contrate-um-plano`;
- `contrate_plano.html` oferece planos Free, Starter, Pro e Multiuser, resumo de contratação e início de Checkout Stripe;
- Multiuser coleta dados empresariais, CNPJ e quantity antes do checkout;
- perfil apresenta acesso à gestão Multiuser conforme autorização;
- notificações possuem limitações de UX registradas na SCRUM-187: [estado de produção](../docs/estado_producao.md#pendências-conhecidas-e-pós-release).

### Gestão Multiuser

O painel `/gestao-multiuser` é do contratante ativo. Visibilidade de botões não substitui autorização no backend. Membros não compartilham documentos, chats ou resultados apenas por pertencerem à mesma Conta. Regras em [Multiuser V1](../docs/multiuser_v1.md).

### `noticia_interna.html`

Template oficial de detalhe de notícia/artigo.

Estado atual esperado:

- conteúdo editorial em superfície legível;
- separação clara entre insight rápido e artigo;
- bloco “Análise da Editora”;
- botão final de navegação com:
  - destino para `index`
  - texto `Voltar Para Home`
  - estilo alinhado à paleta principal do site

## Padrões de Conteúdo Rico

Para HTML vindo do banco:

- usar superfície de leitura adequada;
- preservar contraste no tema dark;
- não introduzir cores inline arbitrárias;
- quando houver botões ou CTAs, preferir classes do tema já existente.

## Checklist de Frontend

- o template estende `base.html`;
- o layout não replica sidebar/navbar manualmente;
- botões têm rótulos claros e atuais;
- chat da Júlia continua consistente com o comportamento documentado no `README.md`;
- páginas de notícia/artigo mantêm navegação clara de retorno.

## Referência Principal

O [README](../README.md) é o índice; regras funcionais ficam nos guias canônicos de cada domínio.
Este arquivo deve focar apenas em padrão visual e estrutural.
