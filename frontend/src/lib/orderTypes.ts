/** Динамический справочник типов приказов: GET /meta/order-types + статический фолбэк. */
import { useQuery } from '@tanstack/react-query'

import { metaApi } from '@/api/endpoints'
import { qk } from '@/api/query'
import { ORDER_TYPE_LABEL } from '@/lib/format'

export interface OrderTypeItem {
  code: string
  title: string
}

export function useOrderTypes() {
  const { data } = useQuery({
    queryKey: qk('/meta/order-types'),
    queryFn: metaApi.orderTypes,
    staleTime: 5 * 60_000,
  })
  const list: OrderTypeItem[] = data?.length ? data : Object.entries(ORDER_TYPE_LABEL).map(([code, title]) => ({ code, title }))
  const labels: Record<string, string> = { ...ORDER_TYPE_LABEL }
  for (const t of list) labels[t.code] = t.title
  return { list, labels }
}
