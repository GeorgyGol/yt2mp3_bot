"""download mp3 from youtube
!!! need ffmpeg installed !!!
In linux (from personal expirience) func youtube_dl.download make strange MP3 format (not playing in some players)
so... in's converting by ffmpeg to mp3 - and in's work!

how to install ffmpeg (on linus):
   sudo apt update
   sudo apt install ffmpeg

check...
    ffmpeg -version
"""

from os import getpid, path
import youtube_dl
import asyoutdl

import re
import json
from collections import OrderedDict
from config import BASE_MP3_PATH
import asyncio

# class MyLogger(object):
#     def debug(self, msg):
#         # if re.search(r'\[download\]\s+\d+', msg):
#         if msg[0] == '\r':
#             # redis_client[self.keyproc] = msg[1:]
#             self.rds.set(key=subkeys.progress, value=msg)
#         else:
#             return
#
#     def info(self, msg):
#         print('INF:', msg)
#
#     def warning(self, msg):
#         self.rds.set(key=subkeys.warning, value=f'WARNING: {msg}')
#
#     def critical(self, msg):
#         self.rds.set(key=subkeys.critical, value=f'CRITICAL ERROR: {msg}')
#
#     def error(self, msg):
#         self.rds.set(key=subkeys.error, value=msg)
#
#     def __init__(self, proc_id=None, prefix=None):
#         self.pid = proc_id
#         self.prefix = prefix
#         self.rds = RedisKeys(prefix=prefix, procID=proc_id)


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
        if which=='audio':
            self.options = self._audio_opt(format, logger, name_template)
        elif which=='video':
            self.options = self._video_opt(format, logger, name_template, video_quality)

    def add_progress_hook(self, hook):
        self.options.update({'progress_hooks': [hook, ]})
        return self.options

    def external_downloader(self, dwnldr):
        self.options.update({'external_downloader': dwnldr})
        # pprint(options)

def _prepare_download(url=None, logger=None, base_path=None):

    def get_info(json_str):
        return {'id': json_str['id'], 'title': json_str['title'],
                'url': json_str['webpage_url']}

    opt = {'logger': logger, 'noplaylist':True}

    # with youtube_dl.YoutubeDL(opt) as ydl:
    with youtube_dl.YoutubeDL(opt) as ydl:

        info = ydl.extract_info(url=url, download=False)

        # get_formats(info)
        src = list()
        template_name = f'{base_path}%(title)s.%(ext)s'
        src = [get_info(info), ]

        # print_opt(info)
        return src, template_name

def download1(strUrl=None, keep_video=False, audio_format='mp3',
              base_path = BASE_MP3_PATH, video_quality='360'):
    assert strUrl

    # logg = MyLogger(prefix=pre, proc_id=getpid())
    logg=None
    # rds = RedisKeys(prefix=pre, procID=getpid())
    # rds.set(key=subkeys.status, value='STARTED')
    try:
        source, templ_file = _prepare_download(logger=logg, url=strUrl, base_path=base_path)
        print('OPT:', source, templ_file)
    except youtube_dl.utils.DownloadError as e:
        # rds.set(key=subkeys.status, value='STOPED')
        # rds.set(key=subkeys.error, value=e)
        return -1

    opts = dwnOptions(which='video' if keep_video else 'audio', video_quality=video_quality,
                      format=audio_format, logger=logg, name_template=templ_file)

    with youtube_dl.YoutubeDL(opts.options) as ydl:

        #         ydl.cache.remove()
        # if callable_hook:
        filename = path.join(base_path, f'{source[0]["title"]}.{audio_format}')
        try:
            ydl.download([source[0]['url']])
            # rds.set_file(filename=filename, status='done')
        except youtube_dl.utils.DownloadError as yterr:
            # rds.set_file(filename=filename, status='error', info='download error')
            pass
        except:
            # rds.set_file(filename=filename, status='error', info='some base error')
            pass

    # rds.set(key=subkeys.status, value='STOPED')
    return filename


async def _prepare_download_asy(url=None, logger=None, base_path=None):

    def get_info(json_str):
        return {'id': json_str['id'], 'title': json_str['title'],
                'url': json_str['webpage_url']}

    opt = {'logger': logger, 'noplaylist':True}

    with youtube_dl.YoutubeDL(opt) as ydl:
    # with asyoutdl.AsyYoutubeDL(opt) as ydl:

        info = ydl.extract_info(url=url, download=False)

        # get_formats(info)
        # src = list()
        template_name = f'{base_path}%(title)s.%(ext)s'
        # src = {'id': info['id'], 'title': json_str['title'],
        #         'url': json_str['webpage_url']}

        # print_opt(info)
        return info, template_name


async def down_asy(url=None):
    try:
        source, templ_file = await _prepare_download_asy(logger=None, url=url, base_path='')
        print('OPT:', source, templ_file)
    except youtube_dl.utils.DownloadError as e:
        return -1

    opts = dwnOptions(which='audio', name_template=templ_file, format='mp3')


    with asyoutdl.AsyYoutubeDL(opts.options) as ydl:
        #         ydl.cache.remove()
        # if callable_hook:
        # opts.external_downloader(asyoutdl.asyhttp(ydl=ydl, params=opts.options))
        filename = path.join('', f'{source["title"]}.mp3')
        try:
            await ydl.download([source['webpage_url']])
        except asyncio.exceptions.CancelledError as err:
            print(err)

    return filename

async def corro_rapidomente(tsk):
    for i in range(10000):
        print('\ncorro', i)
        if i>4:
            tsk.cancel('WHAM!!')
        await asyncio.sleep(1)


async def main(url):
    # Schedule three calls *concurrently*:

    tDwnl = asyncio.create_task(down_asy(url=url))
    tRaton = asyncio.create_task(corro_rapidomente(tDwnl))

    L = await asyncio.gather(
        tRaton,
        tDwnl
    )
    print(L)



if __name__ == '__main__':

    # video_url = 'https://www.youtube.com/watch?v=jzD_yyEcp0M'
    video_url = 'https://www.youtube.com/watch?v=8Fl6d_fSRNs&list=PLkz3fL8MYDt5FbiY3A1g65CdGaI_CISm4'
    # video_url = 'https://www.youtube.com/watch?v=Vh_3zdmaHbk&list=PLkz3fL8MYDt5FbiY3A1g65CdGaI_CISm4&index=3'
    #
    # download1(strUrl=video_url)
    # down_asy(video_url)
    asyncio.run(main(video_url))
    print('All done.')
