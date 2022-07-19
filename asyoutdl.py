from __future__ import absolute_import, unicode_literals

import asyncio
import contextlib
import datetime
import errno
import fileinput
import html
import io
import json
import os
import random
import re
import socket
import time
import traceback

import requests
from youtube_dl import YoutubeDL
from youtube_dl.compat import (
    compat_http_client,
    compat_numeric_types,
    compat_str,
    compat_urllib_error,
)
from youtube_dl.downloader import get_external_downloader, PROTOCOL_MAP, FFmpegFD, HlsFD
from youtube_dl.downloader.http import HttpFD
from youtube_dl.postprocessor import (
    FFmpegFixupM3u8PP,
    FFmpegFixupM4aPP,
    FFmpegFixupStretchedPP,
    FFmpegMergerPP,
)
from youtube_dl.utils import (
    ContentTooShortError,
    DEFAULT_OUTTMPL,
    determine_ext,
    determine_protocol,
    DownloadError,
    encode_compat_str,
    encodeFilename,
    error_to_compat_str,
    ExtractorError,
    formatSeconds,
    GeoRestrictedError,
    int_or_none,
    ISO3166Utils,
    MaxDownloadsReached,
    PostProcessingError,
    prepend_extension,
    replace_extension,
    SameFileError,
    sanitize_path,
    sanitize_url,
    sanitized_Request,
    subtitles_filename,
    UnavailableVideoError,
    url_basename,
    write_json_file,
)
# from youtube_dl.downloader.common import FileDownloader
from youtube_dl.utils import (
    sanitize_open,
    write_xattr,
    XAttrMetadataError,
    XAttrUnavailableError,
)

import config


def get_suitable_downloader1(info_dict, params={}):
    """Get the downloader class that can handle the info dict."""
    protocol = determine_protocol(info_dict)
    info_dict['protocol'] = protocol

    # if (info_dict.get('start_time') or info_dict.get('end_time')) and not info_dict.get('requested_formats') and FFmpegFD.can_download(info_dict):
    #     return FFmpegFD

    external_downloader = params.get('external_downloader')
    if external_downloader is not None:
        ed = get_external_downloader(external_downloader)
        if ed.can_download(info_dict):
            return ed

    if protocol.startswith('m3u8') and info_dict.get('is_live'):
        return FFmpegFD

    if protocol == 'm3u8' and params.get('hls_prefer_native') is True:
        return HlsFD

    if protocol == 'm3u8_native' and params.get('hls_prefer_native') is False:
        return FFmpegFD

    return PROTOCOL_MAP.get(protocol, asyhttp)


class asyhttp(HttpFD):
    async def real_download(self, filename, info_dict):
        url = info_dict['url']

        class DownloadContext(dict):
            __getattr__ = dict.get
            __setattr__ = dict.__setitem__
            __delattr__ = dict.__delitem__

        ctx = DownloadContext()
        ctx.filename = filename
        ctx.tmpfilename = self.temp_name(filename)
        ctx.stream = None

        # Do not include the Accept-Encoding header
        headers = {'Youtubedl-no-compression': 'True'}
        add_headers = info_dict.get('http_headers')
        if add_headers:
            headers.update(add_headers)

        is_test = self.params.get('test', False)
        chunk_size = self._TEST_FILE_SIZE if is_test else (
                info_dict.get('downloader_options', {}).get('http_chunk_size')
                or self.params.get('http_chunk_size') or 0)

        ctx.open_mode = 'wb'
        ctx.resume_len = 0
        ctx.data_len = None
        ctx.block_size = self.params.get('buffersize', 1024)
        ctx.start_time = time.time()
        ctx.chunk_size = None

        if self.params.get('continuedl', True):
            # Establish possible resume length
            if os.path.isfile(encodeFilename(ctx.tmpfilename)):
                ctx.resume_len = os.path.getsize(
                    encodeFilename(ctx.tmpfilename))

        ctx.is_resume = ctx.resume_len > 0

        count = 0
        retries = self.params.get('retries', 0)

        class SucceedDownload(Exception):
            pass

        class RetryDownload(Exception):
            def __init__(self, source_error):
                self.source_error = source_error

        class NextFragment(Exception):
            pass

        def set_range(req, start, end):
            range_header = 'bytes=%d-' % start
            if end:
                range_header += compat_str(end)
            req.add_header('Range', range_header)

        def establish_connection():
            ctx.chunk_size = (random.randint(int(chunk_size * 0.95), chunk_size)
                              if not is_test and chunk_size else chunk_size)
            if ctx.resume_len > 0:
                range_start = ctx.resume_len
                if ctx.is_resume:
                    self.report_resuming_byte(ctx.resume_len)
                ctx.open_mode = 'ab'
            elif ctx.chunk_size > 0:
                range_start = 0
            else:
                range_start = None
            ctx.is_resume = False
            range_end = range_start + ctx.chunk_size - 1 if ctx.chunk_size else None
            if range_end and ctx.data_len is not None and range_end >= ctx.data_len:
                range_end = ctx.data_len - 1
            has_range = range_start is not None
            ctx.has_range = has_range
            request = sanitized_Request(url, None, headers)
            if has_range:
                set_range(request, range_start, range_end)
            # Establish connection
            try:
                try:
                    ctx.data = self.ydl.urlopen(request)
                except (compat_urllib_error.URLError,) as err:
                    # reason may not be available, e.g. for urllib2.HTTPError on python 2.6
                    reason = getattr(err, 'reason', None)
                    if isinstance(reason, socket.timeout):
                        raise RetryDownload(err)
                    raise err
                # When trying to resume, Content-Range HTTP header of response has to be checked
                # to match the value of requested Range HTTP header. This is due to a webservers
                # that don't support resuming and serve a whole file with no Content-Range
                # set in response despite of requested Range (see
                # https://github.com/ytdl-org/youtube-dl/issues/6057#issuecomment-126129799)
                if has_range:
                    content_range = ctx.data.headers.get('Content-Range')
                    if content_range:
                        content_range_m = re.search(r'bytes (\d+)-(\d+)?(?:/(\d+))?', content_range)
                        # Content-Range is present and matches requested Range, resume is possible
                        if content_range_m:
                            if range_start == int(content_range_m.group(1)):
                                content_range_end = int_or_none(content_range_m.group(2))
                                content_len = int_or_none(content_range_m.group(3))
                                accept_content_len = (
                                    # Non-chunked download
                                        not ctx.chunk_size
                                        # Chunked download and requested piece or
                                        # its part is promised to be served
                                        or content_range_end == range_end
                                        or content_len < range_end)
                                if accept_content_len:
                                    ctx.data_len = content_len
                                    return
                    # Content-Range is either not present or invalid. Assuming remote webserver is
                    # trying to send the whole file, resume is not possible, so wiping the local file
                    # and performing entire redownload
                    self.report_unable_to_resume()
                    ctx.resume_len = 0
                    ctx.open_mode = 'wb'
                ctx.data_len = int_or_none(ctx.data.info().get('Content-length', None))
                return
            except (compat_urllib_error.HTTPError,) as err:
                if err.code == 416:
                    # Unable to resume (requested range not satisfiable)
                    try:
                        # Open the connection again without the range header
                        ctx.data = self.ydl.urlopen(
                            sanitized_Request(url, None, headers))
                        content_length = ctx.data.info()['Content-Length']
                    except (compat_urllib_error.HTTPError,) as err:
                        if err.code < 500 or err.code >= 600:
                            raise
                    else:
                        # Examine the reported length
                        if (content_length is not None
                                and (ctx.resume_len - 100 < int(content_length) < ctx.resume_len + 100)):
                            # The file had already been fully downloaded.
                            # Explanation to the above condition: in issue #175 it was revealed that
                            # YouTube sometimes adds or removes a few bytes from the end of the file,
                            # changing the file size slightly and causing problems for some users. So
                            # I decided to implement a suggested change and consider the file
                            # completely downloaded if the file size differs less than 100 bytes from
                            # the one in the hard drive.
                            self.report_file_already_downloaded(ctx.filename)
                            self.try_rename(ctx.tmpfilename, ctx.filename)
                            self._hook_progress({
                                'filename': ctx.filename,
                                'status': 'finished',
                                'downloaded_bytes': ctx.resume_len,
                                'total_bytes': ctx.resume_len,
                            })
                            raise SucceedDownload()
                        else:
                            # The length does not match, we start the download over
                            self.report_unable_to_resume()
                            ctx.resume_len = 0
                            ctx.open_mode = 'wb'
                            return
                elif err.code < 500 or err.code >= 600:
                    # Unexpected HTTP error
                    raise
                raise RetryDownload(err)
            except socket.error as err:
                if err.errno != errno.ECONNRESET:
                    # Connection reset is no problem, just retry
                    raise
                raise RetryDownload(err)

        async def download():
            data_len = ctx.data.info().get('Content-length', None)

            # Range HTTP header may be ignored/unsupported by a webserver
            # (e.g. extractor/scivee.py, extractor/bambuser.py).
            # However, for a test we still would like to download just a piece of a file.
            # To achieve this we limit data_len to _TEST_FILE_SIZE and manually control
            # block size when downloading a file.
            if is_test and (data_len is None or int(data_len) > self._TEST_FILE_SIZE):
                data_len = self._TEST_FILE_SIZE

            if data_len is not None:
                data_len = int(data_len) + ctx.resume_len
                min_data_len = self.params.get('min_filesize')
                max_data_len = self.params.get('max_filesize')
                # TODO: to async
                if min_data_len is not None and data_len < min_data_len:
                    self.to_screen('\r[download] File is smaller than min-filesize (%s bytes < %s bytes). Aborting.' % (
                    data_len, min_data_len))
                    return False
                if max_data_len is not None and data_len > max_data_len:
                    self.to_screen('\r[download] File is larger than max-filesize (%s bytes > %s bytes). Aborting.' % (
                    data_len, max_data_len))
                    return False

            byte_counter = 0 + ctx.resume_len
            block_size = ctx.block_size
            start = time.time()

            # measure time over whole while-loop, so slow_down() and best_block_size() work together properly
            now = None  # needed for slow_down() in the first loop run
            before = start  # start measuring

            def retry(e):
                to_stdout = ctx.tmpfilename == '-'
                if ctx.stream is not None:
                    if not to_stdout:
                        ctx.stream.close()
                    ctx.stream = None
                ctx.resume_len = byte_counter if to_stdout else os.path.getsize(encodeFilename(ctx.tmpfilename))
                raise RetryDownload(e)

            while True:
                await asyncio.sleep(0)
                try:
                    # Download and write
                    data_block = ctx.data.read(
                        block_size if data_len is None else min(block_size, data_len - byte_counter))
                # socket.timeout is a subclass of socket.error but may not have
                # errno set
                except socket.timeout as e:
                    retry(e)
                except socket.error as e:
                    # SSLError on python 2 (inherits socket.error) may have
                    # no errno set but this error message
                    if e.errno in (errno.ECONNRESET, errno.ETIMEDOUT) or getattr(e, 'message',
                                                                                 None) == 'The read operation timed out':
                        retry(e)
                    raise

                byte_counter += len(data_block)

                # exit loop when download is finished
                if len(data_block) == 0:
                    break

                # Open destination file just in time
                if ctx.stream is None:
                    try:
                        ctx.stream, ctx.tmpfilename = sanitize_open(
                            ctx.tmpfilename, ctx.open_mode)
                        assert ctx.stream is not None
                        ctx.filename = self.undo_temp_name(ctx.tmpfilename)
                        self.report_destination(ctx.filename)
                    except (OSError, IOError) as err:
                        self.report_error('unable to open for writing: %s' % str(err))
                        return False

                    if self.params.get('xattr_set_filesize', False) and data_len is not None:
                        try:
                            write_xattr(ctx.tmpfilename, 'user.ytdl.filesize', str(data_len).encode('utf-8'))
                        except (XAttrUnavailableError, XAttrMetadataError) as err:
                            self.report_error('unable to set filesize xattr: %s' % str(err))

                try:
                    ctx.stream.write(data_block)
                except (IOError, OSError) as err:
                    self.to_stderr('\n')
                    self.report_error('unable to write data: %s' % str(err))
                    return False

                # Apply rate limit
                self.slow_down(start, now, byte_counter - ctx.resume_len)

                # end measuring of one loop run
                now = time.time()
                after = now

                # Adjust block size
                if not self.params.get('noresizebuffer', False):
                    block_size = self.best_block_size(after - before, len(data_block))

                before = after

                # Progress message
                speed = self.calc_speed(start, now, byte_counter - ctx.resume_len)
                if ctx.data_len is None:
                    eta = None
                else:
                    eta = self.calc_eta(start, time.time(), ctx.data_len - ctx.resume_len,
                                        byte_counter - ctx.resume_len)

                self._hook_progress({
                    'status': 'downloading',
                    'downloaded_bytes': byte_counter,
                    'total_bytes': ctx.data_len,
                    'tmpfilename': ctx.tmpfilename,
                    'filename': ctx.filename,
                    'eta': eta,
                    'speed': speed,
                    'elapsed': now - ctx.start_time,
                })

                if data_len is not None and byte_counter == data_len:
                    break

            if not is_test and ctx.chunk_size and ctx.data_len is not None and byte_counter < ctx.data_len:
                ctx.resume_len = byte_counter
                # ctx.block_size = block_size
                raise NextFragment()

            if ctx.stream is None:
                self.to_stderr('\n')
                self.report_error('Did not get any data blocks')
                return False
            if ctx.tmpfilename != '-':
                ctx.stream.close()

            if data_len is not None and byte_counter != data_len:
                err = ContentTooShortError(byte_counter, int(data_len))
                if count <= retries:
                    retry(err)
                raise err

            self.try_rename(ctx.tmpfilename, ctx.filename)

            # Update file modification time
            if self.params.get('updatetime', True):
                info_dict['filetime'] = self.try_utime(ctx.filename, ctx.data.info().get('last-modified', None))

            self._hook_progress({
                'downloaded_bytes': byte_counter,
                'total_bytes': byte_counter,
                'filename': ctx.filename,
                'status': 'finished',
                'elapsed': time.time() - ctx.start_time,
            })

            return True

        while count <= retries:
            await asyncio.sleep(0)
            try:
                establish_connection()
                return await download()
            except RetryDownload as e:
                count += 1
                if count <= retries:
                    self.report_retry(e.source_error, count, retries)
                continue
            except NextFragment:
                continue
            except SucceedDownload:
                return True

        self.report_error('giving up after %s retries' % retries)
        return False


class AsyYoutubeDL(YoutubeDL):
    def __init__(self, params=None, auto_init=True, tele_message=None):
        self._t_mess = tele_message
        super(AsyYoutubeDL, self).__init__(params=params, auto_init=auto_init)

    async def process_video_result(self, info_dict, download=True):
        assert info_dict.get('_type', 'video') == 'video'

        if 'id' not in info_dict:
            raise ExtractorError('Missing "id" field in extractor result')
        if 'title' not in info_dict:
            raise ExtractorError('Missing "title" field in extractor result')

        def report_force_conversion(field, field_not, conversion):
            self.report_warning(
                '"%s" field is not %s - forcing %s conversion, there is an error in extractor'
                % (field, field_not, conversion))

        def sanitize_string_field(info, string_field):
            field = info.get(string_field)
            if field is None or isinstance(field, compat_str):
                return
            report_force_conversion(string_field, 'a string', 'string')
            info[string_field] = compat_str(field)

        def sanitize_numeric_fields(info):
            for numeric_field in self._NUMERIC_FIELDS:
                field = info.get(numeric_field)
                if field is None or isinstance(field, compat_numeric_types):
                    continue
                report_force_conversion(numeric_field, 'numeric', 'int')
                info[numeric_field] = int_or_none(field)

        sanitize_string_field(info_dict, 'id')
        sanitize_numeric_fields(info_dict)

        if 'playlist' not in info_dict:
            # It isn't part of a playlist
            info_dict['playlist'] = None
            info_dict['playlist_index'] = None

        thumbnails = info_dict.get('thumbnails')
        if thumbnails is None:
            thumbnail = info_dict.get('thumbnail')
            if thumbnail:
                info_dict['thumbnails'] = thumbnails = [{'url': thumbnail}]
        if thumbnails:
            thumbnails.sort(key=lambda t: (
                t.get('preference') if t.get('preference') is not None else -1,
                t.get('width') if t.get('width') is not None else -1,
                t.get('height') if t.get('height') is not None else -1,
                t.get('id') if t.get('id') is not None else '', t.get('url')))
            for i, t in enumerate(thumbnails):
                t['url'] = sanitize_url(t['url'])
                if t.get('width') and t.get('height'):
                    t['resolution'] = '%dx%d' % (t['width'], t['height'])
                if t.get('id') is None:
                    t['id'] = '%d' % i

        if self.params.get('list_thumbnails'):
            self.list_thumbnails(info_dict)
            return

        thumbnail = info_dict.get('thumbnail')
        if thumbnail:
            info_dict['thumbnail'] = sanitize_url(thumbnail)
        elif thumbnails:
            info_dict['thumbnail'] = thumbnails[-1]['url']

        if 'display_id' not in info_dict and 'id' in info_dict:
            info_dict['display_id'] = info_dict['id']

        for ts_key, date_key in (
                ('timestamp', 'upload_date'),
                ('release_timestamp', 'release_date'),
        ):
            if info_dict.get(date_key) is None and info_dict.get(ts_key) is not None:
                # Working around out-of-range timestamp values (e.g. negative ones on Windows,
                # see http://bugs.python.org/issue1646728)
                try:
                    upload_date = datetime.datetime.utcfromtimestamp(info_dict[ts_key])
                    info_dict[date_key] = upload_date.strftime('%Y%m%d')
                except (ValueError, OverflowError, OSError):
                    pass

        # Auto generate title fields corresponding to the *_number fields when missing
        # in order to always have clean titles. This is very common for TV series.
        for field in ('chapter', 'season', 'episode'):
            if info_dict.get('%s_number' % field) is not None and not info_dict.get(field):
                info_dict[field] = '%s %d' % (field.capitalize(), info_dict['%s_number' % field])

        for cc_kind in ('subtitles', 'automatic_captions'):
            cc = info_dict.get(cc_kind)
            if cc:
                for _, subtitle in cc.items():
                    for subtitle_format in subtitle:
                        if subtitle_format.get('url'):
                            subtitle_format['url'] = sanitize_url(subtitle_format['url'])
                        if subtitle_format.get('ext') is None:
                            subtitle_format['ext'] = determine_ext(subtitle_format['url']).lower()

        automatic_captions = info_dict.get('automatic_captions')
        subtitles = info_dict.get('subtitles')

        if self.params.get('listsubtitles', False):
            if 'automatic_captions' in info_dict:
                self.list_subtitles(
                    info_dict['id'], automatic_captions, 'automatic captions')
            self.list_subtitles(info_dict['id'], subtitles, 'subtitles')
            return

        info_dict['requested_subtitles'] = self.process_subtitles(
            info_dict['id'], subtitles, automatic_captions)

        # We now pick which formats have to be downloaded
        if info_dict.get('formats') is None:
            # There's only one format available
            formats = [info_dict]
        else:
            formats = info_dict['formats']

        if not formats:
            raise ExtractorError('No video formats found!')

        def is_wellformed(f):
            url = f.get('url')
            if not url:
                self.report_warning(
                    '"url" field is missing or empty - skipping format, '
                    'there is an error in extractor')
                return False
            if isinstance(url, bytes):
                sanitize_string_field(f, 'url')
            return True

        # Filter out malformed formats for better extraction robustness
        formats = list(filter(is_wellformed, formats))

        formats_dict = {}

        # We check that all the formats have the format and format_id fields
        for i, format in enumerate(formats):
            sanitize_string_field(format, 'format_id')
            sanitize_numeric_fields(format)
            format['url'] = sanitize_url(format['url'])
            if not format.get('format_id'):
                format['format_id'] = compat_str(i)
            else:
                # Sanitize format_id from characters used in format selector expression
                format['format_id'] = re.sub(r'[\s,/+\[\]()]', '_', format['format_id'])
            format_id = format['format_id']
            if format_id not in formats_dict:
                formats_dict[format_id] = []
            formats_dict[format_id].append(format)

        # Make sure all formats have unique format_id
        for format_id, ambiguous_formats in formats_dict.items():
            if len(ambiguous_formats) > 1:
                for i, format in enumerate(ambiguous_formats):
                    format['format_id'] = '%s-%d' % (format_id, i)

        for i, format in enumerate(formats):
            if format.get('format') is None:
                format['format'] = '{id} - {res}{note}'.format(
                    id=format['format_id'],
                    res=self.format_resolution(format),
                    note=' ({0})'.format(format['format_note']) if format.get('format_note') is not None else '',
                )
            # Automatically determine file extension if missing
            if format.get('ext') is None:
                format['ext'] = determine_ext(format['url']).lower()
            # Automatically determine protocol if missing (useful for format
            # selection purposes)
            if format.get('protocol') is None:
                format['protocol'] = determine_protocol(format)
            # Add HTTP headers, so that external programs can use them from the
            # json output
            full_format_info = info_dict.copy()
            full_format_info.update(format)
            format['http_headers'] = self._calc_headers(full_format_info)
        # Remove private housekeeping stuff
        if '__x_forwarded_for_ip' in info_dict:
            del info_dict['__x_forwarded_for_ip']

        # TODO Central sorting goes here

        if formats[0] is not info_dict:
            # only set the 'formats' fields if the original info_dict list them
            # otherwise we end up with a circular reference, the first (and unique)
            # element in the 'formats' field in info_dict is info_dict itself,
            # which can't be exported to json
            info_dict['formats'] = formats
        if self.params.get('listformats'):
            self.list_formats(info_dict)
            return

        req_format = self.params.get('format')
        if req_format is None:
            req_format = self._default_format_spec(info_dict, download=download)
            if self.params.get('verbose'):
                self._write_string('[debug] Default format spec: %s\n' % req_format)

        format_selector = self.build_format_selector(req_format)

        # While in format selection we may need to have an access to the original
        # format set in order to calculate some metrics or do some processing.
        # For now we need to be able to guess whether original formats provided
        # by extractor are incomplete or not (i.e. whether extractor provides only
        # video-only or audio-only formats) for proper formats selection for
        # extractors with such incomplete formats (see
        # https://github.com/ytdl-org/youtube-dl/pull/5556).
        # Since formats may be filtered during format selection and may not match
        # the original formats the results may be incorrect. Thus original formats
        # or pre-calculated metrics should be passed to format selection routines
        # as well.
        # We will pass a context object containing all necessary additional data
        # instead of just formats.
        # This fixes incorrect format selection issue (see
        # https://github.com/ytdl-org/youtube-dl/issues/10083).
        incomplete_formats = (
            # All formats are video-only or
                all(f.get('vcodec') != 'none' and f.get('acodec') == 'none' for f in formats)
                # all formats are audio-only
                or all(f.get('vcodec') == 'none' and f.get('acodec') != 'none' for f in formats))

        ctx = {
            'formats': formats,
            'incomplete_formats': incomplete_formats,
        }

        formats_to_download = list(format_selector(ctx))
        if not formats_to_download:
            raise ExtractorError('requested format not available',
                                 expected=True)

        if download:
            if len(formats_to_download) > 1:
                self.to_screen(
                    '[info] %s: downloading video in %s formats' % (info_dict['id'], len(formats_to_download)))
            for format in formats_to_download:
                new_info = dict(info_dict)
                new_info.update(format)
                await self.process_info(new_info)
        # We update the info dict with the best quality format (backwards compatibility)
        info_dict.update(formats_to_download[-1])
        return info_dict

    async def process_info(self, info_dict):
        """Process a single resolved IE result."""

        assert info_dict.get('_type', 'video') == 'video'

        max_downloads = self.params.get('max_downloads')
        if max_downloads is not None:
            if self._num_downloads >= int(max_downloads):
                raise MaxDownloadsReached()

        # TODO: backward compatibility, to be removed
        info_dict['fulltitle'] = info_dict['title']

        if 'format' not in info_dict:
            info_dict['format'] = info_dict['ext']

        reason = self._match_entry(info_dict, incomplete=False)
        if reason is not None:
            self.to_screen('[download] ' + reason)
            return

        self._num_downloads += 1

        info_dict['_filename'] = filename = self.prepare_filename(info_dict)

        # Forced printings
        self.__forced_printings(info_dict, filename, incomplete=False)

        # Do nothing else if in simulate mode
        if self.params.get('simulate', False):
            return

        if filename is None:
            return

        def ensure_dir_exists(path):
            try:
                dn = os.path.dirname(path)
                if dn and not os.path.exists(dn):
                    os.makedirs(dn)
                return True
            except (OSError, IOError) as err:
                if isinstance(err, OSError) and err.errno == errno.EEXIST:
                    return True
                self.report_error('unable to create directory ' + error_to_compat_str(err))
                return False

        if not ensure_dir_exists(sanitize_path(encodeFilename(filename))):
            return

        if self.params.get('writedescription', False):
            descfn = replace_extension(filename, 'description', info_dict.get('ext'))
            if self.params.get('nooverwrites', False) and os.path.exists(encodeFilename(descfn)):
                self.to_screen('[info] Video description is already present')
            elif info_dict.get('description') is None:
                self.report_warning('There\'s no description to write.')
            else:
                try:
                    self.to_screen('[info] Writing video description to: ' + descfn)
                    with io.open(encodeFilename(descfn), 'w', encoding='utf-8') as descfile:
                        descfile.write(info_dict['description'])
                except (OSError, IOError):
                    self.report_error('Cannot write description file ' + descfn)
                    return

        if self.params.get('writeannotations', False):
            annofn = replace_extension(filename, 'annotations.xml', info_dict.get('ext'))
            if self.params.get('nooverwrites', False) and os.path.exists(encodeFilename(annofn)):
                self.to_screen('[info] Video annotations are already present')
            elif not info_dict.get('annotations'):
                self.report_warning('There are no annotations to write.')
            else:
                try:
                    self.to_screen('[info] Writing video annotations to: ' + annofn)
                    with io.open(encodeFilename(annofn), 'w', encoding='utf-8') as annofile:
                        annofile.write(info_dict['annotations'])
                except (KeyError, TypeError):
                    self.report_warning('There are no annotations to write.')
                except (OSError, IOError):
                    self.report_error('Cannot write annotations file: ' + annofn)
                    return

        subtitles_are_requested = any([self.params.get('writesubtitles', False),
                                       self.params.get('writeautomaticsub')])

        if subtitles_are_requested and info_dict.get('requested_subtitles'):
            # subtitles download errors are already managed as troubles in relevant IE
            # that way it will silently go on when used with unsupporting IE
            subtitles = info_dict['requested_subtitles']
            ie = self.get_info_extractor(info_dict['extractor_key'])
            for sub_lang, sub_info in subtitles.items():
                sub_format = sub_info['ext']
                sub_filename = subtitles_filename(filename, sub_lang, sub_format, info_dict.get('ext'))
                if self.params.get('nooverwrites', False) and os.path.exists(encodeFilename(sub_filename)):
                    self.to_screen('[info] Video subtitle %s.%s is already present' % (sub_lang, sub_format))
                else:
                    self.to_screen('[info] Writing video subtitles to: ' + sub_filename)
                    if sub_info.get('data') is not None:
                        try:
                            # Use newline='' to prevent conversion of newline characters
                            # See https://github.com/ytdl-org/youtube-dl/issues/10268
                            with io.open(encodeFilename(sub_filename), 'w', encoding='utf-8', newline='') as subfile:
                                subfile.write(sub_info['data'])
                        except (OSError, IOError):
                            self.report_error('Cannot write subtitles file ' + sub_filename)
                            return
                    else:
                        try:
                            sub_data = ie._request_webpage(
                                sub_info['url'], info_dict['id'], note=False).read()
                            with io.open(encodeFilename(sub_filename), 'wb') as subfile:
                                subfile.write(sub_data)
                        except (ExtractorError, IOError, OSError, ValueError) as err:
                            self.report_warning('Unable to download subtitle for "%s": %s' %
                                                (sub_lang, error_to_compat_str(err)))
                            continue

        if self.params.get('writeinfojson', False):
            infofn = replace_extension(filename, 'info.json', info_dict.get('ext'))
            if self.params.get('nooverwrites', False) and os.path.exists(encodeFilename(infofn)):
                self.to_screen('[info] Video description metadata is already present')
            else:
                self.to_screen('[info] Writing video description metadata as JSON to: ' + infofn)
                try:
                    write_json_file(self.filter_requested_info(info_dict), infofn)
                except (OSError, IOError):
                    self.report_error('Cannot write metadata to JSON file ' + infofn)
                    return

        self._write_thumbnails(info_dict, filename)

        if not self.params.get('skip_download', False):
            try:
                async def dl(name, info):
                    fd = get_suitable_downloader1(info, self.params)(self, self.params)
                    for ph in self._progress_hooks:
                        fd.add_progress_hook(ph)
                    if self.params.get('verbose'):
                        self.to_screen('[debug] Invoking downloader on %r' % info.get('url'))
                    return await fd.download(name, info)

                if info_dict.get('requested_formats') is not None:
                    downloaded = []
                    success = True
                    merger = FFmpegMergerPP(self)
                    if not merger.available:
                        postprocessors = []
                        self.report_warning('You have requested multiple '
                                            'formats but ffmpeg or avconv are not installed.'
                                            ' The formats won\'t be merged.')
                    else:
                        postprocessors = [merger]

                    def compatible_formats(formats):
                        video, audio = formats
                        # Check extension
                        video_ext, audio_ext = video.get('ext'), audio.get('ext')
                        if video_ext and audio_ext:
                            COMPATIBLE_EXTS = (
                                ('mp3', 'mp4', 'm4a', 'm4p', 'm4b', 'm4r', 'm4v', 'ismv', 'isma'),
                                ('webm')
                            )
                            for exts in COMPATIBLE_EXTS:
                                if video_ext in exts and audio_ext in exts:
                                    return True
                        # TODO: Check acodec/vcodec
                        return False

                    filename_real_ext = os.path.splitext(filename)[1][1:]
                    filename_wo_ext = (
                        os.path.splitext(filename)[0]
                        if filename_real_ext == info_dict['ext']
                        else filename)
                    requested_formats = info_dict['requested_formats']
                    if self.params.get('merge_output_format') is None and not compatible_formats(requested_formats):
                        info_dict['ext'] = 'mkv'
                        self.report_warning(
                            'Requested formats are incompatible for merge and will be merged into mkv.')
                    # Ensure filename always has a correct extension for successful merge
                    filename = '%s.%s' % (filename_wo_ext, info_dict['ext'])
                    if os.path.exists(encodeFilename(filename)):
                        self.to_screen(
                            '[download] %s has already been downloaded and '
                            'merged' % filename)
                    else:
                        for f in requested_formats:
                            new_info = dict(info_dict)
                            new_info.update(f)
                            fname = prepend_extension(
                                self.prepare_filename(new_info),
                                'f%s' % f['format_id'], new_info['ext'])
                            if not ensure_dir_exists(fname):
                                return
                            downloaded.append(fname)
                            partial_success = await dl(fname, new_info)
                            success = success and partial_success
                        info_dict['__postprocessors'] = postprocessors
                        info_dict['__files_to_merge'] = downloaded
                else:
                    # Just a single file
                    success = await dl(filename, info_dict)
            except (compat_urllib_error.URLError, compat_http_client.HTTPException, socket.error) as err:
                self.report_error('unable to download video data: %s' % error_to_compat_str(err))
                return
            except (OSError, IOError) as err:
                raise UnavailableVideoError(err)
            except (ContentTooShortError,) as err:
                self.report_error(
                    'content too short (expected %s bytes and served %s)' % (err.expected, err.downloaded))
                return

            if success and filename != '-':
                # Fixup content
                fixup_policy = self.params.get('fixup')
                if fixup_policy is None:
                    fixup_policy = 'detect_or_warn'

                INSTALL_FFMPEG_MESSAGE = 'Install ffmpeg or avconv to fix this automatically.'

                stretched_ratio = info_dict.get('stretched_ratio')
                if stretched_ratio is not None and stretched_ratio != 1:
                    if fixup_policy == 'warn':
                        self.report_warning('%s: Non-uniform pixel ratio (%s)' % (
                            info_dict['id'], stretched_ratio))
                    elif fixup_policy == 'detect_or_warn':
                        stretched_pp = FFmpegFixupStretchedPP(self)
                        if stretched_pp.available:
                            info_dict.setdefault('__postprocessors', [])
                            info_dict['__postprocessors'].append(stretched_pp)
                        else:
                            self.report_warning(
                                '%s: Non-uniform pixel ratio (%s). %s'
                                % (info_dict['id'], stretched_ratio, INSTALL_FFMPEG_MESSAGE))
                    else:
                        assert fixup_policy in ('ignore', 'never')

                if (info_dict.get('requested_formats') is None
                        and info_dict.get('container') == 'm4a_dash'):
                    if fixup_policy == 'warn':
                        self.report_warning(
                            '%s: writing DASH m4a. '
                            'Only some players support this container.'
                            % info_dict['id'])
                    elif fixup_policy == 'detect_or_warn':
                        fixup_pp = FFmpegFixupM4aPP(self)
                        if fixup_pp.available:
                            info_dict.setdefault('__postprocessors', [])
                            info_dict['__postprocessors'].append(fixup_pp)
                        else:
                            self.report_warning(
                                '%s: writing DASH m4a. '
                                'Only some players support this container. %s'
                                % (info_dict['id'], INSTALL_FFMPEG_MESSAGE))
                    else:
                        assert fixup_policy in ('ignore', 'never')

                if (info_dict.get('protocol') == 'm3u8_native'
                        or info_dict.get('protocol') == 'm3u8'
                        and self.params.get('hls_prefer_native')):
                    if fixup_policy == 'warn':
                        self.report_warning('%s: malformed AAC bitstream detected.' % (
                            info_dict['id']))
                    elif fixup_policy == 'detect_or_warn':
                        fixup_pp = FFmpegFixupM3u8PP(self)
                        if fixup_pp.available:
                            info_dict.setdefault('__postprocessors', [])
                            info_dict['__postprocessors'].append(fixup_pp)
                        else:
                            self.report_warning(
                                '%s: malformed AAC bitstream detected. %s'
                                % (info_dict['id'], INSTALL_FFMPEG_MESSAGE))
                    else:
                        assert fixup_policy in ('ignore', 'never')

                try:
                    self.post_process(filename, info_dict)
                except (PostProcessingError) as err:
                    self.report_error('postprocessing: %s' % str(err))
                    return
                self.record_download_archive(info_dict)

    def to_screen(self, message, skip_eol=False):
        """Print message to stdout if not in quiet mode."""

        mess = html.escape(message)
        strAPI = fr'https://api.telegram.org/bot{config.BOT_TOKEN}/editMessageText?chat_id={self._t_mess.chat.id}&text={mess}&message_id={self._t_mess.message_id}'
        res = requests.get(strAPI)
        return self.to_stdout(message, skip_eol, check_quiet=True)

    async def download(self, url_list):
        """Download a given list of URLs."""
        outtmpl = self.params.get('outtmpl', DEFAULT_OUTTMPL)
        if (len(url_list) > 1
                and outtmpl != '-'
                and '%' not in outtmpl
                and self.params.get('max_downloads') != 1):
            raise SameFileError(outtmpl)

        for url in url_list:
            try:
                # It also downloads the videos
                res = await self.extract_info(
                    url, force_generic_extractor=self.params.get('force_generic_extractor', False))
            except UnavailableVideoError:
                self.report_error('unable to download video')
            except MaxDownloadsReached:
                self.to_screen('[info] Maximum number of downloaded files reached.')
                raise
            else:
                if self.params.get('dump_single_json', False):
                    self.to_stdout(json.dumps(res))

        return self._download_retcode

    async def process_ie_result(self, ie_result, download=True, extra_info={}):
        """
        Take the result of the ie(may be modified) and resolve all unresolved
        references (URLs, playlist items).

        It will also download the videos if 'download'.
        Returns the resolved ie_result.
        """
        result_type = ie_result.get('_type', 'video')

        if result_type in ('url', 'url_transparent'):
            ie_result['url'] = sanitize_url(ie_result['url'])
            extract_flat = self.params.get('extract_flat', False)
            if ((extract_flat == 'in_playlist' and 'playlist' in extra_info)
                    or extract_flat is True):
                self.__forced_printings(
                    ie_result, self.prepare_filename(ie_result),
                    incomplete=True)
                return ie_result

        if result_type == 'video':
            self.add_extra_info(ie_result, extra_info)
            return await self.process_video_result(ie_result, download=download)
        elif result_type == 'url':
            # We have to add extra_info to the results because it may be
            # contained in a playlist
            return self.extract_info(ie_result['url'],
                                     download,
                                     ie_key=ie_result.get('ie_key'),
                                     extra_info=extra_info)
        elif result_type == 'url_transparent':
            # Use the information from the embedding page
            info = self.extract_info(
                ie_result['url'], ie_key=ie_result.get('ie_key'),
                extra_info=extra_info, download=False, process=False)

            # extract_info may return None when ignoreerrors is enabled and
            # extraction failed with an error, don't crash and return early
            # in this case
            if not info:
                return info

            force_properties = dict(
                (k, v) for k, v in ie_result.items() if v is not None)
            for f in ('_type', 'url', 'id', 'extractor', 'extractor_key', 'ie_key'):
                if f in force_properties:
                    del force_properties[f]
            new_result = info.copy()
            new_result.update(force_properties)

            # Extracted info may not be a video result (i.e.
            # info.get('_type', 'video') != video) but rather an url or
            # url_transparent. In such cases outer metadata (from ie_result)
            # should be propagated to inner one (info). For this to happen
            # _type of info should be overridden with url_transparent. This
            # fixes issue from https://github.com/ytdl-org/youtube-dl/pull/11163.
            if new_result.get('_type') == 'url':
                new_result['_type'] = 'url_transparent'

            return await self.process_ie_result(
                new_result, download=download, extra_info=extra_info)
        elif result_type in ('playlist', 'multi_video'):
            # Protect from infinite recursion due to recursively nested playlists
            # (see https://github.com/ytdl-org/youtube-dl/issues/27833)
            webpage_url = ie_result['webpage_url']
            if webpage_url in self._playlist_urls:
                self.to_screen(
                    '[download] Skipping already downloaded playlist: %s'
                    % ie_result.get('title') or ie_result.get('id'))
                return

            self._playlist_level += 1
            self._playlist_urls.add(webpage_url)
            try:
                return self.__process_playlist(ie_result, download)
            finally:
                self._playlist_level -= 1
                if not self._playlist_level:
                    self._playlist_urls.clear()
        elif result_type == 'compat_list':
            self.report_warning(
                'Extractor %s returned a compat_list result. '
                'It needs to be updated.' % ie_result.get('extractor'))

            def _fixup(r):
                self.add_extra_info(
                    r,
                    {
                        'extractor': ie_result['extractor'],
                        'webpage_url': ie_result['webpage_url'],
                        'webpage_url_basename': url_basename(ie_result['webpage_url']),
                        'extractor_key': ie_result['extractor_key'],
                    }
                )
                return r

            ie_result['entries'] = [
                await self.process_ie_result(_fixup(r), download, extra_info)
                for r in ie_result['entries']
            ]
            return ie_result
        else:
            raise Exception('Invalid result type: %s' % result_type)

    async def download_with_info_file(self, info_filename):
        with contextlib.closing(fileinput.FileInput(
                [info_filename], mode='r',
                openhook=fileinput.hook_encoded('utf-8'))) as f:
            # FileInput doesn't have a read method, we can't call json.load
            info = self.filter_requested_info(json.loads('\n'.join(f)))
        try:
            await self.process_ie_result(info, download=True)
        except DownloadError:
            webpage_url = info.get('webpage_url')
            if webpage_url is not None:
                self.report_warning('The info failed to download, trying with "%s"' % webpage_url)
                return self.download([webpage_url])
            else:
                raise
        return self._download_retcode

    async def extract_info(self, url, download=True, ie_key=None, extra_info={},
                           process=True, force_generic_extractor=False):
        """
        Return a list with a dictionary for each video extracted.

        Arguments:
        url -- URL to extract

        Keyword arguments:
        download -- whether to download videos during extraction
        ie_key -- extractor key hint
        extra_info -- dictionary containing the extra values to add to each result
        process -- whether to resolve all unresolved references (URLs, playlist items),
            must be True for download to work.
        force_generic_extractor -- force using the generic extractor
        """

        if not ie_key and force_generic_extractor:
            ie_key = 'Generic'

        if ie_key:
            ies = [self.get_info_extractor(ie_key)]
        else:
            ies = self._ies

        for ie in ies:
            if not ie.suitable(url):
                await asyncio.sleep(0)
                continue

            ie = self.get_info_extractor(ie.ie_key())
            if not ie.working():
                self.report_warning('The program functionality for this site has been marked as broken, '
                                    'and will probably not work.')

            return await self.__extract_info(url, ie, download, extra_info, process)
        else:
            self.report_error('no suitable InfoExtractor for URL %s' % url)

    def __handle_extraction_exceptions(func):
        def wrapper(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except GeoRestrictedError as e:
                msg = e.msg
                if e.countries:
                    msg += '\nThis video is available in %s.' % ', '.join(
                        map(ISO3166Utils.short2full, e.countries))
                msg += '\nYou might want to use a VPN or a proxy server (with --proxy) to workaround.'
                self.report_error(msg)
            except ExtractorError as e:  # An error we somewhat expected
                self.report_error(compat_str(e), e.format_traceback())
            except MaxDownloadsReached:
                raise
            except Exception as e:
                if self.params.get('ignoreerrors', False):
                    self.report_error(error_to_compat_str(e), tb=encode_compat_str(traceback.format_exc()))
                else:
                    raise

        return wrapper

    @__handle_extraction_exceptions
    async def __extract_info(self, url, ie, download, extra_info, process):
        ie_result = ie.extract(url)
        if ie_result is None:  # Finished already (backwards compatibility; listformats and friends should be moved here)
            return
        if isinstance(ie_result, list):
            # Backwards compatibility: old IE result format
            ie_result = {
                '_type': 'compat_list',
                'entries': ie_result,
            }
        self.add_default_extra_info(ie_result, ie, url)
        if process:
            return await self.process_ie_result(ie_result, download, extra_info)
        else:
            return ie_result

    def __forced_printings(self, info_dict, filename, incomplete):
        def print_mandatory(field):
            if (self.params.get('force%s' % field, False)
                    and (not incomplete or info_dict.get(field) is not None)):
                self.to_stdout(info_dict[field])

        def print_optional(field):
            if (self.params.get('force%s' % field, False)
                    and info_dict.get(field) is not None):
                self.to_stdout(info_dict[field])

        print_mandatory('title')
        print_mandatory('id')
        if self.params.get('forceurl', False) and not incomplete:
            if info_dict.get('requested_formats') is not None:
                for f in info_dict['requested_formats']:
                    self.to_stdout(f['url'] + f.get('play_path', ''))
            else:
                # For RTMP URLs, also include the playpath
                self.to_stdout(info_dict['url'] + info_dict.get('play_path', ''))
        print_optional('thumbnail')
        print_optional('description')
        if self.params.get('forcefilename', False) and filename is not None:
            self.to_stdout(filename)
        if self.params.get('forceduration', False) and info_dict.get('duration') is not None:
            self.to_stdout(formatSeconds(info_dict['duration']))
        print_mandatory('format')
        if self.params.get('forcejson', False):
            self.to_stdout(json.dumps(info_dict))
