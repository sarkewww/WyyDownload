"""音乐下载器模块

提供网易云音乐下载及批量任务管理功能，包括：
- 通用工具函数（文件名安全处理、速度/时间格式化、歌词合并、ZIP打包等）
- 音乐信息获取
- 文件下载到本地（支持断点续传）
- 音乐标签写入
- 多线程批量下载任务管理
"""

import re
import os
import sqlite3
import time
import logging
import threading
import uuid
import atexit
import zipfile
import shutil
import tempfile
from io import BytesIO
from typing import Dict, List, Optional, Tuple, Any, Union, Callable
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from mutagen.flac import FLAC
from mutagen.mp3 import MP3
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TDRC, TRCK, APIC
from mutagen.mp4 import MP4

from music_api import NeteaseAPI, APIException
from cookie_manager import CookieManager


# ==================== 通用工具函数 ====================

VALID_LEVELS = ['standard', 'exhigh', 'lossless', 'hires', 'sky', 'dolby', 'jyeffect', 'jymaster']
ILLEGAL_CHARS = r'<>:"/\|?*'


def safe_filename(name: str) -> str:
    return ''.join(c for c in (name or 'file') if c not in ILLEGAL_CHARS)


def format_speed(bytes_per_sec: int) -> str:
    if bytes_per_sec >= 1024 * 1024:
        return f"{bytes_per_sec / (1024 * 1024):.1f} MB/s"
    elif bytes_per_sec >= 1024:
        return f"{bytes_per_sec / 1024:.1f} KB/s"
    return f"{bytes_per_sec} B/s"


def format_eta(seconds: float) -> str:
    if seconds < 0:
        return "--"
    if seconds < 60:
        return f"{int(seconds)}秒"
    if seconds < 3600:
        return f"{int(seconds) // 60}分{int(seconds) % 60}秒"
    h = int(seconds) // 3600
    m = (int(seconds) % 3600) // 60
    return f"{h}时{m}分"


def merge_translation_lyric(lrc: str, tlyric: str) -> str:
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


def make_zip_response(files_dir: Path, zip_name: str) -> Tuple[BytesIO, str]:
    zip_buf = BytesIO()
    with zipfile.ZipFile(zip_buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(files_dir.iterdir()):
            zf.write(str(f), f.name)
    zip_buf.seek(0)
    return zip_buf, safe_filename(zip_name) + '.zip'


# ==================== 音乐下载相关类 ====================


class AudioFormat(Enum):
    """音频格式枚举"""
    MP3 = "mp3"
    FLAC = "flac"
    M4A = "m4a"
    UNKNOWN = "unknown"


@dataclass
class MusicInfo:
    """音乐信息数据类"""
    id: int
    name: str
    artists: str
    album: str
    pic_url: str
    duration: int
    track_number: int
    download_url: str
    file_type: str
    file_size: int
    quality: str
    lyric: str = ""
    tlyric: str = ""


@dataclass
class DownloadResult:
    """下载结果数据类"""
    success: bool
    file_path: Optional[str] = None
    file_size: int = 0
    error_message: str = ""
    music_info: Optional[MusicInfo] = None


class DownloadException(Exception):
    """下载异常类"""
    pass


class MusicDownloader:
    """音乐下载器主类"""
    
    def __init__(self, download_dir: str = "downloads", max_concurrent: int = 3):
        """
        初始化音乐下载器
        
        Args:
            download_dir: 下载目录
            max_concurrent: 最大并发下载数
        """
        self.download_dir = Path(download_dir)
        self.download_dir.mkdir(exist_ok=True)
        self.max_concurrent = max_concurrent
        
        # 初始化依赖
        self.cookie_manager = CookieManager()
        self.api = NeteaseAPI()
        self._cover_cache: Dict[str, bytes] = {}  # URL -> bytes

    def _fetch_cover(self, pic_url: str) -> Optional[bytes]:
        if not pic_url:
            return None
        if pic_url in self._cover_cache:
            return self._cover_cache[pic_url]
        try:
            r = requests.get(pic_url, timeout=10)
            r.raise_for_status()
            self._cover_cache[pic_url] = r.content
            if len(self._cover_cache) > 500:
                self._cover_cache.pop(next(iter(self._cover_cache)))
            return r.content
        except Exception:
            return None
    
    def _sanitize_filename(self, filename: str) -> str:
        """清理文件名，移除非法字符
        
        Args:
            filename: 原始文件名
            
        Returns:
            清理后的安全文件名
        """
        # 移除或替换非法字符
        illegal_chars = r'[<>:"/\\|?*]'
        filename = re.sub(illegal_chars, '_', filename)
        
        # 移除前后空格和点
        filename = filename.strip(' .')
        
        # 限制长度
        if len(filename) > 200:
            filename = filename[:200]
        
        return filename or "unknown"
    
    def _determine_file_extension(self, url: str, content_type: str = "") -> str:
        """根据URL和Content-Type确定文件扩展名
        
        Args:
            url: 下载URL
            content_type: HTTP Content-Type头
            
        Returns:
            文件扩展名
        """
        # 首先尝试从URL获取
        if '.flac' in url.lower():
            return '.flac'
        elif '.mp3' in url.lower():
            return '.mp3'
        elif '.m4a' in url.lower():
            return '.m4a'
        
        # 从Content-Type获取
        content_type = content_type.lower()
        if 'flac' in content_type:
            return '.flac'
        elif 'mpeg' in content_type or 'mp3' in content_type:
            return '.mp3'
        elif 'mp4' in content_type or 'm4a' in content_type:
            return '.m4a'
        
        return '.mp3'  # 默认
    
    def get_music_info(self, music_id: int, quality: str = "standard", cookies: Dict[str, str] = None) -> MusicInfo:
        """获取音乐详细信息
        
        Args:
            music_id: 音乐ID
            quality: 音质等级
            
        Returns:
            音乐信息对象
            
        Raises:
            DownloadException: 获取信息失败时抛出
        """
        try:
            # 获取cookies
            if cookies is None:
                cookies = self.cookie_manager.parse_cookies()
            
            # 获取音乐URL信息
            url_result = self.api.get_song_url(music_id, quality, cookies)
            if not url_result.get('data') or not url_result['data']:
                raise DownloadException(f"无法获取音乐ID {music_id} 的播放链接")
            
            song_data = url_result['data'][0]
            download_url = song_data.get('url', '')
            if not download_url:
                raise DownloadException(f"音乐ID {music_id} 无可用的下载链接")
            
            # 获取音乐详情
            detail_result = self.api.get_song_detail(music_id)
            if not detail_result.get('songs') or not detail_result['songs']:
                raise DownloadException(f"无法获取音乐ID {music_id} 的详细信息")
            
            song_detail = detail_result['songs'][0]
            
            # 获取歌词
            lyric_result = self.api.get_lyric(music_id, cookies)
            lyric = lyric_result.get('lrc', {}).get('lyric', '') if lyric_result else ''
            tlyric = lyric_result.get('tlyric', {}).get('lyric', '') if lyric_result else ''
            
            # 构建艺术家字符串
            artists = '/'.join(artist['name'] for artist in song_detail.get('ar', []))
            
            # 创建MusicInfo对象
            music_info = MusicInfo(
                id=music_id,
                name=song_detail.get('name', '未知歌曲'),
                artists=artists or '未知艺术家',
                album=song_detail.get('al', {}).get('name', '未知专辑'),
                pic_url=song_detail.get('al', {}).get('picUrl', ''),
                duration=song_detail.get('dt', 0) // 1000,  # 转换为秒
                track_number=song_detail.get('no', 0),
                download_url=download_url,
                file_type=song_data.get('type', 'mp3').lower(),
                file_size=song_data.get('size', 0),
                quality=quality,
                lyric=lyric,
                tlyric=tlyric
            )
            
            return music_info
            
        except APIException as e:
            raise DownloadException(f"API调用失败: {e}")
        except Exception as e:
            raise DownloadException(f"获取音乐信息时发生错误: {e}")
    
    def download_music_file(self, music_id: int, quality: str = "standard",
                            progress_callback: Callable[[int, int, int], None] = None,
                            cookies: Dict[str, str] = None) -> DownloadResult:
        """下载音乐文件到本地（支持断点续传）
        
        Args:
            music_id: 音乐ID
            quality: 音质等级
            progress_callback: 进度回调 callback(downloaded, total_size, speed)
        """
        try:
            music_info = self.get_music_info(music_id, quality, cookies)
            filename = f"{music_info.artists} - {music_info.name}"
            sfilename = self._sanitize_filename(filename)
            file_ext = self._determine_file_extension(music_info.download_url, music_info.file_type)
            file_path = self.download_dir / f"{sfilename}{file_ext}"
            part_path = file_path.with_suffix(file_path.suffix + '.part')
            
            if file_path.exists():
                if progress_callback:
                    fs = file_path.stat().st_size
                    progress_callback(fs, fs, 0)
                return DownloadResult(success=True, file_path=str(file_path),
                                      file_size=file_path.stat().st_size, music_info=music_info)
            
            # 断点续传: 检查 .part 文件
            downloaded = 0
            if part_path.exists():
                downloaded = part_path.stat().st_size
            
            headers = {}
            if downloaded > 0:
                headers['Range'] = f'bytes={downloaded}-'
            
            response = requests.get(music_info.download_url, stream=True, timeout=60, headers=headers)
            total_size = int(response.headers.get('content-length', music_info.file_size or 0))
            if downloaded > 0 and response.status_code == 206:
                total_size += downloaded
            elif response.status_code == 200 and downloaded > 0:
                downloaded = 0
                try:
                    part_path.unlink(missing_ok=True)
                except OSError:
                    pass
            
            response.raise_for_status()
            mode = 'ab' if downloaded > 0 else 'wb'
            
            if progress_callback:
                start_time = time.time()
                last_time = start_time
                with open(part_path, mode) as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            now = time.time()
                            if now - last_time >= 0.3:
                                elapsed = now - start_time
                                speed = int(downloaded / elapsed) if elapsed > 0 else 0
                                progress_callback(downloaded, total_size, speed)
                                last_time = now
                    if downloaded > 0:
                        elapsed = time.time() - start_time
                        speed = int(downloaded / elapsed) if elapsed > 0 else 0
                        progress_callback(downloaded, max(total_size, downloaded), speed)
            else:
                with open(part_path, mode) as f:
                    for chunk in response.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            
            # 下载完成, 重命名 .part -> 正式文件
            if part_path.exists():
                if file_path.exists():
                    file_path.unlink()
                part_path.rename(file_path)

            if file_path.stat().st_size == 0:
                file_path.unlink(missing_ok=True)
                return DownloadResult(success=False, error_message="下载文件为空，请重试")
            
            self._write_music_tags(file_path, music_info)
            return DownloadResult(success=True, file_path=str(file_path),
                                  file_size=file_path.stat().st_size, music_info=music_info)
        except DownloadException:
            raise
        except requests.RequestException as e:
            return DownloadResult(success=False, error_message=f"下载请求失败: {e}")
        except Exception as e:
            return DownloadResult(success=False, error_message=f"下载过程中发生错误: {e}")

    def _write_music_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入音乐标签信息
        
        Args:
            file_path: 音乐文件路径
            music_info: 音乐信息
        """
        try:
            file_ext = file_path.suffix.lower()
            
            if file_ext == '.mp3':
                self._write_mp3_tags(file_path, music_info)
            elif file_ext == '.flac':
                self._write_flac_tags(file_path, music_info)
            elif file_ext == '.m4a':
                self._write_m4a_tags(file_path, music_info)
                
        except Exception as e:
            print(f"写入音乐标签失败: {e}")
    
    def _write_mp3_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入MP3标签"""
        try:
            audio = MP3(str(file_path), ID3=ID3)
            
            # 添加ID3标签
            audio.tags.add(TIT2(encoding=3, text=music_info.name))
            audio.tags.add(TPE1(encoding=3, text=music_info.artists))
            audio.tags.add(TALB(encoding=3, text=music_info.album))
            
            if music_info.track_number > 0:
                audio.tags.add(TRCK(encoding=3, text=str(music_info.track_number)))
            
            # 下载并添加封面
            if music_info.pic_url:
                cover_data = self._fetch_cover(music_info.pic_url)
                if cover_data:
                    audio.tags.add(APIC(
                        encoding=3,
                        mime='image/jpeg',
                        type=3,
                        desc='Cover',
                        data=cover_data
                    ))
            
            audio.save()
        except Exception as e:
            print(f"写入MP3标签失败: {e}")
    
    def _write_flac_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入FLAC标签"""
        try:
            audio = FLAC(str(file_path))
            
            audio['TITLE'] = music_info.name
            audio['ARTIST'] = music_info.artists
            audio['ALBUM'] = music_info.album
            
            if music_info.track_number > 0:
                audio['TRACKNUMBER'] = str(music_info.track_number)
            
            if music_info.pic_url:
                cover_data = self._fetch_cover(music_info.pic_url)
                if cover_data:
                    from mutagen.flac import Picture
                    picture = Picture()
                    picture.type = 3
                    picture.mime = 'image/jpeg'
                    picture.desc = 'Cover'
                    picture.data = cover_data
                    audio.add_picture(picture)
            
            audio.save()
        except Exception as e:
            print(f"写入FLAC标签失败: {e}")
    
    def _write_m4a_tags(self, file_path: Path, music_info: MusicInfo) -> None:
        """写入M4A标签"""
        try:
            audio = MP4(str(file_path))
            
            audio['\xa9nam'] = music_info.name
            audio['\xa9ART'] = music_info.artists
            audio['\xa9alb'] = music_info.album
            
            if music_info.track_number > 0:
                audio['trkn'] = [(music_info.track_number, 0)]
            
            if music_info.pic_url:
                cover_data = self._fetch_cover(music_info.pic_url)
                if cover_data:
                    audio['covr'] = [cover_data]
            
            audio.save()
        except Exception as e:
            print(f"写入M4A标签失败: {e}")


# ==================== 批量下载任务管理器 ====================

_batch_logger = logging.getLogger('batch_manager')


class BatchTaskManager:
    """批量下载任务管理器（多线程）"""

    TTL_SECONDS = 3600

    def __init__(self, downloader):
        self.downloader = downloader
        self.tasks: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self.executor = ThreadPoolExecutor(max_workers=5)
        atexit.register(self.shutdown)

    def shutdown(self):
        self.executor.shutdown(wait=False)

    def _cleanup_expired(self):
        now = time.time()
        expired = [tid for tid, t in self.tasks.items()
                   if t['status'] in ('completed', 'failed', 'cancelled')
                   and now - t.get('start_time', now) > self.TTL_SECONDS]
        for tid in expired:
            self.tasks.pop(tid, None)

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
            self._cleanup_expired()
            self.tasks[task_id] = task
        self.executor.submit(self._run_download, task_id, tracks)
        return task_id

    def _run_download(self, task_id: str, tracks: List[Dict]):
        with self.lock:
            task = self.tasks.get(task_id)
            if not task:
                return
        level = task['level']
        task['_cancelled'] = False
        downloader = self.downloader
        _batch_logger.info(f"[DL-TASK-{task_id}] starting download, {len(tracks)} tracks")
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
                        _batch_logger.error(f"[DL-TASK-{task_id}] unexpected download error", exc_info=True)
            pl_name = task['playlist_info'].get('name', 'playlist')
            pl_creator = task['playlist_info'].get('creator', '')
            success_count = task.get('success', 0)
            _batch_logger.info(f"[DL-TASK-{task_id}] all completed: {success_count}/{task.get('total')}")
            zip_buffer = None
            files_count = 0
            zip_size = 0
            if success_count > 0:
                zip_buffer = BytesIO()
                with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zf:
                    files_in_tmp = sorted(tmp_path.iterdir())
                    files_count = len(files_in_tmp)
                    for f in files_in_tmp:
                        zf.write(str(f), f.name)
                zip_buffer.seek(0)
                zip_size = zip_buffer.getbuffer().nbytes
            with self.lock:
                t = self.tasks.get(task_id)
                if t is None:
                    return
                if success_count > 0:
                    t['zip_buffer'] = zip_buffer
                    t['zip_filename'] = safe_filename(f"{pl_name}-{pl_creator}").strip('-') + '.zip'
                    t['status'] = 'completed'
                    _batch_logger.info(f"[DL-TASK-{task_id}] ZIP created: {files_count} files, {zip_size} bytes")
                else:
                    t['status'] = 'failed'

    def _download_one(self, task_id, idx, track, sid, safe_name, level, tmp_path, downloader):
        with self.lock:
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
            result = downloader.download_music_file(sid, level, progress_callback=progress_cb, cookies=t.get('cookies'))
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
        with self.lock:
            self._cleanup_expired()
            task = self.tasks.get(task_id)
            if not task:
                return None
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
                'speed_formatted': format_speed(task['speed']),
                'avg_speed_formatted': format_speed(avg_speed),
                'eta_formatted': format_eta(eta),
                'percent': round(task['completed'] / task['total'] * 100, 1) if task['total'] > 0 else 0,
                'files_progress': list(task.get('_file_progress', {}).values()),
            }

    def get_result(self, task_id: str) -> Optional[tuple]:
        with self.lock:
            task = self.tasks.get(task_id)
            if not task or task['status'] != 'completed':
                return None
            return task.get('zip_buffer'), task.get('zip_filename'), task

    def cleanup(self, task_id: str):
        with self.lock:
            self.tasks.pop(task_id, None)

    def cancel(self, task_id: str) -> bool:
        with self.lock:
            task = self.tasks.get(task_id)
            if task and task['status'] == 'running':
                task['_cancelled'] = True
                task['status'] = 'cancelled'
                return True
            return False


class Database:
    """SQLite 数据库管理类"""

    def __init__(self, db_path: str = 'data.db'):
        self._db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn

    def _execute(self, sql: str, params=(), commit: bool = True):
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(sql, params)
                if commit:
                    conn.commit()
                return cur
            finally:
                conn.close()

    def _init_db(self):
        self._execute("""
            CREATE TABLE IF NOT EXISTS download_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                song_id TEXT NOT NULL,
                quality TEXT DEFAULT '',
                created_at TEXT NOT NULL
            )
        """)
        self._execute("""
            CREATE TABLE IF NOT EXISTS search_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                keyword TEXT NOT NULL,
                search_type TEXT DEFAULT '1',
                created_at TEXT NOT NULL
            )
        """)

    def _trim(self, table: str, max_rows: int):
        conn = self._conn()
        try:
            row = conn.execute(f"SELECT COUNT(*) as cnt FROM {table}").fetchone()
            if row['cnt'] > max_rows:
                conn.execute(
                    f"DELETE FROM {table} WHERE id NOT IN (SELECT id FROM {table} ORDER BY id DESC LIMIT ?)",
                    (max_rows,)
                )
                conn.commit()
        finally:
            conn.close()

    def add_download(self, name: str, song_id: str, quality: str = ''):
        self._execute(
            "INSERT INTO download_history (name, song_id, quality, created_at) VALUES (?, ?, ?, ?)",
            (name, str(song_id), quality, time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        self._trim('download_history', 50)

    def get_downloads(self, limit: int = 50) -> list:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT name, song_id, quality, created_at FROM download_history ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{'name': r['name'], 'song_id': r['song_id'],
                     'quality': r['quality'], 'time': r['created_at']} for r in rows]
        finally:
            conn.close()

    def clear_downloads(self):
        self._execute("DELETE FROM download_history")

    def add_search(self, keyword: str, search_type: str = '1'):
        self._execute(
            "INSERT INTO search_history (keyword, search_type, created_at) VALUES (?, ?, ?)",
            (keyword, search_type, time.strftime('%Y-%m-%d %H:%M:%S'))
        )
        self._trim('search_history', 50)

    def get_searches(self, limit: int = 50) -> list:
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT keyword, search_type, created_at FROM search_history ORDER BY id DESC LIMIT ?",
                (limit,)
            ).fetchall()
            return [{'keyword': r['keyword'], 'type': r['search_type'],
                     'time': r['created_at']} for r in rows]
        finally:
            conn.close()

    def clear_searches(self):
        self._execute("DELETE FROM search_history")


if __name__ == "__main__":
    downloader = MusicDownloader()
    print("音乐下载器模块")
    print("支持的功能:")
    print("- 音乐下载")
    print("- 音乐标签写入")
    print("- 批量下载任务管理")
