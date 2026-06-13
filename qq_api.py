"""QQ音乐API模块

提供QQ音乐相关API接口封装，包括：
- 歌曲URL获取（支持21种音质）
- 歌曲详情获取
- 歌词获取（base64解码）
- 搜索功能
- 歌单和专辑详情（待实现）
"""

import json
import base64
import logging
import urllib.parse
from typing import Dict, List, Optional, Any

import requests


class APIException(Exception):
    """QQ音乐API异常类"""
    pass


class QQAPIConstants:
    """QQ音乐API相关常量"""
    BASE_URL = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
    SONG_URL = 'https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg'
    SEARCH_URL = 'https://c.y.qq.com/soso/fcgi-bin/client_search_cp'
    PLAYLIST_URL = 'https://c.y.qq.com/v8/fcg-bin/fcg_v8_playlist_cp.fcg'
    USER_AGENT = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'

    FILE_CONFIG = {
        '128':      {'s': 'M500', 'e': '.mp3',  'bitrate': '128kbps'},
        '320':      {'s': 'M800', 'e': '.mp3',  'bitrate': '320kbps'},
        'flac':     {'s': 'F000', 'e': '.flac', 'bitrate': 'FLAC'},
        'master':   {'s': 'AI00', 'e': '.flac', 'bitrate': '臻品母带'},
        'atmos_2':  {'s': 'Q000', 'e': '.flac', 'bitrate': '全景声2.0'},
        'atmos_51': {'s': 'Q001', 'e': '.flac', 'bitrate': '全景声5.1'},
        'ogg_640':  {'s': 'O801', 'e': '.ogg',  'bitrate': '640kbps'},
        'ogg_320':  {'s': 'O800', 'e': '.ogg',  'bitrate': '320kbps'},
        'ogg_192':  {'s': 'O600', 'e': '.ogg',  'bitrate': '192kbps'},
        'ogg_96':   {'s': 'O400', 'e': '.ogg',  'bitrate': '96kbps'},
        'aac_320':  {'s': 'C800', 'e': '.m4a',  'bitrate': '320kbps'},
        'aac_256':  {'s': 'C700', 'e': '.m4a',  'bitrate': '256kbps'},
        'aac_192':  {'s': 'C600', 'e': '.m4a',  'bitrate': '192kbps'},
        'aac_128':  {'s': 'C500', 'e': '.m4a',  'bitrate': '128kbps'},
        'aac_96':   {'s': 'C400', 'e': '.m4a',  'bitrate': '96kbps'},
        'aac_64':   {'s': 'C300', 'e': '.m4a',  'bitrate': '64kbps'},
        'aac_48':   {'s': 'C200', 'e': '.m4a',  'bitrate': '48kbps'},
        'aac_24':   {'s': 'C100', 'e': '.m4a',  'bitrate': '24kbps'},
        'ape':      {'s': 'A000', 'e': '.ape',  'bitrate': 'APE'},
        'dts':      {'s': 'D000', 'e': '.dts',  'bitrate': 'DTS'},
        'dolby':    {'s': 'RS01', 'e': '.flac', 'bitrate': '杜比全景声'},
        'hires':    {'s': 'SQ00', 'e': '.flac', 'bitrate': 'Hi-Res'},
    }

    QUALITY_DISPLAY = {
        '128': '标准', '320': 'HQ高品质', 'flac': 'SQ无损品质',
        'master': '臻品母带3.0', 'atmos_2': '臻品全景声2.0', 'atmos_51': '臻品音质2.0',
        'ogg_640': 'OGG 640kbps', 'ogg_320': 'OGG高品质', 'ogg_192': 'OGG标准', 'ogg_96': 'OGG 96kbps',
        'aac_320': 'AAC 320kbps', 'aac_256': 'AAC 256kbps', 'aac_192': 'AAC高品质',
        'aac_128': 'AAC 128kbps', 'aac_96': 'AAC标准', 'aac_64': 'AAC 64kbps',
        'aac_48': 'AAC 48kbps', 'aac_24': 'AAC 24kbps',
        'ape': 'APE无损', 'dts': 'DTS', 'dolby': '杜比全景声', 'hires': 'Hi-Res',
    }


class QQMusicAPI:
    """QQ音乐API主类"""

    def __init__(self):
        self.logger = logging.getLogger('qq_api')
        self.cookies: Dict[str, str] = {}
        self.guid = '10000'
        self.uin = '0'

    def set_cookies(self, cookie_str: str):
        if cookie_str:
            for item in cookie_str.split(';'):
                item = item.strip()
                if '=' in item:
                    key, value = item.split('=', 1)
                    if key and value:
                        self.cookies[key] = value

    def _extract_songmid(self, id_or_url: str) -> str:
        """从URL或ID中提取songmid"""
        if 'y.qq.com' in id_or_url:
            import re
            m = re.search(r'/songDetail/([^/?]+)', id_or_url)
            if m:
                return m.group(1)
            m = re.search(r'id=(\w+)', id_or_url)
            if m:
                return m.group(1)
        return str(id_or_url).strip()

    def _request(self, url: str, post_data=None) -> str:
        headers = {
            'User-Agent': QQAPIConstants.USER_AGENT,
        }
        if post_data:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'
        if self.cookies:
            cookie_str = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
            headers['Cookie'] = cookie_str

        try:
            if post_data:
                resp = requests.post(url, data=post_data, headers=headers, timeout=30)
            else:
                resp = requests.get(url, headers=headers, timeout=30)
            resp.encoding = 'utf-8'
            return resp.text
        except requests.RequestException as e:
            raise APIException(f"HTTP请求失败: {e}")

    def get_song_url(self, songmid: str, quality: str) -> Dict[str, Any]:
        """获取歌曲播放URL"""
        if quality not in QQAPIConstants.FILE_CONFIG:
            raise APIException(f"不支持的音质: {quality}")

        file_info = QQAPIConstants.FILE_CONFIG[quality]
        file_name = f"{file_info['s']}{songmid}{songmid}{file_info['e']}"

        payload = {
            'req_1': {
                'module': 'vkey.GetVkeyServer',
                'method': 'CgiGetVkey',
                'param': {
                    'filename': [file_name],
                    'guid': self.guid,
                    'songmid': [songmid],
                    'songtype': [0],
                    'uin': self.uin,
                    'loginflag': 1,
                    'platform': '20'
                }
            },
            'loginUin': self.uin,
            'comm': {
                'uin': self.uin,
                'format': 'json',
                'ct': 24,
                'cv': 0
            }
        }

        try:
            response_text = self._request(QQAPIConstants.BASE_URL, json.dumps(payload))
            data = json.loads(response_text)
            purl = data.get('req_1', {}).get('data', {}).get('midurlinfo', [{}])[0].get('purl', '')

            if not purl:
                return None

            music_url = data['req_1']['data']['sip'][1] + purl
            return {
                'url': music_url.replace('http://', 'https://'),
                'size': 0,
                'br': file_info['bitrate'],
                'type': file_info['e'].lstrip('.'),
                'level': quality,
            }
        except Exception as e:
            self.logger.error(f"获取QQ音乐URL失败: {e}")
            return None

    def get_song_detail(self, songmid: str, songid: int = 0) -> Dict[str, Any]:
        """获取歌曲详情"""
        if songid:
            req_data = {'songid': songid, 'platform': 'yqq', 'format': 'json'}
        else:
            req_data = {'songmid': songmid, 'platform': 'yqq', 'format': 'json'}

        try:
            response_text = self._request(QQAPIConstants.SONG_URL, urllib.parse.urlencode(req_data))
            data = json.loads(response_text)

            if data.get('code') != 0 or not data.get('data'):
                raise APIException(f"获取歌曲详情失败: {data.get('msg', '未知错误')}")

            song_info = data['data'][0]
            album_info = song_info.get('album', {})
            singers = song_info.get('singer', [])
            singer_names = '/'.join(s.get('name', '') for s in singers)
            album_mid = album_info.get('mid', '')
            pic_url = f'https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg?max_age=2592000' if album_mid else ''

            return {
                'songs': [{
                    'id': str(song_info.get('id', songid)),
                    'mid': song_info.get('mid', songmid),
                    'name': song_info.get('name', ''),
                    'ar': [{'name': s.get('name', '')} for s in singers],
                    'al': {
                        'name': album_info.get('name', ''),
                        'picUrl': pic_url,
                        'mid': album_mid,
                    },
                    'dt': (song_info.get('interval', 0) or 0) * 1000,
                    'no': 0,
                }],
                'code': 200,
            }
        except Exception as e:
            self.logger.error(f"获取QQ歌曲详情失败: {e}")
            raise APIException(f"获取歌曲详情失败: {e}")

    def get_lyric(self, songid: int, cookies: Dict[str, str] = None) -> Dict[str, Any]:
        """获取歌词（base64解码）"""
        payload = {
            'music.musichallSong.PlayLyricInfo.GetPlayLyricInfo': {
                'module': 'music.musichallSong.PlayLyricInfo',
                'method': 'GetPlayLyricInfo',
                'param': {
                    'trans_t': 0,
                    'roma_t': 0,
                    'crypt': 0,
                    'lrc_t': 0,
                    'interval': 208,
                    'trans': 1,
                    'ct': 6,
                    'songID': songid
                }
            },
            'comm': {
                'ct': '6',
                'cv': '80600'
            }
        }

        try:
            response_text = self._request(QQAPIConstants.BASE_URL, json.dumps(payload))
            data = json.loads(response_text)
            lyric_data = data.get('music.musichallSong.PlayLyricInfo.GetPlayLyricInfo', {}).get('data', {})

            lyric = ''
            tlyric = ''
            if lyric_data.get('lyric'):
                lyric = base64.b64decode(lyric_data['lyric']).decode('utf-8')
            if lyric_data.get('trans'):
                tlyric = base64.b64decode(lyric_data['trans']).decode('utf-8')

            return {
                'lrc': {'lyric': lyric},
                'tlyric': {'lyric': tlyric},
                'code': 200,
            }
        except Exception as e:
            self.logger.error(f"获取QQ歌词失败: {e}")
            return {'lrc': {'lyric': ''}, 'tlyric': {'lyric': ''}, 'code': 200}

    def search_music(self, keyword: str, cookies: Dict[str, str] = None,
                     limit: int = 30, search_type: int = 1, offset: int = 0) -> List[Dict[str, Any]]:
        """搜索音乐（待完善）"""
        # TODO: QQ Music search API requires additional research
        # The client_search_cp endpoint may need specific parameters and signing
        self.logger.warning("QQ音乐搜索API尚未完整实现")
        return []

    def get_playlist_detail(self, playlist_id: str, cookies: Dict[str, str] = None) -> Dict[str, Any]:
        """获取歌单详情（待完善）"""
        self.logger.warning("QQ音乐歌单API尚未完整实现")
        return None

    def get_album_detail(self, album_id: str, cookies: Dict[str, str] = None) -> Dict[str, Any]:
        """获取专辑详情（待完善）"""
        self.logger.warning("QQ音乐专辑API尚未完整实现")
        return None

    def get_quality_display_name(self, quality: str) -> str:
        return QQAPIConstants.QUALITY_DISPLAY.get(quality, quality)


_module_api = QQMusicAPI()

VALID_LEVELS = list(QQAPIConstants.FILE_CONFIG.keys())


def qq_url_v1(songmid: str, level: str, cookies: Dict[str, str]) -> Optional[Dict[str, Any]]:
    _module_api.set_cookies('; '.join(f'{k}={v}' for k, v in cookies.items()) if cookies else '')
    return _module_api.get_song_url(songmid, level)


def qq_name_v1(songmid: str, songid: int = 0) -> Dict[str, Any]:
    return _module_api.get_song_detail(songmid, songid)


def qq_lyric_v1(songid: int, cookies: Dict[str, str] = None) -> Dict[str, Any]:
    return _module_api.get_lyric(songid, cookies)


def qq_search_music(keyword: str, cookies: Dict[str, str] = None,
                    limit: int = 30, search_type: int = 1, offset: int = 0) -> List[Dict[str, Any]]:
    return _module_api.search_music(keyword, cookies, limit, search_type, offset)


def qq_playlist_detail(playlist_id: str, cookies: Dict[str, str] = None) -> Optional[Dict[str, Any]]:
    return _module_api.get_playlist_detail(playlist_id, cookies)


def qq_album_detail(album_id: str, cookies: Dict[str, str] = None) -> Optional[Dict[str, Any]]:
    return _module_api.get_album_detail(album_id, cookies)


if __name__ == "__main__":
    print("QQ音乐API模块")
    print("支持的功能:")
    print("- 歌曲URL获取 (21种音质)")
    print("- 歌曲详情获取")
    print("- 歌词获取 (base64解码)")
    print("- 搜索功能 (待完善)")
    print("- 歌单/专辑详情 (待完善)")
