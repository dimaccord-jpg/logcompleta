import asyncio
import json
import shutil
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

import websockets


TARGET_URL = "http://127.0.0.1:5000/cleide-bi-frete"
VIEWPORT = {"width": 1440, "height": 1400}
OUTPUT_PATH = Path("logs/cleide_click_diagnosis.json")
CHROME_CANDIDATES = [
    Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
]


def find_browser() -> Path:
    for candidate in CHROME_CANDIDATES:
        if candidate.exists():
            return candidate
    raise FileNotFoundError("Chrome/Edge not found in expected paths")


def get_json(url: str, timeout: float = 10.0):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class CDP:
    def __init__(self, websocket_url: str):
        self.websocket_url = websocket_url
        self.ws = None
        self.msg_id = 0
        self.pending = {}
        self.events = []

    async def connect(self):
        self.ws = await websockets.connect(self.websocket_url, max_size=None)
        asyncio.create_task(self._reader())

    async def _reader(self):
        while True:
            raw = await self.ws.recv()
            message = json.loads(raw)
            if "id" in message:
                future = self.pending.pop(message["id"], None)
                if future and not future.done():
                    future.set_result(message)
            else:
                self.events.append(message)

    async def send(self, method: str, params=None):
        self.msg_id += 1
        payload = {"id": self.msg_id, "method": method, "params": params or {}}
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.pending[self.msg_id] = future
        await self.ws.send(json.dumps(payload))
        response = await asyncio.wait_for(future, timeout=20)
        if "error" in response:
            raise RuntimeError(f"CDP error for {method}: {response['error']}")
        return response.get("result", {})

    def drain_events(self, method: str):
        drained = [evt for evt in self.events if evt.get("method") == method]
        self.events = [evt for evt in self.events if evt.get("method") != method]
        return drained


async def evaluate(cdp: CDP, expression: str):
    result = await cdp.send(
        "Runtime.evaluate",
        {
            "expression": expression,
            "returnByValue": True,
            "awaitPromise": True,
        },
    )
    return result.get("result", {}).get("value")


async def main():
    def log(message: str):
        print(message, flush=True)

    browser_path = find_browser()
    temp_root = Path(r"C:\tmp")
    temp_root.mkdir(parents=True, exist_ok=True)
    user_data_dir = tempfile.mkdtemp(prefix="cleide-click-diag-", dir=str(temp_root))
    port = 9222
    browser = subprocess.Popen(
        [
            str(browser_path),
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            "--headless=new",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-extensions",
            "--disable-renderer-backgrounding",
            f"--user-data-dir={user_data_dir}",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        log(f"Launching browser: {browser_path}")
        version = None
        for _ in range(50):
            try:
                version = get_json(f"http://127.0.0.1:{port}/json/version", timeout=1.0)
                break
            except Exception:
                time.sleep(0.2)
        if not version:
            stderr_output = ""
            if browser.stderr:
                try:
                    stderr_output = browser.stderr.read()
                except Exception:
                    stderr_output = ""
            raise RuntimeError(f"Could not connect to browser DevTools endpoint. stderr={stderr_output!r}")

        log("Connected to browser version endpoint")
        cdp = CDP(version["webSocketDebuggerUrl"])
        await cdp.connect()
        await cdp.send("Target.createTarget", {"url": "about:blank", "newWindow": False})
        targets = get_json(f"http://127.0.0.1:{port}/json/list", timeout=5.0)
        page_target = next(t for t in reversed(targets) if t.get("type") == "page" and t.get("url") == "about:blank")
        page = CDP(page_target["webSocketDebuggerUrl"])
        await page.connect()
        log("Connected to page target")

        await page.send("Page.enable")
        await page.send("Runtime.enable")
        await page.send("Log.enable")
        await page.send("Network.enable")
        await page.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        await page.send(
            "Page.setDownloadBehavior",
            {"behavior": "allow", "downloadPath": str((Path.cwd() / "logs").resolve())},
        )
        await page.send(
            "Emulation.setDeviceMetricsOverride",
            {
                "width": VIEWPORT["width"],
                "height": VIEWPORT["height"],
                "deviceScaleFactor": 1,
                "mobile": False,
            },
        )

        await page.send("Page.navigate", {"url": TARGET_URL})
        log(f"Navigating to {TARGET_URL}")

        for _ in range(100):
            state = await evaluate(page, "document.readyState")
            if state == "complete":
                break
            await asyncio.sleep(0.1)
        await asyncio.sleep(2.0)
        log("Page loaded; installing instrumentation")

        await evaluate(
            page,
            r"""
(() => {
  if (window.__cleideDiagInstalled) return true;
  window.__cleideDiagInstalled = true;
  window.__cleideDiag = { events: [], consoleErrors: [], jsErrors: [], requests: [] };

  const describe = (el) => {
    if (!el) return null;
    const rect = el.getBoundingClientRect();
    const cs = getComputedStyle(el);
    return {
      tag: el.tagName,
      id: el.id || "",
      classes: Array.from(el.classList || []),
      text: (el.innerText || el.textContent || "").trim().slice(0, 200),
      href: el.href || "",
      rect: {
        x: rect.x,
        y: rect.y,
        width: rect.width,
        height: rect.height,
        top: rect.top,
        right: rect.right,
        bottom: rect.bottom,
        left: rect.left,
      },
      zIndex: cs.zIndex,
      position: cs.position,
      pointerEvents: cs.pointerEvents,
      display: cs.display,
      opacity: cs.opacity,
      visibility: cs.visibility,
    };
  };

  const selectors = {
    iniciar_upload: "#cleideHeroUploadCta",
    ir_para_dashboard: "#cleideHeroDashboardCta",
    baixar_modelo: "#cleideTemplateDownloadBtn",
  };

  const handler = (phase) => (event) => {
    const anchor = event.target && event.target.closest ? event.target.closest("a") : null;
    const current = event.currentTarget;
    window.__cleideDiag.events.push({
      phase,
      type: event.type,
      defaultPrevented: event.defaultPrevented,
      cancelBubble: event.cancelBubble,
      target: describe(event.target),
      currentTarget: current === document ? { tag: "DOCUMENT" } : describe(current),
      anchor: describe(anchor),
      x: event.clientX,
      y: event.clientY,
      locationHref: location.href,
      locationHash: location.hash,
      time: Date.now(),
    });
  };

  ["pointerdown", "mousedown", "mouseup", "click"].forEach((type) => {
    document.addEventListener(type, handler("capture"), true);
    document.addEventListener(type, handler("bubble"), false);
  });

  window.addEventListener("error", (event) => {
    window.__cleideDiag.jsErrors.push({
      message: event.message,
      filename: event.filename,
      lineno: event.lineno,
      colno: event.colno,
    });
  });

  const origError = console.error;
  console.error = (...args) => {
    window.__cleideDiag.consoleErrors.push(args.map((item) => String(item)));
    return origError.apply(console, args);
  };

  window.__cleideCollectTarget = (key) => {
    const selector = selectors[key];
    const el = document.querySelector(selector);
    if (!el) {
      return { key, selector, found: false };
    }
    const rect = el.getBoundingClientRect();
    const cx = rect.left + rect.width / 2;
    const cy = rect.top + rect.height / 2;
    const topEl = document.elementFromPoint(cx, cy);
    const stack = document.elementsFromPoint(cx, cy).slice(0, 8);
    const anchorAtPoint = topEl && topEl.closest ? topEl.closest("a") : null;
    return {
      key,
      selector,
      found: true,
      expected: describe(el),
      center: { x: cx, y: cy },
      elementFromPoint: describe(topEl),
      elementFromPointAnchor: describe(anchorAtPoint),
      stack: stack.map(describe),
      hrefAttribute: el.getAttribute("href"),
      hrefResolved: el.href || "",
      hasInlineOnclick: el.hasAttribute("onclick"),
    };
  };

  window.__cleideReadEvents = () => {
    const events = window.__cleideDiag.events.slice();
    window.__cleideDiag.events.length = 0;
    return events;
  };

  window.__cleideSnapshot = () => JSON.parse(JSON.stringify(window.__cleideDiag));
  return true;
})();
            """,
        )

        async def inspect_target(key: str):
            log(f"Inspecting target: {key}")
            before = await evaluate(page, f"window.__cleideCollectTarget({json.dumps(key)})")
            if not before.get("found"):
                return {"before": before, "after": None, "events": [], "postState": None}

            center = before["center"]
            await evaluate(page, "window.__cleideReadEvents()")
            pre_state = await evaluate(
                page,
                "({ href: location.href, hash: location.hash, activeTag: document.activeElement && document.activeElement.tagName })",
            )

            for event_type in ("mouseMoved", "mousePressed", "mouseReleased"):
                params = {
                    "type": event_type,
                    "x": center["x"],
                    "y": center["y"],
                    "button": "left",
                    "clickCount": 1,
                }
                if event_type == "mousePressed":
                    params["buttons"] = 1
                await page.send("Input.dispatchMouseEvent", params)
                await asyncio.sleep(0.05)

            await asyncio.sleep(0.5)
            log(f"Collecting post-click state: {key}")
            after = await evaluate(page, f"window.__cleideCollectTarget({json.dumps(key)})")
            events = await evaluate(page, "window.__cleideReadEvents()")
            post_state = await evaluate(
                page,
                "({ href: location.href, hash: location.hash, activeTag: document.activeElement && document.activeElement.tagName })",
            )
            return {
                "before": before,
                "after": after,
                "events": events,
                "preState": pre_state,
                "postState": post_state,
            }

        targets = {}
        for key in ("iniciar_upload", "ir_para_dashboard", "baixar_modelo"):
            targets[key] = await inspect_target(key)

        log("Collecting console/runtime snapshot")
        log_entries = page.drain_events("Log.entryAdded")
        exceptions = page.drain_events("Runtime.exceptionThrown")
        snapshot = await evaluate(page, "window.__cleideSnapshot()")

        result = {
            "url": TARGET_URL,
            "viewport": VIEWPORT,
            "browser": str(browser_path),
            "targets": targets,
            "consoleLogEntries": log_entries,
            "runtimeExceptions": exceptions,
            "pageSnapshot": snapshot,
        }
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(result, indent=2, ensure_ascii=False))
    finally:
        browser.terminate()
        try:
            browser.wait(timeout=5)
        except subprocess.TimeoutExpired:
            browser.kill()
        shutil.rmtree(user_data_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
