// Cuotix: admin.
'use strict';
window.Cuotix = Object.freeze({
  money: value => 'C$ ' + Number(value || 0).toLocaleString('en-US', {
    minimumFractionDigits: 2,
    maximumFractionDigits: 2
  })
});
document.querySelector('[aria-controls="sidebar"]')?.addEventListener('click', event => {
  const open = document.body.classList.toggle('menu-open');
  event.currentTarget.setAttribute('aria-expanded', String(open));
});
document.addEventListener('keydown', event => {if (event.key === 'Escape') {document.body.classList.remove('menu-open');document.querySelector('[aria-controls="sidebar"]')?.setAttribute('aria-expanded','false');}});
document.querySelectorAll('[data-print]').forEach(button => button.addEventListener('click', () => window.print()));
document.querySelectorAll('[data-refresh]').forEach(button => button.addEventListener('click', () => window.location.reload()));
document.querySelectorAll('form[method="post"]').forEach(form => form.addEventListener('submit', event => {
  const confirmation = event.submitter?.dataset.confirm || form.dataset.confirm;
  if (confirmation && !window.confirm(confirmation)) {event.preventDefault(); return;}
  if (form.dataset.submitted) {event.preventDefault(); return;}
  form.dataset.submitted = 'true';
  form.querySelectorAll('button:not([type="button"])').forEach(button => {button.disabled = true;button.textContent = 'Procesando…';});
}));
window.addEventListener('pageshow', event => {if(event.persisted) window.location.reload();});
const paletteNames = new Map([
  ['naranja', 'Naranja y amarillo'],
  ['azul', 'Azul corporativo'],
  ['verde', 'Verde y menta']
]);
const paletteChromeColors = new Map([
  ['naranja', '#3b2818'],
  ['azul', '#141d2e'],
  ['verde', '#17352f']
]);
const paletteButtons = document.querySelectorAll('[data-palette-choice]');
const paletteStatus = document.querySelector('[data-palette-status]');
const applyPalette = (name, persist = false) => {
  const selected = paletteNames.has(name) ? name : 'naranja';
  document.documentElement.dataset.palette = selected;
  document.querySelector('meta[name="theme-color"]')?.setAttribute('content', paletteChromeColors.get(selected));
  paletteButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.paletteChoice === selected)));
  if (persist) localStorage.setItem('cuotix-palette', selected);
  if (paletteStatus) paletteStatus.textContent = `Paleta activa: ${paletteNames.get(selected)}.`;
};
applyPalette(localStorage.getItem('cuotix-palette'));
paletteButtons.forEach(button => button.addEventListener('click', () => applyPalette(button.dataset.paletteChoice, true)));
