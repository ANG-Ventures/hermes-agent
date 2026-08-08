'use client'

import type { SyntaxHighlighterProps } from '@assistant-ui/react-streamdown'
import { type ComponentProps, type FC, lazy, Suspense, useMemo } from 'react'
import type ShikiHighlighter from 'react-shiki'

import { CodeCard, CodeCardBody } from '@/components/chat/code-card'
import { ExpandableBlock } from '@/components/chat/expandable-block'
import { SHIKI_COLOR_REPLACEMENTS, SHIKI_THEME, useCachedShikiHtml } from '@/components/chat/shiki-html-cache'
import { CopyButton } from '@/components/ui/copy-button'
import { useI18n } from '@/i18n'
import { isLikelyProseCodeBlock } from '@/lib/markdown-code'

/**
 * Streamdown's code adapter renders header + body as inline siblings, so we
 * own the wrapping `<CodeCard>` here and neutralize the upstream
 * `data-streamdown="code-block"` chrome from styles.css. The card is
 * background-only — no header row, no language label — so a fence reads as a
 * tinted slab of the reply; copy is a hover-reveal control in the corner.
 *
 * Highlighting is cache-first (shiki-html-cache): a fence that has been
 * highlighted before this app run renders its colored HTML in the SAME commit
 * it mounts — remounts (session switch wholesale-replace, idle budget raise)
 * never show the plain->highlighted swap. Only the first-ever render of a
 * given fence tokenizes async (plain code shows for that one pass).
 */
interface HermesSyntaxHighlighterProps extends SyntaxHighlighterProps {
  defer?: boolean
}

export { SHIKI_COLOR_REPLACEMENTS, SHIKI_THEME }

const MAX_HIGHLIGHT_CHARS = 150_000
const MAX_HIGHLIGHT_LINES = 3_000
const CHUNK_LINES = 200
const EST_LINE_PX = 16

// react-shiki (and through it the multi-MB shiki grammar/theme bundle) is the
// heaviest dependency in the renderer. `shiki-block.tsx` is its only static
// importer, so this lazy() is the single seam that keeps shiki out of the
// entry chunk — it loads on the first highlighted code block, not at boot.
const ShikiBlock = lazy(() => import('./shiki-block'))

/** Drop-in ShikiHighlighter that suspends on first use and renders the code
 *  as plain preformatted text until the shiki chunk arrives. */
export const LazyShiki: FC<ComponentProps<typeof ShikiHighlighter>> = props => (
  <Suspense fallback={<PlainCode code={String(props.children ?? '')} />}>
    <ShikiBlock {...props} />
  </Suspense>
)

export function exceedsHighlightBudget(code: string): boolean {
  if (code.length > MAX_HIGHLIGHT_CHARS) {
    return true
  }

  let lines = 1
  let idx = code.indexOf('\n')

  while (idx !== -1) {
    if ((lines += 1) > MAX_HIGHLIGHT_LINES) {
      return true
    }

    idx = code.indexOf('\n', idx + 1)
  }

  return false
}

interface CodeChunk {
  text: string
  lines: number
}

export function chunkByLines(code: string, perChunk: number): CodeChunk[] {
  const lines = code.split('\n')

  if (lines.length <= perChunk) {
    return [{ text: code, lines: lines.length }]
  }

  const chunks: CodeChunk[] = []

  for (let i = 0; i < lines.length; i += perChunk) {
    const slice = lines.slice(i, i + perChunk)
    chunks.push({ text: slice.join('\n'), lines: slice.length })
  }

  return chunks
}

const PlainCode: FC<{ code: string }> = ({ code }) => {
  const chunks = useMemo(() => chunkByLines(code, CHUNK_LINES), [code])

  if (chunks.length === 1) {
    return <code className="block whitespace-pre">{code}</code>
  }

  return (
    <>
      {chunks.map((chunk, index) => (
        <code
          className="block whitespace-pre [content-visibility:auto]"
          key={index}
          style={{ containIntrinsicSize: `auto ${chunk.lines * EST_LINE_PX}px` }}
        >
          {chunk.text}
        </code>
      ))}
    </>
  )
}

// Renders the cache-first highlighted HTML; falls back to plain code while the
// first-ever tokenize of this fence is in flight (or if it failed).
const CachedHighlight: FC<{ code: string; language: string }> = ({ code, language }) => {
  const { html } = useCachedShikiHtml(code, language)

  if (html === null) {
    return <PlainCode code={code} />
  }

  // Shiki output: static HTML from our own tokenizer over escaped code — the
  // standard shiki consumption pattern (upstream react-shiki does the same).

  return <div dangerouslySetInnerHTML={{ __html: html }} />
}

export const SyntaxHighlighter: FC<HermesSyntaxHighlighterProps> = ({
  components: { Pre },
  language,
  code,
  defer = false
}) => {
  const { t } = useI18n()
  const trimmed = (code ?? '').replace(/^\n+/, '').trimEnd()

  // Streaming may hand us empty/incomplete fences — render nothing rather
  // than a transient empty card.
  if (!trimmed.trim()) {
    return null
  }

  if (isLikelyProseCodeBlock(language, trimmed)) {
    return <div className="aui-prose-fence whitespace-pre-wrap wrap-anywhere text-foreground">{trimmed}</div>
  }

  const plain = defer || exceedsHighlightBudget(trimmed)

  return (
    <CodeCard data-streaming={defer ? 'true' : undefined}>
      <CopyButton
        appearance="inline"
        className="absolute right-1.5 top-1.5 z-10 h-5 gap-0 rounded-md px-1 opacity-0 transition-opacity group-hover/code:opacity-100 focus-visible:opacity-100"
        iconClassName="size-2.5"
        label={t.assistant.tool.copyCode}
        showLabel={false}
        text={trimmed}
      />
      <CodeCardBody className="[&_pre]:px-3 [&_pre]:py-2.5">
        <ExpandableBlock>
          <Pre className="aui-shiki m-0 overflow-hidden bg-transparent p-0">
            {plain ? <PlainCode code={trimmed} /> : <CachedHighlight code={trimmed} language={language || 'text'} />}
          </Pre>
        </ExpandableBlock>
      </CodeCardBody>
    </CodeCard>
  )
}
