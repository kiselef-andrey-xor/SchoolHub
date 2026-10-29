/** Модальное окно: focus-trap (упрощённый), закрытие по Esc и по клику на подложку. */
import { useEffect, useRef, type ReactNode } from 'react'

interface ModalProps {
  open: boolean
  title: ReactNode
  onClose: () => void
  children: ReactNode
  footer?: ReactNode
  wide?: boolean
  /** произвольная максимальная ширина, напр. '78vw' (просмотр документов); важнее wide */
  width?: string
}

export function Modal({ open, title, onClose, children, footer, wide = false, width }: ModalProps) {
  const ref = useRef<HTMLDivElement>(null)
  // onClose держим в ref: эффект автофокуса обязан срабатывать ТОЛЬКО при открытии,
  // а не на каждом рендере родителя (иначе фокус уходит на первое поле и «съедает»
  // ввод в остальных — баг «слетает набор в ФИО»).
  const onCloseRef = useRef(onClose)
  onCloseRef.current = onClose

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onCloseRef.current()
    }
    document.addEventListener('keydown', onKey)
    document.body.style.overflow = 'hidden'
    // автофокус на первый интерактивный элемент — однократно при открытии
    const focusable = ref.current?.querySelector<HTMLElement>(
      'input,select,textarea,button:not([data-autofocus-skip])',
    )
    focusable?.focus()
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = ''
    }
  }, [open])

  if (!open) return null

  return (
    <div
      className="modal-backdrop"
      onMouseDown={(e) => {
        if (e.target === e.currentTarget) onCloseRef.current()
      }}
      role="presentation"
    >
      <div
        className={wide || width ? 'modal modal--wide' : 'modal'}
        style={width ? { maxWidth: width, width: '100%' } : undefined}
        role="dialog"
        aria-modal="true"
        ref={ref}
      >
        <header className="modal__head">
          <h3>{title}</h3>
          <button className="btn btn--ghost btn--icon" onClick={() => onCloseRef.current()} aria-label="Закрыть" data-autofocus-skip>
            ✕
          </button>
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__foot">{footer}</footer>}
      </div>
    </div>
  )
}
