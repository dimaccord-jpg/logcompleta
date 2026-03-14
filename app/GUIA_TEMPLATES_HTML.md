## 📘 Guia de Templates HTML – Agentefrete

Este documento descreve **como estão estruturados hoje** os templates HTML do projeto Agentefrete e define **padrões para novos desenvolvimentos de frontend** (páginas, componentes e ajustes visuais).

Não é um passo-a-passo de migração de tema; é um **guia de arquitetura visual e de templates** alinhado com o código vigente.

---

## 1. Visão geral de layout e stack

- **Stack de frontend**:
  - **HTML + Jinja2** (templates Flask).
  - **Bootstrap 5** (CDN) e **Bootstrap Icons**.
  - **Fonte principal**: `Inter` (Google Fonts).
  - **Tema dark próprio** via `static/css/agentefrete-theme.css` (design system Agentefrete).

- **Layout base**:
  - Arquivo: `app/templates/base.html`.
  - Padrão:
    - `<head>` com Bootstrap, Bootstrap Icons, Google Fonts e o CSS `agentefrete-theme.css`.
    - Sidebar fixa à esquerda (desktop/tablet) e navbar/offcanvas móvel (mobile).
    - Container principal `#content` com o bloco Jinja `{% block content %}{% endblock %}`.
    - Área centralizada para **flash messages**.

- **Extensão padrão**:
  - Todos os templates de página devem **estender** `base.html`:

    ```jinja
    {% extends "base.html" %}

    {% block content %}
    <!-- conteúdo específico da página -->
    {% endblock %}
    ```

  - Não crie novos layouts base sem necessidade. Use `base.html` como única base de navegação global.

---

## 2. Design system e CSS global

- **Arquivo principal de tema**:
  - `static/css/agentefrete-theme.css` contém:
    - Tokens de design em `:root` (cores, raios de borda, transições, etc.).
    - Overrides de Bootstrap (cards, botões, forms, tabelas, badges) para tema escuro.
    - Estilos da sidebar, hero da home, efeitos de glow, glass e gradientes.

- **Diretriz de contraste (tema dark)**:
  - O tema escuro é global; por isso:
    - **Evite definir `color: #...` inline** para textos comuns (`p`, `li`, `span`, etc.).
    - Use o que já existe no design system:
      - `var(--af-text-primary)` para textos principais.
      - `var(--af-text-secondary)` / `var(--af-text-muted)` para textos de apoio.

- **Conteúdo editorial rico (HTML vindo do banco)**:
  - Para textos longos (ex.: artigos gerados pela Júlia, conteúdo em `noticia_interna.html`), **sempre** envolver em:
    - `af-readable-surface` no container (card/fundo).
    - `af-readable-content` no bloco de texto.
  - Essas classes garantem:
    - Tipografia consistente.
    - Cores com contraste adequado no tema dark.
    - Estilização segura de tags (`p`, `li`, `blockquote`, `td`, `th`, links, headings).

---

## 3. Templates principais do portal

### 3.1 `base.html` – layout e navegação

- Responsabilidades:
  - Define **estrutura global** (sidebar, navbar mobile, offcanvas).
  - Controla o comportamento de colapso da sidebar (`#sidebar` e `#content` com classe `active`).
  - Centraliza exibição de **flash messages** (alertas de sucesso/erro).

- Padrões para novas páginas:
  - Não replique a estrutura de sidebar/nav manualmente; **sempre** derive de `base.html`.
  - Qualquer novo template de página de app deve focar apenas em preencher `{% block content %}`.

### 3.2 `index.html` – home / notícias

- Arquivo: `app/templates/index.html`.
- Extende `base.html` e adiciona:
  - **Ticker de indicadores** (Dólar, Petróleo, BDI, FBX) no topo.
  - Hero com título “Agentefrete” e slogan.
  - Card de **newsletter** (formulário para `/inscrever-newsletter`).
  - Layout em duas colunas:
    - Esquerda: `noticias.html` (insights/notícias).
    - Direita: `artigos.html` (artigos de longo formato).

- Boas práticas:
  - O ticker é alimentado pelo backend (`indicadores` em `app/web.py`); não reimplementar lógica de formato de índices no template.
  - Para novas seções na home:
    - Reutilizar classes como `ri-card`, `af-glow`, `af-hero-home`.
    - Manter coerência com o layout existente (container + grid Bootstrap).

### 3.3 `noticias.html` e `artigos.html` – listas

- `noticias.html`:
  - Loop em `noticias` filtrando `tipo != 'artigo'`.
  - Exibe cards com título, resumo, fonte, data e links:
    - Detalhe interno (`url_for('detalhe_noticia', ...)`).
    - Fonte original (link externo).
  - Usa classes `card-noticia` e `ri-card` para hover e estilo.

- `artigos.html`:
  - Loop em `noticias` filtrando `tipo == 'artigo'`.
  - Lista em `list-group` com título, subtítulo e data.
  - Links levam para a mesma rota de detalhe de notícia.

- Padrão para novos blocos de lista:
  - Reutilizar `ri-card` como container principal.
  - Quando aplicável, usar `card-noticia` para cards clicáveis de conteúdo.
  - Manter a filtragem de dados no template somente para condições simples (`tipo`, etc.); lógica complexa deve ficar no backend.

### 3.4 `noticia_interna.html` – detalhe de notícia/artigo

- Extende `base.html` e:
  - Recebe `noticia` e `url_imagem_resolvida` do backend.
  - Header com:
    - Badge condicional:
      - `Artigo Estratégico` (quando `noticia.tipo == 'artigo'`).
      - `Insight Rápido` (para demais tipos).
    - Título principal e, se artigo, subtítulo em estilo premium.
    - Metadados (data, fonte).
  - Imagem de capa (quando `url_imagem_resolvida` existe).
  - Corpo:
    - Para artigos:
      - Renderização de `noticia.conteudo_completo | safe` dentro de um bloco com `af-readable-content`.
    - Para notícias:
      - Box de resumo com fundo claro.
  - Rodapé com:
    - Bloco “Análise da Editora” (avatar da Júlia, link para fonte original).
    - Se artigo, exibe `referencias` e `cta`/`objetivo_lead` quando existirem.

- Padrões para novos tipos de conteúdo:
  - Respeitar a separação **artigo vs. notícia** (estilo, badge, corpo).
  - Qualquer novo campo rico vindo do banco que contenha HTML deve seguir o padrão `af-readable-surface` + `af-readable-content`.

### 3.5 `login.html` – login / cadastro

- Extende `base.html`; layout em card centralizado:
  - Coluna esquerda (desktop):
    - Painel com ícone, branding Agentefrete e lista de benefícios.
  - Coluna direita:
    - Abas `ENTRAR` e `CRIAR CONTA` (tabs Bootstrap).
    - Formulário de login (e-mail/senha, link “Esqueci minha senha”, botão “Entrar com Google”).
    - Formulário de cadastro:
      - Campos de nome, e-mail corporativo, cargo, objetivo de uso, opt-in de newsletter.

- Padrões:
  - Reutilizar classes `af-login-card`, `af-login-sidebar`, `af-login-form` para consistência visual.
  - Qualquer ajuste de copy ou campos deve preservar:
    - Semântica de labels.
    - Uso de classes Bootstrap para form controls.

### 3.6 `fretes.html` – consulta de frete

- Extende `base.html` e é alimentado pela rota `/fretes`:
  - Recebe `indices` (histórico de índices) e `resultado` (resultado da inteligência de frete).
  - Usa Bootstrap para forms e cards, combinando com o tema Agentefrete.

- Padrões:
  - Quando precisar exibir dados de índices/tabelas, preferir:
    - `table` Bootstrap + estilos já definidos no CSS global.
    - Containers com classes de card do design system (`ri-card`, `af-glow`) quando fizer sentido.

### 3.7 `user_area.html` – área do usuário

- Extende `base.html` e:
  - Cabeçalho com:
    - Título “Área do Usuário”.
    - Botão de sair (`logout`).
    - Saudação com `current_user.full_name` ou e-mail.
  - Três cards principais:
    - **Segurança**, **Pagamento**, **Notificações** – todos com ícones, textos explicativos e bullet points.
  - Se `current_user.is_admin`:
    - Alerta adicional com atalho rápido para o Painel ADM.

- Padrões:
  - Novos cards/áreas na página devem seguir o mesmo padrão de card (ícone em círculo, título, subtítulo, lista de pontos).
  - Não duplicar lógica de permissão; uso de `current_user.is_admin` é suficiente para visibilidade básica no template.

### 3.8 Templates auxiliares de autenticação

- `complete_profile.html`:
  - Extende `base.html`, usado após login via Google para completar perfil:
    - Campos de cargo, objetivo de uso e newsletter.

- `request_reset.html` e `reset_password.html`:
  - Extendem `base.html`.
  - Formulários simples para fluxo de “esqueci minha senha”/“redefinir senha”.

- Padrões:
  - Manter estes templates **simples** e alinhados com tema dark.
  - Reutilizar classes de formulário e botões já usadas em `login.html`.

---

## 4. Convenções para novos templates

- **Extensão e blocos**:
  - Sempre usar:

    ```jinja
    {% extends "base.html" %}
    {% block content %}
    <!-- conteúdo aqui -->
    {% endblock %}
    ```

  - Evitar criar novos blocks Jinja globais em `base.html` sem discussão, para não fragmentar a estrutura.

- **CSS**:
  - Preferir:
    - Classes já existentes no `agentefrete-theme.css`.
    - Classes Bootstrap padrão (`card`, `btn`, `form-control`, `table`, `badge`).
  - Evitar:
    - CSS inline complexo.
    - Novas cores fora da paleta definida nos tokens (`--af-*`).

- **Conteúdo rico / HTML dinâmico**:
  - Quando renderizar HTML vindo do banco (`| safe`):
    - Envolver o bloco em container com `af-readable-surface`.
    - Envolver o texto em `af-readable-content`.

- **Acessibilidade e responsividade**:
  - Usar grid Bootstrap (`container`, `row`, `col-*`) para responsividade.
  - Garantir que botões e links tenham textos claros, não apenas ícones.
  - Manter ícones com `aria-hidden="true"` quando forem puramente decorativos, e usar textos visíveis para o usuário.

---

## 5. Checklist para novas páginas/ajustes

Antes de abrir PR com um novo template ou alteração visual relevante:

- **Layout**:
  - [ ] O template estende `base.html` e usa apenas `{% block content %}`.
  - [ ] A navegação (sidebar, navbar mobile) não foi replicada manualmente.

- **Tema / CSS**:
  - [ ] Não foram adicionadas cores inline que conflitem com o tema escuro.
  - [ ] Classes do `agentefrete-theme.css` foram reaproveitadas sempre que possível.
  - [ ] Conteúdo rico vindo do banco usa `af-readable-surface` + `af-readable-content`.

- **UX / Conteúdo**:
  - [ ] Botões têm rótulos claros (ex.: “Ver Insight”, “Voltar ao Portal LogTech”).
  - [ ] Listas de conteúdo seguem o padrão de cards/`list-group` já utilizado.

- **Integração com backend**:
  - [ ] O template não replica lógica de negócios complexa; recebe dados já preparados pela view.
  - [ ] Nomes de campos usados em formulários e loops (`noticias`, `resultado`, etc.) estão alinhados com as rotas em `app/web.py`.

Este guia deve ser atualizado sempre que novos componentes visuais globais forem criados ou quando houver mudanças estruturais em `base.html`, `index.html` ou no design system em `agentefrete-theme.css`.

