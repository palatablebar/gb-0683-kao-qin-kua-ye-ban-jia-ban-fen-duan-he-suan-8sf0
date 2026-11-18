import hashlib,json
from collections import defaultdict
from datetime import date,datetime,time,timedelta
from decimal import Decimal,ROUND_HALF_UP
from app import config
from app.errors import DomainError
from app.modules.m3_versions.rules import schedule,ZONE

# 夜班餐补规则（详见 docs/DATA-DICTIONARY.md）：
# 只有“跨夜班”才产生餐补记录；金额按班次固定标准发放，资格由实际切段结果判定。
AMOUNT=Decimal('15.00')
MIN_NIGHT_MINUTES=300
MIN_EFFECTIVE_MINUTES=360
NIGHT_START=time(22,0)
NIGHT_END=time(6,0)

def window(shift_day):
    day=date.fromisoformat(shift_day)
    return (datetime.combine(day,NIGHT_START,ZONE),datetime.combine(day+timedelta(days=1),NIGHT_END,ZONE))

def _planned(shift):
    row=shift.get('in') or shift.get('out')
    if not row:raise DomainError('MISSING','班次缺少计划时刻')
    return schedule(row)

def is_night(shift):
    start,end,_,_=_planned(shift)
    return end.date()>start.date()

def _minutes(left,right):
    return max(0,int((right-left).total_seconds()//60))

def _overlap(left,right,start,end):
    return _minutes(max(left,start),min(right,end))

def _digest(shift,night_minutes,effective_minutes,eligible,amount,basis,sources):
    canonical={'rule_version':config.MEAL_RULE_VERSION,
        'engine_rule_version':config.RULE_VERSION,'amount':str(AMOUNT),
        'thresholds':[MIN_NIGHT_MINUTES,MIN_EFFECTIVE_MINUTES],
        'night_window':[NIGHT_START.isoformat(),NIGHT_END.isoformat()],
        'shift_key':shift['id'],'night_minutes':night_minutes,'effective_minutes':effective_minutes,
        'eligible':eligible,'payable':amount,
        'basis':[[b['start'],b['end'],b['minutes'],b['night_minutes']] for b in basis],
        'sources':sorted(sources)}
    return hashlib.sha256(json.dumps(canonical,sort_keys=True,ensure_ascii=False,default=str).encode()).hexdigest()

def _record(shift,night_minutes,effective_minutes,basis,sources,status='active',
        reason=None,revision=1,digest=None,replayed=False):
    eligible=night_minutes>=MIN_NIGHT_MINUTES and effective_minutes>=MIN_EFFECTIVE_MINUTES
    payable=AMOUNT if eligible else Decimal('0.00')
    amount=str(payable.quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    item={'shift_key':shift['id'],'employee':shift['employee'],'shift_day':shift['day'],
        'night_minutes':night_minutes,'effective_minutes':effective_minutes,
        'min_night_minutes':MIN_NIGHT_MINUTES,'min_effective_minutes':MIN_EFFECTIVE_MINUTES,
        'eligible':eligible,'amount':amount,
        'rule_version':config.MEAL_RULE_VERSION,
        'engine_rule_version':config.RULE_VERSION,'status':status,
        'reason':reason or ('ELIGIBLE' if eligible else 'BELOW_THRESHOLD'),
        'needs_review':status in ('carried','blocked'),'revision':revision,'replayed':replayed,
        'basis':basis,'source_lines':sorted(sources),
        'source_ids':sorted({s['id'] for s in shift.get('sources',[])}),
        'night_window':[window(shift['day'])[0].isoformat(),window(shift['day'])[1].isoformat()]}
    item['basis_digest']=digest or _digest(shift,night_minutes,effective_minutes,eligible,amount,basis,sources)
    return item

def evaluate(shift,segments):
    start,end,_,_=_planned(shift)
    if not end.date()>start.date():return None
    if not segments:raise DomainError('MISSING','班次缺少有效切段')
    night_start,night_end=window(shift['day'])
    basis=[];effective=0;night=0
    for segment in sorted(segments,key=lambda r:r['start']):
        left=datetime.fromisoformat(segment['start']);right=datetime.fromisoformat(segment['end'])
        minutes=_minutes(left,right);night_minutes=_overlap(left,right,night_start,night_end)
        effective+=minutes;night+=night_minutes
        basis.append({'start':segment['start'],'end':segment['end'],'calendar_day':segment['calendar_day'],
            'minutes':minutes,'night_minutes':night_minutes})
    sources={row['_line'] for row in shift.get('sources',[]) if '_line' in row}
    return _record(shift,night,effective,basis,sources)

def carry(shift,prior,failure):
    """重算失败时沿用旧版可发结果，并强制标记待复核，不得直接封账。
    但规则版本已变化时旧依据已失效，不得兜底发放，只能阻断。"""
    if not prior:return _record(shift,0,0,[],set(),status='blocked',reason='RECALC_FAILED_NO_BASELINE')
    if prior.get('rule_version')!=config.MEAL_RULE_VERSION or prior.get('engine_rule_version')!=config.RULE_VERSION:
        return _record(shift,0,0,prior.get('basis',[]),set(prior.get('source_lines',[])),
            status='blocked',reason='RECALC_FAILED_STALE_RULE')
    carried=dict(prior)
    carried.update(status='carried',reason='RECALC_FAILED_CARRIED',needs_review=True,
        recompute_error=failure,rule_version=config.MEAL_RULE_VERSION,
        engine_rule_version=config.RULE_VERSION,
        carried_rule_version=prior.get('rule_version'),carried_from=prior.get('basis_digest'),
        basis_stale=True,replayed=False)
    return carried

def _supersede(record,prior):
    """同一班次重跑：同依据即重放，不新增次数或金额；依据变化则旧版失效。"""
    if not prior:return record,None
    old_digest=prior.get('basis_digest')
    if old_digest==record['basis_digest']:
        record['revision']=int(prior.get('revision',1))
        record['replayed']=True
        return record,None
    record['revision']=int(prior.get('revision',1))+1
    record['supersedes']=old_digest
    superseded=dict(prior)
    superseded.update(status='superseded',needs_review=False,superseded_by=record['basis_digest'],
        superseded_reason='SUPERSEDED_BY_RECALC',recorded_amount=prior.get('amount'))
    return record,superseded

def _void(prior,reason):
    removed=dict(prior)
    removed.update(status='superseded',needs_review=False,
        superseded_reason=reason,recorded_amount=prior.get('amount'),
        superseded_by=None)
    return removed

def apply(state):
    prior=(state.pop('prior_meal',None) or {}).get('records',[])
    by_key={r['shift_key']:r for r in prior if r.get('shift_key')}
    segments=defaultdict(list)
    for segment in state.get('segments',[]):segments[segment['id']].append(segment)
    records=[];superseded=[];handled=set()
    for shift in state.get('shifts',[]):
        try:
            night=is_night(shift)
        except DomainError as error:
            record=carry(shift,by_key.get(shift['id']),error.code)
            records.append(record);handled.add(shift['id'])
            continue
        if not night:continue
        try:
            record=evaluate(shift,segments.get(shift['id'],[]))
        except DomainError as error:
            record=carry(shift,by_key.get(shift['id']),error.code)
        if record['status']=='active':
            record,old=_supersede(record,by_key.get(shift['id']))
        else:
            old=None
        if old:superseded.append(old)
        records.append(record);handled.add(shift['id'])
    # 本次不再构成有效夜班的旧记录必须失效，不能继续随工资发放。
    for key in sorted(set(by_key)-handled):
        superseded.append(_void(by_key[key],'SUPERSEDED_REMOVED'))
    records.sort(key=lambda r:r['shift_key'])
    superseded.sort(key=lambda r:r['shift_key'])
    payable=sum((Decimal(r['amount']) for r in records if r['status']=='active'),Decimal('0'))
    carried=sum((Decimal(r['amount']) for r in records if r['status']=='carried'),Decimal('0'))
    meal={'rule_version':config.MEAL_RULE_VERSION,
        'engine_rule_version':config.RULE_VERSION,'amount_per_shift':str(AMOUNT),
        'night_window':'22:00–06:00 Asia/Shanghai',
        'thresholds':{'night_minutes':MIN_NIGHT_MINUTES,'effective_minutes':MIN_EFFECTIVE_MINUTES},
        'records':records,'superseded':superseded,
        'summary':{'shifts':len(records),'eligible':sum(1 for r in records if r['eligible']),
            'carried':sum(1 for r in records if r['status']=='carried'),
            'blocked':sum(1 for r in records if r['status']=='blocked'),
            'superseded':len(superseded),'replayed':sum(1 for r in records if r.get('replayed')),
            'payable_count':sum(1 for r in records if r['status'] in ('active','carried') and r['eligible']),
            'active_total':str(payable.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
            'carried_total':str(carried.quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),
            'total':str((payable+carried).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),'unit':'CNY'}}
    state['meal']=meal
    business=state.setdefault('business',{})
    business['meal_total']=meal['summary']['total']
    business['payable_total']=str((Decimal(str(business.get('total','0')))+payable+carried).quantize(Decimal('.01'),rounding=ROUND_HALF_UP))
    business['meal_unit']='CNY'
    return state
