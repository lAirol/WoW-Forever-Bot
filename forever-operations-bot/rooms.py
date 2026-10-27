"""Owned temporary Discord rooms; IDs plus random topic markers guard deletion."""
import time
import asyncio
import discord
from store import RuleError

def owned(channel,room,guild_id):
    return (isinstance(channel,discord.TextChannel) and channel.guild.id==guild_id
            and channel.id==room['channel'] and channel.topic==room['marker'])

def permissions(guild,staff,users):
    access=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
    result={guild.default_role:discord.PermissionOverwrite(view_channel=False),guild.me:access}
    result.update({role:access for role in staff})
    result.update({discord.Object(int(user),type=discord.Member):access for user in users})
    return result

def signature(overwrites):
    return sorted((key.id, isinstance(key,discord.Role) or getattr(key,'type',None) is discord.Role,
                   value.pair()[0].value,value.pair()[1].value) for key,value in overwrites.items())

class Rooms:
    def __init__(self,client,db,cfg):
        self.client,self.db,self.cfg=client,db,cfg
        self.category_lock=asyncio.Lock()
        self.event_locks={}
    async def event(self,event):
        async with self.event_locks.setdefault(event,asyncio.Lock()):
            await self._event(event)
    async def _event(self,event):
        db=self.db;e=db.event(event);room=db.room('event',event)
        if room and not room['channel']:
            guild=self.client.get_guild(self.cfg['guild_id'])
            matches=[c for c in await guild.fetch_channels() if isinstance(c,discord.TextChannel) and c.topic==room['marker']]
            if len(matches)==1:
                db.bind_room('event',event,matches[0].id);room=db.room('event',event)
            else:return
        if e['state'] in ('завершён','отменён'):
            if room and room['channel']:await self.schedule('event',event)
            return
        if not room and e['state'] not in ('сбор','идёт'):return
        guild=self.client.get_guild(self.cfg['guild_id'])
        users={r['user'] for r in db.roster(event) if r['slot']=='основа'}
        if e['organizer']:users.add(e['organizer'])
        acl=permissions(guild,self.client.staff_roles(guild),users)
        if not room:
            async with self.category_lock:
                categories=[c for c in await guild.fetch_channels() if isinstance(c,discord.CategoryChannel) and c.name==self.cfg['activity_category_name']]
                if len(categories)>1:raise RuleError('Дублируется категория активностей.')
                category=categories[0] if categories else await guild.create_category(self.cfg['activity_category_name'],overwrites=permissions(guild,self.client.staff_roles(guild),[]),reason='Закрытые комнаты составов')
            room=db.claim_room('event',event)
            try:
                channel=await guild.create_text_channel(f'сбор-{event}',category=category,topic=room['marker'],overwrites=acl,reason='Состав зафиксирован')
                db.bind_room('event',event,channel.id)
            except Exception:
                db.room_state('event',event,'проверить');raise
            await channel.send('Закрытая комната основного состава и организатора. После завершения или отмены сбора канал удалится через минуту вместе с перепиской.')
            return
        if not room['channel']:
            # An ambiguous create must be reconciled by its exact marker, never duplicated.
            matches=[c for c in await guild.fetch_channels() if isinstance(c,discord.TextChannel) and c.topic==room['marker']]
            if len(matches)==1:db.bind_room('event',event,matches[0].id)
            return
        if room['state']!='открыто':return
        channel=await self.client.fetch_channel(room['channel'])
        if not owned(channel,room,self.cfg['guild_id']):raise RuleError('Метка комнаты изменена; автоматическое управление остановлено.')
        if signature(channel.overwrites)!=signature(acl):await channel.edit(overwrites=acl,reason='Актуальный основной состав')
    async def schedule(self,kind,owner):
        room=self.db.room(kind,owner)
        if not room or room['state']=='удалено':return
        if room['delete_at'] is None:
            self.db.schedule_delete(kind,owner,int(time.time())+self.cfg['delete_delay_seconds'])
            channel=await self.client.fetch_channel(room['channel'])
            if owned(channel,room,self.cfg['guild_id']):
                await channel.send('Канал закрыт и будет удалён через минуту вместе с перепиской. Автоматическая копия сообщений не сохраняется.')
    async def cleanup(self):
        deleted=[]
        for room in self.db.rooms():
            if room['state']!='закрывается' or room['delete_at'] is None or room['delete_at']>time.time():continue
            try:
                channel=await self.client.fetch_channel(room['channel'])
                if not owned(channel,room,self.cfg['guild_id']):
                    self.db.room_state(room['kind'],room['owner'],'метка изменена');continue
                await channel.delete(reason='Закрытый временный канал; запланированное удаление')
            except discord.NotFound:pass
            except discord.HTTPException:continue
            self.db.room_state(room['kind'],room['owner'],'удалено')
            deleted.append(room)
        return deleted
