(() => {
  'use strict';

  const form = document.querySelector('#review-time-form');
  const input = form.querySelector('#measurement-time');
  const error = form.querySelector('#measurement-time-error');
  const unknown = form.querySelector('#keep-time-unknown');
  const confirmHook = document.querySelector('[data-time-confirm-hook]');
  const status = document.querySelector('[data-label="P05时间状态"]');
  const timeLabels = document.querySelectorAll('[data-record-two-time]');
  const timePattern = /^(?:[01][0-9]|2[0-3]):[0-5][0-9]$/;

  function clearError() {
    error.hidden = true;
    input.removeAttribute('aria-invalid');
  }

  function confirmTime(time) {
    clearError();
    input.value = time ?? '';
    for (const label of timeLabels) {
      label.textContent = `时间：${time ?? '未知'}`;
    }

    // Reuse the generated action that unlocks Save and returns to P05.
    $axure.internal(axure => axure.event.raiseSyntheticEvent(confirmHook.id, 'onClick'));
    status.querySelector('.text span').textContent = time === null ? '时间未知' : '已确认时间';
  }

  form.addEventListener('submit', event => {
    event.preventDefault();
    const time = input.value.trim();
    if (!timePattern.test(time)) {
      error.textContent = '请输入有效的 HH:MM 时间（00:00 至 23:59），或选择保留未知。';
      error.hidden = false;
      input.setAttribute('aria-invalid', 'true');
      input.focus();
      return;
    }
    confirmTime(time);
  });

  input.addEventListener('input', clearError);
  unknown.addEventListener('click', () => confirmTime(null));
})();
