(function () {
  function normalizeDirectory(raw) {
    if (!Array.isArray(raw)) {
      return [];
    }
    return raw
      .filter((entry) => entry && typeof entry.name === 'string' && typeof entry.handle === 'string')
      .map((entry) => ({
        name: entry.name,
        handle: entry.handle,
        nameLower: entry.name.toLowerCase(),
        handleLower: entry.handle.toLowerCase(),
      }))
      .sort((a, b) => a.nameLower.localeCompare(b.nameLower));
  }

  function escapeHtml(value) {
    return value
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function highlightMatches(text, query) {
    if (!query) {
      return escapeHtml(text);
    }
    const lowerText = text.toLowerCase();
    const lowerQuery = query.toLowerCase();
    let result = '';
    let index = 0;
    while (index < text.length) {
      const matchIndex = lowerText.indexOf(lowerQuery, index);
      if (matchIndex === -1) {
        result += escapeHtml(text.slice(index));
        break;
      }
      result += escapeHtml(text.slice(index, matchIndex));
      result += '<strong>' + escapeHtml(text.slice(matchIndex, matchIndex + query.length)) + '</strong>';
      index = matchIndex + query.length;
    }
    return result;
  }

  function buildOptionLabel(player, query) {
    const safeName = highlightMatches(player.name, query);
    const safeHandle = highlightMatches(player.handle, query);
    return safeName + ' (' + safeHandle + ')';
  }

  function setupSelector(container, directory) {
    if (container.dataset.playerSelectorReady === 'true') {
      return;
    }
    const input = container.querySelector('input[type="text"], input[type="search"]');
    if (!input) {
      return;
    }

    container.dataset.playerSelectorReady = 'true';
    container.classList.add('player-selector--enhanced');
    input.setAttribute('autocomplete', 'off');

    const dropdown = document.createElement('div');
    dropdown.className = 'player-selector__dropdown';
    const list = document.createElement('ul');
    list.className = 'player-selector__list';
    dropdown.appendChild(list);
    container.appendChild(dropdown);

    let results = [];
    let activeIndex = -1;
    let currentQuery = '';

    function closeDropdown() {
      dropdown.classList.remove('is-open');
      activeIndex = -1;
    }

    function openDropdown() {
      if (!results.length) {
        closeDropdown();
        return;
      }
      dropdown.classList.add('is-open');
    }

    function renderResults() {
      list.innerHTML = '';
      if (!results.length) {
        closeDropdown();
        return;
      }
      results.forEach((player, index) => {
        const item = document.createElement('li');
        item.className = 'player-selector__option';
        if (index === activeIndex) {
          item.classList.add('is-active');
        }
        item.innerHTML = buildOptionLabel(player, currentQuery);
        item.dataset.handle = player.handle;
        item.dataset.name = player.name;
        item.addEventListener('mousedown', (event) => {
          event.preventDefault();
          selectOption(index);
        });
        list.appendChild(item);
      });
      openDropdown();
    }

    function selectOption(index) {
      const choice = results[index];
      if (!choice) {
        return;
      }
      input.value = choice.handle;
      input.dataset.selectedHandle = choice.handle;
      input.dataset.selectedName = choice.name;
      input.dispatchEvent(new Event('change', { bubbles: true }));
      closeDropdown();
    }

    function updateResults() {
      currentQuery = input.value.trim();
      input.dataset.selectedHandle = '';
      input.dataset.selectedName = '';
      if (!directory.length) {
        results = [];
        renderResults();
        return;
      }
      if (!currentQuery) {
        results = directory.slice(0, 5);
      } else {
        const normalizedQuery = currentQuery.toLowerCase();
        results = directory
          .filter(
            (player) =>
              player.nameLower.includes(normalizedQuery) ||
              player.handleLower.includes(normalizedQuery)
          )
          .slice(0, 5);
      }
      activeIndex = results.length ? 0 : -1;
      renderResults();
    }

    input.addEventListener('input', () => {
      updateResults();
    });

    input.addEventListener('focus', () => {
      updateResults();
    });

    input.addEventListener('blur', () => {
      window.setTimeout(() => {
        closeDropdown();
      }, 120);
    });

    input.addEventListener('keydown', (event) => {
      if (!results.length) {
        return;
      }
      if (event.key === 'ArrowDown') {
        event.preventDefault();
        activeIndex = (activeIndex + 1) % results.length;
        renderResults();
      } else if (event.key === 'ArrowUp') {
        event.preventDefault();
        activeIndex = (activeIndex - 1 + results.length) % results.length;
        renderResults();
      } else if (event.key === 'Enter') {
        event.preventDefault();
        const index = activeIndex >= 0 ? activeIndex : 0;
        selectOption(index);
      } else if (event.key === 'Escape') {
        closeDropdown();
      }
    });
  }

  document.addEventListener('DOMContentLoaded', () => {
    const containers = Array.from(document.querySelectorAll('[data-player-selector]'));
    if (!containers.length) {
      return;
    }
    const directory = normalizeDirectory(window.PLAYER_DIRECTORY || []);
    containers.forEach((container) => setupSelector(container, directory));
  });
})();
