/** Управление справочником должностей (Владелец/Администрация):
 *  добавить, переименовать, синонимы для распознавания в DOCX, удалить свободную. */
import { useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import { Modal } from '@/components/Modal'
import { useToast } from '@/components/Toast'
import { positionsApi } from '@/api/endpoints'
import type { Position } from '@/types'

interface Props {
  onClose: () => void
}

export function PositionsModal({ onClose }: Props) {
  const toast = useToast()
  const qc = useQueryClient()
  const { data: positions = [] } = useQuery({ queryKey: ['/meta/positions'], queryFn: positionsApi.list })
  const [title, setTitle] = useState('')
  const [rows, setRows] = useState<Record<string, { title: string; aliases: string }> | null>(null)

  const draft = (p: Position) => (rows && rows[p.id]) || { title: p.title, aliases: p.aliases ?? '' }

  const save = useMutation({
    mutationFn: ({ id, payload }: { id: string; payload: { title?: string; aliases?: string } }) =>
      positionsApi.update(id, payload),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['/meta/positions'] })
      toast.success('Должность сохранена')
    },
    onError: (e) => toast.error(e),
  })

  const create = useMutation({
    mutationFn: () => positionsApi.create(title.trim()),
    onSuccess: () => {
      setTitle('')
      void qc.invalidateQueries({ queryKey: ['/meta/positions'] })
      toast.success('Должность добавлена')
    },
    onError: (e) => toast.error(e),
  })

  const remove = useMutation({
    mutationFn: (id: string) => positionsApi.remove(id),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: ['/meta/positions'] })
      toast.success('Должность удалена')
    },
    onError: (e) => toast.error(e),
  })

  return (
    <Modal open title="Справочник должностей" onClose={onClose} width="760px">
      <div className="stack" style={{ gap: 10 }}>
        <div className="tiny muted">
          Должности назначаются сотрудникам в карточке пользователя (можно несколько). В плане работы и ВШК
          ответственным может быть сотрудник, должность целиком или «Администрация». Синонимы помогают
          распознавать должности при импорте Word-документов («зам по УВР», «руководители ШМО»…).
        </div>
        <div className="row" style={{ gap: 8 }}>
          <input
            className="input grow"
            value={title}
            onChange={(e) => setTitle(e.target.value)}
            placeholder="Новая должность, напр. «Учитель математики»"
          />
          <button className="btn btn--primary" disabled={!title.trim() || create.isPending} onClick={() => create.mutate()}>
            ＋ Добавить
          </button>
        </div>
        <div style={{ maxHeight: '52vh', overflowY: 'auto', border: '1px solid var(--border)', borderRadius: 8 }}>
          <table className="table">
            <thead>
              <tr>
                <th style={{ width: '42%' }}>Должность</th>
                <th>Синонимы (через запятую)</th>
                <th style={{ width: 40 }} />
              </tr>
            </thead>
            <tbody>
              {positions.map((p) => {
                const d = draft(p)
                const dirty = d.title !== p.title || d.aliases !== (p.aliases ?? '')
                return (
                  <tr key={p.id}>
                    <td>
                      <input
                        className="input"
                        value={d.title}
                        onChange={(e) => setRows({ ...(rows ?? {}), [p.id]: { ...d, title: e.target.value } })}
                        onBlur={() => dirty && d.title.trim() && save.mutate({ id: p.id, payload: { title: d.title.trim() } })}
                      />
                    </td>
                    <td>
                      <input
                        className="input"
                        value={d.aliases}
                        placeholder="зам по увр, завуч…"
                        onChange={(e) => setRows({ ...(rows ?? {}), [p.id]: { ...d, aliases: e.target.value } })}
                        onBlur={() => dirty && save.mutate({ id: p.id, payload: { aliases: d.aliases } })}
                      />
                    </td>
                    <td>
                      <button
                        className="btn btn--sm btn--ghost"
                        title="Удалить (только если никому не назначена)"
                        onClick={() => {
                          if (window.confirm(`Удалить должность «${p.title}»?`)) remove.mutate(p.id)
                        }}
                      >
                        🗑
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </div>
        <div className="row">
          <span className="grow" />
          <button className="btn btn--primary" onClick={onClose}>
            Готово
          </button>
        </div>
      </div>
    </Modal>
  )
}
