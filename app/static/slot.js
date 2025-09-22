(function () {
  const machines = document.querySelectorAll('.slot-machine');
  if (!machines.length) {
    return;
  }

  const STATUS_CLASSES = ['is-win', 'is-lose', 'is-error'];

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

  machines.forEach((machine) => {
    const prizes = parsePrizes(machine);
    const reels = Array.from(machine.querySelectorAll('.slot-reel'));
    const statusEl = machine.querySelector('.slot-status');
    const form = machine.querySelector('[data-role="slot-form"]');
    const lever = machine.querySelector('.slot-lever');
    const defaultPrize = prizes[0] || null;
    let busy = false;

    reels.forEach((reel, index) => {
      const face = reel.querySelector('[data-role="face"]');
      const seed = prizes[index % prizes.length] || defaultPrize;
      updateFace(face, seed || null);
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
      setStatus(statusEl, 'Spinning...', null);
      machine.classList.add('is-spinning');
      machine.classList.add('lever-pulling');
      if (lever) {
        lever.classList.add('is-active');
      }

      reels.forEach((reel, index) => {
        const face = reel.querySelector('[data-role="face"]');
        reel.classList.add('spinning');
        const shimmerPrize = prizes[(index + 1) % prizes.length] || defaultPrize;
        updateFace(face, shimmerPrize || null, { placeholder: true });
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

      const reelResults = Array.isArray(payload.reels) ? payload.reels : [];
      const resultPrize = payload.prize || null;
      const outcome = payload.outcome || 'lose';
      const playerDelta = Number(payload.player_delta || 0);
      const stopDelay = 450;

      reels.forEach((reel, index) => {
        const face = reel.querySelector('[data-role="face"]');
        const symbol = reelResults[index];
        const matchedPrize = prizes.find((prize) => prize.symbol === symbol) ||
          (resultPrize && resultPrize.symbol === symbol ? resultPrize : null);
        window.setTimeout(() => {
          reel.classList.remove('spinning');
          updateFace(face, matchedPrize || { symbol });
          if (index === reels.length - 1) {
            finalize();
          }
        }, stopDelay * (index + 1));
      });

      function finalize() {
        machine.classList.remove('is-spinning');
        busy = false;
        const joined = reelResults.join(' ');
        if (outcome === 'win') {
          const multiplier = resultPrize && resultPrize.multiplier
            ? Number(resultPrize.multiplier)
            : wager > 0
              ? Number((playerDelta / wager).toFixed(2))
              : 0;
          const payoutText = Number.isFinite(playerDelta)
            ? `Won ${playerDelta.toFixed(2)} credits.`
            : 'Winner!';
          const legendText = multiplier ? `(${multiplier.toFixed(2)}× wager)` : '';
          setStatus(
            statusEl,
            `Jackpot! ${joined} ${payoutText} ${legendText}`.trim(),
            'is-win',
          );
        } else {
          const lossText = Number.isFinite(playerDelta)
            ? `Lost ${Math.abs(playerDelta).toFixed(2)} credits.`
            : 'No payout this time.';
          setStatus(statusEl, `${joined || 'No match'} — ${lossText}`, 'is-lose');
        }
      }

      function finishWithError(message) {
        machine.classList.remove('is-spinning');
        machine.classList.remove('lever-pulling');
        if (lever) {
          lever.classList.remove('is-active');
        }
        reels.forEach((reel) => reel.classList.remove('spinning'));
        busy = false;
        setStatus(statusEl, message, 'is-error');
      }
    });
  });
})();
