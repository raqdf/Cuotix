// Cuotix: graficos.
'use strict';

document.querySelectorAll('[data-trend]').forEach(chart => {
  const values = JSON.parse(chart.dataset.trend);
  const maximum = Math.max(...values.map(item => item.value), 1);
  values.forEach(item => {
    const column = document.createElement('div'); column.className = 'trend-item';
    const value = document.createElement('span');value.className = 'bar-value';value.textContent = window.Cuotix.money(item.value);
    const bar = document.createElement('div');bar.className = 'bar';bar.style.height = (item.value / maximum * 115) + 'px';bar.title = item.label + ': ' + window.Cuotix.money(item.value);
    const label = document.createElement('span');label.className = 'bar-label';label.textContent = item.label;
    column.append(value, bar, label);chart.append(column);
  });
});
