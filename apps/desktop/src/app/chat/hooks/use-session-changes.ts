import { useCallback, useEffect, useMemo, useRef } from 'react'

import { type ChatMessage, toChatMessages } from '@/lib/chat-messages'
import type { ClientSessionState } from '@/app/types'
import type { SessionMessage, StatusResponse } from '@/types/hermes'

type GatewayRequest = <T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number) => Promise<T>

export const DEFAULT_SESSION_CHANGES_POLL_MS = 2_500
export const DEFAULT_SESSION_CHANGES_REFOCUS_DEBOUNCE_MS = 1_000
export const DEFAULT_SESSION_CHANGES_T_SILENCE_MS = 30_000

export interface SessionChangesResponse {
  last_id?: number
  messages?: SessionMessage[]
}

export interface SessionChangesTiming {
  pollIntervalMs: number
  refocusDebounceMs: number
  tSilenceMs: number
}

export interface UseSessionChangesArgs {
  activeSessionId: string | null
  currentView: string
  messages: ChatMessage[]
  requestGateway: GatewayRequest
  statusSnapshot: StatusResponse | null
  updateSessionState: (
    sessionId: string,
    updater: (state: ClientSessionState) => ClientSessionState,
    storedSessionId?: string | null
  ) => ClientSessionState
}

export interface SessionChangesController {
  cursor: number
  disabled: boolean
  renderedIds: Set<string>
}

export function sessionChangesSupported(status: StatusResponse | null): boolean {
  return Boolean((status as { capabilities?: Record<string, unknown> } | null)?.capabilities?.session_changes)
}

export function sessionChangesTiming(status: StatusResponse | null): SessionChangesTiming {
  const config = (status as { config?: Record<string, unknown> } | null)?.config
  const dashboard = config?.dashboard && typeof config.dashboard === 'object' ? config.dashboard : undefined
  const sync =
    dashboard && 'session_sync' in dashboard && typeof dashboard.session_sync === 'object'
      ? (dashboard.session_sync as Record<string, unknown>)
      : undefined

  const numberValue = (key: string, fallback: number) => {
    const value = sync?.[key]
    const numeric = typeof value === 'number' ? value : typeof value === 'string' ? Number(value) : NaN

    return Number.isFinite(numeric) && numeric > 0 ? numeric : fallback
  }

  return {
    pollIntervalMs: numberValue('poll_interval', DEFAULT_SESSION_CHANGES_POLL_MS),
    refocusDebounceMs: numberValue('refocus_debounce', DEFAULT_SESSION_CHANGES_REFOCUS_DEBOUNCE_MS),
    tSilenceMs: numberValue('t_silence', DEFAULT_SESSION_CHANGES_T_SILENCE_MS)
  }
}

export function maxCommittedMessageId(messages: readonly Pick<ChatMessage, 'id'>[]): number {
  let max = 0

  for (const message of messages) {
    const numeric = Number(message.id)

    if (Number.isInteger(numeric) && numeric > max) {
      max = numeric
    }
  }

  return max
}

export function createSessionChangesController(messages: readonly ChatMessage[]): SessionChangesController {
  return {
    cursor: maxCommittedMessageId(messages),
    disabled: false,
    renderedIds: new Set(messages.map(message => message.id))
  }
}

export function isFeatureDisabledError(error: unknown): boolean {
  const text = error instanceof Error ? error.message : String(error)

  return /session changes disabled|feature.?disabled|disabled/i.test(text)
}

export function appendFetchedMessages(
  current: readonly ChatMessage[],
  fetchedRows: readonly SessionMessage[]
): { cursor: number; messages: ChatMessage[]; renderedIds: Set<string> } {
  const renderedIds = new Set(current.map(message => message.id))
  const newRows = fetchedRows.filter(row => row.id === undefined || !renderedIds.has(String(row.id)))
  const materialized = toChatMessages([...newRows])
  const appendable = materialized.filter(message => !renderedIds.has(message.id))

  for (const message of appendable) {
    renderedIds.add(message.id)
  }

  return {
    cursor: advanceCursorAfterRows(0, fetchedRows, appendable, renderedIds),
    messages: [...current, ...appendable],
    renderedIds
  }
}

export function advanceCursorAfterRows(
  previousCursor: number,
  fetchedRows: readonly SessionMessage[],
  renderedMessages: readonly Pick<ChatMessage, 'id'>[],
  renderedIds = new Set(renderedMessages.map(message => message.id))
): number {
  let cursor = previousCursor

  for (const row of fetchedRows) {
    const id = Number(row.id)

    if (!Number.isInteger(id) || id <= cursor) {
      continue
    }

    if (!renderedIds.has(String(id))) {
      break
    }

    cursor = id
  }

  return cursor
}

function isWindowFocused(): boolean {
  return typeof document.hasFocus === 'function' ? document.hasFocus() : true
}

export function useSessionChanges({
  activeSessionId,
  currentView,
  messages,
  requestGateway,
  statusSnapshot,
  updateSessionState
}: UseSessionChangesArgs) {
  const supported = sessionChangesSupported(statusSnapshot)
  const timing = useMemo(() => sessionChangesTiming(statusSnapshot), [statusSnapshot])
  const controllerRef = useRef<SessionChangesController>(createSessionChangesController(messages))
  const pollTimerRef = useRef<number | null>(null)
  const refocusTimerRef = useRef<number | null>(null)
  const focusedRef = useRef(isWindowFocused())
  const inFlightRef = useRef(false)
  const sessionIdRef = useRef(activeSessionId)

  const eligible = Boolean(activeSessionId && currentView === 'chat' && supported && focusedRef.current)

  useEffect(() => {
    if (sessionIdRef.current === activeSessionId) {
      controllerRef.current.renderedIds = new Set(messages.map(message => message.id))

      return
    }

    sessionIdRef.current = activeSessionId
    controllerRef.current = createSessionChangesController(messages)
  }, [activeSessionId, messages])

  const clearPollTimer = useCallback(() => {
    if (pollTimerRef.current !== null) {
      window.clearInterval(pollTimerRef.current)
      pollTimerRef.current = null
    }
  }, [])

  const pollOnce = useCallback(async () => {
    const sessionId = sessionIdRef.current

    if (!sessionId || controllerRef.current.disabled || inFlightRef.current) {
      return
    }

    inFlightRef.current = true

    try {
      const since = controllerRef.current.cursor
      const response = await requestGateway<SessionChangesResponse>('session.changes', {
        session_id: sessionId,
        since_message_id: since
      })
      const rows = response.messages ?? []

      updateSessionState(sessionId, state => {
        const result = appendFetchedMessages(state.messages, rows)
        const cursor = advanceCursorAfterRows(since, rows, result.messages, result.renderedIds)

        controllerRef.current.cursor = cursor
        controllerRef.current.renderedIds = result.renderedIds

        return result.messages === state.messages ? state : { ...state, messages: result.messages }
      })
    } catch (error) {
      if (isFeatureDisabledError(error)) {
        controllerRef.current.disabled = true
        clearPollTimer()
        console.info('session.changes disabled; live session sync stopped')
      }
    } finally {
      inFlightRef.current = false
    }
  }, [clearPollTimer, requestGateway, updateSessionState])

  const schedulePollTimer = useCallback(() => {
    clearPollTimer()

    if (!eligible || controllerRef.current.disabled) {
      return
    }

    pollTimerRef.current = window.setInterval(() => void pollOnce(), timing.pollIntervalMs)
  }, [clearPollTimer, eligible, pollOnce, timing.pollIntervalMs])

  useEffect(() => {
    schedulePollTimer()

    return clearPollTimer
  }, [clearPollTimer, schedulePollTimer])

  useEffect(() => {
    const onBlur = () => {
      focusedRef.current = false
      clearPollTimer()
    }

    const onFocus = () => {
      focusedRef.current = true

      if (refocusTimerRef.current !== null) {
        window.clearTimeout(refocusTimerRef.current)
      }

      refocusTimerRef.current = window.setTimeout(() => {
        refocusTimerRef.current = null
        schedulePollTimer()

        if (sessionIdRef.current && currentView === 'chat' && supported && !controllerRef.current.disabled) {
          void pollOnce()
        }
      }, timing.refocusDebounceMs)
    }

    window.addEventListener('blur', onBlur)
    window.addEventListener('focus', onFocus)

    return () => {
      window.removeEventListener('blur', onBlur)
      window.removeEventListener('focus', onFocus)
      clearPollTimer()

      if (refocusTimerRef.current !== null) {
        window.clearTimeout(refocusTimerRef.current)
        refocusTimerRef.current = null
      }
    }
  }, [clearPollTimer, currentView, pollOnce, schedulePollTimer, supported, timing.refocusDebounceMs])

  return {
    pollOnce
  }
}
