/**
 * Глобальный «предохранитель»: если при отображении страницы случится ошибка
 * (как бывало с белым экраном), пользователь видит понятное сообщение и кнопку
 * «Перезагрузить», а не пустой белый экран.
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'

interface Props {
  children: ReactNode
}

interface State {
  error: Error | null
}

export class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null }

  static getDerivedStateFromError(error: Error): State {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // след для администратора в консоли браузера (F12 → Console)
    console.error('SchoolHub: ошибка отображения страницы', error, info.componentStack)
  }

  render() {
    if (!this.state.error) return this.props.children
    return (
      <div
        style={{
          minHeight: '100vh',
          display: 'flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: 24,
          fontFamily: 'system-ui, Segoe UI, sans-serif',
          background: 'var(--bg, #f6f7fb)',
          color: 'var(--ink-900, #1f2430)',
        }}
      >
        <div style={{ maxWidth: 560, background: 'var(--surface, #fff)', borderRadius: 12, padding: 28, boxShadow: '0 8px 30px rgba(31,36,48,.12)' }}>
          <div style={{ fontSize: 40, lineHeight: 1 }}>😕</div>
          <h1 style={{ margin: '12px 0 8px', fontSize: 22 }}>Страница не открылась</h1>
          <p style={{ margin: '0 0 18px', lineHeight: 1.5 }}>
            При отображении страницы произошла внутренняя ошибка. Данные не потеряны.
            Нажмите «Перезагрузить» — обычно этого достаточно. Если ошибка повторяется,
            сообщите администратору SchoolHub.
          </p>
          <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
            <button
              type="button"
              onClick={() => window.location.reload()}
              style={{
                padding: '10px 18px',
                borderRadius: 8,
                border: 'none',
                background: '#2f6fed',
                color: '#fff',
                fontSize: 15,
                cursor: 'pointer',
              }}
            >
              Перезагрузить
            </button>
            <a
              href="/"
              style={{
                padding: '10px 18px',
                borderRadius: 8,
                border: '1px solid var(--border-strong, #d4d8e3)',
                background: 'var(--surface, #fff)',
                color: 'var(--ink-900, #1f2430)',
                fontSize: 15,
                textDecoration: 'none',
              }}
            >
              На главную
            </a>
          </div>
        </div>
      </div>
    )
  }
}
