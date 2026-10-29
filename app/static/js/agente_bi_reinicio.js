(function () {
  var botao = document.getElementById("agente-bi-reinicio");
  var dialogo = document.getElementById("agente-bi-reinicio-dialogo");
  if (!botao || !dialogo) return;

  var cancelar = document.getElementById("agente-bi-reinicio-cancelar");
  var confirmar = document.getElementById("agente-bi-reinicio-confirmar");
  var erro = document.getElementById("agente-bi-reinicio-erro");
  var url = botao.getAttribute("data-reinicio-url");
  var enviando = false;

  var mensagens = {
    execucao_em_andamento: "Há uma execução em andamento. Os dados atuais foram mantidos.",
    operacao_nao_permitida: "A operação não foi permitida. Os dados atuais foram mantidos.",
    autenticacao_necessaria: "É necessário entrar novamente. Os dados atuais foram mantidos.",
    reinicio_indisponivel: "Não foi possível gerar um novo BI. Os dados atuais foram mantidos."
  };
  var inconclusivo = "Não foi possível confirmar o resultado. Recarregue a página para verificar.";

  function limparErro() {
    if (!erro) return;
    erro.hidden = true;
    erro.textContent = "";
  }

  function mostrarErro(texto) {
    if (!erro) return;
    erro.hidden = false;
    erro.textContent = texto;
  }

  function abrir() {
    if (enviando) return;
    limparErro();
    if (typeof dialogo.showModal === "function") dialogo.showModal();
  }

  function fechar() {
    if (enviando) return;
    limparErro();
    if (dialogo.open && typeof dialogo.close === "function") dialogo.close();
  }

  function liberar() {
    enviando = false;
    dialogo.removeAttribute("aria-busy");
    if (confirmar) confirmar.disabled = false;
    if (cancelar) cancelar.disabled = false;
    botao.disabled = false;
  }

  function mensagemDe(resultado) {
    var codigo = resultado && resultado.corpo && resultado.corpo.codigo;
    if (codigo && mensagens[codigo]) return mensagens[codigo];
    return inconclusivo;
  }

  botao.addEventListener("click", function (event) {
    event.preventDefault();
    abrir();
  });

  if (cancelar) cancelar.addEventListener("click", fechar);

  dialogo.addEventListener("cancel", function (event) {
    if (enviando) event.preventDefault();
  });

  if (confirmar) {
    confirmar.addEventListener("click", function () {
      if (enviando || !url) return;
      enviando = true;
      dialogo.setAttribute("aria-busy", "true");
      confirmar.disabled = true;
      if (cancelar) cancelar.disabled = true;
      botao.disabled = true;
      limparErro();
      fetch(url, {
        method: "POST",
        credentials: "same-origin",
        headers: { "Accept": "application/json", "Content-Type": "application/json" },
        body: "{}"
      }).then(function (resposta) {
        return resposta.json().then(
          function (corpo) {
            return { status: resposta.status, corpo: corpo || {} };
          },
          function () {
            return { status: resposta.status, corpo: {} };
          }
        );
      }).then(function (resultado) {
        if (resultado.status >= 200 && resultado.status < 300 && resultado.corpo && resultado.corpo.ok === true) {
          document.documentElement.setAttribute("data-agente-bi-reiniciado", "1");
          botao.hidden = true;
          window.location.reload();
          return;
        }
        liberar();
        mostrarErro(mensagemDe(resultado));
      }).catch(function () {
        liberar();
        mostrarErro(inconclusivo);
      });
    });
  }
})();
