"""网易云音乐API服务主程序

提供网易云音乐相关API服务，包括：
- 歌曲信息获取
- 音乐搜索
- 歌单和专辑详情
- 音乐下载
- 健康检查
"""

import logging
import json
import re
import sys
import time
import threading
import uuid
import traceback
import zipfile
import shutil
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
        playlist_detail, album_detail, batch_song_urls
    )
    from cookie_manager import CookieManager, CookieException
    from music_downloader import MusicDownloader, DownloadException
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
            'message': message
        }
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
        logger = logging.getLogger('music_api')
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
        try:
            # 处理短链接
            if '163cn.tv' in id_or_url:
                response = requests.get(id_or_url, allow_redirects=False, timeout=10)
                id_or_url = response.headers.get('Location', id_or_url)
            
            # 处理网易云链接
            if 'music.163.com' in id_or_url:
                index = id_or_url.find('id=') + 3
                if index > 2:
                    return id_or_url[index:].split('&')[0]
            
            # 直接返回ID
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
app = Flask(__name__)
api_service = MusicAPIService(config)


def _format_speed(bytes_per_sec: int) -> str:
    """格式化下载速度"""
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def _format_eta(seconds: float) -> str:
    """格式化预计剩余时间"""
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds) // 60}分{int(seconds) % 60}秒"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}时{m}分"


def _merge_translation_lyric(lrc: str, tlyric: str) -> str:
    """合并翻译歌词"""
    if not tlyric:
        return lrc

    def parse_time_tag(line: str):
        m = re.match(r'\[(\d+):(\d+[\.:]?\d*)\]', line)
        if m:
            return int(m.group(1)) * 60 + float(m.group(2).replace(':', '.'))
        return None

    lrc_lines = lrc.strip().split('\n')
    tlyric_lines = tlyric.strip().split('\n')
    tlyric_map = {}
    for line in tlyric_lines:
        t = parse_time_tag(line)
        if t is not None:
            text = re.sub(r'\[\d+:\d+[\.:]?\d*\]', '', line).strip()
            tlyric_map[t] = text
    result = []
    for line in lrc_lines:
        t = parse_time_tag(line)
        text = re.sub(r'\[\d+:\d+[\.:]?\d*\]', '', line).strip()
        if t is not None and t in tlyric_map and tlyric_map[t]:
            tag = re.match(r'(\[\d+:\d+[\.:]?\d*\])', line)
            if tag:
                result.append(f"{tag.group(1)}{text} (翻译: {tlyric_map[t]})")
                continue
        result.append(line)
    return '\n'.join(result)


class BatchTaskManager:
    """批量下载任务管理器（多线程）"""

    def __init__(self, downloader):
        self.downloader = downloader
        self.tasks: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5)

    def create_task(self, tracks: List[Dict], playlist_info: Dict, level: str, cookies: Dict) -> str:
        task_id = str(uuid.uuid4())[:8]
        task = {
            'task_id': task_id, 'status': 'running',
            'total': len(tracks), 'completed': 0, 'failed': 0, 'success': 0,
            'current_file': '', 'current_index': 0,
            'downloaded_bytes': 0, 'total_bytes': 0, 'speed': 0,
            'errors': [], 'files': [], 'playlist_info': playlist_info,
            'level': level, 'cookies': cookies,
            '_pre_sizes': {}, 'start_time': time.time(),
        }
        with self.lock:
            self.tasks[task_id] = task
        self.executor.submit(self._run_download, task_id, tracks)
        return task_id

    def _run_download(self, task_id: str, tracks: List[Dict]):
        task = self.tasks.get(task_id)
        if not task:
            return
        level = task['level']
        task['_cancelled'] = False
        downloader = self.downloader
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            with ThreadPoolExecutor(max_workers=3) as dl_executor:
                futures = []
                for i, track in enumerate(tracks):
                    sid = track['id']
                    safe_name = f"{i + 1:03d}. {track['artists']} - {track['name']}"
                    safe_name = ''.join(c for c in safe_name if c not in r'<>:"/\|?*')
                    future = dl_executor.submit(
                        self._download_one, task_id, i, track, sid, safe_name, level, tmp_path, downloader)
                futures.append(future)
            for future in as_completed(futures):
                if task.get('_cancelled'):
                    break
                try:
                    future.result()
                except Exception:
                        pass
            with self.lock:
                t = self.tasks.get(task_id)
                if t is None:
                    return
                if t['success'] > 0:
                    zip_buffer = BytesIO()
                    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                        for f in sorted(tmp_path.iterdir()):
                            zf.write(str(f), f.name)
                    t['zip_buffer'] = zip_buffer
                    t['zip_filename'] = f"{t['playlist_info'].get('name', 'playlist')}.zip"
                    t['zip_filename'] = ''.join(c for c in t['zip_filename'] if c not in r'<>:"/\|?*')
                    t['status'] = 'completed'
                else:
                    t['status'] = 'failed'

    def _download_one(self, task_id, idx, track, sid, safe_name, level, tmp_path, downloader):
        t = self.tasks.get(task_id)
        if t is None:
            return False, None
        with self.lock:
            t['current_index'] = idx + 1
            t['current_file'] = f"{track['artists']} - {track['name']}"

        def progress_cb(downloaded, total_size, speed):
            with self.lock:
                tsk = self.tasks.get(task_id)
                if tsk is None:
                    return
                tsk['speed'] = speed
                prev = tsk.get(f'_lb_{idx}', 0)
                if downloaded > prev:
                    tsk['downloaded_bytes'] = tsk.get('downloaded_bytes', 0) + (downloaded - prev)
                    tsk[f'_lb_{idx}'] = downloaded
                ps = tsk.get('_pre_sizes', {})
                ps[idx] = total_size
                tsk['_pre_sizes'] = ps
                tsk['total_bytes'] = max(tsk['total_bytes'], sum(ps.values()))
                fp = tsk.get('_file_progress', {})
                fp[idx] = {'name': f"{track['artists']} - {track['name']}", 'downloaded': downloaded, 'total': total_size, 'status': 'downloading'}
                tsk['_file_progress'] = fp

        try:
            result = downloader.download_music_file(sid, level, progress_callback=progress_cb)
            if result.success and result.file_path:
                src = Path(result.file_path)
                dst = tmp_path / f"{safe_name}{src.suffix}"
                shutil.copy2(str(src), str(dst))
                fs = dst.stat().st_size
                with self.lock:
                    tsk = self.tasks.get(task_id)
                    if tsk:
                        tsk['success'] += 1
                        tsk['completed'] += 1
                        tsk['files'].append({'name': f"{safe_name}{src.suffix}", 'size': fs})
                        fp = tsk.get('_file_progress', {})
                        if idx in fp: fp[idx]['status'] = 'done'
                return True, result
            else:
                err_msg = result.error_message or '未知错误'
                with self.lock:
                    tsk = self.tasks.get(task_id)
                    if tsk:
                        tsk['failed'] += 1
                        tsk['completed'] += 1
                        tsk['errors'].append({'index': idx + 1, 'name': track['name'], 'reason': err_msg})
                        fp = tsk.get('_file_progress', {})
                        if idx in fp: fp[idx]['status'] = 'failed'
                return False, result
        except Exception as e:
            with self.lock:
                tsk = self.tasks.get(task_id)
                if tsk:
                    tsk['failed'] += 1
                    tsk['completed'] += 1
                    tsk['errors'].append({'index': idx + 1, 'name': track['name'], 'reason': str(e)})
            return False, None

    def get_progress(self, task_id: str) -> Optional[Dict]:
        task = self.tasks.get(task_id)
        if not task:
            return None
        with self.lock:
            elapsed = time.time() - task['start_time']
            avg_speed = int(task['downloaded_bytes'] / elapsed) if elapsed > 0 else 0
            remaining_bytes = max(0, task['total_bytes'] - task['downloaded_bytes'])
            eta = remaining_bytes / avg_speed if avg_speed > 0 and task['total_bytes'] > 0 else -1
            return {
                'task_id': task['task_id'], 'status': task['status'],
                'total': task['total'], 'completed': task['completed'],
                'success': task['success'], 'failed': task['failed'],
                'current_index': task['current_index'], 'current_file': task['current_file'],
                'downloaded_bytes': task['downloaded_bytes'], 'total_bytes': task['total_bytes'],
                'speed': task['speed'], 'errors': task['errors'],
                'speed_formatted': _format_speed(task['speed']),
                'avg_speed_formatted': _format_speed(avg_speed),
                'eta_formatted': _format_eta(eta),
                'percent': round(task['completed'] / task['total'] * 100, 1) if task['total'] > 0 else 0,
                'files_progress': list(task.get('_file_progress', {}).values()),
            }

    def get_result(self, task_id: str) -> Optional[tuple]:
        task = self.tasks.get(task_id)
        if not task or task['status'] != 'completed':
            return None
        return task.get('zip_buffer'), task.get('zip_filename'), task

    def cleanup(self, task_id: str):
        self.tasks.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        task = self.tasks.get(task_id)
        if task and task['status'] == 'running':
            task['_cancelled'] = True
            task['status'] = 'cancelled'
            return True
        return False


batch_task_mgr = BatchTaskManager(api_service.downloader)
qr_manager = QRLoginManager()

VALID_LEVELS = ['standard', 'exhigh', 'lossless', 'hires', 'sky', 'jyeffect', 'jymaster']
ILLEGAL_CHARS = r'<>:"/\|?*'


def safe_filename(name: str) -> str:
    return ''.join(c for c in (name or 'file') if c not in ILLEGAL_CHARS)


def make_zip_response(files_dir: Path, zip_name: str) -> Tuple[BytesIO, str]:
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files_dir.iterdir()):
            zf.write(str(f), f.name)
    zip_buf.seek(0)
    return zip_buf, safe_filename(zip_name) + '.zip'


def get_playlist_or_fail(playlist_id):
    """获取歌单信息，失败时返回错误响应"""
    cookies = api_service._get_cookies()
    playlist = playlist_detail(playlist_id, cookies)
    if not playlist or not playlist.get('tracks'):
        return None, APIResponse.error("获取歌单详情失败或歌单为空", 404)
    return playlist, None


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


@app.before_request
def before_request():
    """请求前处理"""
    # 记录请求信息
    api_service.logger.info(
        f"{request.method} {request.path} - IP: {request.remote_addr} - "
        f"User-Agent: {request.headers.get('User-Agent', 'Unknown')}"
    )


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
        result = search_music(keyword, cookies, limit, int(search_type))
        
        # search_music返回的是歌曲列表，需要包装成前端期望的格式
        if result:
            for song in result:
                # 添加艺术家字符串（如果需要）
                if 'artists' in song:
                    song['artist_string'] = song['artists']
        
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
        
        cookies = api_service._get_cookies()
        result = playlist_detail(playlist_id, cookies)
        
        # 适配前端期望的响应格式
        response_data = {
            'status': 'success',
            'playlist': result
        }
        
        return APIResponse.success(response_data, "获取歌单详情成功")
        
    except Exception as e:
        api_service.logger.error(f"获取歌单异常: {e}\n{traceback.format_exc()}")
        return APIResponse.error(f"获取歌单失败: {str(e)}", 500)


@app.route('/playlist/batch', methods=['GET', 'POST'])
def batch_get_playlist_urls():
    """批量获取歌单中所有歌曲的URL"""
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
        playlist = playlist_detail(playlist_id, cookies)
        if not playlist or not playlist.get('tracks'):
            return APIResponse.error("获取歌单详情失败或歌单为空", 404)
        tracks = playlist['tracks']
        song_ids: List[int] = [t['id'] for t in tracks]
        total = len(song_ids)
        api_service.logger.info(f"批量解析歌单 {playlist_id}，共 {total} 首歌曲，音质: {level}")
        urls_result: Dict[int, Dict] = {}
        with ThreadPoolExecutor(max_workers=min(5, total)) as executor:
            for i in range(0, len(song_ids), 20):
                batch = song_ids[i:i + 20]
                future_to_id = {executor.submit(url_v1, sid, level, cookies): sid for sid in batch}
                for future in as_completed(future_to_id):
                    sid = future_to_id[future]
                    try:
                        result = future.result()
                        if result and result.get('data') and len(result['data']) > 0:
                            d = result['data'][0]
                            urls_result[sid] = {
                                'url': d.get('url', ''), 'size': d.get('size', 0),
                                'size_formatted': api_service._format_file_size(d.get('size', 0)),
                                'type': d.get('type', ''), 'level': d.get('level', level),
                                'quality_name': api_service._get_quality_display_name(d.get('level', level)),
                                'br': d.get('br', 0),
                            }
                        else:
                            urls_result[sid] = None
                    except Exception:
                        urls_result[sid] = None
        resolved_tracks = []
        for track in tracks:
            url_info = urls_result.get(track['id'])
            td = {'id': track['id'], 'name': track['name'], 'artists': track['artists'],
                   'album': track['album'], 'picUrl': track['picUrl'],
                   'duration': track.get('duration', 0)}
            if url_info:
                td.update(url_info)
            else:
                td.update({'url': '', 'size': 0, 'size_formatted': '获取失败', 'type': '',
                            'level': level, 'quality_name': api_service._get_quality_display_name(level), 'br': 0})
            resolved_tracks.append(td)
        response_data = {
            'playlist': {'id': playlist.get('id'), 'name': playlist.get('name'),
                          'coverImgUrl': playlist.get('coverImgUrl'), 'creator': playlist.get('creator'),
                          'trackCount': playlist.get('trackCount'), 'description': playlist.get('description')},
            'tracks': resolved_tracks,
            'resolved': sum(1 for t in resolved_tracks if t['url']),
            'total': total,
        }
        api_service.logger.info(f"批量解析完成，成功: {response_data['resolved']}/{total}")
        return APIResponse.success(response_data, "批量获取歌单歌曲URL成功")
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
        playlist = playlist_detail(playlist_id, cookies)
        if not playlist or not playlist.get('tracks'):
            return APIResponse.error("获取歌单详情失败或歌单为空", 404)
        tracks = playlist['tracks']
        playlist_info = {
            'id': playlist.get('id'), 'name': playlist.get('name'),
            'coverImgUrl': playlist.get('coverImgUrl'), 'creator': playlist.get('creator'),
            'trackCount': playlist.get('trackCount'), 'description': playlist.get('description'),
        }
        task_id = batch_task_mgr.create_task(tracks, playlist_info, level, cookies)
        api_service.logger.info(f"批量下载任务创建: {task_id}, 歌单: {playlist_id}, 共 {len(tracks)} 首")
        return APIResponse.success({'task_id': task_id}, "下载任务已创建")
    except Exception as e:
        api_service.logger.error(f"创建批量下载任务异常: {e}")
        return APIResponse.error(f"创建任务失败: {str(e)}", 500)


@app.route('/playlist/download/batch/progress/<task_id>', methods=['GET'])
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
        zip_buffer.seek(0)
        response = send_file(zip_buffer, as_attachment=True, download_name=zip_filename, mimetype='application/zip')
        response.headers['X-Download-Count'] = str(task_info.get('success', 0))
        response.headers['X-Total-Count'] = str(task_info.get('total', 0))
        response.headers['X-Fail-List'] = json.dumps(task_info.get('errors', []))
        response.call_on_close(lambda: batch_task_mgr.cleanup(task_id))
        return response
    except Exception as e:
        return APIResponse.error(f"获取结果失败: {str(e)}", 500)


@app.route('/playlist/download/batch/cancel/<task_id>', methods=['POST'])
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
        song_id = api_service._extract_music_id(song_id)
        cookies = api_service._get_cookies()
        lyric_info = lyric_v1(song_id, cookies)
        if not lyric_info:
            return APIResponse.error("获取歌词失败", 404)
        lrc = lyric_info.get('lrc', {}).get('lyric', '')
        tl = lyric_info.get('tlyric', {}).get('lyric', '')
        if not lrc:
            return APIResponse.error("未找到歌词", 404)
        if tlyric and tl:
            lrc = _merge_translation_lyric(lrc, tl)
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
                        lrc = _merge_translation_lyric(lrc, tl)
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


@app.route('/album', methods=['GET', 'POST'])
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
        
        cookies = api_service._get_cookies()
        result = album_detail(album_id, cookies)
        
        # 适配前端期望的响应格式
        response_data = {
            'status': 200,
            'album': result
        }
        
        return APIResponse.success(response_data, "获取专辑详情成功")
        
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
        validation_error = api_service._validate_request_params({'music_id': music_id})
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
                    music_id, quality
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
                'hires', 'sky', 'jyeffect', 'jymaster'
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

