/** Печать учётной этикетки позиции инвентаря (как на фото из школы):
 *  наименование, код, инвентарный номер и штрихкод EAN-13 / Code-128.
 *  Если штрихкода в карточке нет — предлагается служебный EAN-13 (префикс 200…),
 *  его можно сразу присвоить позиции. */
import { useEffect, useMemo, useState } from 'react'
import { createPortal } from 'react-dom'

import { equipmentApi } from '@/api/endpoints'
import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { barcodeSvg, internalEan13 } from '@/lib/barcode'
import type { EquipmentItem } from '@/types'

export function LabelModal({ open, item, onClose }: { open: boolean; item: EquipmentItem | null; onClose: () => void }) {
  const toast = useToast()
  const [copies, setCopies] = useState(1)
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    if (!open) return
    document.body.classList.add('print-sheet-active')
    return () => document.body.classList.remove('print-sheet-active')
  }, [open])

  const code = useMemo(() => {
    if (!item) return null
    const raw = item.barcode?.trim() || ''
    if (raw) {
      const svg = barcodeSvg(raw)
      if (svg) return { value: raw, ...svg, internal: false }
    }
    // инвентарный номер из цифр/латиницы кодируем как есть, остальное — служебный EAN-13
    const inv = (item.inventory_number ?? '').trim()
    const fromInv = inv && barcodeSvg(inv)
    if (fromInv) return { value: fromInv.code, ...fromInv, internal: false }
    const internal = internalEan13(item.id)
    const svg = barcodeSvg(internal)!
    return { value: internal, ...svg, internal: true }
  }, [item])

  if (!item || !code) return null

  const label = (key: number) => (
    <div className="inv-label" key={key}>
      <div className="inv-label__name">{item.name}</div>
      {item.inventory_number && <div className="inv-label__line">инв. № {item.inventory_number}</div>}
      {item.serial_number && <div className="inv-label__line">сер. № {item.serial_number}</div>}
      {item.location && <div className="inv-label__line">каб. {item.location}</div>}
      <div className="inv-label__code" dangerouslySetInnerHTML={{ __html: code.svg }} />
    </div>
  )

  async function assignCode() {
    if (!item || !code) return
    setBusy(true)
    try {
      await equipmentApi.update(item.id, { barcode: code.value })
      toast.success(`Штрихкод ${code.value} присвоен позиции`)
      onClose()
    } catch (err) {
      toast.error(err)
    } finally {
      setBusy(false)
    }
  }

  return (
    <>
      <Modal
        open={open}
        onClose={onClose}
        title="Этикетка со штрихкодом"
        width="460px"
        footer={
          <>
            <button className="btn" onClick={onClose}>Закрыть</button>
            <button className="btn btn--primary" onClick={() => window.print()}>🖨 Печать</button>
          </>
        }
      >
        <div className="stack" style={{ gap: 12, alignItems: 'center' }}>
          {label(0)}
          {code.internal && (
            <div className="alert alert--info small" style={{ margin: 0 }}>
              В карточке нет штрихкода — показан служебный EAN-13 (префикс 200…).
              <div style={{ marginTop: 6 }}>
                <button className="btn btn--sm" disabled={busy} onClick={() => void assignCode()}>
                  📌 Присвоить этот код позиции
                </button>
              </div>
            </div>
          )}
          <label className="row small" style={{ gap: 8 }}>
            <span className="muted">Этикеток на лист:</span>
            <select className="select" value={copies} onChange={(e) => setCopies(Number(e.target.value))}>
              {[1, 2, 4, 6, 8].map((n) => (
                <option key={n} value={n}>{n}</option>
              ))}
            </select>
          </label>
          <div className="tiny muted">
            Формат — самоклеящиеся этикетки ~70×50 мм или обычная бумага (вырезать по пунктиру).
            Сканер читает код и находит позицию в инвентаре.
          </div>
        </div>
      </Modal>
      {open && createPortal(<div className="print-sheet print-sheet--labels">{Array.from({ length: copies }, (_, i) => label(i + 1))}</div>, document.body)}
    </>
  )
}
