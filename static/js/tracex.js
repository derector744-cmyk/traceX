// traceX — общие интерактивные утилиты (toasts, confirm-модалка, индикатор пароля)

(function () {
  // ---------------------------------------------------------------------
  // Toasts
  // ---------------------------------------------------------------------
  function ensureToastRoot() {
    let root = document.getElementById('tx-toast-root');
    if (!root) {
      root = document.createElement('div');
      root.id = 'tx-toast-root';
      document.body.appendChild(root);
    }
    return root;
  }

  window.txToast = function (message, kind) {
    kind = kind || 'success';
    const root = ensureToastRoot();
    const el = document.createElement('div');
    el.className = 'tx-toast' + (kind === 'error' ? ' tx-toast-error' : '');
    el.innerHTML =
      '<div class="flex items-start gap-2">' +
      '<span>' + (kind === 'error' ? '⚠️' : '✅') + '</span>' +
      '<span>' + message + '</span>' +
      '</div>';
    root.appendChild(el);
    setTimeout(() => {
      el.classList.add('tx-toast-out');
      setTimeout(() => el.remove(), 250);
    }, 4200);
  };

  // ---------------------------------------------------------------------
  // Confirm-модалка (замена стандартного confirm())
  // Использование: <form data-confirm="Удалить расследование?"> ... </form>
  // ---------------------------------------------------------------------
  function showConfirmModal(message) {
    return new Promise((resolve) => {
      const backdrop = document.createElement('div');
      backdrop.className = 'tx-modal-backdrop';
      backdrop.innerHTML = `
        <div class="tx-modal-box bg-obsidian-panel border border-obsidian-border rounded-xl p-5 w-[90%] max-w-sm shadow-2xl">
          <div class="text-white font-semibold mb-2 flex items-center gap-2"><span>🗑️</span><span>Подтвердите действие</span></div>
          <p class="text-sm text-obsidian-muted mb-5">${message}</p>
          <div class="flex justify-end gap-2">
            <button data-role="cancel" class="tx-btn px-4 py-1.5 rounded-md text-sm border border-obsidian-border hover:border-obsidian-muted">Отмена</button>
            <button data-role="confirm" class="tx-btn px-4 py-1.5 rounded-md text-sm font-semibold bg-red-600 hover:bg-red-500 text-white">Удалить</button>
          </div>
        </div>`;
      document.body.appendChild(backdrop);

      function cleanup(result) {
        backdrop.remove();
        resolve(result);
      }
      backdrop.addEventListener('click', (e) => {
        if (e.target === backdrop) cleanup(false);
      });
      backdrop.querySelector('[data-role="cancel"]').addEventListener('click', () => cleanup(false));
      backdrop.querySelector('[data-role="confirm"]').addEventListener('click', () => cleanup(true));
    });
  }

  function initConfirmForms() {
    document.querySelectorAll('form[data-confirm]').forEach((form) => {
      if (form.dataset.txBound) return;
      form.dataset.txBound = '1';
      form.addEventListener('submit', function (e) {
        if (form.dataset.txConfirmed === '1') return;
        e.preventDefault();
        showConfirmModal(form.dataset.confirm).then((ok) => {
          if (ok) {
            form.dataset.txConfirmed = '1';
            form.submit();
          }
        });
      });
    });
  }

  // ---------------------------------------------------------------------
  // Индикатор надёжности пароля (страница регистрации)
  // ---------------------------------------------------------------------
  function initPasswordStrength() {
    const input = document.getElementById('f-password');
    const bar = document.getElementById('password-strength-bar');
    const label = document.getElementById('password-strength-label');
    if (!input || !bar) return;

    input.addEventListener('input', () => {
      const val = input.value;
      let score = 0;
      if (val.length >= 6) score++;
      if (val.length >= 10) score++;
      if (/[A-ZА-Я]/.test(val)) score++;
      if (/[0-9]/.test(val)) score++;
      if (/[^A-Za-zА-Яа-я0-9]/.test(val)) score++;

      const levels = [
        { width: '10%', color: '#ef4444', text: 'Очень слабый' },
        { width: '30%', color: '#f87171', text: 'Слабый' },
        { width: '55%', color: '#eab308', text: 'Средний' },
        { width: '75%', color: '#22c55e', text: 'Хороший' },
        { width: '100%', color: '#16a34a', text: 'Отличный' },
      ];
      const lvl = levels[Math.min(score, levels.length - 1)];
      bar.style.width = val ? lvl.width : '0%';
      bar.style.backgroundColor = lvl.color;
      if (label) label.textContent = val ? lvl.text : '';
    });
  }

  // ---------------------------------------------------------------------
  // Стаггер-анимация появления карточек (задаёт --tx-delay по индексу)
  // ---------------------------------------------------------------------
  function initStagger() {
    document.querySelectorAll('[data-tx-stagger] > *').forEach((el, i) => {
      el.style.setProperty('--tx-delay', i);
      el.classList.add('tx-fade-up');
    });
  }

  window.addEventListener('DOMContentLoaded', () => {
    initConfirmForms();
    initPasswordStrength();
    initStagger();

    const flashEl = document.getElementById('tx-flash-data');
    if (flashEl) {
      try {
        const data = JSON.parse(flashEl.textContent);
        if (data && data.message) {
          window.txToast(data.message, data.kind);
        }
      } catch (e) { /* noop */ }
    }
  });

  // Экспортируем на случай динамически подгружаемого контента (напр. после fetch)
  window.txInitConfirmForms = initConfirmForms;
  window.txInitStagger = initStagger;
})();
