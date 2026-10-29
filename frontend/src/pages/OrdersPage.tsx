/** Реестр приказов: фильтры (тип, статус, период, поиск, «мои неподписанные»), пагинация, RBAC-действия. */
import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { metaApi, ordersApi, type OrderFilters } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { OrderFormModal } from '@/components/OrderFormModal'
import {
  AckBadge,
  EmptyState,
  ErrorState,
  OrderStatusBadge,
  OrderTypeBadge,
  Pagination,
  Progress,
  Spinner,
} from '@/components/ui'
import { CAP, can } from '@/lib/permissions'
import { formatDate, toDateInput } from '@/lib/format'
import { useOrderTypes } from '@/lib/orderTypes'
import { usePinnedView } from '@/lib/pinnedView'
import { useAuthStore } from '@/store/auth'
import type { OrderStatus, OrderType } from '@/types'

const PER_PAGE = 15

export function OrdersPage() {
  const { list: TYPE_LIST } = useOrderTypes()
  const [typesOpen, setTypesOpen] = useState(false)
  const user = useAuthStore((s) => s.user)!
  const canManage = can(user.capabilities, CAP.ordersManage)
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()

  const [search, setSearch] = useState(searchParams.get('search') ?? '')
  const [debounced, setDebounced] = useState(search)
  const [type, setType] = useState<OrderType | ''>((searchParams.get('order_type') as OrderType) ?? '')
  const [status, setStatus] = useState<OrderStatus | ''>((searchParams.get('status') as OrderStatus) ?? '')
  const [dateFrom, setDateFrom] = useState(searchParams.get('date_from') ?? '')
  const [dateTo, setDateTo] = useState(searchParams.get('date_to') ?? '')
  const [minePending, setMinePending] = useState(searchParams.get('mine_pending') === 'true')
  const [ackFilter, setAckFilter] = useState<'all' | 'pending' | 'acknowledged' | ''>('')
  const [page, setPage] = useState(1)
  const [formOpen, setFormOpen] = useState(false)

  // закреплённый вид списка: «Запомнить этот вид» сохраняет фильтры в профиле
  const pin = usePinnedView(
    'orders',
    () => ({
      ...(type ? { order_type: type } : {}),
      ...(status ? { status } : {}),
      ...(dateFrom ? { date_from: dateFrom } : {}),
      ...(dateTo ? { date_to: dateTo } : {}),
      ...(minePending ? { mine_pending: 'true' } : {}),
      ...(ackFilter ? { ack_filter: ackFilter } : {}),
    }),
    (p) => {
      setType((p.order_type as OrderType | '') ?? '')
      setStatus((p.status as OrderStatus | '') ?? '')
      setDateFrom(p.date_from ?? '')
      setDateTo(p.date_to ?? '')
      setMinePending(p.mine_pending === 'true')
      setAckFilter((p.ack_filter as 'all' | 'pending' | 'acknowledged' | '') ?? '')
      setPage(1)
    },
  )

  // debounce поиска, чтобы не дёргать API на каждый символ
  useEffect(() => {
    const id = window.setTimeout(() => {
      setDebounced(search)
      setPage(1)
    }, 350)
    return () => window.clearTimeout(id)
  }, [search])

  const filters = useMemo<OrderFilters>(
    () => ({
      search: debounced || undefined,
      order_type: type || undefined,
      status: canManage && status ? status : undefined,
      date_from: dateFrom || undefined,
      date_to: dateTo || undefined,
      ack: ackFilter || undefined,
      mine_pending: minePending || undefined,
      page,
      per_page: PER_PAGE,
    }),
    [debounced, type, status, canManage, dateFrom, dateTo, ackFilter, minePending, page],
  )

  const { data, isLoading, error, refetch, isFetching } = useQuery({
    queryKey: qk('/orders', filters as never),
    queryFn: () => ordersApi.list(filters),
  })

  // синхронизируем фильтры с URL (можно делиться ссылкой на выборку)
  useEffect(() => {
    const next: Record<string, string> = {}
    if (debounced) next.search = debounced
    if (type) next.order_type = type
    if (status && canManage) next.status = status
    if (dateFrom) next.date_from = dateFrom
    if (dateTo) next.date_to = dateTo
    if (minePending) next.mine_pending = 'true'
    if (page > 1) next.page = String(page)
    setSearchParams(next, { replace: true })
  }, [debounced, type, status, canManage, dateFrom, dateTo, minePending, page, setSearchParams])

  const resetFilters = () => {
    setSearch('')
    setDebounced('')
    setType('')
    setStatus('')
    setDateFrom('')
    setDateTo('')
    setAckFilter('')
    setMinePending(false)
    setPage(1)
  }

  const hasFilters = Boolean(debounced || type || status || dateFrom || dateTo || ackFilter || minePending)

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <h1>Приказы</h1>
          <div className="page-head__hint">
            {canManage
              ? 'Загружайте PDF/DOCX, публикуйте и отслеживайте ознакомление сотрудников.'
              : 'Опубликованные приказы школы. Откройте документ и подтвердите ознакомление.'}
          </div>
        </div>
        <div className="page-head__actions">
          {canManage && (
            <button className="btn" onClick={() => setTypesOpen(true)} title="Справочник типов приказов">
              ⚙ Типы приказов
            </button>
          )}
          {canManage && (
            <button className="btn btn--primary" onClick={() => setFormOpen(true)}>
              + Новый приказ
            </button>
          )}
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
                placeholder="Номер, заголовок или текст описания…"
              />
            </label>

            <label className="field">
              <span className="field__label">Тип</span>
              <select className="select" value={type} onChange={(e) => { setType(e.target.value as OrderType | ''); setPage(1) }}>
                <option value="">Все типы</option>
                {TYPE_LIST.map((t) => (
                  <option key={t.code} value={t.code}>{t.title}</option>
                ))}
              </select>
            </label>

            {canManage && (
              <label className="field">
                <span className="field__label">Статус</span>
                <select className="select" value={status} onChange={(e) => { setStatus(e.target.value as OrderStatus | ''); setPage(1) }}>
                  <option value="">Все статусы</option>
                  <option value="DRAFT">Черновики</option>
                  <option value="PUBLISHED">Опубликованные</option>
                  <option value="ARCHIVED">Архив</option>
                </select>
              </label>
            )}

            <label className="field">
              <span className="field__label">Дата с</span>
              <input className="input" type="date" value={dateFrom} onChange={(e) => { setDateFrom(e.target.value); setPage(1) }} />
            </label>
            <label className="field">
              <span className="field__label">Дата по</span>
              <input className="input" type="date" value={dateTo} onChange={(e) => { setDateTo(e.target.value); setPage(1) }} />
            </label>

            {canManage && (
              <label className="field">
                <span className="field__label">Ознакомление</span>
                <select className="select" value={ackFilter} onChange={(e) => { setAckFilter(e.target.value as never); setPage(1) }}>
                  <option value="">Любое</option>
                  <option value="pending">Есть неподписанные</option>
                  <option value="acknowledged">Все подписали</option>
                </select>
              </label>
            )}
          </div>

          <div className="row" style={{ marginTop: 12 }}>
            <button
              className={minePending ? 'chip chip--active' : 'chip'}
              onClick={() => { setMinePending((v) => !v); setPage(1) }}
            >
              ⏳ Ждут моей подписи
            </button>
            <button className="chip" onClick={() => { const d = new Date(); setDateFrom(toDateInput(new Date(d.getFullYear(), d.getMonth(), 1))); setDateTo(toDateInput(d)); setPage(1) }}>
              📅 Этот месяц
            </button>
            {hasFilters && (
              <button className="btn btn--sm btn--ghost" onClick={resetFilters}>
                ✕ Сбросить фильтры
              </button>
            )}
            <button
              className="btn btn--sm btn--ghost"
              title="Сохранить текущие фильтры в профиле: список откроется с ними при следующем входе"
              onClick={() => void pin.save()}
            >
              📌 Запомнить этот вид
            </button>
            {pin.isPinned && (
              <button className="btn btn--sm btn--ghost" title="Открепить сохранённый вид" onClick={() => void pin.reset()}>
                ✖ Открепить
              </button>
            )}
            <span className="grow" />
            {isFetching && !isLoading && <span className="tiny muted">обновляем…</span>}
            {data && <span className="tiny muted">найдено: {data.total}</span>}
          </div>
        </div>
      </section>

      <section className="card">
        {isLoading ? (
          <Spinner label="Загружаем реестр приказов…" />
        ) : error ? (
          <ErrorState error={error} onRetry={() => void refetch()} />
        ) : !data || data.items.length === 0 ? (
          <EmptyState
            icon="📜"
            title={hasFilters ? 'Ничего не найдено' : 'Приказов пока нет'}
            hint={hasFilters ? 'Попробуйте изменить условия фильтрации' : canManage ? 'Создайте первый приказ — он появится здесь' : 'Как только администрация опубликует приказ, вы увидите его здесь'}
            action={hasFilters ? <button className="btn btn--sm" onClick={resetFilters}>Сбросить фильтры</button> : undefined}
          />
        ) : (
          <>
            <table className="table table--clickable">
              <thead>
                <tr>
                  <th style={{ width: 110 }}>Номер</th>
                  <th>Наименование</th>
                  <th style={{ width: 190 }}>Тип</th>
                  <th style={{ width: 110 }}>Дата</th>
                  {canManage && <th style={{ width: 130 }}>Статус</th>}
                  <th style={{ width: 190 }}>Ознакомление</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((o) => {
                  const progress = o.ack_total ? (o.ack_done / o.ack_total) * 100 : 0
                  return (
                    <tr key={o.id} onClick={() => navigate(`/orders/${o.id}`)}>
                      <td className="nowrap">
                        <strong>№{o.number}</strong>
                        <div className="tiny muted">{o.order_year}</div>
                      </td>
                      <td>
                        <div className="strong">{o.title}</div>
                        {o.ack_deadline && (
                          <div className="tiny muted">ознакомиться до {formatDate(o.ack_deadline)}</div>
                        )}
                      </td>
                      <td><OrderTypeBadge type={o.order_type} /></td>
                      <td className="nowrap">{formatDate(o.issued_on)}</td>
                      {canManage && <td><OrderStatusBadge status={o.status} /></td>}
                      <td onClick={(e) => e.stopPropagation()}>
                        <div className="stack" style={{ gap: 5 }}>
                          {canManage ? (
                            <>
                              <div className="row" style={{ gap: 8 }}>
                                <span className="tiny muted nowrap">{o.ack_done}/{o.ack_total}</span>
                                <div className="grow"><Progress value={progress} tone={progress === 100 ? 'ok' : progress >= 50 ? undefined : 'warn'} /></div>
                              </div>
                              <Link className="tiny" to={`/orders/${o.id}`}>лист ознакомления →</Link>
                            </>
                          ) : (
                            <>
                              <AckBadge status={o.my_ack?.status} />
                              {!o.my_ack && o.status === 'PUBLISHED' && (
                                <span className="tiny muted">вы не в списке ознакомления</span>
                              )}
                            </>
                          )}
                        </div>
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            <Pagination page={data.page} pages={data.pages} total={data.total} perPage={data.per_page} onPage={setPage} />
          </>
        )}
      </section>

      <TypesModal open={typesOpen} onClose={() => setTypesOpen(false)} />

      <OrderFormModal
        open={formOpen}
        onClose={() => setFormOpen(false)}
        onSaved={(order) => { void refetch(); navigate(`/orders/${order.id}`) }}
      />
    </div>
  )
}


/** Управление справочником типов приказов (ADMIN/OWNER). */
function TypesModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const toast = useToast()
  const qc = useQueryClient()
  const { list, labels } = useOrderTypes()
  const [title, setTitle] = useState('')
  const [busy, setBusy] = useState(false)

  const refresh = () => void qc.invalidateQueries({ queryKey: qk('/meta/order-types') })

  async function add() {
    const value = title.trim()
    if (!value) return
    setBusy(true)
    try {
      await metaApi.addOrderType(value)
      setTitle('')
      refresh()
      toast.success(`Тип «${value}» добавлен`)
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  async function remove(code: string) {
    if (!window.confirm(`Удалить тип «${labels[code] ?? code}»? Нельзя, если он используется в приказах.`)) return
    try {
      await metaApi.removeOrderType(code)
      refresh()
      toast.success('Тип удалён')
    } catch (err) {
      toast.error(err)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Типы приказов"
      footer={<button className="btn btn--primary" onClick={onClose}>Готово</button>}
    >
      <div className="stack" style={{ gap: 10 }}>
        <div className="stack" style={{ gap: 6 }}>
          {list.map((t) => (
            <div className="row small" key={t.code} style={{ gap: 8, padding: '6px 8px', background: 'var(--surface-2)', borderRadius: 8 }}>
              <span className="mono tiny muted">{t.code}</span>
              <span className="grow">{t.title}</span>
              <button className="btn btn--sm btn--danger" onClick={() => void remove(t.code)}>🗑</button>
            </div>
          ))}
        </div>
        <div className="row" style={{ gap: 8 }}>
          <input
            className="input field--grow"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Новый тип: например, «По питанию»"
          />
          <button className="btn btn--primary" disabled={busy || !title.trim()} onClick={() => void add()}>
            ＋ Добавить
          </button>
        </div>
        <div className="tiny muted">
          Типы используются в фильтрах, формах и бейджах приказов. Удалить занятый тип нельзя —
          сначала переведите приказы на другой тип.
        </div>
      </div>
    </Modal>
  )
}
