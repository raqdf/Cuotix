// Cuotix: caja.
'use strict';

document.querySelectorAll('[data-count-form]').forEach(form => {
  const update = () => {
    let cents = 0;
    form.querySelectorAll('[data-denomination]').forEach(input => {
      const value = Math.max(0, Math.floor(Number(input.value) || 0)) * Number(input.dataset.denomination) * 100;
      cents += value;
      input.closest('.count-row').querySelector('[data-line-total]').textContent = window.Cuotix.money(value / 100);
    });
    cents += Math.round(Math.max(0, Number(form.querySelector('[data-coins]').value) || 0) * 100);
    const difference = (cents - Math.round(Number(form.dataset.balance) * 100)) / 100;
    form.querySelector('[data-count-total]').textContent = window.Cuotix.money(cents / 100);
    const output = form.querySelector('[data-count-difference]');
    output.textContent = window.Cuotix.money(difference);output.classList.toggle('text-danger', difference !== 0);output.classList.toggle('text-success', difference === 0);
    form.querySelector('[name="motivo"]').required = difference !== 0;
  };
  form.addEventListener('input', update);update();
});
