# Diagnóstico: Artigos e notícias rápidas não publicados em homologação

**Escopo:** Este documento cobre **ambos** os fluxos que alimentam o portal em https://homolog0514.agentefrete.com.br/:

- **Artigos da Júlia** (conteúdo estratégico de longo formato)
- **Notícias rápidas / insights** (conteúdo automático a partir do Scout + Verificador)

Quando **nem artigos nem notícias rápidas** aparecem no site — inclusive ao usar os botões de bypass no Admin — o problema pode ser **um bloqueio comum** (janela, frequência, sem pauta, falha na Júlia) ou **ambiente** (execução em segundo plano escondendo o erro; banco efêmero no Render).

---

## 1. Por que o relatório anterior não era conclusivo

Em **homolog** (e prod), os botões **"Executar Cleiton"** e **"Executar artigo agora"** rodam em **segundo plano** para evitar timeout do worker. A resposta HTTP é imediata: *"Execução iniciada em segundo plano"*. O **resultado real** (status, motivo, caminho_usado) só ia para os **logs** do Render; na tela do Admin não aparecia o motivo da falha. Assim, não dava para concluir **qual** bloqueio estava ocorrendo.

Além disso:

- O botão **"Executar Cleiton"** (que dispara tanto notícia quanto artigo, conforme decisão do orquestrador) **respeitava a janela de publicação**. Fora do horário 6–22 (hora do servidor, em geral UTC), o ciclo era ignorado com *"Fora da janela de publicação"* mesmo em disparo manual.
- Sem ver o resultado na interface, era impossível distinguir entre: fora da janela, sem pauta elegível, falha na Júlia (API, qualidade, etc.) ou exceção não tratada.

---

## 2. O que foi feito para tornar o diagnóstico conclusivo

1. **Bypass da janela em ambos os botões manuais**  
   - **"Executar Cleiton"** (com "Executar agora (bypass de frequência)"): passa a usar `ignorar_janela_publicacao=True` quando há bypass de frequência.  
   - **"Executar artigo agora (bypass diário)"**: já usava bypass de janela; mantido.  
   Assim, em homolog você pode disparar **artigo e notícia** manualmente a **qualquer hora**, sem bloqueio por janela.

2. **Persistência e exibição do último resultado manual**  
   - Após cada execução (sync ou em background), o resultado é gravado em `{DATA_DIR}/last_admin_run.json`.  
   - Na página **Admin → Agentes - Júlia**, a seção **"Última execução manual (Admin)"** mostra:  
     **Origem**, **Status**, **Motivo**, **Caminho usado**, **mission_id**, **timestamp**.  
   - Em homolog: clique no botão, aguarde alguns segundos, **atualize a página** e leia o **Motivo** e **Caminho usado** para obter o **erro exato**.

Com isso, o diagnóstico deixa de ser uma lista de “causas prováveis” e passa a ser: **execute, atualize a página e leia o motivo/caminho** para identificar a causa real.

---

## 3. Como obter o erro exato no ambiente de homologação

1. Acesse o Admin em homolog e vá em **Agentes - Júlia**.
2. Clique em **"Executar Cleiton"** com **"Executar agora (bypass de frequência)"** **ou** em **"Executar artigo agora (bypass diário)"**.
3. Aguarde **cerca de 30–60 segundos** (tempo típico do ciclo + Júlia).
4. **Atualize a página** (F5).
5. Na seção **"Última execução manual (Admin)"**, leia:
   - **Status:** `sucesso` | `ignorado` | `falha`
   - **Motivo:** texto explicando o que aconteceu.
   - **Caminho usado:** indica em que ponto o fluxo parou (veja a tabela abaixo).

O **Motivo** e o **Caminho usado** são a **resposta conclusiva** sobre o motivo de artigos/notícias não estarem sendo publicados.

---

## 4. Tabela de diagnóstico: Caminho usado → Causa e ação

| **caminho_usado** / **motivo** | **Causa** | **Ação** |
|--------------------------------|-----------|----------|
| `fora_janela_publicacao` | Ciclo não roda fora do horário configurado (ex.: 6–22 UTC). | Em **disparo manual** isso não deve mais ocorrer (bypass ativo). Se ainda aparecer, conferir se o deploy com o bypass da janela está ativo. Para o **cron**, ajustar janela em `config_regras` ou TZ do servidor. |
| `ignorado_frequencia` | Última execução foi há menos de N horas (N = `frequencia_horas` em `config_regras`). | Aumentar intervalo do cron ou reduzir `frequencia_horas` em homolog (ex.: 1). |
| `sem_fonte_artigo` / "Nenhum item de série ou pauta manual elegível para artigo" | Para **artigo**: não há pauta com `tipo='artigo'`, `status='pendente'` e `status_verificacao` em `aprovado`/`revisar`. | Inserir pautas de artigo e aprovar no Verificador; ou conferir se `JULIA_STATUS_VERIFICACAO_PERMITIDOS=aprovado,revisar` em homolog. |
| `noticia_rapida` + status `falha` ou "Despacho para agente operacional falhou" | Orquestrador despachou **notícia**, mas a **Júlia** não publicou (sem pauta elegível, falha na redação, imagem, qualidade ou publicação). | Ver seção 5 (Júlia). Para notícia, é preciso ter pauta `tipo='noticia'` aprovada; o Scout enche a tabela e o Verificador aprova. |
| `pauta_manual` ou `artigo` + status `falha` | Missão de **artigo** foi despachada, mas a Júlia falhou (pauta não encontrada, pipeline quebrou). | Ver seção 5 (Júlia) e logs do Render. |
| `excecao` / motivo com stack trace | Erro não tratado (ex.: banco, import, API). | Ver logs completos no Render; corrigir exceção (env, DB, API key, etc.). |

---

## 5. Falha dentro da Júlia (artigo e notícia)

Tanto **artigo** quanto **notícia rápida** passam pelo **mesmo pipeline** da Júlia:

1. **obter_pauta_validada(tipo_missao)** — pauta `pendente` com `status_verificacao` permitido (`aprovado` ou `revisar` em homolog).  
   - Se não houver: **Motivo** no dispatch será algo como "Júlia não publicou conteúdo (falha de geração ou sem pauta válida)". **Caminho** no ciclo pode ser sucesso, mas `dispatch_ok` fica False.
2. **Redação** (Gemini) — falha ou timeout → pauta vai para `falha`.
3. **Imagem** — falha ou fallback → segue com fallback; em caso de erro crítico, pipeline pode falhar.
4. **Qualidade** — conteúdo reprovado → pauta vai para `falha`.
5. **Publicação** (NoticiaPortal + publisher) — erro de DB ou de marcação de publicado.

Para **notícias rápidas**: é necessário que o **Scout** tenha coletado itens e o **Verificador** tenha aprovado ao menos uma pauta com `tipo='noticia'`. Se não houver nenhuma pauta `noticia` elegível, a Júlia retorna False e o ciclo aparece como falha de despacho.

**Como verificar:** Logs do Web Service no Render no momento do disparo; tabela `pautas` (status, status_verificacao, tipo); tabela `noticias_portal` (se algum registro foi criado).

---

## 6. Banco e dados efêmeros no Render

No Render, o **filesystem do serviço é efêmero**: a cada deploy os arquivos locais são recriados. Se **DB_URI_*** e **DATA_DIR** (ou `APP_DATA_DIR` / `RENDER_DISK_PATH`) não apontarem para um **disco persistente** (Render Persistent Disk), então:

- Os SQLites (auth, noticias, gerencial, etc.) são recriados vazios a cada deploy.
- O arquivo `last_admin_run.json` pode ser perdido após deploy (o diagnóstico na página passa a não mostrar a última execução até um novo disparo).

**Ação:** Em homolog, configurar **Render Persistent Disk** e definir `APP_DATA_DIR` (ou equivalente) para o caminho do disco, e usar esse mesmo diretório para os `DB_URI_*` (ex.: `sqlite:///data/noticias.db` com `data` no disco). Ver `README_DEPLOY.md` e `RENDER_CRON_HOMOLOG.md`.

---

## 7. Fluxo único: artigo e notícia

- **Gatilho:** Cron (`/cron/executar-cleiton`) ou botões no Admin (**Executar Cleiton** ou **Executar artigo agora**).
- **Orquestrador (Cleiton):** Verifica frequência e janela (com bypass nos botões manuais); decide **tipo de missão** (artigo ou notícia). Para artigo, exige pauta/série elegível; para notícia, segue com caminho `noticia_rapida`. Roda Scout + Verificador (que alimentam pautas de notícia). Monta payload e **despacha para a Júlia**.
- **Júlia:** Obtém pauta validada para o tipo (artigo ou noticia), gera conteúdo, imagem, qualidade, publica em `noticias_portal` e marca `publicado_em`.

Se **qualquer** bloqueio ocorrer no orquestrador (janela, frequência, sem fonte para artigo), **nenhum** conteúdo é publicado. Se o despacho ocorrer mas a Júlia falhar (sem pauta, redação, qualidade, etc.), o ciclo retorna falha e o **Motivo** na **Última execução manual** deve indicar isso.

---

## 8. Checklist conclusivo (homolog)

| Passo | Ação |
|-------|------|
| 1 | Fazer deploy com as alterações (bypass de janela nos dois botões + persistência de última execução). |
| 2 | No Admin → Agentes - Júlia, clicar em **"Executar agora (bypass de frequência)"** ou **"Executar artigo agora (bypass diário)"**. |
| 3 | Aguardar ~30–60 s e **atualizar a página**. |
| 4 | Ler **Última execução manual**: **Status**, **Motivo**, **Caminho usado**. |
| 5 | Usar a **tabela da seção 4** para mapear **caminho_usado** / **motivo** → causa e ação. |
| 6 | Se o motivo indicar falha na Júlia: checar logs do Render, pautas elegíveis (artigo vs noticia), API keys, qualidade. |
| 7 | Se o banco for efêmero: configurar disco persistente e `DB_URI_*` / `APP_DATA_DIR`. |

---

## 9. Referências no projeto

- **Cron em homolog:** `RENDER_CRON_HOMOLOG.md`
- **Regras (frequência, janela):** `app/run_cleiton_agente_regras.py` e tabela `config_regras` (bind `gerencial`)
- **Orquestrador (bloqueios):** `app/run_cleiton_agente_orquestrador.py`
- **Dispatcher e Júlia:** `app/run_cleiton_agente_dispatcher.py`, `app/run_julia.py`, `app/run_julia_agente_pipeline.py`
- **Rota do cron:** `app/web.py` → `/cron/executar-cleiton`
- **Status de verificação permitidos (Júlia):** `app/run_julia_regras.py` e env `JULIA_STATUS_VERIFICACAO_PERMITIDOS`
