/**
 * Хранилище сессии (крошечный zustand-подобный стор без зависимостей).
 *
 * Компоненты подписываются через useAuthStore(selector) — перерендер только
 * при изменении выбранного среза.
 */
import { useSyncExternalStore } from 'react'

import { authApi } from '@/api/endpoints'
import { clearTokens, getAccessToken, onSessionChange, refreshAccessToken, setAccessToken } from '@/api/client'
import type { Me } from '@/types'

interface AuthState {
  status: 'guest' | 'loading' | 'authed'
  user: Me | null
  error: string | null
}

let state: AuthState = {
  // всегда стартуем с 'loading': сессия восстанавливается молча через httpOnly-cookie
  status: 'loading',
  user: null,
  error: null,
}

const subscribers = new Set<() => void>()

function set(next: Partial<AuthState>) {
  state = { ...state, ...next }
  subscribers.forEach((fn) => fn())
}

function subscribe(fn: () => void) {
  subscribers.add(fn)
  return () => subscribers.delete(fn)
}

function snapshot() {
  return state
}

export async function loadProfile(): Promise<Me | null> {
  set({ status: 'loading' })
  try {
    if (!getAccessToken()) {
      // access-токена в памяти нет (перезагрузка вкладки) — пробуем молча
      // восстановить сессию по refresh-cookie; нет cookie → гость
      const token = await refreshAccessToken()
      if (!token) {
        set({ status: 'guest', user: null })
        return null
      }
    }
    const me = await authApi.me()
    set({ status: 'authed', user: me, error: null })
    return me
  } catch (err) {
    clearTokens()
    set({ status: 'guest', user: null, error: (err as Error).message })
    return null
  }
}

export async function login(email: string, password: string, remember: boolean): Promise<Me> {
  const tokens = await authApi.login(email.trim().toLowerCase(), password, remember)
  setAccessToken(tokens.access_token)
  const me = await authApi.me()
  set({ status: 'authed', user: me, error: null })
  return me
}

export async function logout() {
  try {
    if (getAccessToken()) await authApi.logout()
  } catch {
    /* сервер мог быть недоступен — токены всё равно сбрасываем */
  }
  clearTokens()
  set({ status: 'guest', user: null })
}

/** Подписка на события клиента (сброс токенов при истёкшем refresh). */
onSessionChange((event) => {
  if (event === 'logout' && state.status !== 'guest') {
    set({ status: 'guest', user: null })
  }
})

export function useAuthStore<T>(selector: (s: AuthState) => T): T {
  return useSyncExternalStore(subscribe, () => selector(snapshot()), () => selector(state))
}

export const authStore = {
  get: () => state,
  loadProfile,
  login,
  logout,
  set,
}
