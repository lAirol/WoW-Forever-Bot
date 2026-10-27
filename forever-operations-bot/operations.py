"""Persistent operations panel and manual order workflow."""
import asyncio
import io
import uuid
import discord
from discord import app_commands
from store import RuleError,utc
from rooms import permissions,signature

class Operations:
    def __init__(self,env):
        self.env=env;self.client=env.client;self.board_lock=asyncio.Lock();self.order_locks={}
        self.register()
    @property
    def db(self):return self.env.DB
    @property
    def cfg(self):return self.env.CFG
    def guard(self,i,staff=True):self.env.check(i,self.cfg['staff_role_names'] if staff else self.cfg['leader_role_names'])
    async def board(self,guild):
        async with self.board_lock:
            marker=self.db.setting('order_board_marker','forever:orders:'+uuid.uuid4().hex)
            ident=self.db.setting('order_board_id')
            acl=permissions(guild,self.client.staff_roles(guild),[])
            if ident:
                channel=await self.client.fetch_channel(int(ident))
                if not isinstance(channel,discord.TextChannel) or channel.guild.id!=guild.id or channel.topic!=marker:raise RuleError('Метка реестра заказов изменена. Нужна проверка оператора.')
                if signature(channel.overwrites)!=signature(acl):await channel.edit(overwrites=acl,reason='Закрытый реестр операторов')
                return channel
            channels=await guild.fetch_channels()
            matches=[c for c in channels if isinstance(c,discord.TextChannel) and c.topic==marker]
            if len(matches)>1:raise RuleError('Найдено несколько реестров с одной меткой.')
            if matches:channel=matches[0]
            else:
                categories=[c for c in channels if isinstance(c,discord.CategoryChannel) and c.name=='04 · ЗАКАЗЫ']
                if len(categories)!=1:raise RuleError('Нужна категория «04 · ЗАКАЗЫ».')
                channel=await guild.create_text_channel('реестр-заказов',category=categories[0],topic=marker,overwrites=acl,reason='Внутренний реестр заказов операторов')
            self.db.setting('order_board_id',channel.id)
            return channel
    def card(self,ident):
        o=self.db.order(ident);clean=self.env.clean
        em=discord.Embed(title=f"Заказ №{ident} · {clean(o['title'])}"[:256],description=clean(o['description']),color=0xD6A33C)
        em.add_field(name='Статус',value=o['state'])
        em.add_field(name='Источник / внешний номер',value=clean(o['source']+' / '+(o['external_ref'] or 'нет')))
        em.add_field(name='Ответственный',value=f"<@{o['operator']}>")
        em.add_field(name='Срок',value=f"<t:{o['due']}:F> · <t:{o['due']}:R>",inline=False)
        if o['event']:
            e=self.db.event(o['event']);link=f"https://discord.com/channels/{self.cfg['guild_id']}/{e['channel']}/{e['message']}" if e['message'] else 'Карточка сбора требует восстановления'
            em.add_field(name=f"Сбор №{e['id']} · {self.env.STATUS[e['state']]}",value=link,inline=False)
        if o['report']:em.add_field(name='Отчёт / причина отмены',value=clean(o['report'])[:950]+'\nПолный текст — кнопка TXT.',inline=False)
        em.set_footer(text=f'Заказ №{ident} · Внутренний учёт. Статус и оплата на площадке подтверждаются отдельно.')
        return em
    async def publish(self,ident,guild):
        o=self.db.order(ident)
        if o['message']:return await self.refresh(ident)
        channel=await self.board(guild)
        msg=await channel.send(embed=self.card(ident),view=OrderView(self,ident))
        self.db.bind_order(ident,channel.id,msg.id)
        return msg
    async def refresh(self,ident):
        o=self.db.order(ident)
        if not o['message']:return
        channel=await self.client.fetch_channel(o['channel']);msg=await channel.fetch_message(o['message'])
        await msg.edit(embed=self.card(ident),view=OrderView(self,ident));return msg
    async def panel(self,guild):
        channel=guild.get_channel(self.cfg['roster_channel_ids'][0])
        text='**Панель команды**\nСоздай сбор, найди игроков или проверь свои записи. Заказы ведутся операторами в закрытом реестре. Отдельные места — `/требование-сбора`, повторы классов — `/повторы-класса`. Полные фильтры состава — `/сбор`, поиск — `/карточки`. Не отправляйте логины, пароли и персональные данные покупателей.'
        ident=self.db.setting('operations_panel_message')
        if ident:
            msg=await channel.fetch_message(int(ident));await msg.edit(content=text,view=PanelView(self))
        else:
            msg=await channel.send(text,view=PanelView(self));self.db.setting('operations_panel_message',msg.id)
        return msg
    def restore(self):
        self.client.add_view(PanelView(self))
        for o in self.db.orders():
            if o['message']:self.client.add_view(OrderView(self,o['id']),message_id=o['message'])
    async def my_signups(self,i):
        self.env.check(i)
        with self.db.tx() as c:
            rows=c.execute('SELECT e.id,e.title,e.state,e.start,e.channel,e.message,s.role,s.slot,s.snapshot FROM signups s JOIN events e ON e.id=s.event WHERE s.user=? ORDER BY e.start DESC LIMIT 100',(i.user.id,)).fetchall()
        text='\n\n'.join(f"Сбор №{r['id']} | {r['title']} | {r['state']} | {r['slot']} · {r['role']}\n{r['snapshot']}\nhttps://discord.com/channels/{self.cfg['guild_id']}/{r['channel']}/{r['message']}" for r in rows) or 'Записей пока нет.'
        await self.env.reply(i,'Последние записи (до 100):')
        await i.followup.send(file=discord.File(io.BytesIO(text.encode('utf-8')),filename='мои-записи.txt'),ephemeral=True)
    async def order_list(self,i):
        self.guard(i)
        rows=self.db.orders();active=[o for o in rows if o['state'] not in ('закрыт','отменён')]
        text='\n'.join(f"№{o['id']} | {o['state']} | {o['title']} | {o['source']} {o['external_ref']} | оператор {o['operator']} | сбор {o['event'] or 'нет'}" for o in rows) or 'Заказов пока нет.'
        await i.followup.send(f'Открытых заказов: {len(active)} · Всего: {len(rows)}. Карточка по номеру: `/заказы номер:…`.',file=discord.File(io.BytesIO(text.encode('utf-8')),filename='реестр-заказов.txt'),ephemeral=True)
    def register(self):
        service=self
        @self.client.tree.command(name='панель-команды',description='Оператору: создать или обновить постоянную панель управления')
        @app_commands.guild_only()
        async def panel(i:discord.Interaction):
            service.guard(i);await i.response.defer(ephemeral=True,thinking=True)
            msg=await service.panel(i.guild);await service.env.reply(i,msg.jump_url)
        @self.client.tree.command(name='заказ',description='Оператору: вручную зарегистрировать заказ и открыть его карточку')
        @app_commands.guild_only()
        async def order(i:discord.Interaction):
            service.guard(i);await i.response.send_modal(OrderModal(service))
        @self.client.tree.command(name='заказы',description='Оператору: реестр заказов или личная карточка по номеру')
        @app_commands.guild_only()
        async def orders(i:discord.Interaction,номер:int|None=None):
            service.guard(i);await i.response.defer(ephemeral=True,thinking=True)
            if номер is None:await service.order_list(i)
            else:await i.followup.send(embed=service.card(номер),view=OrderView(service,номер),ephemeral=True)
        @self.client.tree.command(name='мои-записи',description='Показать свои последние записи в составы, включая альтов и резерв')
        @app_commands.guild_only()
        async def my(i:discord.Interaction):
            service.env.check(i);await i.response.defer(ephemeral=True,thinking=True);await service.my_signups(i)
        @self.client.tree.command(name='обновить-заказ',description='Оператору: восстановить карточку заказа из базы')
        @app_commands.guild_only()
        async def refresh(i:discord.Interaction,номер:int):
            service.guard(i);await i.response.defer(ephemeral=True,thinking=True)
            o=service.db.order(номер)
            if not o['message']:raise RuleError('Публикацию нужно проверить в реестре и привязать существующее сообщение.')
            await service.refresh(номер);await service.env.reply(i,'Карточка обновлена.')
        @self.client.tree.command(name='привязать-заказ',description='Оператору: восстановить связь с карточкой после сетевого сбоя')
        @app_commands.guild_only()
        async def bind(i:discord.Interaction,номер:int,сообщение:str):
            service.guard(i);await i.response.defer(ephemeral=True,thinking=True)
            board=await service.board(i.guild)
            try:msg=await board.fetch_message(int(сообщение))
            except (ValueError,discord.NotFound):raise RuleError('Нужен ID сообщения в реестре заказов.')
            if msg.author.id!=service.client.user.id or not msg.embeds or not (msg.embeds[0].footer.text or '').startswith(f'Заказ №{номер} · '):raise RuleError('Это не карточка указанного заказа от бота.')
            service.db.bind_order(номер,board.id,msg.id);await service.refresh(номер);await service.env.reply(i,'Связь восстановлена.')

class View(discord.ui.View):
    async def on_error(self,i,error,item):
        print('FOREVER_DIAGNOSTIC:operations_error:'+type(error).__name__,flush=True)
        await self.service.env.reply(i,str(error) if isinstance(error,RuleError) else 'Действие могло сохраниться. Проверь карточку; не создавай дубль вслепую.')

class Action(discord.ui.Button):
    def __init__(self,key,label,custom_id,row=0,disabled=False):
        super().__init__(label=label,custom_id=custom_id,row=row,disabled=disabled,style=discord.ButtonStyle.secondary);self.key=key
    async def callback(self,i):await self.view.action(i,self.key)

class PanelView(View):
    def __init__(self,service):
        super().__init__(timeout=None);self.service=service
        for key,label in [('event','Создать сбор'),('directory','Найти игроков'),('mine','Мои записи'),('order','Новый заказ'),('orders','Реестр заказов')]:
            self.add_item(Action(key,label,'forever:panel:'+key))
    async def action(self,i,key):
        s=self.service
        if key in ('event','directory'):s.guard(i,False)
        elif key in ('order','orders'):s.guard(i)
        else:s.env.check(i)
        if key=='event':await i.response.send_modal(EventModal(s))
        elif key=='order':await i.response.send_modal(OrderModal(s))
        else:
            await i.response.defer(ephemeral=True,thinking=True)
            if key=='mine':await s.my_signups(i)
            elif key=='orders':await s.order_list(i)
            else:
                view=s.env.DirectoryView(i.user.id);await i.followup.send(embed=view.render(),view=view,ephemeral=True)

class OrderView(View):
    def __init__(self,service,ident):
        super().__init__(timeout=None);self.service=service;self.ident=ident
        o=service.db.order(ident);closed=o['state'] in ('закрыт','отменён')
        for key,label,disabled in [('event','Создать сбор',o['event'] is not None or closed),('link','Привязать сбор',o['event'] is not None or closed),('report','Отчёт',o['state']!='ожидает отчёта'),('close','Закрыть заказ',o['state']!='ожидает отчёта' or not o['report']),('cancel','Отменить',closed)]:
            self.add_item(Action(key,label,f'forever:order:{ident}:{key}',disabled=disabled))
        self.add_item(Action('export','Карточка и отчёт · TXT',f'forever:order:{ident}:export',row=1))
    async def action(self,i,key):
        s=self.service;s.guard(i)
        if key=='event':await i.response.send_modal(EventModal(s,self.ident))
        elif key in ('report','cancel','link'):await i.response.send_modal(OrderActionModal(s,self.ident,key))
        else:
            await i.response.defer(ephemeral=True,thinking=True)
            if key=='export':
                o=s.db.order(self.ident);text='\n'.join(f'{k}: {v}' for k,v in o.items())
                await i.followup.send(file=discord.File(io.BytesIO(text.encode('utf-8')),filename=f'заказ-{self.ident}.txt'),ephemeral=True)
            else:
                s.db.order_action(self.ident,i.user.id,'close');await s.refresh(self.ident)
                await s.env.reply(i,'Заказ закрыт во внутреннем учёте. Подтверждение площадки выполняется отдельно.')

class Modal(discord.ui.Modal):
    async def on_error(self,i,error):
        print('FOREVER_DIAGNOSTIC:operations_modal_error:'+type(error).__name__,flush=True)
        await self.service.env.reply(i,str(error) if isinstance(error,RuleError) else 'Состояние нужно проверить; не создавай повторную карточку вслепую.')

class OrderModal(Modal):
    def __init__(self,service):
        super().__init__(title='Новый заказ · ручной ввод');self.service=service
        for key,label,limit,required,placeholder in [('title','Услуга / название',120,True,'Без данных покупателя'),('source','Источник',20,True,'вручную, FunPay или G2G'),('ref','Номер заказа на площадке',80,False,'Обязателен для FunPay/G2G'),('due','Выполнить до · дата и часовой пояс',40,True,'2026-11-01T20:00+03:00'),('description','Задача · без логинов и паролей',800,True,'Цель, объём, условия выполнения')]:
            item=discord.ui.TextInput(label=label,max_length=limit,required=required,placeholder=placeholder,style=discord.TextStyle.paragraph if key=='description' else discord.TextStyle.short)
            setattr(self,key+'_input',item);self.add_item(item)
    async def on_submit(self,i):
        s=self.service;s.guard(i);await i.response.defer(ephemeral=True,thinking=True)
        ident=s.db.create_order(str(self.title_input),str(self.source_input),str(self.ref_input),utc(str(self.due_input)),str(self.description_input),i.user.id)
        try:msg=await s.publish(ident,i.guild)
        except Exception:
            await s.env.reply(i,f'Заказ №{ident} сохранён. Проверь публикацию в реестре; дубль не создавай.');raise
        await s.env.reply(i,f'Заказ №{ident}: {msg.jump_url}')

class OrderActionModal(Modal):
    def __init__(self,service,ident,key):
        super().__init__(title={'report':'Отчёт о выполнении','cancel':'Причина отмены','link':'Связать существующий сбор'}[key]);self.service=service;self.ident=ident;self.key=key
        self.text=discord.ui.TextInput(label='Номер сбора' if key=='link' else 'Результат / причина · без секретов',max_length=15 if key=='link' else 1500,style=discord.TextStyle.short if key=='link' else discord.TextStyle.paragraph)
        self.add_item(self.text)
    async def on_submit(self,i):
        s=self.service;s.guard(i);await i.response.defer(ephemeral=True,thinking=True)
        if self.key=='link':
            try:event=int(str(self.text))
            except ValueError:raise RuleError('Нужен числовой номер сбора.')
            s.db.link_order(self.ident,event,i.user.id);await s.client.refresh(event)
        else:s.db.order_action(self.ident,i.user.id,self.key,str(self.text))
        await s.refresh(self.ident);await s.env.reply(i,'Карточка заказа обновлена.')

class EventModal(Modal):
    def __init__(self,service,order=None):
        super().__init__(title='Создать сбор');self.service=service;self.order=order
        self.name_input=discord.ui.TextInput(label='Название · будет видно участникам',max_length=120,default=service.db.order(order)['title'] if order else None)
        self.start_input=discord.ui.TextInput(label='Начало · дата и часовой пояс',placeholder='2026-11-01T20:00+03:00',max_length=40)
        self.minutes_input=discord.ui.TextInput(label='Длительность в минутах',default='60',max_length=3)
        self.roles_input=discord.ui.TextInput(label='Места: танки, хилы, дд',default='1,1,3',max_length=15)
        self.armor_input=discord.ui.TextInput(label='Броня · необязательный фильтр',placeholder='ткань, кожа, кольчуга или латы',required=False,max_length=15)
        for item in (self.name_input,self.start_input,self.minutes_input,self.roles_input,self.armor_input):self.add_item(item)
    async def on_submit(self,i):
        s=self.service;s.guard(i,self.order is not None);await i.response.defer(ephemeral=True,thinking=True)
        try:minutes=int(str(self.minutes_input));caps=tuple(int(v.strip()) for v in str(self.roles_input).split(','))
        except ValueError:raise RuleError('Длительность — число; места — три числа через запятую: 1,1,3.')
        event=s.db.create_event(str(self.name_input),utc(str(self.start_input)),minutes,caps,armor=str(self.armor_input),organizer=i.user.id,order=self.order)
        channel=i.guild.get_channel(s.cfg['roster_channel_ids'][1 if self.order else 0])
        try:
            msg=await channel.send(embed=s.env.card(event),view=s.env.EventView(event));s.db.bind(event,channel.id,msg.id)
        except Exception:
            await s.env.reply(i,f'Сбор №{event} сохранён; публикацию нужно проверить. Повторно не создавай.');raise
        if self.order:await s.refresh(self.order)
        await s.env.reply(i,f'Сбор №{event}: {msg.jump_url}')

def install(env):
    service=Operations(env);env.client.ops=service;return service
