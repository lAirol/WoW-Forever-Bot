"""Transactional roster store. No Discord credentials or buyer data."""
from contextlib import contextmanager
from datetime import datetime, timezone
import sqlite3
import uuid
import json

ROLES = ('танк', 'хил', 'дд')
OPEN = ('набор', 'сбор', 'идёт')
CLASSES=('Воин','Паладин','Охотник','Разбойник','Жрец','Шаман','Маг','Чернокнижник','Друид')
ARMORS=('ткань','кожа','кольчуга','латы')

def class_name(value):
    for name in CLASSES:
        if name.casefold()==value.strip().casefold(): return name
    raise RuleError('Неизвестный класс. Выбери один из девяти классов WoW Forever.')

class RuleError(Exception):
    pass

def utc(text):
    try:
        value = datetime.fromisoformat(text)
        if value.tzinfo is None:
            raise ValueError()
        return int(value.timestamp())
    except ValueError:
        raise RuleError('Укажи дату со смещением: 2026-11-01T20:00+03:00.')

class Store:
    def __init__(self, path):
        self.path = str(path)
        with self.tx() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS characters(
              id INTEGER PRIMARY KEY, user INTEGER NOT NULL, name TEXT NOT NULL,
              realm TEXT NOT NULL, details TEXT NOT NULL, roles TEXT NOT NULL,
              UNIQUE(user,name,realm));
            CREATE TABLE IF NOT EXISTS events(
              id INTEGER PRIMARY KEY, title TEXT NOT NULL, start INTEGER NOT NULL,
              end INTEGER NOT NULL, tank INTEGER, heal INTEGER, damage INTEGER,
              state TEXT NOT NULL DEFAULT 'набор', channel INTEGER, message INTEGER,
              reminded INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS signups(
              seq INTEGER PRIMARY KEY AUTOINCREMENT, event INTEGER NOT NULL,
              user INTEGER NOT NULL, character INTEGER NOT NULL, role TEXT NOT NULL,
              slot TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0,
              snapshot TEXT NOT NULL, UNIQUE(event,user),
              FOREIGN KEY(event) REFERENCES events(id),
              FOREIGN KEY(character) REFERENCES characters(id));
            CREATE TABLE IF NOT EXISTS tickets(
              id INTEGER PRIMARY KEY, user INTEGER NOT NULL,
              state TEXT NOT NULL, channel INTEGER UNIQUE);
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_ticket
              ON tickets(user) WHERE state IN ('создаётся','открыто','проверить');
            CREATE TABLE IF NOT EXISTS rooms(
              kind TEXT NOT NULL, owner INTEGER NOT NULL, channel INTEGER UNIQUE,
              marker TEXT NOT NULL, state TEXT NOT NULL DEFAULT 'создаётся',
              delete_at INTEGER, PRIMARY KEY(kind,owner));
            CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS orders(
              id INTEGER PRIMARY KEY,title TEXT NOT NULL,source TEXT NOT NULL,external_ref TEXT NOT NULL DEFAULT '',
              due INTEGER NOT NULL,description TEXT NOT NULL,operator INTEGER NOT NULL,event INTEGER UNIQUE,
              state TEXT NOT NULL DEFAULT 'новый',report TEXT NOT NULL DEFAULT '',channel INTEGER,message INTEGER,
              FOREIGN KEY(event) REFERENCES events(id));
            CREATE UNIQUE INDEX IF NOT EXISTS unique_order_reference ON orders(source,external_ref) WHERE external_ref<>'';
            CREATE TABLE IF NOT EXISTS order_audit(id INTEGER PRIMARY KEY,order_id INTEGER NOT NULL,actor INTEGER,
              action TEXT NOT NULL,at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              FOREIGN KEY(order_id) REFERENCES orders(id));
            CREATE TABLE IF NOT EXISTS character_audit(id INTEGER PRIMARY KEY,character INTEGER NOT NULL,revision INTEGER NOT NULL,
              actor INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,at INTEGER NOT NULL,
              FOREIGN KEY(character) REFERENCES characters(id));
            CREATE TABLE IF NOT EXISTS character_audit_archive(id INTEGER PRIMARY KEY,character INTEGER NOT NULL,revision INTEGER NOT NULL,
              actor INTEGER NOT NULL,status TEXT NOT NULL,reason TEXT NOT NULL,at INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS event_requirements(id INTEGER PRIMARY KEY AUTOINCREMENT,event INTEGER NOT NULL,
              role TEXT NOT NULL,places INTEGER NOT NULL,classes TEXT NOT NULL DEFAULT '',armor TEXT NOT NULL DEFAULT '',
              FOREIGN KEY(event) REFERENCES events(id));
            CREATE TABLE IF NOT EXISTS event_rules_audit(id INTEGER PRIMARY KEY,event INTEGER NOT NULL,actor INTEGER NOT NULL,
              action TEXT NOT NULL,details TEXT NOT NULL,at INTEGER NOT NULL DEFAULT (strftime('%s','now')),
              FOREIGN KEY(event) REFERENCES events(id));
            ''')
            for table,columns in {
                'characters':{'class_name':"TEXT NOT NULL DEFAULT ''",'armor':"TEXT NOT NULL DEFAULT ''",'revision':'INTEGER NOT NULL DEFAULT 0',
                    'admission':"TEXT NOT NULL DEFAULT 'не проверен'",'reviewer':'INTEGER','reviewed_at':'INTEGER','approval_until':'INTEGER','review_note':"TEXT NOT NULL DEFAULT ''",'review_serial':'INTEGER NOT NULL DEFAULT 0'},
                'events':{'max_people':'INTEGER','classes':"TEXT NOT NULL DEFAULT ''",'armor':"TEXT NOT NULL DEFAULT ''",'organizer':'INTEGER','require_approved':'INTEGER NOT NULL DEFAULT 0','class_limit':'INTEGER NOT NULL DEFAULT 0'},
                'signups':{'profile_revision':'INTEGER NOT NULL DEFAULT 0','class_snapshot':"TEXT NOT NULL DEFAULT ''",'armor_snapshot':"TEXT NOT NULL DEFAULT ''"}
            }.items():
                existing={r['name'] for r in c.execute(f'PRAGMA table_info({table})')}
                for name,definition in columns.items():
                    if name not in existing:c.execute(f'ALTER TABLE {table} ADD COLUMN {name} {definition}')
            c.execute('UPDATE events SET max_people=tank+heal+damage WHERE max_people IS NULL')
            # Admission was removed by the owner. Retain old audit data, disable old event flags.
            c.execute('UPDATE events SET require_approved=0 WHERE require_approved<>0')
            # Legacy descriptions may identify a class; never guess armor from gear/class.
            for r in c.execute("SELECT id,details FROM characters WHERE class_name=''").fetchall():
                try: name=class_name(r['details'].split('/')[0])
                except RuleError: continue
                c.execute('UPDATE characters SET class_name=? WHERE id=?',(name,r['id']))
            for r in c.execute("SELECT seq,snapshot FROM signups WHERE class_snapshot=''").fetchall():
                parts=r['snapshot'].split(' · ')
                try:legacy_class=class_name(parts[2].split('/')[0])
                except (RuleError,IndexError):legacy_class=''
                armor=r['snapshot'].rsplit(' · броня: ',1)[-1] if ' · броня: ' in r['snapshot'] else ''
                c.execute('UPDATE signups SET class_snapshot=?,armor_snapshot=? WHERE seq=?',(legacy_class,armor if armor in ARMORS else '',r['seq']))

    @contextmanager
    def tx(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA foreign_keys=ON')
        c.execute('BEGIN IMMEDIATE')
        try:
            yield c
            c.commit()
        except BaseException:
            c.rollback()
            raise
        finally:
            c.close()

    def characters(self, user):
        with self.tx() as c:
            return [dict(x) for x in c.execute('SELECT * FROM characters WHERE user=? ORDER BY id',(user,))]

    def all_characters(self):
        with self.tx() as c:
            return [dict(x) for x in c.execute('SELECT * FROM characters ORDER BY user,id')]

    def character(self, user, name, realm, details, roles, character_class=None, armor=None):
        if not roles or not set(roles) <= set(ROLES):
            raise RuleError('Роли через запятую: танк, хил, дд.')
        if not name.strip() or not realm.strip() or max(len(name),len(realm))>60 or len(details)>350:
            raise RuleError('Ник и мир: 1–60 символов; описание — до 350.')
        with self.tx() as c:
            old=c.execute('SELECT * FROM characters WHERE user=? AND name=? AND realm=?',(user,name,realm)).fetchone()
            if character_class is None:
                try: character_class=class_name(details.split('/')[0])
                except RuleError: character_class=old['class_name'] if old else ''
            else: character_class=class_name(character_class)
            armor=armor.strip().casefold() if armor is not None else (old['armor'] if old else '')
            if armor and armor not in ARMORS:raise RuleError('Тип брони: ткань, кожа, кольчуга или латы.')
            if old and (old['class_name']!=character_class or old['armor']!=armor):
                if c.execute("SELECT 1 FROM signups s JOIN events e ON e.id=s.event WHERE s.character=? AND e.state IN ('набор','сбор','идёт')",(old['id'],)).fetchone():
                    raise RuleError('Сначала сними персонажа с незавершённых сборов, затем меняй класс или броню.')
            if c.execute('SELECT count(*) FROM characters WHERE user=?',(user,)).fetchone()[0]>=25 and not c.execute('SELECT 1 FROM characters WHERE user=? AND name=? AND realm=?',(user,name,realm)).fetchone():
                raise RuleError('Максимум 25 персонажей.')
            c.execute('''INSERT INTO characters(user,name,realm,details,roles,class_name,armor) VALUES(?,?,?,?,?,?,?)
                ON CONFLICT(user,name,realm) DO UPDATE SET details=excluded.details,roles=excluded.roles,class_name=excluded.class_name,armor=excluded.armor''',
                (user,name,realm,details,','.join(sorted(set(roles))),character_class,armor))

    def remove_character(self, user, character):
        with self.tx() as c:
            if c.execute('SELECT 1 FROM signups WHERE character=?',(character,)).fetchone():
                raise RuleError('Персонаж связан с историей записей. Можно обновить его карточку.')
            if not c.execute('SELECT 1 FROM characters WHERE id=? AND user=?',(character,user)).fetchone():raise RuleError('Персонаж не найден.')
            c.execute('INSERT OR IGNORE INTO character_audit_archive SELECT * FROM character_audit WHERE character=?',(character,))
            c.execute('DELETE FROM character_audit WHERE character=?',(character,))
            if not c.execute('DELETE FROM characters WHERE id=? AND user=?',(character,user)).rowcount:
                raise RuleError('Персонаж не найден.')

    def create_event(self, title, start, duration, capacities, max_people=None, classes=(), armor='', organizer=None, order=None, setup=False):
        if not 1<=len(title)<=120 or not 15<=duration<=720:
            raise RuleError('Название: 1–120 символов; длительность: 15–720 минут.')
        if len(capacities)!=3 or any(n<0 or n>40 for n in capacities) or not 1<=sum(capacities)<=40:
            raise RuleError('Всего 1–40 мест, без отрицательных значений.')
        if start<=int(datetime.now(timezone.utc).timestamp()):
            raise RuleError('Начало должно быть в будущем.')
        max_people=sum(capacities) if max_people is None else max_people
        if max_people!=sum(capacities):raise RuleError('Максимум людей должен равняться сумме мест танков, хилов и дд.')
        if isinstance(classes,str):classes=[s.strip() for s in classes.split(',') if s.strip()]
        classes=sorted({class_name(s) for s in classes})
        armor=armor.strip().casefold()
        if armor and armor not in ARMORS:raise RuleError('Неизвестный тип брони.')
        with self.tx() as c:
            ident=c.execute('INSERT INTO events(title,start,end,tank,heal,damage,max_people,classes,armor,organizer) VALUES(?,?,?,?,?,?,?,?,?,?)',
                (title,start,start+duration*60,*capacities,max_people,','.join(classes),armor,organizer)).lastrowid
            if setup:c.execute("UPDATE events SET state='настройка' WHERE id=?",(ident,))
            if order is not None:self._link_order(c,order,ident,organizer)
            return ident

    def event(self, event):
        with self.tx() as c:
            row=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not row: raise RuleError('Сбор не найден.')
            return dict(row)

    def events(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM events WHERE state IN ('набор','сбор','идёт')")]

    def message_events(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT * FROM events WHERE message IS NOT NULL')]

    def bind(self,event,channel,message):
        with self.tx() as c:
            c.execute('UPDATE events SET channel=?,message=? WHERE id=?',(channel,message,event))

    def roster(self,event):
        with self.tx() as c:
            return [dict(r) for r in c.execute('SELECT * FROM signups WHERE event=? ORDER BY seq',(event,))]

    def _conflict(self,c,user,event):
        return c.execute('''SELECT 1 FROM signups s JOIN events e ON e.id=s.event
            WHERE s.user=? AND s.event<>? AND s.slot='основа'
            AND e.state IN ('набор','сбор','идёт') AND e.start<? AND e.end>?''',
            (user,event['id'],event['end'],event['start'])).fetchone() is not None

    def join(self,event,user,character,role,reserve=False):
        if role not in ROLES: raise RuleError('Неизвестная роль.')
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not e or e['state']!='набор': raise RuleError('Запись уже закрыта.')
            ch=c.execute('SELECT * FROM characters WHERE id=? AND user=?',(character,user)).fetchone()
            if not ch or role not in ch['roles'].split(','): raise RuleError('Выбери своего персонажа с подходящей ролью.')
            self.eligible(e,ch)
            if c.execute('SELECT 1 FROM signups WHERE event=? AND user=?',(event,user)).fetchone():
                raise RuleError('Ты уже записан. Для смены персонажа сначала откажись.')
            import slots
            seats=self._seats(c,e)
            candidate={'user':user,'role':role,'class_name':ch['class_name'],'armor':ch['armor']}
            if e['class_limit'] and ch['class_name'] not in CLASSES:raise RuleError('Для ограничения повторов укажи класс через /персонаж.')
            if not any(slots.fits(candidate,seat) for seat in seats):raise RuleError('Для этого персонажа нет подходящих мест выбранной роли. Проверь требования сбора и тип брони в /персонаж.')
            possible=self._allocation(c,e,seats,candidate) is not None
            slot='резерв' if reserve or not possible or self._conflict(c,user,e) else 'основа'
            c.execute('INSERT INTO signups(event,user,character,role,slot,snapshot,profile_revision,class_snapshot,armor_snapshot) VALUES(?,?,?,?,?,?,?,?,?)',
                (event,user,character,role,slot,f"{ch['name']} · {ch['realm']} · {ch['details']} · броня: {ch['armor'] or 'не указана'}",ch['revision'],ch['class_name'],ch['armor']))
            return slot

    @staticmethod
    def eligible(e,ch):
        if e['classes'] and ch['class_name'] not in e['classes'].split(','):
            raise RuleError('Класс персонажа не подходит этому сбору. Допущены: '+e['classes'])
        if e['armor'] and ch['armor']!=e['armor']:
            raise RuleError('Для этого сбора нужна броня «'+e['armor']+'». Обнови карточку через /персонаж, если тип не указан.')

    def _promote(self,c,e):
        promoted=[]
        seats=self._seats(c,e)
        for role,col in zip(ROLES,('tank','heal','damage')):
            used=c.execute("SELECT count(*) FROM signups WHERE event=? AND role=? AND slot='основа'",(e['id'],role)).fetchone()[0]
            for r in c.execute("SELECT * FROM signups WHERE event=? AND role=? AND slot='резерв' ORDER BY seq",(e['id'],role)).fetchall():
                if used>=e[col]: break
                if self._conflict(c,r['user'],e): continue
                ch=c.execute('SELECT * FROM characters WHERE id=?',(r['character'],)).fetchone()
                try:self.eligible(e,ch)
                except RuleError:continue
                candidate={'user':r['user'],'role':r['role'],'class_name':ch['class_name'],'armor':ch['armor']}
                if self._allocation(c,e,seats,candidate) is None:continue
                c.execute("UPDATE signups SET slot='основа',ready=0 WHERE seq=?",(r['seq'],))
                promoted.append(r['user']);used+=1
        return promoted

    def leave(self,event,user):
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not e or e['state']!='набор': raise RuleError('Состав зафиксирован. Обратись к лидеру.')
            if not c.execute('DELETE FROM signups WHERE event=? AND user=?',(event,user)).rowcount:
                raise RuleError('Ты не записан.')
            return self._promote(c,e)

    def promote(self,event):
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            return self._promote(c,e) if e and e['state']=='набор' else []

    def ready(self,event,user):
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not e or e['state'] not in ('набор','сбор'): raise RuleError('Подтверждение сейчас недоступно.')
            signup=c.execute('SELECT * FROM signups WHERE event=? AND user=?',(event,user)).fetchone()
            if signup:
                ch=c.execute('SELECT * FROM characters WHERE id=?',(signup['character'],)).fetchone()
                self.eligible(e,ch)
            if not c.execute("UPDATE signups SET ready=1 WHERE event=? AND user=? AND slot='основа'",(event,user)).rowcount:
                raise RuleError('Подтверждение доступно основному составу.')

    def transition(self,event,state):
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not e: raise RuleError('Сбор не найден.')
            allowed={'настройка':{'набор','отменён'},'набор':{'сбор','отменён'},'сбор':{'набор','идёт','отменён'},'идёт':{'завершён','отменён'}}
            if state not in allowed.get(e['state'],set()): raise RuleError('Недопустимый переход состояния.')
            if state=='идёт':
                if self._allocation(c,e,self._seats(c,e)) is None:raise RuleError('Основной состав не заполняет места по требованиям класса и брони.')
                for s in c.execute("SELECT * FROM signups WHERE event=? AND slot='основа'",(event,)).fetchall():
                    ch=c.execute('SELECT * FROM characters WHERE id=?',(s['character'],)).fetchone()
                    self.eligible(e,ch)
                for role,col in zip(ROLES,('tank','heal','damage')):
                    n=c.execute("SELECT count(*) FROM signups WHERE event=? AND role=? AND slot='основа' AND ready=1",(event,role)).fetchone()[0]
                    if n!=e[col]: raise RuleError('Для запуска нужны полный состав и подтверждение каждого.')
            c.execute('UPDATE events SET state=? WHERE id=?',(state,event))
            order=c.execute('SELECT * FROM orders WHERE event=?',(event,)).fetchone()
            if order and order['state'] not in ('закрыт','отменён'):
                target={'набор':'набор','сбор':'набор','идёт':'выполнение','завершён':'ожидает отчёта','отменён':'приостановлен'}[state]
                c.execute('UPDATE orders SET state=? WHERE id=?',(target,order['id']))
                c.execute('INSERT INTO order_audit(order_id,action) VALUES(?,?)',(order['id'],'Сбор: '+state))
            if state=='набор': c.execute('UPDATE signups SET ready=0 WHERE event=?',(event,))

    def character_card(self,ident):
        with self.tx() as c:
            row=c.execute('SELECT * FROM characters WHERE id=?',(ident,)).fetchone()
            if not row:raise RuleError('Карточка персонажа не найдена.')
            return dict(row)

    @staticmethod
    def _seats(c,e):
        import slots
        rules=[dict(r) for r in c.execute('SELECT * FROM event_requirements WHERE event=? ORDER BY id',(e['id'],))]
        return slots.layout(e,rules)

    @staticmethod
    def _allocation(c,e,seats,extra=None):
        import slots
        records=[dict(r) for r in c.execute("SELECT user,role,class_snapshot AS class_name,armor_snapshot AS armor FROM signups WHERE event=? AND slot='основа' ORDER BY seq",(e['id'],))]
        if extra is not None:records.append(extra)
        return slots.allocate(records,seats,e['class_limit'])

    def requirements(self,event):
        with self.tx() as c:return [dict(r) for r in c.execute('SELECT * FROM event_requirements WHERE event=? ORDER BY id',(event,))]

    def seat_allocation(self,event):
        with self.tx() as c:
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            if not e:raise RuleError('Сбор не найден.')
            seats=self._seats(c,e)
            return seats,self._allocation(c,e,seats)

    def _rules_changed(self,c,e,actor,action,details):
        import slots
        seats=self._seats(c,e)
        if not slots.feasible(seats,e['class_limit']):raise RuleError('С такими местами и лимитом повторов классов полный состав невозможен.')
        if self._allocation(c,e,seats) is None:raise RuleError('Новые требования не подходят текущей основе. Измени требования или попроси неподходящего участника снять запись.')
        c.execute('UPDATE signups SET ready=0 WHERE event=?',(e['id'],))
        c.execute('INSERT INTO event_rules_audit(event,actor,action,details) VALUES(?,?,?,?)',(e['id'],actor,action,json.dumps(details,ensure_ascii=False)))
        return self._promote(c,e)

    @staticmethod
    def _editable_event(c,event):
        e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
        if not e or e['state'] not in ('настройка','набор'):raise RuleError('Требования меняются только при настройке или в открытом наборе.')
        return e

    def add_requirement(self,event,actor,role,places,classes='',armor=''):
        if role not in ROLES or not 1<=places<=40:raise RuleError('Выбери роль и количество мест 1–40.')
        if isinstance(classes,str):classes=[s.strip() for s in classes.split(',') if s.strip()]
        classes=','.join(sorted({class_name(s) for s in classes}));armor=armor.strip().casefold()
        if armor and armor not in ARMORS:raise RuleError('Тип брони: ткань, кожа, кольчуга или латы.')
        if not classes and not armor:raise RuleError('Укажи класс или тип брони для выделенных мест.')
        with self.tx() as c:
            e=self._editable_event(c,event)
            ident=c.execute('INSERT INTO event_requirements(event,role,places,classes,armor) VALUES(?,?,?,?,?)',(event,role,places,classes,armor)).lastrowid
            promoted=self._rules_changed(c,e,actor,'Добавлены места',{'rule':ident,'role':role,'places':places,'classes':classes,'armor':armor})
            return ident,promoted

    def remove_requirement(self,event,actor,rule):
        with self.tx() as c:
            e=self._editable_event(c,event)
            if not c.execute('DELETE FROM event_requirements WHERE id=? AND event=?',(rule,event)).rowcount:raise RuleError('Требование с этим номером не найдено в сборе.')
            return self._rules_changed(c,e,actor,'Убраны выделенные места',{'rule':rule})

    def set_class_limit(self,event,actor,maximum):
        if not 0<=maximum<=40:raise RuleError('Лимит класса: 0–40; 0 отключает ограничение.')
        with self.tx() as c:
            e=self._editable_event(c,event)
            if e['class_limit']==maximum:return []
            c.execute('UPDATE events SET class_limit=? WHERE id=?',(maximum,event))
            e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
            return self._rules_changed(c,e,actor,'Лимит повторов класса',{'maximum':maximum})

    def character_history(self,ident):
        with self.tx() as c:return [dict(r) for r in c.execute('SELECT * FROM character_audit WHERE character=? UNION SELECT * FROM character_audit_archive WHERE character=? ORDER BY id DESC',(ident,ident))]

    def character_events(self,ident):
        with self.tx() as c:return [r['event'] for r in c.execute("SELECT s.event FROM signups s JOIN events e ON e.id=s.event WHERE s.character=? AND e.state IN ('набор','сбор','идёт')",(ident,))]

    def ticket_begin(self,user):
        with self.tx() as c:
            old=c.execute("SELECT * FROM tickets WHERE user=? AND state IN ('создаётся','открыто','проверить')",(user,)).fetchone()
            if old: return dict(old),False
            ident=c.execute("INSERT INTO tickets(user,state) VALUES(?,'создаётся')",(user,)).lastrowid
            return dict(id=ident,user=user,state='создаётся',channel=None),True

    def ticket_update(self,ticket,state,channel=None):
        with self.tx() as c:
            c.execute('UPDATE tickets SET state=?,channel=COALESCE(?,channel) WHERE id=?',(state,channel,ticket))

    def ticket_channel(self,channel):
        with self.tx() as c:
            r=c.execute('SELECT * FROM tickets WHERE channel=?',(channel,)).fetchone()
            return dict(r) if r else None

    def ticket(self,ticket):
        with self.tx() as c:
            r=c.execute('SELECT * FROM tickets WHERE id=?',(ticket,)).fetchone()
            return dict(r) if r else None

    def room(self,kind,owner):
        with self.tx() as c:
            r=c.execute('SELECT * FROM rooms WHERE kind=? AND owner=?',(kind,owner)).fetchone()
            return dict(r) if r else None

    def rooms(self):
        with self.tx() as c:
            return [dict(r) for r in c.execute("SELECT * FROM rooms WHERE state<>'удалено'")]

    def claim_room(self,kind,owner):
        if kind not in ('event','ticket'):raise RuleError('Неизвестный вид комнаты.')
        with self.tx() as c:
            c.execute('INSERT OR IGNORE INTO rooms(kind,owner,marker) VALUES(?,?,?)',(kind,owner,f'forever:{kind}:{owner}:{uuid.uuid4().hex}'))
            return dict(c.execute('SELECT * FROM rooms WHERE kind=? AND owner=?',(kind,owner)).fetchone())

    def bind_room(self,kind,owner,channel):
        with self.tx() as c:
            r=c.execute('SELECT * FROM rooms WHERE kind=? AND owner=?',(kind,owner)).fetchone()
            if not r or (r['channel'] and r['channel']!=channel):raise RuleError('Комната уже связана с другим каналом.')
            c.execute("UPDATE rooms SET channel=?,state='открыто' WHERE kind=? AND owner=?",(channel,kind,owner))

    def room_state(self,kind,owner,state):
        with self.tx() as c:c.execute('UPDATE rooms SET state=? WHERE kind=? AND owner=?',(state,kind,owner))

    def schedule_delete(self,kind,owner,when):
        with self.tx() as c:
            c.execute("UPDATE rooms SET state='закрывается',delete_at=COALESCE(delete_at,?) WHERE kind=? AND owner=? AND channel IS NOT NULL AND state IN ('открыто','закрывается')",(when,kind,owner))

    def setting(self,key,initial=None):
        with self.tx() as c:
            if initial is not None:c.execute('INSERT OR IGNORE INTO settings(key,value) VALUES(?,?)',(key,str(initial)))
            r=c.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
            return r['value'] if r else None

    def create_order(self,title,source,external_ref,due,description,operator):
        title=title.strip();external_ref=external_ref.strip();source=source.strip().casefold();description=description.strip()
        if not 1<=len(title)<=120 or not 1<=len(description)<=800 or len(external_ref)>80:raise RuleError('Название до 120 символов, описание 1–800, номер площадки до 80.')
        if source not in ('вручную','funpay','g2g'):raise RuleError('Источник: вручную, FunPay или G2G.')
        if source!='вручную' and not external_ref:raise RuleError('Укажи номер заказа на площадке.')
        if due<=int(datetime.now(timezone.utc).timestamp()):raise RuleError('Срок должен быть в будущем.')
        with self.tx() as c:
            try:ident=c.execute('INSERT INTO orders(title,source,external_ref,due,description,operator) VALUES(?,?,?,?,?,?)',(title,source,external_ref,due,description,operator)).lastrowid
            except sqlite3.IntegrityError:raise RuleError('Заказ с таким источником и номером уже есть.')
            c.execute('INSERT INTO order_audit(order_id,actor,action) VALUES(?,?,?)',(ident,operator,'Создан вручную'))
            return ident

    def order(self,ident):
        with self.tx() as c:
            r=c.execute('SELECT * FROM orders WHERE id=?',(ident,)).fetchone()
            if not r:raise RuleError('Заказ не найден.')
            return dict(r)

    def orders(self):
        with self.tx() as c:return [dict(r) for r in c.execute('SELECT * FROM orders ORDER BY id DESC')]

    def order_for_event(self,event):
        with self.tx() as c:
            r=c.execute('SELECT id FROM orders WHERE event=?',(event,)).fetchone()
            return r['id'] if r else None

    def bind_order(self,ident,channel,message):
        with self.tx() as c:
            r=c.execute('SELECT * FROM orders WHERE id=?',(ident,)).fetchone()
            if not r or (r['message'] and r['message']!=message):raise RuleError('Заказ уже связан с другой карточкой.')
            c.execute('UPDATE orders SET channel=?,message=? WHERE id=?',(channel,message,ident))

    def _link_order(self,c,order,event,actor):
        o=c.execute('SELECT * FROM orders WHERE id=?',(order,)).fetchone()
        e=c.execute('SELECT * FROM events WHERE id=?',(event,)).fetchone()
        if not o or not e:raise RuleError('Заказ или сбор не найден.')
        if o['event'] or o['state']!='новый':raise RuleError('К этому заказу нельзя привязать ещё один сбор.')
        if e['state'] not in ('настройка','набор','сбор'):raise RuleError('Привязать можно только сбор до начала.')
        if c.execute('SELECT 1 FROM orders WHERE event=?',(event,)).fetchone():raise RuleError('Этот сбор уже связан с другим заказом.')
        c.execute("UPDATE orders SET event=?,state='набор' WHERE id=?",(event,order))
        c.execute('INSERT INTO order_audit(order_id,actor,action) VALUES(?,?,?)',(order,actor,'Привязан сбор №'+str(event)))

    def link_order(self,order,event,actor):
        with self.tx() as c:self._link_order(c,order,event,actor)

    def order_action(self,ident,actor,action,text=''):
        text=text.strip()
        with self.tx() as c:
            o=c.execute('SELECT * FROM orders WHERE id=?',(ident,)).fetchone()
            if not o or o['state'] in ('закрыт','отменён'):raise RuleError('Заказ не найден или уже закрыт.')
            if action=='report':
                if o['state']!='ожидает отчёта' or not 5<=len(text)<=1500:raise RuleError('Отчёт 5–1500 символов доступен после завершения сбора.')
                c.execute('UPDATE orders SET report=? WHERE id=?',(text,ident))
            elif action=='close':
                if o['state']!='ожидает отчёта' or not o['report']:raise RuleError('Сначала завершите сбор и сохраните отчёт.')
                c.execute("UPDATE orders SET state='закрыт' WHERE id=?",(ident,))
            elif action=='cancel':
                if o['event'] and c.execute('SELECT state FROM events WHERE id=?',(o['event'],)).fetchone()['state'] not in ('отменён','завершён'):raise RuleError('Сначала отмените или завершите связанный сбор.')
                if not 5<=len(text)<=1500:raise RuleError('Укажи причину отмены: 5–1500 символов.')
                c.execute("UPDATE orders SET state='отменён',report=? WHERE id=?",(text,ident))
            else:raise RuleError('Неизвестное действие заказа.')
            c.execute('INSERT INTO order_audit(order_id,actor,action) VALUES(?,?,?)',(ident,actor,action))
