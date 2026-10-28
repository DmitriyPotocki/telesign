"""Common commands: /start, /help, /cancel, /new."""
from __future__ import annotations

from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.types import Message

from ..services.context import AppContext

router = Router(name="common")

HELP_TEXT = """\
<b>Бот для дизайнеров интерьера</b>

<b>1. Пришли картинки + бриф одним сообщением.</b> Подпись на фото или на альбоме — \
это и есть бриф, бот начнёт сразу. Никакая разметка не нужна: роли (молд, текстура, \
референс) определяются сами, а обычные слова в подписи («дуб тёмный») работают как подсказка.

<b>2. Или в два этапа:</b> сначала картинки — бот сразу покажет блоки \
«🔧 Шаг #N · смотрю изображение» с рассуждениями (он сам назовёт, что на картинке), \
потом напиши бриф обычным языком текстом или голосом: \
<i>«Сделай скандинавский стиль, пол светлый дуб, 4 варианта»</i>. Тогда бот покажет \
галерею — текущий результат и сгенерированные альтернативы — тапни номер, что править.

Бот проанализирует материалы («🔧 Шаг #N · анализ»), сгенерирует варианты и пришлёт \
<b>все файлы без сжатия</b> (документами): победитель — первым с кнопками, \
остальные — сразу за ним с номерами («Вариант №N»).

<b>3. Правки.</b> Прикрепил картинку к сообщению — бот правит именно её. Прислал \
текст без картинки — выбери номер в галерее. Поправил своё сообщение в чате — бот \
ответит заново. Следующий бриф работает поверх последнего шага; история и откат — /history.

Команды:
/instructs — постоянные инструкции к нейросети
/new — новый проект
/history — шаги текущего проекта и откат
/files — сохранённые файлы
/settings — варианты по умолчанию, формат кадра
/selfcheck — самопроверка
/cancel — отмена текущей генерации"""


@router.message(CommandStart())
async def cmd_start(message: Message, ctx: AppContext) -> None:
    await ctx.db.ensure_user(message.from_user.id)
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(HELP_TEXT, parse_mode="HTML")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, ctx: AppContext) -> None:
    cancelled = ctx.queue.cancel(message.chat.id)
    if not cancelled and ctx.queue.busy(message.chat.id):
        await message.answer("Запрос выполняется, прервать уже поздно — дождись результата.")
    else:
        await message.answer("Отменено." if cancelled else "Сейчас ничего не выполняется.")


@router.message(Command("new"))
async def cmd_new(message: Message, ctx: AppContext) -> None:
    project_id = await ctx.db.create_project(message.from_user.id)
    await ctx.db.set_active_step(project_id, None)
    await message.answer(
        f"Новый проект #{project_id} создан. Файлы и инструкции сохранились, цепочка шагов чистая."
    )
