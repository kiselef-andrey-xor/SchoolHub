/** Модальное окно подписи чувствительного действия (ПЭП «код + слово»).
 *  Используется для: восстановление БД из копии, восстановление файлов из ZIP,
 *  локдаун, очистка журнала событий, удаление учётной записи. */
import { useState } from 'react'

import { Modal } from '@/components/Modal'

interface Props {
  open: boolean
  title: string
  /** что именно подписываем — крупно в теле окна */
  description: string
  danger?: boolean
  submitLabel?: string
  onClose: () => void
  onConfirm: (sign: { code: string; word: string }) => Promise<void>
}

export function PepSignModal({
  open,
  title,
  description,
  danger = true,
  submitLabel = 'Подписать и выполнить',
  onClose,
  onConfirm,
}: Props) {
  const [code, setCode] = useState('')
  const [word, setWord] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit() {
    if (!/^\d{6}$/.test(code) || !word.trim()) return
    setBusy(true)
    try {
      await onConfirm({ code, word: word.trim() })
      setCode('')
      setWord('')
      onClose()
    } catch (err) {
      // ошибку показывает вызывающий код (toast) — окно оставляем открытым
    } finally {
      setBusy(false)
    }
  }

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={title}
      footer={
        <>
          <button className="btn" onClick={onClose} disabled={busy}>Отмена</button>
          <button
            className={`btn ${danger ? 'btn--danger' : 'btn--success'}`}
            onClick={submit}
            disabled={busy || !/^\d{6}$/.test(code) || !word.trim()}
          >
            {busy ? 'Подписываем…' : submitLabel}
          </button>
        </>
      }
    >
      <div className="stack" style={{ gap: 12 }}>
        <div className={`alert ${danger ? 'alert--error' : 'alert--info'}`}>{description}</div>
        <label className="field">
          <span className="field__label">Код из приложения-аутентификатора</span>
          <input
            className="input"
            inputMode="numeric"
            autoComplete="off"
            maxLength={6}
            placeholder="000000"
            value={code}
            onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
          />
          <span className="field__hint">Одноразовый шестизначный код (действует ~30 секунд)</span>
        </label>
        <label className="field">
          <span className="field__label">Контрольное слово</span>
          <input
            className="input"
            type="password"
            autoComplete="off"
            placeholder="Слово, заданное при привязке аутентификатора"
            value={word}
            onChange={(e) => setWord(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && submit()}
          />
          <span className="field__hint">
            Уровня «просто код» для этой операции недостаточно — нужна подпись «код + слово».
            Привязка и слово — в разделе «Электронная подпись» вашего профиля.
          </span>
        </label>
      </div>
    </Modal>
  )
}
