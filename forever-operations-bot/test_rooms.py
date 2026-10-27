import tempfile
import asyncio
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock,AsyncMock
import discord
from store import Store,RuleError
from rooms import Rooms,owned,permissions

class Constraints(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'db'
        self.db=Store(self.path);self.start=int(time.time())+86400
    def tearDown(self):self.tmp.cleanup()
    def character(self,u,cl,armor):
        self.db.character(u,'Тест','Мир',cl+' / тест',['дд'],cl,armor)
        return self.db.characters(u)[0]['id']
    def test_filters_apply_to_main_and_reserve(self):
        e=self.db.create_event('Тест',self.start,30,(0,0,1),1,'Охотник,Шаман','кольчуга')
        mage=self.character(1,'Маг','ткань');hunter=self.character(2,'Охотник','кольчуга')
        wrongarmor=self.character(3,'Охотник','кожа')
        for reserve in (True,False):
            with self.assertRaises(RuleError):self.db.join(e,1,mage,'дд',reserve)
            with self.assertRaises(RuleError):self.db.join(e,3,wrongarmor,'дд',reserve)
        self.assertEqual(self.db.join(e,2,hunter,'дд'),'основа')
        with self.assertRaises(RuleError):self.character(2,'Маг','ткань')
        self.db.leave(e,2);self.character(2,'Маг','ткань')
    def test_max_and_unknown_values(self):
        with self.assertRaises(RuleError):self.db.create_event('Тест',self.start,30,(1,1,3),6)
        with self.assertRaises(RuleError):self.db.create_event('Тест',self.start,30,(0,0,1),classes='Несуществующий')
        with self.assertRaises(RuleError):self.character(1,'Маг','железо')
    def test_no_armor_inference_and_durable_deletion(self):
        self.db.character(1,'Тест','Мир','Шаман / тест',['дд'])
        char=self.db.characters(1)[0]
        self.assertEqual(char['class_name'],'Шаман');self.assertEqual(char['armor'],'')
        r=self.db.claim_room('ticket',1);self.db.bind_room('ticket',1,123)
        self.db.schedule_delete('ticket',1,99);self.db.schedule_delete('ticket',1,150)
        restored=Store(self.path)
        self.assertEqual(restored.room('ticket',1)['delete_at'],99)
        self.assertEqual(restored.room('ticket',1)['marker'],r['marker'])
        with self.assertRaises(RuleError):restored.bind_room('ticket',1,999)

class Cleanup(unittest.IsolatedAsyncioTestCase):
    async def test_simultaneous_refresh_creates_exactly_one_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(Path(tmp)/'db');e=db.create_event('Тест',int(time.time())+86400,30,(0,0,1),organizer=44);db.transition(e,'сбор')
            guild=MagicMock();guild.id=10
            guild.default_role=MagicMock(spec=discord.Role);guild.default_role.id=1
            guild.me=MagicMock(spec=discord.Member);guild.me.id=2
            cat=MagicMock(spec=discord.CategoryChannel);cat.name='rooms'
            guild.fetch_channels=AsyncMock(return_value=[cat])
            channel=MagicMock(spec=discord.TextChannel);channel.id=100;channel.guild=guild;channel.send=AsyncMock()
            async def create(*args,**kw):
                await asyncio.sleep(0)
                channel.topic=kw['topic'];channel.overwrites=kw['overwrites'];return channel
            guild.create_text_channel=AsyncMock(side_effect=create)
            client=MagicMock();client.get_guild.return_value=guild;client.staff_roles.return_value=[]
            client.fetch_channel=AsyncMock(return_value=channel)
            manager=Rooms(client,db,{'guild_id':10,'activity_category_name':'rooms','delete_delay_seconds':60})
            await asyncio.gather(manager.event(e),manager.event(e))
            guild.create_text_channel.assert_awaited_once()
            self.assertEqual(db.room('event',e)['state'],'открыто')
    async def test_cleanup_deletes_only_verified_owned_channel(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Store(Path(tmp)/'db');client=MagicMock();client.fetch_channel=AsyncMock()
            manager=Rooms(client,db,{'guild_id':10,'delete_delay_seconds':60})
            channels=[]
            for n in (1,2,3):
                room=db.claim_room('ticket',n);db.bind_room('ticket',n,100+n);db.schedule_delete('ticket',n,1)
                ch=MagicMock(spec=discord.TextChannel);ch.id=100+n;ch.guild.id=10;ch.topic=room['marker'];ch.delete=AsyncMock()
                channels.append(ch)
            channels[1].topic='changed';channels[2].guild=MagicMock();channels[2].guild.id=999
            client.fetch_channel.side_effect=channels
            await manager.cleanup()
            channels[0].delete.assert_awaited_once()
            for ch in channels[1:]:ch.delete.assert_not_awaited()
            self.assertEqual(db.room('ticket',1)['state'],'удалено')
            self.assertEqual(db.room('ticket',2)['state'],'метка изменена')
    async def test_acl_is_only_selected_users_staff_and_bot(self):
        guild=MagicMock();staff=[MagicMock()]
        acl=permissions(guild,staff,[123,456])
        self.assertFalse(acl[guild.default_role].view_channel)
        users={k.id for k in acl if isinstance(k,discord.Object)}
        self.assertEqual(users,{123,456})
        self.assertTrue(acl[staff[0]].view_channel)

if __name__=='__main__':unittest.main()
