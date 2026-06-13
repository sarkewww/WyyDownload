"""
QQ音乐链接获取服务
读取cookie文件，映射音质，返回歌曲播放链接
"""

import os
import time
from flask import Blueprint, request, jsonify
from qq_api import QQMusic

router = Blueprint('qqmusic', __name__)

quality_map = {
    '标准': '128',
    'HQ高品质': '320',
    'SQ无损品质': 'flac',
    '臻品母带3.0': 'master',
    '臻品全景声2.0': 'atmos_2',
    '臻品音质2.0': 'atmos_51',
    'OGG高品质': 'ogg_320',
    'OGG标准': 'ogg_192',
    'AAC高品质': 'aac_192',
    'AAC标准': 'aac_96',
}

reverse_quality_map = {
    '128': '标准',
    '320': 'HQ高品质',
    'flac': 'SQ无损品质',
    'master': '臻品母带3.0',
    'atmos_2': '臻品全景声2.0',
    'atmos_51': '臻品音质2.0',
    'ogg_320': 'OGG高品质',
    'ogg_192': 'OGG标准',
    'aac_192': 'AAC高品质',
    'aac_96': 'AAC标准'
}

quality_priority = [
    '标准',
    'AAC标准',
    'OGG标准',
    'AAC高品质',
    'HQ高品质',
    'OGG高品质',
    'SQ无损品质',
    '臻品音质2.0',
    '臻品全景声2.0',
    '臻品母带3.0'
]


def get_best_quality(supported_qualities):
    if not supported_qualities:
        return None

    best_quality = None
    highest_priority = -1

    for quality in supported_qualities:
        if quality in quality_priority:
            priority = quality_priority.index(quality)
            if priority > highest_priority:
                highest_priority = priority
                best_quality = quality

    return best_quality or supported_qualities[0]


def load_qq_cookie():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    cookie_path = os.path.join(base_dir, 'qqcookie.txt')
    try:
        if os.path.exists(cookie_path):
            with open(cookie_path, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            print(f'QQ音乐Cookie已加载: {"有效" if content else "空"}')
            return content
        else:
            print('qqcookie.txt文件不存在')
            return ''
    except Exception as e:
        print(f'读取QQ音乐Cookie失败: {e}')
        return ''


def _create_qqmusic():
    cookie_str = load_qq_cookie()
    if not cookie_str:
        return None, jsonify({'code': 500, 'msg': 'Cookie文件为空或不存在，请配置qqcookie.txt', 'data': None})

    qqmusic = QQMusic()
    qqmusic.set_cookies(cookie_str)
    return qqmusic, None


@router.post('/get-link')
def get_link():
    try:
        data = request.get_json() or {}
        songmid = data.get('songmid')
        quality = data.get('quality', 'HQ高品质')

        if not songmid:
            return jsonify({'code': 400, 'msg': '缺少songmid参数', 'data': None})

        qqmusic, err_resp = _create_qqmusic()
        if err_resp:
            return err_resp

        mapped_quality = quality_map.get(quality, quality)
        result = qqmusic.get_music_url(songmid, mapped_quality)

        if not result:
            return jsonify({
                'code': 500,
                'msg': f'无法获取{quality}({mapped_quality})播放链接，可能需要VIP权限或歌曲不存在',
                'data': None
            })

        return jsonify({
            'code': 200,
            'msg': '获取成功',
            'data': {
                'songmid': songmid,
                'quality': mapped_quality,
                'mapped_quality': reverse_quality_map.get(mapped_quality, quality),
                'url': result['url'],
                'bitrate': result.get('bitrate', mapped_quality.upper())
            }
        })
    except Exception as e:
        print(f'获取播放链接错误: {e}')
        return jsonify({'code': 500, 'msg': f'服务器内部错误: {e}', 'data': None})


@router.post('/get-links')
def get_links():
    try:
        data = request.get_json() or {}
        songmid = data.get('songmid')
        qualities = data.get('qualities', ['标准', 'HQ高品质', 'SQ无损品质'])

        if not songmid:
            return jsonify({'code': 400, 'msg': '缺少songmid参数', 'data': None})

        qqmusic, err_resp = _create_qqmusic()
        if err_resp:
            return err_resp

        results = {}

        for quality in qualities:
            mapped_quality = quality_map.get(quality, quality)
            try:
                result = qqmusic.get_music_url(songmid, mapped_quality)
                if result:
                    results[quality] = {
                        'mapped_quality': mapped_quality,
                        'url': result['url'],
                        'bitrate': result['bitrate'],
                        'status': 'success'
                    }
                else:
                    results[quality] = {
                        'mapped_quality': mapped_quality,
                        'url': None,
                        'bitrate': None,
                        'status': 'failed',
                        'reason': '可能需要VIP权限或歌曲不存在'
                    }
            except Exception as e:
                results[quality] = {
                    'mapped_quality': mapped_quality,
                    'url': None,
                    'bitrate': None,
                    'status': 'error',
                    'reason': str(e)
                }

        return jsonify({
            'code': 200,
            'msg': '批量获取完成',
            'data': {
                'songmid': songmid,
                'results': results
            }
        })
    except Exception as e:
        print(f'批量获取播放链接错误: {e}')
        return jsonify({'code': 500, 'msg': f'服务器内部错误: {e}', 'data': None})


@router.get('/qualities')
def qualities():
    return jsonify({
        'code': 200,
        'msg': '获取成功',
        'data': {
            'quality_map': quality_map,
            'supported_qualities': list(quality_map.keys())
        }
    })


@router.get('/cookie-status')
def cookie_status():
    cookie_str = load_qq_cookie()
    return jsonify({
        'code': 200,
        'msg': '检查完成',
        'data': {
            'has_cookie': bool(cookie_str),
            'cookie_length': len(cookie_str),
            'cookie_preview': (cookie_str[:50] + '...') if cookie_str else '无Cookie'
        }
    })


@router.post('/get-lyric')
def get_lyric():
    try:
        data = request.get_json() or {}
        songmid = data.get('songmid')
        songid = data.get('songid')

        if not songmid and not songid:
            return jsonify({'code': 400, 'msg': '缺少songmid或songid参数', 'data': None})

        qqmusic, err_resp = _create_qqmusic()
        if err_resp:
            return err_resp

        if songid:
            result = qqmusic.get_music_lyric_new(songid)
        else:
            song_info = qqmusic.get_music_song(songmid, 0)
            if song_info.get('id'):
                result = qqmusic.get_music_lyric_new(song_info['id'])
            else:
                return jsonify({'code': 500, 'msg': '无法获取歌曲ID，请提供songid参数', 'data': None})

        if 'error' in result:
            return jsonify({'code': 500, 'msg': result['error'], 'data': None})

        return jsonify({
            'code': 200,
            'msg': '获取成功',
            'data': {
                'songmid': songmid or '',
                'songid': songid or '',
                'lyric': result.get('lyric', ''),
                'trans_lyric': result.get('tylyric', '')
            }
        })
    except Exception as e:
        print(f'获取歌词错误: {e}')
        return jsonify({'code': 500, 'msg': f'服务器内部错误: {e}', 'data': None})


@router.post('/check-quality-support')
def check_quality_support():
    try:
        data = request.get_json() or {}
        songmids = data.get('songmids')
        qualities = data.get('qualities')
        mode = data.get('mode', 'single')

        qqmusic, err_resp = _create_qqmusic()
        if err_resp:
            return err_resp

        if mode == 'single':
            if not songmids:
                return jsonify({'code': 400, 'msg': '缺少songmids参数，单首歌曲模式需要提供songmid', 'data': None})

            songmid = songmids[0] if isinstance(songmids, list) else songmids

            song_info = qqmusic.get_music_song(songmid, 0)
            if 'msg' in song_info:
                return jsonify({'code': 500, 'msg': f'歌曲信息获取失败: {song_info["msg"]}', 'data': None})

            all_qualities = list(quality_map.keys())
            supported_qualities = []
            unsupported_qualities = []
            quality_details = {}

            for quality_name in all_qualities:
                quality_code = quality_map[quality_name]
                try:
                    result = qqmusic.get_music_url(songmid, quality_code)
                    if result and result.get('url'):
                        supported_qualities.append(quality_name)
                        quality_details[quality_name] = {
                            'code': quality_code,
                            'bitrate': result['bitrate'],
                            'supported': True,
                            'url_available': True,
                            'url': result['url']
                        }
                    else:
                        unsupported_qualities.append(quality_name)
                        quality_details[quality_name] = {
                            'code': quality_code,
                            'bitrate': None,
                            'supported': False,
                            'url_available': False,
                            'reason': '可能需要VIP权限或该音质不存在'
                        }
                except Exception as e:
                    unsupported_qualities.append(quality_name)
                    quality_details[quality_name] = {
                        'code': quality_code,
                        'bitrate': None,
                        'supported': False,
                        'url_available': False,
                        'reason': f'检查失败: {e}'
                    }

                time.sleep(0.1)

            return jsonify({
                'code': 200,
                'msg': '音质支持检查完成',
                'data': {
                    'songmid': songmid,
                    'song_info': {
                        'name': song_info.get('name'),
                        'singer': song_info.get('singer'),
                        'album': song_info.get('album')
                    },
                    'total_qualities': len(all_qualities),
                    'supported_count': len(supported_qualities),
                    'unsupported_count': len(unsupported_qualities),
                    'supported_qualities': supported_qualities,
                    'unsupported_qualities': unsupported_qualities,
                    'quality_details': quality_details,
                    'best_quality': get_best_quality(supported_qualities)
                }
            })

        elif mode == 'batch':
            if not songmids or not isinstance(songmids, list) or len(songmids) == 0:
                return jsonify({'code': 400, 'msg': '批量模式需要提供songmids数组', 'data': None})

            if not qualities or not isinstance(qualities, list) or len(qualities) == 0:
                return jsonify({'code': 400, 'msg': '批量模式需要提供qualities数组', 'data': None})

            batch_results = {}
            total_checked = 0
            total_supported = 0

            for songmid in songmids:
                batch_results[songmid] = {
                    'song_info': None,
                    'qualities': {},
                    'supported_count': 0,
                    'total_count': len(qualities)
                }

                try:
                    song_info = qqmusic.get_music_song(songmid, 0)
                    if 'msg' not in song_info:
                        batch_results[songmid]['song_info'] = {
                            'name': song_info.get('name'),
                            'singer': song_info.get('singer'),
                            'album': song_info.get('album')
                        }
                except Exception as e:
                    print(f'获取歌曲信息失败 {songmid}: {e}')

                for quality_name in qualities:
                    quality_code = quality_map.get(quality_name, quality_name)
                    total_checked += 1

                    try:
                        result = qqmusic.get_music_url(songmid, quality_code)
                        if result and result.get('url'):
                            batch_results[songmid]['qualities'][quality_name] = {
                                'code': quality_code,
                                'bitrate': result['bitrate'],
                                'supported': True,
                                'url_available': True,
                                'url': result['url']
                            }
                            batch_results[songmid]['supported_count'] += 1
                            total_supported += 1
                        else:
                            batch_results[songmid]['qualities'][quality_name] = {
                                'code': quality_code,
                                'bitrate': None,
                                'supported': False,
                                'url_available': False,
                                'reason': '可能需要VIP权限或该音质不存在'
                            }
                    except Exception as e:
                        batch_results[songmid]['qualities'][quality_name] = {
                            'code': quality_code,
                            'bitrate': None,
                            'supported': False,
                            'url_available': False,
                            'reason': f'检查失败: {e}'
                        }

                    time.sleep(0.05)

            return jsonify({
                'code': 200,
                'msg': '批量音质支持检查完成',
                'data': {
                    'mode': 'batch',
                    'total_songs': len(songmids),
                    'checked_qualities': qualities,
                    'total_checks': total_checked,
                    'total_supported': total_supported,
                    'support_rate': f'{((total_supported / total_checked) * 100):.2f}%' if total_checked > 0 else '0%',
                    'results': batch_results
                }
            })

        else:
            return jsonify({'code': 400, 'msg': 'mode参数无效，支持: single(单首歌曲检查所有音质) 或 batch(批量歌曲检查指定音质)', 'data': None})

    except Exception as e:
        print(f'音质支持检查错误: {e}')
        return jsonify({'code': 500, 'msg': f'服务器内部错误: {e}', 'data': None})


@router.post('/get-info')
def get_info():
    try:
        data = request.get_json() or {}
        songmid = data.get('songmid')
        songid = data.get('songid')

        if not songmid and not songid:
            return jsonify({'code': 400, 'msg': '缺少songmid或songid参数', 'data': None})

        qqmusic, err_resp = _create_qqmusic()
        if err_resp:
            return err_resp

        result = qqmusic.get_music_song(songmid or '', songid or 0)

        if 'msg' in result:
            return jsonify({'code': 500, 'msg': result['msg'], 'data': None})

        return jsonify({
            'code': 200,
            'msg': '获取成功',
            'data': {
                'name': result.get('name'),
                'album': result.get('album'),
                'singer': result.get('singer'),
                'pic': result.get('pic'),
                'mid': result.get('mid'),
                'id': result.get('id'),
                'interval': result.get('interval')
            }
        })
    except Exception as e:
        print(f'获取歌曲信息错误: {e}')
        return jsonify({'code': 500, 'msg': f'服务器内部错误: {e}', 'data': None})
