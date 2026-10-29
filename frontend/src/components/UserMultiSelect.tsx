/** Мультивыбор сотрудников: чипы выбранных + ВСПЛЫВАЮЩЕЕ окно (портал поверх модалок)
 *  с поиском, списком с чекбоксами и массовыми действиями.
 *  Используется в форме задачи (исполнители), приказе (круг ознакомления) и редакторе плана. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { Avatar } from '@/components/ui'
import { UserAvatar } from '@/components/UserAvatar'
import { shortName } from '@/lib/format'
import type { User } from '@/types'

interface Props {
  options: User[]
  value: string[]
  onChange: (ids: string[]) => void
  placeholder?: string
  disabled?: boolean
}

export function UserMultiSelect({ options, value, onChange, placeholder = 'Никто не выбран', disabled }: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)

  // Esc закрывает ТОЛЬКО всплывающее окно (capture + stopPropagation — родительская модалка остаётся)
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        e.stopPropagation()
        setOpen(false)
      }
    }
    window.addEventListener('keydown', onKey, true)
    const t = window.setTimeout(() => searchRef.current?.focus(), 0)
    return () => {
      window.removeEventListener('keydown', onKey, true)
      window.clearTimeout(t)
    }
  }, [open])

  const byId = useMemo(() => new Map(options.map((u) => [u.id, u])), [options])
  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return options
    return options.filter((u) =>
      `${u.full_name} ${(u.positions || []).map((p) => p.title).join(' ')} ${u.email ?? ''} ${u.login ?? ''}`.toLowerCase().includes(q),
    )
  }, [options, query])

  const toggle = (id: string) =>
    onChange(value.includes(id) ? value.filter((x) => x !== id) : [...value, id])

  return (
    <div>
      {value.length > 0 ? (
        <div className="chips">
          {value.map((id) => {
            const u = byId.get(id)
            return (
              <span className="uchip" key={id}>
                {u ? <UserAvatar user={u} size="sm" /> : <Avatar name="?" size="sm" />}
                <span>{u ? shortName(u.full_name) : '…'}</span>
                {!disabled && (
                  <button type="button" className="uchip__x" title="Убрать" onClick={() => toggle(id)}>
                    ✕
                  </button>
                )}
              </span>
            )
          })}
        </div>
      ) : (
        <div className="tiny muted">{placeholder}</div>
      )}

      {!disabled && (
        <button type="button" className="btn btn--sm" style={{ marginTop: 7 }} onClick={() => setOpen(true)}>
          {value.length ? `✎ Изменить список (${value.length})` : '＋ Выбрать сотрудников'}
        </button>
      )}

      {open &&
        createPortal(
          <div
            className="ums-overlay"
            role="presentation"
            onMouseDown={(e) => {
              if (e.target === e.currentTarget) setOpen(false)
            }}
          >
            <div className="ums-panel" role="dialog" aria-modal="true">
              <header className="row" style={{ gap: 8 }}>
                <strong>Выбор сотрудников</strong>
                <span className="grow" />
                <span className="tiny muted">выбрано: {value.length}</span>
                <button type="button" className="btn btn--ghost btn--icon" onClick={() => setOpen(false)} aria-label="Закрыть">
                  ✕
                </button>
              </header>
              <input
                ref={searchRef}
                className="input"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder="Поиск: ФИО, должность, e-mail, логин…"
              />
              <div className="row" style={{ gap: 6 }}>
                <button
                  type="button"
                  className="btn btn--sm"
                  onClick={() => onChange(Array.from(new Set([...value, ...filtered.map((u) => u.id)])))}
                >
                  Выбрать всех найденных
                </button>
                <button type="button" className="btn btn--sm" onClick={() => onChange([])}>
                  Очистить
                </button>
              </div>
              <div className="ums-list">
                {filtered.length === 0 && <div className="small muted" style={{ padding: 10 }}>Никого не найдено</div>}
                {filtered.map((u) => (
                  <div className="picker__row" key={u.id} onClick={() => toggle(u.id)}>
                    <span className="picker__check">{value.includes(u.id) ? '☑' : '☐'}</span>
                    <UserAvatar user={u} size="sm" />
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: 'block', fontWeight: 600 }}>{u.full_name}</span>
                      <span className="tiny muted">
                        {(u.positions || []).map((p) => p.title).join(', ') || 'сотрудник'}
                        {u.login ? ` · ${u.login}` : ''}
                      </span>
                    </span>
                  </div>
                ))}
              </div>
              <footer className="row" style={{ gap: 8 }}>
                <span className="tiny muted">Отмена — Esc или клик мимо окна</span>
                <span className="grow" />
                <button type="button" className="btn btn--primary" onClick={() => setOpen(false)}>
                  Готово
                </button>
              </footer>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
