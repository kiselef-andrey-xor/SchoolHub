/** Мультивыбор ответственных: сотрудники + должности + «Администрация» целиком.
 *  Значение — список ссылок: «<uuid>» | «pos:<uuid>» | «admin» (как хранит бэкенд).
 *  Используется в редакторе плана работы; мероприятия ВШК выбирают ответственных там же. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'

import { Avatar } from '@/components/ui'
import { UserAvatar } from '@/components/UserAvatar'
import { shortName } from '@/lib/format'
import type { Position, User } from '@/types'

interface Props {
  users: User[]
  positions: Position[]
  value: string[]
  onChange: (refs: string[]) => void
  placeholder?: string
  disabled?: boolean
  /** показывать чекбокс «Администрация» (план/ВШК) */
  allowAdmin?: boolean
}

export function refTitle(ref: string, users: User[], positions: Position[]): string {
  if (ref === 'admin') return 'Администрация'
  if (ref.startsWith('pos:')) {
    const p = positions.find((x) => x.id === ref.slice(4))
    return p ? p.title : '…'
  }
  const u = users.find((x) => x.id === ref)
  return u ? shortName(u.full_name) : '…'
}

export function RespMultiSelect({
  users,
  positions,
  value,
  onChange,
  placeholder = 'Никто не выбран',
  disabled,
  allowAdmin = true,
}: Props) {
  const [open, setOpen] = useState(false)
  const [query, setQuery] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)

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

  const q = query.trim().toLowerCase()
  const filteredUsers = useMemo(
    () =>
      q
        ? users.filter((u) =>
            `${u.full_name} ${(u.positions || []).map((p) => p.title).join(' ')} ${u.email ?? ''}`.toLowerCase().includes(q),
          )
        : users,
    [users, q],
  )
  const filteredPositions = useMemo(
    () => (q ? positions.filter((p) => p.title.toLowerCase().includes(q)) : positions),
    [positions, q],
  )

  const toggle = (ref: string) =>
    onChange(value.includes(ref) ? value.filter((x) => x !== ref) : [...value, ref])

  return (
    <div>
      {value.length > 0 ? (
        <div className="chips">
          {value.map((ref) => (
            <span className={`uchip${ref === 'admin' ? ' uchip--admin' : ''}`} key={ref}>
              {ref.startsWith('pos:') || ref === 'admin' ? (
                <Avatar name={refTitle(ref, users, positions)} size="sm" />
              ) : (
                <UserAvatar user={users.find((u) => u.id === ref) ?? ({ full_name: '?' } as User)} size="sm" />
              )}
              <span>{refTitle(ref, users, positions)}</span>
              {!disabled && (
                <button type="button" className="uchip__x" title="Убрать" onClick={() => toggle(ref)}>
                  ✕
                </button>
              )}
            </span>
          ))}
        </div>
      ) : (
        <div className="tiny muted">{placeholder}</div>
      )}

      {!disabled && (
        <button type="button" className="btn btn--sm" style={{ marginTop: 7 }} onClick={() => setOpen(true)}>
          {value.length ? `✎ Изменить список (${value.length})` : '＋ Выбрать ответственных'}
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
                <strong>Ответственные</strong>
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
                placeholder="Поиск: ФИО или должность…"
              />
              <div className="row" style={{ gap: 6 }}>
                <button type="button" className="btn btn--sm" onClick={() => onChange([])}>
                  Очистить
                </button>
                <span className="grow" />
                <button type="button" className="btn btn--primary btn--sm" onClick={() => setOpen(false)}>
                  Готово
                </button>
              </div>
              <div className="ums-list">
                {allowAdmin && (!q || 'администрация'.includes(q)) && (
                  <div className="picker__row" onClick={() => toggle('admin')}>
                    <span className="picker__check">{value.includes('admin') ? '☑' : '☐'}</span>
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: 'block', fontWeight: 700 }}>Администрация</span>
                      <span className="tiny muted">все с ролью Владелец/Администратор</span>
                    </span>
                  </div>
                )}
                {filteredPositions.length > 0 && (
                  <div className="picker__group">Должности — все держатели</div>
                )}
                {filteredPositions.map((p) => {
                  const ref = `pos:${p.id}`
                  return (
                    <div className="picker__row" key={ref} onClick={() => toggle(ref)}>
                      <span className="picker__check">{value.includes(ref) ? '☑' : '☐'}</span>
                      <span style={{ minWidth: 0 }}>
                        <span style={{ display: 'block', fontWeight: 600 }}>{p.title}</span>
                        <span className="tiny muted">
                          держателей: {users.filter((u) => (u.positions || []).some((x) => x.id === p.id)).length}
                        </span>
                      </span>
                    </div>
                  )
                })}
                {filteredUsers.length > 0 && <div className="picker__group">Сотрудники</div>}
                {filteredUsers.map((u) => (
                  <div className="picker__row" key={u.id} onClick={() => toggle(u.id)}>
                    <span className="picker__check">{value.includes(u.id) ? '☑' : '☐'}</span>
                    <UserAvatar user={u} size="sm" />
                    <span style={{ minWidth: 0 }}>
                      <span style={{ display: 'block', fontWeight: 600 }}>{u.full_name}</span>
                      <span className="tiny muted">{(u.positions || []).map((p) => p.title).join(', ') || 'сотрудник'}</span>
                    </span>
                  </div>
                ))}
                {filteredUsers.length === 0 && filteredPositions.length === 0 && (
                  <div className="small muted" style={{ padding: 10 }}>Никого не найдено</div>
                )}
              </div>
            </div>
          </div>,
          document.body,
        )}
    </div>
  )
}
