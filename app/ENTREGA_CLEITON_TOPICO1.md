# Entrega – Camada gerencial Cleiton (Tópico 1)

## 1. Resumo técnico do que foi alterado

- **Orquestração:** Toda a lógica de decisão foi extraída de `run_cleiton.py` para o orquestrador gerencial (`run_cleiton_agente_orquestrador.py`), que lê o plano ativo, aplica regras (frequência, janela, prioridade, retries), registra auditoria e despacha para agentes operacionais via payload padronizado. O Cleiton não gera conteúdo final.
- **Regras:** Engine em `run_cleiton_agente_regras.py` lê configuração persistida na tabela `config_regras` (bind `gerencial`); valores padrão só quando não há registro no banco (sem hardcode de regras de negócio).
- **Dispatch:** Contrato em `run_cleiton_agente_dispatcher.py`: payload com `mission_id`, `tipo_missao`, `tema`, `prioridade`, `janela_publicacao`, `tentativa_atual`, `metadados`. Missões persistidas em `missao_agente`; despacho para Júlia (artigo/noticia) implementado.
- **Auditoria:** Cada decisão (orquestração, dispatch, purge) é persistida em `auditoria_gerencial` por `run_cleiton_agente_auditoria.py`.
- **Retenção:** `run_cleiton_agente_retencao.py` aplica 18 meses para dados (notícias/leads) e 2 meses para imagens (zerar `url_imagem`); eventos de purge registrados na auditoria; idempotente.
- **Ambiente:** `app/env_loader.py` carrega `.env.{APP_ENV}` por caminho absoluto baseado no diretório `app`, evitando dependência do CWD.
- **web.py:** Permanece com apenas roteamento e delegação; a rota `/executar-cleiton` chama `run_cleiton.executar_orquestracao(app)`, que delega ao orquestrador.
- **Modelos:** Novos modelos no bind `gerencial`: `PlanoEstrategico`, `ConfigRegras`, `MissaoAgente`, `AuditoriaGerencial`. Bind `gerencial` e `DB_URI_GERENCIAL` adicionados em `web.py` e `infra.py`.

---

## 2. Lista de arquivos criados/alterados e papel de cada um

| Arquivo | Papel |
|--------|--------|
| **app/env_loader.py** | Novo. Carrega `.env.{APP_ENV}` por caminho absoluto (diretório app). |
| **app/run_cleiton_agente_auditoria.py** | Novo. Registra eventos na tabela `auditoria_gerencial`. |
| **app/run_cleiton_agente_regras.py** | Novo. Engine de regras (frequência, prioridade, janela, retries, retenção) a partir de `config_regras`. |
| **app/run_cleiton_agente_dispatcher.py** | Novo. Constrói payload padronizado, registra missão, despacha para Júlia (e futuros agentes). |
| **app/run_cleiton_agente_orquestrador.py** | Novo. Ciclo gerencial: plano, regras, decisão, auditoria, dispatch, retenção. |
| **app/run_cleiton_agente_retencao.py** | Novo. Limpeza 18 meses (dados) e 2 meses (imagens); purge auditado. |
| **app/run_cleiton.py** | Alterado. Fachada: carrega env via `env_loader`, delega `executar_orquestracao` ao orquestrador; intervalo do loop por `get_frequencia_horas()`. |
| **app/web.py** | Alterado. Adicionado bind `gerencial` e `DB_URI_GERENCIAL`. Rotas inalteradas; `/executar-cleiton` continua delegando. |
| **app/models.py** | Alterado. Novos modelos: `PlanoEstrategico`, `ConfigRegras`, `MissaoAgente`, `AuditoriaGerencial` (bind `gerencial`). |
| **app/infra.py** | Alterado. `OPTIONAL_BINDS` inclui `gerencial`. |
| **app/.env.example** | Alterado. Comentário sobre env_loader; `DB_URI_GERENCIAL`. |
| **app/README_RUN.md** | Alterado. Descrição da camada Cleiton, env_loader, `DB_URI_GERENCIAL`, seção "Executar Cleiton". |
| **app/README_DEPLOY.md** | Alterado. `DB_URI_GERENCIAL` no exemplo `.env.prod`; nota sobre Cleiton e `APP_ENV`. |
| **app/CHECKLIST_VALIDACAO_CLEITON.md** | Novo. Checklist de validação para esta entrega. |
| **app/ENTREGA_CLEITON_TOPICO1.md** | Este arquivo. |

---

## 3. Diff por arquivo (resumo)

- **run_cleiton.py:** Remoção de lógica de decisão e execução direta da Júlia; imports de `dotenv` e agentes removidos do topo; uso de `env_loader.load_app_env()`; `executar_orquestracao` apenas chama `executar_ciclo_gerencial`; loop usa `get_frequencia_horas()`.
- **web.py:** Inclusão de `gerencial` em `db_binds` com `DB_URI_GERENCIAL`. Nenhuma nova lógica de orquestração.
- **models.py:** Inclusão de quatro classes com `__bind_key__ = 'gerencial'` e tabelas correspondentes.
- **infra.py:** Inclusão de `'gerencial'` em `OPTIONAL_BINDS`.

---

## 4. Checklist de validação executado e resultado

- **Imports e payload:** Teste em ambiente sem Flask completo: `env_loader`, `run_cleiton_agente_regras`, `run_cleiton_agente_dispatcher` importam; `construir_payload` retorna dict com `mission_id`, `tipo_missao`, `tema`, `prioridade`, `janela_publicacao`, `tentativa_atual`, `metadados`; fora do app context as regras usam defaults (ex.: frequência 3h, janela 6–22).
- **Rotas / web:** Não foi executado servidor completo (dependência Flask-Session no ambiente de teste). O checklist manual está em `CHECKLIST_VALIDACAO_CLEITON.md`; recomenda-se rodar localmente: login, fretes, home, depois `POST /executar-cleiton` e conferir auditoria no banco.

---

## 5. Atualizações de documentação realizadas

- **README_RUN.md:** Camada gerencial, env_loader, `DB_URI_GERENCIAL`, seção 4 "Executar Cleiton".
- **README_DEPLOY.md:** Exemplo `DB_URI_GERENCIAL`; nota sobre Cleiton e `APP_ENV=prod`.
- **.env.example:** Comentário sobre env_loader; variável `DB_URI_GERENCIAL`.
- **CHECKLIST_VALIDACAO_CLEITON.md:** Criado com itens de validação para rotas, Cleiton, auditoria, retenção e ambiente.

---

## 6. Riscos remanescentes e próximos passos

- **Riscos:** (1) `run_julia.py` ainda depende de `processadas.json`; integração com ciclo de coleta/curadoria fica para etapa futura. (2) Primeira execução do orquestrador cria plano e regras padrão; em produção convém revisar valores em `config_regras`. (3) Retenção de imagens hoje apenas zera `url_imagem`; remoção física de arquivos de mídia (se houver) pode ser feita em passo posterior.
- **Próximos passos sugeridos:** (1) Implementar/ajustar `processar_ciclo_noticias` em `news_ai.py` e integrar ao dispatcher (coleta/curadoria). (2) Integrar `finance.atualizar_indices` no ciclo gerencial se desejado. (3) Expandir dispatcher para outros agentes (imagem, QA, publicação). (4) Tela ou rota de consulta à auditoria gerencial para operação.
