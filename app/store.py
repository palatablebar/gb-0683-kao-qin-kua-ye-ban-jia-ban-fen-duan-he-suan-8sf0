import sqlite3,json,os
from pathlib import Path
from app.errors import DomainError

def connection():
    path=Path(os.environ.get('DB_PATH','data/records.db'));path.parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path,timeout=10);db.row_factory=sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('CREATE TABLE IF NOT EXISTS runs (id TEXT PRIMARY KEY, body TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1, decision TEXT NOT NULL DEFAULT "待复核")')
    db.execute('CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS meal_ledger (period TEXT NOT NULL, shift_key TEXT NOT NULL, digest TEXT NOT NULL, revision INTEGER NOT NULL, body TEXT NOT NULL, PRIMARY KEY(period,shift_key))')
    db.execute('CREATE TABLE IF NOT EXISTS meal_history (period TEXT NOT NULL, shift_key TEXT NOT NULL, digest TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(period,shift_key,digest))')
    return db

def save(run_id, body):
    with connection() as db:
        cur=db.execute('INSERT OR IGNORE INTO runs(id,body) VALUES (?,?)',(run_id,json.dumps(body,ensure_ascii=False)))
        stored=cur.rowcount==1
        if not stored:
            # 同源重跑未复核前刷新证据快照，使餐补依据与账本一致；已复核版本保持冻结。
            updated=db.execute("UPDATE runs SET body=? WHERE id=? AND version=1 AND decision='待复核'",
                (json.dumps(body,ensure_ascii=False),run_id))
            stored=updated.rowcount==1
        db.execute("INSERT INTO meta(key,value) VALUES ('latest',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(run_id,))
        # 已复核冻结的批次不得再改写周期餐补账本。
        if stored:_persist_meal(db,body)
    return get(run_id)

def get(run_id):
    with connection() as db:r=db.execute('SELECT * FROM runs WHERE id=?',(run_id,)).fetchone()
    if not r:raise DomainError('NOT_FOUND','批次不存在',404)
    body=json.loads(r['body']);body.update(run_id=r['id'],review_version=r['version'],decision=r['decision']);return body

def history():
    with connection() as db:rows=db.execute("SELECT id,version,decision FROM runs ORDER BY CASE WHEN id=(SELECT value FROM meta WHERE key='latest') THEN 0 ELSE 1 END,rowid DESC LIMIT 100").fetchall()
    return [dict(r) for r in rows]

def review(run_id, version, decision):
    if decision not in ('通过','退回'):raise DomainError('DECISION','复核状态无效')
    with connection() as db:
        cur=db.execute('UPDATE runs SET version=version+1,decision=? WHERE id=? AND version=?',(decision,run_id,version))
        if cur.rowcount!=1:
            if not db.execute('SELECT 1 FROM runs WHERE id=?',(run_id,)).fetchone():raise DomainError('NOT_FOUND','批次不存在',404)
            raise DomainError('STALE','复核版本已更新，请刷新',409)
    return get(run_id)

def period(parameters):
    value=(parameters or {}).get('period','2026-10') if isinstance(parameters,dict) else '2026-10'
    return str(value)

def meal_prior(parameters):
    """读取同一结算周期上一次已落地的餐补当前版，供重跑判定重用/失效/兜底。"""
    scope=period(parameters)
    with connection() as db:
        rows=db.execute('SELECT body FROM meal_ledger WHERE period=? ORDER BY shift_key',(scope,)).fetchall()
    return {'period':scope,'records':[json.loads(r['body']) for r in rows]}

def _persist_meal(db,body):
    meal=body.get('meal') or {}
    records=meal.get('records') or []
    if not meal:return
    scope=period(body.get('manifest',{}).get('parameters',{}))
    for record in records:
        if record.get('status') not in ('active','carried'):continue
        db.execute('INSERT INTO meal_ledger(period,shift_key,digest,revision,body) VALUES (?,?,?,?,?) '
            'ON CONFLICT(period,shift_key) DO UPDATE SET digest=excluded.digest,revision=excluded.revision,body=excluded.body',
            (scope,record['shift_key'],record['basis_digest'],int(record.get('revision',1)),json.dumps(record,ensure_ascii=False)))
    for record in meal.get('superseded') or []:
        db.execute('INSERT OR IGNORE INTO meal_history(period,shift_key,digest,body) VALUES (?,?,?,?)',
            (scope,record['shift_key'],record['basis_digest'],json.dumps(record,ensure_ascii=False)))
        if record.get('superseded_reason')=='SUPERSEDED_REMOVED':
            db.execute('DELETE FROM meal_ledger WHERE period=? AND shift_key=?',(scope,record['shift_key']))
