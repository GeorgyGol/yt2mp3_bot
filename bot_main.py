import logging

from aiogram import Bot, Dispatcher, executor, types
from aiogram.contrib.fsm_storage.redis import RedisStorage2
from aiogram.contrib.middlewares.logging import LoggingMiddleware
from aiogram.dispatcher import FSMContext
from aiogram.dispatcher.filters import Text
from aiogram.dispatcher.filters.state import State, StatesGroup
from aiogram.types import (InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup,
                           KeyboardButton)
from aiogram.types.message import ParseMode
from aiogram.types.input_file import InputFile
from shutil import rmtree

from aiogram.utils.markdown import text, hbold, html_decoration

import config
from y_down import download1
from multiprocessing import Process, active_children
from os import getpid, path
import asyncio

storage = RedisStorage2(config.REDIS_HOST, 6379, db=0)
logging.basicConfig(level=logging.ERROR)

bot = Bot(token=config.BOT_TOKEN)
dp = Dispatcher(bot, storage=storage)
dp.middleware.setup(LoggingMiddleware())

def_video = 250


class DownloadState(StatesGroup):
    select = State()
    dwnload = State()


class InfoMessage():
    sAudio = 'Качаем аудио.\nАдресок изволите?'
    sVideo = 'Качаем видео.\nТекущее качество "{qual}"\nАдресок изволите?'
    sStart = 'Что качаем?'

async def down_load(url, chat_id=None):
    fl = download1(strUrl=url, keep_video=False)
    await bot.send_document(chat_id=chat_id, document=InputFile(fl))
    rmtree(fl, ignore_errors=True)
    return 0

def iterate_group(iterator, count):
    for i in range(0, len(iterator), count):
        yield iterator[i:i + count]


def main_menu():
    rkbt = ReplyKeyboardMarkup(resize_keyboard=True)
    rkbt.row(KeyboardButton('main'), KeyboardButton('help'), KeyboardButton('stop'))
    return rkbt


def menu():
    ikbt = InlineKeyboardMarkup()
    ikbt.row(InlineKeyboardButton(f'audio', callback_data='audio'),
             InlineKeyboardButton(f'video', callback_data='video'))

    return ikbt


def video_menu():
    ikbt = InlineKeyboardMarkup()
    ikbt.row(InlineKeyboardButton('tiny', callback_data='tiny'),
             InlineKeyboardButton('150', callback_data='150'),
             InlineKeyboardButton('250', callback_data='250'))
    ikbt.row(InlineKeyboardButton('360', callback_data='360'),
             InlineKeyboardButton('480', callback_data='480'),
             InlineKeyboardButton('720', callback_data='720'),
             InlineKeyboardButton('best', callback_data='best'))

    return ikbt


async def show_menu(chat_id):
    await bot.send_message(chat_id=chat_id, text='Что качаем?', parse_mode=ParseMode.HTML, reply_markup=menu())


@dp.message_handler(commands='start', state='*')
@dp.message_handler(Text(equals='start'), state='*')
async def start(message: types.Message, state: FSMContext):
    if message.chat.type == 'group':
        return
    await state.finish()

    th = text(html_decoration.bold(f'Здравствуйте {message.from_user.full_name}!'),
              'Вас приветствует телеграм бот-качалка из youtube',
              sep='\n')

    await bot.send_message(chat_id=message.chat.id, text=th, parse_mode=ParseMode.HTML, reply_markup=main_menu())

    _mess = text(InfoMessage.sStart, sep='\n')
    await bot.send_message(chat_id=message.chat.id,
                           text=_mess, reply_markup=menu(), parse_mode=ParseMode.HTML)

    await DownloadState.select.set()
    await state.update_data(videoh=str(def_video))


@dp.message_handler(commands='main', state='*')
@dp.message_handler(Text(equals='main'), state='*')
async def main(message: types.Message, state: FSMContext):
    await DownloadState.select.set()
    _mess = text(InfoMessage.sStart, sep='\n')
    await bot.send_message(chat_id=message.chat.id,
                           text=_mess, reply_markup=menu(), parse_mode=ParseMode.HTML)


@dp.message_handler(state='*', commands='help')
@dp.message_handler(Text(equals='help'), state='*')
async def show_help(message: types.Message, state: FSMContext):
    _mess = text(hbold('Качалка с youtube (только по одному адреса за раз, списки запрещены)'),
                 'Качает аудио (в максимальном качестве) и видео (качество на выбор)',
                 'Команды:',
                 ' - /main - возврат на первый экран',
                 ' - /back - возврат на предидущий экран',
                 ' - /stop - остановить активное скачивание',
                 ' - /help - это хелп',
                 hbold('Выбор "чего качаем":'),
                 ' - audio - качаем аудио (вводим адрес и качаем)',
                 ' - video - качаем видео (выбор качества)',
                 hbold(
                     'Выбор качества видео (чем лучше качество, тем больше конечный файл, тем должше будет скачивание - подумайте, а оно вам надо?):'),
                 ' - tiny - самое плохое (самый маленький файл)',
                 ' - 150 - по высоте не менее 150',
                 ' - 250 - по высоте не менее 250',
                 ' - 360 - по высоте не менее 360',
                 ' - 480 - по высоте не менее 480',
                 ' - 720 - по высоте не менее 720 (почти HD)',
                 ' - best - самое лучшее (ждать долго)',
                 hbold('А дальше вводим адрес и качаем. Скаченный файл посылается в этот чат (персональный, с ботом)'),
                 sep='\n')
    await bot.send_message(chat_id=message.chat.id, disable_web_page_preview=True,
                           text=_mess, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
    await show_menu(message.chat.id)


@dp.message_handler(commands='stop', state='*')
@dp.message_handler(Text(equals='stop'), state='*')
async def show_state(message: types.Message, state: FSMContext):
    print('STOP PRESSED')
    st = await state.get_state()
    ud = await state.get_data()
    if st==DownloadState.dwnload:
        await DownloadState.select.set()
        await bot.send_message(chat_id=message.chat.id, text=f'Останавливаем процесс {ud["curproc"]}')
        for chp in active_children():
            if str(chp.pid) == ud["curproc"]:
                chp.terminate()
                await state.update_data(curproc=0)



@dp.callback_query_handler(Text(equals=['audio', 'video'], ignore_case=True), state=DownloadState.select)
async def process_select(callback_query: types.CallbackQuery, state: FSMContext):
    await bot.answer_callback_query(callback_query.id)
    ud = await state.get_data()

    if callback_query.data == 'audio':
        await bot.send_message(chat_id=callback_query.message.chat.id, parse_mode=types.ParseMode.HTML,
                               text=InfoMessage.sAudio, reply_markup=main_menu())
        await state.update_data(current='audio')

    else:
        await callback_query.message.reply(InfoMessage.sVideo.format(qual=ud["videoh"]),
                                           reply_markup=video_menu())
        await state.update_data(current='video')


@dp.callback_query_handler(Text(equals=['tiny', 'best', '150', '250', '360', '480', '720'], ignore_case=True),
                           state=DownloadState.select)
async def process_select_video(callback_query: types.CallbackQuery, state: DownloadState):
    await bot.answer_callback_query(callback_query.id)
    await state.update_data(videoh=callback_query.data)
    # st = await state.get_state()

    ud = await state.get_data()
    await bot.edit_message_text(message_id=callback_query.message.message_id,
                                chat_id=callback_query.message.chat.id,
                                parse_mode=types.ParseMode.HTML,
                                text=InfoMessage.sVideo.format(qual=ud["videoh"]),
                                reply_markup=video_menu())


#     await message.reply(text='Непонятная команда')


# async def down_load(url, chat_id=None):
#     try:
#         fl = download1(strUrl=url, keep_video=False)
#         await bot.send_document(chat_id=chat_id, document=InputFile(fl))
#         rmtree(fl, ignore_errors=True)
#         return 0
#     finally:
#         return -1

@dp.message_handler(state=DownloadState.select)
async def echo(message: types.Message, state: FSMContext):
    st = await state.get_state()

    ud = await state.get_data()

    if ud['current'] == 'audio':
        # download audio
        # await bot.send_message(chat_id=message.chat.id, text='Will be audio download')
        await bot.send_message(chat_id=message.chat.id, text=f'Will be audio download {message.text}',
                               parse_mode=types.ParseMode.HTML, reply_markup=main_menu())

        # asyncio.create_task(down_load(url=message.text, chat_id=message.chat.id))

        # fl = await download1(strUrl=message.text)
        # fl=None
        # p = Process(target = down_load, args=(message.text, message.chat.id))
        await DownloadState.dwnload.set()

        # p.start()
        # print(p.pid)
        await down_load(message.text, chat_id=message.chat.id)
        # await state.update_data(curproc=p.pid)

        # p.join()
        # print(fl)
        # if path.exists(fl):
        #     await bot.send_document(chat_id=message.chat.id, document=InputFile(fl))
        #     rmtree(fl, ignore_errors=True)

    elif ud['current'] == 'video':
        # select video quality
        await bot.send_message(chat_id=message.chat.id, text='Качаем видео')

    else:
        await bot.send_message(chat_id=message.chat.id, text='Что-то непонятное происходит...')


#
#
# @dp.callback_query_handler(Text(equals='mons', ignore_case=True), state=OrderFood.select_rubr)
# async def process_mons(callback_query: types.CallbackQuery, state: FSMContext):
#     await bot.answer_callback_query(callback_query.id)
#     await OrderFood.select_mon.set()
#     await state.update_data(current_R=callback_query.data, top=botserv.list_pages[0], select_mons='main')
#     await bot.send_message(chat_id=callback_query.message.chat.id, parse_mode=types.ParseMode.HTML,
#                            text=hbold('МОНИТОРИНГИ'), reply_markup=rplmain_menu(show_back=True))
#     await bot.send_message(chat_id=callback_query.message.chat.id,
#                            text='Выберете мониторинг', reply_markup=mons_menu())
#
#
# @dp.callback_query_handler(Text(equals=['infl', 'fin', 'trends', 'rail', 'tends',
#                                         'santech', 'growtech', 'socmon', 'tez'],
#                                 ignore_case=True), state=OrderFood.select_mon)
# async def process_mon_items(callback_query: types.CallbackQuery, state: FSMContext):
#     await bot.answer_callback_query(callback_query.id)
#     await state.update_data(select_mons=callback_query.data, top=botserv.list_pages[0])
#     await show_items(callback_query.message, state)
#
#
# @dp.callback_query_handler(lambda cbm: cbm.data and cbm.data in list(map(str, botserv.list_pages)),
#                            state=[OrderFood.select_rubr, OrderFood.select_mon])
# async def process_news(callback_query: types.CallbackQuery, state: FSMContext):
#     await bot.answer_callback_query(callback_query.id)
#     await state.update_data(top=int(callback_query.data))
#     await show_items(callback_query.message, state)
#
#
# @dp.callback_query_handler(state=OrderFood.select_rubr)
# async def process_select(callback_query: types.CallbackQuery, state: FSMContext):
#     await bot.answer_callback_query(callback_query.id)
#     await state.update_data(current_R=callback_query.data)
#     _mess = 'Выберете раздел:'
#     await bot.edit_message_text(message_id=callback_query.message.message_id,
#                                 chat_id=callback_query.message.chat.id,
#                                 parse_mode=types.ParseMode.HTML,
#                                 text=f'{_mess}{callback_query.data}', reply_markup=main_menu())
#     ud = await state.get_data()
#     print(ud)
#
# @dp.message_handler(state='*')
# async def echo(message: types.Message, state: FSMContext):
#     await message.reply(text='Непонятная команда')
#     await show_context_menu(message, state)
#
#
# @dp.message_handler(content_types=ContentType.ANY, state='*')
# async def unknown_message(msg: types.Message, state: FSMContext):
#     message_text = text('Ха-ха. Вы пошутили - я тоже посмеялся',
#                         '(help вам в помощь)')
#
#     await bot.send_message(msg.from_user.id, message_text, parse_mode=types.ParseMode.HTML,
#                            reply_markup=main_menu())
#     await show_context_menu(msg, state)
#
#
# async def shutdown(dispatcher: Dispatcher):
#     await dispatcher.storage.close()
#     await dispatcher.storage.wait_closed()


if __name__ == '__main__':
    executor.start_polling(dp, skip_updates=True)
