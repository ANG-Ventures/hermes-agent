import { act, cleanup, render } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage } from '@/lib/chat-messages'
import type { ClientSessionState } from '@/app/types'
import type { StatusResponse } from '@/types/hermes'

import {
  advanceCursorAfterRows,
  appendFetchedMessages,
  createSessionChangesController,
  maxCommittedMessageId,
  sessionChangesSupported,
  useSessionChanges
} from './use-session-changes'

const SID = 'session-1'

function message(id: string, role: ChatMessage['role'] = 'user'): ChatMessage {
  return {
    id,
    role,
    parts: [{ type: 'text', text: `${role}:${id}` }]
  }
}

function status(capabilities: Record<string, unknown> = { session_changes: true }): StatusResponse {
  return {
    active_sessions: 0,
    capabilities,
    config_path: '',
    config_version: 1,
    env_path: '',
    gateway_exit_reason: null,
    gateway_health_url: null,
    gateway_pid: null,
    gateway_platforms: {},
    gateway_running: true,
    gateway_state: 'running',
    gateway_updated_at: null,
    hermes_home: '',
    latest_config_version: 1,
    release_date: '',
    version: 'test'
  } as StatusResponse
}

function setFocused(focused: boolean) {
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => focused })
}

interface HarnessProps {
  activeSessionId?: string | null
  currentView?: string
  initialMessages?: ChatMessage[]
  requestGateway: ReturnType<typeof vi.fn>
  statusSnapshot?: StatusResponse | null
}

function Harness({
  activeSessionId = SID,
  currentView = 'chat',
  initialMessages = [message('4')],
  requestGateway,
  statusSnapshot = status()
}: HarnessProps) {
  const state: ClientSessionState = {
    awaitingResponse: false,
    branch: '',
    busy: false,
    cwd: '',
    fast: false,
    interrupted: false,
    messages: initialMessages,
    model: '',
    needsInput: false,
    pendingBranchGroup: null,
    personality: '',
    provider: '',
    reasoningEffort: '',
    sawAssistantPayload: false,
    serviceTier: '',
    storedSessionId: SID,
    streamId: null,
    turnStartedAt: null,
    yolo: false
  }

  useSessionChanges({
    activeSessionId,
    currentView,
    messages: initialMessages,
    requestGateway,
    statusSnapshot,
    updateSessionState: (_sessionId, updater) => updater(state)
  })

  return null
}

beforeEach(() => {
  vi.useFakeTimers()
  setFocused(true)
})

afterEach(() => {
  cleanup()
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('useSessionChanges B1', () => {
  it('gates polling on the session_changes capability', () => {
    expect(sessionChangesSupported(status())).toBe(true)
    expect(sessionChangesSupported(status({}))).toBe(false)

    const requestGateway = vi.fn(async () => ({ messages: [], last_id: 0 }))
    render(createElement(Harness, { requestGateway, statusSnapshot: status({}) }))

    act(() => vi.advanceTimersByTime(10_000))

    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('initializes the in-memory cursor from the loaded transcript max id', () => {
    expect(maxCommittedMessageId([message('1'), message('12'), message('temp-user')])).toBe(12)
    expect(createSessionChangesController([]).cursor).toBe(0)
  })

  it('stops polling while blurred and coalesces refocus to exactly one immediate poll', async () => {
    const requestGateway = vi.fn(async () => ({ messages: [], last_id: 4 }))

    render(createElement(Harness, { requestGateway }))

    act(() => {
      window.dispatchEvent(new Event('blur'))
      vi.advanceTimersByTime(7_500)
    })

    expect(requestGateway).not.toHaveBeenCalled()

    await act(async () => {
      window.dispatchEvent(new Event('focus'))
      window.dispatchEvent(new Event('focus'))
      vi.advanceTimersByTime(999)
      await Promise.resolve()
    })

    expect(requestGateway).not.toHaveBeenCalled()

    await act(async () => {
      vi.advanceTimersByTime(1)
      await Promise.resolve()
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(requestGateway).toHaveBeenCalledWith('session.changes', {
      session_id: SID,
      since_message_id: 4
    })
  })

  it('stops quietly on feature-disabled errors without advancing the cursor', async () => {
    const info = vi.spyOn(console, 'info').mockImplementation(() => undefined)
    const requestGateway = vi.fn(async () => {
      throw new Error('session changes disabled')
    })

    render(createElement(Harness, { requestGateway }))

    await act(async () => {
      vi.advanceTimersByTime(2_500)
      await Promise.resolve()
    })

    await act(async () => {
      vi.advanceTimersByTime(10_000)
      await Promise.resolve()
    })

    expect(requestGateway).toHaveBeenCalledTimes(1)
    expect(info).toHaveBeenCalledTimes(1)
  })

  it('does not advance the cursor over unfetched or unrendered rows', () => {
    expect(
      advanceCursorAfterRows(
        9,
        [
          { id: 10, role: 'user', content: 'own' },
          { id: 11, role: 'assistant', content: 'remote' }
        ],
        [message('10')]
      )
    ).toBe(10)
  })
})

describe('useSessionChanges B2 materialization', () => {
  it('dedupes already-rendered committed ids and appends new rows through the resume materializer', () => {
    const result = appendFetchedMessages([message('10', 'user')], [
      { id: 10, role: 'user', content: 'already rendered' },
      { id: 11, role: 'assistant', content: 'new assistant', timestamp: 11 }
    ])

    expect(result.messages).toHaveLength(2)
    expect(result.messages.map(row => row.id)).toEqual(['10', '11'])
    expect(result.messages[1]?.parts).toEqual([{ type: 'text', text: 'new assistant' }])
  })
})

describe('useSessionChanges B3 partial turns', () => {
  it('renders a polled assistant tool-call prefix as the existing pending tool-call shape', () => {
    const result = appendFetchedMessages([], [
      {
        id: 20,
        role: 'assistant',
        content: '',
        tool_calls: [
          {
            id: 'call-1',
            function: { name: 'search_files', arguments: { query: 'needle' } }
          }
        ]
      }
    ])

    expect(result.messages).toHaveLength(1)
    expect(result.messages[0]?.id).toBe('20')
    const [part] = result.messages[0]?.parts ?? []

    expect(part).toEqual(
      expect.objectContaining({
        toolCallId: 'call-1',
        toolName: 'search_files',
        type: 'tool-call'
      })
    )
    expect(part && 'result' in part).toBe(false)
  })

  it('keeps the cursor at the last rendered/deduped id when a fetched row is not rendered', () => {
    expect(
      advanceCursorAfterRows(
        20,
        [
          { id: 21, role: 'assistant', content: 'rendered' },
          { id: 22, role: 'assistant', content: 'not rendered yet' }
        ],
        [message('21', 'assistant')]
      )
    ).toBe(21)
  })
})
