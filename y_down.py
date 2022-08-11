import asyncio
import pathlib
import shutil
from os import path

import youtube_dl
from aiogram.types.input_file import InputFile

import asyoutdl
from config import BASE_MP3_PATH


# some service funtions

def get_formats(video_info):
    """
    get all possible formats for download - print its on console

    :param video_info: meta from Youtube
    :return:
    """

    print('=' * 50)
    # pprint(file_info)
    formats = video_info.get('formats', [video_info])
    for f in formats:
        print('FS:', f['filesize'], 'formID:', f['format_id'], 'formNote:', f['format_note'], 'EXT:', f['ext'])
    print('=' * 50)


def print_opt(opt, from_name=''):
    from pprint import pprint
    print(f'============ options from {from_name} =====================')
    pprint(opt)
    print('============================================================')


# ===============================================================

class dwnOptions():

    def _audio_opt(self, format, logger, name_template):
        return {
            'format': 'bestaudio/best',
            'postprocessors': [{
                'key': 'FFmpegExtractAudio',
                'preferredcodec': format,
            }],
            'outtmpl': name_template,
            'logger': logger,
            'prefer_ffmpeg': True,
            'keepvideo': False,
            'noplaylist': True
        }

    def _video_opt(self, format, logger, name_template, vq):
        # post_proc = [{
        #     'key': 'FFmpegVideoConvertor',
        #     'preferedformat': audio_format,
        #     'merge-output-format': audio_format,
        #     'preferredcodec': audio_format,
        # }]
        post_proc = [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': format,
            'merge-output-format': format,
            'preferredcodec': format,
        }]

        # ffmpform = 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]'
        if vq == 'best':
            ffmpform = 'bestvideo[ext=mp4]+bestaudio/best[ext=mp4]'
        elif vq == 'tiny':
            ffmpform = 'worstvideo[ext=mp4]+bestaudio/worstbest[ext=mp4]'
        else:
            try:
                ht = int(vq)
                ffmpform = f'bestvideo[height <= {ht}][ext=mp4]+bestaudio/best[height <= {ht}][ext=mp4]'
            except:
                ffmpform = 'worstvideo[ext=mp4]+bestaudio/worst[ext=mp4]'
        # ffmpform = 'bestvideo[height <= 480][ext=mp4]+bestaudio/best[height <= 480][ext=mp4]'

        return {
            'format': ffmpform,
            'outtmpl': name_template,
            'logger': logger,
            'prefer_ffmpeg': True,
            'keepvideo': True,
            'noplaylist': True
        }

    def __init__(self, which='audio', format='mp3', logger=None, name_template='', video_quality='360'):
        if which == 'audio':
            self.options = self._audio_opt(format, logger, name_template)
        elif which == 'video':
            self.options = self._video_opt(format, logger, name_template, video_quality)

    def add_progress_hook(self, hook):
        self.options.update({'progress_hooks': [hook, ]})
        return self.options

def _prepare_download_asy(url=None, logger=None, template='%(title)s.%(ext)s'):
    opt = {'logger': logger, 'noplaylist': True, 'outtmpl': template}

    with youtube_dl.YoutubeDL(opt) as ydl:
        return ydl.extract_info(url=url, download=False), template


async def down_asy(url=None, base_path=None, format='mp3', tele_message=None, video_size='250'):
    def clear_tails():
        shutil.rmtree(base_path, ignore_errors=True)
        # dir = pathlib.Path(base_path)
        # for f in dir.glob('*'):
        #     f.unlink()

    Msg = await tele_message.bot.send_message(chat_id=tele_message.chat.id, text=f'Загружаем:')

    template = f'{base_path}%(title)s.%(ext)s'
    try:
        source, templ = _prepare_download_asy(logger=None, url=url, template=template)
    except youtube_dl.utils.DownloadError as e:
        msg_err = await tele_message.bot.send_message(chat_id=tele_message.chat.id,
                                                      text=f'Сбор информации о скачиваемом ролике\n{e}\nОстановлено')
        # await msg_err.edit_text(msg_err.text + ' - вот так')

        return -1
    if format == 'mp3':
        opts = dwnOptions(which='audio', format=format, name_template=templ)
    else:
        opts = dwnOptions(which='video', format=format, name_template=templ, video_quality=video_size)

    with asyoutdl.AsyYoutubeDL(opts.options, tele_message=Msg) as ydl:
        fname = pathlib.Path(ydl.prepare_filename(source)).stem
        filename = path.join(base_path, f'{fname}.{format}')

        try:
            await ydl.download([source['webpage_url']])
            fl = InputFile(path_or_bytesio=filename)
            Message = await tele_message.bot.send_document(chat_id=tele_message.chat.id, document=fl.get_file())
            # pathlib.Path(filename).unlink()
            clear_tails()

        except asyncio.exceptions.CancelledError as err:
            # cancel by user - clear dwnld files
            clear_tails()
            await tele_message.bot.send_message(chat_id=tele_message.chat.id,
                                                text=f'Остановлено пользователем')
        except youtube_dl.utils.DownloadError as err:
            await tele_message.bot.send_message(chat_id=tele_message.chat.id,
                                                text=f'Ошибка скачивания: {err}\nПопробуйте скачать еще раз - так бывает')
        except FileNotFoundError:
            if format != 'mp3':
                filename = path.join(base_path, f'{fname}.mkv')
                fl = InputFile(path_or_bytesio=filename)
                Message = await tele_message.bot.send_document(chat_id=tele_message.chat.id, document=fl.get_file())
                clear_tails()
                # pathlib.Path(filename).unlink()
            else:
                raise FileNotFoundError

    return filename


if __name__ == '__main__':

    # video_url = 'https://www.youtube.com/watch?v=jzD_yyEcp0M'
    video_url = 'https://www.youtube.com/watch?v=8Fl6d_fSRNs&list=PLkz3fL8MYDt5FbiY3A1g65CdGaI_CISm4'
    video_url = 'https://www.youtube.com/watch?v=cRkxq0xqGpc'


    # video_url = 'https://www.youtube.com/watch?v=Vh_3zdmaHbk&list=PLkz3fL8MYDt5FbiY3A1g65CdGaI_CISm4&index=3'
    #
    # download1(strUrl=video_url)
    # down_asy(video_url)
    # asyncio.run(main(video_url))
    def clear_tails(fname):
        print('CLEAR', 'FILES/420049032/', fname)
        dir = pathlib.Path('FILES/420049032/')
        print(list(dir.glob('*')))
        for f in dir.glob(f'*'):
            print(f)
            # f.unlink()


    clear_tails('INNA - Shining Star [Online Video]')
    print('All done.')
