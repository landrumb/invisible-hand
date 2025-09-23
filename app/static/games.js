(() => {
  function postJSON(url, payload) {
    return fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload),
      credentials: "include"
    }).then((response) => {
      if (!response.ok) {
        return response.json().catch(() => ({error: response.statusText})).then((data) => {
          throw new Error(data.error || "Request failed");
        });
      }
      return response.json();
    });
  }

  function initReactionGame(root) {
    const startButton = root.querySelector('[data-role="start"]');
    const reactButton = root.querySelector('[data-role="react"]');
    const status = root.querySelector('[data-role="status"]');
    const submitUrl = root.dataset.submitUrl;
    const token = root.dataset.token;
    let timer = null;
    let startedAt = 0;

    function reset() {
      if (timer) {
        clearTimeout(timer);
        timer = null;
      }
      startedAt = 0;
      startButton.disabled = false;
      reactButton.hidden = true;
      reactButton.disabled = true;
      status.textContent = "Waiting to start…";
      root.classList.remove("armed");
    }

    function readyUp() {
      startButton.disabled = true;
      reactButton.hidden = true;
      reactButton.disabled = true;
      status.textContent = "Waiting for the signal…";
      const delay = 1200 + Math.random() * 1800;
      timer = setTimeout(() => {
        root.classList.add("armed");
        reactButton.hidden = false;
        reactButton.disabled = false;
        status.textContent = "React now!";
        startedAt = performance.now();
      }, delay);
    }

    function submitReaction() {
      if (!startedAt) {
        status.textContent = "Too early! Restart the round.";
        reset();
        return;
      }
      const elapsed = (performance.now() - startedAt) / 1000;
      status.textContent = "Checking payout…";
      reactButton.disabled = true;
      postJSON(submitUrl, {token, duration: elapsed})
        .then((result) => {
          status.textContent = result.message || "Round resolved.";
          if (result.category) {
            root.dataset.outcome = result.category;
          }
        })
        .catch((error) => {
          status.textContent = error.message;
        })
        .finally(() => {
          setTimeout(reset, 1500);
        });
    }

    startButton.addEventListener("click", readyUp);
    reactButton.addEventListener("click", submitReaction);
  }

  function initNewcomb(root) {
    const field = root.querySelector('[data-role="selection-field"]');
    const boxes = root.querySelectorAll('[data-box]');

    function updateSelection() {
      const active = [];
      boxes.forEach((box) => {
        if (box.classList.contains("selected")) {
          active.push(box.dataset.box);
        }
      });
      field.value = active.join(",");
    }

    boxes.forEach((box) => {
      box.addEventListener("click", () => {
        box.classList.toggle("selected");
        updateSelection();
      });
    });
  }

  function initTaskGame(root) {
    const submitUrl = root.dataset.submitUrl;
    const token = root.dataset.token;
    const status = root.querySelector('[data-role="status"]');
    const type = root.dataset.taskType;
    const state = root.dataset.state ? JSON.parse(root.dataset.state) : {};

    function handleResult(result) {
      status.textContent = result.message || "Task complete.";
      if (result.category) {
        root.dataset.outcome = result.category;
      }
    }

    function handleError(error) {
      status.textContent = error.message || "Something went wrong.";
    }

    if (type === "swipe_card") {
      const card = root.querySelector('[data-role="card"]');
      let tracking = false;
      let startTime = 0;

      function onPointerDown(event) {
        event.preventDefault();
        tracking = true;
        startTime = performance.now();
        card.classList.add("active");
      }

      function onPointerUp(event) {
        if (!tracking) return;
        event.preventDefault();
        tracking = false;
        card.classList.remove("active");
        const elapsed = (performance.now() - startTime) / 1000;
        status.textContent = "Scanning…";
        postJSON(submitUrl, {token, duration: elapsed})
          .then(handleResult)
          .catch(handleError);
      }

      card.addEventListener("mousedown", onPointerDown);
      card.addEventListener("touchstart", onPointerDown, {passive: false});
      document.addEventListener("mouseup", onPointerUp);
      document.addEventListener("touchend", onPointerUp, {passive: false});
    } else if (type === "prime_shields") {
      const segments = Array.from(root.querySelectorAll(".shield-segment"));
      let startTime = null;
      let complete = false;
      function checkComplete() {
        if (complete || startTime === null) return;
        const ready = segments.every((segment) => segment.classList.contains("lit"));
        if (!ready) return;
        complete = true;
        const elapsed = (performance.now() - startTime) / 1000;
        status.textContent = "Routing power…";
        postJSON(submitUrl, {token, duration: elapsed})
          .then(handleResult)
          .catch(handleError);
      }

      segments.forEach((segment) => {
        segment.addEventListener("click", () => {
          if (startTime === null) {
            startTime = performance.now();
          }
          const pressed = segment.getAttribute("aria-pressed") === "true";
          if (pressed) {
            segment.setAttribute("aria-pressed", "false");
            segment.classList.remove("lit");
            complete = false;
          } else {
            segment.setAttribute("aria-pressed", "true");
            segment.classList.add("lit");
          }
          checkComplete();
        });
      });
    } else if (type === "align_engine") {
      const slider = root.querySelector('[data-role="slider"]');
      const target = root.querySelector('[data-role="target"]');
      const targetValue = state.target || 50;
      target.style.left = `${targetValue}%`;
      let complete = false;
      let startTime = null;

      slider.addEventListener("input", () => {
        if (complete) return;
        if (startTime === null) {
          startTime = performance.now();
        }
        const value = Number(slider.value);
        const delta = Math.abs(value - targetValue);
        if (delta <= (state.precision || 3)) {
          complete = true;
          status.textContent = "Engines aligned…";
          const elapsed = (performance.now() - startTime) / 1000;
          postJSON(submitUrl, {token, value, duration: elapsed})
            .then(handleResult)
            .catch(handleError);
        }
      });
    }
  }

  document.addEventListener("DOMContentLoaded", () => {
    const reaction = document.querySelector("[data-reaction-game]");
    if (reaction) {
      initReactionGame(reaction);
    }
    const newcomb = document.querySelector("[data-newcomb-game]");
    if (newcomb) {
      initNewcomb(newcomb);
    }
    document.querySelectorAll("[data-task-game]").forEach((root) => initTaskGame(root));
  });
})();
