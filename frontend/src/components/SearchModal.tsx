/** Глобальный поиск (Ctrl+K): приказы, задачи, входящие, люди, служебки,
 *  заявки техподдержки и инструкции — по разделам.
 *  Секции приходят с сервера уже отфильтрованными по правам текущего пользователя. */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'

import { searchApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { Modal } from '@/components/Modal'
import { Spinner } from '@/components/ui'
import type { SearchHit, SearchResults } from '@/types'

type SectionKey = keyof SearchResults

const SECTION_META: { key: SectionKey; label: string; icon: string }[] = [
  { key: 'orders', label: 'Приказы', icon: '📜' },
  { key: 'tasks', label: 'Задачи', icon: '✅' },
  { key: 'incoming', label: 'Входящие письма', icon: '📥' },
  { key: 'users', label: 'Сотрудники', icon: '👥' },
  { key: 'vshk', label: 'ВШК', icon: '🔎' },
  { key: 'certifications', label: 'Аттестация и КПК', icon: '🏅' },
  { key: 'equipment', label: 'Инвентарь', icon: '🖥' },
  { key: 'movement', label: 'Движение обучающихся', icon: '🎓' },
  { key: 'notes', label: 'Служебные записки', icon: '📝' },
  { key: 'helpdesk', label: 'Заявки техподдержки', icon: '🛠' },
  { key: 'help', label: 'Справка и инструкции', icon: '❔' },
]

export function SearchModal({ open, onClose }: { open: boolean; onClose: () => void }) {
  const navigate = useNavigate()
  const [q, setQ] = useState('')
  const [debounced, setDebounced] = useState('')

  useEffect(() => {
    if (!open) {
      setQ('')
      setDebounced('')
      return
    }
    const id = window.setTimeout(() => setDebounced(q.trim()), 250)
    return () => window.clearTimeout(id)
  }, [q, open])

  const enabled = open && debounced.length >= 2
  const { data, isFetching } = useQuery({
    queryKey: qk('/search', { q: debounced }),
    queryFn: () => searchApi.global(debounced),
    enabled,
  })

  function go(hit: SearchHit) {
    onClose()
    navigate(hit.url)
  }

  // считаем по списку секций, а не руками: новая секция не потеряется из счётчика
  const total = data ? SECTION_META.reduce((sum, sec) => sum + (data[sec.key]?.length ?? 0), 0) : 0

  return (
    <Modal open={open} onClose={onClose} title="Поиск по системе" width="640px">
      <div className="stack" style={{ gap: 12 }}>
        <input
          className="input"
          placeholder="Приказ, задача, № письма, № регистрации, штрихкод, сотрудник… (минимум 2 символа)"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          autoFocus
        />
        {enabled && (
          <div className="stack" style={{ gap: 10, maxHeight: '52vh', overflowY: 'auto' }}>
            {isFetching && !data && <Spinner label="Ищем…" />}
            {data && total === 0 && (
              <div className="small muted">Ничего не найдено по запросу «{debounced}».</div>
            )}
            {data &&
              SECTION_META.map((sec) => {
                const hits = data[sec.key] ?? []
                if (!hits.length) return null
                return (
                  <div key={sec.key}>
                    <div className="tiny muted" style={{ marginBottom: 4 }}>
                      {sec.icon} {sec.label}
                    </div>
                    <div className="stack" style={{ gap: 4 }}>
                      {hits.map((h) => (
                        <button
                          key={h.id}
                          className="search-hit"
                          onClick={() => go(h)}
                          title={h.subtitle}
                        >
                          <span className="search-hit__title">{h.title}</span>
                          {h.subtitle && <span className="search-hit__sub">{h.subtitle}</span>}
                        </button>
                      ))}
                    </div>
                  </div>
                )
              })}
          </div>
        )}
        {!enabled && (
          <div className="tiny muted">
            Ищем по названиям приказов и задач, номерам писем и ФИО сотрудников. Показываются
            только разделы, которые вам доступны.
          </div>
        )}
      </div>
    </Modal>
  )
}
