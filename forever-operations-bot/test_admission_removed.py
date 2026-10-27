import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from store import Store,RuleError
import directory
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('FOREVER_DATABASE',str(Path(_tmp.name)/'import.db'))
import bot
import operations

class AdmissionRemoved(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.db=Store(self.path)
        self.patch=patch.object(bot,'DB',self.db);self.patch.start()
    async def asyncTearDown(self):self.patch.stop();self.tmp.cleanup()
    def char(self,user):
        self.db.character(user,'Тест','Мир','Маг / Лёд / Человек / ур. 60',['дд'],'Маг','ткань')
        return self.db.characters(user)[0]['id']
    def event(self):return self.db.create_event('Тест',int(time.time())+86400,30,(0,0,1))
    async def test_old_suspended_and_expired_statuses_do_not_block_any_step(self):
        first=self.char(1);second=self.char(2);e=self.event()
        with self.db.tx() as c:
            c.execute("UPDATE characters SET admission='отстранён' WHERE id=?",(first,))
            c.execute("UPDATE characters SET admission='допущен',approval_until=1 WHERE id=?",(second,))
            c.execute('UPDATE events SET require_approved=1 WHERE id=?',(e,))
        self.assertEqual(self.db.join(e,1,first,'дд'),'основа')
        self.assertEqual(self.db.join(e,2,second,'дд'),'резерв')
        self.assertEqual(self.db.leave(e,1),[2]);self.db.ready(e,2)
        self.db.transition(e,'сбор');self.db.transition(e,'идёт')
        self.assertEqual(self.db.event(e)['state'],'идёт')
    async def test_old_strict_flags_disabled_on_restart_without_losing_history(self):
        ch=self.char(1);e=self.event()
        with self.db.tx() as c:
            c.execute('UPDATE events SET require_approved=1 WHERE id=?',(e,))
            c.execute("INSERT INTO character_audit(character,revision,actor,status,reason,at) VALUES(?,0,1,'допущен','Историческое решение',1)",(ch,))
        restored=Store(self.path)
        self.assertEqual(restored.event(e)['require_approved'],0)
        self.assertEqual(len(restored.character_history(ch)),1)
    async def test_old_audit_does_not_prevent_unreferenced_character_deletion(self):
        ch=self.char(1)
        with self.db.tx() as c:c.execute("INSERT INTO character_audit(character,revision,actor,status,reason,at) VALUES(?,0,1,'не проверен','Старая запись',1)",(ch,))
        self.db.remove_character(1,ch)
        self.assertEqual(self.db.characters(1),[])
        self.assertEqual(self.db.character_history(ch)[0]['reason'],'Старая запись')
    async def test_commands_panels_reports_have_no_admission_controls(self):
        self.char(1)
        self.assertNotIn('допуск',directory.VIEWS)
        with self.assertRaises(RuleError):directory.select(self.db.all_characters(),{'admission':'допущен'})
        self.assertNotIn('допуск',directory.export_text(directory.select(self.db.all_characters(),{}),{},'персонажи').lower())
        self.assertIsNone(bot.client.tree.get_command('проверка-персонажа'))
        for cmd in bot.client.tree.get_commands():
            self.assertTrue(all(p.name not in ('допуск','проверенные') for p in cmd.parameters))
        self.assertFalse(any(c.custom_id=='forever:panel:review' for c in operations.PanelView(bot.client.ops).children))
        self.assertTrue(all('допуск' not in f.name.lower() for f in bot.card(self.event()).fields))

if __name__=='__main__':unittest.main()
