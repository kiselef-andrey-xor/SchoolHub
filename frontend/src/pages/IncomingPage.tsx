/** Журнал входящих писем: авто-записи из задач с входящим номером + ручные.
 *  Доступ: Владелец + сотрудники с персональным грантом (галочка в карточке).
 *  Экспорт CSV и печатная форма — как в листе ознакомления. */
import { useEffect, useMemo, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { incomingApi, usersApi } from '@/api/endpoints'
import { downloadAuthorized } from '@/api/client'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Badge, EmptyState, ErrorState, Pagination, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDate, shortName } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import type { IncomingLetter } from '@/types'

const PER_PAGE = 25

interface FormState {
  id?: string
  reg_no: string
  incoming_number: string
  incoming_date: string
  sent_date: string
  addressee: string
  subject: string
  note: string
  responsible_id: string
}

const emptyForm = (): FormState => ({
  reg_no: '',
  incoming_number: '',
  incoming_date: new Date().toISOString().slice(0, 10),
  sent_date: '',
  addressee: '',
  subject: '',
  note: '',
  responsible_id: '',
})

export function IncomingPage() {
  const me = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()
  const navigate = useNavigate()

  const [searchParams] = useSearchParams()
  // ?search=… — переход из глобального поиска (Ctrl+K)
  const [search, setSearch] = useState(() => searchParams.get('search') ?? '')
  const [debounced, setDebounced] = useState(() => searchParams.get('search') ?? '')
  const [year, setYear] = useState<number | ''>('')
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    const id = window.setTimeout(() => { setDebounced(search); setPage(1) }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  // пока открыт журнал, печать (Ctrl+P / кнопка) отдаёт печатную форму журнала
  useEffect(() => {
    document.body.classList.add('print-sheet-active')
    return () => document.body.classList.remove('print-sheet-active')
  }, [])

  const params = useMemo(
    () => ({ search: debounced || undefined, year: year || undefined, page, per_page: PER_PAGE }),
    [debounced, year, page],
  )

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/incoming', params as never),
    queryFn: () => incomingApi.list(params),
  })

  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
  })

  // печатная форма — ВЕСЬ журнал в текущем фильтре (а не только страница списка)
  const printParams = useMemo(
    () => ({ search: debounced || undefined, year: year || undefined }),
    [debounced, year],
  )
  const { data: printRows = [] } = useQuery({
    queryKey: qk('/incoming/print', printParams as never),
    queryFn: async () => {
      const out: IncomingLetter[] = []
      for (let p = 1; p <= 20; p += 1) {
        const chunk = await incomingApi.list({ ...printParams, page: p, per_page: 200 })
        out.push(...chunk.items)
        if (out.length >= chunk.total || chunk.items.length === 0) break
      }
      return out.sort((a, b) => a.reg_year - b.reg_year || a.reg_no - b.reg_no)
    },
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/incoming'] })
  }

  const years = useMemo(() => {
    const now = new Date().getFullYear()
    return Array.from({ length: 6 }, (_, i) => now - i)
  }, [])

  async function submit() {
    if (!form) return
    setBusy(true)
    try {
      const payload = {
        reg_no: form.id && form.reg_no ? Number(form.reg_no) : undefined,
        incoming_number: form.incoming_number.trim(),
        incoming_date: form.incoming_date || null,
        sent_date: form.sent_date || null,
        addressee: form.addressee.trim() || null,
        subject: form.subject.trim(),
        note: form.note.trim() || null,
        responsible_id: form.responsible_id || null,
      }
      if (form.id) {
        await incomingApi.update(form.id, payload)
        toast.success('Запись журнала обновлена')
      } else {
        await incomingApi.create(payload)
        toast.success('Запись добавлена в журнал')
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

  async function removeRow(row: IncomingLetter) {
    if (!window.confirm(`Удалить запись ${row.reg_no}/${row.reg_year} «${row.incoming_number}» из журнала?`)) return
    try {
      await incomingApi.remove(row.id)
      toast.success('Запись удалена')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  const rows = data?.items ?? []

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Журнал входящих писем</h1>
          <div className="page-head__hint">
            Записи создаются автоматически из задач с входящим номером и вручную.
            Регистрация — по году поступления.
          </div>
        </div>
        <div className="page-head__actions">
          <button
            className="btn"
            onClick={() => void downloadAuthorized(incomingApi.exportCsv(year || undefined), 'incoming-journal.csv')}
            title="Экспорт журнала в CSV (Excel)"
          >
            ⬇ CSV
          </button>
          <button className="btn" onClick={() => window.print()} title="Печать журнала">🖨 Печать</button>
          <button className="btn btn--primary" onClick={() => setForm(emptyForm())}>＋ Запись</button>
        </div>
      </div>

      <section className="card">
        <div className="card__body">
          <div className="filters">
            <label className="field field--grow">
              <span className="field__label">Поиск</span>
              <input
                className="input"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="№ входящего, тема, адресат, примечание…"
              />
            </label>
            <label className="field">
              <span className="field__label">Год</span>
              <select className="select" value={year} onChange={(e) => { setYear(e.target.value ? Number(e.target.value) : ''); setPage(1) }}>
                <option value="">Все годы</option>
                {years.map((y) => (<option key={y} value={y}>{y}</option>))}
              </select>
            </label>
          </div>
        </div>
      </section>

      <section className="card">
        {isLoading ? (
          <Spinner label="Открываем журнал…" />
        ) : error ? (
          <ErrorState error={error} onRetry={() => void refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState icon="✉️" title="Записей нет" hint="Добавьте запись вручную или укажите входящий номер в задаче" />
        ) : (
          <>
            <div style={{ overflowX: 'auto' }}>
              <table className="table">
                <thead>
                  <tr>
                    <th style={{ width: 110 }}>№ регистрации</th>
                    <th style={{ width: 120 }}>Дата входящего</th>
                    <th style={{ width: 130 }}>№ входящего</th>
                    <th style={{ width: 120 }}>Дата отправки</th>
                    <th style={{ width: 160 }}>Адресат</th>
                    <th>Тема письма</th>
                    <th style={{ width: 160 }}>Примечание</th>
                    <th style={{ width: 160 }}>Ответственный</th>
                    <th style={{ width: 90 }} />
                  </tr>
                </thead>
                <tbody>
                  {rows.map((r) => (
                    <tr key={r.id}>
                      <td className="mono">{r.reg_no}/{r.reg_year}</td>
                      <td className="small nowrap">{formatDate(r.incoming_date)}</td>
                      <td className="mono small">{r.incoming_number}</td>
                      <td className="small nowrap">{r.sent_date ? formatDate(r.sent_date) : '—'}</td>
                      <td className="small">{r.addressee ?? '—'}</td>
                      <td className="small">
                        {r.task_id ? (
                          <a href={`#/tasks/${r.task_id}`} onClick={(e) => { e.preventDefault(); navigate(`/tasks/${r.task_id}`) }}>
                            {r.subject}
                          </a>
                        ) : (
                          r.subject
                        )}
                        {!r.task_id && <Badge tone="muted">вручную</Badge>}
                      </td>
                      <td className="small">{r.note ?? '—'}</td>
                      <td className="small">{r.responsible ? shortName(r.responsible.full_name) : '—'}</td>
                      <td>
                        <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                          <button
                            className="btn btn--sm"
                            onClick={() =>
                              setForm({
                                id: r.id,
                                reg_no: String(r.reg_no),
                                incoming_number: r.incoming_number,
                                incoming_date: r.incoming_date,
                                sent_date: r.sent_date ?? '',
                                addressee: r.addressee ?? '',
                                subject: r.subject,
                                note: r.note ?? '',
                                responsible_id: r.responsible?.id ?? '',
                              })
                            }
                          >
                            ✎
                          </button>
                          {me.role === 'OWNER' && (
                            <button className="btn btn--sm btn--danger" onClick={() => void removeRow(r)}>🗑</button>
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

      <Modal
        open={form !== null}
        onClose={() => setForm(null)}
        title={form?.id ? 'Запись журнала входящих' : 'Новая запись журнала'}
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
          <div className="form-grid">
            <label className="field">
              <span className="field__label">№ регистрации (вручную)</span>
              <input
                className="input"
                type="number"
                min={1}
                value={form.reg_no}
                onChange={(e) => setForm({ ...form, reg_no: e.target.value })}
                placeholder={form.id ? '' : 'авто по году'}
                disabled={!form.id}
              />
              <span className="field__hint">{form.id ? 'В пределах года записи' : 'Присваивается автоматически'}</span>
            </label>
            <label className="field">
              <span className="field__label">№ входящего *</span>
              <input className="input" required value={form.incoming_number} onChange={(e) => setForm({ ...form, incoming_number: e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Дата входящего</span>
              <input className="input" type="date" value={form.incoming_date} onChange={(e) => setForm({ ...form, incoming_date: e.target.value })} />
              <span className="field__hint">№ регистрации присваивается по году этой даты</span>
            </label>
            <label className="field">
              <span className="field__label">Дата отправки (вручную)</span>
              <input className="input" type="date" value={form.sent_date} onChange={(e) => setForm({ ...form, sent_date: e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Адресат</span>
              <input className="input" value={form.addressee} onChange={(e) => setForm({ ...form, addressee: e.target.value })} placeholder="Директору, зам. директора…" />
            </label>
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              <span className="field__label">Тема письма *</span>
              <input className="input" required value={form.subject} onChange={(e) => setForm({ ...form, subject: e.target.value })} />
            </label>
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              <span className="field__label">Примечание</span>
              <input className="input" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Ответственный</span>
              <select className="select" value={form.responsible_id} onChange={(e) => setForm({ ...form, responsible_id: e.target.value })}>
                <option value="">— не назначен —</option>
                {people.map((u) => (<option key={u.id} value={u.id}>{u.full_name}</option>))}
              </select>
            </label>
          </div>
        )}
      </Modal>

      {createPortal(
        <div className="print-sheet">
          <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>Журнал входящих писем</h2>
          <div style={{ marginBottom: 8 }}>
            {year ? `за ${year} год` : 'за весь период'} · записей: {printRows.length}
          </div>
          <table className="print-sheet__list">
            <thead>
              <tr>
                <th style={{ width: 70 }}>№ рег.</th>
                <th style={{ width: 80 }}>Дата входящего</th>
                <th style={{ width: 90 }}>№ входящего</th>
                <th style={{ width: 80 }}>Дата отправки</th>
                <th style={{ width: 110 }}>Адресат</th>
                <th>Тема письма</th>
                <th style={{ width: 110 }}>Примечание</th>
                <th style={{ width: 110 }}>Ответственный</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.id}>
                  <td>{r.reg_no}/{r.reg_year}</td>
                  <td>{formatDate(r.incoming_date)}</td>
                  <td>{r.incoming_number}</td>
                  <td>{r.sent_date ? formatDate(r.sent_date) : ''}</td>
                  <td>{r.addressee ?? ''}</td>
                  <td>{r.subject}</td>
                  <td>{r.note ?? ''}</td>
                  <td>{r.responsible ? shortName(r.responsible.full_name) : ''}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ marginTop: 12, fontSize: 11 }}>
            Журнал сформирован {new Date().toLocaleString('ru-RU')} · SchoolHub
          </div>
        </div>,
        document.body,
      )}
    </div>
  )
}
