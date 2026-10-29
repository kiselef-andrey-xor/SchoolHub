/** Массовое создание локальных учётных записей: таблица «ФИО ПАРОЛЬ[, e-mail[, роль]]».
 *  Источник — вставка из буфера (Excel/таблица) или CSV/TSV/TXT-файл.
 *  Парсинг и предпроверка — на клиенте, сервер получает уже разобранные строки. */
import { useMemo, useRef, useState } from 'react'

import { usersApi } from '@/api/endpoints'
import { Modal } from '@/components/Modal'
import { Badge } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { ROLE_LABEL_FULL } from '@/lib/format'
import type { BulkImportResult, Role } from '@/types'

const ROLE_ALIASES: Record<string, Role> = {
  teacher: 'TEACHER', учитель: 'TEACHER',
  admin: 'ADMIN', administrator: 'ADMIN', админ: 'ADMIN', администратор: 'ADMIN', администрация: 'ADMIN', завуч: 'ADMIN', директор: 'ADMIN',
  owner: 'OWNER', владелец: 'OWNER',
}

export interface ParsedRow {
  line: number
  full_name: string
  password: string
  email: string | null
  role: Role
  error: string | null
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]{2,}$/

export function parseTable(text: string, actorRole: Role): ParsedRow[] {
  const lines = text.split(/\r?\n/)
  const rows: ParsedRow[] = []
  let lineNo = 0
  for (const raw of lines) {
    lineNo += 1
    const line = raw.trim()
    if (!line) continue
    const delim = line.includes('\t') ? '\t' : line.includes(';') ? ';' : line.includes(',') ? ',' : null
    const cells = delim ? line.split(delim).map((c) => c.trim()) : [line]
    // строка-заголовок пропускается
    if (cells[0].toLowerCase() === 'фио' || (cells[0].toLowerCase().includes('фио') && cells.some((c) => c.toLowerCase().includes('пароль')))) continue
    if (cells.length < 2) {
      rows.push({ line: lineNo, full_name: cells[0] ?? '', password: '', email: null, role: 'TEACHER', error: 'Нужно минимум две колонки: ФИО и ПАРОЛЬ' })
      continue
    }
    const [full_name, password, third, fourth] = cells
    let email: string | null = null
    let roleRaw = ''
    if (fourth !== undefined) {
      email = third || null
      roleRaw = fourth
    } else if (third !== undefined) {
      if (third.includes('@')) email = third
      else roleRaw = third
    }
    const role = roleRaw ? (ROLE_ALIASES[roleRaw.toLowerCase()] ?? null) : 'TEACHER'
    let error: string | null = null
    if (full_name.length < 2) error = 'Слишком короткое ФИО'
    else if (!password || password.length < 8) error = 'Пароль короче 8 символов'
    else if (email && !EMAIL_RE.test(email)) error = 'Похоже, e-mail с ошибкой'
    else if (roleRaw && !role) error = `Не распознана роль «${roleRaw}»`
    else if (role && actorRole === 'ADMIN' && role !== 'TEACHER') error = 'Администрация может заводить только учителей'
    rows.push({ line: lineNo, full_name, password, email, role: role ?? 'TEACHER', error })
  }
  return rows
}

interface Props {
  open: boolean
  actorRole: Role
  onClose: () => void
  onDone: () => void
}

export function BulkImportModal({ open, actorRole, onClose, onDone }: Props) {
  const toast = useToast()
  const fileRef = useRef<HTMLInputElement>(null)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<BulkImportResult | null>(null)

  const rows = useMemo(() => parseTable(text, actorRole), [text, actorRole])
  const valid = rows.filter((r) => !r.error)
  const invalid = rows.filter((r) => r.error)

  function reset() {
    setText('')
    setResult(null)
  }

  async function onFile(f: File) {
    if (f.size > 2 * 1024 * 1024) {
      toast.error('Файл больше 2 МБ — вставьте данные вручную')
      return
    }
    const content = await f.text()
    setText(content)
    setResult(null)
  }

  function downloadTemplate() {
    const sample = 'ФИО\tПАРОЛЬ\tE-mail (необязательно)\tРоль (необязательно)\nИванова Иванна Петровна\tParol123!\t\tУчитель\nСидоров Сидор Сидорович\tParol123!\tsidorov@school.ru\tАдминистрация\n'
    const url = URL.createObjectURL(new Blob(['\ufeff' + sample], { type: 'text/csv;charset=utf-8' }))
    const a = document.createElement('a')
    a.href = url
    a.download = 'schoolhub-users-template.csv'
    a.click()
    setTimeout(() => URL.revokeObjectURL(url), 2000)
  }

  async function submit() {
    if (valid.length === 0) return
    setBusy(true)
    try {
      const res = await usersApi.bulkCreate(
        valid.map((r) => ({ full_name: r.full_name, password: r.password, email: r.email, role: r.role })),
      )
      setResult(res)
      onDone()
      if (res.created.length) toast.success(`Создано учётных записей: ${res.created.length}`)
      if (res.failed.length) toast.error(`Ошибок: ${res.failed.length}`)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      wide
      onClose={() => { reset(); onClose() }}
      title="Массовая загрузка учётных записей"
      footer={
        result ? (
          <button className="btn btn--primary" onClick={() => { reset(); onClose() }}>Готово</button>
        ) : (
          <>
            <button className="btn" onClick={() => { reset(); onClose() }} disabled={busy}>Отмена</button>
            <button className="btn btn--primary" onClick={() => void submit()} disabled={busy || valid.length === 0}>
              {busy ? 'Создаём…' : `Создать учётки (${valid.length})`}
            </button>
          </>
        )
      }
    >
      {result ? (
        <div className="stack" style={{ gap: 12 }}>
          <div className="alert alert--info small">
            Создано: <strong>{result.created.length}</strong> из {result.total}.
            {' '}Сообщите сотрудникам их логины для входа (e-mail не обязателен — вход по логину и паролю).
          </div>
          {result.created.length > 0 && (
            <table className="table">
              <thead>
                <tr><th>ФИО</th><th style={{ width: 220 }}>Логин для входа</th><th style={{ width: 240 }}>E-mail</th><th style={{ width: 140 }}>Роль</th></tr>
              </thead>
              <tbody>
                {result.created.map((c) => (
                  <tr key={`${c.full_name}-${c.login ?? c.email ?? ''}`}>
                    <td>{c.full_name}</td>
                    <td className="mono">{c.login ?? '—'}</td>
                    <td className="small">{c.email ?? '—'}</td>
                    <td><Badge tone="muted">{ROLE_LABEL_FULL[c.role]}</Badge></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
          {result.failed.length > 0 && (
            <div className="alert alert--error small">
              <strong>Не созданы:</strong>
              <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                {result.failed.map((f) => (<li key={f.row}>строка {f.row}: {f.full_name} — {f.error}</li>))}
              </ul>
            </div>
          )}
        </div>
      ) : (
        <div className="stack" style={{ gap: 12 }}>
          <div className="row" style={{ gap: 8, flexWrap: 'wrap' }}>
            <button className="btn btn--sm" onClick={() => fileRef.current?.click()}>📄 Выбрать файл (CSV/TSV/TXT)</button>
            <button className="btn btn--sm btn--ghost" onClick={downloadTemplate}> Шаблон таблицы</button>
            <input
              ref={fileRef}
              type="file"
              accept=".csv,.tsv,.txt,text/csv,text/plain"
              style={{ display: 'none' }}
              onChange={(e) => { const f = e.target.files?.[0]; if (f) void onFile(f); e.target.value = '' }}
            />
          </div>
          <label className="field">
            <span className="field__label">Данные: по одной учётке в строке</span>
            <textarea
              className="input mono"
              style={{ minHeight: 160, resize: 'vertical' }}
              value={text}
              onChange={(e) => { setText(e.target.value); setResult(null) }}
              placeholder={'Иванова Иванна Петровна\tParol123!\nСидоров Сидор\tParol123!\tsidorov@school.ru\nПетров Пётр\tParol123!\t\tАдминистрация'}
            />
            <span className="field__hint">
              Колонки (через таб, «;» или «,»): ФИО, ПАРОЛЬ (мин. 8 символов), e-mail (необязательно), роль (необязательно: Учитель / Администрация / Владелец).
              Без e-mail сотрудник входит по сгенерированному логину (транслит ФИО) — логины покажем после создания.
            </span>
          </label>
          {rows.length > 0 && (
            <div className="stack" style={{ gap: 6 }}>
              <div className="row" style={{ gap: 8 }}>
                <Badge tone="ok">к созданию: {valid.length}</Badge>
                {invalid.length > 0 && <Badge tone="danger">с ошибками: {invalid.length}</Badge>}
              </div>
              <div style={{ maxHeight: 220, overflow: 'auto' }}>
                <table className="table">
                  <thead>
                    <tr><th style={{ width: 40 }}>Стр.</th><th>ФИО</th><th style={{ width: 220 }}>E-mail</th><th style={{ width: 140 }}>Роль</th><th style={{ width: 260 }}>Проблема</th></tr>
                  </thead>
                  <tbody>
                    {rows.map((r) => (
                      <tr key={r.line} style={r.error ? { opacity: 0.75 } : undefined}>
                        <td className="tiny muted">{r.line}</td>
                        <td>{r.full_name || '—'}</td>
                        <td className="small">{r.email ?? '—'}</td>
                        <td className="small">{ROLE_LABEL_FULL[r.role]}</td>
                        <td className="small" style={{ color: r.error ? 'var(--danger)' : undefined }}>{r.error ?? ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
        </div>
      )}
    </Modal>
  )
}
