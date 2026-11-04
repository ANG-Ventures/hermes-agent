#!/usr/bin/env python3
"""Manual copied-ledger replay for proof-pruning acceptance.

SCHEDULE: manual (implementer/reviewer acceptance only)
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import threading
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    os.environ['HERMES_HOME'] = str(args.out.resolve() / 'home')
    os.environ['HERMES_DISABLE_LAZY_INSTALLS'] = '1'
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from cron import executions as e, occurrences as o
    logging.disable(logging.CRITICAL)
    e.EXECUTIONS_FILE = args.out.resolve() / 'home/cron/executions.db'
    e.EXECUTIONS_FILE.parent.mkdir(parents=True)
    shutil.copyfile(args.snapshot, e.EXECUTIONS_FILE)
    assert e._ledger_path() != args.snapshot.resolve()
    # Fixed stamps/ids make exact reader-output diffs independent of scheduling and wall clock.
    fixed = datetime(2026, 11, 4, 12, tzinfo=timezone.utc)
    e._hermes_now = lambda: fixed
    e._PROCESS_ID = 'replay'
    e._process_start_time = lambda pid: 1
    e._emit_execution_state = lambda *a, **k: None
    e.record_cron_finish = lambda *a, **k: None
    class IDs:
        n = 0
        def __call__(self):
            self.n += 1
            return type('ID', (), {'hex': 'replay-%05d' % self.n})()
    e.uuid.uuid4 = IDs()
    with e._transaction() as conn:
        jobs = [r[0] for r in conn.execute('SELECT DISTINCT job_id FROM executions ORDER BY job_id')]
        counts = dict(conn.execute('SELECT status,count(*) FROM executions GROUP BY status'))
        sync = conn.execute('PRAGMA synchronous').fetchone()[0]
    assert sync == 2
    pending = [e.create_execution(jobs[i % len(jobs)], source='replay',
               scheduled_instant=fixed.isoformat())['id'] for i in range(120)]
    holds = []
    local = threading.local()
    transaction = e._transaction
    @contextmanager
    def measured():
        # Start the clock only after acquiring the actual RLock, include connection/commit.
        with e._lock:
            start = time.perf_counter()
            try:
                with transaction() as conn:
                    yield conn
            finally:
                if getattr(local, 'finishing', False):
                    holds.append(time.perf_counter() - start)
    e._transaction = measured
    gate = threading.Event()
    def finish(execution):
        gate.wait()
        local.finishing = True
        try:
            e.finish_execution(execution, success=True)
        finally:
            local.finishing = False
    start = time.perf_counter()
    with ThreadPoolExecutor(max_workers=24) as pool:
        futures = [pool.submit(finish, execution) for execution in pending]
        gate.set()
        for i in range(150):
            job = {'id': jobs[i % len(jobs)]}
            slot = fixed.isoformat()
            o.completed_occurrence_at(job, slot)
            o.rewound_watermark(job, slot)
            row = e.create_execution(job['id'], source='replay')
            e.set_execution_occurrence(row['id'], slot)
        claim_wall = time.perf_counter() - start
        for f in futures:
            f.result()
    wall = time.perf_counter() - start
    queries = []
    with e._transaction() as conn:
        for row in conn.execute('SELECT DISTINCT job_id,scheduled_instant FROM executions '
                                'WHERE scheduled_instant IS NOT NULL ORDER BY job_id,scheduled_instant'):
            queries.append(tuple(row))
    reads = []
    for job, slot in queries:
        reads.append([job, slot, o.completed_occurrence_at({'id': job}, slot),
                      o.rewound_watermark({'id': job}, slot)])
    for job in jobs:
        reads.append([job, '2000-01-01T00:00:00+00:00',
                      o.rewound_watermark({'id': job}, '2000-01-01T00:00:00+00:00')])
    (args.out / 'occurrence-reads.json').write_text(json.dumps(reads, sort_keys=True, indent=2)+'\n')
    with e._transaction() as conn:
        columns = [r[1] for r in conn.execute('PRAGMA table_info(executions)') if r[1] != 'rewind_proof']
        rows = [dict(zip(columns, row)) for row in conn.execute(
            'SELECT '+','.join(columns)+' FROM executions ORDER BY id')]
    # Pids are local to each replay, not reader-visible identities.
    for row in rows:
        if row['process_id'] == 'replay':
            row['pid'] = 0
    (args.out / 'retained-rows.json').write_text(json.dumps(rows, sort_keys=True, indent=2)+'\n')
    holds.sort()
    result = dict(pid=os.getpid(), snapshot_rows=sum(counts.values()), snapshot_status=counts,
                  claims=150, finishes=len(holds), synchronous=sync, wall_s=wall,
                  claims_wall_s=claim_wall, hold_p50_ms=holds[len(holds)//2]*1000,
                  hold_p90_ms=holds[int(len(holds)*.9)]*1000, hold_ms=[v*1000 for v in holds],
                  reader_queries=len(reads))
    (args.out / 'measurements.json').write_text(json.dumps(result, indent=2)+'\n')
    print(json.dumps({k:v for k,v in result.items() if k != 'hold_ms'}, sort_keys=True))
    e.close_shared_connections()


if __name__ == '__main__':
    main()
