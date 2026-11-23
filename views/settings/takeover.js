/* Viewport streaming and user input through the authenticated native bridge. */
export function setupTakeover(bridge, toast, refresh) {
  const $ = (selector) => document.querySelector(selector);
  const canvas = $("#takeover-canvas");
  let current = null;
  let displayedPageId = "";
  let pendingFrame = false;
  let closing = false;
  let queue = Promise.resolve();
  let path = null;
  let pointerId = null;
  let wheelTimer = null;
  let wheel = 0;

  function controls() {
    $("#takeover-controls").hidden = !current;
    $("#takeover-resume").hidden = !current?.can_resume;
  }

  async function poll() {
    if (!current || pendingFrame || closing || path || document.hidden
        || !$("#tab-takeover").classList.contains("active")) return;
    const id = current.id;
    pendingFrame = true;
    try {
      const frame = await bridge.apiGet(`browser/takeover/frame/${id}`);
      if (current?.id !== id || closing) return;
      current = frame;
      const image = new Image();
      image.onload = () => {
        if (current?.id !== id || closing || path) return;
        canvas.width = frame.width;
        canvas.height = frame.height;
        canvas.getContext("2d").drawImage(image, 0, 0, canvas.width, canvas.height);
        displayedPageId = frame.page_id;
        zoom();
      };
      image.src = frame.image;
      $("#takeover-url").textContent = frame.url;
      const tabs = $("#takeover-tabs");
      tabs.replaceChildren();
      for (const tab of frame.tabs || []) {
        const option = document.createElement("option");
        option.value = tab.index;
        option.textContent = `${tab.index + 1}. ${tab.url || "加载中"}`;
        option.selected = tab.active;
        tabs.append(option);
      }
      controls();
    } catch (error) {
      if (current?.id === id) $("#takeover-status").textContent = `画面读取失败：${error.message}。可以刷新重试。`;
    } finally {
      pendingFrame = false;
    }
  }

  function zoom() {
    const scale = $("#takeover-zoom").value;
    canvas.style.width = scale === "fit" ? "100%" : `${canvas.width * Number(scale)}px`;
  }

  async function open(record = {}) {
    if (closing) return;
    $("#takeover-open").disabled = true;
    $("#takeover-status").textContent = "正在打开浏览器…";
    try {
      current = await bridge.apiPost("browser/takeover/start", {
        id: record.id || "", account_name: record.account_name || "", job_id: record.job_id || "",
      });
      displayedPageId = "";
      $("#takeover-status").textContent = current.original
        ? "已接管原任务的浏览器现场。点击、拖动、滚动或输入即可操作。"
        : "已用同一账号打开页面。原浏览器已关闭时，原验证码和编辑状态不会恢复；请在这里核对或重新操作。";
      controls();
      $("[data-tab='takeover']").click();
      await poll();
      await refresh();
    } catch (error) {
      $("#takeover-status").textContent = error.message;
      toast(error.message, true);
    } finally {
      $("#takeover-open").disabled = false;
    }
  }

  function send(payload) {
    const id = current?.id;
    if (!id || closing || !displayedPageId) return Promise.resolve();
    const pageId = displayedPageId;
    queue = queue.then(async () => {
      if (current?.id !== id) return;
      await bridge.apiPost(`browser/takeover/input/${id}`, {...payload, page_id: pageId});
    }).catch((error) => toast(`浏览器操作失败：${error.message}`, true));
    return queue.then(poll);
  }

  function point(event) {
    const box = canvas.getBoundingClientRect();
    return {x: Math.max(0, Math.min(canvas.width - 1, (event.clientX - box.left) * canvas.width / box.width)),
      y: Math.max(0, Math.min(canvas.height - 1, (event.clientY - box.top) * canvas.height / box.height))};
  }

  canvas.addEventListener("pointerdown", (event) => {
    if (!current || closing || !canvas.width || path || $("#takeover-mode").value === "pan") return;
    event.preventDefault();
    canvas.focus({preventScroll: true});
    pointerId = event.pointerId;
    canvas.setPointerCapture(pointerId);
    path = [point(event)];
  });
  canvas.addEventListener("pointermove", (event) => {
    if (!path || event.pointerId !== pointerId) return;
    const next = point(event);
    const last = path[path.length - 1];
    if (Math.hypot(next.x - last.x, next.y - last.y) < 2) return;
    if (path.length >= 98) path.splice(1, 1);
    path.push(next);
  });
  canvas.addEventListener("pointerup", (event) => {
    if (!path || event.pointerId !== pointerId) return;
    const points = [...path, point(event)];
    path = null;
    canvas.releasePointerCapture(pointerId);
    pointerId = null;
    const first = points[0], last = points[points.length - 1];
    const moved = points.some(p => Math.hypot(p.x - first.x, p.y - first.y) > 5);
    send(moved ? {type: "drag", points} : {type: "click", ...last,
      button: event.button === 2 ? "right" : "left"});
  });
  canvas.addEventListener("pointercancel", () => { path = null; pointerId = null; });
  canvas.addEventListener("contextmenu", (event) => event.preventDefault());
  canvas.addEventListener("wheel", (event) => {
    if (!current || $("#takeover-mode").value === "pan") return;
    event.preventDefault();
    wheel = Math.max(-3000, Math.min(3000, wheel + event.deltaY));
    if (!wheelTimer) wheelTimer = setTimeout(() => {
      const dy = wheel; wheel = 0; wheelTimer = null;
      send({type: "wheel", dy});
    }, 150);
  }, {passive: false});
  canvas.addEventListener("keydown", (event) => {
    if (!current || event.isComposing) return;
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") return;
    let key = event.key;
    if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "a") key = "Control+A";
    else if (event.ctrlKey || event.metaKey || event.altKey) return;
    else if (event.shiftKey && ["Enter", "Tab"].includes(key)) key = `Shift+${key}`;
    if (["Enter", "Tab", "Escape", "Backspace", "Delete", "ArrowLeft", "ArrowRight", "ArrowUp",
      "ArrowDown", "Home", "End", "PageUp", "PageDown", "Control+A", "Shift+Enter", "Shift+Tab"].includes(key)) {
      event.preventDefault(); send({type: "key", key});
    } else if (key.length === 1) {
      event.preventDefault(); send({type: "text", text: key});
    }
  });
  canvas.addEventListener("paste", (event) => {
    const text = event.clipboardData?.getData("text/plain");
    if (current && text) { event.preventDefault(); send({type: "text", text: text.slice(0, 8192)}); }
  });

  async function close(save, processed = false) {
    if (!current || closing) return;
    closing = true;
    const id = current.id;
    try {
      await queue;
      const result = await bridge.apiPost(`browser/takeover/close/${id}`, {save, processed});
      current = null;
      displayedPageId = "";
      canvas.getContext("2d").clearRect(0, 0, canvas.width, canvas.height);
      $("#takeover-status").textContent = result.message;
      controls();
      toast(result.message);
      await refresh();
    } catch (error) {
      toast(error.message, true);
    } finally {
      closing = false;
    }
  }

  $("#takeover-open").addEventListener("click", () => open());
  $("#takeover-type").addEventListener("click", async () => {
    const text = $("#takeover-text").value;
    if (!text) return;
    await send({type: "text", text});
    canvas.focus({preventScroll: true});
  });
  $("#takeover-save").addEventListener("click", () => close(true));
  $("#takeover-discard").addEventListener("click", () => close(false));
  $("#takeover-resume").addEventListener("click", () => close(true, true));
  $("#takeover-zoom").addEventListener("change", zoom);
  $("#takeover-mode").addEventListener("change", (event) => {
    canvas.style.touchAction = event.target.value === "pan" ? "auto" : "none";
    canvas.style.cursor = event.target.value === "pan" ? "grab" : "crosshair";
  });
  $("#takeover-tabs").addEventListener("change", (event) => send({type: "tab", index: Number(event.target.value)}));
  document.querySelectorAll("[data-browser-action]").forEach(button => button.addEventListener("click", () => send({type: button.dataset.browserAction})));
  document.querySelectorAll("[data-browser-scroll]").forEach(button => button.addEventListener("click", () => send({type: "wheel", dy: Number(button.dataset.browserScroll)})));
  document.querySelectorAll("[data-browser-key]").forEach(button => button.addEventListener("click", () => send({type: "key", key: button.dataset.browserKey})));
  setInterval(poll, 1000);

  function render(sessions, waits) {
    const box = $("#takeover-sessions");
    box.replaceChildren();
    const records = [...sessions, ...waits.filter(wait => wait.platform === "tieba"
      && !sessions.some(session => session.account_name === wait.account_name && session.job_id === wait.job_id))];
    for (const record of records) {
      const row = document.createElement("div"); row.className = "takeover-session";
      const text = document.createElement("span");
      text.textContent = `贴吧 / ${record.account_name} · ${record.id ? "现场已保留" : "原现场已关闭，可重开页面"}`;
      const button = document.createElement("button"); button.className = "btn";
      button.textContent = record.id ? "接管这个现场" : "打开原帖核对";
      button.addEventListener("click", () => open(record));
      row.append(text, button); box.append(row);
    }
  }
  return {open, render, poll};
}
