/** Закрепляемые виды списков: «Запомнить этот вид» в панели фильтров сохраняет
 *  текущие фильтры в профиле (GET/PUT /users/me/list-views) и применяет их при
 *  следующем входе — с любого устройства. Фильтры из URL (?status=…) приоритетнее
 *  закреплённых: ссылка из поиска/уведомления всегда открывает именно то, что просили. */
import { useEffect, useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useSearchParams } from 'react-router-dom'

import { usersApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { useToast } from '@/components/Toast'

export type ViewParams = Record<string, string>

export function usePinnedView(
  listKey: string,
  encode: () => ViewParams,
  decode: (params: ViewParams) => void,
) {
  const [searchParams] = useSearchParams()
  const qc = useQueryClient()
  const toast = useToast()
  const applied = useRef(false)

  const { data: views } = useQuery({
    queryKey: qk('/users/me/list-views'),
    queryFn: usersApi.listViews,
  })
  const saved: ViewParams | null = views?.[listKey] ?? null
  const isPinned = Boolean(saved && Object.keys(saved).length)

  useEffect(() => {
    if (applied.current || !views) return
    applied.current = true
    if (Array.from(searchParams.keys()).length > 0) return   // явные фильтры из ссылки
    const params = views[listKey]
    if (params && Object.keys(params).length) decode(params)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [views])

  async function save() {
    try {
      await usersApi.saveListView(listKey, encode())
      void qc.invalidateQueries({ queryKey: qk('/users/me/list-views') })
      toast.success('Вид закреплён: список откроется с этими фильтрами при следующем входе')
    } catch (err) {
      toast.error(err)
    }
  }

  async function reset() {
    try {
      await usersApi.deleteListView(listKey)
      void qc.invalidateQueries({ queryKey: qk('/users/me/list-views') })
      decode({})
      toast.success('Закреплённый вид сброшен')
    } catch (err) {
      toast.error(err)
    }
  }

  return { saved, isPinned, save, reset }
}
