from app.modules.m7_archive.export_out import checksum

def execute(state):
    reconcile=state.get('reconcile',{})
    meal=state.get('meal') or {}
    state['archive']={'sha256':checksum(state),
        'evidence_count':len(state.get('calculations',[]))+len(meal.get('records',[]))+len(meal.get('superseded',[]))+len(state['quarantine']),
        'meal_records':len(meal.get('records',[])),'meal_cleared':bool(reconcile.get('meal',{}).get('cleared')),
        'ready':bool(reconcile.get('ready'))}
    return state
