/** Учёт оборудования и инвентаря: наименование, инвентарный и заводской номера,
 *  количество, местонахождение. Доступ: Владелец + грант от Владельца. */
import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { equipmentApi } from '@/api/endpoints'
import { downloadAuthorized } from '@/api/client'
import { qk } from '@/api/query'
import { LabelModal } from '@/components/LabelModal'
import { Modal } from '@/components/Modal'
import { ScanModal } from '@/components/ScanModal'
import { EmptyState, ErrorState, Pagination, Spinner } from '@/components/ui'
import { useToast } from '@/components/Toast'
import { formatDate } from '@/lib/format'
import { useAuthStore } from '@/store/auth'
import { useSearchParams } from 'react-router-dom'
import type { EquipmentItem, EquipmentMove } from '@/types'

const PER_PAGE = 25

interface FormState {
  id?: string
  name: string
  inventory_number: string
  serial_number: string
  barcode: string
  quantity: string
  location: string
  note: string
}

const emptyForm = (): FormState => ({
  name: '',
  inventory_number: '',
  serial_number: '',
  barcode: '',
  quantity: '1',
  location: '',
  note: '',
})

export function EquipmentPage() {
  const me = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()

  const [searchParams] = useSearchParams()
  const [search, setSearch] = useState(() => searchParams.get('search') ?? '')
  const [debounced, setDebounced] = useState(() => searchParams.get('search') ?? '')
  const [page, setPage] = useState(1)
  const [form, setForm] = useState<FormState | null>(null)
  const [busy, setBusy] = useState(false)
  const [moveFor, setMoveFor] = useState<EquipmentItem | null>(null)
  const [moveForm, setMoveForm] = useState({ to_location: '', move_date: '', quantity: '1', note: '' })
  const [month, setMonth] = useState(() => new Date().toISOString().slice(0, 7))
  const [movesPrint, setMovesPrint] = useState(false)
  const [scanOpen, setScanOpen] = useState(false)
  const [labelFor, setLabelFor] = useState<EquipmentItem | null>(null)
  const [importResult, setImportResult] = useState<{
    created: number
    updated: number
    total: number
    failed: { row: number; reason: string }[]
  } | null>(null)
  const importRef = useRef<HTMLInputElement>(null)

  useEffect(() => {
    const id = window.setTimeout(() => { setDebounced(search); setPage(1) }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  const params = useMemo(() => ({ search: debounced || undefined, page, per_page: PER_PAGE }), [debounced, page])

  const { data, isLoading, error, refetch } = useQuery({
    queryKey: qk('/equipment', params as never),
    queryFn: () => equipmentApi.list(params),
  })

  const invalidate = () => void qc.invalidateQueries({ queryKey: ['/equipment'] })

  const { data: moves = [] } = useQuery({
    queryKey: qk('/equipment/moves', { month }),
    queryFn: () => equipmentApi.moves(month),
  })

  // печать отчёта по перемещениям
  useEffect(() => {
    if (!movesPrint) return
    document.body.classList.add('print-sheet-active')
    const t = window.setTimeout(() => window.print(), 250)
    const after = () => setMovesPrint(false)
    window.addEventListener('afterprint', after)
    return () => {
      window.clearTimeout(t)
      window.removeEventListener('afterprint', after)
      document.body.classList.remove('print-sheet-active')
    }
  }, [movesPrint])

  const months = useMemo(() => {
    const list: string[] = []
    const d = new Date()
    const pad = (n: number) => String(n).padStart(2, '0')
    for (let i = 0; i < 24; i += 1) {
      list.push(`${d.getFullYear()}-${pad(d.getMonth() + 1)}`)
      d.setMonth(d.getMonth() - 1)
    }
    return list
  }, [])

  async function submitMove() {
    if (!moveFor) return
    setBusy(true)
    try {
      await equipmentApi.moveItem(moveFor.id, {
        to_location: moveForm.to_location.trim(),
        move_date: moveForm.move_date || null,
        quantity: Number(moveForm.quantity || 1),
        note: moveForm.note.trim() || null,
      })
      toast.success(`Перемещено: ${moveFor.name} → ${moveForm.to_location.trim()}`)
      setMoveFor(null)
      invalidate()
      void refetch()
      void qc.invalidateQueries({ queryKey: ['/equipment/moves'] })
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function onImportFile(f: File) {
    setBusy(true)
    try {
      const res = await equipmentApi.importCsv(f)
      setImportResult(res)
      invalidate()
      void refetch()
      if (res.created || res.updated) toast.success(`Импорт: создано ${res.created}, обновлено ${res.updated}`)
      if (res.failed.length) toast.error(`Строк с ошибками: ${res.failed.length}`)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function submit() {
    if (!form) return
    setBusy(true)
    try {
      const payload = {
        name: form.name.trim(),
        inventory_number: form.inventory_number.trim() || null,
        serial_number: form.serial_number.trim() || null,
        barcode: form.barcode.trim() || null,
        quantity: Number(form.quantity || 1),
        location: form.location.trim() || null,
        note: form.note.trim() || null,
      }
      if (form.id) {
        await equipmentApi.update(form.id, payload)
        toast.success('Позиция обновлена')
      } else {
        await equipmentApi.create(payload)
        toast.success('Позиция добавлена')
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

  async function removeRow(row: EquipmentItem) {
    if (!window.confirm(`Удалить позицию «${row.name}» из учёта?`)) return
    try {
      await equipmentApi.remove(row.id)
      toast.success('Позиция удалена')
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  /** Сканирование (камера / аппаратный сканер): ищем позицию по коду и показываем её. */
  async function onScanCode(code: string): Promise<boolean> {
    try {
      const item = await equipmentApi.scan(code)
      setScanOpen(false)
      setSearch(code)
      setPage(1)
      invalidate()
      toast.success(`Найдено: ${item.name}${item.location ? ` (${item.location})` : ''}`)
      return true
    } catch (err) {
      const status = (err as { status?: number })?.status
      if (status === 404) return false
      toast.error(err)
      return false
    }
  }

  const rows = data?.items ?? []

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Оборудование и инвентарь</h1>
          <div className="page-head__hint">
            Учёт школьного имущества: наименование, инвентарный и заводской номера,
            количество и местонахождение.
          </div>
        </div>
        <div className="page-head__actions">
          <input
            ref={importRef}
            type="file"
            accept=".csv,.txt,text/csv,text/plain"
            style={{ display: 'none' }}
            onChange={(e) => {
              const f = e.target.files?.[0]
              if (f) void onImportFile(f)
              e.target.value = ''
            }}
          />
          <button className="btn" disabled={busy} onClick={() => importRef.current?.click()} title="Импорт из CSV в формате экспорта">
            ⬆ Импорт CSV
          </button>
          <button className="btn" onClick={() => void downloadAuthorized('/equipment/export.csv', 'equipment.csv')}>
            ⬇ CSV
          </button>
          <button className="btn" onClick={() => setScanOpen(true)} title="Найти позицию по штрихкоду: камера или аппаратный сканер">
            📷 Сканировать
          </button>
          <button className="btn btn--primary" onClick={() => setForm(emptyForm())}>＋ Позиция</button>
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
                placeholder="Наименование, номер, штрихкод, кабинет… (сканер работает здесь как клавиатура)"
              />
            </label>
          </div>
        </div>
      </section>

      <section className="card">
        {isLoading ? (
          <Spinner label="Открываем учёт…" />
        ) : error ? (
          <ErrorState error={error} onRetry={() => void refetch()} />
        ) : rows.length === 0 ? (
          <EmptyState icon="🖥" title="Пока пусто" hint="Добавьте первую позицию кнопкой «＋ Позиция»" />
        ) : (
          <>
            <table className="table">
              <thead>
                <tr>
                  <th>Наименование</th>
                  <th style={{ width: 150 }}>Инв. номер</th>
                  <th style={{ width: 170 }}>Заводской (серийный) №</th>
                  <th style={{ width: 150 }}>Штрихкод</th>
                  <th style={{ width: 110 }}>Кол-во</th>
                  <th style={{ width: 200 }}>Местонахождение</th>
                  <th style={{ width: 90 }} />
                </tr>
              </thead>
              <tbody>
                {rows.map((r) => (
                  <tr key={r.id}>
                    <td>
                      <div className="small strong">{r.name}</div>
                      {r.note && <div className="tiny muted">{r.note}</div>}
                    </td>
                    <td className="mono small">{r.inventory_number ?? '—'}</td>
                    <td className="mono small">{r.serial_number ?? '—'}</td>
                    <td className="mono small">{r.barcode ?? '—'}</td>
                    <td className="small">{r.quantity}</td>
                    <td className="small">{r.location ?? '—'}</td>
                    <td>
                      <div className="row" style={{ gap: 6, justifyContent: 'flex-end' }}>
                        <button
                          className="btn btn--sm"
                          title="Этикетка со штрихкодом (печать)"
                          onClick={() => setLabelFor(r)}
                        >
                          🏷
                        </button>
                        <button
                          className="btn btn--sm"
                          title="Переместить (из кабинета в кабинет)"
                          onClick={() => {
                            setMoveFor(r)
                            setMoveForm({ to_location: '', move_date: new Date().toISOString().slice(0, 10), quantity: String(r.quantity), note: '' })
                          }}
                        >
                          🚚
                        </button>
                        <button
                          className="btn btn--sm"
                          onClick={() =>
                            setForm({
                              id: r.id,
                              name: r.name,
                              inventory_number: r.inventory_number ?? '',
                              serial_number: r.serial_number ?? '',
                              barcode: r.barcode ?? '',
                              quantity: String(r.quantity),
                              location: r.location ?? '',
                              note: r.note ?? '',
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
            <Pagination page={data!.page} pages={data!.pages} total={data!.total} perPage={data!.per_page} onPage={setPage} />
          </>
        )}
      </section>

      <section className="card">
        <header className="card__head">
          <h3>Перемещения за месяц</h3>
          <span className="grow" />
          <select className="select" style={{ width: 160 }} value={month} onChange={(e) => setMonth(e.target.value)}>
            {months.map((m) => (<option key={m} value={m}>{m}</option>))}
          </select>
          <button className="btn btn--sm" onClick={() => void downloadAuthorized(equipmentApi.movesCsv(month), `equipment-moves-${month}.csv`)}>⬇ CSV</button>
          <button className="btn btn--sm" onClick={() => setMovesPrint(true)} disabled={moves.length === 0}>🖨 Печать</button>
        </header>
        <div className="card__body">
          {moves.length === 0 ? (
            <div className="small muted">За {month} перемещений не было. Кнопка 🚚 в строке позиции оформляет перемещение.</div>
          ) : (
            <table className="table">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>Дата</th>
                  <th>Наименование</th>
                  <th style={{ width: 130 }}>Инв. номер</th>
                  <th style={{ width: 140 }}>Откуда</th>
                  <th style={{ width: 140 }}>Куда</th>
                  <th style={{ width: 80 }}>Кол-во</th>
                  <th style={{ width: 160 }}>Кто оформил</th>
                </tr>
              </thead>
              <tbody>
                {moves.map((m: EquipmentMove) => (
                  <tr key={m.id}>
                    <td className="small nowrap">{formatDate(m.move_date)}</td>
                    <td className="small">{m.item_name}{m.note && <div className="tiny muted">{m.note}</div>}</td>
                    <td className="mono small">{m.inventory_number ?? '—'}</td>
                    <td className="small">{m.from_location ?? '—'}</td>
                    <td className="small">{m.to_location}</td>
                    <td className="small">{m.quantity}</td>
                    <td className="small">{m.actor_name ?? '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <Modal
        open={moveFor !== null}
        onClose={() => setMoveFor(null)}
        title={`Перемещение: ${moveFor?.name ?? ''}`}
        footer={
          <>
            <button className="btn" onClick={() => setMoveFor(null)} disabled={busy}>Отмена</button>
            <button className="btn btn--primary" onClick={() => void submitMove()} disabled={busy}>Переместить</button>
          </>
        }
      >
        {moveFor && (
          <div className="form-grid">
            <label className="field">
              <span className="field__label">Текущее местонахождение</span>
              <input className="input" value={moveFor.location ?? '—'} disabled />
            </label>
            <label className="field">
              <span className="field__label">Куда *</span>
              <input className="input" required value={moveForm.to_location} onChange={(e) => setMoveForm({ ...moveForm, to_location: e.target.value })} placeholder="Каб. 321" />
            </label>
            <label className="field">
              <span className="field__label">Дата перемещения</span>
              <input className="input" type="date" value={moveForm.move_date} onChange={(e) => setMoveForm({ ...moveForm, move_date: e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Количество</span>
              <input className="input" type="number" min={1} value={moveForm.quantity} onChange={(e) => setMoveForm({ ...moveForm, quantity: e.target.value })} />
            </label>
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              <span className="field__label">Основание / примечание</span>
              <input className="input" value={moveForm.note} onChange={(e) => setMoveForm({ ...moveForm, note: e.target.value })} placeholder="Накладная №…, устная заявка завхоза" />
            </label>
          </div>
        )}
      </Modal>

      {movesPrint &&
        createPortal(
          <div className="print-sheet">
            <h2 style={{ margin: '0 0 4px', fontSize: 18 }}>Отчёт о перемещениях оборудования за {month}</h2>
            <div style={{ marginBottom: 8 }}>записей: {moves.length}</div>
            <table className="print-sheet__list">
              <thead>
                <tr>
                  <th style={{ width: 80 }}>Дата</th>
                  <th>Наименование</th>
                  <th style={{ width: 100 }}>Инв. номер</th>
                  <th style={{ width: 110 }}>Откуда</th>
                  <th style={{ width: 110 }}>Куда</th>
                  <th style={{ width: 60 }}>Кол-во</th>
                  <th style={{ width: 140 }}>Примечание</th>
                  <th style={{ width: 120 }}>Кто оформил</th>
                </tr>
              </thead>
              <tbody>
                {moves.map((m: EquipmentMove) => (
                  <tr key={m.id}>
                    <td>{formatDate(m.move_date)}</td>
                    <td>{m.item_name}</td>
                    <td>{m.inventory_number ?? ''}</td>
                    <td>{m.from_location ?? ''}</td>
                    <td>{m.to_location}</td>
                    <td>{m.quantity}</td>
                    <td>{m.note ?? ''}</td>
                    <td>{m.actor_name ?? ''}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>,
          document.body,
        )}

      <Modal
        open={importResult !== null}
        onClose={() => setImportResult(null)}
        title="Импорт инвентаря из CSV"
        footer={<button className="btn btn--primary" onClick={() => setImportResult(null)}>Готово</button>}
      >
        {importResult && (
          <div className="stack" style={{ gap: 10 }}>
            <div className="alert alert--info small">
              Строк в файле: <strong>{importResult.total}</strong> · создано:{' '}
              <strong>{importResult.created}</strong> · обновлено (по инв. номеру):{' '}
              <strong>{importResult.updated}</strong>
            </div>
            {importResult.failed.length > 0 && (
              <div className="alert alert--error small">
                <strong>Пропущены строки:</strong>
                <ul style={{ margin: '6px 0 0 18px', padding: 0 }}>
                  {importResult.failed.map((f) => (
                    <li key={f.row}>строка {f.row}: {f.reason}</li>
                  ))}
                </ul>
              </div>
            )}
            <div className="tiny muted">
              Формат — как у экспорта: колонки через «;» — Наименование, Инв. номер, Заводской №,
              Количество, Местонахождение, Примечание. Строка с уже существующим инвентарным
              номером обновляет позицию, а не создаёт дубль.
            </div>
          </div>
        )}
      </Modal>

      <Modal
        open={form !== null}
        onClose={() => setForm(null)}
        title={form?.id ? 'Позиция инвентаря' : 'Новая позиция'}
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
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              <span className="field__label">Наименование *</span>
              <input className="input" required value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="Ноутбук Lenovo ThinkPad E14" />
            </label>
            <label className="field">
              <span className="field__label">Инвентарный номер</span>
              <input className="input mono" value={form.inventory_number} onChange={(e) => setForm({ ...form, inventory_number: e.target.value })} placeholder="ИН-000123" />
            </label>
            <label className="field">
              <span className="field__label">Заводской (серийный) номер</span>
              <input className="input mono" value={form.serial_number} onChange={(e) => setForm({ ...form, serial_number: e.target.value })} placeholder="SN: L12345678" />
            </label>
            <label className="field">
              <span className="field__label">Штрихкод (EAN-13 / Code-128)</span>
              <input
                className="input mono"
                value={form.barcode}
                onChange={(e) => setForm({ ...form, barcode: e.target.value })}
                placeholder="2000000044323 — или сосканируйте"
              />
            </label>
            <label className="field">
              <span className="field__label">Количество *</span>
              <input className="input" type="number" min={0} value={form.quantity} onChange={(e) => setForm({ ...form, quantity: e.target.value })} />
            </label>
            <label className="field">
              <span className="field__label">Местонахождение</span>
              <input className="input" value={form.location} onChange={(e) => setForm({ ...form, location: e.target.value })} placeholder="Кабинет 405" />
            </label>
            <label className="field" style={{ gridColumn: '1 / -1' }}>
              <span className="field__label">Примечание</span>
              <input className="input" value={form.note} onChange={(e) => setForm({ ...form, note: e.target.value })} placeholder="Ответственный: учитель физики" />
            </label>
          </div>
        )}
      </Modal>

      <ScanModal open={scanOpen} onClose={() => setScanOpen(false)} onCode={onScanCode} />
      <LabelModal open={labelFor !== null} item={labelFor} onClose={() => setLabelFor(null)} />
    </div>
  )
}
