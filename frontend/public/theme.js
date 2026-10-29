/* Тема применяется ДО отрисовки интерфейса — без «вспышки» светлой темы.
   Явный выбор пользователя хранится в localStorage; пока выбора нет —
   тема операционной системы (prefers-color-scheme).

   Вынесено из inline-<script> в index.html намеренно: политика CSP nginx
   (frontend/security-headers.conf) работает с `script-src 'self'`, то есть
   БЕЗ 'unsafe-inline'. Inline-скрипт потребовал бы разрешить любой встроенный
   код и обессмыслил бы CSP как защиту от XSS (review/REVIEW.md §5.13, §P0-2). */
;(function () {
  try {
    var saved = localStorage.getItem('schoolhub-theme')
    var dark =
      saved === 'dark' ||
      ((saved !== 'light') &&
        window.matchMedia &&
        window.matchMedia('(prefers-color-scheme: dark)').matches)
    document.documentElement.setAttribute('data-theme', dark ? 'dark' : 'light')
  } catch (e) {
    document.documentElement.setAttribute('data-theme', 'light')
  }
})()
