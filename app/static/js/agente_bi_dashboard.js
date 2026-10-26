(function () {
  var raiz = document.getElementById("agente-bi-dashboard");
  if (!raiz) return;

  var status = document.getElementById("agente-bi-dashboard-status");
  var erro = document.getElementById("agente-bi-dashboard-erro");
  var lista = document.getElementById("agente-bi-dashboard-lista");
  var origens = document.getElementById("agente-bi-dashboard-origens");
  var aberto = document.getElementById("agente-bi-dashboard-aberto");
  var tituloAberto = document.getElementById("agente-bi-dashboard-aberto-titulo");
  var vinculo = document.getElementById("agente-bi-dashboard-vinculo");
  var atualizacao = document.getElementById("agente-bi-dashboard-atualizacao");
  var evolucao = document.getElementById("agente-bi-dashboard-evolucao");
  var formulario = document.getElementById("agente-bi-dashboard-filtros");
  var grade = document.getElementById("agente-bi-dashboard-grade");
  var graficosAtivos = [];
  var consultaAtual = null;
  var painelAtual = null;
  var parametrosEmMemoria = {};
  var ocupado = false;

  var ROTULOS = {
    soma: "Soma",
    contagem: "Contagem",
    contagem_distinta: "Contagem distinta",
    media: "Média",
    minimo: "Mínimo",
    maximo: "Máximo",
    count_if: "Contagem condicional",
    participacao: "Participação",
    razao: "Razão",
    par_numerico: "Pares",
    indefinida: "Sem operação"
  };
  var TIPOS = {
    indicador: "Indicador",
    barra: "Barras",
    linha: "Linha",
    pizza: "Pizza",
    tabela: "Tabela",
    dispersao: "Dispersão"
  };
  var AVISOS = {
    linhas_pre_preenchidas_excluidas: "Linhas pré-preenchidas foram excluídas",
    linhas_vazias_excluidas: "Linhas vazias foram excluídas",
    ha_exclusoes: "Há registros excluídos da conta",
    juncao_sem_duplicacao: "A junção não duplicou linhas",
    pares_completos: "Somente pares completos entraram"
  };
  var ERROS = {
    limite_graficos: "O dashboard aceita no máximo seis gráficos.",
    nao_encontrado: "Esse dashboard não está disponível nesta conta.",
    analise_indisponivel: "O cálculo gravado não pode ser aberto.",
    parametro_inconsistente: "O filtro não combina com a definição autorizada.",
    informar_parametro: "Informe os filtros exigidos pela definição.",
    identidade_invalida: "O dashboard não corresponde à versão do dataset.",
    atualizacao_incompativel: "Os dados novos não são compatíveis com o dashboard. A atualização automática foi bloqueada e precisa de encaminhamento autorizado.",
    mudanca_significado: "O significado dos dados mudou. A atualização automática foi bloqueada e precisa de encaminhamento autorizado.",
    ambiguidade_relevante: "Há ambiguidade nos dados novos. A atualização automática foi bloqueada e precisa de encaminhamento autorizado.",
    autenticacao_necessaria: "Entre na sua conta para abrir o dashboard.",
    operacao_nao_permitida: "Sua franquia não pode consultar este dashboard.",
    payload_invalido: "Não foi possível ler este dashboard.",
    alteracao_invalida: "Esse pedido não é uma alteração válida do dashboard.",
    nao_autorizada: "Esse pedido não está autorizado para este dashboard.",
    admissao_recusada: "A franquia não autorizou o uso de IA nesta alteração.",
    efeito_incerto: "A interpretação não pôde ser confirmada. O dashboard não foi alterado."
  };

  function el(tag, classe, texto) {
    var no = document.createElement(tag);
    if (classe) no.className = classe;
    if (texto !== undefined && texto !== null) no.textContent = String(texto);
    return no;
  }

  function anunciar(texto) {
    if (status) status.textContent = texto;
  }

  function mostrarErro(codigo) {
    if (!erro) return;
    erro.hidden = false;
    erro.textContent = ERROS[codigo] || "Não foi possível consultar o dashboard.";
  }

  function limparErro() {
    if (!erro) return;
    erro.hidden = true;
    erro.textContent = "";
  }

  function pedir(url, opcoes) {
    return fetch(url, opcoes).then(function (resposta) {
      return resposta.json().then(function (corpo) {
        return { status: resposta.status, corpo: corpo };
      }).catch(function () {
        return { status: resposta.status, corpo: null };
      });
    });
  }

  function jsonPost(url, corpo) {
    return pedir(url, {
      method: "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(corpo)
    });
  }

  function token(nome) {
    return getComputedStyle(document.documentElement).getPropertyValue(nome).trim();
  }

  function cores() {
    return [
      token("--af-primary"),
      token("--af-success"),
      token("--af-warning"),
      token("--af-danger"),
      token("--af-gold"),
      token("--af-info")
    ].filter(Boolean);
  }

  function numeroFiel(texto) {
    if (typeof texto !== "string" || !/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(texto)) return null;
    var numero = Number(texto);
    if (!isFinite(numero)) return null;
    return numero;
  }

  function nomeVisivel(grafico) {
    var tipo = TIPOS[grafico.tipo] || grafico.tipo || "Gráfico";
    var operacao = ROTULOS[grafico.operacao] || grafico.operacao || "";
    return operacao ? tipo + " · " + operacao : tipo;
  }

  function limparGraficos() {
    graficosAtivos.forEach(function (grafico) {
      if (grafico && grafico.destroy) grafico.destroy();
    });
    graficosAtivos = [];
  }

  function tabela(grafico) {
    var wrap = el("div", "agente-bi-dashboard-tabela-wrap");
    var table = el("table", "agente-bi-dashboard-tabela");
    var caption = el("caption");
    var partes = [nomeVisivel(grafico)];
    if (grafico.unidade) partes.push("unidade " + grafico.unidade);
    if (grafico.total !== null && grafico.total !== undefined) partes.push("total " + grafico.total);
    caption.textContent = partes.join(". ");
    table.appendChild(caption);
    var thead = el("thead");
    var cabeca = el("tr");
    ["Categoria", "Resultado", "Registros elegíveis"].forEach(function (texto) {
      var th = el("th", null, texto);
      th.scope = "col";
      cabeca.appendChild(th);
    });
    thead.appendChild(cabeca);
    table.appendChild(thead);
    var tbody = el("tbody");
    var linhas = Array.isArray(grafico.serie) ? grafico.serie.slice() : [];
    if (!linhas.length && grafico.resultado !== null && grafico.resultado !== undefined) {
      linhas = [{ chave: "Total", resultado: grafico.resultado, elegiveis: grafico.elegiveis }];
    }
    linhas.forEach(function (ponto) {
      var tr = el("tr");
      tr.appendChild(el("td", null, ponto.chave === undefined || ponto.chave === null || ponto.chave === "" ? "Total" : ponto.chave));
      tr.appendChild(el("td", null, ponto.resultado === null || ponto.resultado === undefined ? "" : ponto.resultado));
      tr.appendChild(el("td", null, ponto.elegiveis === null || ponto.elegiveis === undefined ? "" : ponto.elegiveis));
      tbody.appendChild(tr);
    });
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function desenharChart(destino, grafico) {
    if (grafico.tipo === "tabela" || grafico.status !== "calculado" || typeof window.Chart !== "function") {
      if (grafico.tipo !== "tabela" && grafico.status === "calculado" && typeof window.Chart !== "function") {
        destino.appendChild(el("p", "agente-bi-dashboard-nota", "Chart.js indisponível nesta página. Os valores permanecem na tabela."));
      }
      return;
    }
    var serie = Array.isArray(grafico.serie) ? grafico.serie : [];
    var rotulos = [];
    var valores = [];
    var pontos = [];
    if (grafico.tipo === "indicador") {
      var unico = numeroFiel(grafico.resultado);
      if (unico === null) return;
      rotulos = ["Total"];
      valores = [unico];
    } else if (grafico.tipo === "dispersao") {
      serie.forEach(function (ponto) {
        var y = numeroFiel(ponto.resultado);
        if (y === null || typeof ponto.chave !== "string" || !/^-?(?:0|[1-9]\d*)(?:\.\d+)?$/.test(ponto.chave)) return;
        var x = numeroFiel(ponto.chave);
        if (x === null) return;
        pontos.push({ x: x, y: y });
      });
    } else if (serie.length) {
      serie.forEach(function (ponto) {
        var valor = numeroFiel(ponto.resultado);
        if (valor === null) return;
        rotulos.push(ponto.chave === undefined || ponto.chave === null ? "" : String(ponto.chave));
        valores.push(valor);
      });
      if (!valores.length) return;
    } else {
      return;
    }
    var plot = el("div", "agente-bi-dashboard-plot");
    var canvas = document.createElement("canvas");
    canvas.setAttribute("role", "img");
    canvas.setAttribute("aria-label", nomeVisivel(grafico) + ". Os valores exatos estão na tabela.");
    plot.appendChild(canvas);
    destino.appendChild(plot);
    var paleta = cores();
    var tipoChart = grafico.tipo === "linha" ? "line" : grafico.tipo === "pizza" ? "pie" : grafico.tipo === "dispersao" ? "scatter" : "bar";
    var dados = grafico.tipo === "dispersao"
      ? { datasets: [{ label: nomeVisivel(grafico), data: pontos, backgroundColor: paleta[0] || token("--af-primary") }] }
      : {
          labels: rotulos,
          datasets: [{
            label: nomeVisivel(grafico),
            data: valores,
            backgroundColor: grafico.tipo === "pizza" ? paleta : (paleta[0] || token("--af-primary")),
            borderColor: paleta[0] || token("--af-primary"),
            borderWidth: 1
          }]
        };
    var chart = new window.Chart(canvas, {
      type: tipoChart,
      data: dados,
      options: {
        responsive: true,
        maintainAspectRatio: false,
        animation: false,
        plugins: {
          legend: {
            display: grafico.tipo !== "indicador",
            labels: { color: token("--af-text") }
          }
        },
        scales: tipoChart === "pie" || tipoChart === "scatter" && !pontos.length ? {} : (tipoChart === "scatter" ? {
          x: { ticks: { color: token("--af-text-secondary") }, grid: { color: token("--af-border-strong") } },
          y: { ticks: { color: token("--af-text-secondary") }, grid: { color: token("--af-border-strong") } }
        } : {
          x: { ticks: { color: token("--af-text-secondary") }, grid: { color: token("--af-border-strong") } },
          y: { ticks: { color: token("--af-text-secondary") }, grid: { color: token("--af-border-strong") } }
        })
      }
    });
    canvas.setAttribute("data-chart-type", tipoChart);
    canvas.setAttribute("data-cor", paleta[0] || "");
    graficosAtivos.push(chart);
  }

  function desenharGraficos(consulta) {
    limparGraficos();
    if (!grade) return;
    grade.textContent = "";
    (consulta.graficos || []).forEach(function (grafico) {
      var card = el("article", "agente-bi-dashboard-card");
      card.setAttribute("data-ref", grafico.ref || "");
      card.setAttribute("data-status", grafico.status || "");
      var heading = el("h4", "agente-bi-dashboard-card-titulo", nomeVisivel(grafico));
      heading.id = "agente-bi-grafico-" + (grafico.ref || "item");
      card.appendChild(heading);
      if (grafico.unidade) card.appendChild(el("p", "agente-bi-dashboard-meta", "Unidade: " + grafico.unidade));
      if (grafico.status === "bloqueado") {
        card.appendChild(el("p", "agente-bi-dashboard-bloqueado", "Definição bloqueada. Motivo: " + (grafico.motivo || "indisponivel") + "."));
      } else if (grafico.resultado !== null && grafico.resultado !== undefined) {
        var valor = el("p", "agente-bi-dashboard-valor");
        valor.setAttribute("data-resultado", grafico.resultado);
        valor.textContent = grafico.resultado;
        card.appendChild(valor);
      }
      if (grafico.total !== null && grafico.total !== undefined) {
        card.appendChild(el("p", "agente-bi-dashboard-meta", "Total do cálculo: " + grafico.total));
      }
      if (grafico.status !== "bloqueado") card.appendChild(tabela(grafico));
      desenharChart(card, grafico);
      var avisos = Array.isArray(grafico.avisos) ? grafico.avisos : [];
      if (avisos.length) {
        var ul = el("ul", "agente-bi-dashboard-avisos");
        avisos.forEach(function (aviso) {
          var frase = AVISOS[aviso] ? AVISOS[aviso] + " (" + aviso + ")" : aviso;
          ul.appendChild(el("li", null, frase));
        });
        card.appendChild(ul);
      }
      grade.appendChild(card);
    });
  }

  function valorFiltro(campo, tipo) {
    if (tipo === "booleano") return campo.value === "true";
    return campo.value;
  }

  function desenharFiltros(consulta) {
    if (!formulario) return;
    formulario.textContent = "";
    var filtros = Array.isArray(consulta.filtros) ? consulta.filtros : [];
    if (!filtros.length) {
      formulario.appendChild(el("p", "agente-bi-dashboard-nota", "Esta definição não declara filtros."));
      return;
    }
    filtros.forEach(function (filtro) {
      var label = el("label", "agente-bi-dashboard-campo");
      var legenda = filtro.nome + (filtro.operador ? " (" + filtro.operador + ")" : "");
      label.appendChild(el("span", null, legenda));
      var campo;
      if (filtro.tipo === "booleano") {
        campo = el("select");
        [["", "Escolha"], ["true", "Sim"], ["false", "Não"]].forEach(function (opcao) {
          var item = el("option", null, opcao[1]);
          item.value = opcao[0];
          campo.appendChild(item);
        });
      } else {
        campo = el("input");
        campo.type = filtro.tipo === "data" ? "date" : "text";
        if (filtro.tipo === "numero") campo.setAttribute("inputmode", "decimal");
        campo.maxLength = filtro.tipo === "numero" ? 40 : 80;
        campo.autocomplete = "off";
      }
      campo.name = filtro.nome;
      campo.required = filtro.obrigatorio === true;
      campo.setAttribute("data-tipo", filtro.tipo);
      label.appendChild(campo);
      formulario.appendChild(label);
    });
    var aplicar = el("button", null, "Aplicar filtros");
    aplicar.type = "button";
    aplicar.addEventListener("click", aplicarFiltros);
    formulario.appendChild(aplicar);
    var salvar = el("button", null, "Salvar consulta filtrada");
    salvar.type = "button";
    salvar.hidden = !consultaAtual || !painelAtual || consultaAtual.calculo_id === painelAtual.analise_id;
    salvar.addEventListener("click", salvarConsulta);
    formulario.appendChild(salvar);
  }

  function aplicarFiltros() {
    if (ocupado || !painelAtual) return;
    var parametros = {};
    var campos = formulario.querySelectorAll("input, select");
    for (var i = 0; i < campos.length; i += 1) {
      var campo = campos[i];
      if (!campo.name) continue;
      if (campo.required && !campo.value) {
        mostrarErro("informar_parametro");
        anunciar("Informe os filtros exigidos pela definição.");
        return;
      }
      if (!campo.value) continue;
      parametros[campo.name] = valorFiltro(campo, campo.getAttribute("data-tipo"));
    }
    ocupar(true);
    limparErro();
    anunciar("Aplicando filtros no cálculo gravado.");
    var url = raiz.getAttribute("data-dashboards-url") + "/" + painelAtual.id + "/filtros";
    jsonPost(url, { parametros: parametros }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.consulta) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível aplicar os filtros.");
        return;
      }
      consultaAtual = resposta.corpo.consulta;
      desenharGraficos(consultaAtual);
      var salvar = formulario.querySelector("button:last-of-type");
      if (salvar) salvar.hidden = consultaAtual.calculo_id === painelAtual.analise_id;
      anunciar("Filtros aplicados. Os gráficos repetem a memória do cálculo.");
      if (tituloAberto) tituloAberto.focus();
    }).catch(function () {
      mostrarErro("");
      anunciar("Não foi possível aplicar os filtros.");
    }).then(function () {
      ocupar(false);
    });
  }

  function salvarConsulta() {
    if (ocupado || !consultaAtual) return;
    ocupar(true);
    limparErro();
    anunciar("Salvando a consulta filtrada.");
    jsonPost(raiz.getAttribute("data-montar-url"), { analise_id: consultaAtual.calculo_id }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível salvar o dashboard.");
        return;
      }
      mostrarConsulta(resposta.corpo);
      return carregarLista();
    }).catch(function () {
      mostrarErro("");
    }).then(function () {
      ocupar(false);
    });
  }

  function mostrarConsulta(corpo) {
    painelAtual = corpo.dashboard;
    consultaAtual = corpo.consulta;
    if (aberto) aberto.hidden = false;
    if (tituloAberto) {
      tituloAberto.textContent = "Revisão " + consultaAtual.revisao + " de " + consultaAtual.chave;
    }
    if (vinculo) {
      vinculo.textContent = "Dataset " + consultaAtual.dataset_id + ", versão " + consultaAtual.versao + ". Valores do cálculo " + consultaAtual.calculo_id + ".";
    }
    desenharAtualizacao();
    desenharEvolucao();
    desenharFiltros(consultaAtual);
    desenharGraficos(consultaAtual);
    anunciar("Dashboard aberto com " + (consultaAtual.graficos || []).length + " gráficos.");
    if (tituloAberto) tituloAberto.focus();
  }

  function desenharAtualizacao() {
    if (!atualizacao) return;
    atualizacao.textContent = "";
    if (raiz.getAttribute("data-atualizacao") !== "1" || !painelAtual) {
      atualizacao.hidden = true;
      return;
    }
    atualizacao.hidden = false;
    var modo = el("label", "agente-bi-dashboard-campo");
    modo.appendChild(document.createTextNode("Dados"));
    var selecao = el("select");
    selecao.name = "modo";
    [["substituir", "Substituir"], ["acrescentar", "Acrescentar"]].forEach(function (item) {
      var opcao = el("option", null, item[1]);
      opcao.value = item[0];
      selecao.appendChild(opcao);
    });
    modo.appendChild(selecao);
    var arquivo = el("label", "agente-bi-dashboard-campo");
    arquivo.appendChild(document.createTextNode("Arquivo"));
    var campo = el("input");
    campo.type = "file";
    campo.name = "arquivos";
    campo.accept = ".xlsx,.csv,text/csv,application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
    arquivo.appendChild(campo);
    var enviar = el("button", null, "Atualizar dados");
    enviar.type = "button";
    enviar.addEventListener("click", function () {
      if (ocupado || !painelAtual) return;
      if (!campo.files || !campo.files.length) {
        mostrarErro("payload_invalido");
        anunciar("Escolha o arquivo compatível para atualizar o dashboard.");
        return;
      }
      var dados = new FormData();
      dados.append("modo", selecao.value);
      dados.append("arquivos", campo.files[0]);
      ocupar(true);
      limparErro();
      anunciar("Conferindo compatibilidade e recalculando sem IA.");
      pedir(raiz.getAttribute("data-dashboards-url") + "/" + painelAtual.id + "/atualizacao", {
        method: "POST",
        credentials: "same-origin",
        headers: { Accept: "application/json" },
        body: dados
      }).then(function (resposta) {
        if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.consulta) {
          mostrarErro(resposta.corpo && resposta.corpo.codigo);
          anunciar("A atualização automática foi bloqueada.");
          return;
        }
        mostrarConsulta(resposta.corpo);
        return carregarLista();
      }).catch(function () {
        mostrarErro("");
        anunciar("Não foi possível atualizar o dashboard.");
      }).then(function () {
        ocupar(false);
      });
    });
    atualizacao.appendChild(modo);
    atualizacao.appendChild(arquivo);
    atualizacao.appendChild(enviar);
  }

  function desenharEvolucao() {
    if (!evolucao) return;
    evolucao.textContent = "";
    if (!painelAtual) {
      evolucao.hidden = true;
      return;
    }
    evolucao.hidden = false;
    var campo = el("label", "agente-bi-dashboard-campo");
    campo.appendChild(document.createTextNode("Pedido em texto"));
    var entrada = el("input");
    entrada.type = "text";
    entrada.name = "texto";
    entrada.maxLength = 500;
    entrada.autocomplete = "off";
    entrada.setAttribute("aria-label", "Pedido para alterar o dashboard");
    campo.appendChild(entrada);
    var aplicar = el("button", null, "Aplicar pedido");
    aplicar.type = "button";
    aplicar.addEventListener("click", function () {
      if (ocupado || !painelAtual) return;
      var texto = entrada.value.trim();
      if (!texto) {
        anunciar("Escreva o que deseja alterar no dashboard.");
        return;
      }
      ocupar(true);
      limparErro();
      anunciar("Interpretando o pedido.");
      var corpoPedido = { texto: texto };
      var vigentes = parametrosDaRevisao(painelAtual);
      if (vigentes) corpoPedido.parametros = vigentes;
      jsonPost(raiz.getAttribute("data-dashboards-url") + "/" + painelAtual.id + "/evolucao", corpoPedido).then(function (resposta) {
        if (!resposta.corpo || resposta.corpo.ok !== true) {
          mostrarErro(resposta.corpo && resposta.corpo.codigo);
          anunciar("Não foi possível alterar o dashboard.");
          return;
        }
        if (resposta.corpo.situacao === "aplicada" && resposta.corpo.consulta) {
          guardarParametros(resposta.corpo);
          mostrarConsulta(resposta.corpo);
          return carregarLista();
        }
        mostrarResposta(resposta.corpo);
      }).catch(function () {
        mostrarErro("");
        anunciar("Não foi possível alterar o dashboard.");
      }).then(function () {
        ocupar(false);
      });
    });
    var resposta = el("div", "agente-bi-dashboard-evolucao-resposta");
    resposta.id = "agente-bi-dashboard-evolucao-resposta";
    evolucao.appendChild(campo);
    evolucao.appendChild(aplicar);
    evolucao.appendChild(resposta);
  }

  function chaveRevisao(painel) {
    return String(painel.id) + ":" + String(painel.revisao);
  }

  function parametrosDaRevisao(painel) {
    if (!painel) return null;
    var salvos = parametrosEmMemoria[chaveRevisao(painel)];
    if (!salvos || typeof salvos !== "object" || Array.isArray(salvos)) return null;
    if (!Object.keys(salvos).length) return null;
    return salvos;
  }

  function guardarParametros(corpo) {
    var painel = corpo && corpo.dashboard;
    var valores = corpo && corpo.parametros;
    if (!painel || !valores || typeof valores !== "object" || Array.isArray(valores)) return;
    parametrosEmMemoria[chaveRevisao(painel)] = JSON.parse(JSON.stringify(valores));
  }

  function mostrarResposta(corpo) {
    var destino = document.getElementById("agente-bi-dashboard-evolucao-resposta");
    if (!destino) return;
    destino.textContent = "";
    if (corpo.situacao === "esclarecimento" && corpo.esclarecimento) {
      destino.appendChild(el("p", null, corpo.esclarecimento.pergunta));
      anunciar(corpo.esclarecimento.pergunta);
      return;
    }
    if (corpo.situacao === "explicacao" && corpo.explicacao) {
      escreverBloco(destino, "Resultado comprovado", corpo.explicacao.comprovado);
      escreverBloco(destino, "Hipótese", corpo.explicacao.hipotese);
      escreverBloco(destino, "Informação não verificável", corpo.explicacao.nao_verificavel);
      anunciar("Explicação baseada nos agregados do dashboard.");
    }
  }

  function escreverBloco(destino, titulo, linhas) {
    if (!Array.isArray(linhas) || !linhas.length) return;
    destino.appendChild(el("p", null, titulo));
    linhas.forEach(function (linha) {
      destino.appendChild(el("p", null, linha));
    });
  }

  function abrir(id) {
    if (ocupado) return;
    ocupar(true);
    limparErro();
    anunciar("Abrindo dashboard.");
    pedir(raiz.getAttribute("data-dashboards-url") + "/" + id + "/consulta", {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.consulta) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível abrir o dashboard.");
        return;
      }
      mostrarConsulta(resposta.corpo);
    }).catch(function () {
      mostrarErro("");
      anunciar("Não foi possível abrir o dashboard.");
    }).then(function () {
      ocupar(false);
    });
  }

  function botao(texto, aoClicar) {
    var botaoEl = el("button", "agente-bi-dashboard-acao", texto);
    botaoEl.type = "button";
    botaoEl.addEventListener("click", aoClicar);
    return botaoEl;
  }

  function desenharLista(dashboards) {
    if (!lista) return;
    lista.textContent = "";
    if (!dashboards.length) {
      lista.appendChild(el("p", "agente-bi-dashboard-nota", "Nenhum dashboard salvo nesta conta."));
      return;
    }
    dashboards.forEach(function (item) {
      lista.appendChild(botao("Abrir revisão " + item.revisao + " de " + item.chave, function () {
        abrir(item.id);
      }));
    });
  }

  function maisRecente(analises, etapa, contrato) {
    var escolhida = null;
    analises.forEach(function (item) {
      if (!item || item.etapa !== etapa || item.estado !== "concluida" || item.contrato_versao !== contrato) return;
      if (!escolhida || item.id > escolhida.id) escolhida = item;
    });
    return escolhida;
  }

  function montarCalculo(analiseId) {
    return jsonPost(raiz.getAttribute("data-montar-url"), { analise_id: analiseId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível salvar o dashboard.");
        return null;
      }
      mostrarConsulta(resposta.corpo);
      return carregarLista();
    });
  }

  function calcularEMontar(definicaoId) {
    return jsonPost(raiz.getAttribute("data-calculo-url"), { analise_id: definicaoId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.calculo) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível calcular os indicadores.");
        return null;
      }
      return montarCalculo(resposta.corpo.calculo.id);
    });
  }

  function definirCalcularEMontar(selecaoId) {
    return jsonPost(raiz.getAttribute("data-definicao-url"), { analise_id: selecaoId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.definicao) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível preparar as definições.");
        return null;
      }
      return calcularEMontar(resposta.corpo.definicao.id);
    });
  }

  function desenharOrigens(analises) {
    if (!origens) return;
    origens.textContent = "";
    var calculo = maisRecente(analises, "execucao", "calculo.v1");
    var definicao = maisRecente(analises, "definicao", "definir.v1");
    var selecao = maisRecente(analises, "selecao", "selecao.descoberta.v1");
    if (calculo) {
      origens.appendChild(botao("Salvar dashboard do cálculo " + calculo.id, function () {
        if (ocupado) return;
        ocupar(true);
        limparErro();
        anunciar("Salvando dashboard.");
        montarCalculo(calculo.id).catch(function () {
          mostrarErro("");
        }).then(function () {
          ocupar(false);
        });
      }));
    }
    if (definicao) {
      origens.appendChild(botao("Calcular e salvar dashboard", function () {
        if (ocupado) return;
        ocupar(true);
        limparErro();
        anunciar("Calculando indicadores já definidos.");
        calcularEMontar(definicao.id).catch(function () {
          mostrarErro("");
        }).then(function () {
          ocupar(false);
        });
      }));
    }
    if (selecao) {
      origens.appendChild(botao("Montar dashboard da seleção", function () {
        if (ocupado) return;
        ocupar(true);
        limparErro();
        anunciar("Preparando o dashboard da seleção aprovada.");
        definirCalcularEMontar(selecao.id).catch(function () {
          mostrarErro("");
        }).then(function () {
          ocupar(false);
        });
      }));
    }
  }

  function ocupar(ativo) {
    ocupado = ativo;
    raiz.setAttribute("aria-busy", ativo ? "true" : "false");
    raiz.querySelectorAll("button").forEach(function (botaoEl) {
      botaoEl.disabled = ativo;
    });
  }

  function carregarLista() {
    return pedir(raiz.getAttribute("data-dashboards-url"), {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      var dashboards = resposta.corpo && resposta.corpo.dashboards;
      if (!resposta.corpo || resposta.corpo.ok !== true || !Array.isArray(dashboards)) {
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível ler os dashboards salvos.");
        return;
      }
      desenharLista(dashboards);
      if (!consultaAtual) {
        anunciar(dashboards.length ? "Escolha um dashboard salvo para reabrir." : "Nenhum dashboard salvo nesta conta.");
      }
    });
  }

  function carregarOrigens() {
    return pedir(raiz.getAttribute("data-analises-url"), {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      var analises = resposta.corpo && resposta.corpo.analises;
      if (!resposta.corpo || resposta.corpo.ok !== true || !Array.isArray(analises)) return;
      desenharOrigens(analises);
    }).catch(function () {
      return null;
    });
  }

  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (consultaAtual) desenharGraficos(consultaAtual);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  anunciar("Carregando dashboards salvos.");
  carregarLista().then(carregarOrigens).catch(function () {
    mostrarErro("");
    anunciar("Não foi possível ler os dashboards salvos.");
  });
})();
