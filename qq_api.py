"""
QQ音乐API Python版本
原作者: 苏晓晴
"""

import json
import base64
import re as _re
import random
import time
import urllib.parse
import urllib3
import requests
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


class QQMusic:
    def __init__(self):
        self.base_url = 'https://u.y.qq.com/cgi-bin/musicu.fcg'
        self.guid = '10000'
        self.uin = '0'
        self.cookies = {}
        self.headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/58.0.3029.110 Safari/537.3'
        }

        self.file_config = {
            '128': {'s': 'M500', 'e': '.mp3', 'bitrate': '128kbps'},
            '320': {'s': 'M800', 'e': '.mp3', 'bitrate': '320kbps'},
            'flac': {'s': 'F000', 'e': '.flac', 'bitrate': 'FLAC'},
            'master': {'s': 'AI00', 'e': '.flac', 'bitrate': 'Master'},
            'atmos_2': {'s': 'Q000', 'e': '.flac', 'bitrate': 'Atmos 2'},
            'atmos_51': {'s': 'Q001', 'e': '.flac', 'bitrate': 'Atmos 5.1'},
            'ogg_640': {'s': 'O801', 'e': '.ogg', 'bitrate': '640kbps'},
            'ogg_320': {'s': 'O800', 'e': '.ogg', 'bitrate': '320kbps'},
            'ogg_192': {'s': 'O600', 'e': '.ogg', 'bitrate': '192kbps'},
            'ogg_96': {'s': 'O400', 'e': '.ogg', 'bitrate': '96kbps'},
            'aac_320': {'s': 'C800', 'e': '.m4a', 'bitrate': '320kbps'},
            'aac_256': {'s': 'C700', 'e': '.m4a', 'bitrate': '256kbps'},
            'aac_192': {'s': 'C600', 'e': '.m4a', 'bitrate': '192kbps'},
            'aac_128': {'s': 'C500', 'e': '.m4a', 'bitrate': '128kbps'},
            'aac_96': {'s': 'C400', 'e': '.m4a', 'bitrate': '96kbps'},
            'aac_64': {'s': 'C300', 'e': '.m4a', 'bitrate': '64kbps'},
            'aac_48': {'s': 'C200', 'e': '.m4a', 'bitrate': '48kbps'},
            'aac_24': {'s': 'C100', 'e': '.m4a', 'bitrate': '24kbps'},
            'ape': {'s': 'A000', 'e': '.ape', 'bitrate': 'APE'},
            'dts': {'s': 'D000', 'e': '.dts', 'bitrate': 'DTS'},
            'dolby': {'s': 'RS01', 'e': '.flac', 'bitrate': 'Dolby Atmos'},
            'hires': {'s': 'SQ00', 'e': '.flac', 'bitrate': 'Hi-Res'}
        }

        self.song_url = 'https://c.y.qq.com/v8/fcg-bin/fcg_play_single_song.fcg'
        self.lyric_url = 'https://c.y.qq.com/lyric/fcgi-bin/fcg_query_lyric_new.fcg'

    def set_cookies(self, cookie_str):
        if cookie_str:
            for item in cookie_str.split('; '):
                if '=' in item:
                    key, value = item.split('=', 1)
                    if key and value:
                        self.cookies[key] = value

    def ids(self, url_str):
        if 'y.qq.com' in url_str:
            if '/songDetail/' in url_str:
                import re
                match = re.search(r'/songDetail/([^/?]+)', url_str)
                return match.group(1) if match else ''

            if 'id=' in url_str:
                import re
                match = re.search(r'id=(\w+)', url_str)
                return match.group(1) if match else ''
        return None

    def _request(self, request_url, post_fields=None):
        method = 'POST' if post_fields else 'GET'
        headers = dict(self.headers)
        if post_fields:
            headers['Content-Type'] = 'application/x-www-form-urlencoded'

        if self.cookies:
            cookie_str = '; '.join(f'{k}={v}' for k, v in self.cookies.items())
            headers['Cookie'] = cookie_str

        if method == 'POST':
            resp = requests.post(request_url, data=post_fields, headers=headers, timeout=30, verify=False)
        else:
            resp = requests.get(request_url, headers=headers, timeout=30, verify=False)

        resp.encoding = 'utf-8'
        return resp.text

    def get_music_url(self, songmid, file_type='flac'):
        if file_type not in self.file_config:
            raise ValueError(f"Invalid file_type. Choose from: {', '.join(self.file_config.keys())}")

        file_info = self.file_config[file_type]
        file_name = f"{file_info['s']}{songmid}{songmid}{file_info['e']}"

        req_data = {
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
            response = self._request(self.base_url, json.dumps(req_data))
            data = json.loads(response)
            purl = data.get('req_1', {}).get('data', {}).get('midurlinfo', [{}])[0].get('purl', '')

            if not purl:
                return None

            music_url = data['req_1']['data']['sip'][1] + purl
            return {
                'url': music_url.replace('http://', 'https://'),
                'bitrate': file_info['bitrate']
            }
        except Exception as e:
            print(f'获取音乐URL失败: {e}')
            return None

    def get_music_song(self, mid, sid):
        if not sid and mid and mid.isdigit():
            sid = int(mid)
            mid = ''
        if sid != 0:
            req_data = {'songid': sid, 'platform': 'yqq', 'format': 'json'}
        else:
            req_data = {'songmid': mid, 'platform': 'yqq', 'format': 'json'}

        try:
            response = self._request(self.song_url, urllib.parse.urlencode(req_data))
            data = json.loads(response)

            if data.get('data') and len(data['data']) > 0:
                song_info = data['data'][0]
                album_info = song_info.get('album', {})
                singers = song_info.get('singer', [])
                singer_names = ', '.join(s.get('name', '') for s in singers)

                album_mid = album_info.get('mid', '')
                img_url = f'https://y.qq.com/music/photo_new/T002R800x800M000{album_mid}.jpg?max_age=2592000' if album_mid else 'https://example.com/default-cover.jpg'

                interval = song_info.get('interval', 0)
                minutes = interval // 60
                seconds = interval % 60
                duration_str = f'{minutes}:{seconds:02d}'

                return {
                    'name': song_info.get('name', 'Unknown'),
                    'album': album_info.get('name', 'Unknown'),
                    'singer': singer_names,
                    'pic': img_url,
                    'mid': song_info.get('mid', mid),
                    'id': song_info.get('id', sid),
                    'interval': duration_str
                }
            else:
                return {'msg': '信息获取错误/歌曲不存在'}
        except Exception as e:
            print(f'获取歌曲信息失败: {e}')
            return {'msg': '信息获取错误/歌曲不存在'}

    def get_music_lyric_new(self, songid):
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
            response = self._request(self.base_url, json.dumps(payload))
            data = json.loads(response)
            lyric_data = data.get('music.musichallSong.PlayLyricInfo.GetPlayLyricInfo', {}).get('data', {})

            lyric = ''
            tylyric = ''
            if lyric_data.get('lyric'):
                lyric = base64.b64decode(lyric_data['lyric']).decode('utf-8')
            if lyric_data.get('trans'):
                tylyric = base64.b64decode(lyric_data['trans']).decode('utf-8')

            return {'lyric': lyric, 'tylyric': tylyric}
        except Exception as e:
            print(f'获取歌词失败: {e}')
            return {'error': '无法获取歌词'}

    def search_music(self, keyword, limit=30, page=1, search_type=0):
        """搜索音乐
        search_type: 0=歌曲 7=歌词 8=专辑 12=MV
        """
        url = 'https://c.y.qq.com/soso/fcgi-bin/client_search_cp'
        params = {
            'w': keyword,
            'p': page,
            'n': limit,
            'format': 'json',
            't': search_type,
        }
        try:
            resp = requests.get(url, params=params, headers=self.headers, timeout=15, verify=False)
            resp.encoding = 'utf-8'
            return resp.json()
        except Exception as e:
            print(f'QQ搜索失败: {e}')
            return {'code': -1, 'data': {}}

    def process_request(self, song_url):
        songmid = self.ids(song_url)
        if not songmid:
            return {'error': '歌曲ID无效'}

        sid = 0
        mid = songmid

        if songmid.isdigit():
            sid = int(songmid)
            mid = ''

        try:
            music_info = self.get_music_song(mid, sid)
            if 'msg' in music_info:
                return music_info

            music_lyric = self.get_music_lyric_new(music_info['id'])

            file_types = ['aac_48', 'aac_96', 'aac_192', 'ogg_96', 'ogg_192', 'ogg_320', 'ogg_640', 'atmos_51', 'atmos_2', 'master', 'flac', '320', '128']
            results = {}

            for file_type in file_types:
                result = self.get_music_url(music_info['mid'], file_type)
                if result:
                    results[file_type] = {
                        'url': result['url'],
                        'bitrate': result['bitrate']
                    }

            return {
                'music_info': music_info,
                'music_url': results,
                'music_lyric': music_lyric
            }
        except Exception as e:
            print(f'处理请求失败: {e}')
            return {'error': '处理请求失败'}

    @staticmethod
    def _hash33(s):
        h = 0
        for c in s:
            h = (h << 5) + h + ord(c)
            h &= 0xFFFFFFFF
        return h & 0x7FFFFFFF

    def get_qr_code(self):
        t = random.randint(0, 9999999) / 10000000
        url = (f'https://xui.ptlogin2.qq.com/ssl/ptqrshow'
               f'?appid=716027609&e=2&l=M&s=3&d=72&v=4&t={t}'
               f'&daid=383&pt_3rd_aid=100497308'
               f'&u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump')
        try:
            sess = requests.Session()
            headers = {
                'User-Agent': 'Mozilla/5.0 Chrome/149.0.0.0',
                'Referer': 'https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=716027609&daid=383&style=33&login_text=%E7%99%BB%E5%BD%95&hide_title_bar=1&hide_border=1&target=self&s_url=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump&pt_3rd_aid=100497308&theme=2&verify_theme=',
            }
            resp = sess.get(url, timeout=15, headers=headers)
            for c in sess.cookies:
                if c.value:
                    self.cookies[c.name] = c.value
            qrsig = ''
            for c in sess.cookies:
                if c.name == 'qrsig':
                    qrsig = c.value
            if not qrsig:
                m = _re.search(r'qrsig=([^;]+)', resp.headers.get('Set-Cookie', ''))
                if m:
                    qrsig = m.group(1)
                    self.cookies['qrsig'] = qrsig
            if not qrsig:
                return None
            return {
                'qrsig': qrsig,
                'image': 'data:image/png;base64,' + base64.b64encode(resp.content).decode()
            }
        except Exception as e:
            print(f'获取QQ二维码失败: {e}')
            return None

    def check_qr_login(self, qrsig):
        ptqrtoken = self._hash33(qrsig)
        ts = int(time.time() * 1000)
        url = (f'https://xui.ptlogin2.qq.com/ssl/ptqrlogin'
               f'?u1=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump'
               f'&ptqrtoken={ptqrtoken}&ptredirect=0&h=1&t=1&g=1&from_ui=1&ptlang=2052'
               f'&action=0-0-{ts}&js_ver=26030415&js_type=1&login_sig=&pt_uistyle=40'
               f'&aid=716027609&daid=383&pt_3rd_aid=100497308')
        try:
            headers = {
                'User-Agent': 'Mozilla/5.0 Chrome/149.0.0.0',
                'Referer': 'https://xui.ptlogin2.qq.com/cgi-bin/xlogin?appid=716027609&daid=383&style=33&login_text=%E7%99%BB%E5%BD%95&hide_title_bar=1&hide_border=1&target=self&s_url=https%3A%2F%2Fgraph.qq.com%2Foauth2.0%2Flogin_jump&pt_3rd_aid=100497308&theme=2&verify_theme=',
            }
            resp = requests.get(url, timeout=15, headers=headers, cookies=self.cookies)
            resp.encoding = 'utf-8'
            text = resp.text
            m = _re.search(r"ptuiCB\('(\d+)','(\d+)','([^']*)','([^']*)','([^']*)'", text)
            if not m:
                return (-1, '解析失败', {}, '')
            code = int(m.group(1))
            msg = m.group(5)
            cb_url = m.group(3)
            cookies = dict(self.cookies)
            for c in resp.cookies:
                if c.value:
                    cookies[c.name] = c.value
            return (code, msg, cookies, cb_url)
        except Exception as e:
            print(f'检查QQ登录状态失败: {e}')
            return (-1, str(e), {}, '')

    def exchange_callback(self, callback_url):
        try:
            sess = requests.Session()
            sess.cookies.update(self.cookies)
            headers = {
                'User-Agent': 'Mozilla/5.0 Chrome/149.0.0.0',
                'Referer': 'https://xui.ptlogin2.qq.com/',
            }
            sess.get(callback_url, timeout=15, allow_redirects=True, headers=headers)
            result = {}
            for c in sess.cookies:
                if c.value:
                    result[c.name] = c.value
            return result
        except Exception as e:
            print(f'交换QQ回调失败: {e}')
            return {}
