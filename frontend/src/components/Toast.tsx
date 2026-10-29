/** Тосты: контекст + хук useToast(). Без внешних библиотек. */
import { createContext, useCallback, useContext, useMemo, useState, type ReactNode } from 'react'

type ToastKind = 'info' | 'success' | 'error'
interface Toast {
  id: number
  kind: ToastKind
  text: string
}

interface ToastApi {
  show: (text: string, kind?: ToastKind) => void
  success: (text: string) => void
  error: (text: unknown) => void
}

const ToastContext = createContext<ToastApi | null>(null)

let counter = 0

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toasts, setToasts] = useState<Toast[]>([])

  const remove = useCallback((id: number) => {
    setToasts((prev) => prev.filter((t) => t.id !== id))
  }, [])

  const show = useCallback(
    (text: string, kind: ToastKind = 'info') => {
      const id = ++counter
      setToasts((prev) => [...prev, { id, kind, text }])
      window.setTimeout(() => remove(id), kind === 'error' ? 7000 : 4000)
    },
    [remove],
  )

  const api = useMemo<ToastApi>(
    () => ({
      show,
      success: (text: string) => show(text, 'success'),
      error: (error: unknown) => {
        const message =
          error instanceof Error && error.message ? error.message : 'Не удалось выполнить операцию'
        show(message, 'error')
      },
    }),
    [show],
  )

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toasts" role="status" aria-live="polite">
        {toasts.map((t) => (
          <div key={t.id} className={`toast toast--${t.kind}`} onClick={() => remove(t.id)}>
            <span>{t.kind === 'error' ? '⛔' : t.kind === 'success' ? '✅' : 'ℹ️'}</span>
            <span>{t.text}</span>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast(): ToastApi {
  const ctx = useContext(ToastContext)
  if (!ctx) throw new Error('useToast должен использоваться внутри <ToastProvider>')
  return ctx
}
