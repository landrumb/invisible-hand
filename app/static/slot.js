(function () {
  const machines = document.querySelectorAll('.slot-machine');
  if (!machines.length) {
    return;
  }

  const STATUS_CLASSES = ['is-win', 'is-lose', 'is-error'];
  const FACE_SELECTOR = '[data-role="face"]';
  const LINE_LABELS = {
    row: ['Top row', 'Middle row', 'Bottom row'],
    column: ['Left column', 'Center column', 'Right column'],
    diagonal: ['Main diagonal', 'Counter diagonal'],
  };

  function parsePrizes(machine) {
    try {
      const raw = machine.dataset.prizes;
      if (!raw) {
        return [];
      }
      const parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        return parsed;
      }
      return [];
    } catch (err) {
      console.warn('Unable to parse slot prizes', err);
      return [];
    }
  }

  function updateFace(face, prize, { placeholder = false } = {}) {
    if (!face) {
      return;
    }
    face.innerHTML = '';
    let symbolText = '❓';
    let label = '';
    if (prize && typeof prize === 'object') {
      if (prize.symbol) {
        symbolText = prize.symbol;
      }
      if (prize.label) {
        label = prize.label;
      }
    }

    if (prize && prize.image) {
      const img = document.createElement('img');
      img.src = prize.image;
      img.alt = label || symbolText;
      img.loading = 'lazy';
      face.appendChild(img);
      face.dataset.image = prize.image;
    } else {
      const span = document.createElement('span');
      span.textContent = symbolText;
      face.appendChild(span);
      delete face.dataset.image;
    }
    face.dataset.symbol = symbolText;
    face.dataset.label = label;

    face.classList.toggle('is-placeholder', placeholder);
  }

  function setStatus(statusEl, message, variant) {
    if (!statusEl) {
      return;
    }
    STATUS_CLASSES.forEach((cls) => statusEl.classList.remove(cls));
    if (variant && STATUS_CLASSES.includes(variant)) {
      statusEl.classList.add(variant);
    }
    statusEl.textContent = message;
  }

  function clearHighlights(machine) {
    machine.classList.remove('has-line-wins');
    machine.querySelectorAll('.slot-face').forEach((face) => {
      face.classList.remove('is-line-win');
    });
  }

  function applyWins(machine, wins) {
    if (!Array.isArray(wins) || !wins.length) {
      return;
    }
    machine.classList.add('has-line-wins');
    wins.forEach((win) => {
      const coords = Array.isArray(win.coordinates) ? win.coordinates : [];
      coords.forEach((pair) => {
        let columnIndex;
        let rowIndex;
        if (Array.isArray(pair)) {
          columnIndex = Number(pair[0]);
          rowIndex = Number(pair[1]);
        } else if (pair && typeof pair === 'object') {
          columnIndex = Number(pair.column);
          rowIndex = Number(pair.row);
        } else {
          columnIndex = Number(pair);
          rowIndex = Number(pair);
        }
        if (!Number.isFinite(columnIndex) || !Number.isFinite(rowIndex)) {
          return;
        }
        const selector = `${FACE_SELECTOR}[data-column="${columnIndex}"][data-row="${rowIndex}"]`;
        const face = machine.querySelector(selector);
        if (face) {
          face.classList.add('is-line-win');
        }
      });
    });
  }

  function describeLine(win) {
    if (!win) {
      return '';
    }
    const labels = LINE_LABELS[win.line_type] || [];
    let label = labels[win.index] || (win.line_type ? win.line_type : 'line');
    if (label) {
      label = label.charAt(0).toUpperCase() + label.slice(1);
    }
    const prize = win.prize && typeof win.prize === 'object' ? win.prize : {};
    const prizeLabelRaw =
      typeof prize.label === 'string' && prize.label
        ? prize.label
        : typeof prize.symbol === 'string'
          ? prize.symbol
          : '';
    const prizeLabel = prizeLabelRaw;
    const multiplierSource = win.multiplier != null ? win.multiplier : prize.multiplier;
    const multiplier = Number(multiplierSource);
    const payout = Number(win.payout);
    const pieces = [];
    if (label) {
      pieces.push(label);
    }
    if (prizeLabel) {
      pieces.push(prizeLabel);
    }
    if (Number.isFinite(multiplier)) {
      pieces.push(`(${multiplier.toFixed(2)}×)`);
    }
    if (Number.isFinite(payout)) {
      pieces.push(`${payout.toFixed(2)} credits`);
    }
    return pieces.join(' ');
  }

  function describeGrid(reels) {
    if (!Array.isArray(reels) || !reels.length) {
      return '';
    }
    const rows = [];
    for (let row = 0; row < 3; row += 1) {
      const rowSymbols = [];
      for (let col = 0; col < reels.length; col += 1) {
        const column = Array.isArray(reels[col]) ? reels[col] : [];
        rowSymbols.push(column[row] || '❓');
      }
      rows.push(rowSymbols.join(' '));
    }
    return rows.join(' / ');
  }

  machines.forEach((machine) => {
    const prizes = parsePrizes(machine);
    const reels = Array.from(machine.querySelectorAll('.slot-reel'));
    const statusEl = machine.querySelector('.slot-status');
    const form = machine.querySelector('[data-role="slot-form"]');
    const lever = machine.querySelector('.slot-lever');
    const defaultPrize = prizes[0] || null;
    let busy = false;

    const facesByColumn = reels.map((reel, columnIndex) => {
      const faces = Array.from(reel.querySelectorAll(FACE_SELECTOR));
      faces.forEach((face, rowIndex) => {
        face.dataset.column = String(columnIndex);
        face.dataset.row = String(rowIndex);
        const seedIndex = prizes.length ? (columnIndex + rowIndex) % prizes.length : 0;
        const seedPrize = prizes[seedIndex] || defaultPrize;
        updateFace(face, seedPrize || null);
      });
      return faces;
    });

    machine.addEventListener('mouseenter', () => {
      machine.classList.add('lever-armed');
    });

    machine.addEventListener('mouseleave', () => {
      machine.classList.remove('lever-armed');
    });

    if (!form) {
      return;
    }

    function triggerSpin() {
      if (busy) {
        return;
      }
      if (typeof form.requestSubmit === 'function') {
        form.requestSubmit();
      } else {
        const submitEvent = new Event('submit', { bubbles: true, cancelable: true });
        if (form.dispatchEvent(submitEvent) && !submitEvent.defaultPrevented) {
          form.submit();
        }
      }
    }

    if (lever) {
      lever.addEventListener('click', (event) => {
        event.preventDefault();
        triggerSpin();
      });
      lever.addEventListener('keydown', (event) => {
        if (event.key === 'Enter' || event.key === ' ') {
          event.preventDefault();
          triggerSpin();
        }
      });
    }

    form.addEventListener('submit', async (event) => {
      event.preventDefault();
      if (busy) {
        return;
      }

      const formData = new FormData(form);
      const slotId = formData.get('slot_id');
      const wagerRaw = formData.get('wager');
      const wager = typeof wagerRaw === 'string' ? parseFloat(wagerRaw) : Number(wagerRaw);

      if (!slotId) {
        setStatus(statusEl, 'Select a slot machine before spinning.', 'is-error');
        return;
      }
      if (!Number.isFinite(wager) || wager <= 0) {
        setStatus(statusEl, 'Enter a wager greater than zero.', 'is-error');
        return;
      }

      busy = true;
      clearHighlights(machine);
      setStatus(statusEl, 'Spinning...', null);
      machine.classList.add('is-spinning');
      machine.classList.add('lever-pulling');
      if (lever) {
        lever.classList.add('is-active');
      }

      reels.forEach((reel, columnIndex) => {
        reel.classList.add('spinning');
        const faces = facesByColumn[columnIndex] || [];
        faces.forEach((face, rowIndex) => {
          const shimmerIndex = prizes.length
            ? (columnIndex + rowIndex + 1) % prizes.length
            : 0;
          const shimmerPrize = prizes[shimmerIndex] || defaultPrize;
          updateFace(face, shimmerPrize || null, { placeholder: true });
        });
      });

      window.setTimeout(() => {
        machine.classList.remove('lever-pulling');
        if (lever) {
          lever.classList.remove('is-active');
        }
      }, 700);

      let response;
      try {
        response = await fetch(form.action, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            Accept: 'application/json',
          },
          body: JSON.stringify({
            slot_id: slotId,
            wager,
          }),
        });
      } catch (error) {
        console.error('Slot spin failed', error);
        finishWithError('Connection error. Please try again.');
        return;
      }

      if (!response.ok) {
        let errorMessage = 'Spin failed. Please try again.';
        try {
          const errorPayload = await response.json();
          if (errorPayload && errorPayload.error) {
            errorMessage = errorPayload.error;
          }
        } catch (parseErr) {
          // ignore JSON parse errors
        }
        finishWithError(errorMessage);
        return;
      }

      let payload;
      try {
        payload = await response.json();
      } catch (err) {
        console.error('Invalid slot response', err);
        finishWithError('Unexpected response from the casino.');
        return;
      }

      const reelResults = Array.isArray(payload.reels)
        ? payload.reels.map((column) => (Array.isArray(column) ? column.slice(0, 3) : []))
        : [];
      const resultPrize = payload.prize || null;
      const playerDelta = Number(payload.player_delta || 0);
      const wins = Array.isArray(payload.wins) ? payload.wins : [];
      const stopDelay = 450;

      reels.forEach((reel, columnIndex) => {
        const faces = facesByColumn[columnIndex] || [];
        const columnSymbols = Array.isArray(reelResults[columnIndex])
          ? reelResults[columnIndex]
          : [];
        window.setTimeout(() => {
          reel.classList.remove('spinning');
          faces.forEach((face, rowIndex) => {
            const symbol = columnSymbols[rowIndex];
            const matchedPrize = prizes.find((prize) => prize.symbol === symbol) ||
              (resultPrize && resultPrize.symbol === symbol ? resultPrize : null);
            updateFace(face, matchedPrize || { symbol });
          });
          if (columnIndex === reels.length - 1) {
            finalize();
          }
        }, stopDelay * (columnIndex + 1));
      });

      function finalize() {
        machine.classList.remove('is-spinning');
        busy = false;
        applyWins(machine, wins);
        const gridSummary = describeGrid(reelResults);
        const winDescriptions = wins.map(describeLine).filter(Boolean);
        let variant = null;
        if (playerDelta > 0) {
          variant = 'is-win';
        } else if (playerDelta < 0) {
          variant = 'is-lose';
        }
        const netText = Number.isFinite(playerDelta)
          ? playerDelta > 0
            ? `Won ${playerDelta.toFixed(2)} credits.`
            : playerDelta === 0
              ? 'Broke even.'
              : `Lost ${Math.abs(playerDelta).toFixed(2)} credits.`
          : '';
        const messageParts = [];
        if (gridSummary) {
          messageParts.push(gridSummary);
        }
        if (winDescriptions.length) {
          messageParts.push(`Lines: ${winDescriptions.join(' • ')}`);
        } else {
          messageParts.push('No line wins.');
        }
        if (netText) {
          messageParts.push(netText);
        }
        setStatus(statusEl, messageParts.join(' — '), variant);
      }

      function finishWithError(message) {
        machine.classList.remove('is-spinning');
        machine.classList.remove('lever-pulling');
        if (lever) {
          lever.classList.remove('is-active');
        }
        reels.forEach((reel) => reel.classList.remove('spinning'));
        clearHighlights(machine);
        busy = false;
        setStatus(statusEl, message, 'is-error');
      }
    });
  });
})();
