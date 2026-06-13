"""网易云音乐API服务主程序

提供网易云音乐相关API服务，包括：
- 歌曲信息获取
- 音乐搜索
- 歌单和专辑详情
- 音乐下载
- 健康检查

项目基于 https://github.com/Suxiaoqinx 的 https://github.com/Suxiaoqinx/Netease_url 二次开发
"""

import json
import logging
import sys
import time
import threading
import traceback
import tempfile
import requests
from io import BytesIO
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Any, Optional, Tuple, List
from urllib.parse import quote
from flask import Flask, request, send_file, render_template, Response, make_response

try:
    from music_api import (
        NeteaseAPI, APIException, QRLoginManager,
        url_v1, name_v1, lyric_v1, search_music, 
        playlist_detail, album_detail
    )
    from cookie_manager import CookieManager, CookieException
    from music_downloader import (
        MusicDownloader, DownloadException, BatchTaskManager, Database,
        safe_filename, merge_translation_lyric, make_zip_response, VALID_LEVELS
    )
    from qq_api import QQMusic
    from qqmusic_link import quality_map, reverse_quality_map, quality_priority, get_best_quality
except ImportError as e:
    print(f"导入模块失败: {e}")
    print("请确保所有依赖模块存在且可用")
    sys.exit(1)


@dataclass
class APIConfig:
    """API配置类"""
    host: str = '0.0.0.0'
    port: int = 5000
    
    debug: bool = False
    downloads_dir: str = 'downloads'
    max_file_size: int = 500 * 1024 * 1024  # 500MB
    request_timeout: int = 30
    log_level: str = 'INFO'
    cors_origins: str = '*'


class APIResponse:
    """API响应工具类"""
    
    @staticmethod
    def success(data: Any = None, message: str = 'success', status_code: int = 200) -> Tuple[Dict[str, Any], int]:
        """成功响应"""
        response = {
            'status': status_code,
            'success': True,
            'message': message       }
        if data is not None:
            response['data'] = data
        return response, status_code
    
    @staticmethod
    def error(message: str, status_code: int = 400, error_code: str = None) -> Tuple[Dict[str, Any], int]:
        """错误响应"""
        response = {
            'status': status_code,
            'success': False,
            'message': message
        }
        if error_code:
            response['error_code'] = error_code
        return response, status_code


class MusicAPIService:
    """音乐API服务类"""
    
    def __init__(self, config: APIConfig):
        self.config = config
        self.logger = self._setup_logger()
        self.cookie_manager = CookieManager()
        self.netease_api = NeteaseAPI()
        self.downloader = MusicDownloader()
        
        # 创建下载目录
        self.downloads_path = Path(config.downloads_dir)
        self.downloads_path.mkdir(exist_ok=True)
        
        self.logger.info(f"音乐API服务初始化完成，下载目录: {self.downloads_path.absolute()}")
    
    def _setup_logger(self) -> logging.Logger:
        """设置日志记录器"""
        logger = logging.getLogger('music_api_service')
        logger.setLevel(getattr(logging, self.config.log_level.upper()))
        
        if not logger.handlers:
            # 控制台处理器
            console_handler = logging.StreamHandler()
            console_formatter = logging.Formatter(
                '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
            )
            console_handler.setFormatter(console_formatter)
            logger.addHandler(console_handler)
            
            # 文件处理器
            try:
                file_handler = logging.FileHandler('music_api.log', encoding='utf-8')
                file_formatter = logging.Formatter(
                    '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
                )
                file_handler.setFormatter(file_formatter)
                logger.addHandler(file_handler)
            except Exception as e:
                logger.warning(f"无法创建日志文件: {e}")
        
        return logger
    
    def _get_cookies(self) -> Dict[str, str]:
        """获取Cookie"""
        try:
            cookie_str = self.cookie_manager.read_cookie()
            return self.cookie_manager.parse_cookie_string(cookie_str)
        except CookieException as e:
            self.logger.warning(f"获取Cookie失败: {e}")
            return {}
        except Exception as e:
            self.logger.error(f"Cookie处理异常: {e}")
            return {}
    
    def _extract_music_id(self, id_or_url: str) -> str:
        """提取音乐ID"""
        from urllib.parse import unquote
        id_or_url = unquote(id_or_url)
        try:
            if '163cn.tv' in id_or_url:
                try:
                    response = requests.get(id_or_url, allow_redirects=False, timeout=10)
                    id_or_url = response.headers.get('Location', id_or_url)
                except Exception:
                    api_service.logger.debug(f"短链接解析失败: {id_or_url}")

            if 'music.163.com' in id_or_url:
                index = id_or_url.find('id=') + 3
                if index > 2:
                    return id_or_url[index:].split('&')[0]
            
            return str(id_or_url).strip()
            
        except Exception as e:
            self.logger.error(f"提取音乐ID失败: {e}")
            return str(id_or_url).strip()
    
    def _format_file_size(self, size_bytes: int) -> str:
        """格式化文件大小"""
        if size_bytes == 0:
            return "0B"
        
        units = ["B", "KB", "MB", "GB", "TB"]
        size = float(size_bytes)
        unit_index = 0
        
        while size >= 1024.0 and unit_index < len(units) - 1:
            size /= 1024.0
            unit_index += 1
        
        return f"{size:.2f}{units[unit_index]}"
    
    def _get_quality_display_name(self, quality: str) -> str:
        """获取音质显示名称"""
        quality_names = {
            'standard': "标准音质",
            'exhigh': "极高音质", 
            'lossless': "无损音质",
            'hires': "Hi-Res音质",
            'sky': "沉浸环绕声",
            'jyeffect': "高清环绕声",
            'jymaster': "超清母带",
            'dolby': "杜比全景声"
        }
        return quality_names.get(quality, f"未知音质({quality})")
    
    def _validate_request_params(self, required_params: Dict[str, Any]) -> Optional[Tuple[Dict[str, Any], int]]:
        """验证请求参数"""
        for param_name, param_value in required_params.items():
            if not param_value:
                return APIResponse.error(f"参数 '{param_name}' 不能为空", 400)
        return None
    
    def _safe_get_request_data(self) -> Dict[str, Any]:
        """安全获取请求数据"""
        try:
            if request.method == 'GET':
                return dict(request.args)
            else:
                # 优先使用JSON数据，然后是表单数据
                json_data = request.get_json(silent=True) or {}
                form_data = dict(request.form)
                # 合并数据，JSON优先
                return {**form_data, **json_data}
        except Exception as e:
            self.logger.error(f"获取请求数据失败: {e}")
            return {}


# 创建Flask应用和服务实例
config = APIConfig()
app = Flask(__name__, static_folder='templates/static')
api_service = MusicAPIService(config)

batch_task_mgr = BatchTaskManager(api_service.downloader)
qr_manager = QRLoginManager()
db = Database()

# QQ音乐服务实例
qq_cookie_mgr = CookieManager(platform='qq')
qq_api = QQMusic()
QQ_VALID_LEVELS = list(qq_api.file_config.keys())
QQ_QUALITY_DEGRADE_ORDER = [
    'master',    # 臻品母带 → 超级会员
    'atmos_2',   # 臻品全景声 → 超级会员
    'hires',     # Hi-Res → 豪华绿钻+
    'flac',      # SQ无损 → 音乐包/豪华绿钻+
    '320',       # HQ高品质 → 所有用户
    '128',       # 标准 → 所有用户
]
qq_downloader = MusicDownloader(download_dir='downloads/qq')
qq_batch_mgr = BatchTaskManager(qq_downloader)


def get_playlist_or_fail(playlist_id):
    """获取歌单信息，失败时返回错误响应"""
    cookies = api_service._get_cookies()
    playlist = playlist_detail(playlist_id, cookies)
    if not playlist or not playlist.get('tracks'):
        return None, APIResponse.error("获取歌单详情失败或歌单为空", 404)
    return playlist, None


def get_tracks_and_info(source_type, source_id, level=None):
    """统一获取歌单/专辑的 tracks 和信息, 返回 (tracks, info, error_response)"""
    cookies = api_service._get_cookies()
    if source_type == 'album':
        album = album_detail(source_id, cookies)
        if not album or not album.get('songs'):
            return None, None, APIResponse.error("获取专辑详情失败", 404)
        info = {'id': album.get('id'), 'name': album.get('name'),
                'coverImgUrl': album.get('coverImgUrl'), 'creator': album.get('artist', ''),
                'description': album.get('description', ''), 'trackCount': len(album['songs'])}
        return album['songs'], info, None
    playlist = playlist_detail(source_id, cookies)
    if not playlist or not playlist.get('tracks'):
        return None, None, APIResponse.error("获取歌单详情失败或歌单为空", 404)
    info = {'id': playlist.get('id'), 'name': playlist.get('name'),
            'coverImgUrl': playlist.get('coverImgUrl'), 'creator': playlist.get('creator'),
            'description': playlist.get('description', ''), 'trackCount': playlist.get('trackCount')}
    return playlist['tracks'], info, None


@app.route('/qr-login/start', methods=['POST'])
def qr_login_start():
    """生成登录二维码"""
    try:
        unikey = qr_manager.generate_qr_key()
        if not unikey:
            return APIResponse.error("生成二维码key失败", 500)
        qr_url = f'https://music.163.com/login?codekey={unikey}'
        img_data = None
        try:
            import qrcode, base64
            qr = qrcode.QRCode(box_size=6, border=2)
            qr.add_data(qr_url)
            qr.make(fit=True)
            img = qr.make_image(fill_color='#6c5ce7', back_color='white')
            buf = BytesIO()
            img.save(buf, format='PNG')
            img_data = 'data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode()
        except Exception:
            pass
        return APIResponse.success({'unikey': unikey, 'qr_url': qr_url, 'qr_image': img_data}, "二维码已生成")
    except Exception as e:
        api_service.logger.error(f"生成二维码异常: {e}")
        return APIResponse.error(f"生成失败: {str(e)}", 500)


@app.route('/qr-login/check/<unikey>', methods=['GET'])
def qr_login_check(unikey: str):
    """检查二维码登录状态"""
    try:
        code, cookies = qr_manager.check_qr_login(unikey)
        if code == 803:
            parts = [f"MUSIC_U={cookies.get('MUSIC_U', '')}"]
            for k, v in cookies.items():
                if k != 'MUSIC_U' and v:
                    parts.append(f"{k}={v}")
            parts.append("os=pc;appver=8.9.70")
            cookie_str = ';'.join(parts)
            try:
                api_service.cookie_manager.write_cookie(cookie_str)
                api_service.logger.info("二维码登录成功，Cookie已保存")
            except Exception as e:
                api_service.logger.error(f"保存Cookie失败: {e}")
            return APIResponse.success({'code': code, 'status': '登录成功', 'cookie': cookie_str})
        status_map = {801: '等待扫码', 802: '扫码成功，请在手机上确认', 800: '二维码已过期'}
        return APIResponse.success({'code': code, 'status': status_map.get(code, f'状态码: {code}')})
    except Exception as e:
        return APIResponse.error(f"检查登录状态失败: {str(e)}", 500)


# 简单速率限制（每IP每秒最多30请求）
_rate_limiter: Dict[str, List[float]] = {}
RATE_LIMIT = 30  # 每秒请求数
RATE_WINDOW = 1  # 秒

@app.before_request
def before_request():
    """请求前处理"""
    now = time.time()
    ip = request.remote_addr or '0.0.0.0'
    if ip not in _rate_limiter:
        _rate_limiter[ip] = []
    _rate_limiter[ip] = [t for t in _rate_limiter[ip] if now - t < RATE_WINDOW]
    if len(_rate_limiter[ip]) >= RATE_LIMIT:
        return APIResponse.error("请求过于频繁，请稍后再试", 429)
    _rate_limiter[ip].append(now)
    api_service.logger.debug(f"{request.method} {request.path}")


@app.after_request
def after_request(response: Response) -> Response:
    """请求后处理 - 设置CORS头"""
    response.headers.add('Access-Control-Allow-Origin', config.cors_origins)
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    response.headers.add('Access-Control-Max-Age', '3600')
    
    # 记录响应信息
    api_service.logger.info(f"响应状态: {response.status_code}")
    return response


@app.errorhandler(400)
def handle_bad_request(e):
    """处理400错误"""
    return APIResponse.error("请求参数错误", 400)


@app.errorhandler(404)
def handle_not_found(e):
    """处理404错误"""
    return APIResponse.error("请求的资源不存在", 404)


@app.errorhandler(500)
def handle_internal_error(e):
    """处理500错误"""
    api_service.logger.error(f"服务器内部错误: {e}")
    return APIResponse.error("服务器内部错误", 500)


@app.route('/')
def index() -> str:
    """首页路由"""
    return render_template('index.html')


@app.route('/health', methods=['GET'])
def health_check():
    """健康检查API"""
    try:
        # 检查Cookie状态
        cookie_status = api_service.cookie_manager.is_cookie_valid()
        
        health_info = {
            'service': 'running',
            'timestamp': int(time.time()) if 'time' in sys.modules else None,
            'cookie_status': 'valid' if cookie_status else 'invalid',
            'downloads_dir': str(api_service.downloads_path.absolute()),
            'version': '2.0.0'
        }
        
        return APIResponse.success(health_info, "API服务运行正常")
        
    except Exception as e:
        api_service.logger.error(f"健康检查失败: {e}")
        return APIResponse.error(f"健康检查失败: {str(e)}", 500)


@app.route('/cookie', methods=['GET', 'POST'])
def cookie_manage():
    """Cookie管理API"""
    try:
        if request.method == 'GET':
            content = api_service.cookie_manager.read_cookie()
            info = api_service.cookie_manager.get_cookie_info()
            info['raw_content'] = content
            return APIResponse.success(info, "获取Cookie信息成功")
        data = api_service._safe_get_request_data()
        new_cookie = (data.get('cookie') or '').strip()
        if not new_cookie:
            return APIResponse.error("cookie参数不能为空")
        try:
            api_service.cookie_manager.write_cookie(new_cookie)
            api_service.logger.info("Cookie已通过API更新")
            return APIResponse.success(None, "Cookie更新成功")
        except CookieException as e:
            return APIResponse.error(f"Cookie更新失败: {e}")
    except Exception as e:
        return APIResponse.error(f"操作失败: {str(e)}", 500)


@app.route('/song', methods=['GET', 'POST'])
@app.route('/Song_V1', methods=['GET', 'POST'])  # 向后兼容
def get_song_info():
    """获取歌曲信息API"""
    try:
        # 获取请求参数
        data = api_service._safe_get_request_data()
        song_ids = data.get('ids') or data.get('id')
        url = data.get('url')
        level = data.get('level', 'lossless')
        info_type = data.get('type', 'url')
        
        # 参数验证
        if not song_ids and not url:
            return APIResponse.error("必须提供 'ids'、'id' 或 'url' 参数")
        
        # 提取音乐ID
        music_id = api_service._extract_music_id(song_ids or url)
        
        # 验证音质参数
        valid_levels = VALID_LEVELS
        if level not in valid_levels:
            return APIResponse.error(f"无效的音质参数，支持: {', '.join(valid_levels)}")
        
        # 验证类型参数
        valid_types = ['url', 'name', 'lyric', 'json']
        if info_type not in valid_types:
            return APIResponse.error(f"无效的类型参数，支持: {', '.join(valid_types)}")
        
        cookies = api_service._get_cookies()
        
        # 根据类型获取不同信息
        if info_type == 'url':
            result = url_v1(music_id, level, cookies)
            if result and result.get('data') and len(result['data']) > 0:
                song_data = result['data'][0]
                response_data = {
                    'id': song_data.get('id'),
                    'url': song_data.get('url'),
                    'level': song_data.get('level'),
                    'quality_name': api_service._get_quality_display_name(song_data.get('level', level)),
                    'size': song_data.get('size'),
                    'size_formatted': api_service._format_file_size(song_data.get('size', 0)),
                    'type': song_data.get('type'),
                    'bitrate': song_data.get('br')
                }
                return APIResponse.success(response_data, "获取歌曲URL成功")
            else:
                return APIResponse.error("获取音乐URL失败，可能是版权限制或音质不支持", 404)
        
        elif info_type == 'name':
            result = name_v1(music_id)
            return APIResponse.success(result, "获取歌曲信息成功")
        
        elif info_type == 'lyric':
            result = lyric_v1(music_id, cookies)
            return APIResponse.success(result, "获取歌词成功")
        
        elif info_type == 'json':
            # 获取完整的歌曲信息（用于前端解析）
            song_info = name_v1(music_id)
            url_info = url_v1(music_id, level, cookies)
            lyric_info = lyric_v1(music_id, cookies)
            
            if not song_info or 'songs' not in song_info or not song_info['songs']:
                return APIResponse.error("未找到歌曲信息", 404)
            
            song_data = song_info['songs'][0]
            
            # 构建前端期望的响应格式
            response_data = {
                'id': music_id,
                'name': song_data.get('name', ''),
                'ar_name': ', '.join(artist['name'] for artist in song_data.get('ar', [])),
                'al_name': song_data.get('al', {}).get('name', ''),
                'pic': song_data.get('al', {}).get('picUrl', ''),
                'level': level,
                'lyric': lyric_info.get('lrc', {}).get('lyric', '') if lyric_info else '',
                'tlyric': lyric_info.get('tlyric', {}).get('lyric', '') if lyric_info else ''
            }
            
            # 添加URL和大小信息
            if url_info and url_info.get('data') and len(url_info['data']) > 0:
                url_data = url_info['data'][0]
                response_data.update({
                    'url': url_data.get('url', ''),
                    'size': api_service._format_file_size(url_data.get('size', 0)),
                    'level': url_data.get('level', level)
                })
            else:
                response_data.update({
                    'url': '',
                    'size': '获取失败'
                })
            
            return APIResponse.success(response_data, "获取歌曲信息成功")
            
    except APIException as e:
        api_service.logger.error(f"API调用失败: {e}")
        return APIResponse.error(f"API调用失败: {str(e)}", 500)
    except Exception as e:
        api_service.logger.error(f"获取歌曲信息异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"服务器错误: {str(e)}", 500)


@app.route('/search', methods=['GET', 'POST'])
@app.route('/Search', methods=['GET', 'POST'])  # 向后兼容
def search_music_api():
    """搜索音乐API"""
    try:
        # 获取请求参数
        data = api_service._safe_get_request_data()
        keyword = data.get('keyword') or data.get('keywords') or data.get('q')
        limit = int(data.get('limit', 30))
        offset = int(data.get('offset', 0))
        search_type = data.get('type', '1')  # 1-歌曲, 10-专辑, 100-歌手, 1000-歌单
        
        # 参数验证
        validation_error = api_service._validate_request_params({'keyword': keyword})
        if validation_error:
            return validation_error
        
        # 限制搜索数量
        if limit > 100:
            limit = 100
        
        cookies = api_service._get_cookies()
        result = search_music(keyword, cookies, limit, int(search_type), offset)
        
        # search_music返回的是歌曲列表，需要包装成前端期望的格式
        if result:
            for song in result:
                # 添加艺术家字符串（如果需要）
                if 'artists' in song:
                    song['artist_string'] = song['artists']

        db.add_search(keyword, search_type, 'netease')

        return APIResponse.success(result, "搜索完成")
        
    except ValueError as e:
        return APIResponse.error(f"参数格式错误: {str(e)}")
    except Exception as e:
        api_service.logger.error(f"搜索音乐异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"搜索失败: {str(e)}", 500)


@app.route('/playlist', methods=['GET', 'POST'])
@app.route('/Playlist', methods=['GET', 'POST'])  # 向后兼容
def get_playlist():
    """获取歌单详情API"""
    try:
        # 获取请求参数
        data = api_service._safe_get_request_data()
        playlist_id = data.get('id')
        
        # 参数验证
        validation_error = api_service._validate_request_params({'playlist_id': playlist_id})
        if validation_error:
            return validation_error

        if 'y.qq.com' in str(playlist_id):
            return APIResponse.error("检测到 QQ 音乐链接，请切换到 <a href='/qq'>QQ音乐工具箱</a>", 400)
        
        cookies = api_service._get_cookies()
        result = playlist_detail(playlist_id, cookies)

        return APIResponse.success({'playlist': result}, "获取歌单详情成功")
        
    except Exception as e:
        api_service.logger.error(f"获取歌单异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"获取歌单失败: {str(e)}", 500)


@app.route('/playlist/batch', methods=['GET', 'POST'])
def batch_get_playlist_urls():
    """批量获取歌单中所有歌曲的URL（复用 get_tracks_and_info + _do_batch_resolve）"""
    try:
        data = api_service._safe_get_request_data()
        playlist_id = data.get('id')
        level = data.get('level', 'lossless')
        validation_error = api_service._validate_request_params({'playlist_id': playlist_id})
        if validation_error:
            return validation_error
        if level not in VALID_LEVELS:
            return APIResponse.error(f"无效的音质参数，支持: {', '.join(VALID_LEVELS)}")
        tracks, info, err = get_tracks_and_info('playlist', playlist_id)
        if err:
            return err
        return _do_batch_resolve(tracks, info, level, 'playlist')
    except Exception as e:
        api_service.logger.error(f"批量获取歌单URL异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"批量获取失败: {str(e)}", 500)


@app.route('/playlist/download/batch/start', methods=['POST'])
def batch_download_start():
    """启动批量下载任务（多线程）"""
    try:
        data = api_service._safe_get_request_data()
        playlist_id = data.get('id')
        level = data.get('level', 'lossless')
        validation_error = api_service._validate_request_params({'playlist_id': playlist_id})
        if validation_error:
            return validation_error
        valid_levels = VALID_LEVELS
        if level not in valid_levels:
            return APIResponse.error(f"无效的音质参数，支持: {', '.join(valid_levels)}")
        cookies = api_service._get_cookies()
        api_service.logger.info(f"[DL-START] step1: fetching playlist {playlist_id}")
        playlist = playlist_detail(playlist_id, cookies)
        api_service.logger.info(f"[DL-START] step2: got playlist, {len(playlist.get('tracks', []))} tracks")
        if not playlist or not playlist.get('tracks'):
            return APIResponse.error("获取歌单详情失败或歌单为空", 404)
        tracks = playlist['tracks']
        playlist_info = {
            'id': playlist.get('id'), 'name': playlist.get('name'),
            'coverImgUrl': playlist.get('coverImgUrl'), 'creator': playlist.get('creator'),
            'trackCount': playlist.get('trackCount'), 'description': playlist.get('description'),
        }
        api_service.logger.info(f"[DL-START] step3: creating task for {len(tracks)} tracks")
        task_id = batch_task_mgr.create_task(tracks, playlist_info, level, cookies)
        api_service.logger.info(f"[DL-START] step4: task_id={task_id}, returning")
        return APIResponse.success({'task_id': task_id}, "下载任务已创建")
    except Exception as e:
        api_service.logger.error(f"创建批量下载任务异常: {e}")
        return APIResponse.error(f"创建任务失败: {str(e)}", 500)


@app.route('/playlist/download/batch/progress/<task_id>', methods=['GET'])
@app.route('/album/download/batch/progress/<task_id>', methods=['GET'])
def batch_download_progress(task_id: str):
    """查询批量下载进度（实时速度）"""
    try:
        progress = batch_task_mgr.get_progress(task_id)
        if progress is None:
            return APIResponse.error("任务不存在或已过期", 404)
        return APIResponse.success(progress, "获取进度成功")
    except Exception as e:
        return APIResponse.error(f"获取进度失败: {str(e)}", 500)


@app.route('/playlist/download/batch/result/<task_id>', methods=['GET'])
@app.route('/album/download/batch/result/<task_id>', methods=['GET'])
def batch_download_result(task_id: str):
    """获取批量下载结果ZIP"""
    try:
        result = batch_task_mgr.get_result(task_id)
        if result is None:
            progress = batch_task_mgr.get_progress(task_id)
            if progress and progress['status'] == 'running':
                return APIResponse.error("下载尚未完成", 202)
            return APIResponse.error("任务不存在或已过期", 404)
        zip_buffer, zip_filename, task_info = result
        zip_data = zip_buffer.getvalue()
        api_service.logger.info(f"[DL-RESULT-{task_id}] sending ZIP, {len(zip_data)} bytes, {task_info.get('success')}/{task_info.get('total')} songs")
        safe_fn = quote(zip_filename)
        response = Response(zip_data, mimetype='application/zip',
                           headers={'Content-Disposition': f"attachment; filename*=UTF-8''{safe_fn}"})
        response.headers['X-Download-Count'] = str(task_info.get('success', 0))
        response.headers['X-Total-Count'] = str(task_info.get('total', 0))
        response.headers['X-Fail-List'] = quote(json.dumps(task_info.get('errors', [])), safe='')
        batch_task_mgr.cleanup(task_id)
        return response
    except Exception as e:
        return APIResponse.error(f"获取结果失败: {str(e)}", 500)


@app.route('/playlist/download/batch/cancel/<task_id>', methods=['POST'])
@app.route('/album/download/batch/cancel/<task_id>', methods=['POST'])
def batch_download_cancel(task_id: str):
    """取消批量下载任务"""
    try:
        if batch_task_mgr.cancel(task_id):
            return APIResponse.success(None, "任务已取消")
        return APIResponse.error("任务不存在或无法取消", 404)
    except Exception as e:
        return APIResponse.error(f"取消失败: {str(e)}", 500)


@app.route('/lyric/download', methods=['GET', 'POST'])
def download_lyric():
    """下载歌词文件(.lrc)"""
    try:
        data = api_service._safe_get_request_data()
        song_id = data.get('id')
        tlyric = (data.get('tlyric', '') or '').lower() in ('true', '1', 'yes')
        validation_error = api_service._validate_request_params({'song_id': song_id})
        if validation_error:
            return validation_error

        cookies = api_service._get_cookies()
        lyric_info = lyric_v1(song_id, cookies)
        if not lyric_info:
            return APIResponse.error("获取歌词失败", 404)
        lrc = lyric_info.get('lrc', {}).get('lyric', '')
        tl = lyric_info.get('tlyric', {}).get('lyric', '')
        if not lrc:
            return APIResponse.error("未找到歌词", 404)
        if tlyric and tl:
            lrc = merge_translation_lyric(lrc, tl)
        song_info = name_v1(song_id)
        song_name = 'lyric'
        if song_info and song_info.get('songs') and song_info['songs']:
            s = song_info['songs'][0]
            ar = '/'.join(a['name'] for a in s.get('ar', []))
            song_name = f"{ar} - {s.get('name', '')}"
        safe_name = ''.join(c for c in song_name if c not in r'<>:"/\|?*') + '.lrc'
        resp = make_response(lrc)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
        return resp
    except Exception as e:
        return APIResponse.error(f"下载歌词失败: {str(e)}", 500)


@app.route('/playlist/lyric/batch', methods=['POST'])
def batch_download_lyric():
    """批量下载歌单歌词(ZIP)"""
    try:
        data = api_service._safe_get_request_data()
        playlist_id = data.get('id')
        tlyric = (data.get('tlyric', '') or '').lower() in ('true', '1', 'yes')
        validation_error = api_service._validate_request_params({'playlist_id': playlist_id})
        if validation_error:
            return validation_error
        cookies = api_service._get_cookies()
        playlist = playlist_detail(playlist_id, cookies)
        if not playlist or not playlist.get('tracks'):
            return APIResponse.error("获取歌单详情失败或歌单为空", 404)
        tracks = playlist['tracks']
        total = len(tracks)
        api_service.logger.info(f"批量下载歌单歌词 {playlist_id}，共 {total} 首")
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            success_count = 0
            for i, track in enumerate(tracks):
                sid = track['id']
                safe_name = f"{i + 1:03d}. {track['artists']} - {track['name']}"
                safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*') + '.lrc'
                try:
                    lyric_info = lyric_v1(sid, cookies)
                    lrc = lyric_info.get('lrc', {}).get('lyric', '')
                    tl = lyric_info.get('tlyric', {}).get('lyric', '')
                    if not lrc:
                        continue
                    if tlyric and tl:
                        lrc = merge_translation_lyric(lrc, tl)
                    (tmp_path / safe_name).write_text(lrc, encoding='utf-8')
                    success_count += 1
                except Exception:
                    pass
            if success_count == 0:
                return APIResponse.error("所有歌词获取失败", 500)
            zip_buffer, zip_filename = make_zip_response(tmp_path, playlist.get('name', 'playlist') + '_lyrics')
            response = send_file(zip_buffer, as_attachment=True, download_name=zip_filename, mimetype='application/zip')
            response.headers['X-Lyric-Count'] = str(success_count)
            response.headers['X-Total-Count'] = str(total)
            return response
    except Exception as e:
        return APIResponse.error(f"批量歌词下载失败: {str(e)}", 500)


@app.route('/cover/download', methods=['GET', 'POST'])
def download_cover():
    """下载歌曲封面图"""
    try:
        data = api_service._safe_get_request_data()
        song_id = data.get('id')
        validation_error = api_service._validate_request_params({'song_id': song_id})
        if validation_error:
            return validation_error
        song_id = api_service._extract_music_id(song_id)
        song_info = name_v1(song_id)
        if not song_info or not song_info.get('songs') or not song_info['songs']:
            return APIResponse.error("未找到歌曲信息", 404)
        s = song_info['songs'][0]
        pic_url = s.get('al', {}).get('picUrl') or s.get('album', {}).get('picUrl', '')
        if not pic_url:
            return APIResponse.error("未找到封面图片", 404)
        pic_url = pic_url.replace('http://', 'https://') + '?param=500y500'
        resp = requests.get(pic_url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        resp.raise_for_status()
        ar = '/'.join(a['name'] for a in s.get('ar', []))
        safe_name = ''.join(c for c in f"{ar} - {s.get('name', '')}" if c not in r'<>:"/\|?*') + '.jpg'
        response = make_response(resp.content)
        response.headers['Content-Type'] = 'image/jpeg'
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
        return response
    except Exception as e:
        api_service.logger.error(f"下载封面异常: {e}")
        return APIResponse.error(f"下载封面失败: {str(e)}", 500)


@app.route('/playlist/cover/batch', methods=['POST'])
def batch_download_cover():
    """批量下载歌单封面图 (打包为ZIP)"""
    try:
        data = api_service._safe_get_request_data()
        playlist_id = data.get('id')
        validation_error = api_service._validate_request_params({'playlist_id': playlist_id})
        if validation_error:
            return validation_error
        cookies = api_service._get_cookies()
        playlist = playlist_detail(playlist_id, cookies)
        if not playlist or not playlist.get('tracks'):
            return APIResponse.error("获取歌单详情失败或歌单为空", 404)
        tracks = playlist['tracks']
        total = len(tracks)
        api_service.logger.info(f"批量下载歌单封面 {playlist_id}，共 {total} 张")

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            success_count = 0

            for i, track in enumerate(tracks):
                pic_url = (track.get('picUrl') or '').replace('http://', 'https://')
                if not pic_url:
                    continue
                pic_url = pic_url + '?param=500y500'
                safe_name = f"{i + 1:03d}. {track['artists']} - {track['name']}"
                safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*') + '.jpg'
                try:
                    resp = requests.get(pic_url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                    resp.raise_for_status()
                    (tmp_path / safe_name).write_bytes(resp.content)
                    success_count += 1
                    api_service.logger.info(f"  [{i + 1}/{total}] 封面已获取: {safe_name}")
                except Exception as e:
                    api_service.logger.warning(f"  [{i + 1}/{total}] 封面获取失败: {track['name']} - {e}")

            if success_count == 0:
                return APIResponse.error("所有封面获取失败", 500)
            zip_buffer, zip_filename = make_zip_response(tmp_path, playlist.get('name', 'playlist') + '_covers')
            response = send_file(zip_buffer, as_attachment=True, download_name=zip_filename, mimetype='application/zip')
            response.headers['X-Cover-Count'] = str(success_count)
            response.headers['X-Total-Count'] = str(total)
            return response

    except Exception as e:
        api_service.logger.error(f"批量封面下载异常: {e}")
        return APIResponse.error(f"批量封面下载失败: {str(e)}", 500)


def _do_batch_resolve(tracks, info, level, source='playlist'):
    """执行批量 URL 解析并返回结果"""
    if not tracks:
        return APIResponse.success({'tracks': [], 'resolved': 0, 'total': 0, source: info})
    cookies = api_service._get_cookies()
    song_ids = [t['id'] for t in tracks]
    api_service.logger.info(f"批量解析 {source} {info.get('id')}, 共 {len(song_ids)} 首")
    urls_result = {}
    with ThreadPoolExecutor(max_workers=min(5, len(song_ids))) as executor:
        for i in range(0, len(song_ids), 20):
            batch = song_ids[i:i+20]
            f2id = {executor.submit(url_v1, sid, level, cookies): sid for sid in batch}
            for future in as_completed(f2id):
                sid = f2id[future]
                try:
                    result = future.result()
                    if result and result.get('data') and result['data']:
                        d = result['data'][0]
                        urls_result[sid] = {'url': d.get('url', ''), 'size': d.get('size', 0),
                            'size_formatted': api_service._format_file_size(d.get('size', 0)),
                            'type': d.get('type', ''), 'level': d.get('level', level),
                            'quality_name': api_service._get_quality_display_name(d.get('level', level)),
                            'br': d.get('br', 0)}
                    else: urls_result[sid] = None
                except Exception: urls_result[sid] = None
    resolved = []
    for t in tracks:
        ui = urls_result.get(t['id'])
        td = {'id': t['id'], 'name': t['name'], 'artists': t['artists'],
              'album': t['album'], 'picUrl': t['picUrl'], 'duration': t.get('duration', 0)}
        if ui: td.update(ui)
        else: td.update({'url': '', 'size': 0, 'size_formatted': '获取失败', 'type': '',
                          'level': level, 'quality_name': api_service._get_quality_display_name(level), 'br': 0})
        resolved.append(td)
    result = {'tracks': resolved, 'resolved': sum(1 for t in resolved if t['url']), 'total': len(tracks)}
    result[source] = info
    return APIResponse.success(result, f"批量获取{source}歌曲URL成功")


@app.route('/album/batch', methods=['GET', 'POST'])
def batch_get_album_urls():
    try:
        data = api_service._safe_get_request_data()
        album_id = data.get('id'); level = data.get('level', 'lossless')
        if not album_id: return APIResponse.error("缺少专辑ID")
        if 'playlist' in str(album_id): return APIResponse.error("检测到歌单链接，请切换到「歌单」Tab", 400)
        if level not in VALID_LEVELS: return APIResponse.error("无效音质")
        tracks, info, err = get_tracks_and_info('album', album_id)
        if err: return err
        return _do_batch_resolve(tracks, info, level, 'album')
    except Exception as e:
        return APIResponse.error(f"批量获取失败: {str(e)}", 500)

@app.route('/album/download/batch/start', methods=['POST'])
def album_batch_download_start():
    try:
        data = api_service._safe_get_request_data()
        album_id = data.get('id'); level = data.get('level', 'lossless')
        validation_error = api_service._validate_request_params({'album_id': album_id})
        if validation_error: return validation_error
        if 'playlist' in str(album_id): return APIResponse.error("检测到歌单链接，请切换到「歌单」Tab", 400)
        if level not in VALID_LEVELS: return APIResponse.error("无效音质")
        tracks, info, err = get_tracks_and_info('album', album_id)
        if err: return err
        cookies = api_service._get_cookies()
        task_id = batch_task_mgr.create_task(tracks, info, level, cookies)
        api_service.logger.info(f"专辑批量下载任务: {task_id}, 共 {len(tracks)} 首")
        return APIResponse.success({'task_id': task_id}, "下载任务已创建")
    except Exception as e:
        return APIResponse.error(f"创建任务失败: {str(e)}", 500)

@app.route('/album/cover/batch', methods=['POST'])
def album_batch_download_cover():
    try:
        data = api_service._safe_get_request_data()
        album_id = data.get('id')
        validation_error = api_service._validate_request_params({'album_id': album_id})
        if validation_error: return validation_error
        if 'playlist' in str(album_id): return APIResponse.error("检测到歌单链接，请切换到「歌单」Tab", 400)
        cookies = api_service._get_cookies()
        album = album_detail(album_id, cookies)
        if not album or not album.get('songs'): return APIResponse.error("获取专辑失败", 404)
        tracks = album['songs']
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir); success = 0
            for i, t in enumerate(tracks):
                pic = (t.get('picUrl') or '').replace('http://', 'https://')
                if not pic: continue
                pic = pic + '?param=500y500'
                sn = safe_filename(f"{i+1:03d}. {t['artists']} - {t['name']}") + '.jpg'
                try:
                    r = requests.get(pic, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
                    r.raise_for_status(); (tmp_path / sn).write_bytes(r.content); success += 1
                except Exception: pass
            if success == 0: return APIResponse.error("所有封面获取失败", 500)
            zip_buf, zip_name = make_zip_response(tmp_path, album.get('name', 'album') + '_covers')
            resp = send_file(zip_buf, as_attachment=True, download_name=zip_name, mimetype='application/zip')
            resp.headers['X-Cover-Count'] = str(success); return resp
    except Exception as e:
        return APIResponse.error(f"批量封面下载失败: {str(e)}", 500)

@app.route('/album/lyric/batch', methods=['POST'])
def album_batch_download_lyric():
    try:
        data = api_service._safe_get_request_data()
        album_id = data.get('id')
        validation_error = api_service._validate_request_params({'album_id': album_id})
        if validation_error: return validation_error
        if 'playlist' in str(album_id): return APIResponse.error("检测到歌单链接，请切换到「歌单」Tab", 400)
        cookies = api_service._get_cookies()
        album = album_detail(album_id, cookies)
        if not album or not album.get('songs'): return APIResponse.error("获取专辑失败", 404)
        tracks = album['songs']
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir); success = 0
            for i, t in enumerate(tracks):
                try:
                    li = lyric_v1(t['id'], cookies)
                    lrc = li.get('lrc', {}).get('lyric', '')
                    if not lrc: continue
                    sn = safe_filename(f"{i+1:03d}. {t['artists']} - {t['name']}") + '.lrc'
                    (tmp_path / sn).write_text(lrc, encoding='utf-8'); success += 1
                except Exception: pass
            if success == 0: return APIResponse.error("所有歌词获取失败", 500)
            zip_buf, zip_name = make_zip_response(tmp_path, album.get('name', 'album') + '_lyrics')
            resp = send_file(zip_buf, as_attachment=True, download_name=zip_name, mimetype='application/zip')
            resp.headers['X-Lyric-Count'] = str(success); return resp
    except Exception as e:
        return APIResponse.error(f"批量歌词下载失败: {str(e)}", 500)
@app.route('/Album', methods=['GET', 'POST'])  # 向后兼容
def get_album():
    """获取专辑详情API"""
    try:
        # 获取请求参数
        data = api_service._safe_get_request_data()
        album_id = data.get('id')
        
        # 参数验证
        validation_error = api_service._validate_request_params({'album_id': album_id})
        if validation_error:
            return validation_error

        if 'playlist' in str(album_id):
            return APIResponse.error("检测到歌单链接，请切换到「歌单」Tab 查询", 400)

        if 'y.qq.com' in str(album_id):
            return APIResponse.error("检测到 QQ 音乐链接，请切换到 <a href='/qq'>QQ音乐工具箱</a>", 400)

        cookies = api_service._get_cookies()
        result = album_detail(album_id, cookies)

        return APIResponse.success({'album': result}, "获取专辑详情成功")
        
    except Exception as e:
        api_service.logger.error(f"获取专辑异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"获取专辑失败: {str(e)}", 500)


@app.route('/download', methods=['GET', 'POST'])
@app.route('/Download', methods=['GET', 'POST'])  # 向后兼容
def download_music_api():
    """下载音乐API"""
    try:
        # 获取请求参数
        data = api_service._safe_get_request_data()
        music_id = data.get('id')
        quality = data.get('quality', 'lossless')
        return_format = data.get('format', 'file')  # file 或 json
        
        # 参数验证
        validation_error = api_service._validate_request_params({'id': music_id})
        if validation_error:
            return validation_error
        
        # 验证音质参数
        valid_qualities = VALID_LEVELS
        if quality not in valid_qualities:
            return APIResponse.error(f"无效的音质参数，支持: {', '.join(valid_qualities)}")
        
        # 验证返回格式
        if return_format not in ['file', 'json']:
            return APIResponse.error("返回格式只支持 'file' 或 'json'")
        
        music_id = api_service._extract_music_id(music_id)
        cookies = api_service._get_cookies()
        
        # 获取音乐基本信息
        song_info = name_v1(music_id)
        if not song_info or 'songs' not in song_info or not song_info['songs']:
            return APIResponse.error("未找到音乐信息", 404)
        
        # 获取音乐下载链接
        url_info = url_v1(music_id, quality, cookies)
        if not url_info or 'data' not in url_info or not url_info['data'] or not url_info['data'][0].get('url'):
            return APIResponse.error("无法获取音乐下载链接，可能是版权限制或音质不支持", 404)
        
        # 构建音乐信息
        song_data = song_info['songs'][0]
        url_data = url_info['data'][0]
        
        music_info = {
            'id': music_id,
            'name': song_data['name'],
            'artist_string': ', '.join(artist['name'] for artist in song_data['ar']),
            'album': song_data['al']['name'],
            'pic_url': song_data['al']['picUrl'],
            'file_type': url_data['type'],
            'file_size': url_data['size'],
            'duration': song_data.get('dt', 0),
            'download_url': url_data['url']
        }
        
        # 生成安全文件名
        safe_name = f"{music_info['name']} [{quality}]"
        safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*')
        filename = f"{safe_name}.{music_info['file_type']}"
        
        file_path = api_service.downloads_path / filename
        
        # 检查文件是否已存在
        if file_path.exists():
            api_service.logger.info(f"文件已存在: {filename}")
        else:
            # 使用优化后的下载器下载
            try:
                download_result = api_service.downloader.download_music_file(
                    music_id, quality, cookies=cookies
                )
                
                if not download_result.success:
                    return APIResponse.error(f"下载失败: {download_result.error_message}", 500)
                
                file_path = Path(download_result.file_path)
                api_service.logger.info(f"下载完成: {filename}")
                
            except DownloadException as e:
                api_service.logger.error(f"下载异常: {e}")
                return APIResponse.error(f"下载失败: {str(e)}", 500)
        
        # 根据返回格式返回结果
        if return_format == 'json':
            response_data = {
                'music_id': music_id,
                'name': music_info['name'],
                'artist': music_info['artist_string'],
                'album': music_info['album'],
                'quality': quality,
                'quality_name': api_service._get_quality_display_name(quality),
                'file_type': music_info['file_type'],
                'file_size': music_info['file_size'],
                'file_size_formatted': api_service._format_file_size(music_info['file_size']),
                'file_path': str(file_path.absolute()),
                'filename': filename,
                'duration': music_info['duration']
            }
            return APIResponse.success(response_data, "下载完成")
        else:
            # 返回文件下载
            if not file_path.exists():
                return APIResponse.error("文件不存在", 404)
            
            try:
                response = send_file(
                    str(file_path),
                    as_attachment=True,
                    download_name=filename,
                    mimetype=f"audio/{music_info['file_type']}"
                )
                response.headers['X-Download-Message'] = 'Download completed successfully'
                response.headers['X-Download-Filename'] = quote(filename, safe='')
                return response
            except Exception as e:
                api_service.logger.error(f"发送文件失败: {e}")
                return APIResponse.error(f"文件发送失败: {str(e)}", 500)
            
    except Exception as e:
        api_service.logger.error(f"下载音乐异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"下载异常: {str(e)}", 500)


# ---------- 下载记录（SQLite 持久化） ----------

@app.route('/dl/history', methods=['GET'])
def dl_history_get():
    records = db.get_downloads(50)
    return APIResponse.success(records)

@app.route('/dl/history', methods=['POST'])
def dl_history_add():
    try:
        data = api_service._safe_get_request_data()
        name = data.get('name', '')
        song_id = data.get('song_id', '')
        quality = data.get('quality', '')
        if not name or not song_id:
            return APIResponse.error("缺少必要参数")
        db.add_download(name, song_id, quality, 'netease')
        return APIResponse.success(None, "记录已保存")
    except Exception as e:
        return APIResponse.error(f"保存失败: {str(e)}", 500)

@app.route('/dl/history', methods=['DELETE'])
def dl_history_clear():
    try:
        db.clear_downloads()
        return APIResponse.success(None, "记录已清空")
    except Exception as e:
        return APIResponse.error(f"清空失败: {str(e)}", 500)


# ---------- 搜索记录（SQLite 持久化） ----------

@app.route('/search/history', methods=['GET'])
def search_history_get():
    records = db.get_searches(50)
    return APIResponse.success(records)

@app.route('/search/history', methods=['DELETE'])
def search_history_clear():
    try:
        db.clear_searches()
        return APIResponse.success(None, "记录已清空")
    except Exception as e:
        return APIResponse.error(f"清空失败: {str(e)}", 500)


# ---------- 批量歌词 ----------
@app.route('/lyric/batch', methods=['POST'])
def batch_lyric():
    try:
        data = api_service._safe_get_request_data()
        ids_str = data.get('ids', '')
        if not ids_str:
            return APIResponse.error("缺少ids参数")
        song_ids = [int(x.strip()) for x in ids_str.split(',') if x.strip().isdigit()]
        if not song_ids:
            return APIResponse.error("无效的歌曲ID列表")
        cookies = api_service._get_cookies()
        result = {}
        with ThreadPoolExecutor(max_workers=min(5, len(song_ids))) as executor:
            f2id = {executor.submit(lyric_v1, sid, cookies): sid for sid in song_ids}
            for future in as_completed(f2id):
                sid = f2id[future]
                try:
                    li = future.result()
                    result[sid] = {
                        'lyric': li.get('lrc', {}).get('lyric', '') if li else '',
                        'tlyric': li.get('tlyric', {}).get('lyric', '') if li else ''
                    }
                except Exception:
                    result[sid] = {'lyric': '', 'tlyric': ''}
        return APIResponse.success(result)
    except Exception as e:
        return APIResponse.error(f"批量获取歌词失败: {str(e)}", 500)


@app.route('/api/info', methods=['GET'])
def api_info():
    """API信息接口"""
    try:
        info = {
            'name': '网易云音乐API服务',
            'version': '2.0.0',
            'description': '提供网易云音乐相关API服务',
            'endpoints': {
                '/health': 'GET - 健康检查',
                '/song': 'GET/POST - 获取歌曲信息',
                '/search': 'GET/POST - 搜索音乐',
                '/playlist': 'GET/POST - 获取歌单详情',
                '/playlist/batch': 'GET/POST - 批量获取歌单歌曲URL',
                '/playlist/download/batch/start': 'POST - 启动批量下载',
                '/playlist/download/batch/progress/<id>': 'GET - 查询下载进度',
                '/playlist/download/batch/result/<id>': 'GET - 获取下载ZIP',
                '/album': 'GET/POST - 获取专辑详情',
                '/lyric/download': 'GET/POST - 下载歌词(.lrc)',
                '/playlist/lyric/batch': 'POST - 批量下载歌词(ZIP)',
                '/cookie': 'GET/POST - Cookie管理',
                '/download': 'GET/POST - 下载音乐',
                '/api/info': 'GET - API信息'
            },
            'supported_qualities': [
                'standard', 'exhigh', 'lossless', 
                'hires', 'sky', 'dolby', 'jyeffect', 'jymaster'
            ],
            'config': {
                'downloads_dir': str(api_service.downloads_path.absolute()),
                'max_file_size': f"{config.max_file_size // (1024*1024)}MB",
                'request_timeout': f"{config.request_timeout}s"
            }
        }
        
        return APIResponse.success(info, "API信息获取成功")
        
    except Exception as e:
        api_service.logger.error(f"获取API信息异常: {e}")
        return APIResponse.error(f"获取API信息失败: {str(e)}", 500)


# ==================== QQ音乐 API 路由 ====================

def _parse_duration_ms(interval_str):
    """Convert 'm:ss' or 'mm:ss' format to milliseconds"""
    try:
        parts = interval_str.split(':')
        if len(parts) == 2:
            minutes = int(parts[0])
            seconds = int(parts[1])
            return (minutes * 60 + seconds) * 1000
    except (ValueError, AttributeError):
        pass
    return 0

def _adapt_song_detail(new_result):
    """Convert QQMusic.get_music_song() format → QQMusicAPI.get_song_detail() format"""
    if 'msg' in new_result:
        return {'songs': [], 'code': 404}
    singers = [{'name': name.strip()} for name in new_result.get('singer', '').split(',') if name.strip()]
    return {
        'songs': [{
            'id': str(new_result.get('id', '')),
            'mid': new_result.get('mid', ''),
            'name': new_result.get('name', ''),
            'ar': singers,
            'al': {
                'name': new_result.get('album', ''),
                'picUrl': new_result.get('pic', ''),
                'mid': '',
            },
            'dt': _parse_duration_ms(new_result.get('interval', '0:00')),
            'no': 0,
        }],
        'code': 200,
    }

def _adapt_lyric(new_result):
    """Convert QQMusic.get_music_lyric_new() format → QQMusicAPI.get_lyric() format"""
    if 'error' in new_result:
        return {'lrc': {'lyric': ''}, 'tlyric': {'lyric': ''}, 'code': 200}
    return {
        'lrc': {'lyric': new_result.get('lyric', '')},
        'tlyric': {'lyric': new_result.get('tylyric', '')},
        'code': 200,
    }

def _qq_get_cookies():
    try:
        cookie_str = qq_cookie_mgr.read_cookie()
        return qq_cookie_mgr.parse_cookie_string(cookie_str)
    except Exception as e:
        api_service.logger.warning(f"获取QQ Cookie失败: {e}")
        return {}

def _qq_prepare():
    cookies = _qq_get_cookies()
    cookies.pop('qrsig', None)
    qq_api.set_cookies('; '.join(f'{k}={v}' for k, v in cookies.items()) if cookies else '')
    return cookies

def _qq_get_quality_cn(code):
    return reverse_quality_map.get(code, code)

def _degrade_qq_quality(real_mid, requested_level):
    if requested_level not in QQ_QUALITY_DEGRADE_ORDER:
        requested_level = '128'
    start_idx = QQ_QUALITY_DEGRADE_ORDER.index(requested_level)
    for level in QQ_QUALITY_DEGRADE_ORDER[start_idx:]:
        result = qq_api.get_music_url(real_mid, level)
        if result and result.get('url'):
            return result, level, (level != requested_level)
    return None, None, False


@app.route('/qq')
def qq_index():
    return render_template('qq.html')


@app.route('/qq/health', methods=['GET'])
def qq_health_check():
    try:
        cookie_status = qq_cookie_mgr.is_cookie_valid()
        return APIResponse.success({
            'service': 'running',
            'platform': 'qq',
            'timestamp': int(time.time()),
            'cookie_status': 'valid' if cookie_status else 'invalid',
            'version': '1.0.0'
        }, "QQ音乐API服务运行正常")
    except Exception as e:
        return APIResponse.error(f"健康检查失败: {str(e)}", 500)


@app.route('/qq/cookie', methods=['GET', 'POST'])
def qq_cookie_manage():
    try:
        if request.method == 'GET':
            content = qq_cookie_mgr.read_cookie()
            info = qq_cookie_mgr.get_cookie_info()
            info['raw_content'] = content
            return APIResponse.success(info, "获取QQ Cookie信息成功")
        data = api_service._safe_get_request_data()
        new_cookie = (data.get('cookie') or '').strip()
        if not new_cookie:
            return APIResponse.error("cookie参数不能为空")
        try:
            qq_cookie_mgr.write_cookie(new_cookie)
            api_service.logger.info("QQ Cookie已通过API更新")
            return APIResponse.success(None, "QQ Cookie更新成功")
        except CookieException as e:
            return APIResponse.error(f"QQ Cookie更新失败: {e}")
    except Exception as e:
        return APIResponse.error(f"操作失败: {str(e)}", 500)


@app.route('/qq/qr-login/start', methods=['POST'])
def qq_qr_login_start():
    try:
        qq_api.cookies = {}
        result = qq_api.get_qr_code()
        if not result:
            return APIResponse.error("生成二维码失败", 500)
        return APIResponse.success({
            'qrsig': result['qrsig'],
            'qr_image': result['image'],
        }, "二维码已生成")
    except Exception as e:
        return APIResponse.error(f"生成失败: {str(e)}", 500)


@app.route('/qq/qr-login/check/<qrsig>', methods=['GET'])
def qq_qr_login_check(qrsig: str):
    try:
        ret = qq_api.check_qr_login(qrsig)
        if not isinstance(ret, tuple) or len(ret) < 3:
            return APIResponse.error("检查登录失败", 500)
        code, msg, cookies = ret[0], ret[1], ret[2]
        callback_url = ret[3] if len(ret) > 3 and ret[3] else ''

        if code == 0:
            final_cookies = dict(cookies)
            if callback_url:
                oauth = qq_api.exchange_callback(callback_url)
                final_cookies.update(oauth)
            final_cookies.pop('qrsig', None)
            cookie_str = '; '.join(f'{k}={v}' for k, v in final_cookies.items() if v)
            if cookie_str:
                try:
                    qq_cookie_mgr.write_cookie(cookie_str)
                    api_service.logger.info("QQ扫码登录成功，Cookie已保存")
                except Exception as e:
                    api_service.logger.error(f"保存QQ Cookie失败: {e}")
            return APIResponse.success({'code': code, 'status': '登录成功', 'cookie': cookie_str})
        elif code == 66:
            return APIResponse.success({'code': code, 'status': '等待扫码'})
        elif code == 67:
            return APIResponse.success({'code': code, 'status': '扫码成功，请在手机上确认'})
        elif code == 65:
            return APIResponse.success({'code': code, 'status': '二维码已过期'})
        else:
            return APIResponse.success({'code': code, 'status': msg or f'状态码: {code}'})
    except Exception as e:
        return APIResponse.error(f"检查登录状态失败: {str(e)}", 500)


@app.route('/qq/song', methods=['GET', 'POST'])
def qq_get_song_info():
    try:
        data = api_service._safe_get_request_data()
        song_id = data.get('ids') or data.get('id') or data.get('url')
        level = data.get('level', 'flac')
        info_type = data.get('type', 'url')

        if not song_id:
            return APIResponse.error("必须提供 ids、id 或 url 参数")

        songmid = qq_api.ids(song_id) or song_id.strip()

        if level not in QQ_VALID_LEVELS:
            return APIResponse.error(f"无效的音质参数，支持: {', '.join(QQ_VALID_LEVELS[:6])} ...")

        _qq_prepare()

        if info_type == 'url':
            raw = qq_api.get_music_song(songmid, 0)
            song_info = _adapt_song_detail(raw)
            if not song_info or not song_info.get('songs'):
                return APIResponse.error("未找到歌曲信息", 404)
            real_mid = song_info['songs'][0].get('mid', songmid)
            url_result, actual_level, degraded = _degrade_qq_quality(real_mid, level)
            if url_result and url_result.get('url'):
                ext = qq_api.file_config.get(actual_level, {}).get('e', '.mp3')
                return APIResponse.success({
                    'id': songmid,
                    'url': url_result['url'],
                    'bitrate': url_result.get('bitrate', ''),
                    'ext': ext,
                    'level': actual_level,
                    'quality_name': _qq_get_quality_cn(actual_level),
                    'degraded': degraded,
                }, "获取QQ歌曲URL成功")
            return APIResponse.error("获取QQ音乐URL失败，可能需要VIP或音质不支持", 404)

        elif info_type == 'name':
            raw = qq_api.get_music_song(songmid, 0)
            result = _adapt_song_detail(raw)
            return APIResponse.success(result, "获取QQ歌曲信息成功")

        elif info_type == 'lyric':
            raw = qq_api.get_music_song(songmid, 0)
            song_detail = _adapt_song_detail(raw)
            sid = None
            if song_detail and song_detail.get('songs'):
                sid = int(song_detail['songs'][0].get('id', 0))
            if sid:
                raw_lyric = qq_api.get_music_lyric_new(sid)
                result = _adapt_lyric(raw_lyric)
            else:
                result = {'lrc': {'lyric': ''}, 'tlyric': {'lyric': ''}, 'code': 200}
            return APIResponse.success(result, "获取QQ歌词成功")

        elif info_type == 'json':
            raw = qq_api.get_music_song(songmid, 0)
            song_info = _adapt_song_detail(raw)
            if not song_info or not song_info.get('songs'):
                return APIResponse.error("未找到歌曲信息", 404)
            song_data = song_info['songs'][0]
            real_mid = song_data.get('mid', songmid)
            sid = int(song_data.get('id', 0))

            url_result, actual_level, degraded = _degrade_qq_quality(real_mid, level)
            raw_lyric = qq_api.get_music_lyric_new(sid) if sid else {}
            lyric_info = _adapt_lyric(raw_lyric)

            # Extract pay info from raw API response
            payplay = (raw.get('pay_play', 0) if isinstance(raw, dict) else 0) or 0

            response_data = {
                'id': songmid,
                'name': song_data.get('name', ''),
                'ar_name': ', '.join(a['name'] for a in song_data.get('ar', [])),
                'al_name': song_data.get('al', {}).get('name', ''),
                'pic': song_data.get('al', {}).get('picUrl', ''),
                'level': actual_level,
                'lyric': lyric_info.get('lrc', {}).get('lyric', ''),
                'tlyric': lyric_info.get('tlyric', {}).get('lyric', ''),
                'degraded': degraded,
                'payplay': payplay,
            }
            if url_result and url_result.get('url'):
                response_data.update({
                    'url': url_result['url'],
                    'size': '',
                    'level': actual_level,
                })
            else:
                response_data.update({'url': '', 'size': '获取失败'})

            return APIResponse.success(response_data, "获取QQ歌曲信息成功")

    except Exception as e:
        api_service.logger.error(f"获取QQ歌曲信息异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"服务器错误: {str(e)}", 500)


@app.route('/qq/search', methods=['GET', 'POST'])
def qq_search_music_api():
    try:
        data = api_service._safe_get_request_data()
        keyword = data.get('keyword') or data.get('keywords') or data.get('q')
        limit = min(int(data.get('limit', 30)), 100)
        search_type = data.get('type', '1')

        validation_error = api_service._validate_request_params({'keyword': keyword})
        if validation_error:
            return validation_error

        SEARCH_T_MAP = {'1': 0, '10': 8, '1004': 12, '2': 7}
        t = SEARCH_T_MAP.get(search_type, 0)

        raw = qq_api.search_music(keyword, limit, search_type=t)
        if raw.get('code') != 0:
            return APIResponse.error(raw.get('message', '搜索失败'), 500)

        dk = {0: 'song', 8: 'album', 12: 'mv', 7: 'lyric'}
        data_key = dk.get(t, 'song')
        items = raw.get('data', {}).get(data_key, {}).get('list', [])

        result = []
        for item in items:
            if t == 0:  # 歌曲
                singers = '/'.join(s.get('name', '') for s in item.get('singer', []))
                pay = item.get('pay', {}) or {}
                result.append({
                    'id': item.get('songmid', ''),
                    'songid': item.get('songid'),
                    'name': item.get('songname', ''),
                    'artists': singers,
                    'album': item.get('albumname', ''),
                    'albummid': item.get('albummid', ''),
                    'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{item.get('albummid', '')}.jpg",
                    'interval': item.get('interval', 0),
                    'duration': (item.get('interval', 0) or 0) * 1000,
                    'sizeflac': item.get('sizeflac', 0),
                    'size320': item.get('size320', 0),
                    'size128': item.get('size128', 0),
                    'payplay': pay.get('payplay', 0),
                })
            elif t == 8:  # 专辑
                singers = '/'.join(s.get('name', '') for s in item.get('singer_list', []))
                result.append({
                    'id': item.get('albumMID', ''),
                    'name': item.get('albumName', ''),
                    'artists': singers,
                    'album': item.get('singerName', ''),
                    'picUrl': item.get('albumPic', ''),
                    'song_count': item.get('song_count', 0),
                    'public_time': item.get('publicTime', ''),
                })
            elif t == 12:  # MV
                result.append({
                    'id': item.get('v_id', ''),
                    'name': item.get('mv_name', ''),
                    'artists': item.get('singer_name', ''),
                    'picUrl': item.get('mv_pic_url', ''),
                    'duration': item.get('duration', 0),
                    'play_count': item.get('play_count', 0),
                })
            elif t == 7:  # 歌词
                singers = '/'.join(s.get('name', '') for s in item.get('singer', []))
                result.append({
                    'id': item.get('songmid', ''),
                    'songid': item.get('songid'),
                    'name': item.get('songname', ''),
                    'artists': singers,
                    'album': item.get('albumname', ''),
                    'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{item.get('albummid', '')}.jpg",
                    'interval': item.get('interval', 0),
                    'lyric_preview': item.get('content', '')[:200],
                })

        db.add_qq_search(keyword, search_type)
        return APIResponse.success(result, "搜索完成")
    except Exception as e:
        api_service.logger.error(f"QQ搜索异常: {e}")
        return APIResponse.error(f"搜索失败: {str(e)}", 500)


@app.route('/qq/playlist/batch', methods=['GET', 'POST'])
def qq_playlist_batch():
    try:
        data = api_service._safe_get_request_data()
        pid = data.get('id', '').strip()
        level = data.get('level', 'flac')
        if not pid:
            return APIResponse.error("必须提供歌单ID或链接")

        import re
        m = re.search(r'playlist/(\d+)', pid)
        if m:
            pid = m.group(1)

        if level not in QQ_VALID_LEVELS:
            return APIResponse.error("无效的音质参数")

        raw = qq_api.get_playlist_detail(pid, num=200)
        if raw.get('code') != 0:
            return APIResponse.error('获取歌单失败', 404)

        cdlist = raw.get('data', {}).get('cdlist', [])
        if not cdlist:
            return APIResponse.error('歌单不存在或为空', 404)
        cd = cdlist[0]

        songs = cd.get('songlist', [])
        total = len(songs)
        resolved = 0
        tracks = [None] * total

        _qq_prepare()

        # Build track base info and collect mids for parallel resolution
        mids_to_resolve = []
        for i, s in enumerate(songs):
            singers = '/'.join(sg.get('name', '') for sg in s.get('singer', []))
            songmid = s.get('songmid', '')
            pay = s.get('pay', {}) or {}
            track = {
                'id': songmid,
                'songid': s.get('songid', ''),
                'name': s.get('songname', ''),
                'artists': singers,
                'album': s.get('albumname', ''),
                'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{s.get('albummid', '')}.jpg",
                'interval': s.get('interval', 0),
                'duration': (s.get('interval', 0) or 0) * 1000,
                'url': '', 'level': level, 'quality_name': '获取失败',
                'degraded': False, 'ext': '', 'size': 0, 'size_formatted': '-', 'br': 0,
                'payplay': pay.get('payplay', 0),
            }
            tracks[i] = track
            if songmid:
                mids_to_resolve.append((i, songmid))

        # Parallel resolve URLs
        with ThreadPoolExecutor(max_workers=min(8, len(mids_to_resolve))) as executor:
            f2idx = {executor.submit(_degrade_qq_quality, mid, level): idx for idx, mid in mids_to_resolve}
            for future in as_completed(f2idx):
                idx = f2idx[future]
                try:
                    url_result, actual_level, degraded = future.result()
                    if url_result and url_result.get('url'):
                        ext = qq_api.file_config.get(actual_level, {}).get('e', '.mp3')
                        tracks[idx].update({
                            'url': url_result['url'],
                            'level': actual_level,
                            'quality_name': _qq_get_quality_cn(actual_level),
                            'degraded': degraded,
                            'ext': ext,
                        })
                        resolved += 1
                except Exception:
                    pass

        info = {
            'id': cd.get('disstid', pid),
            'name': cd.get('dissname', ''),
            'creator': cd.get('nickname', ''),
            'coverImgUrl': cd.get('logo', ''),
            'trackCount': cd.get('songnum', total),
        }
        return APIResponse.success({'playlist': info, 'tracks': tracks, 'resolved': resolved, 'total': total}, '批量解析完成')
    except Exception as e:
        api_service.logger.error(f"QQ歌单批量解析异常: {e}")
        return APIResponse.error(f"批量解析失败: {str(e)}", 500)


@app.route('/qq/playlist', methods=['GET', 'POST'])
def qq_playlist_detail():
    try:
        data = api_service._safe_get_request_data()
        pid = data.get('id', '').strip()
        if not pid:
            return APIResponse.error("必须提供歌单ID或链接")

        # Extract dissid from URL or direct ID
        import re
        m = re.search(r'playlist/(\d+)', pid)
        if m:
            pid = m.group(1)

        raw = qq_api.get_playlist_detail(pid)
        if raw.get('code') != 0:
            return APIResponse.error(raw.get('message', '获取歌单失败'), 404)

        cdlist = raw.get('data', {}).get('cdlist', [])
        if not cdlist:
            return APIResponse.error('歌单不存在或为空', 404)

        cd = cdlist[0]
        songs = []
        for s in cd.get('songlist', []):
            singers = '/'.join(sg.get('name', '') for sg in s.get('singer', []))
            songs.append({
                'id': s.get('songmid', ''),
                'songid': s.get('songid', ''),
                'name': s.get('songname', ''),
                'artists': singers,
                'album': s.get('albumname', ''),
                'albummid': s.get('albummid', ''),
                'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{s.get('albummid', '')}.jpg",
                'interval': s.get('interval', 0),
            })

        info = {
            'id': cd.get('disstid', pid),
            'name': cd.get('dissname', ''),
            'creator': cd.get('nickname', ''),
            'cover': cd.get('logo', ''),
            'desc': cd.get('desc', ''),
            'song_count': cd.get('songnum', len(songs)),
            'play_count': cd.get('visitnum', 0),
        }
        return APIResponse.success({'info': info, 'songs': songs}, '获取歌单成功')
    except Exception as e:
        api_service.logger.error(f"QQ歌单详情异常: {e}")
        return APIResponse.error(f"获取失败: {str(e)}", 500)


@app.route('/qq/album', methods=['GET', 'POST'])
def qq_album_detail():
    try:
        data = api_service._safe_get_request_data()
        aid = data.get('id', '').strip()
        if not aid:
            return APIResponse.error("必须提供专辑ID或链接")

        import re
        m = re.search(r'albumDetail/(\w+)', aid)
        if m:
            aid = m.group(1)

        raw = qq_api.get_album_detail(aid)
        if raw.get('code') != 0:
            return APIResponse.error('获取专辑失败', 404)

        info = raw.get('data', {})
        songs = []
        for s in info.get('list', []):
            singers = '/'.join(sg.get('name', '') for sg in s.get('singer', []))
            songs.append({
                'id': s.get('songmid', ''),
                'songid': s.get('songid', ''),
                'name': s.get('songname', ''),
                'artists': singers,
                'album': info.get('name', ''),
                'albummid': s.get('albummid', ''),
                'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{info.get('mid', '')}.jpg",
                'interval': s.get('interval', 0),
            })

        return APIResponse.success({
            'album': {
                'id': info.get('mid', aid),
                'name': info.get('name', ''),
                'artist': info.get('singername', ''),
                'coverImgUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{info.get('mid', '')}.jpg",
                'desc': info.get('desc', ''),
                'company': info.get('company', ''),
                'aDate': info.get('aDate', ''),
                'songCount': info.get('cur_song_num', len(songs)),
            },
            'songs': songs,
        }, '获取专辑成功')
    except Exception as e:
        api_service.logger.error(f"QQ专辑详情异常: {e}")
        return APIResponse.error(f"获取失败: {str(e)}", 500)


@app.route('/qq/album/batch', methods=['GET', 'POST'])
def qq_album_batch():
    try:
        data = api_service._safe_get_request_data()
        aid = data.get('id', '').strip()
        level = data.get('level', 'flac')
        if not aid:
            return APIResponse.error("必须提供专辑ID或链接")

        import re
        m = re.search(r'albumDetail/(\w+)', aid)
        if m:
            aid = m.group(1)

        if level not in QQ_VALID_LEVELS:
            return APIResponse.error("无效的音质参数")

        raw = qq_api.get_album_detail(aid)
        if raw.get('code') != 0:
            return APIResponse.error('获取专辑失败', 404)

        info = raw.get('data', {})
        songlist = info.get('list', [])
        total = len(songlist)
        resolved = 0
        tracks = [None] * total

        _qq_prepare()

        mids_to_resolve = []
        for i, s in enumerate(songlist):
            singers = '/'.join(sg.get('name', '') for sg in s.get('singer', []))
            songmid = s.get('songmid', '')
            pay = s.get('pay', {}) or {}
            track = {
                'id': songmid,
                'songid': s.get('songid', ''),
                'name': s.get('songname', ''),
                'artists': singers,
                'album': info.get('name', ''),
                'picUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{info.get('mid', '')}.jpg",
                'interval': s.get('interval', 0),
                'duration': (s.get('interval', 0) or 0) * 1000,
                'url': '', 'level': level, 'quality_name': '获取失败',
                'degraded': False, 'ext': '', 'size': 0, 'size_formatted': '-', 'br': 0,
                'payplay': pay.get('payplay', 0),
            }
            tracks[i] = track
            if songmid:
                mids_to_resolve.append((i, songmid))

        with ThreadPoolExecutor(max_workers=min(8, len(mids_to_resolve))) as executor:
            f2idx = {executor.submit(_degrade_qq_quality, mid, level): idx for idx, mid in mids_to_resolve}
            for future in as_completed(f2idx):
                idx = f2idx[future]
                try:
                    url_result, actual_level, degraded = future.result()
                    if url_result and url_result.get('url'):
                        ext = qq_api.file_config.get(actual_level, {}).get('e', '.mp3')
                        tracks[idx].update({
                            'url': url_result['url'],
                            'level': actual_level,
                            'quality_name': _qq_get_quality_cn(actual_level),
                            'degraded': degraded,
                            'ext': ext,
                        })
                        resolved += 1
                except Exception:
                    pass

        result_info = {
            'id': info.get('mid', aid),
            'name': info.get('name', ''),
            'artist': info.get('singername', ''),
            'coverImgUrl': f"https://y.qq.com/music/photo_new/T002R300x300M000{info.get('mid', '')}.jpg",
            'trackCount': info.get('cur_song_num', total),
        }
        return APIResponse.success({'album': result_info, 'tracks': tracks, 'resolved': resolved, 'total': total}, '批量解析完成')
    except Exception as e:
        api_service.logger.error(f"QQ专辑批量解析异常: {e}")
        return APIResponse.error(f"批量解析失败: {str(e)}", 500)


@app.route('/qq/download', methods=['GET', 'POST'])
def qq_download_music_api():
    try:
        data = api_service._safe_get_request_data()
        music_id = data.get('id')
        quality = data.get('quality', 'flac')

        validation_error = api_service._validate_request_params({'id': music_id})
        if validation_error:
            return validation_error

        if quality not in QQ_VALID_LEVELS:
            return APIResponse.error("无效的音质参数")

        songmid = qq_api.ids(music_id) or music_id.strip()
        _qq_prepare()

        raw = qq_api.get_music_song(songmid, 0)
        song_info = _adapt_song_detail(raw)
        if not song_info or not song_info.get('songs'):
            return APIResponse.error("未找到QQ音乐信息", 404)
        real_mid = song_info['songs'][0].get('mid', songmid)

        url_result, actual_quality, degraded = _degrade_qq_quality(real_mid, quality)
        if not url_result or not url_result.get('url'):
            return APIResponse.error("无法获取QQ音乐下载链接，所有音质均不可用", 404)

        song_data = song_info['songs'][0]
        ext = qq_api.file_config.get(actual_quality, {}).get('e', '.mp3')
        music_info = {
            'id': songmid,
            'name': song_data['name'],
            'artist_string': ', '.join(a['name'] for a in song_data['ar']),
            'album': song_data['al']['name'],
            'pic_url': song_data['al']['picUrl'],
            'file_type': ext.lstrip('.'),
            'file_size': 0,
            'duration': song_data.get('dt', 0),
            'download_url': url_result['url']
        }

        fn_fmt = data.get('filename_format', '{title} [{quality}]')
        safe_artist = ''.join(c for c in music_info['artist_string'] if c not in r'<>:"/\|?*')
        safe_title = ''.join(c for c in music_info['name'] if c not in r'<>:"/\|?*')
        safe_album = ''.join(c for c in music_info['album'] if c not in r'<>:"/\|?*')
        safe_name = fn_fmt.replace('{artist}', safe_artist).replace('{title}', safe_title).replace('{album}', safe_album).replace('{quality}', actual_quality)
        safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*')
        safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*')
        filename = f"{safe_name}.{music_info['file_type']}"
        file_path = qq_downloader.download_dir / filename

        if not file_path.exists():
            try:
                dl_url = music_info['download_url']
                api_service.logger.info(f"QQ下载: {filename} <- {dl_url[:60]}...")
                resp = requests.get(dl_url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                }, timeout=300, stream=True, verify=False)
                resp.raise_for_status()
                file_path.parent.mkdir(parents=True, exist_ok=True)
                with open(file_path, 'wb') as f:
                    for chunk in resp.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                api_service.logger.info(f"QQ下载完成: {filename}")
                db.add_qq_download(music_info['name'], music_id, actual_quality)
            except Exception as e:
                api_service.logger.error(f"QQ下载失败: {e}")
                return APIResponse.error(f"下载失败: {str(e)}", 500)

        return_format = data.get('format', 'file')
        if return_format == 'json':
            return APIResponse.success({
                'music_id': songmid,
                'name': music_info['name'],
                'artist': music_info['artist_string'],
                'album': music_info['album'],
                'quality': actual_quality,
                'quality_name': _qq_get_quality_cn(actual_quality),
                'file_type': music_info['file_type'],
                'file_size': music_info['file_size'],
                'file_size_formatted': '0B',
                'file_path': str(file_path.absolute()),
                'filename': filename,
                'duration': music_info['duration'],
                'degraded': degraded,
            }, "下载完成")
        else:
            if not file_path.exists():
                return APIResponse.error("文件不存在", 404)
            response = send_file(
                str(file_path), as_attachment=True, download_name=filename,
                mimetype=f"audio/{music_info['file_type']}"
            )
            return response

    except Exception as e:
        api_service.logger.error(f"QQ下载异常: {e}")
        return APIResponse.error(f"下载异常: {str(e)}", 500)


@app.route('/qq/lyric/download', methods=['GET', 'POST'])
def qq_download_lyric():
    try:
        data = api_service._safe_get_request_data()
        song_id = data.get('id')
        validation_error = api_service._validate_request_params({'song_id': song_id})
        if validation_error:
            return validation_error

        songmid = qq_api.ids(song_id) or song_id.strip()
        _qq_prepare()

        raw = qq_api.get_music_song(songmid, 0)
        song_detail = _adapt_song_detail(raw)
        sid = None
        if song_detail and song_detail.get('songs'):
            sid = int(song_detail['songs'][0].get('id', 0))
        if not sid:
            return APIResponse.error("无法获取歌曲ID", 404)

        lyric_info = _adapt_lyric(qq_api.get_music_lyric_new(sid))
        lrc = lyric_info.get('lrc', {}).get('lyric', '')
        if not lrc:
            return APIResponse.error("未找到歌词", 404)

        song_name = 'lyric'
        if song_detail and song_detail.get('songs'):
            s = song_detail['songs'][0]
            ar = '/'.join(a['name'] for a in s.get('ar', []))
            song_name = f"{ar} - {s.get('name', '')}"
        safe_name = ''.join(c for c in song_name if c not in r'<>:"/\|?*') + '.lrc'
        resp = make_response(lrc)
        resp.headers['Content-Type'] = 'text/plain; charset=utf-8'
        resp.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
        return resp
    except Exception as e:
        return APIResponse.error(f"下载歌词失败: {str(e)}", 500)


@app.route('/qq/cover/download', methods=['GET', 'POST'])
def qq_download_cover():
    try:
        data = api_service._safe_get_request_data()
        song_id = data.get('id')
        validation_error = api_service._validate_request_params({'song_id': song_id})
        if validation_error:
            return validation_error

        songmid = qq_api.ids(song_id) or song_id.strip()
        _qq_prepare()

        raw = qq_api.get_music_song(songmid, 0)
        song_detail = _adapt_song_detail(raw)
        if not song_detail or not song_detail.get('songs'):
            return APIResponse.error("未找到歌曲信息", 404)
        s = song_detail['songs'][0]
        pic_url = s.get('al', {}).get('picUrl', '')
        if not pic_url:
            return APIResponse.error("未找到封面图片", 404)
        resp_cover = requests.get(pic_url, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
        resp_cover.raise_for_status()
        ar = '/'.join(a['name'] for a in s.get('ar', []))
        safe_name = ''.join(c for c in f"{ar} - {s.get('name', '')}" if c not in r'<>:"/\|?*') + '.jpg'
        response = make_response(resp_cover.content)
        response.headers['Content-Type'] = 'image/jpeg'
        response.headers['Content-Disposition'] = f"attachment; filename*=UTF-8''{quote(safe_name, safe='')}"
        return response
    except Exception as e:
        return APIResponse.error(f"下载封面失败: {str(e)}", 500)


@app.route('/qq/dl/history', methods=['GET'])
def qq_dl_history_get():
    records = db.get_qq_downloads(50)
    return APIResponse.success(records)

@app.route('/qq/dl/history', methods=['POST'])
def qq_dl_history_add():
    try:
        data = api_service._safe_get_request_data()
        name = data.get('name', '')
        song_id = data.get('song_id', '')
        quality = data.get('quality', '')
        if not name or not song_id:
            return APIResponse.error("缺少必要参数")
        db.add_qq_download(name, song_id, quality)
        return APIResponse.success(None, "记录已保存")
    except Exception as e:
        return APIResponse.error(f"保存失败: {str(e)}", 500)

@app.route('/qq/dl/history', methods=['DELETE'])
def qq_dl_history_clear():
    try:
        db.clear_qq_downloads()
        return APIResponse.success(None, "记录已清空")
    except Exception as e:
        return APIResponse.error(f"清空失败: {str(e)}", 500)


def start_api_server():
    """启动API服务器"""
    try:
        print("\n" + "="*60)
        print("🚀 网易云音乐API服务启动中...")
        print("="*60)
        print(f"📡 服务地址: http://{config.host}:{config.port}")
        print(f"📁 下载目录: {api_service.downloads_path.absolute()}")
        print(f"📋 日志级别: {config.log_level}")
        print("\n📚 API端点:")
        print(f"  ├─ GET  /health        - 健康检查")
        print(f"  ├─ POST /song          - 获取歌曲信息")
        print(f"  ├─ POST /search        - 搜索音乐")
        print(f"  ├─ POST /playlist      - 获取歌单详情")
        print(f"  ├─ POST /playlist/batch- 批量获取歌单歌曲URL")
        print(f"  ├─ POST /album         - 获取专辑详情")
        print(f"  ├─ POST /lyric/download- 下载歌词")
        print(f"  ├─ POST /cookie        - Cookie管理")
        print(f"  └─ GET  /api/info      - API信息")
        print("\n🎵 支持的音质:")
        print(f"  standard, exhigh, lossless, hires, sky, jyeffect, jymaster")
        print("="*60)
        print(f"⏰ 启动时间: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        print("🌟 服务已就绪，等待请求...\n")
        
        # 启动Flask应用
        app.run(
            host=config.host,
            port=config.port,
            debug=config.debug,
            threaded=True
        )
        
    except KeyboardInterrupt:
        print("\n\n👋 服务已停止")
    except Exception as e:
        api_service.logger.error(f"启动服务失败: {e}")
        print(f"❌ 启动失败: {e}")
        sys.exit(1)


if __name__ == '__main__':
    start_api_server()

