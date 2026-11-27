"""Inert failure authority. Provider verification is an internal Python boundary.

No tool accepts an Observation, verifier callable, or enrollment switch. The trusted
reader verifies source captures BEFORE publish_evidence; this module persists its
attestation and rechecks durable references/policy under the board write lock.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from functools import cmp_to_key
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time

from hermes_cli.kanban_db_connect import write_txn

PROTOCOL = 1
PRODUCERS = ('main-green-streak', 'ci-red-owner', 'main-tip-red', 'landing-advisory', 'gate-C')
SCHEMA = """
CREATE TABLE IF NOT EXISTS failure_authority (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL,
 minimum_writer_version INTEGER NOT NULL, policy_generation INTEGER NOT NULL,
 mode TEXT NOT NULL CHECK(mode IN ('on','preserve')));
INSERT OR IGNORE INTO failure_authority VALUES(1,1,1,1,'preserve');
CREATE TABLE IF NOT EXISTS failure_producers (
 producer TEXT PRIMARY KEY COLLATE BINARY, enabled INTEGER NOT NULL DEFAULT 0,
 enrolled INTEGER NOT NULL DEFAULT 0, generation INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS failure_write_context (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), protocol INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS failure_receipts (
 token TEXT PRIMARY KEY, digest TEXT NOT NULL, observation_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failure_keys (
 key_id INTEGER PRIMARY KEY, canonical TEXT NOT NULL UNIQUE COLLATE BINARY,
 schema_version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS failure_observations (
 obs_id INTEGER PRIMARY KEY, source_key TEXT NOT NULL UNIQUE COLLATE BINARY,
 key_id INTEGER REFERENCES failure_keys, result TEXT NOT NULL,
 receipt TEXT NOT NULL REFERENCES failure_receipts, data TEXT NOT NULL,
 disposition TEXT NOT NULL, episode_id INTEGER, card_id TEXT, reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failure_episodes (
 episode_id INTEGER PRIMARY KEY, key_id INTEGER NOT NULL REFERENCES failure_keys,
 generation INTEGER NOT NULL, state TEXT NOT NULL CHECK(state IN ('active','healed')),
 revision INTEGER NOT NULL DEFAULT 1, latest_failure_obs INTEGER,
 healed_obs INTEGER, predecessor INTEGER REFERENCES failure_episodes,
 first_failure_obs INTEGER NOT NULL UNIQUE REFERENCES failure_observations,
 UNIQUE(key_id,generation));
CREATE UNIQUE INDEX IF NOT EXISTS failure_one_active_episode ON failure_episodes(key_id) WHERE state='active';
CREATE TABLE IF NOT EXISTS failure_bindings (
 binding_id INTEGER PRIMARY KEY, episode_id INTEGER NOT NULL REFERENCES failure_episodes,
 card_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
 state TEXT NOT NULL CHECK(state IN ('pending','bound','detached')),
 generation INTEGER NOT NULL DEFAULT 1, provenance_json TEXT NOT NULL,
 task_kind TEXT NOT NULL CHECK(task_kind IN ('repair','diagnostic')),
 detector_only INTEGER NOT NULL DEFAULT 1, reason TEXT NOT NULL DEFAULT '');
CREATE UNIQUE INDEX IF NOT EXISTS failure_one_binding ON failure_bindings(episode_id) WHERE state IN ('pending','bound');
CREATE TABLE IF NOT EXISTS failure_transitions (
 transition_id INTEGER PRIMARY KEY, episode_id INTEGER REFERENCES failure_episodes,
 observation_id INTEGER REFERENCES failure_observations, revision INTEGER,
 reason TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failure_unresolved (
 source_key TEXT PRIMARY KEY COLLATE BINARY, obs_id INTEGER REFERENCES failure_observations,
 reason TEXT NOT NULL, retry_state TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS failure_memberships (
 id INTEGER PRIMARY KEY, obs_id INTEGER NOT NULL REFERENCES failure_observations,
 episode_id INTEGER NOT NULL REFERENCES failure_episodes, revision INTEGER NOT NULL,
 UNIQUE(obs_id,episode_id,revision));
DROP TRIGGER IF EXISTS failure_owner_terminal;
CREATE TRIGGER failure_owner_terminal AFTER UPDATE OF status ON tasks
WHEN NEW.status IN ('done','archived') AND OLD.status NOT IN ('done','archived')
BEGIN
 INSERT INTO failure_transitions(episode_id,revision,reason,detail)
 SELECT e.episode_id,e.revision+1,'owner_terminal',NEW.id FROM failure_bindings b
 JOIN failure_episodes e USING(episode_id) WHERE b.card_id=NEW.id AND b.state='bound';
 UPDATE failure_episodes SET revision=revision+1 WHERE episode_id IN
 (SELECT episode_id FROM failure_bindings WHERE card_id=NEW.id AND state='bound');
 UPDATE failure_bindings SET state='detached',generation=generation+1,reason='owner_terminal'
 WHERE card_id=NEW.id AND state='bound';
 INSERT INTO failure_bindings(episode_id,state,provenance_json,task_kind,detector_only,reason)
 SELECT e.episode_id,'pending',b.provenance_json,b.task_kind,b.detector_only,'owner_terminal'
 FROM failure_episodes e JOIN failure_bindings b USING(episode_id)
 WHERE b.card_id=NEW.id AND b.reason='owner_terminal' AND e.state='active'
 AND b.binding_id=(SELECT MAX(y.binding_id) FROM failure_bindings y WHERE y.episode_id=e.episode_id AND y.card_id=NEW.id AND y.reason='owner_terminal')
 AND NOT EXISTS(SELECT 1 FROM failure_bindings x WHERE x.episode_id=e.episode_id AND x.state IN ('pending','bound'));
END;
DROP TRIGGER IF EXISTS failure_legacy_insert;
CREATE TRIGGER failure_legacy_insert BEFORE INSERT ON tasks
WHEN (EXISTS(SELECT 1 FROM failure_producers WHERE enrolled=1)
 OR EXISTS(SELECT 1 FROM failure_authority WHERE singleton=1 AND mode<>'preserve'))
 AND (NEW.created_by IN ('main-green-streak','ci-red-owner','main-tip-red','main_tip_red','main-tip-red-rollup',
 'landing-queue','landing-advisory','gate-C')
 OR NEW.signature GLOB 'main-tip-red:*' OR NEW.signature GLOB 'main-tip-red-rollup:*'
 OR NEW.idempotency_key GLOB 'ci-red-owner:*' OR NEW.idempotency_key GLOB 'main-green-streak:*'
 OR NEW.idempotency_key GLOB 'main-red:*' OR NEW.idempotency_key GLOB 'main-tip-red:*'
 OR NEW.idempotency_key GLOB 'main-tip-red-rollup:*' OR NEW.idempotency_key GLOB 'ci-advisory-landed:*')
 AND NOT EXISTS(SELECT 1 FROM failure_write_context WHERE singleton=1 AND protocol>=1)
BEGIN SELECT RAISE(ABORT,'failure_authority: incompatible writer; retain source retry'); END;
"""


def migrate(conn):
    # executescript commits before running: execute complete statements inside a
    # savepoint instead, preserving a caller's transaction and atomic trigger DDL.
    conn.execute('SAVEPOINT failure_schema_migration')
    try:
        statement = ''
        for line in SCHEMA.splitlines(keepends=True):
            statement += line
            if sqlite3.complete_statement(statement):
                conn.execute(statement)
                statement = ''
        if statement.strip():
            raise ValueError('incomplete failure authority schema')
        conn.executemany('INSERT OR IGNORE INTO failure_producers(producer) VALUES(?)',
                         ((p,) for p in PRODUCERS))
        conn.execute('RELEASE failure_schema_migration')
    except BaseException:
        conn.execute('ROLLBACK TO failure_schema_migration')
        conn.execute('RELEASE failure_schema_migration')
        raise


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


@dataclass(frozen=True)
class Observation:
    key: str | None
    source_key: str
    result: str
    sha: str
    run_id: int
    attempt: int
    job_id: int
    lineage: tuple[str, ...]
    policy_generation: int
    started_at: int | None = None
    completed_at: int | None = None
    complete: bool = False
    reason: str = ''
    retry_cursor: str = ''
    task_kind: str = 'repair'
    verifier_version: str = '1'


@dataclass(frozen=True)
class EvidenceRef:
    token: str
    digest: str


@dataclass(frozen=True)
class PreparedCard:
    title: str
    body: str
    assignee: str | None
    session_id: str
    tenant: str | None


@dataclass(frozen=True)
class Decision:
    action: str
    key_id: int | None = None
    episode_id: int | None = None
    card_id: str | None = None
    revision: int | None = None
    reason: str = ''


def prepare_card(conn, *, title, body, assignee, session_id, tenant=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli.model_policy import validate_assignee_route
    if not title.strip() or not body.strip() or not session_id:
        raise ValueError('bounded title/body and explicit home required')
    assignee = kb._canonical_assignee(assignee)
    validate_assignee_route(assignee, model_override=None, allow_flagship_reason=None)
    return PreparedCard(title.strip(), body, assignee, session_id, tenant)


def _blob_root(conn):
    path = conn.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise ValueError('durable board required')
    return Path(path).resolve().parent / 'failure-evidence'


def _validate_observation(obs):
    if type(obs) is not Observation or obs.result not in ('failure','pass','unknown','waived'):
        raise ValueError('typed verifier observation required')
    if not obs.source_key or obs.task_kind not in ('repair','diagnostic'):
        raise ValueError('source and ownership kind required')
    if obs.key is not None:
        fields = json.loads(obs.key)
        if (not isinstance(fields, list) or len(fields) != 13 or type(fields[0]) is not int
                or fields[0] != 1 or _json(fields) != obs.key):
            raise ValueError('exact canonical v1 key required')
        if not isinstance(fields[3],str) or fields[3] != fields[3].lower() or any(c in fields[3] for c in '/: '):
            raise ValueError('provider hostname required')
        if not isinstance(fields[2],str) or fields[9] not in ('suite','step','job'):
            raise ValueError('tenant and unit kind required')
        if (not isinstance(fields[6],str) or not fields[6].startswith('.github/workflows/')
                or '..' in fields[6].split('/')):
            raise ValueError('repository-relative workflow required')
        if fields[11] == 'pytest-node':
            if fields[9]=='job' or not isinstance(fields[12],str) or '::' not in fields[12]:
                raise ValueError('exact pytest node required')
        elif fields[11] == 'unlocalized-job':
            if fields[9]!='job' or fields[12]!='':
                raise ValueError('unlocalized job discriminator required')
        else:
            raise ValueError('unsupported failure parser')
        if any(not isinstance(fields[i], str) or not fields[i] for i in (1,3,6,9,10,11)):
            raise ValueError('missing identity axis')
        if any(type(fields[i]) is not int or fields[i] <= 0 for i in (4,5)):
            raise ValueError('numeric repository/workflow required')
        if not isinstance(fields[7], list) or not isinstance(fields[8], list):
            raise ValueError('explicit chain and matrix required')
        for entry in fields[7]:
            if (not isinstance(entry,list) or len(entry)!=3 or not isinstance(entry[0],str)
                    or not entry[0] or type(entry[1]) is not int or entry[1]<=0
                    or not isinstance(entry[2],str) or not entry[2]):
                raise ValueError('verified reusable call chain required')
        axes = fields[8]
        if (any(not isinstance(a,list) or len(a)!=2 or not isinstance(a[0],str) or not a[0]
                or (a[1] is not None and type(a[1]) not in (str,int,float,bool)) for a in axes)
                or axes != sorted(axes, key=lambda a:a[0]) or len({a[0] for a in axes}) != len(axes)):
            raise ValueError('canonical matrix required')
    if obs.result in ('failure','pass') and (obs.key is None or not obs.sha or obs.attempt < 1):
        raise ValueError('qualified execution required')
    if obs.result == 'pass' and (not obs.complete or obs.sha not in obs.lineage):
        raise ValueError('incomplete evidence cannot attest pass')


def publish_evidence(conn, capture: bytes, *, verifier):
    """Trusted internal reader only. No SQL lock spans verification or blob I/O."""
    if conn.in_transaction:
        raise RuntimeError('publish before transaction')
    obs = verifier(capture)
    _validate_observation(obs)
    digest = hashlib.sha256(capture).hexdigest()
    root = _blob_root(conn)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    temp = root / ('.publish-' + secrets.token_hex(12))
    try:
        with temp.open('xb') as stream:
            os.chmod(temp, 0o600)
            stream.write(capture)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, root / digest)
        directory = os.open(root, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temp.unlink(missing_ok=True)
    token = secrets.token_hex(24)
    with write_txn(conn):
        conn.execute('INSERT INTO failure_receipts VALUES(?,?,?)', (token,digest,_json(asdict(obs))))
    return EvidenceRef(token, digest)


def _load(conn, ref):
    if type(ref) is not EvidenceRef:
        raise ValueError('durable evidence reference required, not verified boolean')
    row = conn.execute('SELECT digest,observation_json FROM failure_receipts WHERE token=?', (ref.token,)).fetchone()
    if not row or row[0] != ref.digest:
        raise ValueError('unattested receipt')
    if hashlib.sha256((_blob_root(conn)/ref.digest).read_bytes()).hexdigest() != ref.digest:
        raise ValueError('durable evidence digest mismatch')
    data = json.loads(row[1])
    data['lineage'] = tuple(data['lineage'])
    obs = Observation(**data)
    _validate_observation(obs)
    return obs


def _transition(conn, episode, obs, reason, detail):
    conn.execute('INSERT INTO failure_transitions(episode_id,observation_id,revision,reason,detail) VALUES(?,?,?,?,?)',
                 (episode['episode_id'],obs,episode['revision'],reason,_json(detail)))


def _order(a,b,lineage):
    if a['sha'] not in lineage or b['sha'] not in lineage:
        return None
    if a['sha'] != b['sha']:
        return -1 if lineage.index(a['sha']) < lineage.index(b['sha']) else 1
    if a['run_id'] == b['run_id']:
        return (a['attempt'] > b['attempt']) - (a['attempt'] < b['attempt'])
    if a['completed_at'] is not None and b['started_at'] is not None and a['completed_at'] < b['started_at']:
        return -1
    if b['completed_at'] is not None and a['started_at'] is not None and b['completed_at'] < a['started_at']:
        return 1
    return None


def _reduce(conn,key_id,obs_id,obs):
    rows = conn.execute("SELECT obs_id,data FROM failure_observations WHERE key_id=? AND result IN ('failure','pass') AND disposition NOT IN ('DISABLED','NO_ACTION')", (key_id,)).fetchall()
    values = [dict(json.loads(r['data']), obs_id=r['obs_id']) for r in rows]
    # Captures attest ancestry, not arrival order. A delayed prefix cannot erase
    # a retained extension; contradictory chains establish no ordering.
    lineages = tuple(dict.fromkeys(tuple(v['lineage']) for v in values))
    comparisons = {}
    def compare(a, b):
        pair = (a['obs_id'], b['obs_id'])
        if pair not in comparisons:
            comparisons[pair] = compare_uncached(a, b)
        return comparisons[pair]

    def compare_uncached(a, b):
        orders = {_order(a, b, chain) for chain in lineages
                  if len(set(chain)) == len(chain) and a['sha'] in chain and b['sha'] in chain}
        orders.discard(None)
        order = orders.pop() if len(orders) == 1 else None
        # A pass must itself attest the failed history it claims to heal.
        passing, failing = (a, b) if a['result'] == 'pass' else (b, a)
        if a['result'] != b['result'] and passing['result'] == 'pass':
            heals = order == (1 if passing is a else -1)
            if heals and failing['sha'] not in passing['lineage']:
                return None
        return order
    failures = [v for v in values if v['result'] == 'failure']
    # Incomparable passes add uncertainty, not a new red or revoked boundary.
    values = [v for v in values if v['result'] == 'failure' or
              all(compare(v, f) is not None for f in failures)]
    def segment_order(a, b):
        order = compare(a, b)
        # Unordered passes with identical relationships to every failure define
        # the same healing cut. Tie-break only their representation, not evidence.
        if order is None and a['result'] == b['result'] == 'pass' and all(
                compare(a, f) == compare(b, f) for f in failures):
            return (a['source_key'] > b['source_key']) - (a['source_key'] < b['source_key'])
        return order
    ambiguous = any(segment_order(a, b) is None or
                    (segment_order(a, b) == 0 and a['result'] != b['result'])
                    for i, a in enumerate(values) for b in values[i+1:])
    old = conn.execute("SELECT * FROM failure_episodes WHERE key_id=? AND state='active'", (key_id,)).fetchone()
    if ambiguous:
        # Incomparable/force-pushed history cannot certify healing or stale red.
        if old:
            return old
        healed = {r[0] for r in conn.execute(
            'SELECT m.obs_id FROM failure_memberships m JOIN failure_episodes e USING(episode_id) WHERE e.key_id=? AND e.healed_obs IS NOT NULL', (key_id,))}
        values = [v for v in failures if v['obs_id'] not in healed]
    else:
        values.sort(key=cmp_to_key(segment_order))
    segments, current = [], None
    for value in values:
        if value['result']=='failure':
            if current is None:
                current = {'first':value['obs_id'],'failures':[],'heal':None}
                segments.append(current)
            current['failures'].append(value['obs_id'])
        elif current is not None and not ambiguous:
            current['heal'] = value['obs_id']
            current = None
    if not segments:
        return None
    # Preserve an active card through late-boundary reconstruction; terminal
    # cards are never recycled. Historical detached bindings remain tombstones.
    transferable = conn.execute("SELECT b.* FROM failure_bindings b JOIN failure_episodes e USING(episode_id) LEFT JOIN tasks t ON t.id=b.card_id WHERE e.key_id=? AND e.state='active' AND (b.state='pending' OR (b.state='bound' AND t.status NOT IN ('done','archived'))) ORDER BY b.binding_id DESC LIMIT 1", (key_id,)).fetchone()
    conn.execute("UPDATE failure_episodes SET state='healed' WHERE key_id=?", (key_id,))
    predecessor, active = None, None
    for segment in segments:
        episode = conn.execute('SELECT * FROM failure_episodes WHERE first_failure_obs=?',(segment['first'],)).fetchone()
        state = 'healed' if segment['heal'] else 'active'
        if episode is None:
            generation = conn.execute('SELECT COALESCE(MAX(generation),0)+1 FROM failure_episodes WHERE key_id=?',(key_id,)).fetchone()[0]
            eid = conn.execute('INSERT INTO failure_episodes(key_id,generation,state,latest_failure_obs,healed_obs,predecessor,first_failure_obs) VALUES(?,?,?,?,?,?,?)',
                              (key_id,generation,state,segment['failures'][-1],segment['heal'],predecessor,segment['first'])).lastrowid
        else:
            eid = episode['episode_id']
            conn.execute('UPDATE failure_episodes SET state=?,latest_failure_obs=?,healed_obs=?,predecessor=?,revision=revision+1 WHERE episode_id=?',
                         (state,segment['failures'][-1],segment['heal'],predecessor,eid))
        episode = conn.execute('SELECT * FROM failure_episodes WHERE episode_id=?',(eid,)).fetchone()
        for member in segment['failures']:
            conn.execute('INSERT OR IGNORE INTO failure_memberships(obs_id,episode_id,revision) VALUES(?,?,?)',(member,eid,episode['revision']))
        _transition(conn,episode,obs_id,'reduced',{'state':state,'ambiguous':ambiguous})
        if state=='active':
            active = episode
        predecessor = eid
    if active and transferable and transferable['episode_id'] != active['episode_id']:
        conn.execute("UPDATE failure_bindings SET state='detached',generation=generation+1,reason='late_boundary_transfer' WHERE binding_id=?",(transferable['binding_id'],))
        conn.execute("UPDATE failure_bindings SET state='detached',generation=generation+1,reason='late_boundary_superseded' WHERE episode_id=? AND state IN ('pending','bound')",(active['episode_id'],))
        conn.execute("INSERT INTO failure_bindings(episode_id,card_id,state,generation,provenance_json,task_kind,detector_only) VALUES(?,?,?,?,?,?,?)",
                     (active['episode_id'],transferable['card_id'],transferable['state'],transferable['generation']+1,transferable['provenance_json'],transferable['task_kind'],transferable['detector_only']))
    return active


def _create_owner(conn,card,producer):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_boomerang_default as kbd
    if not isinstance(card,PreparedCard):
        raise ValueError('prepared card required')
    boomerang_wake, boomerang_source = kbd.resolve(None)
    tid = kb._new_task_id()
    now = int(time.time())
    # Uncommitted guard row is visible only to this connection; SQLite's write
    # lock prevents a second writer until it is removed before commit. Unlike a
    # UDF trigger this remains evaluable by unupgraded SQLite connections.
    conn.execute('INSERT INTO failure_write_context VALUES(1,?)',(PROTOCOL,))
    try:
        kb._insert_task_row(conn, {
            'id':tid,'title':card.title,'body':card.body,'assignee':card.assignee,
            'status':'ready','created_by':producer,'created_at':now,
            'workspace_kind':'scratch','session_id':card.session_id,'tenant':card.tenant,
            'completion_contract':'local-only',
            'boomerang_wake':1 if boomerang_wake else 0,
        })
    finally:
        conn.execute('DELETE FROM failure_write_context WHERE singleton=1')
    kb._append_event(conn,tid,'created',{'assignee':card.assignee,'status':'ready','failure_authority':PROTOCOL,
                                         'boomerang_wake':bool(boomerang_wake),'boomerang_wake_source':boomerang_source})
    return tid


def _bind(conn,episode,obs,card,producer):
    binding = conn.execute("SELECT * FROM failure_bindings WHERE episode_id=? AND state IN ('pending','bound')",(episode['episode_id'],)).fetchone()
    if binding and binding['state']=='bound':
        return Decision('ATTACHED',episode['key_id'],episode['episode_id'],binding['card_id'],episode['revision'])
    if not binding:
        bid = conn.execute("INSERT INTO failure_bindings(episode_id,state,provenance_json,task_kind) VALUES(?,'pending',?,?)",(episode['episode_id'],_json({'source':obs.source_key,'producer':producer}),obs.task_kind)).lastrowid
    else:
        bid = binding['binding_id']
    from hermes_cli import kanban_db as kb
    try:
        with write_txn(conn,allow_nested=True):
            tid = _create_owner(conn,card,producer)
            conn.execute("UPDATE failure_bindings SET card_id=?,state='bound',detector_only=1,generation=generation+1 WHERE binding_id=?",(tid,bid))
    except ValueError as exc:
        conn.execute('UPDATE failure_bindings SET reason=? WHERE binding_id=?',(str(exc),bid))
        return Decision('EVIDENCE_PENDING',episode['key_id'],episode['episode_id'],revision=episode['revision'],reason=str(exc))
    _transition(conn,episode,None,'owner_bound',{'card_id':tid,'binding_id':bid})
    return Decision('REPAIR' if obs.task_kind=='repair' else 'DIAGNOSTIC',episode['key_id'],episode['episode_id'],tid,episode['revision'])


def observe_failure(conn, ref, *, producer, card_spec=None):
    obs = _load(conn,ref)  # disk and verifier work outside the transaction
    with write_txn(conn):
        policy = conn.execute('SELECT * FROM failure_authority WHERE singleton=1').fetchone()
        if obs.policy_generation != policy['policy_generation']:
            raise ValueError('stale evidence policy; retry verification')
        enrollment = conn.execute('SELECT * FROM failure_producers WHERE producer=?',(producer,)).fetchone()
        if not enrollment:
            raise ValueError('unsupported producer; compatibility capture required')
        enabled = enrollment['enabled'] and enrollment['enrolled']
        prior = conn.execute('SELECT * FROM failure_observations WHERE source_key=?',(obs.source_key,)).fetchone()
        if prior:
            if prior['data'] != _json(asdict(obs)):
                # Only unprocessed, disabled evidence may acquire a current-policy
                # attestation. Every source fact remains immutable.
                previous = json.loads(prior['data'])
                previous['policy_generation'] = obs.policy_generation
                if prior['disposition'] != 'DISABLED' or _json(previous) != _json(asdict(obs)):
                    raise ValueError('source replay changed payload')
                conn.execute('UPDATE failure_observations SET receipt=?,data=? WHERE obs_id=?',
                             (ref.token, _json(asdict(obs)), prior['obs_id']))
            if prior['disposition'] != 'DISABLED' or not enabled:
                return Decision('REPLAY',prior['key_id'],prior['episode_id'],prior['card_id'],reason=prior['disposition'])
        key_id = None
        if obs.key is not None:
            conn.execute('INSERT OR IGNORE INTO failure_keys(canonical) VALUES(?)',(obs.key,))
            key_id = conn.execute('SELECT key_id FROM failure_keys WHERE canonical=?',(obs.key,)).fetchone()[0]
        if prior:
            oid = prior['obs_id']
            conn.execute("UPDATE failure_observations SET disposition='EVIDENCE_PENDING' WHERE obs_id=?", (oid,))
        else:
            oid = conn.execute('INSERT INTO failure_observations(source_key,key_id,result,receipt,data,disposition,reason) VALUES(?,?,?,?,?,?,?)',
                               (obs.source_key,key_id,obs.result,ref.token,_json(asdict(obs)),'EVIDENCE_PENDING',obs.reason)).lastrowid
        if not enabled:
            decision = Decision('DISABLED',key_id)
        elif obs.result in ('unknown','waived'):
            conn.execute('INSERT INTO failure_unresolved VALUES(?,?,?,?)',(obs.source_key,oid,obs.reason or obs.result,_json({'cursor':obs.retry_cursor,'attempts':0})))
            decision = Decision('EVIDENCE_PENDING',key_id,reason=obs.reason or obs.result)
        else:
            if obs.result=='pass' and policy['mode']=='preserve':
                decision = Decision('NO_ACTION',key_id,reason='preserve mode; healing disabled')
            else:
                episode = _reduce(conn,key_id,oid,obs)
                if episode:
                    failure = conn.execute('SELECT data FROM failure_observations WHERE obs_id=?', (episode['latest_failure_obs'],)).fetchone()
                    data = json.loads(failure['data'])
                    data['lineage'] = tuple(data['lineage'])
                    decision = _bind(conn,episode,Observation(**data),card_spec,producer)
                elif obs.result=='pass':
                    decision = Decision('HEALED',key_id)
                else:
                    decision = Decision('HISTORICAL',key_id)
        conn.execute('UPDATE failure_observations SET disposition=?,episode_id=?,card_id=?,reason=? WHERE obs_id=?',
                     (decision.action,decision.episode_id,decision.card_id,decision.reason,oid))
        return decision


def reconcile_pending(conn, *, producer, card_spec):
    """Bounded tick primitive; wiring to dispatcher is S4, not a new daemon."""
    with write_txn(conn):
        enrollment = conn.execute('SELECT * FROM failure_producers WHERE producer=?',(producer,)).fetchone()
        if not enrollment or not enrollment['enabled'] or not enrollment['enrolled']:
            return ()
        rows = conn.execute("SELECT e.*,o.data,b.task_kind FROM failure_bindings b JOIN failure_episodes e USING(episode_id) JOIN failure_observations o ON o.obs_id=e.latest_failure_obs WHERE b.state='pending' AND e.state='active' AND json_extract(b.provenance_json,'$.producer')=? ORDER BY b.binding_id LIMIT 32", (producer,)).fetchall()
        results = []
        for row in rows:
            data = json.loads(row['data'])
            data['lineage'] = tuple(data['lineage'])
            data['task_kind'] = row['task_kind']
            obs = Observation(**data)
            results.append(_bind(conn,row,obs,card_spec,producer))
        return tuple(results)


def record_verification(conn,ref,*,producer):
    return observe_failure(conn,ref,producer=producer)


def recheck_for_claim(conn,task_id,expected_revision=None):
    """Caller holds shared claim transaction. Disabled enrollment is NOT obsolete."""
    if not conn.in_transaction:
        raise RuntimeError('claim check must share claim transaction')
    rows = conn.execute('SELECT b.*,e.state AS episode_state,e.revision FROM failure_bindings b JOIN failure_episodes e USING(episode_id) WHERE b.card_id=?',(task_id,)).fetchall()
    if not rows:
        return Decision('claim')
    active = [r for r in rows if r['state']=='bound' and r['episode_state']=='active']
    revision = max(r['revision'] for r in rows)
    if expected_revision is not None and expected_revision != revision:
        return Decision('retry',revision=revision,reason='episode revision changed')
    preserve = conn.execute("SELECT mode FROM failure_authority WHERE singleton=1").fetchone()[0] == 'preserve'
    if active or preserve or any(not r['detector_only'] for r in rows):
        return Decision('claim',revision=revision)
    return Decision('healed',revision=revision,reason='all detector bindings healed or detached')


def detach_owner(conn,episode_id,*,expected_revision,reason):
    with write_txn(conn):
        e = conn.execute('SELECT * FROM failure_episodes WHERE episode_id=?',(episode_id,)).fetchone()
        if not e or e['revision'] != expected_revision:
            return Decision('retry')
        binding = conn.execute("SELECT * FROM failure_bindings WHERE episode_id=? AND state IN ('pending','bound')", (episode_id,)).fetchone()
        if e['state']=='active' and binding is None:
            return Decision('retry', reason='active responsibility missing')
        conn.execute("UPDATE failure_bindings SET state='detached',generation=generation+1,reason=? WHERE episode_id=? AND state IN ('pending','bound')",(reason,episode_id))
        conn.execute('UPDATE failure_episodes SET revision=revision+1 WHERE episode_id=?',(episode_id,))
        if e['state']=='active':
            conn.execute("INSERT INTO failure_bindings(episode_id,state,provenance_json,task_kind,detector_only,reason) VALUES(?,'pending',?,?,?,?)",
                         (episode_id,binding['provenance_json'],binding['task_kind'],binding['detector_only'],reason))
        _transition(conn,e,None,'owner_detached',{'reason':reason})
        return Decision('EVIDENCE_PENDING',e['key_id'],episode_id,revision=expected_revision+1,reason=reason)


def adopt_owner(conn,episode_id,card_id,*,expected_revision,provenance_ref):
    obs = _load(conn,provenance_ref)
    with write_txn(conn):
        e = conn.execute('SELECT e.*,k.canonical FROM failure_episodes e JOIN failure_keys k USING(key_id) WHERE episode_id=?',(episode_id,)).fetchone()
        task = conn.execute('SELECT status FROM tasks WHERE id=?',(card_id,)).fetchone()
        if not e or e['revision'] != expected_revision or e['state']!='active':
            return Decision('retry')
        if obs.key != e['canonical'] or obs.result != 'failure' or not task or task['status'] in ('done','archived'):
            raise ValueError('exact failure-linked live adoption required')
        binding = conn.execute("SELECT * FROM failure_bindings WHERE episode_id=? AND state IN ('pending','bound')", (episode_id,)).fetchone()
        if not binding:
            return Decision('retry', reason='active responsibility missing')
        provenance = json.loads(binding['provenance_json'])
        provenance['receipt'] = provenance_ref.token
        conn.execute("UPDATE failure_bindings SET state='detached',generation=generation+1 WHERE episode_id=? AND state IN ('pending','bound')",(episode_id,))
        conn.execute("INSERT INTO failure_bindings(episode_id,card_id,state,provenance_json,task_kind,detector_only) VALUES(?,?,'bound',?,?,0)",(episode_id,card_id,_json(provenance),binding['task_kind']))
        conn.execute('UPDATE failure_episodes SET revision=revision+1 WHERE episode_id=?',(episode_id,))
        _transition(conn,e,None,'owner_adopted',{'card_id':card_id})
        return Decision('ATTACHED',e['key_id'],episode_id,card_id,expected_revision+1)
