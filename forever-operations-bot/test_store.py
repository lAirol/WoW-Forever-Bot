from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from store import Store,RuleError,utc

class RosterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'test.sqlite3'
        self.db=Store(self.path)
        self.start=int(datetime.now(timezone.utc).timestamp())+86400
    def tearDown(self): self.tmp.cleanup()
    def char(self,user,roles=('дд',)):
        self.db.character(user,f'Игрок{user}','Мир','Маг / Лёд / Человек / ур. 60',roles)
        return self.db.characters(user)[0]['id']
    def event(self,caps=(0,0,1),start=None):
        return self.db.create_event('Рейд',start or self.start,60,caps)
    def test_last_slot_is_atomic(self):
        event=self.event()
        chars={u:self.char(u) for u in range(1,17)}
        with ThreadPoolExecutor(max_workers=8) as pool:
            result=list(pool.map(lambda u:self.db.join(event,u,chars[u],'дд'),chars))
        self.assertEqual(result.count('основа'),1)
        self.assertEqual(result.count('резерв'),15)
    def test_fifo_promotion_and_ready_reset(self):
        event=self.event()
        for u in (1,2,3): self.db.join(event,u,self.char(u),'дд')
        self.db.ready(event,1)
        self.assertEqual(self.db.leave(event,1),[2])
        self.assertEqual(self.db.roster(event)[0]['ready'],0)
    def test_cannot_use_someone_elses_character_or_wrong_role(self):
        event=self.event();char=self.char(1)
        with self.assertRaises(RuleError):self.db.join(event,2,char,'дд')
        with self.assertRaises(RuleError):self.db.join(event,1,char,'танк')
        self.assertEqual(self.db.roster(event),[])
    def test_double_signup_rejected(self):
        event=self.event();char=self.char(1)
        self.db.join(event,1,char,'дд')
        with self.assertRaises(RuleError):self.db.join(event,1,char,'дд')
        self.assertEqual(len(self.db.roster(event)),1)
    def test_overlap_is_reserve_then_promotes_when_freed(self):
        a=self.event();b=self.event();char=self.char(1)
        self.assertEqual(self.db.join(a,1,char,'дд'),'основа')
        self.assertEqual(self.db.join(b,1,char,'дд'),'резерв')
        self.db.transition(a,'отменён')
        self.assertEqual(self.db.promote(b),[1])
    def test_adjacent_times_do_not_conflict(self):
        a=self.event();b=self.event(start=self.start+3600);char=self.char(1)
        self.db.join(a,1,char,'дд')
        self.assertEqual(self.db.join(b,1,char,'дд'),'основа')
    def test_full_ready_roster_required_and_locked_roster_stable(self):
        event=self.event();char=self.char(1)
        self.db.transition(event,'сбор')
        with self.assertRaises(RuleError):self.db.transition(event,'идёт')
        with self.assertRaises(RuleError):self.db.join(event,1,char,'дд')
        self.db.transition(event,'набор')
        self.db.join(event,1,char,'дд');self.db.transition(event,'сбор')
        with self.assertRaises(RuleError):self.db.leave(event,1)
        self.db.ready(event,1);self.db.transition(event,'идёт');self.db.transition(event,'завершён')
        with self.assertRaises(RuleError):self.db.transition(event,'набор')
    def test_restart_preserves_roster_message_and_snapshot(self):
        event=self.event();char=self.char(1)
        self.db.join(event,1,char,'дд');self.db.bind(event,123,456)
        self.db.character(1,'Игрок1','Мир','Другое описание',['дд'])
        restarted=Store(self.path)
        self.assertEqual(restarted.event(event)['message'],456)
        self.assertIn('Маг',restarted.roster(event)[0]['snapshot'])
    def test_archived_card_remains_available_for_persistent_view(self):
        event=self.event()
        self.db.bind(event,123,456)
        self.db.transition(event,'отменён')
        self.assertEqual(self.db.events(),[])
        self.assertEqual(self.db.message_events()[0]['message'],456)
    def test_one_ticket_even_concurrently_and_unknown_result_blocks_retry(self):
        with ThreadPoolExecutor(max_workers=6) as pool:
            tickets=list(pool.map(lambda _:self.db.ticket_begin(1),range(12)))
        self.assertEqual(sum(fresh for _,fresh in tickets),1)
        t=tickets[0][0]
        self.db.ticket_update(t['id'],'проверить')
        self.assertFalse(self.db.ticket_begin(1)[1])
    def test_closed_ticket_can_be_followed_by_new_one(self):
        a,_=self.db.ticket_begin(1);self.db.ticket_update(a['id'],'открыто',333)
        self.assertEqual(self.db.ticket_channel(333)['user'],1)
        self.db.ticket_update(a['id'],'закрыто')
        b,fresh=self.db.ticket_begin(1)
        self.assertTrue(fresh);self.assertNotEqual(a['id'],b['id'])
    def test_timezone_required_and_equivalent_offsets(self):
        with self.assertRaises(RuleError):utc('2027-01-01T20:00')
        self.assertEqual(utc('2027-01-01T20:00+03:00'),utc('2027-01-01T17:00Z'))
    def test_character_owner_controls_and_history_preserved(self):
        char=self.char(1)
        with self.assertRaises(RuleError):self.db.remove_character(2,char)
        self.db.join(self.event(),1,char,'дд')
        with self.assertRaises(RuleError):self.db.remove_character(1,char)

if __name__=='__main__': unittest.main()
