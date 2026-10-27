"""Leader commands for exact role seats. Existing signups are never silently expelled."""
import discord
from discord import app_commands
from store import ROLES,ARMORS
import slots

def summary(db,event):
    e=db.event(event);rules=db.requirements(event)
    if not rules and not e['class_limit']:return ''
    seats,allocation=db.seat_allocation(event);occupied=set((allocation or {}).values());lines=[]
    if e['class_limit']:lines.append(f"Одного класса — максимум {e['class_limit']} во всей основе")
    for rule in rules:
        indices=[i for i,s in enumerate(seats) if s['rule_id']==rule['id']]
        label=slots.label(seats[indices[0]])
        lines.append(f"№{rule['id']} · {label} — {sum(i in occupied for i in indices)}/{rule['places']}")
    if any(s['rule_id'] is None for s in seats):lines.append('Остальные места — по общим фильтрам сбора.')
    if allocation is None:lines.append('⚠ Состав не соответствует требованиям: лидер должен проверить записи.')
    text='\n'.join(lines)
    return text if len(text)<=900 else text[:850]+'\nПолный список — кнопка «Требования».'

def full_text(db,event):
    e=db.event(event);seats,allocation=db.seat_allocation(event);occupied={seat:user for user,seat in (allocation or {}).items()}
    lines=[f"Сбор №{event} | {e['title']}",f"Максимум одного класса: {e['class_limit'] or 'без ограничения'}",
        f"Общие классы: {e['classes'] or 'любые'} | общая броня: {e['armor'] or 'любая'}",'']
    for n,seat in enumerate(seats):
        rule=f" | правило №{seat['rule_id']}" if seat['rule_id'] is not None else ' | обычное место'
        lines.append(f"Место {n+1}: {slots.label(seat)}{rule} | "+(f"Discord {occupied[n]}" if n in occupied else 'свободно'))
    if allocation is None:lines.append('Внимание: текущую основу невозможно распределить по требованиям.')
    lines.append('\nРезерв места не занимает. Класс и броня берутся из карточки персонажа при записи.')
    return '\n'.join(lines)

def install(env):
    async def changed(i,number,promoted):
        await env.client.refresh(number)
        await env.client.notify_promotions(number,promoted)
    def guard(i,number):
        env.check(i,env.CFG['leader_role_names']);env.client.check_board(i,number)
    @env.client.tree.command(name='требование-сбора',description='Лидеру: выделить места определённой роли под классы или тип брони')
    @app_commands.guild_only()
    @app_commands.choices(роль=[app_commands.Choice(name=n,value=n) for n in ROLES],броня=[app_commands.Choice(name=n,value=n) for n in ARMORS])
    @app_commands.describe(номер='Номер существующего сбора',мест='Сколько отдельных мест выделить, не увеличивая общий размер',классы='Допустимые классы через запятую; например Шаман или Охотник,Шаман',броня='Тип брони только для этих мест')
    async def add(i:discord.Interaction,номер:int,роль:str,мест:app_commands.Range[int,1,40],классы:str|None=None,броня:str|None=None):
        guard(i,номер);await i.response.defer(ephemeral=True,thinking=True)
        async with env.client.event_lock(номер):
            rule,promoted=env.DB.add_requirement(номер,i.user.id,роль,мест,классы or '',броня or '')
            await changed(i,номер,promoted)
        await env.reply(i,f'Требование №{rule} добавлено. Остальные места сохранились. Основе нужно заново подтвердить готовность.')
    @env.client.tree.command(name='убрать-требование',description='Лидеру: вернуть выделенные места к общим фильтрам сбора')
    @app_commands.guild_only()
    async def remove(i:discord.Interaction,номер:int,правило:int):
        guard(i,номер);await i.response.defer(ephemeral=True,thinking=True)
        async with env.client.event_lock(номер):
            promoted=env.DB.remove_requirement(номер,i.user.id,правило);await changed(i,номер,promoted)
        await env.reply(i,'Требование убрано. Количество мест не изменилось; готовность нужно подтвердить заново.')
    @env.client.tree.command(name='повторы-класса',description='Лидеру: ограничить количество игроков одного класса в основе')
    @app_commands.guild_only()
    @app_commands.describe(максимум='Общий максимум для каждого класса; 0 отключает ограничение')
    async def limit(i:discord.Interaction,номер:int,максимум:app_commands.Range[int,0,40]):
        guard(i,номер);await i.response.defer(ephemeral=True,thinking=True)
        async with env.client.event_lock(номер):
            promoted=env.DB.set_class_limit(номер,i.user.id,максимум);await changed(i,номер,promoted)
        await env.reply(i,'Лимит повторов обновлён. После изменения правил основе нужно подтвердить готовность заново.')
