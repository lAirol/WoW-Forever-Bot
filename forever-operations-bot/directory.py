"""Character directory: read-only filters and reports; no Discord/network access."""
from collections import defaultdict
import re
from store import CLASSES,ARMORS,ROLES,RuleError

VIEWS={'персонажи':'Персонажи','люди':'Люди','классы':'По классам','роли':'По игровым ролям',
       'броня':'По броне','миры':'По мирам','расы':'По расам'}
LABELS={'role':'Роль','class_name':'Класс','armor':'Броня','realm':'Мир','race':'Раса',
        'specialization':'Специализация','min_level':'Уровень от','max_level':'Уровень до','user':'Участник','search':'Поиск'}

def enrich(row):
    result=dict(row)
    # Only parse the exact format written by /персонаж; unknown legacy values stay unknown.
    match=re.fullmatch(r'([^/]+) / ([^/]+) / ([^/]+) / ур\. (\d+)',row['details'])
    result.update(specialization=match[2].strip() if match else '',race=match[3].strip() if match else '',
                  level=int(match[4]) if match else None)
    result['role_set']=set(row['roles'].split(','))
    return result

def validate(filters):
    if set(filters)-set(LABELS):raise RuleError('Неизвестный фильтр.')
    for key,limit in [('realm',60),('race',60),('specialization',120),('search',100)]:
        if filters.get(key) and len(filters[key])>limit:raise RuleError(f'{LABELS[key]}: максимум {limit} символов.')
    for key,choices in [('role',ROLES),('class_name',CLASSES),('armor',ARMORS)]:
        if filters.get(key) and filters[key] not in choices:raise RuleError('Неизвестное значение: '+LABELS[key])
    lo,hi=filters.get('min_level'),filters.get('max_level')
    if lo is not None and not 1<=lo<=999 or hi is not None and not 1<=hi<=999:raise RuleError('Уровень: от 1 до 999.')
    if lo is not None and hi is not None and lo>hi:raise RuleError('Минимальный уровень больше максимального.')

def select(rows,filters):
    validate(filters);result=[]
    for row in rows:
        r=enrich(row)
        if filters.get('role') and filters['role'] not in r['role_set']:continue
        if any(filters.get(k) and str(r[k]).casefold()!=str(filters[k]).strip().casefold()
               for k in ('class_name','armor','realm','race','user')):continue
        if filters.get('specialization') and filters['specialization'].strip().casefold() not in r['specialization'].casefold():continue
        if filters.get('search') and filters['search'].strip().casefold() not in (r['name']+' '+r['realm']+' '+r['details']).casefold():continue
        if filters.get('min_level') is not None and (r['level'] is None or r['level']<filters['min_level']):continue
        if filters.get('max_level') is not None and (r['level'] is None or r['level']>filters['max_level']):continue
        result.append(r)
    return sorted(result,key=lambda r:(r['name'].casefold(),r['realm'].casefold(),r['id']))

def groups(rows,view):
    if view not in VIEWS:raise RuleError('Неизвестный вид списка.')
    by=defaultdict(list)
    for r in rows:
        if view=='роли':keys=sorted(r['role_set'])
        elif view=='люди':keys=[r['user']]
        else:keys=[r[{'классы':'class_name','броня':'armor','миры':'realm','расы':'race'}[view]] or 'Не указано']
        for key in keys:by[key].append(r)
    return sorted(by.items(),key=lambda pair:str(pair[0]).casefold())

def filter_text(filters):
    return ' · '.join(f'{LABELS[k]}: {v}' for k,v in filters.items() if v is not None and v!='') or 'Без фильтров'

def export_text(rows,filters,view):
    head=f'Карточки команды | {VIEWS[view]}\n{filter_text(filters)}\nПерсонажей: {len(rows)}; людей: {len({r["user"] for r in rows})}\n'
    if view not in ('персонажи','люди'):
        head+='\nСводка (у одного человека могут быть разные персонажи/роли):\n'
        for key,items in groups(rows,view):head+=f'{key}: персонажей {len(items)}, людей {len({r["user"] for r in items})}\n'
    return head+'\n'+('\n'.join(f"Discord {r['user']} | №{r['id']} {r['name']} | {r['realm']} | {r['details']} | роли: {r['roles']} | броня: {r['armor'] or 'не указана'}" for r in rows) or 'Совпадений нет.')
