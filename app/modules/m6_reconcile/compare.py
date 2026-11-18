from decimal import Decimal,ROUND_HALF_UP
from app.modules.m8_meal.rules import AMOUNT,MIN_NIGHT_MINUTES,MIN_EFFECTIVE_MINUTES
from app.reasons import MEAL_REASONS,REASONS

CENT=Decimal('.01')

def _money(value):
    return Decimal(str(value)).quantize(CENT,rounding=ROUND_HALF_UP)

def _issue(record,code,label):
    return {'shift_key':record.get('shift_key',''),'code':code,'label':label}

def meal_balance(state):
    """餐补必须与工资口径对平：逐班重算资格与金额，任何不一致即不得封账。"""
    meal=state.get('meal')
    if not meal:return {'present':False,'balanced':False,'cleared':False,'reason':'MEAL_MISSING',
        'reason_label':REASONS['MEAL_MISSING'],
        'records':0,'payable':0,'total':'0.00','expected':'0.00','difference':'0.00','issues':[]}
    issues=[];payable=Decimal('0');expected=Decimal('0')
    for record in meal.get('records',[]):
        amount=_money(record.get('amount',0))
        recomputed=AMOUNT if (record.get('night_minutes',0)>=record.get('min_night_minutes',MIN_NIGHT_MINUTES)
            and record.get('effective_minutes',0)>=record.get('min_effective_minutes',MIN_EFFECTIVE_MINUTES)) else Decimal('0.00')
        status=record.get('status')
        if status=='active':
            expected+=recomputed
            if amount!=recomputed:
                issues.append(_issue(record,'MEAL_AMOUNT_MISMATCH',REASONS['MEAL_AMOUNT_MISMATCH']))
        elif status=='carried':
            expected+=amount
            issues.append(_issue(record,'MEAL_CARRIED_UNVERIFIED',MEAL_REASONS['RECALC_FAILED_CARRIED']))
        else:
            if amount!=0:issues.append(_issue(record,'MEAL_BLOCKED_PAYABLE',REASONS['MEAL_BLOCKED_PAYABLE']))
            issues.append(_issue(record,'MEAL_BLOCKED',MEAL_REASONS.get(record.get('reason'),REASONS['MEAL_MISSING'])))
        if status in ('active','carried'):payable+=amount
    summary=_money(meal.get('summary',{}).get('total',0))
    if _money(payable)!=summary:
        issues.append({'shift_key':'','code':'MEAL_SUMMARY_MISMATCH','label':REASONS['MEAL_SUMMARY_MISMATCH']})
    for old in meal.get('superseded',[]):
        if old.get('status')!='superseded':
            issues.append(_issue(old,'MEAL_SUPERSEDE_NOT_VOID',REASONS['MEAL_SUPERSEDE_NOT_VOID']))
    business=state.get('business') or {}
    if 'meal_total' in business and _money(business.get('meal_total',0))!=summary:
        issues.append({'shift_key':'','code':'MEAL_WAGE_MISMATCH','label':REASONS['MEAL_WAGE_MISMATCH']})
    if 'payable_total' in business and _money(business.get('payable_total',0))!=_money(business.get('total',0))+summary:
        issues.append({'shift_key':'','code':'MEAL_WAGE_MISMATCH','label':REASONS['MEAL_WAGE_MISMATCH']})
    hard={'MEAL_AMOUNT_MISMATCH','MEAL_SUMMARY_MISMATCH','MEAL_BLOCKED_PAYABLE','MEAL_SUPERSEDE_NOT_VOID',
        'MEAL_MISSING','MEAL_WAGE_MISMATCH'}
    balanced=not any(issue['code'] in hard for issue in issues)
    cleared=balanced and not issues
    return {'present':True,'balanced':balanced,'cleared':cleared,'records':len(meal.get('records',[])),
        'payable':meal.get('summary',{}).get('payable_count',0),
        'total':str(_money(payable)),'expected':str(_money(expected)),
        'difference':str(_money(payable-expected)),'carried':meal.get('summary',{}).get('carried',0),
        'superseded':meal.get('summary',{}).get('superseded',0),'issues':issues,
        'rule_version':meal.get('rule_version')}

def balance(state):
    accepted=len(state['rows']);isolated=len(state['quarantine'])
    accounted=accepted+isolated+state['blank_count']+state['header_count']+state['duplicate_count']
    meal=meal_balance(state)
    counted=accounted==state['input_count']
    return {'input':state['input_count'],'accepted':accepted,'isolated':isolated,
        'blank':state['blank_count'],'headers':state['header_count'],'duplicates':state['duplicate_count'],
        'accounted':accounted,'balanced':counted,'business':state.get('business',{}),
        'meal':meal,'ready':counted and meal.get('balanced',False) and meal.get('cleared',False)}
