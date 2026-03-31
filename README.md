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
- **Upload de planilha (.xlsx):** processado em `app/upload_handler.py`. O arquivo é gravado em disco só durante a leitura e é **removido em seguida**. Não há persistência de linhas da planilha em tabela de negócio: o resultado fica **apenas na sessão Flask** (efêmero, TTL em `app/upload_handler.py`), com amostragem opcional por mês antes de guardar na sessão.
- **Localidades (PostgreSQL único):** para cada linha, cidade e UF da planilha geram a chave `cidade-uf` (minúscula, mesma regra que `gerar_chave_busca` / `chave_busca` em `base_localidades`). `get_localidade_completa_por_chave` em `app/infra.py` consulta `base_localidades` com `LOWER(TRIM(chave_busca))` e devolve `id_cidade`, `id_uf`, nomes e chave. **Não existe segundo banco de dados** para localidades.
- **Payload por linha (sessão):** além dos campos financeiros e operacionais (`data_emissao`, `peso_real`, `valor_nf`, `valor_frete_total`, `modal`, opcional `valor_imposto`), cada registro inclui `id_cidade_origem`, `id_uf_origem`, `uf_origem` (sigla 2 letras), `id_cidade_destino`, `id_uf_destino`, `uf_destino`.
- **BI (`app/roberto_bi.py`):** com upload ativo, o BI usa **somente** os dados da sessão (sem misturar com `FreteReal`). **UF de origem e destino vêm diretamente do payload** (`uf_origem` / `uf_destino`). A função `_enriquecer_ufs_cliente` é **fallback**: só consulta `base_localidades` por `id_cidade` quando o payload não trouxer UF (ex.: sessões antigas ou dados parciais).
- **Previsão:** lógica em `app/roberto_modelo.py`.

Documentação de execução, boot, PostgreSQL e variáveis: [`app/README_RUN.md`](app/README_RUN.md), [`app/README_DEPLOY.md`](app/README_DEPLOY.md) (produção), [`app/.env.example`](app/.env.example).

Segurança de segredos e política de repositório: [`SECURITY_SECRETS.md`](SECURITY_SECRETS.md).
