/**
 * Светлая/тёмная тема.
 *
 * data-theme на <html> ставится скриптом в index.html ещё до загрузки React
 * (иначе была бы «вспышка» светлой темы). Явный выбор пользователя хранится
 * в localStorage «schoolhub-theme»; пока выбора нет — используется тема
 * операционной системы (prefers-color-scheme).
 */
export type ThemeMode = 'light' | 'dark'

const THEME_KEY = 'schoolhub-theme'

export function currentTheme(): ThemeMode {
  return document.documentElement.getAttribute('data-theme') === 'dark' ? 'dark' : 'light'
}

export function applyTheme(mode: ThemeMode): void {
  document.documentElement.setAttribute('data-theme', mode)
  try {
    localStorage.setItem(THEME_KEY, mode)
  } catch {
    // приватный режим/переполненный storage — тема останется до перезагрузки
  }
}

/** Переключить светлую/тёмную; возвращает новый режим (для состояния кнопки). */
export function toggleTheme(): ThemeMode {
  const next: ThemeMode = currentTheme() === 'dark' ? 'light' : 'dark'
  applyTheme(next)
  return next
}
