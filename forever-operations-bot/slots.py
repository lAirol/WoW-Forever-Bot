"""Exact role seats and maximum matching. No game-derived armor assumptions."""
from collections import Counter
from store import ROLES,CLASSES,RuleError

def layout(event,rules):
    result=[]
    for role,col in zip(ROLES,('tank','heal','damage')):
        selected=[r for r in rules if r['role']==role]
        used=sum(r['places'] for r in selected)
        if used>event[col]:raise RuleError(f'Требования для роли «{role}» превышают число мест ({event[col]}).')
        for rule in selected+[{'id':None,'places':event[col]-used,'classes':'','armor':''}]:
            general=set(filter(None,event['classes'].split(',')));specific=set(filter(None,rule['classes'].split(',')))
            allowed=general & specific if general and specific else general or specific
            if general and specific and not allowed:raise RuleError('Классы места противоречат общему фильтру сбора.')
            if event['armor'] and rule['armor'] and event['armor']!=rule['armor']:raise RuleError('Броня места противоречит общему фильтру сбора.')
            for ordinal in range(rule['places']):
                result.append({'role':role,'classes':tuple(sorted(allowed)),'armor':rule['armor'] or event['armor'],
                    'rule_id':rule['id'],'ordinal':ordinal+1})
    return result

def fits(character,seat):
    return (character['role']==seat['role'] and (not seat['classes'] or character['class_name'] in seat['classes'])
            and (not seat['armor'] or character['armor']==seat['armor']))

def matching(records,seats,accept=fits):
    """Return user -> seat index, or None. Augmenting paths avoid greedy seat allocation."""
    owners={}
    def place(record,seen):
        for index,seat in enumerate(seats):
            if index in seen or not accept(record,seat):continue
            seen.add(index)
            if index not in owners or place(owners[index],seen):
                owners[index]=record;return True
        return False
    for record in records:
        if not place(record,set()):return None
    return {record['user']:index for index,record in owners.items()}

def feasible(seats,class_limit):
    if not class_limit:return True
    choices=[{'class_name':cl} for cl in CLASSES for _ in range(class_limit)]
    wanted=[{'user':i,**seat} for i,seat in enumerate(seats)]
    return matching(wanted,choices,lambda seat,choice:not seat['classes'] or choice['class_name'] in seat['classes']) is not None

def allocate(records,seats,class_limit):
    if class_limit:
        counts=Counter(r['class_name'] for r in records)
        if any(cl not in CLASSES or count>class_limit for cl,count in counts.items()):return None
    return matching(records,seats)

def label(seat):
    return seat['role']+' · '+(', '.join(seat['classes']) or 'любой класс')+' · '+(seat['armor'] or 'любая броня')
