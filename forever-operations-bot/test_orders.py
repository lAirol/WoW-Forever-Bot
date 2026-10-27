import os
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock,AsyncMock,patch
import discord
from store import Store,RuleError
_tmp=tempfile.TemporaryDirectory()
os.environ.setdefault('FOREVER_DATABASE',str(Path(_tmp.name)/'import.db'))
import bot
import operations

class Orders(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db';self.db=Store(self.path)
        self.patch=patch.object(bot,'DB',self.db);self.patch.start();self.start=int(time.time())+86400
    async def asyncTearDown(self):self.patch.stop();self.tmp.cleanup()
    def order(self,ref=''):return self.db.create_order('Услуга','вручную',ref,self.start+7200,'Проверка задачи',1)
    def event(self,order=None):return self.db.create_event('Группа',self.start,30,(0,0,1),organizer=1,order=order)
    def finish(self,event):
        self.db.character(1,'Тест','Мир','Маг / Лёд / Человек / ур. 60',['дд'])
        self.db.join(event,1,self.db.characters(1)[0]['id'],'дд');self.db.ready(event,1)
        for state in ('сбор','идёт','завершён'):self.db.transition(event,state)
    async def test_cycle_report_required_and_persistent(self):
        o=self.order();e=self.event(o)
        self.assertEqual(self.db.order(o)['state'],'набор')
        with self.assertRaises(RuleError):self.db.order_action(o,1,'close')
        self.finish(e);self.assertEqual(self.db.order(o)['state'],'ожидает отчёта')
        with self.assertRaises(RuleError):self.db.order_action(o,1,'close')
        self.db.order_action(o,1,'report','Работа выполнена.');self.db.order_action(o,1,'close')
        self.assertEqual(Store(self.path).order(o)['state'],'закрыт')
        with self.assertRaises(RuleError):self.db.order_action(o,1,'report','Подмена отчёта')
    async def test_atomic_event_creation_rolls_back_on_invalid_link(self):
        with self.assertRaises(RuleError):self.event(999)
        self.assertEqual(self.db.events(),[])
    async def test_one_order_per_event_and_one_event_per_order(self):
        o=self.order();other=self.order();e=self.event(o)
        with self.assertRaises(RuleError):self.db.link_order(other,e,1)
        with self.assertRaises(RuleError):self.event(o)
        self.assertEqual(len(self.db.events()),1)
    async def test_cancel_requires_safe_event_state_and_reason(self):
        o=self.order();e=self.event(o)
        with self.assertRaises(RuleError):self.db.order_action(o,1,'cancel','Передумали')
        self.db.transition(e,'отменён');self.assertEqual(self.db.order(o)['state'],'приостановлен')
        with self.assertRaises(RuleError):self.db.order_action(o,1,'cancel','')
        self.db.order_action(o,1,'cancel','Отменено оператором')
        self.assertEqual(self.db.order(o)['state'],'отменён')
    async def test_reference_unique_under_concurrency(self):
        def create(_):
            try:return self.db.create_order('Услуга','FunPay','123',self.start,'Техническая задача',1)
            except RuleError:return None
        with ThreadPoolExecutor(max_workers=4) as p:results=list(p.map(create,range(8)))
        self.assertEqual(sum(v is not None for v in results),1)
    async def test_panels_persist_and_unauthorized_modal_rejected(self):
        self.assertTrue(operations.PanelView(bot.client.ops).is_persistent())
        order=self.order();view=operations.OrderView(bot.client.ops,order)
        self.assertTrue(view.is_persistent())
        modal=operations.OrderModal(bot.client.ops)
        self.assertEqual(len(modal.children),5)
        i=MagicMock();i.guild_id=bot.CFG['guild_id'];i.user=MagicMock(spec=discord.Member)
        i.user.id=2;i.user.guild.owner_id=1;i.user.roles=[];i.response.defer=AsyncMock()
        with self.assertRaises(RuleError):await modal.on_submit(i)
        i.response.defer.assert_not_awaited();self.assertEqual(len(self.db.orders()),1)
    async def test_order_card_limits_and_closed_buttons(self):
        order=self.db.create_order('*'*120,'вручную','*'*80,self.start,'*'*800,1)
        e=self.event(order);self.finish(e);self.db.order_action(order,1,'report','*'*1500);self.db.order_action(order,1,'close')
        embed=bot.client.ops.card(order)
        self.assertLessEqual(len(embed),6000)
        self.assertTrue(all(len(f.value)<=1024 for f in embed.fields))
        view=operations.OrderView(bot.client.ops,order)
        self.assertTrue(all(c.disabled for c in view.children if c.key!='export'))
    async def test_audit_and_binding_cannot_be_replaced(self):
        o=self.order();self.db.bind_order(o,1,2)
        with self.assertRaises(RuleError):self.db.bind_order(o,1,3)
        with self.db.tx() as c:self.assertEqual(c.execute('SELECT actor FROM order_audit WHERE order_id=?',(o,)).fetchone()['actor'],1)

if __name__=='__main__':unittest.main()
