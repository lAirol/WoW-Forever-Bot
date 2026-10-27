"""Single-server Discord operations bot. Token is runtime-only."""
from __future__ import annotations
import asyncio
from datetime import datetime, timezone
import getpass
import json
import logging
import os
from pathlib import Path
import sys

import discord
from discord import app_commands
from discord.ext import tasks
from store import Store, RuleError, ROLES, CLASSES, ARMORS, utc
from rooms import Rooms
import directory as directory_report
import requirements_ui

HERE=Path(__file__).resolve().parent
CFG=json.loads((HERE/'config.json').read_text(encoding='utf-8'))
DB=Store(os.environ.get('FOREVER_DATABASE',str(HERE/'data.sqlite3')))
STATUS={'настройка':'Настройка требований — запись закрыта','набор':'Набор открыт','сбор':'Состав зафиксирован','идёт':'Идёт','завершён':'Завершён','отменён':'Отменён'}

def role_check(member,names):
    return isinstance(member,discord.Member) and (member.id==member.guild.owner_id or any(r.name.casefold() in {n.casefold() for n in names} for r in member.roles))

async def reply(i,text):
    if i.response.is_done(): await i.followup.send(text,ephemeral=True)
    else: await i.response.send_message(text,ephemeral=True)

def check(i,names=None):
    if i.guild_id!=CFG['guild_id']: raise RuleError('Команда доступна только на сервере команды.')
    if names and not role_check(i.user,names): raise RuleError('Недостаточно прав. Обратись к оператору.')

def clean(text):
    return discord.utils.escape_mentions(discord.utils.escape_markdown(text))

def card(event):
    e=DB.event(event)
    em=discord.Embed(title=e['title'],description=f"**{STATUS[e['state']]}**\nНачало: <t:{e['start']}:F> (<t:{e['start']}:R>)\nОкончание: <t:{e['end']}:t>",color=0x69CCF0)
    roster=DB.roster(event)
    em.add_field(name='Условия записи',value=f"Максимум: {e['max_people']} · Основа: {sum(r['slot']=='основа' for r in roster)}\nКлассы: {e['classes'] or 'любые'}\nБроня: {e['armor'] or 'любая'} (по карточке персонажа)",inline=False)
    room=DB.room('event',event)
    if room and room['channel'] and room['state']!='удалено':
        em.add_field(name='Комната состава',value=f"<#{room['channel']}>"+(' · удаляется после закрытия' if room['delete_at'] else ''),inline=False)
    elif room and room['state']=='удалено':
        em.add_field(name='Комната состава',value='Удалена после закрытия',inline=False)
    for role,col in zip(ROLES,('tank','heal','damage')):
        rows=[r for r in roster if r['role']==role and r['slot']=='основа']
        lines=[f"{'✅' if r['ready'] else '⏳'} <@{r['user']}> · {clean(r['snapshot'])}" for r in rows]
        # Discord field values have a 1024-character limit; full roster is available as a file.
        body='\n'.join(lines) or 'Пока никто не записался'
        if len(body)>1000: body=body[:900]+'\n… Полный список — кнопка «Состав».'
        em.add_field(name=f'{role} — {len(rows)}/{e[col]}',value=body,inline=False)
    reserve=[r for r in roster if r['slot']=='резерв']
    text='\n'.join(f"<@{r['user']}> · {r['role']} · {clean(r['snapshot'])}" for r in reserve)
    em.add_field(name=f'Резерв — {len(reserve)}',value=(text[:900]+'\n…' if len(text)>1000 else text) or 'Резерв свободен',inline=False)
    requirements=requirements_ui.summary(DB,event)
    if requirements:em.add_field(name='Требования к отдельным местам',value=requirements,inline=False)
    em.set_footer(text=f'Сбор №{event} · Роли и персонажи указаны участниками. Состав подтверждает лидер.')
    order=DB.order_for_event(event)
    if order:em.add_field(name='Задача команды',value=f'Связана с внутренним заказом №{order}',inline=False)
    return em

class SafeView(discord.ui.View):
    async def on_error(self,i,error,item):
        print('FOREVER_DIAGNOSTIC:view_error:'+type(error).__name__+':'+str(getattr(error,'status',0))+':'+str(getattr(error,'code',0)),flush=True)
        import traceback
        for frame in traceback.extract_tb(error.__traceback__)[-3:]:
            print('FOREVER_DIAGNOSTIC:frame:'+frame.name+':'+str(frame.lineno),flush=True)
        try:
            await reply(i,str(error) if isinstance(error,RuleError) else 'Не удалось выполнить действие. Оператору нужно проверить состояние.')
        except Exception as response_error:
            print('FOREVER_DIAGNOSTIC:view_response_error:'+type(response_error).__name__+':'+str(getattr(response_error,'status',0))+':'+str(getattr(response_error,'code',0)),flush=True)
        if not isinstance(error,RuleError): logging.error('View failure: %s',type(error).__name__)

class DirectorySelect(discord.ui.Select):
    def __init__(self,key,title,choices,current,row):
        self.key=key
        options=[discord.SelectOption(label=label,value=value,default=value==current) for value,label in choices]
        super().__init__(placeholder=title,options=options,row=row)
    async def callback(self,i):await self.view.change(i,self.key,self.values[0])

class DirectoryButton(discord.ui.Button):
    def __init__(self,key,label,disabled=False):
        self.key=key
        super().__init__(label=label,row=4,disabled=disabled,style=discord.ButtonStyle.secondary)
    async def callback(self,i):await self.view.change(i,self.key)

class DirectoryView(SafeView):
    def __init__(self,user,filters=None,mode='персонажи'):
        super().__init__(timeout=900)
        self.user=user;self.filters=dict(filters or {});self.mode=mode;self.page=0;self.lock=asyncio.Lock()
        directory_report.validate(self.filters)
    def render(self):
        rows=directory_report.select(DB.all_characters(),self.filters)
        entries=rows if self.mode=='персонажи' else directory_report.groups(rows,self.mode)
        pages=max(1,(len(entries)+7)//8);self.page=max(0,min(self.page,pages-1))
        em=discord.Embed(title='Карточки команды · '+directory_report.VIEWS[self.mode],
            description=clean(directory_report.filter_text(self.filters))[:900]+'\n'+f"**Людей: {len({r['user'] for r in rows})} · Персонажей: {len(rows)}**",color=0x69CCF0)
        for item in entries[self.page*8:(self.page+1)*8]:
            if self.mode=='персонажи':
                r=item
                value=f"<@{r['user']}>\n{clean(r['details'])}\nРоли: {clean(r['roles'])} · Броня: {r['armor'] or 'не указана'}"
                em.add_field(name=f"№{r['id']} · {clean(r['name'])} · {clean(r['realm'])}"[:150],value=value if len(value)<=420 else value[:390]+'\nПолные данные — в TXT.',inline=False)
            else:
                key,items=item
                if self.mode=='люди':
                    body='\n'.join(f"№{r['id']} · {clean(r['name'])} · {clean(r['realm'])} · {r['class_name'] or 'класс не указан'} · {r['roles']}" for r in items[:5])
                    if len(items)>5:body+='\nОстальные персонажи — в TXT.'
                    em.add_field(name=f'Участник · {len(items)} персонажей',value=f'<@{key}>\n'+body[:400]+('\nПолный список — в TXT.' if len(body)>400 else ''),inline=False)
                else:em.add_field(name=clean(str(key))[:200],value=f"Персонажей: {len(items)} · Людей: {len({r['user'] for r in items})}",inline=False)
        if not entries:em.add_field(name='Совпадений нет',value='Измени фильтры или нажми «Сбросить».',inline=False)
        em.set_footer(text=f'Страница {self.page+1}/{pages} · Самозаявленные данные · Альты не увеличивают число людей · Меню: 15 минут')
        self.clear_items()
        for row,key,title,choices in [(0,'role','Игровая роль',ROLES),(1,'class_name','Класс',CLASSES),(2,'armor','Броня',ARMORS)]:
            self.add_item(DirectorySelect(key,title,[('__any__',title+': любые')]+[(v,v) for v in choices],self.filters.get(key,'__any__'),row))
        self.add_item(DirectorySelect('mode','Вид списка',list(directory_report.VIEWS.items()),self.mode,3))
        for key,label,disabled in [('prev','Назад',self.page==0),('next','Далее',self.page==pages-1),('reset','Сбросить',False),('export','Весь список · TXT',False)]:
            self.add_item(DirectoryButton(key,label,disabled))
        return em
    async def change(self,i,key,value=None):
        await i.response.defer(ephemeral=True,thinking=(key=='export'))
        check(i,CFG['leader_role_names'])
        if i.user.id!=self.user:raise RuleError('Открой собственный список командой /карточки.')
        async with self.lock:
            if key=='export':
                import io
                rows=directory_report.select(DB.all_characters(),self.filters)
                text=directory_report.export_text(rows,self.filters,self.mode)
                if len(text.encode('utf-8'))>7_000_000:raise RuleError('Слишком большой список. Уточни фильтры.')
                await i.followup.send(file=discord.File(io.BytesIO(text.encode('utf-8')),filename='карточки-команды.txt'),ephemeral=True)
                return
            if key=='mode':
                if value not in directory_report.VIEWS:raise RuleError('Неизвестный вид списка.')
                self.mode=value;self.page=0
            elif key in ('role','class_name','armor'):
                if value=='__any__':self.filters.pop(key,None)
                else:self.filters[key]=value
                self.page=0
            elif key=='reset':self.filters.clear();self.page=0
            elif key=='prev':self.page-=1
            elif key=='next':self.page+=1
            # Ephemeral messages can only be edited through the interaction webhook.
            await i.edit_original_response(embed=self.render(),view=self)

class CharacterSelect(discord.ui.Select):
    def __init__(self,event,role,reserve,characters,user):
        self.event,self.role,self.reserve,self.user=event,role,reserve,user
        super().__init__(placeholder='Выбери своего персонажа',options=[discord.SelectOption(label=f"{c['name']} · {c['realm']}"[:100],value=str(c['id']),description=c['details'][:100] or None) for c in characters])
    async def callback(self,i):
        check(i,CFG['participant_role_names'])
        if i.user.id!=self.user: raise RuleError('Это меню другого участника.')
        i.client.check_board(i,self.event)
        await i.response.defer(ephemeral=True,thinking=True)
        async with i.client.event_lock(self.event):
            slot=DB.join(self.event,i.user.id,int(self.values[0]),self.role,self.reserve)
            await i.client.refresh(self.event)
        await reply(i,f'Ты записан: {slot}, {self.role}. Готовность подтверди отдельной кнопкой.')

class JoinButton(discord.ui.Button):
    def __init__(self,event,role,reserve=False):
        self.event,self.role,self.reserve=event,role,reserve
        super().__init__(label=('Резерв · ' if reserve else '')+role,custom_id=f'forever:{event}:join:{role}:{int(reserve)}',style=discord.ButtonStyle.secondary if reserve else discord.ButtonStyle.primary,row=1 if reserve else 0)
    async def callback(self,i):
        check(i,CFG['participant_role_names'])
        i.client.check_board(i,self.event)
        if DB.event(self.event)['state']!='набор': raise RuleError('Набор закрыт.')
        chars=[c for c in DB.characters(i.user.id) if self.role in c['roles'].split(',')]
        if not chars: raise RuleError('Сначала добавь персонажа командой /персонаж с нужной ролью.')
        view=SafeView(timeout=180)
        view.add_item(CharacterSelect(self.event,self.role,self.reserve,chars,i.user.id))
        await i.response.send_message('Выбери персонажа для этой активности.',view=view,ephemeral=True)

class ActionButton(discord.ui.Button):
    def __init__(self,event,action,label,row=2):
        self.event,self.action=event,action
        super().__init__(label=label,custom_id=f'forever:{event}:{action}',row=row,style=discord.ButtonStyle.secondary)
    async def callback(self,i):
        await i.response.defer(ephemeral=True,thinking=True)
        check(i,CFG['participant_role_names'] if self.action in ('leave','ready') else None)
        i.client.check_board(i,self.event)
        async with i.client.event_lock(self.event):
            if self.action in ('roster','requirements'):
                import io
                text=requirements_ui.full_text(DB,self.event)
                if self.action=='roster':text+='\n\n'+('\n'.join(f"{r['slot']} | {r['role']} | Discord {r['user']} | {r['snapshot']} | готов: {bool(r['ready'])}" for r in DB.roster(self.event)) or 'Список пуст.')
                # Check access against the actual board channel, not just possession of an old button.
                e=DB.event(self.event); channel=i.guild.get_channel(e['channel'])
                if not channel or not channel.permissions_for(i.user).view_channel: raise RuleError('Нет доступа к этому составу.')
                await i.followup.send(file=discord.File(io.BytesIO(text.encode('utf-8')),filename=f"{'состав' if self.action=='roster' else 'требования'}-{self.event}.txt"),ephemeral=True)
                return
            if self.action=='leave':
                promoted=DB.leave(self.event,i.user.id)
                await i.client.refresh(self.event)
                await i.client.notify_promotions(self.event,promoted)
                await reply(i,'Запись снята. Резерв пересчитан.')
            elif self.action=='ready':
                DB.ready(self.event,i.user.id);await i.client.refresh(self.event)
                await reply(i,'Готовность подтверждена.')

class EventView(SafeView):
    def __init__(self,event):
        super().__init__(timeout=None)
        for reserve in (False,True):
            for role in ROLES: self.add_item(JoinButton(event,role,reserve))
        for action,label in [('ready','Готов'),('leave','Отказаться'),('roster','Состав'),('requirements','Требования')]:
            self.add_item(ActionButton(event,action,label))
        state=DB.event(event)['state']
        if state!='набор':
            for item in self.children:
                if isinstance(item,JoinButton) or getattr(item,'action',None)=='leave': item.disabled=True
        if state not in ('набор','сбор'):
            for item in self.children:
                if getattr(item,'action',None)=='ready': item.disabled=True

class TicketView(SafeView):
    def __init__(self): super().__init__(timeout=None)
    @discord.ui.button(label='Обратиться к оператору',style=discord.ButtonStyle.primary,custom_id='forever:ticket:create')
    async def create(self,i,button):
        check(i)
        await i.response.defer(ephemeral=True,thinking=True)
        async with i.client.ticket_lock:
            old,fresh=DB.ticket_begin(i.user.id)
            if not fresh:
                await reply(i,f"У тебя уже есть обращение: <#{old['channel']}>." if old['channel'] else 'Предыдущее создание требует проверки оператора. Повторное обращение не создано.')
                return
            try:
                category=i.client.ticket_category(i.guild)
                overwrites={i.guild.default_role:discord.PermissionOverwrite(view_channel=False),i.user:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True),i.guild.me:discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True,manage_channels=True)}
                for role in i.client.staff_roles(i.guild): overwrites[role]=discord.PermissionOverwrite(view_channel=True,send_messages=True,read_message_history=True)
                room=DB.claim_room('ticket',old['id'])
                channel=await i.guild.create_text_channel(f'обращение-{old["id"]}',category=category,topic=room['marker'],overwrites=overwrites,reason='Личное обращение участника')
                DB.bind_room('ticket',old['id'],channel.id)
                DB.ticket_update(old['id'],'открыто',channel.id)
                await channel.send('**Обращение открыто**\nОпиши вопрос: вступление, участие в группе или проблема. Этот канал доступен тебе, операторам и администраторам сервера. Пароли, коды входа и платёжные реквизиты не отправляй. После `/закрыть-обращение` канал и переписка удаляются через минуту.')
                await reply(i,f'Обращение создано: {channel.mention}')
            except Exception:
                # Timeout may mean Discord created the room. Never blindly retry creation.
                DB.ticket_update(old['id'],'проверить')
                raise

class Client(discord.Client):
    def __init__(self):
        super().__init__(intents=discord.Intents(guilds=True),allowed_mentions=discord.AllowedMentions.none(),enable_debug_events=True)
        self.tree=app_commands.CommandTree(self)
        self.locks={};self.ticket_lock=asyncio.Lock()
        self.rooms=Rooms(self,DB,CFG)
    async def on_interaction(self,interaction):
        # Diagnostic marker only: never log interaction payloads or tokens.
        print('FOREVER_DIAGNOSTIC:interaction:'+str(interaction.type.value),flush=True)
        age=max(0,int((datetime.now(timezone.utc)-interaction.created_at).total_seconds()*1000))
        print('FOREVER_DIAGNOSTIC:interaction_age_ms:'+str(age),flush=True)
        print('FOREVER_DIAGNOSTIC:application_matches:'+str(int(interaction.application_id==self.application_id)),flush=True)
    async def on_socket_event_type(self,event_type):
        if event_type=='INTERACTION_CREATE':
            print('FOREVER_DIAGNOSTIC:gateway_interaction',flush=True)
    async def on_error(self,event_method,*args,**kwargs):
        error=sys.exc_info()[1]
        print('FOREVER_DIAGNOSTIC:event_error:'+type(error).__name__,flush=True)
    def event_lock(self,event): return self.locks.setdefault(event,asyncio.Lock())
    def check_board(self,i,event):
        e=DB.event(event);channel=i.guild.get_channel(e['channel'])
        if not channel or not channel.permissions_for(i.user).view_channel:
            raise RuleError('Нет доступа к каналу этого сбора.')
    def staff_roles(self,guild):
        roles=[]
        for name in CFG['staff_role_names']:
            matches=[r for r in guild.roles if r.name.casefold()==name.casefold()]
            if len(matches)!=1: raise RuleError(f'Нужна ровно одна роль «{name}».')
            roles+=matches
        return roles
    def ticket_category(self,guild):
        matches=[c for c in guild.categories if c.name==CFG['ticket_category_name']]
        if len(matches)!=1: raise RuleError('Оператору нужно создать закрытую категорию обращений.')
        return matches[0]
    async def setup_hook(self):
        def loop_error(loop,context):
            error=context.get('exception')
            print('FOREVER_DIAGNOSTIC:task_error:'+type(error).__name__,flush=True)
            if error is not None:
                import traceback
                frames=traceback.extract_tb(error.__traceback__)
                for frame in frames[-3:]:
                    # Function names and line numbers only, never exception data.
                    print('FOREVER_DIAGNOSTIC:frame:'+frame.name+':'+str(frame.lineno),flush=True)
        asyncio.get_running_loop().set_exception_handler(loop_error)
        if self.application_id!=CFG['application_id']:
            raise RuleError('Токен принадлежит другому приложению. Нужен Forever Operations.')
        self.add_view(TicketView())
        self.ops.restore()
        for e in DB.message_events():
            if e['message']: self.add_view(EventView(e['id']),message_id=e['message'])
        self.tree.copy_global_to(guild=discord.Object(CFG['guild_id']))
        await self.tree.sync(guild=discord.Object(CFG['guild_id']))
        self.reminders.start()
        self.local_operations.start()
        self.room_maintenance.start()
    async def on_ready(self):
        if getattr(self,'reported_ready',False): return
        guild=self.get_guild(CFG['guild_id'])
        if not guild:
            print('FOREVER_NOT_READY:server',flush=True)
            await self.close();return
        if guild.me.guild_permissions.administrator:
            print('FOREVER_NOT_READY:administrator',flush=True)
            await self.close();return
        try:
            self.staff_roles(guild);self.ticket_category(guild)
        except RuleError:
            print('FOREVER_NOT_READY:configuration',flush=True)
            await self.close();return
        for ident in [*CFG['roster_channel_ids'],CFG['panel_channel_id']]:
            channel=guild.get_channel(ident)
            if not channel:
                print(f'FOREVER_NOT_READY:channel:{ident}',flush=True)
                await self.close();return
            perms=channel.permissions_for(guild.me)
            if not all((perms.view_channel,perms.send_messages,perms.embed_links,perms.read_message_history,perms.attach_files)):
                print(f'FOREVER_NOT_READY:permissions:{ident}',flush=True)
                await self.close();return
        self.reported_ready=True
        print('FOREVER_READY',flush=True)
    async def refresh(self,event):
        await self.rooms.event(event)
        e=DB.event(event)
        if not e['message']: return
        channel=self.get_channel(e['channel'])
        if not channel: raise RuleError('Канал карточки недоступен боту.')
        msg=await channel.fetch_message(e['message'])
        await msg.edit(embed=card(event),view=EventView(event))
        order=DB.order_for_event(event)
        if order:await self.ops.refresh(order)
    @tasks.loop(seconds=10)
    async def room_maintenance(self):
        for e in DB.message_events():
            if e['state'] not in ('сбор','идёт') and not DB.room('event',e['id']):continue
            try:
                async with self.event_lock(e['id']):await self.rooms.event(e['id'])
            except (discord.HTTPException,RuleError):pass
        # Recover ticket creation after a timeout or process interruption by its exact marker.
        for room in DB.rooms():
            if room['kind']!='ticket':continue
            try:
                if room['channel']:
                    ticket=DB.ticket(room['owner'])
                    if ticket and ticket['state']=='закрыто':await self.rooms.schedule('ticket',room['owner'])
                    continue
                guild=self.get_guild(CFG['guild_id'])
                matches=[c for c in await guild.fetch_channels() if isinstance(c,discord.TextChannel) and c.topic==room['marker']]
                if len(matches)==1:
                    DB.bind_room('ticket',room['owner'],matches[0].id)
                    DB.ticket_update(room['owner'],'открыто',matches[0].id)
            except discord.HTTPException:pass
        deleted=await self.rooms.cleanup()
        for room in deleted:
            if room['kind']=='event':
                try:
                    async with self.event_lock(room['owner']):await self.refresh(room['owner'])
                except (discord.HTTPException,RuleError):pass
    @room_maintenance.before_loop
    async def before_room_maintenance(self):await self.wait_until_ready()
    async def notify_promotions(self,event,users):
        if users:
            e=DB.event(event);channel=self.get_channel(e['channel'])
            await channel.send('Из резерва в основу: '+', '.join(f'<@{u}>' for u in users)+'. Подтвердите готовность в карточке.',allowed_mentions=discord.AllowedMentions(users=[discord.Object(u) for u in users]))
    @tasks.loop(seconds=60)
    async def reminders(self):
        now=int(datetime.now(timezone.utc).timestamp())
        for e in DB.events():
            async with self.event_lock(e['id']):
                promoted=DB.promote(e['id'])
                if promoted:
                    try:
                        await self.refresh(e['id'])
                        await self.notify_promotions(e['id'],promoted)
                    except (discord.HTTPException,RuleError):
                        logging.error('Promotion notification failed for event %s',e['id'])
            if e['state'] not in ('набор','сбор') or not now<e['start']<=now+900 or e['reminded'] or not e['message']: continue
            # Claim before sending: a network timeout must not cause repeated pings.
            with DB.tx() as c:
                if not c.execute('UPDATE events SET reminded=1 WHERE id=? AND reminded=0',(e['id'],)).rowcount: continue
            try:
                ch=self.get_channel(e['channel'])
                if ch:
                    users=[r['user'] for r in DB.roster(e['id']) if r['slot']=='основа']
                    await ch.send('Скоро начало сбора №'+str(e['id'])+'. Подтвердите готовность: '+', '.join(f'<@{u}>' for u in users),allowed_mentions=discord.AllowedMentions(users=[discord.Object(u) for u in users]))
            except discord.HTTPException: logging.error('Reminder failed for event %s; no automatic retry',e['id'])
    @reminders.before_loop
    async def before_reminders(self): await self.wait_until_ready()
    @tasks.loop(seconds=2)
    async def local_operations(self):
        # Trusted local operator queue. Only two fixed operations; never eval/exec.
        directory=Path(DB.path).resolve().parent/'forever-ops'
        directory.mkdir(exist_ok=True)
        for request in directory.glob('*.request.json'):
            result=request.with_name(request.name.replace('.request.json','.result.json'))
            try:
                data=json.loads(request.read_text(encoding='utf-8'))
                guild=self.get_guild(CFG['guild_id'])
                if data.get('guild_id')!=CFG['guild_id']: raise RuleError('Неверный сервер.')
                if data.get('action')=='smoke-roster':
                    output=await self.smoke_roster(guild)
                elif data.get('action')=='smoke-rooms':
                    output=await self.smoke_rooms(guild)
                elif data.get('action')=='verify-rooms':
                    rows=[r for r in DB.rooms() if r['state']=='закрывается']
                    output={'pending':len(rows)}
                    with DB.tx() as c:
                        output['deleted']=[dict(r) for r in c.execute("SELECT kind,owner,channel,state FROM rooms WHERE state='удалено'")]
                    for row in output['deleted']:
                        try:await self.fetch_channel(row['channel'])
                        except discord.NotFound:row['api_absent']=True
                        else:row['api_absent']=False
                        if row['kind']=='event' and row['api_absent']:
                            async with self.event_lock(row['owner']):await self.refresh(row['owner'])
                elif data.get('action')=='update-guide':
                    channel=guild.get_channel(CFG['roster_channel_ids'][0])
                    msg=await channel.fetch_message(1563351767364927621)
                    await msg.edit(content='**Сборы и требования к местам**\n1. Добавь основного персонажа и альтов через `/персонаж`: класс, роли, броня. Данные указывает участник. Списки и поиск — `/карточки`.\n2. Лидер создаёт `/сбор`: танки, хилы, дд и время с часовым поясом. Для предварительной настройки укажи `настройка:Да` — запись останется закрытой.\n3. Отдельные места: `/требование-сбора номер:N роль:хил мест:1 классы:Шаман`; `/требование-сбора номер:N роль:дд мест:2 броня:кольчуга`. N — номер твоего сбора. Остальные места сохраняют общие фильтры.\n4. `/повторы-класса` задаёт общий максимум каждого класса в основе; 0 отключает лимит. `/убрать-требование` снимает правило по его номеру. Изменения правил снимают готовность; несовместимые с основой изменения отклоняются.\n5. Открой набор: `/состояние номер:N статус:набор`. Кнопки роли → свой персонаж. Если подходящие места заняты, запись идёт в резерв. Несоответствующий ни одному месту персонаж не записывается.\n6. «Готов» подтверждает основу. «Отказаться» доступно в наборе. «Требования» и «Состав» выдают полные личные TXT. Статусы: набор → сбор → идёт → завершён; старт требует полного готового состава по всем местам.\n7. При фиксации создаётся закрытый текстовый канал основы, организатора и операторов. После завершения/отмены он удаляется через 60 секунд. `/закрыть-обращение` также удаляет канал через минуту.\nБот работает, пока включены компьютер и процесс. Внутреннее завершение не подтверждает оплату площадки. Карточки «ТЕСТ БОТА» — технические примеры.')
                    output={'guide_url':msg.jump_url}
                elif data.get('action')=='refresh-seat-rules':
                    for e in DB.message_events():
                        async with self.event_lock(e['id']):await self.refresh(e['id'])
                    output={'updated':len(DB.message_events())}
                elif data.get('action')=='smoke-seat-rules':
                    output=await self.smoke_seat_rules(guild)
                elif data.get('action')=='bootstrap-operations':
                    board=await self.ops.board(guild);panel=await self.ops.panel(guild)
                    output={'order_board':board.id,'panel_url':panel.jump_url}
                elif data.get('action')=='remove-admission':
                    panel=await self.ops.panel(guild)
                    for e in DB.message_events():
                        async with self.event_lock(e['id']):await self.refresh(e['id'])
                    commands=await self.tree.fetch_commands(guild=discord.Object(guild.id))
                    output={'panel_url':panel.jump_url,'review_command_absent':all(c.name!='проверка-персонажа' for c in commands),
                        'options_absent':all(p.name not in ('допуск','проверенные') for c in commands for p in c.options),
                        'review_button_absent':all(c.custom_id!='forever:panel:review' for c in self.ops.env.operations.PanelView(self.ops).children)}
                elif data.get('action')=='smoke-orders':
                    import time
                    ident=DB.create_order('ТЕСТ БОТА — внутренний цикл заказа','вручную','',int(time.time())+172800,'Техническая проверка без покупателя, оплаты и реального рейда.',guild.owner_id)
                    msg=await self.ops.publish(ident,guild)
                    event=DB.create_event('ТЕСТ БОТА — сбор для тестового заказа',int(time.time())+86400,30,(0,0,1),organizer=guild.owner_id,order=ident)
                    ch=guild.get_channel(CFG['roster_channel_ids'][1]);emsg=await ch.send(embed=card(event),view=EventView(event));DB.bind(event,ch.id,emsg.id)
                    char=next(c for c in DB.characters(guild.owner_id) if c['name']=='Тест-Бот' and c['realm']=='Техническая проверка')
                    DB.join(event,guild.owner_id,char['id'],'дд');DB.ready(event,guild.owner_id)
                    DB.transition(event,'сбор');await self.refresh(event)
                    DB.transition(event,'идёт');await self.refresh(event)
                    DB.transition(event,'завершён');await self.refresh(event)
                    DB.order_action(ident,guild.owner_id,'report','Техническая проверка завершена; реальной услуги и платежа нет.')
                    DB.order_action(ident,guild.owner_id,'close');await self.ops.refresh(ident)
                    fetched=await msg.channel.fetch_message(msg.id)
                    if not any(f.name=='Статус' and f.value=='закрыт' for f in fetched.embeds[0].fields):raise RuleError('Статус заказа не обновился в Discord.')
                    output={'order_id':ident,'event_id':event,'url':msg.jump_url,'closed':True,'room_channel':DB.room('event',event)['channel']}
                elif data.get('action')=='smoke-schedule':
                    voice=next((c for c in guild.voice_channels if c.name=='Рейд · 1' and c.category and c.category.name=='07 · ГИЛЬДИЯ'),None)
                    if not voice or not voice.permissions_for(guild.me).view_channel:
                        raise RuleError('Нет доступа к гильдейскому голосовому каналу.')
                    start=int(datetime.now(timezone.utc).timestamp())+86400
                    ev=await guild.create_scheduled_event(name='ТЕСТ БОТА — проверка расписания',start_time=datetime.fromtimestamp(start,timezone.utc),end_time=datetime.fromtimestamp(start+1800,timezone.utc),channel=voice,entity_type=discord.EntityType.voice,privacy_level=discord.PrivacyLevel.guild_only,reason='Техническая проверка создания расписания')
                    await ev.edit(status=discord.EventStatus.cancelled,reason='Техническая проверка завершена; реального рейда нет')
                    output={'event_id':ev.id,'cancelled':True,'voice_id':voice.id}
                elif data.get('action')=='publish-guide':
                    channel=guild.get_channel(CFG['roster_channel_ids'][0])
                    msg=await channel.send('**Автоматическая запись запущена**\n1. Добавь основного персонажа и альтов: `/персонаж` (роли через запятую: танк, хил, дд). Свой список: `/персонажи`.\n2. Лидер создаёт карточку командой `/сбор`; время вводится со смещением, например `2026-11-01T20:00+03:00`.\n3. В карточке нажми свою роль и выбери персонажа. Полные места и пересечение времени переводят в резерв. Можно записаться в резерв сразу.\n4. Кнопка «Готов» — подтверждение для основы. «Отказаться» освобождает место, пока набор открыт.\n5. Лидер меняет статус через `/состояние`: набор → сбор → идёт → завершён. Перед запуском нужен полный готовый состав.\n6. Реальные мероприятия Discord создаёт лидер через `/мероприятие`. Это отдельная запись расписания.\n\nЗакрытое обращение: кнопка в #вопросы-команде. При отключённом/спящем компьютере с процессом бот недоступен. Техническая карточка «ТЕСТ БОТА» не является настоящим рейдом.')
                    output={'guide_url':msg.jump_url}
                else: raise RuleError('Неизвестная локальная операция.')
                result.write_text(json.dumps({'ok':True,**output},ensure_ascii=False),encoding='utf-8')
            except Exception as error:
                result.write_text(json.dumps({'ok':False,'error_type':type(error).__name__}),encoding='utf-8')
            finally:
                request.rename(request.with_suffix('.processed'))
    @local_operations.before_loop
    async def before_local_operations(self): await self.wait_until_ready()
    async def smoke_roster(self,guild):
        channel=guild.get_channel(CFG['roster_channel_ids'][0])
        chars=DB.characters(guild.owner_id)
        character=next((c for c in chars if c['name']=='Тест-Бот' and c['realm']=='Техническая проверка'),None)
        if not character: raise RuleError('Сначала создать явно тестового персонажа через /персонаж.')
        start=int(datetime.now(timezone.utc).timestamp())+86400
        ident=DB.create_event('ТЕСТ БОТА — техническая проверка, не рейд',start,30,(0,0,1))
        steps=[]
        try:
            async with self.event_lock(ident):
                msg=await channel.send(embed=card(ident),view=EventView(ident))
                DB.bind(ident,channel.id,msg.id)
                if DB.join(ident,guild.owner_id,character['id'],'дд',True)!='резерв': raise RuleError('Резерв не сработал.')
                await self.refresh(ident);steps.append('запись в резерв')
                fetched=await channel.fetch_message(msg.id)
                if not any(f.name=='Резерв — 1' for f in fetched.embeds[0].fields): raise RuleError('Карточка резерва не обновилась.')
                DB.leave(ident,guild.owner_id)
                if DB.join(ident,guild.owner_id,character['id'],'дд')!='основа': raise RuleError('Основа не сработала.')
                await self.refresh(ident);steps.append('отказ и запись в основу')
                DB.transition(ident,'сбор')
                try: DB.transition(ident,'идёт')
                except RuleError: steps.append('запуск без готовности отклонён')
                else: raise RuleError('Запуск без готовности был разрешён.')
                DB.ready(ident,guild.owner_id)
                DB.transition(ident,'идёт');DB.transition(ident,'завершён')
                await self.refresh(ident);steps.append('готовность и завершение')
                fetched=await channel.fetch_message(msg.id)
                if 'Завершён' not in fetched.embeds[0].description: raise RuleError('Итоговый статус не опубликован.')
                return {'event_id':ident,'url':msg.jump_url,'steps':steps,'completed':True}
        except Exception:
            e=DB.event(ident)
            if e['state'] in ('набор','сбор','идёт'):
                DB.transition(ident,'отменён')
                try: await self.refresh(ident)
                except Exception: pass
            raise

    async def smoke_rooms(self,guild):
        import time
        from rooms import owned,signature,permissions
        channel=guild.get_channel(CFG['roster_channel_ids'][0])
        char=next(c for c in DB.characters(guild.owner_id) if c['name']=='Тест-Бот' and c['realm']=='Техническая проверка')
        DB.character(guild.owner_id,char['name'],char['realm'],char['details'],['дд'],'Маг','ткань')
        ident=DB.create_event('ТЕСТ БОТА — лимит, фильтры и удаление комнаты',int(time.time())+172800,30,(0,0,1),1,'Маг','ткань',guild.owner_id)
        msg=await channel.send(embed=card(ident),view=EventView(ident));DB.bind(ident,channel.id,msg.id)
        DB.join(ident,guild.owner_id,char['id'],'дд');DB.transition(ident,'сбор')
        await self.refresh(ident)
        room=DB.room('event',ident);roomchannel=await self.fetch_channel(room['channel'])
        if not owned(roomchannel,room,guild.id):raise RuleError('Нет подтверждения владения комнатой.')
        expected=permissions(guild,self.staff_roles(guild),[guild.owner_id])
        if signature(roomchannel.overwrites)!=signature(expected):raise RuleError('Доступ комнаты отличается от ожидаемого.')
        DB.ready(ident,guild.owner_id);DB.transition(ident,'идёт');await self.refresh(ident)
        DB.transition(ident,'завершён');await self.refresh(ident)
        ticket,fresh=DB.ticket_begin(self.user.id)
        if not fresh:raise RuleError('Осталось незакрытое тестовое обращение бота.')
        tr=DB.claim_room('ticket',ticket['id'])
        tch=await guild.create_text_channel(f"обращение-{ticket['id']}",topic=tr['marker'],category=self.ticket_category(guild),overwrites=permissions(guild,self.staff_roles(guild),[guild.owner_id]),reason='Технический тест удаления; реального обращения нет')
        DB.bind_room('ticket',ticket['id'],tch.id);DB.ticket_update(ticket['id'],'открыто',tch.id)
        await tch.send('Техническая проверка автоматического удаления. Реального обращения нет.')
        DB.ticket_update(ticket['id'],'закрыто');await self.rooms.schedule('ticket',ticket['id'])
        return {'event_id':ident,'event_room':roomchannel.id,'ticket_id':ticket['id'],'ticket_room':tch.id,'acl_verified':True,'url':msg.jump_url,'delete_delay_seconds':CFG['delete_delay_seconds']}

    async def smoke_seat_rules(self,guild):
        import time
        channel=guild.get_channel(CFG['roster_channel_ids'][0])
        character=next(c for c in DB.characters(guild.owner_id) if c['name']=='Тест-Бот' and c['realm']=='Техническая проверка')
        example=DB.create_event('ТЕСТ БОТА — шаман-хил и два дд в кольчуге',int(time.time())+172800,30,(0,1,2),organizer=guild.owner_id,setup=True)
        single=None;steps=[]
        try:
            async with self.event_lock(example):
                healer,_=DB.add_requirement(example,guild.owner_id,'хил',1,'Шаман')
                damage,_=DB.add_requirement(example,guild.owner_id,'дд',2,armor='кольчуга')
                DB.set_class_limit(example,guild.owner_id,2)
                example_msg=await channel.send(embed=card(example),view=EventView(example));DB.bind(example,channel.id,example_msg.id)
                try:DB.join(example,guild.owner_id,character['id'],'дд')
                except RuleError:steps.append('setup_blocks_signup')
                else:raise RuleError('Режим настройки разрешил запись.')
                DB.transition(example,'набор')
                for reserve in (False,True):
                    try:DB.join(example,guild.owner_id,character['id'],'дд',reserve)
                    except RuleError:steps.append('wrong_armor_reserve_blocked' if reserve else 'wrong_armor_main_blocked')
                    else:raise RuleError('Персонаж в ткани занял место кольчуги.')
                DB.transition(example,'отменён');await self.refresh(example)
                fetched=await channel.fetch_message(example_msg.id)
                fields=' '.join(f.value for f in fetched.embeds[0].fields)
                if 'Шаман' not in fields or 'кольчуга' not in fields:raise RuleError('Требования не отображаются в Discord.')
            single=DB.create_event('ТЕСТ БОТА — полный цикл с выделенным местом',int(time.time())+259200,30,(0,0,1),organizer=guild.owner_id,setup=True)
            async with self.event_lock(single):
                mage,_=DB.add_requirement(single,guild.owner_id,'дд',1,'Маг','ткань');DB.set_class_limit(single,guild.owner_id,1)
                msg=await channel.send(embed=card(single),view=EventView(single));DB.bind(single,channel.id,msg.id)
                DB.transition(single,'набор');DB.join(single,guild.owner_id,character['id'],'дд');DB.ready(single,guild.owner_id)
                for state in ('сбор','идёт','завершён'):DB.transition(single,state);await self.refresh(single)
                fetched=await channel.fetch_message(msg.id)
                if 'Завершён' not in fetched.embeds[0].description:raise RuleError('Полный цикл не завершился в карточке.')
                steps.append('matching_single_seat_completed')
            commands=await self.tree.fetch_commands(guild=discord.Object(guild.id))
            return {'example_id':example,'example_url':example_msg.jump_url,'example_rules':[healer,damage],
                'completed_id':single,'completed_url':msg.jump_url,'completed_rule':mage,'steps':steps,
                'commands_registered':all(any(c.name==name for c in commands) for name in ('требование-сбора','убрать-требование','повторы-класса'))}
        finally:
            for ident in (example,single):
                if ident and DB.event(ident)['state'] in ('настройка','набор','сбор','идёт'):
                    DB.transition(ident,'отменён')
                    try:await self.refresh(ident)
                    except (discord.HTTPException,RuleError):pass


client=Client()

@client.tree.command(name='персонаж',description='Добавить или обновить своего персонажа; альты — отдельными карточками')
@app_commands.guild_only()
@app_commands.choices(класс=[app_commands.Choice(name=n,value=n) for n in CLASSES],броня=[app_commands.Choice(name=n,value=n) for n in ARMORS])
async def character(i:discord.Interaction,ник:str,мир:str,класс:str,специализация:str,раса:str,уровень:app_commands.Range[int,1,999],роли:str,броня:str|None=None):
    check(i)
    await i.response.defer(ephemeral=True,thinking=True)
    details=f'{класс} / {специализация} / {раса} / ур. {уровень}'
    DB.character(i.user.id,ник.strip(),мир.strip(),details,[s.strip().lower() for s in роли.split(',')],класс,броня)
    ch=next(c for c in DB.characters(i.user.id) if c['name']==ник.strip() and c['realm']==мир.strip())
    for event in DB.character_events(ch['id']):
        try:
            async with client.event_lock(event):await client.refresh(event)
        except (discord.HTTPException,RuleError):pass
    await reply(i,f"Карточка №{ch['id']} сохранена. Свой список: /персонажи.")

@client.tree.command(name='персонажи',description='Мои персонажи и их номера')
@app_commands.guild_only()
async def characters(i:discord.Interaction):
    check(i)
    rows=DB.characters(i.user.id)
    import io
    text='\n'.join(f"№{r['id']} | {r['name']} | {r['realm']} | {r['details']} | {r['roles']} | броня: {r['armor'] or 'не указана'}" for r in rows) or 'Персонажей пока нет. Добавь командой /персонаж.'
    await i.response.send_message(file=discord.File(io.BytesIO(text.encode('utf-8')),filename='мои-персонажи.txt'),ephemeral=True)

@client.tree.command(name='удалить-персонажа',description='Удалить свою карточку, если она не связана с историей сборов')
@app_commands.guild_only()
async def delete_character(i:discord.Interaction,номер:int):
    check(i);DB.remove_character(i.user.id,номер);await reply(i,'Карточка удалена.')

@client.tree.command(name='карточки',description='Лидеру: найти людей и персонажей по ролям, классам, броне и другим фильтрам')
@app_commands.guild_only()
@app_commands.choices(роль=[app_commands.Choice(name=n,value=n) for n in ROLES],класс=[app_commands.Choice(name=n,value=n) for n in CLASSES],броня=[app_commands.Choice(name=n,value=n) for n in ARMORS],вид=[app_commands.Choice(name=label,value=value) for value,label in directory_report.VIEWS.items()])
@app_commands.describe(роль='Игровая роль персонажа, не роль Discord',мир='Точное название мира без учёта регистра',раса='Точное название расы без учёта регистра',специализация='Часть названия специализации',поиск='Часть ника, мира или описания',от='Минимальный уровень включительно',до='Максимальный уровень включительно',участник='Карточки выбранного пользователя Discord')
async def directory_command(i:discord.Interaction,роль:str|None=None,класс:str|None=None,броня:str|None=None,мир:str|None=None,раса:str|None=None,специализация:str|None=None,от:app_commands.Range[int,1,999]|None=None,до:app_commands.Range[int,1,999]|None=None,участник:discord.Member|None=None,поиск:str|None=None,вид:str='персонажи'):
    check(i,CFG['leader_role_names'])
    await i.response.defer(ephemeral=True,thinking=True)
    filters={k:v for k,v in {'role':роль,'class_name':класс,'armor':броня,'realm':мир,'race':раса,'specialization':специализация,'min_level':от,'max_level':до,'user':участник.id if участник else None,'search':поиск}.items() if v is not None and v!=''}
    view=DirectoryView(i.user.id,filters,вид)
    await i.followup.send(embed=view.render(),view=view,ephemeral=True)

@client.tree.command(name='сводка-карточек',description='Лидеру: сколько людей и персонажей зарегистрировано по классам, ролям и броне')
@app_commands.guild_only()
@app_commands.choices(по=[app_commands.Choice(name=label,value=value) for value,label in directory_report.VIEWS.items() if value not in ('люди','персонажи')])
async def directory_summary(i:discord.Interaction,по:str='классы'):
    check(i,CFG['leader_role_names'])
    await i.response.defer(ephemeral=True,thinking=True)
    view=DirectoryView(i.user.id,mode=по)
    await i.followup.send(embed=view.render(),view=view,ephemeral=True)

@client.tree.command(name='сбор',description='Лидеру: создать набор с местами танков, хилов и дд')
@app_commands.guild_only()
@app_commands.describe(максимум='Число игроков: равно сумме мест танков, хилов и дд; без параметра считается автоматически',классы='Допущенные классы через запятую, например Охотник,Шаман',броня='Ограничение по типу брони, указанному в карточке персонажа',настройка='Да: сначала настроить отдельные места без записи, затем открыть через /состояние статус:набор')
@app_commands.choices(броня=[app_commands.Choice(name=n,value=n) for n in ARMORS])
async def event(i:discord.Interaction,название:str,начало:str,минут:app_commands.Range[int,15,720],танков:app_commands.Range[int,0,40],хилов:app_commands.Range[int,0,40],дд:app_commands.Range[int,0,40],максимум:app_commands.Range[int,1,40]|None=None,классы:str|None=None,броня:str|None=None,заказ:int|None=None,настройка:bool=False):
    check(i,CFG['leader_role_names'])
    if заказ is not None:check(i,CFG['staff_role_names'])
    if i.channel_id not in CFG['roster_channel_ids']: raise RuleError('Создавай сбор в #сбор-группы или #набор-на-заказы.')
    await i.response.defer(ephemeral=True,thinking=True)
    event=DB.create_event(название,utc(начало),минут,(танков,хилов,дд),максимум,классы or (),броня or '',i.user.id,заказ,настройка)
    # Do not resend if the HTTP result is uncertain; the DB record remains for recovery.
    try:
        message=await i.channel.send(embed=card(event),view=EventView(event))
        DB.bind(event,i.channel_id,message.id)
        if заказ is not None:await client.ops.refresh(заказ)
    except Exception:
        await reply(i,f'Сбор №{event} сохранён, публикацию нужно проверить. Не повторяй создание вслепую.')
        raise
    await reply(i,f'Набор создан: {message.jump_url}')

@client.tree.command(name='состояние',description='Лидеру: зафиксировать, открыть, начать, завершить или отменить сбор')
@app_commands.guild_only()
@app_commands.choices(статус=[app_commands.Choice(name=n,value=n) for n in ('набор','сбор','идёт','завершён','отменён')])
async def state(i:discord.Interaction,номер:int,статус:app_commands.Choice[str]):
    check(i,CFG['leader_role_names']);await i.response.defer(ephemeral=True,thinking=True)
    async with client.event_lock(номер):
        DB.transition(номер,статус.value);await client.refresh(номер)
    await reply(i,'Статус обновлён. Завершение здесь не подтверждает оплату или выполнение заказа на площадке.')

@client.tree.command(name='обновить-сбор',description='Лидеру: восстановить отображение существующей карточки из базы')
@app_commands.guild_only()
async def refresh(i:discord.Interaction,номер:int):
    check(i,CFG['leader_role_names']);await i.response.defer(ephemeral=True,thinking=True)
    async with client.event_lock(номер): await client.refresh(номер)
    await reply(i,'Карточка обновлена.')

@client.tree.command(name='панель-обращений',description='Оператору: опубликовать кнопку закрытого обращения')
@app_commands.guild_only()
async def panel(i:discord.Interaction):
    print('FOREVER_DIAGNOSTIC:panel_called',flush=True)
    check(i,CFG['staff_role_names'])
    if i.channel_id!=CFG['panel_channel_id']: raise RuleError('Панель публикуется только в #вопросы-команде.')
    client.staff_roles(i.guild);client.ticket_category(i.guild)
    await i.response.defer(ephemeral=True,thinking=True)
    await i.channel.send('**Связаться с оператором**\nВступление, проверка персонажа или проблема с участием. Кнопка создаст отдельный закрытый канал. Доступ имеют автор, операторы и администраторы сервера.',view=TicketView())
    await reply(i,'Панель опубликована.')

@client.tree.command(name='закрыть-обращение',description='Закрыть обращение и удалить канал с перепиской через минуту')
@app_commands.guild_only()
async def close_ticket(i:discord.Interaction):
    check(i)
    ticket=DB.ticket_channel(i.channel_id)
    if not ticket or ticket['state']!='открыто': raise RuleError('Это не открытое обращение.')
    if i.user.id!=ticket['user'] and not role_check(i.user,CFG['staff_role_names']): raise RuleError('Закрыть может автор или оператор.')
    await i.response.defer(ephemeral=True,thinking=True)
    room=DB.room('ticket',ticket['id'])
    if not room:
        # Migrate only a DB-tracked old ticket in the expected category with its exact bot name.
        if i.channel.category_id!=client.ticket_category(i.guild).id or i.channel.name!=f"обращение-{ticket['id']}":
            raise RuleError('Старое обращение требует проверки оператора: категория или имя изменены.')
        room=DB.claim_room('ticket',ticket['id'])
        await i.channel.edit(topic=room['marker'],reason='Миграция известного обращения для удаления после закрытия')
        DB.bind_room('ticket',ticket['id'],i.channel_id)
    await i.channel.set_permissions(discord.Object(ticket['user'],type=discord.Member),view_channel=True,send_messages=False,read_message_history=True,reason='Обращение решено')
    DB.ticket_update(ticket['id'],'закрыто')
    await client.rooms.schedule('ticket',ticket['id'])
    await reply(i,'Обращение закрыто. Канал и переписка удалятся через минуту.')

@client.tree.command(name='мероприятие',description='Лидеру: добавить реальное мероприятие Discord в гильдейском голосовом канале')
@app_commands.guild_only()
async def schedule(i:discord.Interaction,название:str,начало:str,минут:app_commands.Range[int,15,720],голос:discord.VoiceChannel):
    check(i,CFG['leader_role_names'])
    if not голос.category or голос.category.name!='07 · ГИЛЬДИЯ': raise RuleError('Выбери голосовой канал гильдии.')
    start=utc(начало)
    if start<=int(datetime.now(timezone.utc).timestamp()): raise RuleError('Начало должно быть в будущем.')
    if not 1<=len(название)<=100: raise RuleError('Название: до 100 символов.')
    await i.response.defer(ephemeral=True,thinking=True)
    ev=await i.guild.create_scheduled_event(name=название,start_time=datetime.fromtimestamp(start,timezone.utc),end_time=datetime.fromtimestamp(start+минут*60,timezone.utc),channel=голос,entity_type=discord.EntityType.voice,privacy_level=discord.PrivacyLevel.guild_only,reason='Расписание гильдии')
    await reply(i,f'Мероприятие создано: {ev.url}. Запись по ролям создаётся отдельно командой /сбор.')

@client.tree.command(name='привязать-сбор',description='Оператору: восстановить связь с карточкой после неоднозначного сетевого сбоя')
@app_commands.guild_only()
async def bind_event(i:discord.Interaction,номер:int,сообщение:str):
    check(i,CFG['staff_role_names'])
    if i.channel_id not in CFG['roster_channel_ids']: raise RuleError('Выбери канал набора.')
    await i.response.defer(ephemeral=True,thinking=True)
    try: message=await i.channel.fetch_message(int(сообщение))
    except (ValueError,discord.NotFound): raise RuleError('Нужен ID существующего сообщения в этом канале.')
    e=DB.event(номер)
    marker=f'Сбор №{номер} · '
    if message.author.id!=client.user.id or not message.embeds or not (message.embeds[0].footer.text or '').startswith(marker):
        raise RuleError('Сообщение не является карточкой этого сбора от этого бота.')
    if e['message'] and e['message']!=message.id: raise RuleError('Сбор уже привязан к другой карточке.')
    async with client.event_lock(номер):
        DB.bind(номер,i.channel_id,message.id)
        await client.refresh(номер)
    await reply(i,'Связь восстановлена. Существующие записи сохранены.')

@client.tree.error
async def command_error(i,error):
    original=getattr(error,'original',error)
    print('FOREVER_DIAGNOSTIC:command_error:'+type(original).__name__+':'+str(getattr(original,'status',0))+':'+str(getattr(original,'code',0)),flush=True)
    try:
        await reply(i,str(original) if isinstance(original,RuleError) else 'Действие не завершено. Оператору нужно проверить права и текущее состояние; не повторяй создание вслепую.')
    except Exception as response_error:
        print('FOREVER_DIAGNOSTIC:response_error:'+type(response_error).__name__+':'+str(getattr(response_error,'status',0))+':'+str(getattr(response_error,'code',0)),flush=True)
    if not isinstance(original,RuleError): logging.error('Command failure: %s',type(original).__name__)

import operations
operations.install(sys.modules[__name__])
requirements_ui.install(sys.modules[__name__])

if __name__=='__main__':
    if '--check' in sys.argv:
        print(f'Конфигурация загружена. Команд: {len(client.tree.get_commands())}. Сеть и токен не использовались.')
    else:
        token=os.environ.get('DISCORD_BOT_TOKEN') or getpass.getpass('Токен бота (скрытый ввод): ')
        if not token: raise SystemExit('Токен не задан.')
        # Library exception logs can include request data. Keep logs minimal and do not log interactions/tokens.
        logging.basicConfig(level=logging.ERROR,format='%(levelname)s: %(message)s')
        try:
            client.run(token,log_handler=None)
        except discord.LoginFailure:
            print('FOREVER_NOT_READY:token',flush=True)
            raise SystemExit(2)
        except Exception as error:
            # Never print exception/request objects around credential-bearing calls.
            print('FOREVER_NOT_READY:'+type(error).__name__,flush=True)
            raise SystemExit(3)
