import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock,MagicMock,patch
import discord
import directory
from store import Store,RuleError

_import_tmp=tempfile.TemporaryDirectory()
if not os.environ.get('FOREVER_DATABASE'):os.environ['FOREVER_DATABASE']=str(Path(_import_tmp.name)/'import.db')
import bot

class DirectoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Store(Path(self.tmp.name)/'db')
        self.patch=patch.object(bot,'DB',self.db);self.patch.start()
        self.db.character(1,'Охотник-А','Мир','Охотник / Стрельба / Орк / ур. 60',['дд'],'Охотник','кольчуга')
        self.db.character(1,'Шаман-А','Мир','Шаман / Исцеление / Таурен / ур. 55',['хил','дд'],'Шаман','кольчуга')
        self.db.character(2,'Маг-Б','Другой','Маг / Лёд / Человек / ур. 60',['дд'],'Маг','ткань')
    async def asyncTearDown(self):self.patch.stop();self.tmp.cleanup()
    def select(self,**filters):return directory.select(self.db.all_characters(),filters)
    def interaction(self,user=1,leader=True):
        i=MagicMock();i.guild_id=bot.CFG['guild_id']
        i.user=MagicMock(spec=discord.Member);i.user.id=user;i.user.guild.owner_id=999
        role=MagicMock();role.name='Лидер группы';i.user.roles=[role] if leader else []
        i.response.defer=AsyncMock();i.response.is_done.return_value=True
        i.edit_original_response=AsyncMock();i.followup.send=AsyncMock()
        return i
    async def test_combined_filters_and_unknown_legacy_level(self):
        self.assertEqual([r['name'] for r in self.select(role='хил',class_name='Шаман',armor='кольчуга',realm='мИр',race='таурен',specialization='цел',min_level=50,max_level=60,user=1)],['Шаман-А'])
        self.assertEqual(self.select(role='хил',class_name='Маг'),[])
        self.assertEqual([r['name'] for r in self.select(search='маг-б')],['Маг-Б'])
        self.db.character(3,'Старый','Мир','Старое описание',['дд'])
        self.assertFalse(any(r['user']==3 for r in self.select(min_level=1)))
        self.assertTrue(any(r['user']==3 for r in self.select()))
    async def test_unique_people_and_multirole_counts(self):
        rows=self.select();groups=dict(directory.groups(rows,'люди'))
        self.assertEqual(len(rows),3);self.assertEqual(len(groups),2);self.assertEqual(len(groups[1]),2)
        roles=dict(directory.groups(rows,'роли'))
        self.assertEqual(len(roles['дд']),3);self.assertEqual(len(roles['хил']),1)
        report=directory.export_text(rows,{},'классы')
        self.assertIn('Персонажей: 3; людей: 2',report)
    async def test_invalid_filters_rejected(self):
        for filters in ({'min_level':61,'max_level':60},{'role':'DD'},{'search':'x'*101},{'query':'SQL'}):
            with self.assertRaises(RuleError):self.select(**filters)
    async def test_menus_requery_current_data_and_reset(self):
        view=bot.DirectoryView(1,{'realm':'Мир'});view.render()
        i=self.interaction()
        await view.change(i,'role','хил')
        self.assertEqual(view.filters,{'realm':'Мир','role':'хил'})
        em=i.edit_original_response.call_args.kwargs['embed'];self.assertIn('Персонажей: 1',em.description)
        self.db.character(4,'Шаман-Б','Мир','Шаман / Исцеление / Орк / ур. 60',['хил'],'Шаман','кольчуга')
        await view.change(i,'mode','люди')
        self.assertIn('Людей: 2',i.edit_original_response.call_args.kwargs['embed'].description)
        await view.change(i,'reset');self.assertEqual(view.filters,{})
    async def test_permissions_checked_again_on_every_interaction(self):
        view=bot.DirectoryView(1);view.render()
        for i in (self.interaction(user=2),self.interaction(leader=False)):
            with self.assertRaises(RuleError):await view.change(i,'export')
            i.followup.send.assert_not_awaited();i.edit_original_response.assert_not_awaited()
    async def test_export_is_private_and_full(self):
        view=bot.DirectoryView(1,{'armor':'кольчуга'});view.render();i=self.interaction()
        await view.change(i,'export')
        args=i.followup.send.call_args.kwargs
        self.assertTrue(args['ephemeral']);text=args['file'].fp.getvalue().decode('utf-8')
        self.assertIn('Шаман-А',text);self.assertIn('Охотник-А',text);self.assertNotIn('Маг-Б',text)
    async def test_pagination_and_discord_limits_with_long_profiles(self):
        for u in range(10,35):
            for n in range(6):self.db.character(u,'*'*58+str(n),'*'*60,'*'*350,['танк','хил','дд'])
        for mode in directory.VIEWS:
            view=bot.DirectoryView(1,mode=mode);embed=view.render()
            self.assertLessEqual(len(embed),6000)
            self.assertTrue(all(len(f.value)<=1024 and len(f.name)<=256 for f in embed.fields))
            self.assertEqual(len(view.children),8)
            self.assertTrue(all(len(c.options)<=25 for c in view.children if isinstance(c,discord.ui.Select)))
        view=bot.DirectoryView(1);view.render();i=self.interaction();await view.change(i,'next')
        self.assertEqual(view.page,1);self.assertIn('Страница 2/',i.edit_original_response.call_args.kwargs['embed'].footer.text)
        view.page=999;view.render();self.assertLess(view.page,999)

if __name__=='__main__':unittest.main()
