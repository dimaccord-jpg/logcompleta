(function () {
  var detalhe = document.getElementById("agente-bi-dashboard-detalhe");
  var raiz = document.getElementById("agente-bi-dashboard");
  if (!detalhe && !raiz) return;

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
  var continuidadeAtual = null;
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
    conflito_versao: "A revisão mudou. O pedido anterior não foi aplicado.",
    identidade_inconsistente: "O contexto recuperado não corresponde a esta revisão.",
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
    revelarPainel();
    erro.hidden = false;
    erro.textContent = ERROS[codigo] || "Não foi possível consultar o dashboard.";
    avisarJornada(null);
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
        destino.appendChild(el("p", "agente-bi-dashboard-nota", "Chart.js indisponível nesta página. Os valores permanecem em Ver dados."));
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
    canvas.setAttribute("aria-label", nomeVisivel(grafico) + ". Os valores exatos estão em Ver dados.");
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

  function ligacaoDados(consulta, grafico) {
    var base = raiz ? raiz.getAttribute("data-detalhe-base") : "";
    if (!base || !consulta || !grafico || !grafico.ref) return null;
    var ancora = document.createElement("a");
    ancora.className = "agente-bi-dashboard-acao";
    ancora.textContent = "Ver dados";
    ancora.href = base + "/" + consulta.dashboard_id
      + "/revisoes/" + consulta.revisao
      + "/calculos/" + consulta.calculo_id
      + "/graficos/" + encodeURIComponent(grafico.ref);
    ancora.target = "_blank";
    ancora.rel = "noopener noreferrer";
    ancora.setAttribute("aria-label", "Ver dados de " + nomeVisivel(grafico));
    return ancora;
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
      desenharChart(card, grafico);
      var dados = ligacaoDados(consulta, grafico);
      if (dados) card.appendChild(dados);
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
    continuidadeAtual = corpo && corpo.continuidade && typeof corpo.continuidade === "object" ? corpo.continuidade : null;
    revelarPainel();
    recolherFormulariosAnteriores();
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
    avisarJornada("dashboard");
    if (
      continuidadeAtual &&
      continuidadeAtual.situacao === "esclarecimento" &&
      continuidadeAtual.esclarecimento
    ) {
      mostrarResposta({ situacao: "esclarecimento", esclarecimento: continuidadeAtual.esclarecimento });
    }
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
      var vigentes = parametrosVigentes();
      if (vigentes) dados.append("parametros", JSON.stringify(vigentes));
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
        if (vigentes && resposta.corpo.dashboard) {
          parametrosEmMemoria[chaveRevisao(resposta.corpo.dashboard)] = JSON.parse(JSON.stringify(vigentes));
        }
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
    campo.appendChild(document.createTextNode("Pedido ou pergunta"));
    var entrada = el("input");
    entrada.type = "text";
    entrada.name = "texto";
    entrada.maxLength = 500;
    entrada.autocomplete = "off";
    entrada.setAttribute("aria-label", "Pedido ou pergunta sobre o dashboard");
    campo.appendChild(entrada);
    var aplicar = el("button", null, "Aplicar pedido");
    aplicar.type = "button";
    aplicar.addEventListener("click", function () {
      if (ocupado || !painelAtual) return;
      var texto = entrada.value.trim();
      var informados = parametrosDoEsclarecimento();
      var retomada = !texto
        && continuidadeAtual
        && continuidadeAtual.situacao === "esclarecimento"
        && continuidadeAtual.plano_id
        && informados;
      if (!texto && !retomada) {
        anunciar("Escreva o pedido ou a pergunta sobre o dashboard.");
        return;
      }
      ocupar(true);
      limparErro();
      anunciar("Interpretando o pedido.");
      var corpoPedido = retomada
        ? { plano_id: continuidadeAtual.plano_id, parametros: informados }
        : { texto: texto };
      if (!retomada) {
        var vigentes = parametrosDaRevisao(painelAtual);
        if (vigentes) corpoPedido.parametros = vigentes;
        if (informados) {
          corpoPedido.parametros = Object.assign({}, corpoPedido.parametros || {}, informados);
        }
      }
      jsonPost(raiz.getAttribute("data-dashboards-url") + "/" + painelAtual.id + "/evolucao", corpoPedido).then(function (resposta) {
        if (!resposta.corpo || resposta.corpo.ok !== true) {
          mostrarErro(resposta.corpo && resposta.corpo.codigo);
          anunciar("Não foi possível concluir o pedido.");
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
        anunciar("Não foi possível concluir o pedido.");
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

  function parametrosVigentes() {
    var saida = {};
    var memoria = parametrosDaRevisao(painelAtual);
    if (memoria) {
      Object.keys(memoria).forEach(function (nome) {
        saida[nome] = memoria[nome];
      });
    }
    if (formulario) {
      var campos = formulario.querySelectorAll("input, select");
      for (var i = 0; i < campos.length; i += 1) {
        var campo = campos[i];
        if (!campo.name || !campo.value) continue;
        saida[campo.name] = valorFiltro(campo, campo.getAttribute("data-tipo"));
      }
    }
    return Object.keys(saida).length ? saida : null;
  }

  function guardarParametros(corpo) {
    var painel = corpo && corpo.dashboard;
    var valores = corpo && corpo.parametros;
    if (!painel || !valores || typeof valores !== "object" || Array.isArray(valores)) return;
    parametrosEmMemoria[chaveRevisao(painel)] = JSON.parse(JSON.stringify(valores));
  }

  function parametrosDoEsclarecimento() {
    if (!evolucao) return null;
    var campos = evolucao.querySelectorAll("[data-parametro]");
    var saida = {};
    for (var i = 0; i < campos.length; i += 1) {
      var campo = campos[i];
      if (!campo.name || !campo.value) continue;
      saida[campo.name] = campo.value;
    }
    return Object.keys(saida).length ? saida : null;
  }

  function mostrarResposta(corpo) {
    var destino = document.getElementById("agente-bi-dashboard-evolucao-resposta");
    if (!destino) return;
    destino.textContent = "";
    if (corpo.situacao === "esclarecimento" && corpo.esclarecimento) {
      destino.appendChild(el("p", null, corpo.esclarecimento.pergunta));
      var pedidos = corpo.esclarecimento.parametros;
      if (Array.isArray(pedidos)) {
        pedidos.forEach(function (nome) {
          if (typeof nome !== "string" || !nome) return;
          var campo = el("label", "agente-bi-dashboard-campo");
          campo.appendChild(document.createTextNode(nome));
          var entrada = el("input");
          entrada.type = "text";
          entrada.name = nome;
          entrada.maxLength = 80;
          entrada.autocomplete = "off";
          entrada.setAttribute("data-parametro", nome);
          entrada.setAttribute("aria-label", "Valor de " + nome);
          campo.appendChild(entrada);
          destino.appendChild(campo);
        });
      }
      anunciar(corpo.esclarecimento.pergunta);
      return;
    }
    if (corpo.situacao === "explicacao" && corpo.explicacao) {
      var separado = separarFatoELeitura(corpo.explicacao.comprovado);
      escreverBloco(destino, "Resultado comprovado", separado.fatos);
      escreverBloco(destino, "Interpretação não certificada", separado.leituras);
      escreverBloco(destino, "Hipótese", corpo.explicacao.hipotese);
      escreverBloco(destino, "Informação não verificável", corpo.explicacao.nao_verificavel);
      anunciar("Explicação baseada nos agregados do dashboard.");
    }
  }

  function separarFatoELeitura(itens) {
    var fatos = [];
    var leituras = [];
    if (!Array.isArray(itens)) return { fatos: fatos, leituras: leituras };
    itens.forEach(function (item) {
      if (!item || typeof item !== "object" || Array.isArray(item)) return;
      if (typeof item.fato !== "string" || !item.fato) return;
      fatos.push(item.fato);
      if (typeof item.leitura === "string" && item.leitura) leituras.push(item.leitura);
    });
    return { fatos: fatos, leituras: leituras };
  }

  function escreverBloco(destino, titulo, linhas) {
    if (!Array.isArray(linhas) || !linhas.length) return;
    var bloco = el("div");
    bloco.setAttribute("data-bloco", titulo);
    bloco.appendChild(el("p", null, titulo));
    linhas.forEach(function (linha) {
      if (typeof linha !== "string") return;
      bloco.appendChild(el("p", null, linha));
    });
    destino.appendChild(bloco);
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
    revelarPainel();
  }

  var presenca = { datasets: "pendente", analises: "pendente", dashboards: "pendente", sessao: false };
  var passosMontagem = [];

  function revelarPainel() {
    if (raiz) raiz.hidden = false;
  }

  function avisarJornada(fase) {
    document.dispatchEvent(new CustomEvent("agente-bi-jornada", {
      detail: fase ? { fase: fase } : { sincronizar: true }
    }));
  }

  function recolherFormulariosAnteriores() {
    var descoberta = document.getElementById("agente-bi-descoberta");
    if (descoberta) descoberta.hidden = true;
    var progresso = document.getElementById("agente-bi-montagem-progresso");
    if (progresso) progresso.hidden = true;
  }

  function reinicioConfirmado() {
    return document.documentElement.getAttribute("data-agente-bi-reiniciado") === "1";
  }

  function atualizarReinicio() {
    var botao = document.getElementById("agente-bi-reinicio");
    if (!botao) return;
    if (reinicioConfirmado()) {
      botao.hidden = true;
      return;
    }
    var chaves = ["datasets", "analises", "dashboards"];
    var pendente = chaves.some(function (chave) { return presenca[chave] === "pendente"; });
    var haDados = presenca.sessao || chaves.some(function (chave) { return presenca[chave] === "sim"; });
    if (pendente && !haDados) {
      botao.hidden = true;
      return;
    }
    botao.hidden = !haDados;
  }

  function concluirLeitura(chave, valor) {
    if (valor === "falha" && (presenca[chave] === "sim" || presenca[chave] === "nao")) return;
    presenca[chave] = valor;
    atualizarReinicio();
  }

  function candidatas(analises, etapa, contrato) {
    var saida = [];
    (analises || []).forEach(function (item) {
      if (item && item.etapa === etapa && item.estado === "concluida" && item.contrato_versao === contrato) saida.push(item);
    });
    return saida;
  }

  function unica(lista) {
    return lista.length === 1 ? lista[0] : null;
  }

  function selecaoDaAprovacao(analises) {
    var painel = document.getElementById("agente-bi-aprovada");
    var lista = candidatas(analises, "selecao", "selecao.descoberta.v1");
    var marcada = painel && painel.getAttribute("data-selecao");
    if (marcada) {
      var identificador = Number(marcada);
      for (var i = 0; i < lista.length; i += 1) {
        if (lista[i].id === identificador) return lista[i];
      }
      return null;
    }
    var versao = painel && painel.getAttribute("data-versao");
    if (versao) {
      var ligadas = lista.filter(function (item) {
        var resultado = item.resultado;
        if (!resultado || typeof resultado !== "object") return String(item.versao) === versao;
        return String(resultado.dataset_versao) === versao && item.dataset_id === resultado.dataset_id;
      });
      if (ligadas.length === 1) return ligadas[0];
      if (ligadas.length > 1) return null;
    }
    return unica(lista);
  }

  function definicaoDaSelecao(analises, selecao) {
    var lista = candidatas(analises, "definicao", "definir.v1");
    if (selecao) {
      var ligadas = lista.filter(function (item) {
        var resultado = item.resultado;
        return resultado && resultado.origem_selecao_id === selecao.id && (resultado.dataset_id == null || resultado.dataset_id === item.dataset_id);
      });
      if (ligadas.length === 1) return ligadas[0];
      if (ligadas.length > 1) return null;
      if (lista.some(function (item) { return item.resultado && item.resultado.origem_selecao_id; })) return null;
    }
    return unica(lista);
  }

  function calculoDaDefinicao(analises, definicao) {
    var lista = candidatas(analises, "execucao", "calculo.v1");
    if (definicao) {
      var ligadas = lista.filter(function (item) {
        var resultado = item.resultado;
        return resultado && resultado.origem_definicao_id === definicao.id && (resultado.dataset_id == null || resultado.dataset_id === item.dataset_id);
      });
      if (ligadas.length === 1) return ligadas[0];
      if (ligadas.length > 1) return null;
      if (lista.some(function (item) { return item.resultado && item.resultado.origem_definicao_id; })) return null;
    }
    return unica(lista);
  }

  function publicarProgresso() {
    var progresso = document.getElementById("agente-bi-montagem-progresso");
    if (!progresso) return;
    progresso.hidden = false;
    progresso.textContent = "";
    passosMontagem.forEach(function (item) {
      progresso.appendChild(el("li", "agente-bi-montagem-passo is-" + item.estado, item.texto));
    });
    revelarPainel();
  }

  function definirPasso(nome, estadoPasso, texto) {
    var achou = false;
    passosMontagem.forEach(function (item) {
      if (item.nome !== nome) return;
      item.estado = estadoPasso;
      item.texto = texto;
      achou = true;
    });
    if (!achou) passosMontagem.push({ nome: nome, estado: estadoPasso, texto: texto });
    publicarProgresso();
  }

  function iniciarMontagem() {
    passosMontagem = [];
  }

  function interromperPassoAtual() {
    var ativo = null;
    passosMontagem.forEach(function (item) {
      if (item.estado === "andamento") ativo = item;
    });
    if (!ativo) return;
    var textos = {
      "Definição": "Definição interrompida.",
      "Cálculo": "Cálculo interrompido.",
      "Montagem": "Montagem interrompida."
    };
    definirPasso(ativo.nome, "interrompido", textos[ativo.nome] || "Etapa interrompida.");
  }

  function montarCalculo(analiseId) {
    definirPasso("Montagem", "andamento", "Montagem em andamento.");
    return jsonPost(raiz.getAttribute("data-montar-url"), { analise_id: analiseId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true) {
        definirPasso("Montagem", "interrompido", "Montagem interrompida.");
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível salvar o dashboard.");
        return null;
      }
      var painelId = resposta.corpo.dashboard && resposta.corpo.dashboard.id;
      definirPasso("Montagem", "concluido", painelId ? "Montagem " + painelId + " concluída." : "Montagem concluída.");
      mostrarConsulta(resposta.corpo);
      return carregarLista();
    });
  }

  function calcularEMontar(definicaoId) {
    definirPasso("Cálculo", "andamento", "Cálculo em andamento.");
    return jsonPost(raiz.getAttribute("data-calculo-url"), { analise_id: definicaoId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.calculo) {
        definirPasso("Cálculo", "interrompido", "Cálculo interrompido.");
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível calcular os indicadores.");
        return null;
      }
      definirPasso("Cálculo", "concluido", "Cálculo " + resposta.corpo.calculo.id + " concluído.");
      return montarCalculo(resposta.corpo.calculo.id);
    });
  }

  function definirCalcularEMontar(selecaoId) {
    definirPasso("Definição", "andamento", "Definição em andamento.");
    return jsonPost(raiz.getAttribute("data-definicao-url"), { analise_id: selecaoId }).then(function (resposta) {
      if (!resposta.corpo || resposta.corpo.ok !== true || !resposta.corpo.definicao) {
        definirPasso("Definição", "interrompido", "Definição interrompida.");
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível preparar as definições.");
        return null;
      }
      definirPasso("Definição", "concluido", "Definição " + resposta.corpo.definicao.id + " concluída.");
      return calcularEMontar(resposta.corpo.definicao.id);
    });
  }

  function desenharOrigens(analises) {
    if (!origens) return;
    origens.textContent = "";
    var selecao = selecaoDaAprovacao(analises);
    var definicao = definicaoDaSelecao(analises, selecao);
    var calculo = calculoDaDefinicao(analises, definicao);
    // Seleção sem definição vinculada não recebe cálculo de outra rodada.
    if (selecao && !definicao) calculo = null;
    if (calculo || definicao || selecao) {
      origens.appendChild(el("p", "agente-bi-dashboard-nota", "A montagem só começa quando você escolher uma destas ações."));
    }
    if (calculo) {
      origens.appendChild(botao("Salvar dashboard do cálculo " + calculo.id, function () {
        if (ocupado) return;
        ocupar(true);
        limparErro();
        anunciar("Salvando dashboard.");
        iniciarMontagem();
        montarCalculo(calculo.id).catch(function () {
          mostrarErro("");
          interromperPassoAtual();
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
        iniciarMontagem();
        calcularEMontar(definicao.id).catch(function () {
          mostrarErro("");
          interromperPassoAtual();
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
        iniciarMontagem();
        definirCalcularEMontar(selecao.id).catch(function () {
          mostrarErro("");
          interromperPassoAtual();
        }).then(function () {
          ocupar(false);
        });
      }));
    }
    if (origens.querySelector("button")) revelarPainel();
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
        concluirLeitura("dashboards", "falha");
        mostrarErro(resposta.corpo && resposta.corpo.codigo);
        anunciar("Não foi possível ler os dashboards salvos.");
        return;
      }
      concluirLeitura("dashboards", dashboards.length ? "sim" : "nao");
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
      if (!resposta.corpo || resposta.corpo.ok !== true || !Array.isArray(analises)) {
        concluirLeitura("analises", "falha");
        return false;
      }
      concluirLeitura("analises", analises.length ? "sim" : "nao");
      desenharOrigens(analises);
      return !!selecaoDaAprovacao(analises);
    }).catch(function () {
      concluirLeitura("analises", "falha");
      return false;
    });
  }

  function carregarDatasets() {
    var url = raiz.getAttribute("data-datasets-url");
    if (!url) {
      concluirLeitura("datasets", "falha");
      return Promise.resolve();
    }
    return pedir(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      var datasets = resposta.corpo && resposta.corpo.datasets;
      if (!resposta.corpo || resposta.corpo.ok !== true || !Array.isArray(datasets)) {
        concluirLeitura("datasets", "falha");
        return;
      }
      concluirLeitura("datasets", datasets.length ? "sim" : "nao");
    }).catch(function () {
      concluirLeitura("datasets", "falha");
    });
  }

  function observarAprovacao(jaDisponivel) {
    var painel = document.getElementById("agente-bi-aprovada");
    if (!painel || !window.MutationObserver) return;
    if (!jaDisponivel && painel.getAttribute("data-versao")) {
      carregarOrigens();
      return;
    }
    var observador = new MutationObserver(function () {
      if (!painel.getAttribute("data-versao")) return;
      desenharOrigens([]);
      carregarOrigens();
    });
    observador.observe(painel, { attributes: true, attributeFilter: ["data-versao"] });
  }

  function iniciarDetalhe(raizDetalhe) {
    var statusDetalhe = document.getElementById("agente-bi-detalhe-status");
    var erroDetalhe = document.getElementById("agente-bi-detalhe-erro");
    var identidade = document.getElementById("agente-bi-detalhe-identidade");
    var conteudo = document.getElementById("agente-bi-detalhe-conteudo");
    var url = raizDetalhe.getAttribute("data-consulta-url");
    raizDetalhe.setAttribute("aria-busy", "true");
    if (statusDetalhe) statusDetalhe.textContent = "Carregando os dados do gráfico.";
    pedir(url, {
      method: "GET",
      credentials: "same-origin",
      headers: { Accept: "application/json" }
    }).then(function (resposta) {
      var corpo = resposta.corpo;
      var grafico = corpo && corpo.detalhe && corpo.detalhe.grafico;
      if (!corpo || corpo.ok !== true || !grafico || !corpo.detalhe) {
        if (erroDetalhe) {
          erroDetalhe.hidden = false;
          erroDetalhe.textContent = ERROS[corpo && corpo.codigo] || "Não foi possível consultar o dashboard.";
        }
        if (statusDetalhe) statusDetalhe.textContent = "Não foi possível abrir os dados do gráfico.";
        return;
      }
      if (identidade) {
        identidade.textContent = "Revisão " + corpo.detalhe.revisao + " de " + corpo.detalhe.chave + ". " + nomeVisivel(grafico) + ".";
      }
      if (conteudo) {
        conteudo.textContent = "";
        if (grafico.status === "bloqueado") {
          conteudo.appendChild(el("p", "agente-bi-dashboard-bloqueado", "Definição bloqueada. Motivo: " + (grafico.motivo || "indisponivel") + "."));
        }
        conteudo.appendChild(tabela(grafico));
      }
      if (statusDetalhe) statusDetalhe.textContent = "Dados do gráfico " + nomeVisivel(grafico) + ".";
    }).catch(function () {
      if (erroDetalhe) {
        erroDetalhe.hidden = false;
        erroDetalhe.textContent = "Não foi possível consultar o dashboard.";
      }
      if (statusDetalhe) statusDetalhe.textContent = "Não foi possível abrir os dados do gráfico.";
    }).then(function () {
      raizDetalhe.setAttribute("aria-busy", "false");
    });
  }

  if (detalhe) {
    iniciarDetalhe(detalhe);
    return;
  }
  if (!raiz) return;

  if (window.MutationObserver) {
    new MutationObserver(function () {
      if (consultaAtual) desenharGraficos(consultaAtual);
    }).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  }

  document.addEventListener("agente-bi-processamento", function () {
    if (reinicioConfirmado()) return;
    presenca.sessao = true;
    atualizarReinicio();
  });

  anunciar("Carregando dashboards salvos.");
  carregarDatasets().then(function () {
    return carregarLista();
  }).then(function () {
    return carregarOrigens();
  }).then(function (jaDisponivel) {
    observarAprovacao(jaDisponivel === true);
  }).catch(function () {
    if (presenca.dashboards === "pendente") concluirLeitura("dashboards", "falha");
    if (presenca.analises === "pendente") concluirLeitura("analises", "falha");
    if (presenca.datasets === "pendente") concluirLeitura("datasets", "falha");
    mostrarErro("");
    anunciar("Não foi possível ler os dashboards salvos.");
  });
})();
