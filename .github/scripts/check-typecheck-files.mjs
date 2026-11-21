// Fail when a workspace's `typecheck` script runs a tsc that checks no file.
//
// `tsc -p <config>` on a solution-style config ("files": [] plus only
// "references") compiles nothing without -b and exits 0. web's typecheck did
// that and passed a tree whose real build failed (hermes-agent#514). This
// reruns every tsc command of every workspace `typecheck` script with
// --listFilesOnly (no type check, no emit) and counts the source files outside
// node_modules. A command that lists zero is an error.
//
// `tsc -b <projects>` cannot take --listFilesOnly, so each project of a -b
// command and every project it references is listed with -p and the counts are
// summed.
//
// Runs on a laptop too: `node .github/scripts/check-typecheck-files.mjs`.
// `--table` prints a markdown table of workspace, command, files.

import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync, statSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, join, resolve } from 'node:path'

const NPM = process.platform === 'win32' ? 'npm.cmd' : 'npm'

function workspaces() {
  const raw = execFileSync(NPM, ['query', '.workspace'], {
    encoding: 'utf-8',
    shell: process.platform === 'win32'
  })
  return JSON.parse(raw)
}

function tscFor(pkgDir) {
  return createRequire(join(pkgDir, 'package.json')).resolve('typescript/bin/tsc')
}

function listFiles(tsc, cwd, args) {
  let out
  try {
    out = execFileSync(process.execPath, [tsc, ...args, '--listFilesOnly'], {
      cwd,
      encoding: 'utf-8',
      maxBuffer: 256 * 1024 * 1024
    })
  } catch (err) {
    // stdout is the file list; keep only the diagnostics, which tsc prints first.
    const diag = String(err.stdout ?? '')
      .split('\n')
      .filter(l => /error TS\d+/.test(l))
    throw new Error(`tsc ${args.join(' ')} --listFilesOnly failed in ${cwd}:\n${diag.join('\n')}${err.stderr ?? ''}`)
  }
  return out
    .split('\n')
    .map(l => l.trim())
    .filter(l => l && !/[\\/]node_modules[\\/]/.test(l))
}

function configPath(cwd, p) {
  const abs = resolve(cwd, p)
  return existsSync(abs) && statSync(abs).isDirectory() ? join(abs, 'tsconfig.json') : abs
}

// Plain JSON plus comments and trailing commas, which tsconfig allows.
function readConfig(file) {
  const text = readFileSync(file, 'utf-8')
    .replace(/("(?:\\.|[^"\\])*")|\/\*[\s\S]*?\*\/|\/\/[^\n]*/g, (m, str) => str ?? '')
    .replace(/,(\s*[}\]])/g, '$1')
  return JSON.parse(text)
}

// Every project a `tsc -b` of `file` builds: itself and its references, recursively.
function buildProjects(file, seen = new Set()) {
  if (seen.has(file)) return seen
  seen.add(file)
  for (const ref of readConfig(file).references ?? []) buildProjects(configPath(dirname(file), ref.path), seen)
  return seen
}

/** Count the source files one tsc command (argv after `tsc`) would check. */
function countCommand(tsc, cwd, args) {
  const build = args[0] === '-b' || args[0] === '--build'
  if (!build) {
    return listFiles(tsc, cwd, args).length
  }
  const projects = args.slice(1).filter(a => !a.startsWith('-'))
  let total = 0
  for (const p of projects.length ? projects : ['.']) {
    for (const cfg of buildProjects(configPath(cwd, p))) total += listFiles(tsc, cwd, ['-p', cfg, '--noEmit']).length
  }
  return total
}

function main() {
  const table = process.argv.includes('--table')
  const rows = []
  const failures = []
  for (const pkg of workspaces()) {
    const script = pkg.scripts?.typecheck
    if (!script) continue
    const cwd = resolve(pkg.location)
    for (const segment of script.split('&&').map(s => s.trim())) {
      const argv = segment.split(/\s+/)
      if (argv[0] !== 'tsc') continue
      const files = countCommand(tscFor(cwd), cwd, argv.slice(1))
      rows.push({ pkg: pkg.location, cmd: segment, files })
      if (files === 0) failures.push(`${pkg.location}: \`${segment}\` checks 0 files`)
    }
  }

  if (table) {
    console.log('| workspace | typecheck command | files checked |')
    console.log('|---|---|---|')
    for (const r of rows) console.log(`| ${r.pkg} | \`${r.cmd}\` | ${r.files} |`)
  } else {
    for (const r of rows) console.log(`${String(r.files).padStart(5)}  ${r.pkg} :: ${r.cmd}`)
  }

  if (rows.length === 0) {
    console.error('::error::No workspace typecheck script runs tsc — refusing to report green having counted nothing.')
    process.exit(1)
  }
  if (failures.length) {
    for (const f of failures) console.error(`::error::${f} (a solution-style tsconfig needs -b, or -p per reference)`)
    process.exit(1)
  }
  console.log(`all ${rows.length} tsc commands check at least one file`)
}

main()
