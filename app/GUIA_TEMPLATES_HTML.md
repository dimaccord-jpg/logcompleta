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

- home do portal;
- inclui `chat_julia.html`;
- inclui listas de notícias e artigos;
- usa hero, ticker e newsletter.

### `chat_julia.html`

Componente oficial do chat da Júlia.

Estado atual esperado:

- campo de entrada em `textarea`;
- `Shift+Enter` para quebra de linha e `Enter` para envio, controlados no JS;
- não exibe dica visual de teclado;
- mensagem de boas-vindas exibida:
  - `Faça uma pergunta sobre logística, fretes, supply chain ou indicadores. Ex.: "Como o dólar impacta o frete?"`
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

### `user_area.html` + `contrate_plano.html`

- card `Pagamento` em `/perfil` é clicável;
- redireciona para `/contrate-um-plano`;
- template `contrate_plano.html` mostra estado provisório de funcionalidade em construção.

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

Mudanças funcionais e comportamento do sistema devem ser registrados primeiro no `README.md` da raiz.  
Este arquivo deve focar apenas em padrão visual e estrutural.
