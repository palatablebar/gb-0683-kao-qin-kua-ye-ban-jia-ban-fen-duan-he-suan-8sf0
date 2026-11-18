from flask import Blueprint
from app.web import api,page
bp=Blueprint('m8_meal',__name__)
# 餐补页展示当前班次依据，同时运行到对账阶段以显示“是否与工资对平”。
@bp.get('/m8_meal')
def screen():return page(6,'m8_meal.html',run_stage=7)
@bp.route('/api/m8_meal',methods=['GET','POST'])
def preview():return api(7)
