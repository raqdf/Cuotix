// Cuotix: tablas.
'use strict';

document.querySelectorAll('[data-paginate]').forEach(table => {
  const rows = Array.from(table.tBodies[0].rows);
  const size = Number(table.dataset.paginate);
  if (rows.length <= size) return;
  let page = 0;
  const footer = document.createElement('div');
  footer.className = 'table-pagination';
  const description = document.createElement('span');
  description.setAttribute('aria-live', 'polite');
  const controls = document.createElement('div');
  const previous = document.createElement('button');
  const next = document.createElement('button');
  [previous, next].forEach(button => {button.type = 'button';button.className = 'btn btn-sm btn-light';});
  previous.textContent = '← Anterior'; next.textContent = 'Siguiente →';
  controls.append(previous, next);footer.append(description, controls);
  table.parentElement.after(footer);
  const render = () => {
    rows.forEach((row, index) => row.hidden = index < page * size || index >= (page + 1) * size);
    description.textContent = (page * size + 1) + '–' + Math.min((page + 1) * size, rows.length) + ' de ' + rows.length + ' registros';
    previous.disabled = page === 0; next.disabled = (page + 1) * size >= rows.length;
  };
  previous.addEventListener('click', () => {page--;render();});
  next.addEventListener('click', () => {page++;render();});
  render();
});
