import csv,io,os
from datetime import date,timedelta
from decimal import Decimal
import pytest
from app.config import RULE_VERSION,MEAL_RULE_VERSION
from app.workflow import load,run
from app import store
from app.modules.m8_meal.rules import apply as meal_apply,AMOUNT
from app.modules.m6_reconcile.compare import balance
from app.modules.m7_archive.export_out import bundle

HEADER=['id','employee','shift_day','version','punch','at','shift_start','shift_end','break_start','break_end','hourly_rate','paid','note']

def csv_text(rows):
    out=io.StringIO();writer=csv.writer(out);writer.writerow(HEADER);writer.writerows(rows);return out.getvalue().encode()

def shift_rows(employee='E100',day='2026-10-01',start='22:00',end='06:00',bs='02:00',be='03:00',rate='20',paid='0'):
    out_day=(date.fromisoformat(day)+timedelta(days=1)).isoformat() if end<=start else day
    return [
        [employee+'a',employee,day,'1','IN',day+'T'+start+':00+08:00',start,end,bs,be,rate,paid,'夜班'],
        [employee+'b',employee,day,'1','OUT',out_day+'T'+end+':00+08:00',start,end,bs,be,rate,'0','夜班'],
    ]

def day_rows(employee='E200'):
    return [
        [employee+'a',employee,'2026-10-02','1','IN','2026-10-02T09:00:00+08:00','09:00','18:00','12:00','13:00','20','0','日班'],
        [employee+'b',employee,'2026-10-02','1','OUT','2026-10-02T18:00:00+08:00','09:00','18:00','12:00','13:00','20','0','日班'],
    ]

@pytest.fixture
def db(tmp_path,monkeypatch):
    monkeypatch.setenv('DB_PATH',str(tmp_path/'meal.db'));return tmp_path/'meal.db'

def save_run(raw,parameters=None):
    parameters=parameters or {'period':'2026-10'}
    state=run(raw,parameters,prior=store.meal_prior(parameters));store.save(state['run_id'],state);return state

def test_night_shift_gets_allowance(state):
    meal=state['meal'];assert meal['summary']['shifts']==18
    assert meal['summary']['eligible']==18 and meal['summary']['total']=='270.00'
    record=meal['records'][0]
    assert record['night_minutes']==420 and record['effective_minutes']==420
    assert record['amount']=='15.00' and record['status']=='active'

def test_basis_comes_from_segments(state):
    record=[r for r in state['meal']['records'] if r['employee']=='E001'][0]
    assert [b['minutes'] for b in record['basis']]==[120,120,180]
    assert sum(b['minutes'] for b in record['basis'])==record['effective_minutes']
    assert record['source_lines']==[3,5] and record['source_ids']==['00001','00002']

def test_day_shift_has_no_allowance(db):
    state=run(csv_text(day_rows()),{'period':'P-day'})
    assert state['meal']['records']==[] and state['meal']['summary']['total']=='0.00'
    assert state['reconcile']['ready'] is True

def test_extra_break_can_disqualify(db):
    rows=shift_rows(bs='23:00',be='05:30')
    state=run(csv_text(rows),{'period':'P-break'})
    record=state['meal']['records'][0]
    assert record['effective_minutes']==90 and record['night_minutes']==90
    assert record['eligible'] is False and record['amount']=='0.00'
    assert record['reason']=='BELOW_THRESHOLD'

def test_short_night_segment_disqualifies(db):
    rows=shift_rows(start='01:30',end='02:00',bs='01:30',be='01:30')
    state=run(csv_text(rows),{'period':'P-short'})
    assert state['meal']['records']==[]

def test_rerun_same_basis_replays_without_double_counting(db):
    raw=csv_text(shift_rows())
    first=save_run(raw,{'period':'P1'})
    second=run(raw,{'period':'P1'},prior=store.meal_prior({'period':'P1'}))
    assert second['meal']['summary']['replayed']==1
    assert second['meal']['summary']['superseded']==0
    record=second['meal']['records'][0]
    assert record['status']=='active' and record['revision']==1 and record['amount']==first['meal']['records'][0]['amount']
    store.save(second['run_id'],second)
    rows=store.connection().execute('SELECT COUNT(*) FROM meal_ledger WHERE period=?',('P1',)).fetchone()[0]
    assert rows==1
    history=store.connection().execute('SELECT COUNT(*) FROM meal_history WHERE period=?',('P1',)).fetchone()[0]
    assert history==0

def test_changed_break_supersedes_old_allowance(db):
    save_run(csv_text(shift_rows()),{'period':'P2'})
    changed=run(csv_text(shift_rows(bs='01:00',be='05:00')),{'period':'P2'},prior=store.meal_prior({'period':'P2'}))
    assert changed['meal']['summary']['superseded']==1
    record=changed['meal']['records'][0]
    assert record['status']=='active' and record['revision']==2 and record['eligible'] is False
    old=changed['meal']['superseded'][0]
    assert old['status']=='superseded' and old['recorded_amount']=='15.00'
    assert old['superseded_by']==record['basis_digest']
    store.save(changed['run_id'],changed)
    live=store.connection().execute('SELECT revision FROM meal_ledger WHERE period=?',('P2',)).fetchone()
    assert live['revision']==2
    archived=store.connection().execute('SELECT body FROM meal_history WHERE period=?',('P2',)).fetchall()
    assert len(archived)==1

def test_rule_version_change_invalidates_old_allowance(db,monkeypatch):
    save_run(csv_text(shift_rows()),{'period':'P3'})
    from app import config
    monkeypatch.setattr(config,'MEAL_RULE_VERSION','2027.01.01-v2-meal.m1')
    import app.modules.m8_meal.rules as rules
    monkeypatch.setattr(rules.config,'MEAL_RULE_VERSION','2027.01.01-v2-meal.m1')
    rerun=run(csv_text(shift_rows()),{'period':'P3'},prior=store.meal_prior({'period':'P3'}))
    assert rerun['meal']['summary']['superseded']==1
    record=rerun['meal']['records'][0]
    assert record['rule_version']=='2027.01.01-v2-meal.m1' and record['revision']==2
    assert rerun['meal']['superseded'][0]['recorded_amount']=='15.00'

def test_recalc_failure_carries_old_result_and_blocks_archive(db):
    save_run(csv_text(shift_rows()),{'period':'P4'})
    state=run(csv_text(shift_rows()),{'period':'P4'},stage=5)
    state['prior_meal']=store.meal_prior({'period':'P4'})
    state['segments']=[]
    meal_apply(state)
    record=state['meal']['records'][0]
    assert record['status']=='carried' and record['reason']=='RECALC_FAILED_CARRIED'
    assert record['amount']=='15.00' and record['needs_review'] is True
    assert record['carried_from'] and record['basis_stale'] is True
    reconciled=balance(state)
    assert reconciled['meal']['cleared'] is False
    assert reconciled['ready'] is False
    assert any(i['code']=='MEAL_CARRIED_UNVERIFIED' for i in reconciled['meal']['issues'])

def test_recalc_failure_after_rule_change_blocks_instead_of_paying(db,monkeypatch):
    save_run(csv_text(shift_rows()),{'period':'P5'})
    import app.modules.m8_meal.rules as rules
    monkeypatch.setattr(rules.config,'MEAL_RULE_VERSION','2027.02.02-v3-meal.m1')
    state=run(csv_text(shift_rows()),{'period':'P5'},stage=5)
    state['prior_meal']=store.meal_prior({'period':'P5'})
    state['segments']=[]
    meal_apply(state)
    record=state['meal']['records'][0]
    assert record['status']=='blocked' and record['reason']=='RECALC_FAILED_STALE_RULE'
    assert record['amount']=='0.00' and record['needs_review'] is True
    assert balance(state)['ready'] is False

def test_recalc_failure_without_baseline_blocks(db):
    state=run(csv_text(shift_rows()),{'period':'P6'},stage=5)
    state['prior_meal']={}
    state['segments']=[]
    meal_apply(state)
    record=state['meal']['records'][0]
    assert record['status']=='blocked' and record['reason']=='RECALC_FAILED_NO_BASELINE'
    assert record['amount']=='0.00'

def test_removed_night_shift_voids_old_allowance(db):
    save_run(csv_text(shift_rows()),{'period':'P7'})
    day=run(csv_text(day_rows()),{'period':'P7'},prior=store.meal_prior({'period':'P7'}))
    assert day['meal']['records']==[]
    assert any(r['superseded_reason']=='SUPERSEDED_REMOVED' for r in day['meal']['superseded'])
    assert day['meal']['summary']['total']=='0.00'

def test_meal_wage_mismatch_breaks_reconcile(state):
    state['business']['meal_total']='999.00'
    reconciled=balance(state)
    assert reconciled['meal']['cleared'] is False and reconciled['ready'] is False
    assert any(i['code']=='MEAL_WAGE_MISMATCH' for i in reconciled['meal']['issues'])

def test_tampered_amount_breaks_reconcile(state):
    state['meal']['records'][0]['amount']='99.00'
    reconciled=balance(state)
    assert reconciled['meal']['cleared'] is False
    assert any(i['code']=='MEAL_AMOUNT_MISMATCH' for i in reconciled['meal']['issues'])

def test_archive_bundle_carries_basis_and_sources(state):
    text=bundle(state)
    assert text.count('餐补')==18
    assert '原始记录' in text and str(state['meal']['records'][0]['basis_digest']) in text
    assert state['archive']['meal_cleared'] is True and state['archive']['ready'] is True

def test_export_allowed_when_meal_cleared(client):
    s=client.post('/api/runs',json={'sample':'dirty.csv'}).get_json()
    assert client.get('/api/archive/'+s['run_id']+'/export.csv').status_code==200

def test_meal_page_shows_per_shift_basis(client):
    page=client.get('/m8_meal').data.decode()
    assert '夜班餐补依据' in page and '22:00–00:00' in page and '420' in page

def test_api_exposes_meal_stage(client):
    s=client.get('/api/m8_meal').get_json()
    assert s['meal']['summary']['shifts']==18 and s['reconcile']['meal']['cleared'] is True

def test_reviewed_batch_freezes_meal_ledger(client):
    s=client.post('/api/runs',json={'sample':'dirty.csv'}).get_json()
    client.post('/api/runs/'+s['run_id']+'/review',json={'version':1,'decision':'通过'})
    client.post('/api/runs',json={'sample':'dirty.csv'})
    stored=store.get(s['run_id'])
    assert stored['review_version']==2 and stored['meal']['records'][0]['status']=='active'

def test_export_blocked_when_carried(client):
    full=[['1a','E100','2026-10-01','1','IN','2026-10-01T22:00:00+08:00','22:00','06:00','02:00','03:00','20','0','x'],
        ['1b','E100','2026-10-01','1','OUT','2026-10-02T06:00:00+08:00','22:00','06:00','02:00','03:00','20','0','x']]
    params={'period':'PZ'}
    first=client.post('/api/runs',json={'csv':csv_text(full).decode(),'parameters':params}).get_json()
    assert client.get('/api/archive/'+first['run_id']+'/export.csv').status_code==200
    missing=client.post('/api/runs',json={'csv':csv_text(full[:1]).decode(),'parameters':params}).get_json()
    status={r['status'] for r in missing['meal']['records']}
    assert 'carried' in status and missing['reconcile']['ready'] is False
    blocked=client.get('/api/archive/'+missing['run_id']+'/export.csv')
    assert blocked.status_code==422 and blocked.get_json()['code']=='UNBALANCED'

def test_meal_issues_have_labels(state):
    state['meal']['records'][0]['amount']='99.00'
    issue=balance(state)['meal']['issues'][0]
    assert issue['code']=='MEAL_AMOUNT_MISMATCH' and issue['label']=='餐补金额与重算不符'

def test_meal_page_marks_reconcile_status(client):
    page=client.get('/m8_meal').data.decode()
    assert '餐补对平' in page and '判定口径' in page and '工资侧合计' in page
