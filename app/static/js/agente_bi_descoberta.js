(function () {
  var raiz = document.getElementById("agente-bi-descoberta");
  if (!raiz) return;

  var status = document.getElementById("agente-bi-descoberta-status");
  var erro = document.getElementById("agente-bi-descoberta-erro");
  var enviar = document.getElementById("agente-bi-enviar");
  var upload = document.getElementById("agente-bi-upload-item");
  var lista = document.getElementById("agente-bi-preparacao-lista");
  var modalidades = document.getElementById("agente-bi-modalidades");
  var modalidadeA = document.getElementById("agente-bi-modalidade-a");
  var modalidadeB = document.getElementById("agente-bi-modalidade-b");
  var modalidadeC = document.getElementById("agente-bi-modalidade-c");
  var afirmacoes = document.getElementById("agente-bi-afirmacoes");
  var adicionar = document.getElementById("agente-bi-adicionar-afirmacao");
  var formulas = document.getElementById("agente-bi-formulas");
  var texto = document.getElementById("agente-bi-texto");
  var examinar = document.getElementById("agente-bi-examinar");
  var resultadoPainel = document.getElementById("agente-bi-resultado");
  var titulo = document.getElementById("agente-bi-resultado-titulo");
  var descobertas = document.getElementById("agente-bi-descobertas");
  var sugestoes = document.getElementById("agente-bi-sugestoes");
  var criticas = document.getElementById("agente-bi-criticas");
  var instrucao = document.getElementById("agente-bi-instrucao");
  var perguntas = document.getElementById("agente-bi-perguntas");
  var contagem = document.getElementById("agente-bi-selecao-contagem");
  var confirmacao = document.getElementById("agente-bi-confirmacao");
  var aprovar = document.getElementById("agente-bi-aprovar");
  var aprovada = document.getElementById("agente-bi-aprovada");
  var aprovadaTitulo = document.getElementById("agente-bi-aprovada-titulo");
  var aprovadaTexto = document.getElementById("agente-bi-aprovada-texto");
  var catalogo = lerCatalogo();
  var limiteBruto = Number(raiz.getAttribute("data-limite"));
  var limite = Number.isInteger(limiteBruto) && limiteBruto >= 1 ? limiteBruto : 0;
  var estado = { ocupado: false, aprovado: false, colunas: [], analiseId: null, sequencia: 0 };
  var arquivo = document.createElement("input");

  var STATUS = {
    viavel: "Viável",
    condicional: "Condicional",
    dependente_de_novos_dados: "Depende de novos dados"
  };
  var NOTAS = {
    viavel: "Pode entrar na seleção inicial.",
    condicional: "Precisa de esclarecimento e não é uma análise executável aprovada.",
    dependente_de_novos_dados: "Depende de novos dados e não é um gráfico aprovado."
  };
  var PERGUNTAS = {
    confirmar_rotulos: "Confirme os rótulos desta região.",
    resolver_tipo_misto: "A coluna mistura tipos e precisa de esclarecimento.",
    resolver_data_ambigua: "Há datas ambíguas nesta região.",
    resolver_formula_sem_cache: "Há fórmula sem cache observado.",
    resolver_divisao_lateral: "A divisão lateral da planilha é ambígua.",
    confirmar_relacao_candidata: "Há uma relação candidata. Confirmar registra o esclarecimento e não executa junção.",
    escolher_medida: "Há mais de uma medida possível.",
    escolher_interpretacao: "A instrução não define uma interpretação única.",
    informar_dados_ausentes: "Faltam dados para esta leitura."
  };
  var DESCOBERTAS = {
    regiao_com_medida: "Região com medida numérica.",
    regiao_sem_medida: "Região sem medida numérica.",
    relacao_candidata_nao_confirmada: "Relação candidata, ainda sem junção.",
    analise_relacoes_parcial: "A leitura das relações ficou parcial.",
    limite_relacoes_atingido: "O limite de relações foi atingido.",
    cabecalho_ambiguo: "O cabeçalho está ambíguo.",
    dominio_inconsistente: "O domínio da coluna está inconsistente.",
    dados_incompletos: "Os dados estão incompletos.",
    formula_sem_cache: "Há fórmula sem cache."
  };
  var CRITICAS = {
    metrica_inventada: "A referência cita uma métrica fora do catálogo.",
    operacao_incompativel: "A operação não cabe na coluna indicada.",
    referencia_inexistente: "A referência não aponta para uma coluna conhecida.",
    juncao_nao_confirmada: "A junção não foi executada.",
    formula_nao_catalogada: "A fórmula não está no catálogo.",
    formula_diverge_da_operacao: "A fórmula não corresponde à operação.",
    referencia_compativel: "A referência é compatível com o catálogo.",
    instrucao_nao_confiavel: "A instrução foi tratada como dado e não foi reproduzida."
  };
  var ERROS = {
    autenticacao_necessaria: "É preciso entrar na conta para continuar.",
    operacao_nao_permitida: "A franquia não permite esta operação.",
    nao_encontrado: "O recurso não está disponível para esta conta.",
    payload_invalido: "O envio não foi aceito.",
    arquivo_corrompido: "O arquivo não pôde ser lido.",
    arquivo_indevido: "O arquivo não foi aceito.",
    tipo_nao_permitido: "Use uma planilha XLSX ou um CSV.",
    limite_arquivos: "A quantidade de arquivos passou do limite.",
    limite_bytes: "O arquivo passou do tamanho permitido.",
    limite_tempo: "A preparação excedeu o tempo permitido.",
    limite_processamento: "A preparação excedeu o limite de processamento.",
    limite_contrato_persistencia: "Os dados preparados não couberam na persistência.",
    conteudo_nao_permitido: "O conteúdo não pode ser armazenado.",
    pedido_invalido: "O pedido não foi aceito. Na modalidade C, escreva a intenção.",
    limite_graficos: "A seleção passa de seis gráficos.",
    sugestao_nao_executavel: "Só gráficos viáveis entram na seleção aprovada.",
    sugestao_inexistente: "Uma sugestão escolhida não pertence a esta rodada.",
    perguntas_pendentes: "Confirme os esclarecimentos antes de aprovar.",
    confirmacao_ausente: "A confirmação da seleção é necessária.",
    analise_indisponivel: "A rodada de descoberta não pode ser aprovada.",
    selecao_invalida: "A seleção não foi aceita.",
    provedor_indisponivel: "A descoberta não pôde ser concluída.",
    contexto_nao_autorizado: "O contexto desta versão não autoriza a descoberta.",
    intencao_nao_transportada: "A intenção não pôde ser enviada. Reformule o pedido.",
    operacao_em_andamento: "A descoberta ainda está em processamento. Envie de novo para recuperar a mesma operação.",
    efeito_incerto: "A descoberta não foi confirmada. Envie de novo para recuperar a mesma operação."
  };

  arquivo.type = "file";
  arquivo.id = "agente-bi-arquivo";
  arquivo.className = "agente-bi-arquivo";
  arquivo.accept = ".csv,.xlsx,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
  arquivo.multiple = true;
  arquivo.tabIndex = -1;
  arquivo.setAttribute("aria-hidden", "true");
  raiz.appendChild(arquivo);

  function lerCatalogo() {
    var nodo = document.getElementById("agente-bi-descoberta-catalogo");
    if (!nodo) return { agregacoes: [], graficos: [] };
    try {
      var lido = JSON.parse(nodo.textContent || "{}");
      return {
        agregacoes: Array.isArray(lido.agregacoes) ? lido.agregacoes : [],
        graficos: Array.isArray(lido.graficos) ? lido.graficos : []
      };
    } catch (falha) {
      return { agregacoes: [], graficos: [] };
    }
  }

  function el(tag, attrs, texto) {
    var node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (chave) {
        if (chave === "class") node.className = attrs[chave];
        else node.setAttribute(chave, attrs[chave]);
      });
    }
    if (texto != null) node.textContent = String(texto);
    return node;
  }

  function limpar(node) {
    if (!node) return;
    while (node.firstChild) node.removeChild(node.firstChild);
  }

  function legivel(codigo) {
    return String(codigo || "").replace(/_/g, " ");
  }

  function anunciar(mensagem) {
    if (status) status.textContent = mensagem;
  }

  function mostrarErro(mensagem) {
    if (!erro) return;
    erro.hidden = false;
    erro.textContent = mensagem;
  }

  function limparErro() {
    if (!erro) return;
    erro.hidden = true;
    erro.textContent = "";
  }

  function mensagemErro(corpo, reserva) {
    var codigo = corpo && corpo.codigo;
    if (codigo && ERROS[codigo]) return ERROS[codigo];
    return reserva;
  }

  function pedir(url, opcoes) {
    return fetch(url, opcoes).then(function (resposta) {
      return resposta.json().then(
        function (corpo) {
          return { status: resposta.status, corpo: corpo || {} };
        },
        function () {
          return { status: resposta.status, corpo: {} };
        }
      );
    });
  }

  function ocupar(mensagem) {
    estado.ocupado = true;
    raiz.classList.add("is-ocupado");
    raiz.setAttribute("aria-busy", "true");
    anunciar(mensagem);
    if (examinar) examinar.disabled = true;
    if (aprovar) aprovar.disabled = true;
    if (enviar) enviar.disabled = true;
    if (upload) upload.disabled = true;
  }

  function liberar() {
    estado.ocupado = false;
    raiz.classList.remove("is-ocupado");
    raiz.removeAttribute("aria-busy");
    if (enviar) enviar.disabled = false;
    if (upload) upload.disabled = false;
    if (examinar && modalidades && !modalidades.disabled) examinar.disabled = false;
    sincronizarAprovacao();
  }

  function definirLista(pares) {
    limpar(lista);
    pares.forEach(function (par) {
      lista.appendChild(el("dt", null, par[0]));
      lista.appendChild(el("dd", null, par[1]));
    });
  }

  function nomes(codigos) {
    if (!codigos || !codigos.length) return "nenhum";
    return codigos.map(legivel).join(", ");
  }

  function mostrarPreparacao(corpo) {
    estado.datasetId = corpo.dataset && corpo.dataset.id;
    var resumo = corpo.resumo || {};
    var dataset = corpo.dataset || {};
    var qualidade = corpo.qualidade || {};
    var pares = [
      ["Estado", corpo.estado === "preparado" ? "preparado" : "não persistido"],
      ["Versão do dataset", dataset.versao == null ? "indisponível" : String(dataset.versao)],
      ["Arquivos", String(resumo.arquivos || 0)],
      ["Abas", String(resumo.abas || 0)],
      ["Regiões", String(resumo.regioes || 0)],
      ["Linhas elegíveis", String(resumo.linhas_elegiveis || 0)],
      ["Células", String(resumo.celulas || 0)],
      ["Fórmulas", String(resumo.formulas || 0)],
      ["Ambígua", resumo.ambigua ? "sim" : "não"],
      ["Alertas", nomes(corpo.alertas)],
      ["Ambiguidades", nomes((corpo.ambiguidades || []).map(function (item) { return item.codigo; }))]
    ];
    ["datas_ambiguas", "formulas_sem_cache", "dominios_inconsistentes", "valores_ausentes"].forEach(function (chave) {
      if (qualidade[chave]) pares.push([legivel(chave), String(qualidade[chave])]);
    });
    definirLista(pares);
    if (corpo.estado !== "preparado") {
      if (modalidades) modalidades.disabled = true;
      mostrarErro(mensagemErro({ codigo: corpo.motivo }, "Os dados não ficaram preparados."));
      anunciar("A preparação não foi concluída.");
      return;
    }
    estado.colunas = Array.isArray(corpo.colunas) ? corpo.colunas : [];
    if (modalidades) modalidades.disabled = false;
    limpar(afirmacoes);
    adicionarAfirmacao();
    if (examinar) examinar.disabled = false;
    if (confirmacao) {
      confirmacao.checked = false;
      confirmacao.disabled = false;
    }
    estado.aprovado = false;
    esconderResultado();
    anunciar(
      (corpo.reutilizado ? "Esta versão já estava preparada. " : "Dados preparados. ") +
        "Versão " + dataset.versao + ". Escolha as modalidades da rodada. O chat continua bloqueado."
    );
  }

  function esconderResultado() {
    if (resultadoPainel) resultadoPainel.hidden = true;
    if (aprovada) {
      aprovada.hidden = true;
      aprovada.removeAttribute("data-versao");
      aprovada.removeAttribute("data-graficos");
    }
    if (aprovadaTexto) aprovadaTexto.textContent = "";
    estado.analiseId = null;
    raiz.removeAttribute("data-restaurada");
    limpar(descobertas);
    limpar(sugestoes);
    limpar(criticas);
    limpar(perguntas);
    if (instrucao) {
      instrucao.hidden = true;
      instrucao.textContent = "";
    }
  }

  function coordenada(coluna) {
    return {
      arquivo: coluna.arquivo,
      aba: coluna.aba,
      regiao: coluna.regiao,
      indice: coluna.indice
    };
  }

  function adicionarAfirmacao() {
    if (!afirmacoes || afirmacoes.children.length >= 8) return;
    estado.sequencia += 1;
    var bloco = el("div", { class: "agente-bi-afirmacao" });
    bloco.appendChild(campoSelect("Operação", "operacao", catalogo.agregacoes));
    bloco.appendChild(campoSelect("Gráfico", "grafico", catalogo.graficos));
    bloco.appendChild(campoSelect("Medida", "medida", opcoesColuna()));
    bloco.appendChild(campoSelect("Agrupamento", "agrupamento", opcoesColuna()));
    var juncao = el("label", { class: "agente-bi-check" });
    var caixa = el("input");
    caixa.type = "checkbox";
    caixa.className = "agente-bi-juncao";
    juncao.appendChild(caixa);
    juncao.appendChild(el("span", null, "A referência pede junção. O contrato não executa junção."));
    bloco.appendChild(juncao);
    afirmacoes.appendChild(bloco);
  }

  function opcoesColuna() {
    return estado.colunas.map(function (coluna, indice) {
      return {
        valor: String(indice),
        rotulo:
          "Arquivo " + coluna.arquivo +
          ", aba " + coluna.aba +
          ", região " + coluna.regiao +
          ", coluna " + coluna.indice +
          " (" + legivel(coluna.dimensao_principal) + ")"
      };
    });
  }

  function campoSelect(rotulo, campo, opcoes) {
    var id = "agente-bi-" + campo + "-" + estado.sequencia;
    var grupo = el("div", { class: "agente-bi-campo-grupo" });
    var select = el("select", { id: id });
    select.setAttribute("data-campo", campo);
    select.appendChild(el("option", { value: "" }, "Nenhum"));
    (opcoes || []).forEach(function (item) {
      var valor = typeof item === "string" ? item : item.valor;
      var textoOpcao = typeof item === "string" ? legivel(item) : item.rotulo;
      select.appendChild(el("option", { value: valor }, textoOpcao));
    });
    grupo.appendChild(el("label", { class: "agente-bi-campo", for: id }, rotulo));
    grupo.appendChild(select);
    return grupo;
  }

  function valorCampo(linha, campo) {
    var select = linha.querySelector('[data-campo="' + campo + '"]');
    return select ? select.value : "";
  }

  function lerAfirmacoes() {
    var linhas = afirmacoes ? afirmacoes.querySelectorAll(".agente-bi-afirmacao") : [];
    var saida = [];
    for (var i = 0; i < linhas.length; i += 1) {
      var linha = linhas[i];
      var operacao = valorCampo(linha, "operacao");
      var grafico = valorCampo(linha, "grafico");
      var medida = valorCampo(linha, "medida");
      var agrupamento = valorCampo(linha, "agrupamento");
      var juncao = linha.querySelector(".agente-bi-juncao");
      var pedeJuncao = !!(juncao && juncao.checked);
      if (!operacao && !grafico && !medida && !agrupamento && !pedeJuncao) continue;
      if (!operacao || medida === "") return { erro: "A referência precisa de operação e medida." };
      var colunaMedida = estado.colunas[Number(medida)];
      var colunaGrupo = agrupamento === "" ? null : estado.colunas[Number(agrupamento)];
      if (!colunaMedida || (agrupamento !== "" && !colunaGrupo)) {
        return { erro: "A referência precisa usar uma coluna preparada." };
      }
      saida.push({
        operacao: operacao,
        grafico: grafico || null,
        medida: coordenada(colunaMedida),
        agrupamento: colunaGrupo ? coordenada(colunaGrupo) : null,
        juncao: pedeJuncao,
        texto: null
      });
    }
    if (saida.length > 8) return { erro: "A referência aceita até oito afirmações." };
    return { afirmacoes: saida };
  }

  function lerFormulas() {
    var linhas = String(formulas && formulas.value || "").split(/\r?\n/).map(function (item) {
      return item.trim();
    }).filter(Boolean);
    if (linhas.length > 8) return { erro: "A referência aceita até oito fórmulas." };
    for (var i = 0; i < linhas.length; i += 1) {
      if (linhas[i].length > 200) return { erro: "Uma fórmula passou do tamanho aceito." };
    }
    return { formulas: linhas };
  }

  function montarPedido() {
    var escolhidas = [];
    if (modalidadeA && modalidadeA.checked) escolhidas.push("A");
    if (modalidadeB && modalidadeB.checked) escolhidas.push("B");
    if (modalidadeC && modalidadeC.checked) escolhidas.push("C");
    if (!escolhidas.length) return { erro: "Escolha ao menos uma modalidade." };
    var pedido = { modalidades: escolhidas };
    if (escolhidas.indexOf("B") !== -1) {
      var lidas = lerAfirmacoes();
      if (lidas.erro) return lidas;
      var formulasLidas = lerFormulas();
      if (formulasLidas.erro) return formulasLidas;
      pedido.referencia = lidas.afirmacoes.length || formulasLidas.formulas.length
        ? { afirmacoes: lidas.afirmacoes, formulas: formulasLidas.formulas }
        : null;
    }
    if (escolhidas.indexOf("C") !== -1) {
      var instrucaoTexto = String(texto && texto.value || "").trim();
      if (instrucaoTexto.length > 2000) return { erro: "A instrução passou do tamanho aceito." };
      pedido.texto = instrucaoTexto || null;
    }
    return { pedido: pedido };
  }

  function descreverReferencia(item) {
    var partes = [];
    if (item && item.regiao) {
      partes.push("arquivo " + item.regiao.arquivo + ", aba " + item.regiao.aba + ", região " + item.regiao.regiao);
    }
    if (item && item.coluna) partes.push("coluna " + item.coluna.indice);
    if (item && item.relacao != null) partes.push("relação " + item.relacao);
    if (item && item.medida) partes.push("medida " + item.medida.indice);
    return partes.join("; ");
  }

  function selecionavel(item) {
    return item
      && item.status === "viavel"
      && typeof item.grafico === "string"
      && item.grafico
      && item.juncao === "nao_executada"
      && Array.isArray(item.evidencias)
      && item.evidencias.length > 0;
  }

  function renderDescobertas(itens) {
    limpar(descobertas);
    if (!itens || !itens.length) return;
    descobertas.appendChild(el("h4", null, "Descobertas"));
    itens.forEach(function (item) {
      var card = el("article", { class: "agente-bi-card" });
      card.appendChild(el("p", { class: "agente-bi-card-titulo" }, DESCOBERTAS[item.codigo] || legivel(item.codigo)));
      var referencia = descreverReferencia(item);
      if (referencia) card.appendChild(el("p", null, referencia));
      descobertas.appendChild(card);
    });
  }

  function renderSugestoes(itens, leitura) {
    limpar(sugestoes);
    sugestoes.appendChild(el("h4", null, leitura ? "Gráficos aprovados" : "Sugestões"));
    (itens || []).forEach(function (item) {
      var card = el("article", { class: "agente-bi-card" });
      card.setAttribute("data-status", item.status || "");
      card.setAttribute("data-id", item.id || "");
      var nome = [item.grafico ? legivel(item.grafico) : "sem gráfico", item.operacao ? legivel(item.operacao) : legivel(item.leitura || "")].filter(Boolean).join(" · ");
      card.appendChild(el("p", { class: "agente-bi-card-titulo" }, nome));
      var selo = el("p", { class: "agente-bi-status agente-bi-status-" + (item.status || "condicional") }, STATUS[item.status] || legivel(item.status));
      card.appendChild(selo);
      card.appendChild(el("p", null, leitura ? "Gráfico incluído na seleção aprovada." : (NOTAS[item.status] || "Esta sugestão não entra como análise executável.")));
      card.appendChild(el("p", null, "Origem: " + (item.origem === "referencia" ? "referência" : "dados") + "."));
      card.appendChild(el("p", null, item.evidencias && item.evidencias.length ? "Evidências: " + item.evidencias.map(legivel).join(", ") + "." : "Nenhuma evidência publicada."));
      var referencia = descreverReferencia(item);
      if (referencia) card.appendChild(el("p", null, referencia));
      if (item.lacuna) card.appendChild(el("p", null, "Lacuna: " + legivel(item.lacuna) + "."));
      if (!leitura && selecionavel(item)) {
        var rotulo = el("label", { class: "agente-bi-check" });
        var caixa = el("input");
        caixa.type = "checkbox";
        caixa.className = "agente-bi-grafico";
        caixa.setAttribute("data-sugestao", item.id);
        caixa.setAttribute("aria-describedby", "agente-bi-selecao-contagem");
        rotulo.appendChild(caixa);
        rotulo.appendChild(el("span", null, "Incluir este gráfico na seleção"));
        card.appendChild(rotulo);
      }
      sugestoes.appendChild(card);
    });
  }

  function renderCriticas(itens) {
    limpar(criticas);
    if (!itens || !itens.length) return;
    criticas.appendChild(el("h4", null, "Crítica da referência"));
    var listaCritica = el("ul");
    itens.forEach(function (item) {
      listaCritica.appendChild(el("li", null, CRITICAS[item.codigo] || legivel(item.codigo)));
    });
    criticas.appendChild(listaCritica);
  }

  function renderPerguntas(itens) {
    limpar(perguntas);
    if (!itens || !itens.length) return;
    perguntas.appendChild(el("h4", null, "Esclarecimentos pendentes"));
    itens.forEach(function (item) {
      var rotulo = el("label", { class: "agente-bi-check" });
      var caixa = el("input");
      caixa.type = "checkbox";
      caixa.className = "agente-bi-pergunta";
      caixa.setAttribute("data-pergunta", item.id);
      rotulo.appendChild(caixa);
      var complemento = descreverReferencia(item);
      rotulo.appendChild(el("span", null, (PERGUNTAS[item.codigo] || legivel(item.codigo)) + (complemento ? " " + complemento + "." : "")));
      perguntas.appendChild(rotulo);
    });
  }

  function mostrarResultado(corpo) {
    var resultado = corpo.resultado || {};
    estado.analiseId = corpo.analise_id;
    estado.aprovado = false;
    if (confirmacao) confirmacao.checked = false;
    renderDescobertas(resultado.descobertas || []);
    renderSugestoes(resultado.sugestoes || []);
    renderCriticas(resultado.critica_referencia || []);
    renderPerguntas(resultado.perguntas || []);
    if (instrucao) {
      var detectada = resultado.instrucoes_nao_confiaveis && resultado.instrucoes_nao_confiaveis.detectadas;
      instrucao.hidden = !detectada;
      instrucao.textContent = detectada ? "A instrução foi tratada como dado e não foi reproduzida." : "";
    }
    if (resultadoPainel) resultadoPainel.hidden = false;
    if (aprovada) aprovada.hidden = true;
    anunciar("Descobertas prontas para revisão. A seleção inicial aceita até " + limite + " gráficos viáveis.");
    sincronizarAprovacao();
    if (titulo) titulo.focus();
  }

  function idsSelecionados() {
    var saida = [];
    var caixas = raiz.querySelectorAll(".agente-bi-grafico:checked");
    for (var i = 0; i < caixas.length; i += 1) saida.push(caixas[i].getAttribute("data-sugestao"));
    return saida;
  }

  function sincronizarAprovacao() {
    if (!contagem || !aprovar) return;
    if (!raiz.querySelector(".agente-bi-grafico")) {
      contagem.textContent = "Não há gráfico viável para aprovar nesta rodada.";
      aprovar.disabled = true;
      return;
    }
    var ids = idsSelecionados();
    var caixas = raiz.querySelectorAll(".agente-bi-grafico");
    for (var i = 0; i < caixas.length; i += 1) {
      caixas[i].disabled = estado.ocupado || (ids.length >= limite && !caixas[i].checked);
    }
    contagem.textContent = ids.length + " de " + limite + " gráficos viáveis.";
    if (limite && ids.length >= limite) contagem.textContent += " O limite inicial foi atingido.";
    aprovar.disabled = estado.ocupado || estado.aprovado || !limite || !ids.length || ids.length > limite || !(confirmacao && confirmacao.checked);
  }

  function escolherArquivo() {
    if (estado.ocupado) return;
    arquivo.click();
  }

  function enviarArquivos(listaArquivos) {
    var dados = new FormData();
    for (var i = 0; i < listaArquivos.length; i += 1) dados.append("arquivos", listaArquivos[i], listaArquivos[i].name);
    limparErro();
    ocupar("Preparando os dados…");
    pedir(raiz.getAttribute("data-preparacao-url"), {
      method: "POST",
      body: dados,
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true) {
        liberar();
        mostrarErro(mensagemErro(resposta.corpo, "A preparação não foi concluída."));
        anunciar("A preparação não foi concluída.");
        return;
      }
      limparErro();
      mostrarPreparacao(resposta.corpo);
      liberar();
    }).catch(function () {
      liberar();
      mostrarErro("Não foi possível concluir esta etapa. Tente novamente.");
      anunciar("A preparação não foi concluída.");
    });
  }

  function examinarDescobertas() {
    if (estado.ocupado || (modalidades && modalidades.disabled)) return;
    var montado = montarPedido();
    if (montado.erro) {
      mostrarErro(montado.erro);
      return;
    }
    limparErro();
    ocupar("Examinando descobertas…");
    pedir(raiz.getAttribute("data-descoberta-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({ dataset_id: estado.datasetId, pedido: montado.pedido })
    }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.resultado) {
        liberar();
        mostrarErro(mensagemErro(resposta.corpo, "A descoberta não foi concluída."));
        anunciar("A descoberta não foi concluída.");
        return;
      }
      limparErro();
      mostrarResultado(resposta.corpo);
      liberar();
    }).catch(function () {
      liberar();
      mostrarErro("Não foi possível concluir esta etapa. Tente novamente.");
      anunciar("A descoberta não foi concluída.");
    });
  }

  function aprovarSelecao() {
    if (estado.ocupado || !aprovar || aprovar.disabled) return;
    var ids = idsSelecionados();
    var confirmadas = [];
    var caixas = raiz.querySelectorAll(".agente-bi-pergunta");
    for (var i = 0; i < caixas.length; i += 1) {
      confirmadas.push({ id: caixas[i].getAttribute("data-pergunta"), confirmada: caixas[i].checked === true });
    }
    limparErro();
    ocupar("Gravando a seleção…");
    pedir(raiz.getAttribute("data-selecao-url"), {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify({
        analise_id: estado.analiseId,
        sugestoes: ids,
        perguntas: confirmadas,
        confirmacao: true
      })
    }).then(function (resposta) {
      var selecao = resposta.corpo && resposta.corpo.selecao;
      if (!resposta.corpo || resposta.corpo.ok !== true || !selecao) {
        liberar();
        mostrarErro(mensagemErro(resposta.corpo, "A seleção não foi gravada."));
        anunciar("A seleção não foi gravada.");
        return;
      }
      if (aprovada) {
        aprovada.hidden = false;
        aprovada.setAttribute("data-versao", String(selecao.versao));
        aprovada.setAttribute("data-graficos", String(selecao.graficos));
      }
      estado.aprovado = true;
      if (aprovadaTexto) {
        aprovadaTexto.textContent =
          "A seleção de " + selecao.graficos +
          " gráficos viáveis foi gravada na sua conta, na versão " + selecao.versao +
          " do dataset. Sugestões condicionais e leituras que dependem de novos dados não entraram como análises executáveis.";
      }
      liberar();
      anunciar("Seleção aprovada e vinculada à versão " + selecao.versao + ".");
      if (aprovadaTitulo) aprovadaTitulo.focus();
    }).catch(function () {
      liberar();
      mostrarErro("Não foi possível concluir esta etapa. Tente novamente.");
      anunciar("A seleção não foi gravada.");
    });
  }

  function modalidadesLegiveis(lista) {
    var saida = [];
    if (!Array.isArray(lista)) return saida;
    ["A", "B", "C"].forEach(function (letra) {
      if (lista.indexOf(letra) !== -1) saida.push(letra);
    });
    return saida;
  }

  function idsConfirmados(lista) {
    var saida = [];
    if (!Array.isArray(lista)) return saida;
    lista.forEach(function (item) {
      if (typeof item === "string" && /^p[1-9][0-9]{0,2}$/.test(item)) saida.push(item);
    });
    return saida;
  }

  function selecaoConfiavel(item) {
    var resultado = item && item.resultado;
    if (!item || item.etapa !== "selecao" || item.estado !== "concluida") return false;
    if (item.contrato_versao !== "selecao.descoberta.v1") return false;
    if (!resultado || resultado.contrato !== "selecao.descoberta.v1") return false;
    if (resultado.executavel !== "somente_viavel" || resultado.juncao !== "nao_executada") return false;
    if (resultado.chamada_anthropic !== false || resultado.apto_anthropic !== false) return false;
    if (item.dataset_id !== resultado.dataset_id || item.versao !== resultado.dataset_versao) return false;
    if (!Array.isArray(resultado.graficos) || !resultado.graficos.length || (limite && resultado.graficos.length > limite)) return false;
    return resultado.graficos.every(function (grafico) {
      return grafico && grafico.status === "viavel" && typeof grafico.grafico === "string" && grafico.grafico && grafico.juncao === "nao_executada";
    });
  }

  function mostrarRestaurada(item) {
    var resultado = item.resultado;
    var letras = modalidadesLegiveis(resultado.modalidades);
    var confirmadas = idsConfirmados(resultado.perguntas_confirmadas);
    estado.aprovado = true;
    estado.datasetId = item.dataset_id;
    definirLista([
      ["Estado", "seleção aprovada"],
      ["Versão do dataset", String(item.versao)],
      ["Dataset", String(item.dataset_id)],
      ["Modalidades", letras.length ? letras.join(", ") : "nenhuma"],
      ["Gráficos aprovados", String(resultado.graficos.length)],
      ["Confirmações", confirmadas.length ? confirmadas.join(", ") : "nenhuma"]
    ]);
    renderSugestoes(resultado.graficos, true);
    limpar(perguntas);
    if (perguntas) {
      perguntas.appendChild(el("h4", null, "Confirmações"));
      perguntas.appendChild(el(
        "p",
        null,
        confirmadas.length
          ? "Esclarecimentos já confirmados: " + confirmadas.join(", ") + "."
          : "Os esclarecimentos desta seleção já foram confirmados."
      ));
    }
    if (resultadoPainel) resultadoPainel.hidden = false;
    if (aprovada) {
      aprovada.hidden = false;
      aprovada.setAttribute("data-versao", String(item.versao));
      aprovada.setAttribute("data-graficos", String(resultado.graficos.length));
    }
    if (aprovadaTexto) {
      aprovadaTexto.textContent =
        "A seleção de " + resultado.graficos.length +
        " gráficos viáveis permanece gravada na sua conta, na versão " + item.versao +
        " do dataset. Não é necessário enviar a planilha outra vez.";
    }
    if (confirmacao) {
      confirmacao.checked = true;
      confirmacao.disabled = true;
    }
    if (aprovar) aprovar.disabled = true;
    if (contagem) contagem.textContent = resultado.graficos.length + " gráficos já aprovados. Nada novo para enviar.";
    raiz.setAttribute("data-restaurada", "1");
    anunciar("Seleção aprovada recuperada na versão " + item.versao + ". O chat continua bloqueado.");
    if (aprovadaTitulo) aprovadaTitulo.focus();
  }

  function recuperarSelecao() {
    var url = raiz.getAttribute("data-analises-url");
    if (!url) return;
    pedir(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      if (estado.ocupado || estado.datasetId || estado.aprovado) return;
      var analises = resposta.corpo && resposta.corpo.analises;
      if (!resposta.corpo || resposta.corpo.ok !== true || !Array.isArray(analises)) return;
      for (var i = 0; i < analises.length; i += 1) {
        if (selecaoConfiavel(analises[i])) {
          mostrarRestaurada(analises[i]);
          return;
        }
      }
    }).catch(function () {
      if (!estado.ocupado && !estado.datasetId && !estado.aprovado) {
        anunciar("Não foi possível ler a seleção já gravada. O chat continua bloqueado.");
      }
    });
  }

  arquivo.addEventListener("change", function () {
    var listaArquivos = [];
    if (arquivo.files) {
      for (var i = 0; i < arquivo.files.length; i += 1) listaArquivos.push(arquivo.files[i]);
    }
    arquivo.value = "";
    if (!listaArquivos.length || estado.ocupado) return;
    enviarArquivos(listaArquivos);
  });
  if (enviar) enviar.addEventListener("click", escolherArquivo);
  if (upload) upload.addEventListener("click", function (event) {
    event.preventDefault();
    escolherArquivo();
  });
  if (adicionar) adicionar.addEventListener("click", adicionarAfirmacao);
  if (examinar) examinar.addEventListener("click", examinarDescobertas);
  if (aprovar) aprovar.addEventListener("click", aprovarSelecao);
  raiz.addEventListener("change", function (event) {
    var alvo = event.target;
    if (!alvo || !alvo.classList) return;
    if (alvo.classList.contains("agente-bi-grafico")) {
      var ids = idsSelecionados();
      if (alvo.checked && limite && ids.length > limite) {
        alvo.checked = false;
        anunciar("A seleção inicial aceita até " + limite + " gráficos.");
      }
      sincronizarAprovacao();
      return;
    }
    if (alvo.classList.contains("agente-bi-pergunta") || alvo.id === "agente-bi-confirmacao") sincronizarAprovacao();
  });
  recuperarSelecao();

})();
