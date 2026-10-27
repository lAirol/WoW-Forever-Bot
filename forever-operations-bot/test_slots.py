import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from itertools import permutations
import random
from unittest.mock import patch
from store import Store,RuleError
import slots
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('FOREVER_DATABASE',str(Path(_tmp.name)/'import.db'))
import bot
import requirements_ui

class SeatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.db=Store(self.path)
        self.patch=patch.object(bot,'DB',self.db);self.patch.start();self.start=int(time.time())+86400
    async def asyncTearDown(self):self.patch.stop();self.tmp.cleanup()
    def event(self,caps=(0,0,2),**kwargs):return self.db.create_event('Тест мест',self.start,60,caps,**kwargs)
    def char(self,user,cl='Маг',armor='ткань',role='дд'):
        self.db.character(user,'Игрок'+str(user),'Мир',cl+' / Тест / Орк / ур. 60',[role],cl,armor)
        return self.db.characters(user)[0]['id']
    def join(self,e,u,cl='Маг',armor='ткань',role='дд',reserve=False):return self.db.join(e,u,self.char(u,cl,armor,role),role,reserve)
    async def test_separate_healer_and_damage_armor_requirements(self):
        e=self.event((0,1,2));self.db.add_requirement(e,99,'хил',1,'Шаман');self.db.add_requirement(e,99,'дд',2,armor='кольчуга')
        with self.assertRaises(RuleError):self.join(e,1,'Жрец',role='хил')
        self.assertEqual(self.join(e,2,'Шаман','кольчуга','хил'),'основа')
        with self.assertRaises(RuleError):self.join(e,3)
        self.assertEqual(self.join(e,4,'Охотник','кольчуга'),'основа')
        self.assertEqual(self.join(e,5,'Шаман','кольчуга'),'основа')
        for u in (2,4,5):self.db.ready(e,u)
        self.db.transition(e,'сбор');self.db.transition(e,'идёт')
    async def test_reserved_seat_stays_open_and_fifo_skips_wrong_armor(self):
        e=self.event();self.db.add_requirement(e,99,'дд',1,armor='кольчуга')
        self.assertEqual(self.join(e,1),'основа');self.assertEqual(self.join(e,2),'резерв')
        self.assertEqual(self.join(e,3,'Охотник','кольчуга'),'основа')
        self.assertEqual(self.db.leave(e,3),[])
        self.assertEqual(self.join(e,4,'Шаман','кольчуга',reserve=True),'резерв')
        self.assertEqual(self.db.promote(e),[4])
        self.assertEqual(next(r for r in self.db.roster(e) if r['user']==2)['slot'],'резерв')
    async def test_overlapping_class_choices_are_rematched_not_greedily_rejected(self):
        e=self.event();a,_=self.db.add_requirement(e,99,'дд',1,'Маг,Шаман');b,_=self.db.add_requirement(e,99,'дд',1,'Маг')
        self.assertEqual(self.join(e,1),'основа');self.db.ready(e,1)
        self.assertEqual(self.join(e,2,'Шаман','кольчуга'),'основа')
        seats,mapping=self.db.seat_allocation(e)
        self.assertEqual(seats[mapping[1]]['rule_id'],b);self.assertEqual(seats[mapping[2]]['rule_id'],a)
        self.assertEqual(next(r for r in self.db.roster(e) if r['user']==1)['ready'],1)
    async def test_class_limit_applies_across_roles(self):
        e=self.event((0,1,2));self.db.set_class_limit(e,99,1)
        self.assertEqual(self.join(e,1,'Шаман','кольчуга','хил'),'основа')
        self.assertEqual(self.join(e,2,'Шаман','кольчуга'),'резерв')
        self.assertEqual(self.join(e,3),'основа')
        self.assertEqual(self.db.leave(e,1),[2])
        self.assertEqual(self.join(e,4,'Жрец','ткань','хил'),'основа')
    async def test_incompatible_rule_change_rolls_back_and_preserves_readiness(self):
        e=self.event((0,0,1));self.join(e,1);self.db.ready(e,1)
        with self.assertRaises(RuleError):self.db.add_requirement(e,99,'дд',1,'Охотник')
        self.assertEqual(self.db.requirements(e),[]);self.assertEqual(self.db.roster(e)[0]['ready'],1)
        self.db.add_requirement(e,99,'дд',1,'Маг');self.assertEqual(self.db.roster(e)[0]['ready'],0)
    async def test_role_quota_cannot_exceed_capacity(self):
        e=self.event((0,1,1));self.db.add_requirement(e,99,'хил',1,'Шаман')
        with self.assertRaises(RuleError):self.db.add_requirement(e,99,'хил',1,'Жрец')
        self.assertEqual(len(self.db.requirements(e)),1)
    async def test_global_filters_cannot_be_contradicted(self):
        e=self.event(classes='Маг',armor='ткань')
        for classes,armor in [('Охотник',''),('','кольчуга')]:
            with self.assertRaises(RuleError):self.db.add_requirement(e,99,'дд',1,classes,armor)
        self.assertEqual(self.db.requirements(e),[])
    async def test_impossible_class_limit_and_class_quotas_are_rejected(self):
        e=self.event();self.db.add_requirement(e,99,'дд',2,'Шаман')
        with self.assertRaises(RuleError):self.db.set_class_limit(e,99,1)
        self.assertEqual(self.db.event(e)['class_limit'],0)
        e2=self.event((0,0,10))
        with self.assertRaises(RuleError):self.db.set_class_limit(e2,99,1)
    async def test_locked_and_terminal_rules_cannot_change(self):
        e=self.event();rule,_=self.db.add_requirement(e,99,'дд',1,'Маг');self.db.transition(e,'сбор')
        for action in (lambda:self.db.remove_requirement(e,99,rule),lambda:self.db.set_class_limit(e,99,1),lambda:self.db.add_requirement(e,99,'дд',1,'Шаман')):
            with self.assertRaises(RuleError):action()
        self.db.transition(e,'отменён')
        with self.assertRaises(RuleError):self.db.remove_requirement(e,99,rule)
    async def test_removing_rule_can_promote_waiting_player(self):
        e=self.event();rule,_=self.db.add_requirement(e,99,'дд',1,armor='кольчуга')
        self.join(e,1);self.join(e,2);self.db.ready(e,1)
        self.assertEqual(self.db.remove_requirement(e,99,rule),[2])
        self.assertTrue(all(r['ready']==0 for r in self.db.roster(e)))
        self.assertEqual(self.db.event(e)['damage'],2)
    async def test_rules_persist_and_historical_seats_use_signup_snapshots(self):
        e=self.event((0,0,1));self.db.add_requirement(e,99,'дд',1,'Маг');self.join(e,1);self.db.ready(e,1)
        for state in ('сбор','идёт','завершён'):self.db.transition(e,state)
        self.db.character(1,'Игрок1','Мир','Шаман / Тест / Орк / ур. 60',['дд'],'Шаман','кольчуга')
        restored=Store(self.path);seats,mapping=restored.seat_allocation(e)
        self.assertEqual(len(restored.requirements(e)),1);self.assertEqual(mapping,{1:0})
        self.assertEqual(seats[0]['classes'],('Маг',))
    async def test_concurrent_last_class_seats_are_atomic(self):
        e=self.event();self.db.add_requirement(e,99,'дд',1,'Маг');self.db.add_requirement(e,99,'дд',1,'Охотник')
        chars={u:self.char(u,'Маг' if u%2 else 'Охотник','ткань' if u%2 else 'кольчуга') for u in range(1,17)}
        with ThreadPoolExecutor(max_workers=8) as p:results=list(p.map(lambda u:self.db.join(e,u,chars[u],'дд'),chars))
        self.assertEqual(results.count('основа'),2);self.assertEqual(results.count('резерв'),14)
        self.assertEqual({r['class_snapshot'] for r in self.db.roster(e) if r['slot']=='основа'},{'Маг','Охотник'})
    async def test_start_rechecks_matching_even_if_database_was_corrupted(self):
        e=self.event((0,0,1));self.db.add_requirement(e,99,'дд',1,'Маг');self.join(e,1);self.db.ready(e,1);self.db.transition(e,'сбор')
        with self.db.tx() as c:c.execute("UPDATE signups SET class_snapshot='Охотник' WHERE event=?",(e,))
        with self.assertRaises(RuleError):self.db.transition(e,'идёт')
    async def test_setup_mode_blocks_signup_until_opened(self):
        e=self.event((0,0,1),setup=True);ch=self.char(1)
        with self.assertRaises(RuleError):self.db.join(e,1,ch,'дд')
        self.db.add_requirement(e,99,'дд',1,'Маг');self.db.set_class_limit(e,99,1)
        self.db.transition(e,'набор');self.assertEqual(self.db.join(e,1,ch,'дд'),'основа')
    async def test_large_requirements_render_and_export_without_discord_overflow(self):
        e=self.event((0,0,40))
        for _ in range(40):self.db.add_requirement(e,99,'дд',1,'Маг')
        em=bot.card(e);self.assertLessEqual(len(em),6000);self.assertTrue(all(len(f.value)<=1024 for f in em.fields))
        text=requirements_ui.full_text(self.db,e)
        self.assertIn('Место 40:',text);self.assertIn('правило №40',text)
    async def test_matching_agrees_with_exhaustive_small_oracle(self):
        rng=random.Random(42)
        for _ in range(100):
            n=rng.randint(1,5);count=rng.randint(0,n+1)
            seats=[{'role':'дд','classes':tuple(rng.sample(['Маг','Шаман','Охотник'],rng.randint(0,3))),'armor':rng.choice(['','ткань','кольчуга'])} for _ in range(n)]
            records=[{'user':i,'role':'дд','class_name':rng.choice(['Маг','Шаман','Охотник']),'armor':rng.choice(['ткань','кольчуга'])} for i in range(count)]
            oracle=any(all(slots.fits(records[i],seats[j]) for i,j in enumerate(assignment)) for assignment in permutations(range(n),count))
            self.assertEqual(slots.matching(records,seats) is not None,oracle)

if __name__=='__main__':unittest.main()
