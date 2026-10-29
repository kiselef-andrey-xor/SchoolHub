/** Импорт учётных записей из Active Directory: поиск по каталогу → выбор → создание записей SchoolHub. */
import { useState } from 'react'

import { usersApi } from '@/api/endpoints'
import { Modal } from '@/components/Modal'
import { Badge, EmptyState, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { ROLE_LABEL_FULL } from '@/lib/format'
import type { ADUser, Role } from '@/types'

interface Props {
  open: boolean
  actorRole: Role
  onClose: () => void
  onDone: () => void
}

export function ADImportModal({ open, actorRole, onClose, onDone }: Props) {
  const toast = useToast()
  const [query, setQuery] = useState('')
  const [results, setResults] = useState<ADUser[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [role, setRole] = useState<Role>('TEACHER')
  const [searching, setSearching] = useState(false)
  const [importing, setImporting] = useState(false)
  const [searched, setSearched] = useState(false)

  const assignableRoles: Role[] = actorRole === 'OWNER' ? ['OWNER', 'ADMIN', 'TEACHER'] : ['TEACHER']

  async function search() {
    if (query.trim().length < 2) {
      toast.error(new Error('Введите минимум 2 символа для поиска'))
      return
    }
    setSearching(true)
    try {
      const found = await usersApi.adSearch(query.trim(), 50)
      setResults(found)
      setSearched(true)
      if (found.length === 0) toast.show('В каталоге никто не найден')
    } catch (err) {
      toast.error(err)
      setResults([])
    } finally {
      setSearching(false)
    }
  }

  function toggle(username: string) {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(username)) next.delete(username)
      else next.add(username)
      return next
    })
  }

  async function doImport() {
    if (selected.size === 0) return
    setImporting(true)
    try {
      const res = await usersApi.adImport(Array.from(selected), role)
      const parts: string[] = []
      if (res.created.length) parts.push(`создано: ${res.created.length}`)
      if (res.skipped.length) parts.push(`уже были: ${res.skipped.length}`)
      if (res.failed.length) parts.push(`ошибок: ${res.failed.length}`)
      toast.success(`Импорт завершён (${parts.join(', ') || 'нет изменений'})`)
      res.failed.forEach((f) => toast.error(new Error(`${f.username}: ${f.error}`)))
      setSelected(new Set())
      onDone()
      onClose()
    } catch (err) {
      toast.error(err)
    } finally {
      setImporting(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      wide
      title="Импорт пользователей из Active Directory"
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={importing}>Закрыть</button>
          <button className="btn btn--primary" onClick={doImport} disabled={importing || selected.size === 0}>
            {importing ? 'Импортируем…' : `Импортировать (${selected.size})`}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 14 }}>
        <div className="alert alert--info small">
          Импорт создаёт записи SchoolHub <strong>до первого входа</strong> сотрудника — это нужно, чтобы
          сразу назначить роль (например, сделать завуча «Администрацией»). Пароль не создаётся и не
          хранится: вход выполняется через домен.
        </div>

        <div className="row" style={{ gap: 10, alignItems: 'flex-end' }}>
          <label className="field grow">
            <span className="field__label">Поиск в каталоге</span>
            <input
              className="input"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => { if (e.key === 'Enter') void search() }}
              placeholder="ФИО, логин или e-mail — например «Морозова» или zavuch"
            />
          </label>
          <label className="field">
            <span className="field__label">Назначить роль</span>
            <select className="select" value={role} onChange={(e) => setRole(e.target.value as Role)}>
              {assignableRoles.map((r) => (
                <option key={r} value={r}>{ROLE_LABEL_FULL[r]}</option>
              ))}
            </select>
          </label>
          <button className="btn btn--primary" onClick={() => void search()} disabled={searching}>
            {searching ? 'Ищем…' : 'Найти'}
          </button>
        </div>

        {searching ? (
          <Spinner label="Запрашиваем контроллер домена…" />
        ) : results.length === 0 ? (
          searched ? (
            <EmptyState icon="🔍" title="Никого не найдено" hint="Проверьте запрос и доступность контроллера домена" />
          ) : (
            <EmptyState icon="👥" title="Начните поиск" hint="Минимум 2 символа: часть ФИО, логина или e-mail" />
          )
        ) : (
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: 40 }} />
                <th>Логин</th>
                <th>ФИО</th>
                <th>Должность</th>
                <th>Группы AD</th>
              </tr>
            </thead>
            <tbody>
              {results.map((u) => (
                <tr key={u.dn ?? u.username} onClick={() => toggle(u.username)} style={{ cursor: 'pointer' }}>
                  <td>
                    <input
                      type="checkbox"
                      checked={selected.has(u.username)}
                      onChange={() => toggle(u.username)}
                      disabled={u.disabled}
                    />
                  </td>
                  <td className="mono">
                    {u.username}
                    {u.disabled && <Badge tone="muted">отключён в AD</Badge>}
                  </td>
                  <td>
                    <div className="strong">{u.full_name}</div>
                    <div className="tiny muted">{u.email}</div>
                  </td>
                  <td className="small">{u.position ?? '—'}</td>
                  <td>
                    <div className="row" style={{ gap: 4 }}>
                      {u.groups.slice(0, 3).map((g) => (
                        <Badge key={g} tone="muted">{g}</Badge>
                      ))}
                      {u.groups.length > 3 && <span className="tiny muted">+{u.groups.length - 3}</span>}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </Modal>
  )
}
