import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import {
  filePathFromMediaPath,
  gatewayMediaDataUrl,
  isInlineMediaSrc,
  isRemoteGateway,
  mediaExternalUrl,
  pathFromRemoteGatewayFileUrl,
  resolveMediaDisplaySrc,
  resolveMediaPlaybackSrc
} from './media'

describe('isRemoteGateway', () => {
  afterEach(() => {
    $connection.set(null)
  })

  it('is false with no connection', () => {
    $connection.set(null)
    expect(isRemoteGateway()).toBe(false)
  })

  it('is false in local mode', () => {
    $connection.set({ mode: 'local' } as never)
    expect(isRemoteGateway()).toBe(false)
  })

  it('is true in remote mode', () => {
    $connection.set({ mode: 'remote' } as never)
    expect(isRemoteGateway()).toBe(true)
  })
})

describe('filePathFromMediaPath', () => {
  it('passes through a plain path', () => {
    expect(filePathFromMediaPath('/home/u/.hermes/images/a.png')).toBe('/home/u/.hermes/images/a.png')
  })

  it('decodes a file:// URL with encoded characters', () => {
    expect(filePathFromMediaPath('file:///tmp/a%20b.png')).toBe('/tmp/a b.png')
  })
})

describe('mediaExternalUrl', () => {
  afterEach(() => {
    $connection.set(null)
  })

  it('passes through http(s) URLs untouched', () => {
    $connection.set({ mode: 'remote', baseUrl: 'https://gw', token: 't' } as never)
    expect(mediaExternalUrl('https://example.com/a.png')).toBe('https://example.com/a.png')
  })

  it('keeps file:// form in local mode', () => {
    $connection.set({ mode: 'local' } as never)
    expect(mediaExternalUrl('/tmp/a.png')).toBe('file:///tmp/a.png')
    expect(mediaExternalUrl('file:///tmp/a.png')).toBe('file:///tmp/a.png')
  })

  it('routes gateway-local paths through the bytes-to-temp opener even when a token exists', () => {
    $connection.set({ mode: 'remote', baseUrl: 'https://gw', token: 's e/cret' } as never)

    const fromFile = mediaExternalUrl('file:///tmp/a b.png')
    const fromPath = mediaExternalUrl('/tmp/a b.png')

    expect(fromFile.startsWith('hermes-gateway-file://open?')).toBe(true)
    expect(fromPath.startsWith('hermes-gateway-file://open?')).toBe(true)
    expect(fromFile).not.toContain('/api/files/download')
    expect(fromFile).not.toContain('token=')
    expect(pathFromRemoteGatewayFileUrl(fromFile)).toEqual({ path: '/tmp/a b.png' })
    expect(pathFromRemoteGatewayFileUrl(fromPath)).toEqual({ path: '/tmp/a b.png' })
  })

  it('never falls back to file:// when a remote connection lacks a token', () => {
    $connection.set({ mode: 'remote', baseUrl: 'https://gw', profile: 'mbp' } as never)

    const url = mediaExternalUrl('/tmp/a.png')

    expect(url).toBe('hermes-gateway-file://open?path=%2Ftmp%2Fa.png&profile=mbp')
    expect(url).not.toMatch(/^file:/)
    expect(pathFromRemoteGatewayFileUrl(url)).toEqual({ path: '/tmp/a.png', profile: 'mbp' })
  })
})

describe('resolveMediaDisplaySrc', () => {
  const api = vi.fn(async ({ path }: { path: string }) => {
    if (path.startsWith('/api/fs/read-data-url?')) {
      return { dataUrl: 'data:image/png;base64,ZHVtbXk=' }
    }

    throw new Error(`unexpected path ${path}`)
  })

  beforeEach(() => {
    api.mockClear()
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    $connection.set(null)
  })

  it('recognizes inline image URLs', () => {
    expect(isInlineMediaSrc('https://example.com/a.png')).toBe(true)
    expect(isInlineMediaSrc('data:image/png;base64,ZHVtbXk=')).toBe(true)
    expect(isInlineMediaSrc('/Users/me/a.png')).toBe(false)
  })

  it('leaves web, data, and relative markdown image sources unchanged', async () => {
    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ mode: 'remote', profile: 'remote-work' } as never)

    await expect(resolveMediaDisplaySrc('https://example.com/a.png')).resolves.toBe('https://example.com/a.png')
    await expect(resolveMediaDisplaySrc('data:image/png;base64,ZHVtbXk=')).resolves.toBe(
      'data:image/png;base64,ZHVtbXk='
    )
    await expect(resolveMediaDisplaySrc('images/a.png')).resolves.toBe('images/a.png')
    await expect(resolveMediaDisplaySrc('./images/a.png')).resolves.toBe('./images/a.png')
    await expect(resolveMediaDisplaySrc('../images/a.png')).resolves.toBe('../images/a.png')
    expect(api).not.toHaveBeenCalled()
  })

  it('reads remote gateway-local file paths through the desktop fs bridge', async () => {
    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ mode: 'remote', profile: 'remote-work' } as never)

    await expect(resolveMediaDisplaySrc('/Users/me/project/a b.png')).resolves.toBe('data:image/png;base64,ZHVtbXk=')
    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2FUsers%2Fme%2Fproject%2Fa%20b.png',
      profile: 'remote-work'
    })
  })

  it('reads local desktop file paths from the local desktop shell', async () => {
    const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,bG9jYWw=')

    vi.stubGlobal('window', { hermesDesktop: { readFileDataUrl } })
    $connection.set({ mode: 'local' } as never)

    await expect(resolveMediaDisplaySrc('file:///Users/me/project/a%20b.png')).resolves.toBe(
      'data:image/png;base64,bG9jYWw='
    )
    expect(readFileDataUrl).toHaveBeenCalledWith('/Users/me/project/a b.png')
  })
})

describe('resolveMediaPlaybackSrc', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    $connection.set(null)
  })

  it('keeps a remote HTTPS video URL unchanged', async () => {
    vi.stubGlobal('window', { hermesDesktop: { api: vi.fn() } })
    $connection.set({ mode: 'remote', baseUrl: 'https://gateway.test', token: 'secret' } as never)

    await expect(resolveMediaPlaybackSrc('https://cdn.example.com/render.mp4')).resolves.toBe(
      'https://cdn.example.com/render.mp4'
    )
  })

  it('routes gateway-local video through the authenticated download endpoint', async () => {
    vi.stubGlobal('window', { hermesDesktop: { api: vi.fn() } })
    $connection.set({ mode: 'remote', baseUrl: 'https://gateway.test', token: 's e/cret' } as never)

    await expect(resolveMediaPlaybackSrc('/root/outputs/render.mp4')).resolves.toBe(
      'https://gateway.test/api/files/download?path=%2Froot%2Foutputs%2Frender.mp4&token=s%20e%2Fcret'
    )
  })

  it('uses the Electron streaming protocol for local desktop video', async () => {
    vi.stubGlobal('window', { hermesDesktop: { api: vi.fn() } })
    $connection.set({ mode: 'local' } as never)

    await expect(resolveMediaPlaybackSrc('C:\\renders\\demo.mp4')).resolves.toBe(
      'hermes-media://stream/C%3A%5Crenders%5Cdemo.mp4'
    )
  })
})

describe('gatewayMediaDataUrl', () => {
  const api = vi.fn(async ({ path }: { path: string }) => {
    if (path.startsWith('/api/fs/read-data-url?')) {
      return { dataUrl: 'data:image/png;base64,ZHVtbXk=' }
    }

    throw new Error(`unexpected path ${path}`)
  })

  beforeEach(() => {
    api.mockClear()
    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ mode: 'remote' } as never)
  })

  afterEach(() => {
    vi.unstubAllGlobals()
    $connection.set(null)
  })

  it('reads gateway media through the desktop fs bridge instead of /api/media roots', async () => {
    const url = await gatewayMediaDataUrl('/home/u/.hermes/skills/demo/images/a b.png')

    expect(url).toBe('data:image/png;base64,ZHVtbXk=')
    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2Fhome%2Fu%2F.hermes%2Fskills%2Fdemo%2Fimages%2Fa%20b.png'
    })
  })
})
