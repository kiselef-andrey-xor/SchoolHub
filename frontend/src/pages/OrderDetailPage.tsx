/** Карточка приказа: документ, метаданные, версии файлов, ознакомление, действия администратора. */
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import { ordersApi, usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { AckModal } from '@/components/AckModal'
import { AckSheetModal } from '@/components/AckSheetModal'
import { DocViewer } from '@/components/DocViewer'
import { Modal } from '@/components/Modal'
import { OrderFormModal } from '@/components/OrderFormModal'
import { UserMultiSelect } from '@/components/UserMultiSelect'
import { UserAvatar } from '@/components/UserAvatar'
import {
  AckBadge,
  Badge,
  EmptyState,
  ErrorState,
  OrderStatusBadge,
  OrderTypeBadge,
  Progress,
  Spinner,
} from '@/components/ui'
import { useToast } from '@/components/Toast'
import { CAP, can } from '@/lib/permissions'
import { formatDate, formatDateTime, formatSize } from '@/lib/format'
import { useOrderTypes } from '@/lib/orderTypes'
import { useAuthStore } from '@/store/auth'
import type { OrderDetail } from '@/types'

export function OrderDetailPage() {
  const { labels: TYPE_LABELS } = useOrderTypes()
  const { id = '' } = useParams()
  const user = useAuthStore((s) => s.user)!
  const toast = useToast()
  const qc = useQueryClient()
  const navigate = useNavigate()
  const fileInput = useRef<HTMLInputElement>(null)

  const [editOpen, setEditOpen] = useState(false)
  const [ackOpen, setAckOpen] = useState(false)
  const [sheetOpen, setSheetOpen] = useState(false)
  const [recipientsOpen, setRecipientsOpen] = useState(false)
  const [confirmArchive, setConfirmArchive] = useState(false)
  const [confirmDeleteArchived, setConfirmDeleteArchived] = useState(false)
  const [selectedVersion, setSelectedVersion] = useState<string | undefined>(undefined)
  const [uploading, setUploading] = useState(false)

  const canManage = can(user.capabilities, CAP.ordersManage)
  const canSeeSheet = can(user.capabilities, CAP.ordersAckReport)
  const canAck = can(user.capabilities, CAP.ordersAck)

  const { data: order, isLoading, error, refetch } = useQuery({
    queryKey: qk(`/orders/${id}`),
    queryFn: () => ordersApi.detail(id),
  })

  const invalidate = () => {
    void qc.invalidateQueries({ queryKey: ['/orders'] })
    void qc.invalidateQueries({ queryKey: qk(`/orders/${id}`) })
    void qc.invalidateQueries({ queryKey: ['/dashboard'] })
    void qc.invalidateQueries({ queryKey: qk('/notifications/unread-count') })
  }

  if (isLoading) return <Spinner large label="Открываем приказ…" />
  if (error) return <ErrorState error={error} onRetry={() => void refetch()} />
  if (!order) return null

  const myAck = order.my_ack
  const progress = order.ack_total ? (order.ack_done / order.ack_total) * 100 : 0
  const isDraft = order.status === 'DRAFT'
  const isArchived = order.status === 'ARCHIVED'
  const currentFile = order.files.find((f) => f.id === selectedVersion) ?? order.current_file

  async function onUpload(files: FileList | null) {
    if (!files || files.length === 0) return
    const file = files[0]
    const okTypes = ['application/pdf', 'application/vnd.openxmlformats-officedocument.wordprocessingml.document', 'application/msword']
    if (!okTypes.includes(file.type)) {
      toast.error(new Error('Можно загружать только PDF или DOCX'))
      return
    }
    setUploading(true)
    try {
      await ordersApi.uploadFile(order!.id, file)
      toast.success(`Файл «${file.name}» загружен (версия ${order!.files.length + 1})`)
      setSelectedVersion(undefined)
      invalidate()
    } catch (err) {
      toast.error(err)
    } finally {
      setUploading(false)
      if (fileInput.current) fileInput.current.value = ''
    }
  }

  async function run(action: () => Promise<unknown>, successText: string) {
    try {
      await action()
      toast.success(successText)
      invalidate()
      void refetch()
    } catch (err) {
      toast.error(err)
    }
  }

  return (
    <div className="stack" style={{ gap: 16 }}>
      <div className="page-head">
        <div className="page-head__text">
          <div className="row" style={{ gap: 8, marginBottom: 6 }}>
            <Link className="tiny" to="/orders">← Все приказы</Link>
            <OrderStatusBadge status={order.status} />
            <OrderTypeBadge type={order.order_type} />
            {isDraft && <Badge tone="warn">виден только администрации</Badge>}
          </div>
          <h1>Приказ №{order.number} · {order.title}</h1>
          <div className="page-head__hint">
            от {formatDate(order.issued_on)} · {order.order_year} год ·{' '}
            {order.created_by ? `издал(а) ${order.created_by.full_name}` : 'автор не указан'}
            {order.ack_deadline && ` · ознакомиться до ${formatDate(order.ack_deadline)}`}
          </div>
        </div>

        <div className="page-head__actions">
          {canAck && order.status === 'PUBLISHED' && myAck && (
            myAck.status === 'ACKNOWLEDGED' ? (
              <Badge tone="ok">✓ Ознакомлен {formatDate(myAck.acknowledged_at)}</Badge>
            ) : (
              <button className="btn btn--success" onClick={() => setAckOpen(true)}>
                ✓ Ознакомлен
              </button>
            )
          )}
          {canSeeSheet && order.status !== 'DRAFT' && (
            <button className="btn" onClick={() => setSheetOpen(true)}>🗒 Лист ознакомления</button>
          )}
          {isArchived && user.role === 'OWNER' && (
            <button
              className="btn btn--danger"
              onClick={() => setConfirmDeleteArchived(true)}
              title="Удалить архивный приказ безвозвратно (только Владелец)"
            >
              🗑 Удалить архивный
            </button>
          )}
          {canManage && !isArchived && (
            <>
              {isDraft && <button className="btn" onClick={() => setEditOpen(true)}>✎ Редактировать</button>}
              {isDraft && (
                <button
                  className="btn btn--primary"
                  onClick={() => run(() => ordersApi.publish(order.id), 'Приказ опубликован, сотрудникам отправлено уведомление')}
                  disabled={order.files.length === 0}
                  title={order.files.length === 0 ? 'Сначала загрузите документ' : undefined}
                >
                  📢 Опубликовать
                </button>
              )}
              {order.status === 'PUBLISHED' && (
                <button className="btn" onClick={() => setRecipientsOpen(true)}>👥 Круг ознакомления</button>
              )}
              {!isDraft && (
                <button className="btn" onClick={() => setConfirmArchive(true)}>🗄 В архив</button>
              )}
              {isDraft && (
                <button
                  className="btn btn--danger"
                  onClick={() =>
                    run(async () => {
                      await ordersApi.remove(order.id)
                      navigate('/orders')
                    }, 'Черновик удалён')
                  }
                >
                  🗑 Удалить
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {order.status === 'PUBLISHED' && canManage && (
        <div className="alert alert--info small">
          Приказ опубликован: редактирование и замена документа недоступны — сотрудники уже
          знакомятся с ним. Доступны просмотр, лист ознакомления и перевод в архив.
        </div>
      )}

      <div className="grid grid--order" style={{ alignItems: 'start' }}>
        <section className="card">
          <header className="card__head">
            <h3>Документ</h3>
            {currentFile && (
              <span className="tiny muted">
                {currentFile.original_name} · {formatSize(currentFile.size_bytes)} · версия {currentFile.version}
              </span>
            )}
            {canManage && isDraft && (
              <>
                <span className="grow" />
                <input
                  ref={fileInput}
                  type="file"
                  accept=".pdf,.doc,.docx,application/pdf,application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                  style={{ display: 'none' }}
                  onChange={(e) => void onUpload(e.target.files)}
                />
                <button className="btn btn--sm btn--primary" onClick={() => fileInput.current?.click()} disabled={uploading}>
                  {uploading ? 'Загружаем…' : '⬆ Загрузить PDF/DOCX'}
                </button>
              </>
            )}
          </header>
          <div className="card__body">
            {order.files.length === 0 ? (
              <EmptyState
                icon="📎"
                title="Документ не загружен"
                hint={canManage ? 'Загрузите PDF или DOCX — без документа приказ нельзя опубликовать' : 'Администрация ещё не прикрепила файл'}
              />
            ) : (
              <DocViewer orderId={order.id} fileName={currentFile?.original_name} key={currentFile?.id} />
            )}

            {order.files.length > 1 && (
              <div style={{ marginTop: 14 }}>
                <div className="field__label" style={{ marginBottom: 6 }}>История версий</div>
                <div className="row">
                  {order.files.map((f) => (
                    <button
                      key={f.id}
                      className={f.id === currentFile?.id ? 'chip chip--active' : 'chip'}
                      onClick={() => setSelectedVersion(f.id)}
                      title={`${f.original_name}, ${formatSize(f.size_bytes)}, ${formatDateTime(f.created_at)}`}
                    >
                      v{f.version}{f.is_current ? ' · текущая' : ''}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        </section>

        <div className="stack" style={{ gap: 16 }}>
          <section className="card">
            <header className="card__head">
              <h3>О ознакомлении</h3>
              {canManage && <span className="grow" />}
            </header>
            <div className="card__body stack" style={{ gap: 12 }}>
              {myAck ? (
                <div className="row" style={{ gap: 10 }}>
                  <UserAvatar user={user} />
                  <div className="grow">
                    <div className="strong">Ваш статус</div>
                    <div className="small muted">
                      {myAck.status === 'ACKNOWLEDGED'
                        ? `Подтверждено ${formatDateTime(myAck.acknowledged_at)}${myAck.ip_address ? ` · IP ${myAck.ip_address}` : ''}`
                        : 'Ожидает подтверждения'}
                    </div>
                  </div>
                  <AckBadge status={myAck.status} />
                </div>
              ) : (
                <div className="small muted">
                  Вы не входите в список ознакомления по этому приказу.
                </div>
              )}

              {myAck?.comment && (
                <div className="alert alert--info small">
                  <strong>Ваш комментарий:</strong> {myAck.comment}
                </div>
              )}

              {(canSeeSheet || canManage) && order.status !== 'DRAFT' && (
                <div>
                  <div className="row small muted" style={{ marginBottom: 6 }}>
                    <span>Подписали {order.ack_done} из {order.ack_total}</span>
                    <span className="grow" />
                    <span>{progress.toFixed(0)}%</span>
                  </div>
                  <Progress value={progress} tone={progress === 100 ? 'ok' : progress >= 50 ? undefined : 'warn'} />
                </div>
              )}
            </div>
          </section>

          <section className="card">
            <header className="card__head"><h3>Реквизиты</h3></header>
            <div className="card__body">
              <dl className="stack" style={{ gap: 9, margin: 0 }}>
                <Row label="Номер и год" value={`№${order.number} / ${order.order_year}`} />
                <Row label="Тип" value={TYPE_LABELS[order.order_type] ?? order.order_type} />
                <Row label="Дата приказа" value={formatDate(order.issued_on)} />
                <Row label="Срок ознакомления" value={order.ack_deadline ? formatDate(order.ack_deadline) : 'не задан'} />
                <Row
                  label="Круг ознакомления"
                  value={
                    order.require_all
                      ? 'все сотрудники (кроме Владельца)'
                      : `выбранный список · ${order.ack_total} ${order.ack_total === 1 ? 'человек' : 'чел.'}`
                  }
                />
                <Row label="Опубликован" value={order.published_at ? formatDateTime(order.published_at) : '—'} />
                <Row label="Обновлён" value={formatDateTime(order.updated_at)} />
                {order.description && <Row label="Описание" value={order.description} />}
              </dl>
            </div>
          </section>
        </div>
      </div>

      <Modal
        open={confirmDeleteArchived}
        onClose={() => setConfirmDeleteArchived(false)}
        title="Удалить архивный приказ?"
        footer={
          <>
            <button className="btn" onClick={() => setConfirmDeleteArchived(false)}>Отмена</button>
            <button
              className="btn btn--danger"
              onClick={() =>
                void run(async () => {
                  await ordersApi.remove(order.id)
                  setConfirmDeleteArchived(false)
                  navigate('/orders')
                }, 'Архивный приказ удалён')
              }
            >
              Удалить безвозвратно
            </button>
          </>
        }
      >
        <div className="alert alert--warn small">
          Приказ №{order.number} «{order.title}» будет удалён <strong>безвозвратно</strong> вместе
          с документом и листом ознакомления ({order.ack_done} из {order.ack_total} ознакомлений).
          В журнале событий останется запись об удалении. Отменить действие нельзя.
        </div>
      </Modal>

      <OrderFormModal open={editOpen} order={order} onClose={() => setEditOpen(false)} onSaved={() => { invalidate(); void refetch() }} />
      <AckModal open={ackOpen} order={order} onClose={() => setAckOpen(false)} onDone={invalidate} />
      <AckSheetModal open={sheetOpen} order={order} onClose={() => setSheetOpen(false)} />
      <RecipientsModal
        open={recipientsOpen}
        order={order}
        onClose={() => setRecipientsOpen(false)}
        onSaved={() => { invalidate(); void refetch() }}
      />

      <Modal
        open={confirmArchive}
        onClose={() => setConfirmArchive(false)}
        title="Отправить приказ в архив?"
        footer={
          <>
            <button className="btn" onClick={() => setConfirmArchive(false)}>Отмена</button>
            <button
              className="btn btn--primary"
              onClick={async () => {
                setConfirmArchive(false)
                await run(() => ordersApi.archive(order.id), 'Приказ перемещён в архив')
              }}
            >
              В архив
            </button>
          </>
        }
      >
        <p>
          Архивный приказ остаётся доступным для чтения, но его нельзя редактировать, а сбор ознакомлений
          прекращается. Отменить архивирование можно только созданием нового приказа.
        </p>
      </Modal>
    </div>
  )
}

/** Смена круга ознакомления опубликованного приказа (PUT /orders/{id}/recipients).
 *  Подписи уже ознакомившихся сохраняются; добавленные получают уведомление. */
function RecipientsModal({
  order,
  open,
  onClose,
  onSaved,
}: {
  order: OrderDetail
  open: boolean
  onClose: () => void
  onSaved: () => void
}) {
  const toast = useToast()
  const [ids, setIds] = useState<string[]>([])
  const [busy, setBusy] = useState(false)

  const { data: people = [] } = useQuery({
    queryKey: qk('/users/options'),
    queryFn: () => usersApi.options(),
    enabled: open,
  })

  useEffect(() => {
    if (open) setIds(order.recipient_ids.length ? order.recipient_ids : [])
  }, [open, order])

  async function save() {
    if (ids.length === 0) {
      toast.error(new Error('Выберите хотя бы одного сотрудника'))
      return
    }
    setBusy(true)
    try {
      await ordersApi.setRecipients(order.id, ids)
      toast.success('Круг ознакомления обновлён')
      onSaved()
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
      title={`Круг ознакомления — приказ №${order.number}`}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button className="btn btn--primary" onClick={() => void save()} disabled={busy}>
            {busy ? 'Сохраняем…' : 'Сохранить круг'}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        <div className="alert alert--info small">
          Список можно менять даже после публикации: у добавленных появится статус «Не ознакомлен» и придёт
          уведомление, у убранных (кто ещё не подписал) строка удалится из листа. Уже подтверждённые подписи сохраняются.
        </div>
        <UserMultiSelect options={people} value={ids} onChange={setIds} placeholder="Никто не выбран" />
      </div>
    </Modal>
  )
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="stack" style={{ gap: 2 }}>
      <dt className="tiny muted">{label}</dt>
      <dd className="small strong" style={{ margin: 0 }}>{value}</dd>
    </div>
  )
}
