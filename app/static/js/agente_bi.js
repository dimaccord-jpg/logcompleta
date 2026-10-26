(function () {
  var shell = document.getElementById("agente-bi-shell");
  if (!shell) return;

  var form = document.getElementById("agente-bi-form");
  var trigger = document.getElementById("agente-bi-actions-trigger");
  var menu = document.getElementById("agente-bi-actions-menu");
  var input = document.getElementById("agente-bi-input");
  var send = document.getElementById("agente-bi-send");
  var uploadItem = document.getElementById("agente-bi-upload-item");
  var guidance = document.getElementById("agente-bi-composer-guidance");
  var menuHome = menu ? menu.parentNode : null;
  var GUIDANCE = "Para começar, envie seus dados logísticos pelo botão +.";
  var guidanceAnnounced = false;

  function descobertaAtiva() {
    return !!document.getElementById("agente-bi-descoberta");
  }

  function enforceLock() {
    if (input) {
      if (input.disabled) input.disabled = false;
      if (!input.readOnly) input.readOnly = true;
      if (input.getAttribute("aria-readonly") !== "true") input.setAttribute("aria-readonly", "true");
      if (input.hasAttribute("aria-disabled")) input.removeAttribute("aria-disabled");
      if (input.hasAttribute("name")) input.removeAttribute("name");
      if (input.hasAttribute("form")) input.removeAttribute("form");
      if (input.getAttribute("contenteditable")) input.removeAttribute("contenteditable");
      if (input.value) input.value = "";
    }
    if (send) {
      if (!send.disabled) send.disabled = true;
      if (send.type !== "button") send.type = "button";
      if (send.getAttribute("aria-disabled") !== "true") send.setAttribute("aria-disabled", "true");
    }
    if (uploadItem && !descobertaAtiva()) {
      if (!uploadItem.disabled) uploadItem.disabled = true;
      if (uploadItem.getAttribute("aria-disabled") !== "true") uploadItem.setAttribute("aria-disabled", "true");
    }
    if (form && form.hasAttribute("action")) form.removeAttribute("action");
  }

  function revealGuidance() {
    if (guidanceAnnounced || !guidance) return;
    guidanceAnnounced = true;
    guidance.textContent = GUIDANCE;
    if (input) input.placeholder = "";
  }

  function lockAttempt(event) {
    if (event) {
      if (event.cancelable) event.preventDefault();
      event.stopPropagation();
    }
    enforceLock();
    revealGuidance();
  }

  function blockDrag(event) {
    event.preventDefault();
    if (event.dataTransfer) event.dataTransfer.dropEffect = "none";
  }

  function isOpen() {
    return !!(menu && !menu.hidden);
  }

  function focusableItems() {
    if (!menu) return [];
    return Array.prototype.filter.call(
      menu.querySelectorAll('[role="menuitem"]'),
      function (item) {
        return !item.disabled && item.getAttribute("aria-disabled") !== "true";
      }
    );
  }

  function positionMenu() {
    if (!trigger || !menu || !isOpen()) return;
    var rect = trigger.getBoundingClientRect();
    var gap = 8;
    var width = menu.offsetWidth || 260;
    var height = menu.offsetHeight || 220;
    var maxLeft = Math.max(8, window.innerWidth - width - 8);
    var left = rect.left;
    if (left > maxLeft) left = maxLeft;
    if (left < 8) left = 8;
    var top = rect.top - height - gap;
    if (top < 8) top = Math.min(rect.bottom + gap, Math.max(8, window.innerHeight - height - 8));
    menu.style.left = left + "px";
    menu.style.top = top + "px";
  }

  function setTriggerState(open) {
    if (!trigger) return;
    trigger.setAttribute("aria-expanded", open ? "true" : "false");
    trigger.setAttribute("aria-label", open ? "Fechar menu de ações" : "Abrir menu de ações");
    trigger.title = open ? "Fechar menu de ações" : "Abrir menu de ações";
    trigger.classList.toggle("is-open", open);
  }

  function closeMenu(returnFocus) {
    if (!menu || !isOpen()) {
      setTriggerState(false);
      return;
    }
    menu.hidden = true;
    if (menuHome && menu.parentNode !== menuHome) menuHome.appendChild(menu);
    menu.style.left = "";
    menu.style.top = "";
    setTriggerState(false);
    if (returnFocus && trigger) trigger.focus();
  }

  function openMenu(focusLast) {
    if (!menu || !trigger) return;
    enforceLock();
    menu.hidden = false;
    if (menu.parentNode !== document.body) {
      menuHome = menu.parentNode;
      document.body.appendChild(menu);
    }
    setTriggerState(true);
    positionMenu();
    var items = focusableItems();
    if (!items.length) return;
    items[focusLast ? items.length - 1 : 0].focus();
  }

  function toggleMenu() {
    if (isOpen()) closeMenu(true);
    else openMenu(false);
  }

  function blockComposerEvent(event) {
    event.preventDefault();
    event.stopPropagation();
    enforceLock();
  }

  function isEditingKey(event) {
    if (event.key === "Enter" || event.key === "Backspace" || event.key === "Delete" || event.key === "Unidentified") {
      return true;
    }
    var shortcut = (event.key || "").toLowerCase();
    if ((event.ctrlKey || event.metaKey) && (shortcut === "v" || shortcut === "x" || shortcut === "z" || shortcut === "y")) {
      return true;
    }
    if (event.ctrlKey || event.metaKey || event.altKey) return false;
    return event.key.length === 1;
  }

  enforceLock();

  if (form) {
    form.addEventListener("submit", lockAttempt, true);
    if (window.HTMLFormElement && HTMLFormElement.prototype) {
      var nativeSubmit = HTMLFormElement.prototype.submit;
      HTMLFormElement.prototype.submit = function () {
        if (this && (this === form || this.id === "agente-bi-form")) {
          enforceLock();
          revealGuidance();
          return;
        }
        return nativeSubmit.call(this);
      };
    }
    form.submit = function () {
      enforceLock();
      revealGuidance();
    };
    form.addEventListener("keydown", function (event) {
      if (event.key !== "Enter") return;
      if (event.target === trigger || (menu && menu.contains(event.target))) return;
      lockAttempt(event);
    });
    form.addEventListener("dragenter", blockDrag);
    form.addEventListener("dragover", blockDrag);
    form.addEventListener("drop", lockAttempt);
  }

  if (input) {
    input.addEventListener("pointerdown", revealGuidance);
    input.addEventListener("click", revealGuidance);
    input.addEventListener("focus", revealGuidance);
    input.addEventListener("keydown", function (event) {
      if (!isEditingKey(event)) return;
      lockAttempt(event);
    });
    input.addEventListener("beforeinput", lockAttempt);
    input.addEventListener("paste", lockAttempt);
    input.addEventListener("cut", lockAttempt);
    input.addEventListener("drop", lockAttempt);
    input.addEventListener("dragenter", blockDrag);
    input.addEventListener("dragover", blockDrag);
    input.addEventListener("input", function () {
      enforceLock();
      revealGuidance();
    });
    input.addEventListener("compositionstart", lockAttempt);
    input.addEventListener("compositionupdate", lockAttempt);
    input.addEventListener("compositionend", function () {
      enforceLock();
      revealGuidance();
    });
  }

  if (shell) {
    shell.addEventListener("dragenter", blockDrag);
    shell.addEventListener("dragover", blockDrag);
    shell.addEventListener("drop", function (event) {
      if (menu && menu.contains(event.target)) return;
      lockAttempt(event);
    });
  }

  document.addEventListener("dragenter", blockDrag, true);
  document.addEventListener("dragover", blockDrag, true);
  document.addEventListener("drop", function (event) {
    if (event.cancelable) event.preventDefault();
    if (menu && menu.contains(event.target)) return;
    if (trigger && trigger.contains(event.target)) return;
    enforceLock();
    revealGuidance();
  }, true);

  if (send) send.addEventListener("click", lockAttempt);
  if (uploadItem) {
    uploadItem.addEventListener("click", function (event) {
      if (descobertaAtiva()) return;
      blockComposerEvent(event);
    });
  }

  if (window.MutationObserver) {
    var lockObserver = new MutationObserver(function () {
      enforceLock();
    });
    if (input) {
      lockObserver.observe(input, {
        attributes: true,
        attributeFilter: ["readonly", "disabled", "aria-readonly", "aria-disabled", "name", "form", "contenteditable"]
      });
    }
    if (send) {
      lockObserver.observe(send, {
        attributes: true,
        attributeFilter: ["disabled", "type", "aria-disabled"]
      });
    }
    if (uploadItem && !descobertaAtiva()) {
      lockObserver.observe(uploadItem, {
        attributes: true,
        attributeFilter: ["disabled", "aria-disabled"]
      });
    }
    if (form) lockObserver.observe(form, { attributes: true, attributeFilter: ["action"] });
  }

  if (trigger) {
    trigger.addEventListener("click", function (event) {
      event.preventDefault();
      toggleMenu();
    });
    trigger.addEventListener("keydown", function (event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        if (!isOpen()) openMenu(false);
        else {
          var first = focusableItems()[0];
          if (first) first.focus();
        }
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openMenu(true);
      } else if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        toggleMenu();
      } else if (event.key === "Escape" && isOpen()) {
        event.preventDefault();
        closeMenu(true);
      }
    });
  }

  if (menu) {
    menu.addEventListener("dragenter", blockDrag);
    menu.addEventListener("dragover", blockDrag);
    menu.addEventListener("drop", function (event) {
      event.preventDefault();
      event.stopPropagation();
    });
    menu.addEventListener("keydown", function (event) {
      var items = focusableItems();
      var index = items.indexOf(document.activeElement);
      if (!items.length) return;
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        if (index < 0) {
          items[event.key === "ArrowUp" ? items.length - 1 : 0].focus();
          return;
        }
        var step = event.key === "ArrowDown" ? 1 : -1;
        items[(index + step + items.length) % items.length].focus();
      } else if (event.key === "Home") {
        event.preventDefault();
        items[0].focus();
      } else if (event.key === "End") {
        event.preventDefault();
        items[items.length - 1].focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        closeMenu(true);
      }
    });
    menu.addEventListener("focusout", function (event) {
      if (!isOpen()) return;
      var next = event.relatedTarget;
      if (next && (menu.contains(next) || next === trigger)) return;
      closeMenu(false);
    });
  }

  document.addEventListener("pointerdown", function (event) {
    if (!isOpen()) return;
    var target = event.target;
    if (menu.contains(target) || (trigger && trigger.contains(target))) return;
    closeMenu(false);
  });

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape" && isOpen()) {
      event.preventDefault();
      closeMenu(true);
    }
  });

  window.addEventListener("resize", positionMenu);
  window.addEventListener("scroll", positionMenu, true);
})();
