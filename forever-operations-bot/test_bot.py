"""Offline adapter checks; no Discord API, no credentials."""
import asyncio
from datetime import datetime,timezone
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# Isolate the import-time DB from deliverable files.
_import_tmp=tempfile.TemporaryDirectory()
os.environ['FOREVER_DATABASE']=str(Path(_import_tmp.name)/'import.sqlite3')
import bot
import discord
from store import Store,RuleError

class AdapterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Store(Path(self.tmp.name)/'db.sqlite3')
        self.patch=patch.object(bot,'DB',self.db);self.patch.start()
    async def asyncTearDown(self):
        self.patch.stop();self.tmp.cleanup()
    def make_event(self):
        return self.db.create_event('Тестовый рейд',int(datetime.now(timezone.utc).timestamp())+86400,60,(1,1,3))
    async def test_persistent_buttons_survive_reconstruction(self):
        e=self.make_event()
        original=bot.EventView(e)
        restored=bot.EventView(e)
        self.assertTrue(restored.is_persistent())
        self.assertEqual([c.custom_id for c in original.children],[c.custom_id for c in restored.children])
        self.assertEqual(len(set(c.custom_id for c in restored.children)),10)
    async def test_locked_and_finished_views_enforce_state(self):
        e=self.make_event();self.db.transition(e,'сбор')
        view=bot.EventView(e)
        self.assertTrue(all(c.disabled for c in view.children if isinstance(c,bot.JoinButton)))
        self.db.transition(e,'отменён')
        view=bot.EventView(e)
        self.assertTrue(all(c.disabled for c in view.children if getattr(c,'action',None)=='ready'))
        self.assertFalse(next(c for c in view.children if getattr(c,'action',None)=='roster').disabled)
    async def test_card_obeys_discord_field_limits_under_full_roster(self):
        e=self.db.create_event('Рейд',int(datetime.now(timezone.utc).timestamp())+86400,60,(0,0,40))
        for u in range(1,81):
            self.db.character(u,'А'*60,'М'*60,'О'*350,['дд'])
            self.db.join(e,u,self.db.characters(u)[0]['id'],'дд')
        em=bot.card(e)
        self.assertTrue(all(len(f.value)<=1024 for f in em.fields))
        self.assertLess(len(em),6000)
    async def test_commands_are_guild_only_and_serialize(self):
        for cmd in bot.client.tree.get_commands():
            payload=cmd.to_dict(bot.client.tree)
            self.assertTrue(payload['name'])
            self.assertTrue(cmd.guild_only)
            self.assertLessEqual(len(payload.get('options',[])),25)
        self.assertFalse(bot.client.intents.message_content)
        self.assertFalse(bot.client.intents.members)
    async def test_role_guard_and_other_server(self):
        member=MagicMock(spec=discord.Member)
        member.id=2;member.guild.owner_id=1
        role=MagicMock();role.name='СОГИЛЬДЕЕЦ';member.roles=[role]
        self.assertTrue(bot.role_check(member,['согильдеец']))
        self.assertFalse(bot.role_check(member,['Оператор']))
        i=MagicMock();i.guild_id=123
        with self.assertRaises(RuleError):bot.check(i)
    async def test_ticket_panel_is_persistent(self):
        self.assertTrue(bot.TicketView().is_persistent())

if __name__=='__main__': unittest.main()
