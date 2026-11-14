// Cuotix: login.
'use strict';

document.querySelectorAll('[data-toggle-password]').forEach(button => button.addEventListener('click', () => {
  const input = document.getElementById(button.dataset.togglePassword);
  input.type = input.type === 'password' ? 'text' : 'password';
  button.textContent = input.type === 'password' ? 'Ver' : 'Ocultar';
  button.setAttribute('aria-label', input.type === 'password' ? 'Mostrar contraseña' : 'Ocultar contraseña');
}));
