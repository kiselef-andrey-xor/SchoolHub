/** Форма приказа: создание и редактирование (OWNER/ADMIN). */
import { useEffect, useState, type FormEvent } from 'react'
import { useQuery } from '@tanstack/react-query'

import { ordersApi, usersApi, type OrderPayload } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { UserMultiSelect } from '@/components/UserMultiSelect'
import { useToast } from '@/components/Toast'
import { toDateInput } from '@/lib/format'
import { useOrderTypes } from '@/lib/orderTypes'
import type { OrderDetail, OrderType } from '@/types'

interface Props {
  open: boolean
  order?: OrderDetail | null
  onClose: () => void
  onSaved: (order: OrderDetail) => void
}

// типы приходят с сервера (справочник редактирует администрация)

export function OrderFormModal({ open, order, onClose, onSaved }: Props) {
  const { list: TYPE_LIST } = useOrderTypes()
  const toast = useToast()
  const [busy, setBusy] = useState(false)
  const [form, setForm] = useState<OrderPayload>(() => emptyForm())

  // справочник сотрудников — для выборочного круга ознакомления (кроме владельца)
  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
    enabled: open,
  })

  useEffect(() => {
    if (!open) return
    setForm(
      order
        ? {
            number: order.number,
            order_year: order.order_year,
            title: order.title,
            description: order.description ?? '',
            order_type: order.order_type,
            signature_policy: order.signature_policy ?? 'CLICK',
            issued_on: order.issued_on,
            ack_deadline: order.ack_deadline ?? '',
            require_all: order.require_all,
            recipient_ids: order.recipient_ids ?? [],
          }
        : emptyForm(),
    )
  }, [open, order])

  const set = <K extends keyof OrderPayload>(key: K, value: OrderPayload[K]) =>
    setForm((prev) => ({ ...prev, [key]: value }))

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!form.require_all && (form.recipient_ids ?? []).length === 0) {
      toast.error(new Error('Выберите сотрудников для ознакомления или переключитесь на «Все сотрудники»'))
      return
    }
    setBusy(true)
    try {
      const payload: OrderPayload = {
        ...form,
        title: form.title.trim(),
        number: form.number.trim(),
        description: form.description?.trim() ? form.description!.trim() : null,
        ack_deadline: form.ack_deadline ? form.ack_deadline : null,
        recipient_ids: form.require_all ? null : form.recipient_ids ?? [],
      }
      const saved = order ? await ordersApi.update(order.id, payload) : await ordersApi.create(payload)
      toast.success(order ? 'Приказ обновлён' : 'Черновик приказа создан')
      onSaved(saved)
      onClose()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={order ? `Редактирование приказа №${order.number}` : 'Новый приказ'}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button className="btn btn--primary" form="order-form" type="submit" disabled={busy}>
            {busy ? 'Сохраняем…' : order ? 'Сохранить' : 'Создать черновик'}
          </button>
        </>
      }
    >
      <form id="order-form" onSubmit={submit} className="stack" style={{ gap: 14 }}>
        <div className="form-grid">
          <label className="field">
            <span className="field__label">Номер приказа *</span>
            <input className="input" required value={form.number} onChange={(e) => set('number', e.target.value)} placeholder="112-к" />
          </label>
          <label className="field">
            <span className="field__label">Год *</span>
            <input
              className="input"
              type="number"
              required
              min={2000}
              max={2200}
              value={form.order_year}
              onChange={(e) => set('order_year', Number(e.target.value))}
            />
          </label>
          <label className="field">
            <span className="field__label">Дата приказа *</span>
            <input className="input" type="date" required value={form.issued_on} onChange={(e) => set('issued_on', e.target.value)} />
          </label>
          <label className="field">
            <span className="field__label">Срок ознакомления</span>
            <input
              className="input"
              type="date"
              value={form.ack_deadline ?? ''}
              onChange={(e) => set('ack_deadline', e.target.value)}
            />
            <span className="field__hint">Необязательно: используется для напоминаний</span>
          </label>
        </div>

        <label className="field">
          <span className="field__label">Заголовок *</span>
          <input
            className="input"
            required
            minLength={3}
            value={form.title}
            onChange={(e) => set('title', e.target.value)}
            placeholder="О приёме на работу и распределении учебной нагрузки"
          />
        </label>

        <label className="field">
          <span className="field__label">Тип приказа *</span>
          <select className="select" value={form.order_type} onChange={(e) => set('order_type', e.target.value as OrderType)}>
            {TYPE_LIST.map((t) => (
              <option key={t.code} value={t.code}>{t.title}</option>
            ))}
          </select>
        </label>

        <label className="field">
          <span className="field__label">Подпись ознакомления *</span>
          <select
            className="select"
            value={form.signature_policy ?? 'CLICK'}
            onChange={(e) => set('signature_policy', e.target.value as NonNullable<OrderPayload['signature_policy']>)}
          >
            <option value="NONE">Без подписи — только фиксация времени</option>
            <option value="CLICK">Клик «Ознакомлен»</option>
            <option value="TOTP">Код из аутентификатора (ПЭП)</option>
            <option value="CODE_WORD">Код аутентификатора + контрольное слово</option>
          </select>
          <span className="field__hint">
            {form.order_type === 'STUDENTS'
              ? 'Для движения обучающихся действует усиленное автоправило: не ниже кода аутентификатора'
              : 'Чем выше уровень, тем строже подтверждение ознакомления сотрудником'}
          </span>
        </label>

        <label className="field">
          <span className="field__label">Краткое описание</span>
          <textarea
            className="textarea"
            value={form.description ?? ''}
            onChange={(e) => set('description', e.target.value)}
            placeholder="Что важно знать сотрудникам: суть, сроки, ответственные"
          />
        </label>

        <div className="field">
          <span className="field__label">Круг ознакомления *</span>
          <div className="row" style={{ gap: 8 }}>
            <button
              type="button"
              className={form.require_all ?? true ? 'chip chip--active' : 'chip'}
              onClick={() => set('require_all', true)}
            >
              👥 Все сотрудники
            </button>
            <button
              type="button"
              className={form.require_all ?? true ? 'chip' : 'chip chip--active'}
              onClick={() => set('require_all', false)}
            >
              🎯 Выбранный список
            </button>
          </div>
          {form.require_all ?? true ? (
            <span className="field__hint">При публикации ознакомиться должны все активные сотрудники, кроме Владельца.</span>
          ) : (
            <div style={{ marginTop: 8 }}>
              <UserMultiSelect
                options={people}
                value={form.recipient_ids ?? []}
                onChange={(ids) => set('recipient_ids', ids)}
                placeholder="Никто не выбран — приказ нельзя будет опубликовать"
              />
              <span className="field__hint">Круг можно будет изменить и после публикации — подписи уже ознакомившихся сохранятся.</span>
            </div>
          )}
        </div>

        {!order && (
          <div className="alert alert--info">
            Приказ создаётся в статусе «Черновик». Загрузите PDF/DOCX и нажмите «Опубликовать» — сотрудникам
            придёт уведомление, а в листе ознакомления появятся строки на подпись.
          </div>
        )}
      </form>
    </Modal>
  )
}

function emptyForm(): OrderPayload {
  const today = new Date()
  const deadline = new Date(today.getTime() + 7 * 86_400_000)
  return {
    number: '',
    order_year: today.getFullYear(),
    title: '',
    description: '',
    order_type: 'HR',
    signature_policy: 'CLICK',
    issued_on: toDateInput(today),
    ack_deadline: toDateInput(deadline),
    require_all: true,
    recipient_ids: [],
  }
}
