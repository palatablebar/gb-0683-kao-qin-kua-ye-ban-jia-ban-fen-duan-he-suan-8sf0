REASONS = {
    'WIDTH':'列数不齐', 'MISSING':'缺少必填值', 'NUMBER':'数值无效',
    'DATE':'日期无效', 'UNIT':'单位未知', 'CONFLICT':'同版记录冲突',
    'VERSION':'版本不适用', 'REFERENCE':'关联记录缺失', 'RANGE':'区间无效',
    'CONSTRAINT':'约束矛盾', 'METHOD':'检测方法不适用', 'STALE':'复核版本已更新',
    'MEAL_MISSING':'餐补结果缺失', 'MEAL_AMOUNT_MISMATCH':'餐补金额与重算不符',
    'MEAL_SUMMARY_MISMATCH':'餐补合计不符', 'MEAL_BLOCKED_PAYABLE':'餐补已阻断仍计发',
    'MEAL_SUPERSEDE_NOT_VOID':'旧餐补未失效', 'MEAL_WAGE_MISMATCH':'餐补与工资口径不平'
}
MEAL_REASONS = {
    'ELIGIBLE':'达标可发', 'BELOW_THRESHOLD':'未达夜段或有效工时门槛',
    'RECALC_FAILED_CARRIED':'重算失败，沿用旧版可发结果（待复核）',
    'RECALC_FAILED_NO_BASELINE':'重算失败且无可沿用旧版',
    'RECALC_FAILED_STALE_RULE':'规则已变更，旧版不可沿用',
    'SUPERSEDED_BY_RECALC':'依据变化，旧版失效重算',
    'SUPERSEDED_REMOVED':'本次已不构成夜班，旧版失效'
}
