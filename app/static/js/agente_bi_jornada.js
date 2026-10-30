(function () {
  var shell = document.getElementById("agente-bi-shell");
  var jornada = document.getElementById("agente-bi-jornada");
  if (!shell || !jornada) return;

  var FASES = ["upload", "preparados", "analise", "descobertas", "selecao", "construcao", "dashboard"];
  var NOMES = {
    upload: "Upload dos dados",
    preparados: "Dados preparados",
    analise: "Como deseja analisar",
    descobertas: "Descobertas e esclarecimentos",
    selecao: "Seleção dos gráficos",
    construcao: "Construção do dashboard",
    dashboard: "Dashboard e continuidade"
  };
  var RAIZES = {
    upload: "agente-bi-messages",
    preparados: "agente-bi-fase-preparados",
    analise: "agente-bi-descoberta-form",
    descobertas: "agente-bi-fase-descobertas",
    selecao: "agente-bi-fase-selecao",
    construcao: "agente-bi-fase-construcao",
    dashboard: "agente-bi-fase-dashboard"
  };
  var faseAtual = "upload";
  var seguirAutomatico = true;
  var ajustando = false;
  var casaAprovada = null;
  var observador = null;

  function observar() {
    if (!observador || !shell) return;
    observador.disconnect();
    observador.observe(shell, {
      subtree: true,
      childList: true,
      attributes: true,
      attributeFilter: ["hidden", "data-selecao", "data-versao", "data-restaurada"]
    });
  }

  function porId(id) {
    return document.getElementById(id);
  }

  function gradeCheia() {
    var grade = porId("agente-bi-dashboard-grade");
    return !!(grade && grade.childNodes.length);
  }

  function textoDe(id) {
    var node = porId(id);
    return node ? node.textContent || "" : "";
  }

  function preparadoConfirmado() {
    var texto = textoDe("agente-bi-preparacao-lista");
    if (texto.indexOf("seleção aprovada") !== -1) return true;
    if (texto.indexOf("não persistido") !== -1) return false;
    if (texto.indexOf("preparado") !== -1) return true;
    return selecaoAprovada();
  }

  function descobertaConfirmada() {
    var resultado = porId("agente-bi-resultado");
    if (!resultado || resultado.hidden) return false;
    return !!(textoDe("agente-bi-descobertas").trim() || textoDe("agente-bi-sugestoes").trim());
  }

  function selecaoAprovada() {
    var painel = porId("agente-bi-aprovada");
    return !!(painel && (painel.getAttribute("data-selecao") || painel.getAttribute("data-versao")));
  }

  function construcaoConfirmada() {
    var origens = porId("agente-bi-dashboard-origens");
    var progresso = porId("agente-bi-montagem-progresso");
    return selecaoAprovada()
      || !!(origens && origens.querySelector("button"))
      || !!(progresso && progresso.textContent.trim());
  }

  function dashboardConfirmado() {
    var lista = porId("agente-bi-dashboard-lista");
    return gradeCheia() || !!(lista && lista.querySelector("button"));
  }

  function faseAlcancada(nome) {
    if (nome === "upload") return true;
    if (nome === "preparados" || nome === "analise") return preparadoConfirmado();
    if (nome === "descobertas" || nome === "selecao") return descobertaConfirmada();
    if (nome === "construcao") return construcaoConfirmada();
    if (nome === "dashboard") return dashboardConfirmado();
    return false;
  }

  function faseMaisAvancada() {
    var escolhida = "upload";
    FASES.forEach(function (nome) {
      if (faseAlcancada(nome)) escolhida = nome;
    });
    return escolhida;
  }

  function faseVisivel(nome, fase) {
    if (nome === "descobertas") return fase === "descobertas" || fase === "selecao";
    return nome === fase;
  }

  function mover(node, destino) {
    if (node && destino && node.parentNode !== destino) destino.appendChild(node);
  }

  function moverFeedback(fase) {
    var slot = document.querySelector('[data-jornada-feedback="' + fase + '"]');
    if (!slot) return;
    mover(porId("agente-bi-descoberta-status"), slot);
    mover(porId("agente-bi-descoberta-erro"), slot);
    var erroPainel = porId("agente-bi-dashboard-erro");
    var statusPainel = porId("agente-bi-dashboard-status");
    var trazerPainel = fase === "dashboard" || (erroPainel && !erroPainel.hidden);
    if (!trazerPainel) return;
    mover(statusPainel, slot);
    mover(erroPainel, slot);
  }

  function posicionarAprovada(fase) {
    var aprovada = porId("agente-bi-aprovada");
    if (!aprovada) return;
    if (!casaAprovada) casaAprovada = aprovada.parentNode;
    var construcao = porId("agente-bi-fase-construcao");
    if (fase === "construcao" && construcao) {
      var ancora = construcao.querySelector("[data-jornada-feedback]");
      if (ancora && ancora.nextSibling) construcao.insertBefore(aprovada, ancora.nextSibling);
      else construcao.appendChild(aprovada);
      return;
    }
    if (casaAprovada && aprovada.parentNode !== casaAprovada) casaAprovada.appendChild(aprovada);
  }

  function ajustarRevisao(fase) {
    var resultado = porId("agente-bi-resultado");
    var sugestoes = porId("agente-bi-sugestoes");
    var instrucao = porId("agente-bi-instrucao");
    var titulo = porId("agente-bi-resultado-titulo");
    var revisaoTitulo = porId("agente-bi-revisao-titulo");
    var descobertas = porId("agente-bi-descobertas");
    var criticas = porId("agente-bi-criticas");
    var perguntas = porId("agente-bi-perguntas");
    var naRevisao = fase === "descobertas";
    var naSelecao = fase === "selecao";
    if (resultado && (naRevisao || naSelecao)) resultado.hidden = false;
    [titulo, revisaoTitulo, descobertas, criticas, perguntas].forEach(function (node) {
      if (node) node.hidden = !naRevisao;
    });
    if (instrucao) {
      if (naSelecao) instrucao.hidden = true;
      else if (naRevisao) instrucao.hidden = !instrucao.textContent.trim();
    }
    if (sugestoes) sugestoes.classList.toggle("is-so-revisao", naRevisao);
  }

  function aplicar(fase) {
    if (FASES.indexOf(fase) === -1) fase = "upload";
    ajustando = true;
    if (observador) observador.disconnect();
    faseAtual = fase;
    shell.setAttribute("data-jornada-fase", fase);
    moverFeedback(fase);
    posicionarAprovada(fase);
    Object.keys(RAIZES).forEach(function (nome) {
      var node = porId(RAIZES[nome]);
      if (node) node.hidden = !faseVisivel(nome, fase);
    });
    var descoberta = porId("agente-bi-descoberta");
    var painel = porId("agente-bi-dashboard");
    if (descoberta) {
      descoberta.hidden = !(fase === "preparados" || fase === "analise" || fase === "descobertas" || fase === "selecao");
    }
    if (painel) painel.hidden = !(fase === "construcao" || fase === "dashboard");
    ajustarRevisao(fase);
    if (fase === "analise" && selecaoAprovada()) {
      var modalidades = porId("agente-bi-modalidades");
      if (modalidades) modalidades.disabled = true;
    }
    if (fase === "preparados" && !selecaoAprovada()) {
      var prosseguir = porId("agente-bi-prosseguir");
      if (prosseguir && preparadoConfirmado()) prosseguir.hidden = false;
    }
    if (fase === "construcao") {
      var progresso = porId("agente-bi-montagem-progresso");
      if (progresso && progresso.textContent.trim()) progresso.hidden = false;
    }
    if (fase === "dashboard") {
      var aberto = porId("agente-bi-dashboard-aberto");
      if (aberto) aberto.hidden = !gradeCheia();
    }
    atualizarControles();
    ajustando = false;
    observar();
  }

  function atualizarControles() {
    var atual = porId("agente-bi-jornada-atual");
    var indice = FASES.indexOf(faseAtual);
    if (atual) {
      atual.textContent = "Etapa " + (indice + 1) + " de " + FASES.length + ". " + NOMES[faseAtual] + ".";
    }
    jornada.querySelectorAll("[data-jornada-ir]").forEach(function (botao) {
      var nome = botao.getAttribute("data-jornada-ir");
      var ativa = nome === faseAtual;
      var alcancada = faseAlcancada(nome);
      botao.disabled = !alcancada;
      botao.setAttribute("aria-disabled", alcancada ? "false" : "true");
      if (ativa) botao.setAttribute("aria-current", "step");
      else botao.removeAttribute("aria-current");
    });
    var voltar = porId("agente-bi-jornada-voltar");
    var avancar = porId("agente-bi-jornada-avancar");
    var anterior = null;
    var seguinte = null;
    for (var i = indice - 1; i >= 0; i -= 1) {
      if (faseAlcancada(FASES[i])) {
        anterior = FASES[i];
        break;
      }
    }
    for (var j = indice + 1; j < FASES.length; j += 1) {
      if (faseAlcancada(FASES[j])) {
        seguinte = FASES[j];
        break;
      }
    }
    if (voltar) {
      voltar.disabled = !anterior;
      voltar.setAttribute("aria-disabled", anterior ? "false" : "true");
      voltar.setAttribute("data-jornada-destino", anterior || "");
    }
    if (avancar) {
      avancar.disabled = !seguinte;
      avancar.setAttribute("aria-disabled", seguinte ? "false" : "true");
      avancar.setAttribute("data-jornada-destino", seguinte || "");
    }
  }

  function ir(fase, origem) {
    if (FASES.indexOf(fase) === -1 || !faseAlcancada(fase)) return;
    if (origem === "usuario") seguirAutomatico = false;
    if (origem !== "auto" && fase !== "dashboard") {
      ajustando = true;
      var aberto = porId("agente-bi-dashboard-aberto");
      if (aberto) aberto.hidden = true;
      ajustando = false;
    }
    if (fase === faseAtual && origem !== "usuario") {
      aplicar(faseAtual);
      return;
    }
    aplicar(fase);
  }

  function sincronizar() {
    if (ajustando) return;
    var aberto = porId("agente-bi-dashboard-aberto");
    if (aberto && !aberto.hidden && gradeCheia()) {
      ir("dashboard", "acao");
      return;
    }
    if (seguirAutomatico) ir(faseMaisAvancada(), "auto");
    else aplicar(faseAtual);
  }

  jornada.addEventListener("click", function (event) {
    var botao = event.target.closest("button");
    if (!botao || botao.disabled) return;
    if (botao.hasAttribute("data-jornada-ir")) {
      ir(botao.getAttribute("data-jornada-ir"), "usuario");
      return;
    }
    if (botao.id === "agente-bi-jornada-voltar" || botao.id === "agente-bi-jornada-avancar") {
      ir(botao.getAttribute("data-jornada-destino"), "usuario");
    }
  });

  document.addEventListener("agente-bi-jornada", function (event) {
    var detalhe = event.detail || {};
    if (detalhe.fase) {
      seguirAutomatico = false;
      ir(detalhe.fase, "acao");
      return;
    }
    sincronizar();
  });

  if (window.MutationObserver) {
    observador = new MutationObserver(function () {
      if (ajustando) return;
      sincronizar();
    });
  }

  aplicar("upload");
  sincronizar();
})();
