/** Подтверждение ознакомления с приказом (юридически значимое действие: фиксируются время, IP, User-Agent).
 *  Уровень подписи задаёт политика приказа (order.signature_policy): NONE / CLICK / TOTP / CODE_WORD. */
import { useState } from 'react'

import { ordersApi } from '@/api/endpoints'
import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { formatDate } from '@/lib/format'
import { SIGN_POLICY_LABELS } from '@/types'
import { useAuthStore } from '@/store/auth'
import type { OrderDetail } from '@/types'

interface Props {
  open: boolean
  order: OrderDetail
  onClose: () => void
  onDone: () => void
}

export function AckModal({ open, order, onClose, onDone }: Props) {
  const toast = useToast()
  const user = useAuthStore((s) => s.user)
  const [confirmed, setConfirmed] = useState(false)
  const [comment, setComment] = useState('')
  const [busy, setBusy] = useState(false)
  const [code, setCode] = useState('')
  const [word, setWord] = useState('')

  const policy = order.signature_policy ?? 'CLICK'
  const needCode = policy === 'TOTP' || policy === 'CODE_WORD'
  const needWord = policy === 'CODE_WORD'
  const totpReady = !!user?.totp_enabled

  async function submit() {
    if (!confirmed) return
    if (needCode && !/^\d{6}$/.test(code)) return
    if (needWord && !word.trim()) return
    setBusy(true)
    try {
      await ordersApi.acknowledge(order.id, comment.trim() || undefined, {
        code: needCode ? code : undefined,
        word: needWord ? word.trim() : undefined,
      })
      toast.success(needCode ? 'Приказ подписан электронной подписью' : 'Ознакомление подтверждено')
      setConfirmed(false)
      setComment('')
      setCode('')
      setWord('')
      onDone()
      onClose()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  const ready = confirmed && (!needCode || /^\d{6}$/.test(code)) && (!needWord || !!word.trim())

  return (
    <Modal
      open={open}
      onClose={onClose}
      title="Подтверждение ознакомления"
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button className="btn btn--success" onClick={submit} disabled={!ready || busy}>
            {busy ? 'Отправляем…' : needCode ? 'Подписать и подтвердить' : 'Подтвердить ознакомление'}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        <div className="alert alert--info">
          <strong>Приказ №{order.number} от {formatDate(order.issued_on)}</strong>
          <div>{order.title}</div>
          <div className="tiny" style={{ marginTop: 4 }}>
            Требуемый способ подписи: <strong>{SIGN_POLICY_LABELS[policy]}</strong>
          </div>
        </div>

        <label className="row small" style={{ gap: 8, alignItems: 'flex-start' }}>
          <input type="checkbox" checked={confirmed} onChange={(e) => setConfirmed(e.target.checked)} style={{ marginTop: 3 }} />
          <span>
            Я полностью прочитал(а) текст приказа, смысл и порядок его исполнения мне понятны.
            Подтверждение фиксирует дату и время, IP-адрес и браузер — как подпись в бумажном листе ознакомления.
          </span>
        </label>

        {needCode && !totpReady && (
          <div className="alert alert--error">
            Этот приказ требует подписи кодом из приложения-аутентификатора, но аутентификатор не привязан
            к вашей учётной записи. Привяжите его в разделе «Профиль → Электронная подпись (ПЭП)».
            Пароль вместо кода не принимается — подписание заблокировано до привязки.
          </div>
        )}

        {needCode && totpReady && (
          <label className="field">
            <span className="field__label">Код из приложения-аутентификатора</span>
            <input
              className="input"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              placeholder="000000"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            />
            <span className="field__hint">Одноразовый шестизначный код (действует ~30 секунд)</span>
          </label>
        )}

        {needWord && totpReady && (
          <label className="field">
            <span className="field__label">Контрольное слово</span>
            <input
              className="input"
              type="password"
              autoComplete="off"
              value={word}
              onChange={(e) => setWord(e.target.value)}
              placeholder="Слово, заданное при привязке аутентификатора"
            />
            <span className="field__hint">Усиленная подпись «код + слово» для этого приказа</span>
          </label>
        )}

        <label className="field">
          <span className="field__label">Комментарий (необязательно)</span>
          <textarea
            className="textarea"
            value={comment}
            maxLength={500}
            onChange={(e) => setComment(e.target.value)}
            placeholder="Например: «Принято к исполнению» или вопрос ответственному"
          />
          <span className="field__hint">{comment.length}/500 символов</span>
        </label>
      </div>
    </Modal>
  )
}
