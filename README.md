# Log Completa

Aplicação web Flask com módulos editoriais (Júlia), gerencial (Cleiton), inteligência de fretes (Roberto) e área do usuário.

## Ambientes e documentação operacional

| Ambiente | `APP_ENV` | Arquivo carregado pelo app | PostgreSQL (referência operacional) |
|----------|-----------|-----------------------------|-------------------------------------|
| Desenvolvimento local | `dev` | `app/.env.dev` | **Neste repositório, o estado local validado usa PostgreSQL 18** como host do banco de dados de trabalho. Homologação e produção **não** usam essa máquina. |
| Homologação | `homolog` | `app/.env.homolog` | **PostgreSQL 16** (serviço gerenciado ou servidor dedicado). |
| Produção | `prod` | `app/.env.prod` | **PostgreSQL 16** (serviço gerenciado ou servidor dedicado). |

- **`APP_ENV` é obrigatório no processo antes do boot** (variável de ambiente do shell, systemd, painel do host, etc.). O arquivo `app/.env.{APP_ENV}` **não** é lido antes dessa checagem; portanto, definir só `APP_ENV` dentro do `.env` **não** substitui exportar na sessão. **Verificação imediata na sessão:** PowerShell `echo $env:APP_ENV` — deve imprimir `dev`, `homolog` ou `prod` (não deve ficar vazio antes de subir o app). Bash: `echo $APP_ENV`. Detalhes: [`app/README_RUN.md`](app/README_RUN.md), [`app/.env.example`](app/.env.example).

- **`app/.env.example` é apenas modelo** (versionado, sem segredos reais). A execução usa `app/.env.dev`, `app/.env.homolog` ou `app/.env.prod` conforme `APP_ENV`, não o `.env.example`.

- **Risco operacional (local):** mais de um cluster PostgreSQL na mesma máquina pode escutar **a mesma porta** (ex.: 5432). A URI pode ter `localhost:5432` e o nome do banco “certos” e, ainda assim, o **serviço PostgreSQL que está escutando** essa porta ser o cluster errado (incidente validado: PG 16 vazio vs PG 18 com dados). O nome do banco na URL **não prova** instância. Guia de validação e troubleshooting: [`app/README_RUN.md`](app/README_RUN.md).

## Banco de dados

- **Um único PostgreSQL** em cada ambiente, configurado por `DATABASE_URL` (ver `app/.env.example` e `app/env_loader.py`). A aplicação usa um único engine SQLAlchemy; não há bind separado para localidades.
- Tabelas de domínio — incluindo `base_localidades`, `frete_real`, usuários e editoriais — coexistem nesse banco.
- Não há arquitetura multibanco nem uso de SQLite para dados da aplicação (a stack rejeita `DATABASE_URL` que não seja PostgreSQL).

## Roberto Intelligence (BI de fretes)

- **Histórico de referência (“base ouro”):** registros em `frete_real` no mesmo PostgreSQL; colunas de UF já persistidas na tabela quando aplicável.

### Upload de planilha (.xlsx)

- **Onde:** `app/upload_handler.py` (exposto em `POST /api/roberto/upload` em `app/web.py`). O arquivo é gravado em disco só durante a leitura e é **removido em seguida**. Não há persistência de linhas da planilha em tabela de negócio: o resultado fica **apenas na sessão Flask** (efêmero, TTL em `app/upload_handler.py`), com amostragem opcional por mês antes de guardar na sessão.

#### Fluxo atual de processamento (localidades)

1. **Leitura e validação:** cabeçalho e colunas obrigatórias; as linhas de dados são **materializadas em lista** (`read_only` no openpyxl só permite consumir o stream da aba **uma vez** — a lista viabiliza duas passagens lógicas sem reabrir o arquivo).
2. **Pré-coleta de chaves:** percorre as linhas e monta um **conjunto de chaves únicas** no formato `cidade-uf` (origem e destino), usando a mesma normalização de texto de sempre (`strip` + `lower` nas células, depois concatenação `"{cidade}-{uf}"`). **Regra de negócio inalterada:** cada linha continua representando um serviço; **não há deduplicação de linhas** no resultado — apenas deduplicação de valores de chave **para montar o conjunto** enviado ao banco.
3. **Consulta em lote:** uma única query a `base_localidades` via `carregar_localidades_por_chaves` em `app/infra.py`: `SELECT id_cidade, id_uf, cidade_nome, uf_nome, chave_busca FROM base_localidades WHERE chave_busca IN (...)` (lista expandida com `bindparam(..., expanding=True)` no SQLAlchemy). **Não há consulta ao banco por linha** no loop principal do upload.
4. **Mapa em memória:** dicionário `chave normalizada →` mesmo payload de localidade que antes vinha de consulta unitária (`id_cidade`, `id_uf`, `cidade_nome`, `uf_nome`, `chave_busca`).
5. **Loop por linha:** resolve origem e destino **somente** com `dict.get` no mapa; mensagens de erro por linha (localidade não encontrada, data inválida, etc.) permanecem equivalentes ao comportamento anterior.

#### Motivação técnica (performance)

- **Problema anterior:** resolver localidade com uma (ou duas) consultas **por linha** gerava padrão **N+1**, pressão no pool de conexões e, com o predicado antigo na coluna, tendência a **Seq Scan** em `base_localidades` — em arquivos grandes isso contribuía para **timeout de worker** (Gunicorn/uWSGI).
- **Solução aplicada:** **uma** consulta em lote com **igualdade direta** em `chave_busca` (uso de índice/PK) + **mapa em memória** no processamento. Não reintroduzir consulta por linha no upload: volta o custo N+1 e o risco de timeout.

### Localidades no PostgreSQL

- **Um único banco:** `base_localidades` coexiste com as demais tabelas no mesmo PostgreSQL (`DATABASE_URL`). **Não existe segundo banco** só para localidades.
- **Formato da chave:** `cidade-uf` em **minúsculas** (ex.: `cariacica-es`), alinhado a `gerar_chave_busca` / coluna `chave_busca`. Os dados em `chave_busca` são mantidos **normalizados** (minúsculas, sem espaços nas bordas). O código aplica **`strip()` e `lower()` na entrada** da planilha antes de montar a chave e antes de comparar.
- **Lookup unitário (outros fluxos):** `get_localidade_completa_por_chave` em `app/infra.py` usa `WHERE chave_busca = :c` com parâmetro já normalizado — **sem** `LOWER` nem `TRIM` na coluna na SQL, o que permite ao planejador usar o índice/PK. Upload em lote usa `carregar_localidades_por_chaves` (mesma ideia de igualdade direta em `chave_busca`).

### Payload, BI e previsão

- **Payload por linha (sessão):** além dos campos financeiros e operacionais (`data_emissao`, `peso_real`, `valor_nf`, `valor_frete_total`, `modal`, opcional `valor_imposto`), cada registro inclui `id_cidade_origem`, `id_uf_origem`, `uf_origem` (sigla 2 letras), `id_cidade_destino`, `id_uf_destino`, `uf_destino`.
- **BI (`app/roberto_bi.py`):** com upload ativo, o BI usa **somente** os dados da sessão (sem misturar com `FreteReal`). **UF de origem e destino vêm diretamente do payload** (`uf_origem` / `uf_destino`). A função `_enriquecer_ufs_cliente` é **fallback**: só consulta `base_localidades` por `id_cidade` quando o payload não trouxer UF (ex.: sessões antigas ou dados parciais).
- **Previsão:** lógica em `app/roberto_modelo.py`.

Documentação de execução, boot, PostgreSQL e variáveis: [`app/README_RUN.md`](app/README_RUN.md), [`app/README_DEPLOY.md`](app/README_DEPLOY.md) (produção), [`app/.env.example`](app/.env.example).

Segurança de segredos e política de repositório: [`SECURITY_SECRETS.md`](SECURITY_SECRETS.md).
