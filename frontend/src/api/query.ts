/**
 * Обёртка над TanStack Query: ключи запросов, инвалидация, «выключено для гостя».
 */
import { useMutation, useQuery, useQueryClient, type UseQueryOptions } from '@tanstack/react-query'
import { useEffect } from 'react'

import { useAuthStore } from '@/store/auth'
import type { QueryParams } from '@/types'

export const queryClientConfig = {
  defaultOptions: {
    queries: {
      staleTime: 20_000,
      gcTime: 5 * 60_000,
      refetchOnWindowFocus: true,
      retry: (failureCount: number, error: unknown) => {
        const status = (error as { status?: number })?.status
        if (status && status >= 400 && status < 500) return false
        return failureCount < 2
      },
    },
  },
} as const

/** Ключ запроса из пути + параметров (стабильная сериализация). */
export function qk(path: string, params?: QueryParams): readonly unknown[] {
  if (!params) return [path]
  const entries = Object.entries(params)
    .filter(([, v]) => v !== undefined && v !== null && v !== '')
    .sort(([a], [b]) => a.localeCompare(b))
  return [path, entries]
}

/** GET-хук: не выполняется, пока пользователь не аутентифицирован. */
export function useApi<T>(path: string, fetcher: () => Promise<T>, params?: QueryParams, options?: Partial<UseQueryOptions<T>>) {
  const authed = useAuthStore((s) => s.status !== 'guest')
  return useQuery<T>({
    queryKey: qk(path, params),
    queryFn: fetcher,
    enabled: authed,
    ...options,
  })
}

/** Mutation + автоматическая инвалидация списка после успеха. */
export function useApiMutation<TVars, TResult>(
  fn: (vars: TVars) => Promise<TResult>,
  invalidate: string[] = [],
) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: fn,
    onSuccess: () => {
      invalidate.forEach((key) => {
        void qc.invalidateQueries({ queryKey: [key] })
      })
    },
  })
}

/** Автообновление списка задач/уведомлений раз в N секунд (дедлайны «тикают»). */
export function usePolling(intervalMs = 60_000, onTick?: () => void) {
  useEffect(() => {
    if (!onTick || intervalMs <= 0) return
    const id = window.setInterval(onTick, intervalMs)
    return () => window.clearInterval(id)
  }, [intervalMs, onTick])
}
