/** Раздел «Движение обучающихся»: перевод / зачисление / отчисление.
 *  В приказе может быть МНОГО обучающихся (кнопка «+» и массовое добавление классом).
 *  Печатная форма — ОБЩИЙ шаблон на все приказы (кнопка «📄 Печатная форма»),
 *  плейсхолдеры подставляются при печати; строки с «|» печатаются таблицей. */
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'
import { useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { metaApi, movementApi, usersApi } from '@/api/endpoints'
import { downloadAuthorized } from '@/api/client'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Badge, EmptyState, ErrorState, Pagination, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDate } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import type { MovementChild, MovementOrder, MovementStatus, MovementType } from '@/types'

const PER_PAGE = 25

export const MOVEMENT_TYPE_LABEL: Record<MovementType, string> = {
  TRANSFER: 'перевод',
  ENROLLMENT: 'зачисление',
  EXPULSION: 'отчисление',
}
const TYPE_TITLE: Record<MovementType, string> = {
  TRANSFER: 'О переводе',
  ENROLLMENT: 'О зачислении на обучение по основной образовательной программе',
  EXPULSION: 'Об отчислении',
}
const TYPE_VERB: Record<MovementType, string> = {
  TRANSFER: 'Перевести',
  ENROLLMENT: 'Зачислить',
  EXPULSION: 'Отчислить',
}
const MOVEMENT_STATUS_LABEL: Record<MovementStatus, string> = {
  NEW: 'новый',
  DONE: 'выполнен',
  ERROR: 'ошибка',
}
const STATUS_TONE: Record<MovementStatus, 'warn' | 'ok' | 'danger'> = {
  NEW: 'warn',
  DONE: 'ok',
  ERROR: 'danger',
}

interface ChildForm {
  student: string
  birth_date: string
  from_place: string
  to_place: string
}
interface FormState {
  id?: string
  order_type: MovementType
  guardian: string
  children: ChildForm[]
  from_place: string
  to_place: string
  order_date: string
  order_number: string
  signatory: string
  note: string
  status: MovementStatus
}

const emptyChild = (): ChildForm => ({ student: '', birth_date: '', from_place: '', to_place: '' })
const emptyForm = (): FormState => ({
  order_type: 'TRANSFER',
  guardian: '',
  children: [emptyChild()],
  from_place: '',
  to_place: '',
  order_date: new Date().toISOString().slice(0, 10),
  order_number: '',
  signatory: '',
  note: '',
  status: 'NEW',
})

function ruDate(iso: string): string {
  if (!iso) return ''
  const [y, m, d] = iso.split('-')
  return `${d}.${m}.${y}`
}

/** Авто-тело приказа: один ребёнок — абзац, несколько — пункт с таблицей. */
function buildBody(order: { order_type: MovementType; guardian?: string | null; note?: string | null; order_date: string }, children: MovementChild[], fallbackFrom?: string | null, fallbackTo?: string | null): { body: string; control: string } {
  const kids = children.map((k) => ({
    student: k.student,
    birth: k.birth_date ? ruDate(k.birth_date) : '',
    from: k.from_place || fallbackFrom || '',
    to: k.to_place || fallbackTo || '',
  }))
  const lines: string[] = []
  if (kids.length === 1) {
    const k = kids[0]
    const birth = k.birth ? `${k.birth} г.р.` : ''
    if (order.order_type === 'TRANSFER') lines.push(`1. Перевести обучающегося ${k.student}${birth ? ', ' + birth : ''}, из ${k.from || '—'} в ${k.to || '—'} с ${ruDate(order.order_date)}.`)
    else if (order.order_type === 'ENROLLMENT') lines.push(`1. Зачислить обучающегося ${k.student}${birth ? ', ' + birth : ''}, в ${k.to || '—'} с ${ruDate(order.order_date)}.`)
    else lines.push(`1. Отчислить обучающегося ${k.student}${birth ? ', ' + birth : ''}, из ${k.from || '—'} с ${ruDate(order.order_date)}.`)
  } else {
    lines.push(`1. ${TYPE_VERB[order.order_type]} с ${ruDate(order.order_date)} следующих обучающихся:`)
    lines.push('№ п/п | ФИО обучающегося | Дата рождения | Откуда | Куда')
    kids.forEach((k, i) => lines.push(`${i + 1}. | ${k.student} | ${k.birth} | ${k.from} | ${k.to}`))
  }
  let n = 2
  if (order.note) {
    lines.push(`${n}. ${order.note}`)
    n += 1
  }
  return { body: lines.join('\n'), control: `${n}. Контроль исполнения настоящего приказа оставляю за собой.` }
}

/** Строки с «|» — таблица, остальные — абзацы с красной строкой. */
function BodyBlocks({ text }: { text: string }) {
  const blocks: { kind: 'p' | 'table'; rows: string[][] }[] = []
  for (const raw of text.split('\n')) {
    const line = raw.trimEnd()
    if (!line.trim()) continue
    if (line.includes('|')) {
      const cells = line.split('|').map((c) => c.trim())
      const last = blocks[blocks.length - 1]
      if (last && last.kind === 'table') last.rows.push(cells)
      else blocks.push({ kind: 'table', rows: [cells] })
    } else {
      blocks.push({ kind: 'p', rows: [[line.trim()]] })
    }
  }
  return (
    <>
      {blocks.map((b, i) =>
        b.kind === 'p' ? (
          <p className="mv-p" key={i}>{b.rows[0][0]}</p>
        ) : (
          <table className="mv-table" key={i}>
            <tbody>
              {b.rows.map((r, j) => (
                <tr key={j}>
                  {r.map((c, k) => (<td key={k}>{c}</td>))}
                </tr>
              ))}
            </tbody>
          </table>
        ),
      )}
    </>
  )
}

export function MovementPage() {
  const me = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()

  const [status, setStatus] = useState<MovementStatus | ''>('')
  const [type, setType] = useState<MovementType | ''>('')
  // подписант по умолчанию — директор; заменить можно любым сотрудником администрации
  const { data: owners = [] } = useQuery({ queryKey: qk('/users/options', { role: 'OWNER' }), queryFn: () => usersApi.options('OWNER') })
  const { data: admins = [] } = useQuery({ queryKey: qk('/users/options', { role: 'ADMIN' }), queryFn: () => usersApi.options('ADMIN') })
  const staffOptions = useMemo(
    () => [...owners, ...admins].sort((a, b) => a.full_name.localeCompare(b.full_name, 'ru')),
    [owners, admins],
  )
  const defaultSignatory = useMemo(
    () =>
      staffOptions.find((u) => (u.positions || []).some((pp) => /директор/i.test(pp.title))) ??
      owners[0] ??
      admins[0] ??
      null,
    [staffOptions, owners, admins],
  )

  const [searchParams] = useSearchParams()
  const [search, setSearch] = useState(() => searchParams.get('search') ?? '')
  const [debounced, setDebounced] = useState(() => searchParams.get('search') ?? '')
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [bulkText, setBulkText] = useState('')
  const [busy, setBusy] = useState(false)
  const [checked, setChecked] = useState<string[]>([])
  const [printRow, setPrintRow] = useState<MovementOrder | null>(null)
  const [summaryPrint, setSummaryPrint] = useState(false)
  const [summaryOpen, setSummaryOpen] = useState(false)
  const [tplOpen, setTplOpen] = useState(false)
  const [tplText, setTplText] = useState<string>('')

  const { data: schoolInfo } = useQuery({ queryKey: qk('/meta/school'), queryFn: metaApi.school })
  const { data: tpl } = useQuery({ queryKey: qk('/movement/template'), queryFn: movementApi.template })

  useEffect(() => {
    const id = window.setTimeout(() => { setDebounced(search); setPage(1) }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  const printing = Boolean(printRow) || summaryPrint
  useEffect(() => {
    if (!printing) return
    document.body.classList.add('print-sheet-active')
    const t = window.setTimeout(() => window.print(), 250)
    const after = () => {
      setPrintRow(null)
      setSummaryPrint(false)
    }
    window.addEventListener('afterprint', after)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('afterprint', after)
      document.body.classList.remove('print-sheet-active')
    }
  }, [printing])

  const params = useMemo(
    () => ({ status: status || undefined, order_type: type || undefined, search: debounced || undefined, page, per_page: PER_PAGE }),
    [status, type, debounced, page],
  )

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/movement', params as never),
    queryFn: () => movementApi.list(params),
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['/movement'] })

  async function submit() {
    if (!form) return
    const kids = form.children
      .filter((c) => c.student.trim())
      .map((c) => ({
        student: c.student.trim(),
        birth_date: c.birth_date || null,
        from_place: c.from_place.trim() || null,
        to_place: c.to_place.trim() || null,
      }))
    if (kids.length === 0) {
      toast.error(new Error('Добавьте хотя бы одного обучающегося'))
      return
    }
    setBusy(true)
    try {
      const payload = {
        order_type: form.order_type,
        guardian: form.guardian.trim() || null,
        children: kids,
        from_place: form.from_place.trim() || null,
        to_place: form.to_place.trim() || null,
        order_date: form.order_date,
        order_number: form.order_number.trim(),
        signatory: form.signatory.trim() || null,
        note: form.note.trim() || null,
        status: form.status,
      }
      if (form.id) {
        await movementApi.update(form.id, payload)
        toast.success('Приказ обновлён')
      } else {
        await movementApi.create(payload)
        toast.success('Приказ создан — можно печатать')
      }
      setForm(null)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function setStatusRow(row: MovementOrder, value: MovementStatus) {
    try {
      await movementApi.update(row.id, { status: value })
      toast.success(`Статус: ${MOVEMENT_STATUS_LABEL[value]}`)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  async function removeRow(row: MovementOrder) {
    if (!window.confirm(`Удалить приказ № ${row.order_number}?`)) return
    try {
      await movementApi.remove(row.id)
      toast.success('Приказ удалён')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  function applyBulk() {
    if (!form) return
    const fromCls = form.from_place.trim()
    const toCls = form.to_place.trim()
    const added: ChildForm[] = []
    for (const raw of bulkText.split('\n')) {
      const line = raw.trim()
      if (!line) continue
      const cells = line.includes('\t') ? line.split('\t') : line.includes(';') ? line.split(';') : line.split(',')
      const student = cells[0]?.trim() ?? ''
      if (!student) continue
      let birth = ''
      const m = (cells[1] ?? '').trim().match(/(\d{1,2})[.\/](\d{1,2})[.\/](\d{4})/)
      if (m) birth = `${m[3]}-${m[2].padStart(2, '0')}-${m[1].padStart(2, '0')}`
      added.push({
        student,
        birth_date: birth,
        from_place: (cells[2] ?? '').trim() || fromCls,
        to_place: (cells[3] ?? '').trim() || toCls,
      })
    }
    if (added.length === 0) {
      toast.error(new Error('Не разобрал ни одной строки. Формат: ФИО, дата рождения (необязательно)'))
      return
    }
    const existing = form.children.filter((c) => c.student.trim())
    setForm({ ...form, children: [...existing, ...added] })
    setBulkOpen(false)
    setBulkText('')
    toast.success(`Добавлено обучающихся: ${added.length}`)
  }

  async function saveTemplate(value: string | null) {
    setBusy(true)
    try {
      await movementApi.saveTemplate(value)
      void qc.invalidateQueries({ queryKey: qk('/movement/template') })
      toast.success(value ? 'Шаблон печатной формы сохранён для всех приказов' : 'Шаблон сброшен к стандартному')
      setTplOpen(false)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  const rows = data?.items ?? []
  const checkedRows = rows.filter((r) => checked.includes(r.id))

  function copySummary() {
    const head = ['Тип', '№ приказа', 'Дата приказа', 'Обучающийся', 'Дата рождения', 'Представитель', 'Откуда', 'Куда', 'Статус']
    const lines: string[] = []
    for (const r of checkedRows) {
      for (const k of r.children) {
        lines.push([
          MOVEMENT_TYPE_LABEL[r.order_type], r.order_number, ruDate(r.order_date), k.student,
          k.birth_date ? ruDate(k.birth_date) : '', r.guardian ?? '',
          k.from_place || r.from_place || '', k.to_place || r.to_place || '', MOVEMENT_STATUS_LABEL[r.status],
        ].join('\t'))
      }
    }
    void navigator.clipboard.writeText([head.join('\t'), ...lines].join('\n'))
    toast.success('Сводка скопирована — можно вставлять в Excel/другую систему')
  }

  /** Печатный документ приказа: шапка + шаблон с плейсхолдерами + подпись. */
  function printDocument(row: MovementOrder) {
    const template = tpl?.template ?? tpl?.default ?? ''
    const { body, control } = buildBody(row, row.children, row.from_place, row.to_place)
    const preamble = row.guardian
      ? `На основании заявления родителя (законного представителя) ${row.guardian}.`
      : ''
    const text = template
      .replaceAll('{PREAMBLE}', preamble)
      .replaceAll('{BODY}', body)
      .replaceAll('{CONTROL}', control)
      .replaceAll('{NOTE}', row.note ?? '')
      .replaceAll('{GUARDIAN}', row.guardian ?? '')
      .replaceAll('{NUMBER}', row.order_number)
      .replaceAll('{DATE}', ruDate(row.order_date))
      .replaceAll('{TITLE}', TYPE_TITLE[row.order_type])
      .replaceAll('{SIGNATORY}', row.signatory ?? '')
      .replaceAll('{SCHOOL}', (schoolInfo?.school_name ?? '').toUpperCase())
      .split('\n')
      .filter((l) => l.trim() !== '' || l === '')
      .join('\n')
      .replace(/\n{3,}/g, '\n\n')
    return text
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Движение обучающихся</h1>
          <div className="page-head__hint">
            Приказы о переводе, зачислении и отчислении (в том числе с несколькими обучающимися),
            печать по общему шаблону и сводка для внешних систем.
          </div>
        </div>
        <div className="page-head__actions">
          <button
            className="btn"
            onClick={() => {
              setTplText(tpl?.template ?? tpl?.default ?? '')
              setTplOpen(true)
            }}
            title="Общий шаблон печатной формы для всех приказов"
          >
            📄 Печатная форма
          </button>
          <button className="btn" disabled={checked.length === 0} onClick={() => setSummaryOpen(true)}>
            📋 Сводка ({checked.length})
          </button>
          <button
            className="btn btn--primary"
            onClick={() => setForm({ ...emptyForm(), signatory: defaultSignatory?.full_name ?? '' })}
          >
            ＋ Приказ
          </button>
        </div>
      </div>

      <section className="card">
        <div className="card__body">
          <div className="filters">
            <label className="field field--grow">
              <span className="field__label">Поиск</span>
              <input className="input" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="ФИО, № приказа, класс…" />
            </label>
            <label className="field">
              <span className="field__label">Тип</span>
              <select className="select" value={type} onChange={(e) => { setType(e.target.value as MovementType | ''); setPage(1) }}>
                <option value="">Все типы</option>
                <option value="TRANSFER">перевод</option>
                <option value="ENROLLMENT">зачисление</option>
                <option value="EXPULSION">отчисление</option>
              </select>
            </label>
            <label className="field">
              <span className="field__label">Статус</span>
              <select className="select" value={status} onChange={(e) => { setStatus(e.target.value as MovementStatus | ''); setPage(1) }}>
                <option value="">Все статусы</option>
                <option value="NEW">новый</option>
                <option value="DONE">выполнен</option>
                <option value="ERROR">ошибка</option>
              </select>
            </label>
          </div>
        </div>
      </section>

      <section className="card">
        {isLoading ? (
          <Spinner label="Открываем раздел…" />
        ) : error ? (
          <ErrorState error={error} onRetry={() => void refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState icon="🎓" title="Приказов пока нет" hint="Нажмите «＋ Приказ», чтобы составить первый" />
        ) : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 34 }}>
                      <input
                        type="checkbox"
                        checked={checked.length > 0 && checked.length === rows.length}
                        onChange={(e) => setChecked(e.target.checked ? rows.map((r) => r.id) : [])}
                        title="Отметить все на странице"
                      />
                    </th>
                    <th style={{ width: 130 }}>Приказ</th>
                    <th style={{ width: 110 }}>Тип</th>
                    <th>Обучающиеся</th>
                    <th style={{ width: 200 }}>Законный представитель</th>
                    <th style={{ width: 200 }}>Движение</th>
                    <th style={{ width: 130 }}>Статус</th>
                    <th style={{ width: 110 }} />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td>
                        <input
                          type="checkbox"
                          checked={checked.includes(r.id)}
                          onChange={(e) => setChecked((prev) => (e.target.checked ? [...prev, r.id] : prev.filter((x) => x !== r.id)))}
                        />
                      </td>
                      <td className="small nowrap">
                        № {r.order_number}
                        <div className="tiny muted">{formatDate(r.order_date)}</div>
                      </td>
                      <td><Badge tone="brand">{MOVEMENT_TYPE_LABEL[r.order_type]}</Badge></td>
                      <td className="small">
                        {r.children.slice(0, 3).map((k) => k.student).join('; ')}
                        {r.children.length > 3 && <div className="tiny muted">… и ещё {r.children.length - 3}</div>}
                        {r.children.length === 1 && r.children[0].birth_date && (
                          <div className="tiny muted">{formatDate(r.children[0].birth_date)} г.р.</div>
                        )}
                      </td>
                      <td className="small">{r.guardian || '—'}</td>
                      <td className="small">
                        {r.from_place && <span>{r.from_place} → </span>}
                        {r.to_place ?? ''}
                        {!r.from_place && !r.to_place && '—'}
                      </td>
                      <td>
                        <select className="select" value={r.status} onChange={(e) => void setStatusRow(r, e.target.value as MovementStatus)}>
                          <option value="NEW">новый</option>
                          <option value="DONE">выполнен</option>
                          <option value="ERROR">ошибка</option>
                        </select>
                      </td>
                      <td>
                        <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                          <button className="btn btn--sm" title="Печать приказа" onClick={() => setPrintRow(r)}>🖨</button>
                          <button
                            className="btn btn--sm"
                            title="Изменить реквизиты и состав обучающихся"
                            onClick={() =>
                              setForm({
                                id: r.id,
                                order_type: r.order_type,
                                guardian: r.guardian ?? '',
                                children: r.children.length
                                  ? r.children.map((k) => ({
                                      student: k.student,
                                      birth_date: k.birth_date ?? '',
                                      from_place: k.from_place ?? '',
                                      to_place: k.to_place ?? '',
                                    }))
                                  : [emptyChild()],
                                from_place: r.from_place ?? '',
                                to_place: r.to_place ?? '',
                                order_date: r.order_date,
                                order_number: r.order_number,
                                signatory: r.signatory ?? '',
                                note: r.note ?? '',
                                status: r.status,
                              })
                            }
                          >
                            ✎
                          </button>
                          {me.role === 'OWNER' && (
                            <button className="btn btn--sm btn--danger" title="Удалить" onClick={() => void removeRow(r)}>🗑</button>
                          )}
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <Pagination page={data!.page} pages={data!.pages} total={data!.total} perPage={data!.per_page} onPage={setPage} />
          </>
        )}
      </section>

      {/* ---------------- форма приказа с детьми ---------------- */}
      <Modal
        open={form !== null}
        onClose={() => setForm(null)}
        wide
        title={form?.id ? 'Приказ по движению' : 'Новый приказ по движению'}
        footer={
          <>
            <button className="btn" onClick={() => setForm(null)} disabled={busy}>Отмена</button>
            <button className="btn btn--primary" onClick={() => void submit()} disabled={busy}>
              {busy ? 'Сохраняем…' : 'Сохранить'}
            </button>
          </>
        }
      >
        {form && (
          <div className="stack" style={{ gap: 12 }}>
            <div className="form-grid">
              <label className="field">
                <span className="field__label">Тип приказа *</span>
                <select className="select" value={form.order_type} onChange={(e) => setForm({ ...form, order_type: e.target.value as MovementType })}>
                  <option value="TRANSFER">перевод</option>
                  <option value="ENROLLMENT">зачисление</option>
                  <option value="EXPULSION">отчисление</option>
                </select>
              </label>
              <label className="field">
                <span className="field__label">Статус</span>
                <select className="select" value={form.status} onChange={(e) => setForm({ ...form, status: e.target.value as MovementStatus })}>
                  <option value="NEW">новый</option>
                  <option value="DONE">выполнен</option>
                  <option value="ERROR">ошибка</option>
                </select>
              </label>
              <label className="field" style={{ gridColumn: '1 / -1' }}>
                <span className="field__label">Законный представитель (необязательно)</span>
                <input className="input" value={form.guardian} onChange={(e) => setForm({ ...form, guardian: e.target.value })} placeholder="Фамилия И.О. или несколько через «; »" />
              </label>
              <label className="field">
                <span className="field__label">Откуда (по умолчанию для всех)</span>
                <input className="input" value={form.from_place} onChange={(e) => setForm({ ...form, from_place: e.target.value })} placeholder="6А класс / прежняя школа" />
              </label>
              <label className="field">
                <span className="field__label">Куда (по умолчанию для всех)</span>
                <input className="input" value={form.to_place} onChange={(e) => setForm({ ...form, to_place: e.target.value })} placeholder="7А класс / другая школа" />
              </label>
              <label className="field">
                <span className="field__label">Дата приказа *</span>
                <input className="input" type="date" required value={form.order_date} onChange={(e) => setForm({ ...form, order_date: e.target.value })} />
              </label>
              <label className="field">
                <span className="field__label">№ приказа *</span>
                <input className="input" required value={form.order_number} onChange={(e) => setForm({ ...form, order_number: e.target.value })} placeholder="119-У" />
              </label>
              <label className="field">
                <span className="field__label">Подписант</span>
                <select
                  className="select"
                  value={form.signatory}
                  onChange={(e) => setForm({ ...form, signatory: e.target.value })}
                  title="По умолчанию — директор; при необходимости выберите сотрудника администрации"
                >
                  <option value="">— не указан —</option>
                  {form.signatory && !staffOptions.some((u) => u.full_name === form.signatory) && (
                    <option value={form.signatory}>{form.signatory} (внесён вручную)</option>
                  )}
                  {staffOptions.map((u) => (
                    <option key={u.id} value={u.full_name}>
                      {u.full_name}
                      {(u.positions || []).length ? ` — ${(u.positions || []).map((pp) => pp.title).join(', ')}` : ''}
                    </option>
                  ))}
                </select>
                <span className="field__hint">По умолчанию — директор; список администрации для замены</span>
              </label>
              <label className="field" style={{ gridColumn: '1 / -1' }}>
                <span className="field__label">Примечание (отдельный пункт приказа)</span>
                <input className="input" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="Например: классным руководителям внести изменения в журналы" />
              </label>
            </div>

            <div>
              <div className="row" style={{ gap: 8, marginBottom: 8 }}>
                <strong className="small">Обучающиеся ({form.children.filter((c) => c.student.trim()).length})</strong>
                <span className="grow" />
                <button type="button" className="btn btn--sm" onClick={() => setBulkOpen(true)}>👥 Добавить классом</button>
                <button
                  type="button"
                  className="btn btn--sm btn--primary"
                  onClick={() => setForm({ ...form, children: [...form.children, emptyChild()] })}
                >
                  ＋ Ребёнок
                </button>
              </div>
              <div className="stack" style={{ gap: 8 }}>
                {form.children.map((c, i) => (
                  <div key={i} className="card" style={{ padding: 10 }}>
                    <div className="form-grid">
                      <label className="field" style={{ gridColumn: '1 / -1' }}>
                        <span className="field__label">ФИО обучающегося {i + 1} *</span>
                        <input
                          className="input"
                          value={c.student}
                          onChange={(e) => {
                            const children = [...form.children]
                            children[i] = { ...c, student: e.target.value }
                            setForm({ ...form, children })
                          }}
                          placeholder="Кривову Елену Николаевну (в винительном падеже, как в приказе)"
                        />
                      </label>
                      <label className="field">
                        <span className="field__label">Дата рождения</span>
                        <input
                          className="input"
                          type="date"
                          value={c.birth_date}
                          onChange={(e) => {
                            const children = [...form.children]
                            children[i] = { ...c, birth_date: e.target.value }
                            setForm({ ...form, children })
                          }}
                        />
                      </label>
                      <label className="field">
                        <span className="field__label">Откуда (лично)</span>
                        <input
                          className="input"
                          value={c.from_place}
                          onChange={(e) => {
                            const children = [...form.children]
                            children[i] = { ...c, from_place: e.target.value }
                            setForm({ ...form, children })
                          }}
                          placeholder="пусто = общее «откуда»"
                        />
                      </label>
                      <label className="field">
                        <span className="field__label">Куда (лично)</span>
                        <input
                          className="input"
                          value={c.to_place}
                          onChange={(e) => {
                            const children = [...form.children]
                            children[i] = { ...c, to_place: e.target.value }
                            setForm({ ...form, children })
                          }}
                          placeholder="пусто = общее «куда»"
                        />
                      </label>
                      <div className="field" style={{ display: 'flex', alignItems: 'flex-end' }}>
                        <button
                          type="button"
                          className="btn btn--sm btn--danger"
                          disabled={form.children.length === 1}
                          onClick={() => setForm({ ...form, children: form.children.filter((_, j) => j !== i) })}
                        >
                          ✕ Убрать
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Modal>

      {/* ---------------- массовое добавление классом ---------------- */}
      <Modal
        open={bulkOpen}
        onClose={() => setBulkOpen(false)}
        wide
        title="Добавить обучающихся списком (классом)"
        footer={
          <>
            <button className="btn" onClick={() => setBulkOpen(false)}>Отмена</button>
            <button className="btn btn--primary" onClick={applyBulk}>Добавить в приказ</button>
          </>
        }
      >
        <div className="stack" style={{ gap: 10 }}>
          <div className="alert alert--info small">
            По строке на ребёнка: <span className="mono">ФИО[, дата рождения[, откуда[, куда]]]</span>
            (разделитель — запятая, «;» или таб). Если «откуда/куда» не указаны, подставятся общие
            поля формы — удобно для перевода всего класса на новый учебный год: укажите
            «Откуда: 6А», «Куда: 7А» и вставьте список ФИО.
          </div>
          <textarea
            className="input mono"
            style={{ minHeight: 200, resize: 'vertical' }}
            value={bulkText}
            onChange={(e) => setBulkText(e.target.value)}
            placeholder={'Иванова Анна Сергеевна, 12.05.2013\nПетров Пётр Петрович, 03.09.2013\nСидорова Мария Ивановна'}
          />
        </div>
      </Modal>

      {/* ---------------- редактор общего шаблона ---------------- */}
      <Modal
        open={tplOpen}
        onClose={() => setTplOpen(false)}
        wide
        title="Печатная форма приказов (общий шаблон)"
        footer={
          <>
            <button className="btn" onClick={() => void saveTemplate(null)} disabled={busy}>↺ Сбросить к стандарту</button>
            <button className="btn" onClick={() => setTplOpen(false)} disabled={busy}>Отмена</button>
            <button className="btn btn--primary" onClick={() => void saveTemplate(tplText)} disabled={busy}>
              {busy ? 'Сохраняем…' : 'Сохранить для всех приказов'}
            </button>
          </>
        }
      >
        <div className="stack" style={{ gap: 10 }}>
          <div className="alert alert--info small">
            Шаблон ОДИН для всех приказов раздела. Плейсхолдеры: {'{PREAMBLE}'} — «На основании заявления…»,
            {'{BODY}'} — пункты приказа (один ребёнок — абзац, несколько — таблица), {'{CONTROL}'} — пункт
            о контроле, {'{NOTE}'} — примечание, {'{GUARDIAN}'}, {'{NUMBER}'}, {'{DATE}'}, {'{TITLE}'},
            {'{SIGNATORY}'}, {'{SCHOOL}'}. Строки с «|» печатаются таблицей. Шапка («ПРИКАЗ», дата, №)
            и подпись подставляются автоматически.
          </div>
          <textarea
            className="input mono"
            style={{ minHeight: 260, resize: 'vertical' }}
            value={tplText}
            onChange={(e) => setTplText(e.target.value)}
          />
        </div>
      </Modal>

      {/* ---------------- сводка ---------------- */}
      <Modal
        open={summaryOpen}
        onClose={() => setSummaryOpen(false)}
        wide
        title="Сводка по отмеченным приказам"
        footer={
          <>
            <button className="btn" onClick={copySummary}>📋 Скопировать</button>
            <button className="btn" onClick={() => void downloadAuthorized(movementApi.exportCsv(checked), 'movement-summary.csv')}>⬇ CSV</button>
            <button className="btn" onClick={() => { setSummaryOpen(false); setSummaryPrint(true) }}>🖨 Печать</button>
            <button className="btn btn--primary" onClick={() => setSummaryOpen(false)}>Закрыть</button>
          </>
        }
      >
        <table className="table">
          <thead>
            <tr>
              <th>Тип</th><th>№ / дата</th><th>Обучающийся</th><th>Дата рождения</th><th>Откуда</th><th>Куда</th><th>Представитель</th><th>Статус</th>
            </tr>
          </thead>
          <tbody>
            {checkedRows.flatMap((r) =>
              r.children.map((k) => (
                <tr key={`${r.id}-${k.student}`}>
                  <td className="small">{MOVEMENT_TYPE_LABEL[r.order_type]}</td>
                  <td className="small nowrap">№ {r.order_number}<div className="tiny muted">{formatDate(r.order_date)}</div></td>
                  <td className="small">{k.student}</td>
                  <td className="small nowrap">{k.birth_date ? formatDate(k.birth_date) : '—'}</td>
                  <td className="small">{k.from_place || r.from_place || '—'}</td>
                  <td className="small">{k.to_place || r.to_place || '—'}</td>
                  <td className="small">{r.guardian || '—'}</td>
                  <td><Badge tone={STATUS_TONE[r.status]}>{MOVEMENT_STATUS_LABEL[r.status]}</Badge></td>
                </tr>
              )),
            )}
          </tbody>
        </table>
      </Modal>

      {/* ---------------- печатные листы ---------------- */}
      {createPortal(
        <>
          {printRow && (
            <div className="print-sheet movement-print">
              <div className="mv-header">{(schoolInfo?.school_name ?? '').toUpperCase() || 'НАЗВАНИЕ ШКОЛЫ НЕ ЗАДАНО'}</div>
              <div style={{ textAlign: 'center', fontWeight: 700, margin: '26px 0 18px' }}>ПРИКАЗ</div>
              <div className="mv-row">
                <span>{ruDate(printRow.order_date)}</span>
                <span>№ {printRow.order_number}</span>
              </div>
              <div style={{ textAlign: 'center', fontWeight: 700, margin: '10px 0 14px' }}>
                {TYPE_TITLE[printRow.order_type]}
              </div>
              <BodyBlocks text={printDocument(printRow)} />
              <div className="mv-sign">
                <span>Директор школы</span>
                <span className="mv-line" />
                <span>{printRow.signatory ?? ''}</span>
              </div>
            </div>
          )}
          {summaryPrint && (
            <div className="print-sheet">
              <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>Сводка приказов по движению обучающихся</h2>
              <div style={{ marginBottom: 8 }}>
                {schoolInfo?.school_name ?? ''} · сформировано {new Date().toLocaleString('ru-RU')} · приказов: {checkedRows.length}
              </div>
              <table className="print-sheet__list">
                <thead>
                  <tr>
                    <th style={{ width: 70 }}>Тип</th>
                    <th style={{ width: 90 }}>№ приказа</th>
                    <th style={{ width: 80 }}>Дата</th>
                    <th>Обучающийся</th>
                    <th style={{ width: 80 }}>Дата рождения</th>
                    <th style={{ width: 120 }}>Представитель</th>
                    <th style={{ width: 100 }}>Откуда</th>
                    <th style={{ width: 100 }}>Куда</th>
                    <th style={{ width: 70 }}>Статус</th>
                  </tr>
                </thead>
                <tbody>
                  {checkedRows.flatMap((r) =>
                    r.children.map((k) => (
                      <tr key={`${r.id}-${k.student}`}>
                        <td>{MOVEMENT_TYPE_LABEL[r.order_type]}</td>
                        <td>{r.order_number}</td>
                        <td>{ruDate(r.order_date)}</td>
                        <td>{k.student}</td>
                        <td>{k.birth_date ? ruDate(k.birth_date) : ''}</td>
                        <td>{r.guardian ?? ''}</td>
                        <td>{k.from_place || r.from_place || ''}</td>
                        <td>{k.to_place || r.to_place || ''}</td>
                        <td>{MOVEMENT_STATUS_LABEL[r.status]}</td>
                      </tr>
                    )),
                  )}
                </tbody>
              </table>
            </div>
          )}
        </>,
        document.body,
      )}
    </div>
  )
}
