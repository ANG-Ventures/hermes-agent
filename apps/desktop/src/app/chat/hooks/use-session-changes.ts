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
  busy: boolean
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
  suspended: boolean
  unstampedOptimisticIds: Set<string>
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
    renderedIds: new Set(messages.map(message => message.id)),
    suspended: false,
    unstampedOptimisticIds: new Set()
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
    messages: orderCommittedMessages([...current, ...appendable]),
    renderedIds
  }
}

export function orderCommittedMessages(messages: ChatMessage[]): ChatMessage[] {
  return [...messages].sort((a, b) => {
    const left = Number(a.id)
    const right = Number(b.id)
    const leftCommitted = Number.isInteger(left)
    const rightCommitted = Number.isInteger(right)

    if (leftCommitted && rightCommitted) {
      return left - right
    }

    if (leftCommitted !== rightCommitted) {
      return leftCommitted ? -1 : 1
    }

    return 0
  })
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

function optimisticTranscriptIds(messages: readonly ChatMessage[]): string[] {
  return messages
    .filter(message => !Number.isInteger(Number(message.id)) && (message.id.startsWith('user-') || message.pending))
    .map(message => message.id)
}

export function extractCommittedMessageIds(payload: unknown): string[] {
  const row = payload && typeof payload === 'object' ? (payload as Record<string, unknown>) : {}
  const arrayValue = row.message_ids ?? row.row_ids ?? row.committed_ids
  const ids = Array.isArray(arrayValue) ? arrayValue : []
  const scalarIds = [row.user_message_id, row.assistant_message_id, row.message_id]

  return [...ids, ...scalarIds]
    .map(value => (value === undefined || value === null || value === '' ? '' : String(value)))
    .filter(Boolean)
}

export function stampOptimisticTranscriptRows(messages: readonly ChatMessage[], committedIds: readonly string[]) {
  if (!committedIds.length) {
    return {
      messages: [...messages],
      stampedIds: new Set<string>()
    }
  }

  const stampedIds = new Set<string>()
  let nextIdIndex = 0
  const next = messages.map(message => {
    if (Number.isInteger(Number(message.id)) || nextIdIndex >= committedIds.length) {
      return message
    }

    if (!message.id.startsWith('user-') && !message.pending) {
      return message
    }

    const id = committedIds[nextIdIndex]
    nextIdIndex += 1
    stampedIds.add(id)

    return { ...message, id, pending: false }
  })

  return { messages: next, stampedIds }
}

function isWindowFocused(): boolean {
  return typeof document.hasFocus === 'function' ? document.hasFocus() : true
}

export function useSessionChanges({
  activeSessionId,
  busy,
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
  const wasBusyRef = useRef(busy)

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

    if (!sessionId || controllerRef.current.disabled || controllerRef.current.suspended || inFlightRef.current) {
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

    if (!eligible || controllerRef.current.disabled || controllerRef.current.suspended) {
      return
    }

    pollTimerRef.current = window.setInterval(() => void pollOnce(), timing.pollIntervalMs)
  }, [clearPollTimer, eligible, pollOnce, timing.pollIntervalMs])

  useEffect(() => {
    if (busy === wasBusyRef.current) {
      return
    }

    wasBusyRef.current = busy

    if (busy) {
      controllerRef.current.suspended = true
      controllerRef.current.unstampedOptimisticIds = new Set(optimisticTranscriptIds(messages))
      clearPollTimer()

      return
    }

    controllerRef.current.suspended = false
    schedulePollTimer()

    if (eligible && !controllerRef.current.disabled) {
      void pollOnce()
    }
  }, [busy, clearPollTimer, eligible, messages, pollOnce, schedulePollTimer])

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
    markFrame: useCallback(() => undefined, []),
    markTurnComplete: useCallback(
      (sessionId: string, payload: unknown) => {
        if (!sessionId || sessionId !== sessionIdRef.current) {
          return
        }

        const committedIds = extractCommittedMessageIds(payload)

        if (!committedIds.length) {
          return
        }

        updateSessionState(sessionId, state => {
          const stamped = stampOptimisticTranscriptRows(state.messages, committedIds)

          controllerRef.current.renderedIds = new Set(stamped.messages.map(message => message.id))
          controllerRef.current.unstampedOptimisticIds.clear()

          return { ...state, messages: stamped.messages }
        })
      },
      [updateSessionState]
    ),
    pollOnce
  }
}
