"""
Adaptive HLS Video Converter
Converts video files into multiple resolutions optimized for HLS streaming (VOD).
Provides both CLI and GUI interfaces with built-in FFmpeg dependency management.
"""

import argparse
import os
import re
import subprocess
import concurrent.futures
import shutil
import logging
import sys
import threading
import platform
import urllib.request
import zipfile
import tempfile
import time
import json
from pathlib import Path

try:
    import boto3
    from botocore.exceptions import NoCredentialsError
    HAS_BOTO3 = True
except ImportError:
    HAS_BOTO3 = False

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    # Define dummy class to prevent NameError
    class FileSystemEventHandler:
        pass


def get_video_duration(input_file):
    ffprobe_cmd = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_cmd, 
        '-v', 'error', 
        '-show_entries', 'format=duration', 
        '-of', 'default=noprint_wrappers=1:nokey=1', 
        input_file
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        return float(proc.stdout.strip())
    except Exception:
        return 0.0

def get_media_tracks(input_file):
    """Detects the number of audio and subtitle tracks using ffprobe."""
    ffprobe_cmd = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_cmd,
        '-v', 'error',
        '-show_entries', 'stream=codec_type',
        '-of', 'json',
        input_file
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        info = json.loads(proc.stdout)
        audio_count = 0
        subtitle_count = 0
        for stream in info.get('streams', []):
            if stream.get('codec_type') == 'audio':
                audio_count += 1
            elif stream.get('codec_type') == 'subtitle':
                subtitle_count += 1
        return audio_count, subtitle_count
    except Exception as e:
        logging.error(f"Failed to probe media tracks: {e}")
        return 0, 0

def get_media_track_details(input_file):
    """Extracts detailed information about audio and subtitle tracks."""
    ffprobe_cmd = shutil.which("ffprobe") or "ffprobe"
    cmd = [
        ffprobe_cmd,
        '-v', 'error',
        '-show_entries', 'stream=codec_type,codec_name:stream_tags=language,title',
        '-of', 'json',
        input_file
    ]
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        info = json.loads(proc.stdout)
        
        audio_tracks = []
        sub_tracks = []
        a_idx = 0
        s_idx = 0
        
        for stream in info.get('streams', []):
            ctype = stream.get('codec_type')
            tags = stream.get('tags', {})
            lang = tags.get('language', 'und')
            title = tags.get('title', '')
            codec = stream.get('codec_name', 'unknown')
            
            # Format: [eng] Stereo (aac)
            display_name = f"[{lang}] {title} ({codec})" if title else f"[{lang}] ({codec})"
            
            if ctype == 'audio':
                audio_tracks.append({
                    'relative_index': a_idx, 
                    'display': f"Audio {a_idx}: {display_name}",
                    'lang': lang,
                    'title': title,
                    'codec': codec
                })
                a_idx += 1
            elif ctype == 'subtitle':
                # Mark if the subtitle is picture-based (PGS/VobSub) or complex (ASS) which might fail webvtt conversion
                warn_flag = ""
                if codec not in ['subrip', 'webvtt', 'mov_text', 'text']:
                    warn_flag = " (Warning: May fail HLS conversion)"
                sub_tracks.append({'relative_index': s_idx, 'display': f"Sub {s_idx}: {display_name}{warn_flag}", 'codec': codec})
                s_idx += 1
                
        return audio_tracks, sub_tracks
    except Exception as e:
        logging.error(f"Failed to extract media track details: {e}")
        return [], []



try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

TOOL_NAME = "EduCaster (Local Encoder)"
VERSION = "2.0.0"
AUTHOR = "br31technologies"

# Video qualities configuration with recommended bitrates
# The resolution will be passed into scale=-2:{height} to maintain aspect ratio
QUALITIES = {
    '1080p': {'height': '1080', 'bitrate': '5000k', 'audio_bitrate': '192k'},
    '720p':  {'height': '720',  'bitrate': '2800k', 'audio_bitrate': '128k'},
    '480p':  {'height': '480',  'bitrate': '1400k', 'audio_bitrate': '128k'},
    '360p':  {'height': '360',  'bitrate': '800k',  'audio_bitrate': '96k'},
    '240p':  {'height': '240',  'bitrate': '400k',  'audio_bitrate': '64k'}
}

# Add local bin folder to PATH so downloaded FFmpeg gets detected automatically
if getattr(sys, 'frozen', False):
    # If running as an EXE
    BASE_DIR = os.path.dirname(sys.executable)
else:
    # If running as a script
    BASE_DIR = os.path.dirname(os.path.abspath(__file__))

LOCAL_BIN_DIR = os.path.join(BASE_DIR, "ffmpeg_bin")
if os.path.exists(LOCAL_BIN_DIR):
    os.environ["PATH"] = LOCAL_BIN_DIR + os.pathsep + os.environ["PATH"]

def setup_logger(handler=None):
    """Setup logging either to console or GUI text widget."""
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    
    # Clear existing handlers
    for h in logger.handlers[:]:
        logger.removeHandler(h)
        
    formatter = logging.Formatter('%(asctime)s [%(levelname)s] %(message)s', datefmt='%H:%M:%S')
    
    if handler:
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    else:
        stream_handler = logging.StreamHandler(sys.stdout)
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)

def check_dependencies():
    """Verify that FFmpeg is installed and accessible in the system PATH."""
    missing = []
    if shutil.which("ffmpeg") is None:
        missing.append("FFmpeg")
    
    if not HAS_BOTO3:
        missing.append("boto3 (Python Library)")
        
    if not HAS_WATCHDOG:
        missing.append("watchdog (Python Library)")
        
    if missing:
        if "FFmpeg" in missing:
            logging.error("FFmpeg could not be found. Please ensure it is installed.")
            logging.info("Hint: Use 'Tools -> Install Dependencies' in GUI.")
        
        if "boto3 (Python Library)" in missing or "watchdog (Python Library)" in missing:
            logging.error(f"Missing Python libraries: {', '.join([m for m in missing if 'Python' in m])}")
            logging.info("Please run: pip install boto3 watchdog requests")
            
        return False
    return True

# ==========================================
# R2 UPLOADER
# ==========================================

class R2UploadHandler(FileSystemEventHandler):
    def __init__(self, bucket_name, s3_client, source_dir, prefix="", public_domain=""):
        self.bucket_name = bucket_name
        self.s3_client = s3_client
        self.source_dir = source_dir
        self.prefix = prefix
        self.public_domain = public_domain
        self.uploaded_files = set()
        self._lock = threading.Lock()

    def on_created(self, event):
        if not event.is_directory:
            self.upload_file(event.src_path)

    def on_modified(self, event):
        if not event.is_directory:
            # Re-upload playlists when modified
            if event.src_path.endswith('.m3u8'):
                self.upload_file(event.src_path)

    def upload_file(self, file_path):
        # Wait a brief moment to ensure file write is complete (especially for larger segments)
        time.sleep(0.5)
        
        rel_path = os.path.relpath(file_path, self.source_dir)
        # Fix path separators for S3 keys (always forward slashes)
        s3_key = os.path.join(self.prefix, rel_path).replace("\\", "/")
        
        # Avoid re-uploading segments that are already done (only playlists update)
        if file_path.endswith('.ts'):
            with self._lock:
                if file_path in self.uploaded_files:
                    return
                self.uploaded_files.add(file_path)

        try:
            content_type = 'application/x-mpegURL' if file_path.endswith('.m3u8') else 'video/MP2T'
            self.s3_client.upload_file(
                file_path, 
                self.bucket_name, 
                s3_key, 
                ExtraArgs={'ContentType': content_type}
            )
            logging.info(f"[R2] Uploaded: {s3_key}")
        except Exception as e:
            logging.error(f"[R2] Failed to upload {s3_key}: {str(e)}")

def load_r2_config():
    config_path = os.path.join(BASE_DIR, "r2_config.json")
    if not os.path.exists(config_path):
        return None
    try:
        with open(config_path, 'r') as f:
            return json.load(f)
    except:
        return None

def start_r2_uploader(source_dir, video_name):
    if not HAS_BOTO3 or not HAS_WATCHDOG:
        logging.warning("R2 Uploading skipped: Missing libraries.")
        return None, None

    config = load_r2_config()
    if not config:
        logging.warning("R2 Uploading skipped: r2_config.json not found or invalid.")
        return None, None
        
    try:
        s3 = boto3.client(
            's3',
            endpoint_url=config['r2_endpoint_url'],
            aws_access_key_id=config['r2_access_key_id'],
            aws_secret_access_key=config['r2_secret_access_key']
        )
        
        bucket_name = config['r2_bucket_name']
        public_domain = config.get('r2_public_domain', '')
        
        # Prefix in bucket: videos/<video_name>/...
        prefix = f"videos/{video_name}"
        
        event_handler = R2UploadHandler(bucket_name, s3, source_dir, prefix, public_domain)
        observer = Observer()
        observer.schedule(event_handler, source_dir, recursive=True)
        observer.start()
        
        logging.info(f"R2 Uploader started. Watching: {source_dir}")
        logging.info(f"Target: {bucket_name}/{prefix}")
        
        return observer, event_handler, f"{public_domain}/{prefix}/master.m3u8"
        
    except Exception as e:
        logging.error(f"Failed to initialize R2 Uploader: {str(e)}")
        return None, None, None

def process_resolution(input_file, output_dir, quality, config, use_hwaccel=False, segment_time=6, codec='h264', crf=None, cancel_event=None, duration=0.0, single_folder=False, live_mode=False, copy_audio=False, copy_subs=False, fast_mode=True):
    """Spawns an FFmpeg process to convert the input video into a specific HLS resolution."""
    if single_folder:
        resolution_dir = output_dir
        playlist_path = os.path.join(resolution_dir, f'{quality}.m3u8')
        segment_pattern = os.path.join(resolution_dir, f'{quality}_segment_%03d.ts')
    else:
        resolution_dir = os.path.join(output_dir, quality)
        os.makedirs(resolution_dir, exist_ok=True)
        playlist_path = os.path.join(resolution_dir, 'index.m3u8')
        segment_pattern = os.path.join(resolution_dir, 'segment_%03d.ts')
    
    if codec == 'h265':
        vcodec = 'hevc_nvenc' if use_hwaccel else 'libx265'
    else:
        vcodec = 'h264_nvenc' if use_hwaccel else 'libx264'
        
    if fast_mode:
        preset = 'p1' if use_hwaccel else 'ultrafast'
    else:
        preset = 'p2' if use_hwaccel else 'fast'
    
    # Aspect-safe scaling. Width is automatically calculated (-2 ensures the dimension is divisible by 2 for h264 compliance)
    scale_filter = f"scale=-2:{config['height']}"
    
    bitrate_val_str = str(config['bitrate']).lower().replace('k', '').replace('m', '000')
    try:
        bitrate_val = int(bitrate_val_str)
    except ValueError:
        bitrate_val = 5000
    bufsize = f"{int(bitrate_val * 1.5)}k"
    
    if crf is not None:
        if use_hwaccel:
            # For NVIDIA NVENC encoders (h264_nvenc, hevc_nvenc), we use -cq (Constant Quality) instead of -crf
            # The rate control mode must be set to constqp (-rc constqp) or vbr (-rc vbr)
            video_bitrate_args = [
                '-rc', 'vbr',
                '-cq', str(crf),
                '-b:v', '0' # Setting max bitrate to 0 ensures it relies purely on CQ
            ]
        else:
            # For CPU encoders (libx264, libx265), we use standard -crf
            video_bitrate_args = ['-crf', str(crf)]
    else:
        video_bitrate_args = [
            '-b:v', str(config['bitrate']),
            '-maxrate', str(config['bitrate']),
            '-bufsize', bufsize
        ]
    
    if live_mode:
        # Live HLS settings
        hls_flags = [
            '-hls_time', str(segment_time),
            '-hls_list_size', '5',         # Keep only last 5 segments in playlist
            '-hls_flags', 'delete_segments', # Delete old segments from disk (R2 uploader must be fast!)
            '-hls_segment_filename', segment_pattern,
            playlist_path
        ]
    else:
        # VOD HLS settings
        hls_flags = [
            '-hls_time', str(segment_time),
            '-hls_playlist_type', 'vod',
            '-hls_segment_filename', segment_pattern,
            playlist_path
        ]

    ffmpeg_cmd = shutil.which("ffmpeg") or "ffmpeg"
    cmd = [
        ffmpeg_cmd,
        '-hide_banner',
        '-y',
        '-i', input_file,
        '-map', '0:v:0',
        '-map', '0:a:0?', # The '?' means it won't fail if the video has no audio
        '-vf', scale_filter,
        '-c:v', vcodec,
        '-preset', preset,
    ] + video_bitrate_args
    
    if copy_audio:
        cmd.extend(['-c:a', 'copy'])
    else:
        cmd.extend([
            '-c:a', 'aac',
            '-b:a', config['audio_bitrate'],
            '-ar', '48000'
        ])
        
    cmd.extend([
        '-profile:v', 'main',
        '-g', '48',
        '-keyint_min', '48',
        '-sc_threshold', '0',
    ] + hls_flags)
    
    if duration > 0 and crf is None and not live_mode:
        try:
            audio_kb = int(str(config['audio_bitrate']).lower().replace('k', ''))
            expected_size_mb = ((bitrate_val + audio_kb) * duration) / 8192
            logging.info(f"[{quality}] Starting encoding... Expected final size: ~{expected_size_mb:.2f} MB")
        except:
            logging.info(f"[{quality}] Starting encoding...")
    else:
        logging.info(f"[{quality}] Starting encoding...")
        
    start_time = time.time()
    
    try:
        with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as temp_err:
            process = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=temp_err,
                universal_newlines=True,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            
            while process.poll() is None:
                if cancel_event and cancel_event.is_set():
                    process.terminate()
                    process.wait()
                    logging.warning(f"[{quality}] Encoding cancelled by user.")
                    return False, quality
                time.sleep(0.5)
                
            if process.returncode != 0:
                if cancel_event and cancel_event.is_set():
                    return False, quality
                temp_err.seek(0)
                err_output = temp_err.read()
                logging.error(f"[{quality}] FFmpeg encoding failed.\nError snippet: {err_output[-500:]}")
                return False, quality
                
            elapsed = time.time() - start_time
            minutes, seconds = divmod(int(elapsed), 60)
            
            # Calculate actual folder size
            total_size_bytes = 0
            for dirpath, _, filenames in os.walk(resolution_dir):
                for f in filenames:
                    if single_folder and not (f.startswith(f"{quality}_segment") or f == f"{quality}.m3u8"):
                        continue
                    fp = os.path.join(dirpath, f)
                    if not os.path.islink(fp):
                        total_size_bytes += os.path.getsize(fp)
            actual_size_mb = total_size_bytes / (1024 * 1024)
            
            logging.info(f"[{quality}] Successfully finished encoding! Time taken: {minutes}m {seconds}s. Output size: {actual_size_mb:.2f} MB")
            return True, quality
            
    except Exception as e:
        logging.error(f"[{quality}] Exception occurred: {str(e)}")
        return False, quality

def generate_master_playlist(output_dir, successful_qualities, single_folder=False, audio_configs=None, codec='h264', is_multi_track=False):
    """Creates a master.m3u8 playlist combining all generated resolutions."""
    master_playlist_path = os.path.join(output_dir, 'master.m3u8')
    
    bandwidths = {
        '1080p': 2200000,
        '720p': 1600000,
        '480p': 1100000,
        '360p': 800000,
        '240p': 400000
    }
    
    with open(master_playlist_path, 'w') as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n\n")
        
        has_audio = audio_configs and len(audio_configs) > 0
        
        if has_audio:
            f.write("# Audio tracks\n")
            for a_idx, cfg in enumerate(audio_configs):
                is_default = "YES" if a_idx == 0 else "NO"
                stream_idx = len(successful_qualities) + a_idx
                if single_folder:
                    uri = f"stream_{stream_idx}.m3u8"
                else:
                    uri = f"stream_{stream_idx}/index.m3u8"
                f.write(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio_group",NAME="{cfg["name"]}",LANGUAGE="{cfg["lang"]}",DEFAULT={is_default},AUTOSELECT=YES,URI="{uri}"\n')
            f.write("\n")
            
        f.write("# Video variants\n")
        
        sorted_qualities = sorted(
            successful_qualities, 
            key=lambda x: bandwidths.get(x, 0), 
            reverse=True
        )
        
        v_codec_str = "hvc1.1.4.L153.B0" if codec == "h265" else "avc1.4d401f"
        a_codec_str = "mp4a.40.2"
        
        # We need to map sorted qualities back to their original index for stream_N
        original_q_list = list(successful_qualities)
        
        for q in sorted_qualities:
            bw = bandwidths.get(q, 800000)
            v_idx = original_q_list.index(q)
            
            # Since we dynamically scale width based on height to maintain aspect ratio,
            # we write approximate standard 16:9 resolutions into the Master playlist for Apple/HLS player compatibility
            fallback_widths = { '1080p': '1920x1080', '720p': '1280x720', '480p': '854x480', '360p': '640x360', '240p': '426x240' }
            res = fallback_widths.get(q, '1280x720')
            
            if has_audio:
                f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={res},CODECS="{v_codec_str},{a_codec_str}",AUDIO="audio_group"\n')
            else:
                f.write(f'#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={res},CODECS="{v_codec_str},{a_codec_str}"\n')
                
            if single_folder:
                # If generated via multi-track, it uses stream_N. If generated via single track, it uses {q}.m3u8
                if is_multi_track:
                    f.write(f"stream_{v_idx}.m3u8\n")
                else:
                    f.write(f"{q}.m3u8\n")
            else:
                if is_multi_track:
                    f.write(f"stream_{v_idx}/index.m3u8\n")
                else:
                    f.write(f"{q}/index.m3u8\n")
            
    logging.info(f"[Master] Master playlist created: {master_playlist_path}")

def process_video_file(video_path, output_base_dir, use_hwaccel, parallel, selected_qualities=None, segment_time=6, codec='h264', crf=None, custom_bitrates=None, cancel_event=None, single_folder=False, live_mode=False, keep_all_tracks=False, selected_audio=None, upload_to_r2=False, copy_audio=False, fast_mode=True):
    """Runs the conversion flow for a single video."""
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    output_dir = os.path.join(output_base_dir, video_name)
    os.makedirs(output_dir, exist_ok=True)
    
    if selected_qualities is None or len(selected_qualities) == 0:
        selected_qualities = list(QUALITIES.keys())
        
    qualities_to_process = {}
    for q in selected_qualities:
        if q in QUALITIES:
            conf = dict(QUALITIES[q])
            if custom_bitrates and q in custom_bitrates:
                conf['bitrate'] = custom_bitrates[q]
            qualities_to_process[q] = conf
    
    logging.info(f"--- Starting task for '{video_name}' ---")
    
    # Decide if we need multi-track logic
    custom_track_selection = (selected_audio is not None)
    is_multi_track = keep_all_tracks or custom_track_selection
    
    # START R2 UPLOADER
    uploader_observer = None
    uploader_handler = None
    playback_url = None
    if upload_to_r2:
        uploader_observer, uploader_handler, playback_url = start_r2_uploader(output_dir, video_name)
        if playback_url:
            logging.info(f"Live/VOD Playback URL: {playback_url}")
    else:
        logging.info("R2 Uploading is disabled for this job.")
    
    # For Live Mode, generate Master Playlist FIRST
    if live_mode and not is_multi_track: 
        generate_master_playlist(output_dir, list(qualities_to_process.keys()), single_folder, None, codec, False)
        # Manually trigger upload for master.m3u8 since it's created before watcher might be fully active or just to be safe
        if uploader_handler:
             master_path = os.path.join(output_dir, 'master.m3u8')
             uploader_handler.upload_file(master_path)

    duration = get_video_duration(video_path)
    if duration > 0:
        minutes, seconds = divmod(int(duration), 60)
        logging.info(f"Video duration: {minutes}m {seconds}s")
    
    successful_qualities = []

    if is_multi_track:
        logging.info("Multi-track mode enabled. Using a single FFmpeg process to map audio...")
        
        audio_configs = []
        if custom_track_selection:
            # selected_audio is a list of dicts: {'index': 0, 'name': 'English', 'lang': 'eng'}
            audio_configs = selected_audio
        else:
            audio_count, subtitle_count = get_media_tracks(video_path)
            for i in range(audio_count):
                audio_configs.append({'index': i, 'name': f"Audio {i}", 'lang': 'und'})
            
        logging.info(f"Mapping {len(audio_configs)} audio track(s).")
        
        ffmpeg_cmd = shutil.which("ffmpeg") or "ffmpeg"
        cmd = [ffmpeg_cmd, '-hide_banner', '-y', '-i', video_path]
        
        # Map selected audio
        for config in audio_configs:
            cmd.extend(['-map', f"0:a:{config['index']}?"])
            
        # Video settings
        vcodec = 'hevc_nvenc' if use_hwaccel and codec == 'h265' else ('libx265' if codec == 'h265' else ('h264_nvenc' if use_hwaccel else 'libx264'))
        if fast_mode:
            preset = 'p1' if use_hwaccel else 'ultrafast'
        else:
            preset = 'p2' if use_hwaccel else 'fast'
        
        q_keys = list(qualities_to_process.keys())
        
        # Build filter_complex for scaling
        filter_complex = []
        for i, q in enumerate(q_keys):
            conf = qualities_to_process[q]
            # When using hardware acceleration with filter_complex, we need to upload to hardware if not already there.
            # But simpler: just scale in software and encode in hardware.
            # scale=trunc(oh*a/2)*2:height ensures width is even to prevent -22 (Invalid argument) on x264/nvenc
            filter_complex.append(f"[0:v:0]scale=trunc(oh*a/2)*2:{conf['height']},format=yuv420p[v{i}]")
            
        if filter_complex:
            cmd.extend(['-filter_complex', ";".join(filter_complex)])
            
        for i, q in enumerate(q_keys):
            conf = qualities_to_process[q]
            
            bitrate_val_str = str(conf['bitrate']).lower().replace('k', '').replace('m', '000')
            try:
                bitrate_val = int(bitrate_val_str)
            except ValueError:
                bitrate_val = 5000
            bufsize = f"{int(bitrate_val * 1.5)}k"
            
            # Map from the filter_complex output instead of 0:v:0
            cmd.extend(['-map', f'[v{i}]'])
            
            cmd.extend([
                f'-c:v:{i}', vcodec,
                f'-preset', preset
            ])
            
            if crf is not None:
                if use_hwaccel:
                    cmd.extend(['-rc', 'vbr', f'-cq:v:{i}', str(crf), f'-b:v:{i}', '0'])
                else:
                    cmd.extend([f'-crf:v:{i}', str(crf)])
            else:
                cmd.extend([
                    f'-b:v:{i}', str(conf['bitrate']),
                    f'-maxrate:v:{i}', str(conf['bitrate']),
                    f'-bufsize:v:{i}', bufsize
                ])
                
            cmd.extend([
                f'-profile:v:{i}', 'main',
                f'-g', '48', f'-keyint_min', '48', f'-sc_threshold', '0'
            ])
            
        # Audio Codecs
        if len(audio_configs) > 0:
            if copy_audio:
                cmd.extend(['-c:a', 'copy'])
            else:
                cmd.extend(['-c:a', 'aac', '-b:a', '128k', '-ar', '48000'])
            
        # Build var_stream_map
        var_stream_map = []
        
        # In HLS, when mapping multiple audio streams or mapping audio to multiple video resolutions,
        # we must use audio groups (agroup) to prevent the "Same elementary stream found more than once" error.
        
        # Map video streams and assign them to the audio group
        for i in range(len(qualities_to_process)):
            if len(audio_configs) > 0:
                var_stream_map.append(f"v:{i},agroup:audio")
            else:
                var_stream_map.append(f"v:{i}")
                
        # Map audio streams independently into the audio group
        for a_out in range(len(audio_configs)):
            var_stream_map.append(f"a:{a_out},agroup:audio")
            
        cmd.extend(['-var_stream_map', " ".join(var_stream_map)])
        
        # HLS outputs
        master_playlist_path = os.path.join(output_dir, 'master.m3u8')
        
        if live_mode:
            hls_flags = [
                '-f', 'hls',
                '-hls_time', str(segment_time),
                '-hls_list_size', '5',
                '-hls_flags', 'delete_segments',
                '-master_pl_name', 'master.m3u8'
            ]
        else:
            hls_flags = [
                '-f', 'hls',
                '-hls_time', str(segment_time),
                '-hls_playlist_type', 'vod',
                '-master_pl_name', 'master.m3u8'
            ]
            
        if single_folder:
            hls_flags.extend([
                '-hls_segment_filename', 'stream_%v_segment_%03d.ts',
                'stream_%v.m3u8'
            ])
        else:
            # We must create folders for BOTH video streams and audio streams because each gets its own variant (%v)
            total_streams = len(qualities_to_process) + len(audio_configs)
            for i in range(total_streams):
                os.makedirs(os.path.join(output_dir, f'stream_{i}'), exist_ok=True)
            hls_flags.extend([
                '-hls_segment_filename', 'stream_%v/segment_%03d.ts',
                'stream_%v/index.m3u8'
            ])
            
        cmd.extend(hls_flags)
        
        start_time = time.time()
        try:
            with tempfile.TemporaryFile(mode='w+', encoding='utf-8') as temp_err:
                process = subprocess.Popen(
                    cmd,
                    cwd=output_dir,
                    stdout=subprocess.DEVNULL,
                    stderr=temp_err,
                    universal_newlines=True,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
                )
                
                while process.poll() is None:
                    if cancel_event and cancel_event.is_set():
                        process.terminate()
                        process.wait()
                        logging.warning("Encoding cancelled by user.")
                        break
                    time.sleep(0.5)
                    
                if process.returncode == 0:
                    # Generate master.m3u8 manually for full control
                    good_master_path = os.path.join(output_dir, 'master.m3u8')
                    # If not single folder, FFmpeg creates master.m3u8 inside the first variant's folder
                    bad_master_path = os.path.join(output_dir, 'stream_0', 'master.m3u8')
                    
                    successful_qualities = list(qualities_to_process.keys())
                    generate_master_playlist(output_dir, successful_qualities, single_folder, audio_configs, codec, True)
                    
                    if not single_folder and os.path.exists(bad_master_path):
                        os.remove(bad_master_path)
                            
                    elapsed = time.time() - start_time
                    minutes, seconds = divmod(int(elapsed), 60)
                    logging.info(f"Successfully finished multi-track encoding! Time taken: {minutes}m {seconds}s.")
                    successful_qualities = list(qualities_to_process.keys())
                else:
                    if not (cancel_event and cancel_event.is_set()):
                        temp_err.seek(0)
                        err_output = temp_err.read()
                        logging.error(f"FFmpeg command was: {' '.join(cmd)}")
                        logging.error(f"FFmpeg encoding failed.\nError snippet: {err_output[-1000:]}")
        except Exception as e:
            logging.error(f"Exception occurred during multi-track encoding: {str(e)}")
            
    else:
        if parallel:
            logging.info("Running encodings in parallel...")
            workers = min(len(qualities_to_process), (os.cpu_count() or 4))
            with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(process_resolution, video_path, output_dir, q, c, use_hwaccel, segment_time, codec, crf, cancel_event, duration, single_folder, live_mode, copy_audio, copy_subs, fast_mode) 
                    for q, c in qualities_to_process.items()
                ]
                
                for future in concurrent.futures.as_completed(futures):
                    success, quality = future.result()
                    if success:
                        successful_qualities.append(quality)
        else:
            logging.info("Running encodings sequentially...")
            for q, c in qualities_to_process.items():
                if cancel_event and cancel_event.is_set():
                    break
                success, quality = process_resolution(video_path, output_dir, q, c, use_hwaccel, segment_time, codec, crf, cancel_event, duration, single_folder, live_mode, copy_audio, copy_subs, fast_mode)
                if success:
                    successful_qualities.append(quality)
    
    if successful_qualities:
        # Regenerate master playlist at end for VOD to ensure it's correct/complete
        if not live_mode and not is_multi_track:
            generate_master_playlist(output_dir, successful_qualities, single_folder, None, codec, False)
            
        if uploader_handler:
            master_path = os.path.join(output_dir, 'master.m3u8')
            logging.info(f"Forcing upload of master playlist: {master_path}")
            if os.path.exists(master_path):
                uploader_handler.upload_file(master_path)

    # Stop Uploader
    if uploader_observer:
        logging.info("Stopping R2 Uploader...")
        uploader_observer.stop()
        uploader_observer.join()
        
    if cancel_event and cancel_event.is_set():
        logging.warning(f"--- Processing '{video_name}' was cancelled ---")
        return False
                
    if successful_qualities:
        logging.info(f"--- Finished processing '{video_name}' successfully ---\n")
        return True
    else:
        logging.error(f"--- Failed to process '{video_name}' ---\n")
        return False

def run_conversion_job(input_path, output_path, hwaccel, parallel, selected_qualities=None, segment_time=6, codec='h264', crf=None, custom_bitrates=None, on_complete=None, cancel_event=None, single_folder=False, live_mode=False, keep_all_tracks=False, selected_audio=None, upload_to_r2=False, copy_audio=False, fast_mode=True):
    """Entry point for conversion job, handles both batch and single files."""
    if not check_dependencies():
        if on_complete: on_complete()
        return

    if os.path.isfile(input_path):
        process_video_file(input_path, output_path, hwaccel, parallel, selected_qualities, segment_time, codec, crf, custom_bitrates, cancel_event, single_folder, live_mode, keep_all_tracks, selected_audio, upload_to_r2, copy_audio, fast_mode)
        
    elif os.path.isdir(input_path):
        valid_exts = {'.mp4', '.mkv', '.mov', '.avi', '.webm', '.flv'}
        video_files = [
            os.path.join(input_path, f) 
            for f in os.listdir(input_path) 
            if os.path.splitext(f)[1].lower() in valid_exts
        ]
        
        if not video_files:
            logging.warning(f"No valid video files found in directory '{input_path}'")
        else:
            logging.info(f"Found {len(video_files)} videos in batch mode.")
            for i, video_file in enumerate(video_files):
                if cancel_event and cancel_event.is_set():
                    logging.warning("Batch processing cancelled.")
                    break
                logging.info(f"Processing video {i+1} of {len(video_files)}...")
                process_video_file(video_file, output_path, hwaccel, parallel, selected_qualities, segment_time, codec, crf, custom_bitrates, cancel_event, single_folder, live_mode, keep_all_tracks, selected_audio=None, upload_to_r2=upload_to_r2, copy_audio=copy_audio, fast_mode=fast_mode)
    else:
        logging.error(f"Input path '{input_path}' does not exist.")

    if on_complete: on_complete()

# ==========================================
# DEPENDENCY DOWNLOADER
# ==========================================

def download_ffmpeg(on_status, on_complete):
    """Downloads and extracts FFmpeg for Windows systems."""
    os_sys = platform.system().lower()
    
    if os_sys != "windows":
        on_status(f"Auto-install is only supported on Windows.\nFor {os_sys.capitalize()}, please install FFmpeg using your package manager (e.g., brew install ffmpeg / apt install ffmpeg).", True)
        on_complete()
        return

    # Reliable GitHub mirror for FFmpeg Windows builds
    url = "https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-win64-gpl.zip"
    
    os.makedirs(LOCAL_BIN_DIR, exist_ok=True)
    temp_zip = os.path.join(tempfile.gettempdir(), "ffmpeg_download.zip")

    try:
        on_status("Connecting to GitHub to download FFmpeg. Please wait, this may take a moment...", False)
        
        # Download file
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urllib.request.urlopen(req) as response, open(temp_zip, 'wb') as out_file:
            shutil.copyfileobj(response, out_file)
            
        on_status("Download complete. Extracting files...", False)
        
        # Extract specific binaries
        with zipfile.ZipFile(temp_zip, 'r') as zip_ref:
            for file_info in zip_ref.infolist():
                if file_info.filename.endswith("ffmpeg.exe") or file_info.filename.endswith("ffprobe.exe"):
                    file_info.filename = os.path.basename(file_info.filename)
                    zip_ref.extract(file_info, LOCAL_BIN_DIR)

        on_status("FFmpeg successfully installed!", False)
        
        # Dynamically update PATH for current session if not already there
        if LOCAL_BIN_DIR not in os.environ["PATH"]:
            os.environ["PATH"] = LOCAL_BIN_DIR + os.pathsep + os.environ["PATH"]
            
        on_status("Ready to convert videos.", False)

    except Exception as e:
        on_status(f"Failed to download FFmpeg: {str(e)}\nPlease download manually from gyan.dev and add to PATH.", True)
    finally:
        if os.path.exists(temp_zip):
            try:
                os.remove(temp_zip)
            except:
                pass
        on_complete()

# ==========================================
# GUI IMPLEMENTATION
# ==========================================

class TextHandler(logging.Handler):
    """Custom logging handler to route logs to a Tkinter Text widget."""
    def __init__(self, text_widget):
        super().__init__()
        self.text_widget = text_widget

    def emit(self, record):
        msg = self.format(record)
        def append():
            self.text_widget.configure(state='normal')
            self.text_widget.insert(tk.END, msg + '\n')
            self.text_widget.configure(state='disabled')
            self.text_widget.yview(tk.END)
        self.text_widget.after(0, append)

class ConverterGUI(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(f"{TOOL_NAME} v{VERSION}")
        self.geometry("800x850")
        self.minsize(700, 750)
        
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.hwaccel_var = tk.BooleanVar(value=False)
        self.parallel_var = tk.BooleanVar(value=True)
        self.segment_time_var = tk.IntVar(value=6)
        self.live_mode_var = tk.BooleanVar(value=False)
        self.keep_all_tracks_var = tk.BooleanVar(value=False)
        self.upload_r2_var = tk.BooleanVar(value=True)
        self.copy_audio_var = tk.BooleanVar(value=False)
        self.fast_mode_var = tk.BooleanVar(value=True)
        
        self.codec_var = tk.StringVar(value="h264")
        self.use_crf_var = tk.BooleanVar(value=False)
        self.crf_val_var = tk.StringVar(value="28")
        self.single_folder_var = tk.BooleanVar(value=True)
        
        # Dictionary to hold the vars for each quality
        self.quality_vars = {}
        self.quality_bitrate_vars = {}
        for q, conf in QUALITIES.items():
            self.quality_vars[q] = tk.BooleanVar(value=True)
            self.quality_bitrate_vars[q] = tk.StringVar(value=conf['bitrate'])
            
        self.current_audio_tracks = []
        self.audio_vars = []  # To hold BooleanVar for each track
        self.audio_name_vars = [] # To hold StringVar for each track name
        self.current_sub_tracks = []
        
        self.create_menu()
        self.setup_ui()
        
        # Setup logging to text widget
        text_handler = TextHandler(self.log_text)
        setup_logger(text_handler)
        
        logging.info(f"Welcome to the {TOOL_NAME}!")
        logging.info("Checking for system dependencies...")
        
        if check_dependencies():
            logging.info("FFmpeg is available! Ready to convert.")
        
    def create_menu(self):
        menubar = tk.Menu(self)
        
        # Tools Menu
        tools_menu = tk.Menu(menubar, tearoff=0)
        tools_menu.add_command(label="Install Dependencies (FFmpeg)", command=self.install_dependencies)
        menubar.add_cascade(label="Tools", menu=tools_menu)
        
        # Help Menu
        help_menu = tk.Menu(menubar, tearoff=0)
        help_menu.add_command(label="How to Use", command=self.show_help)
        help_menu.add_separator()
        help_menu.add_command(label="About", command=self.show_about)
        menubar.add_cascade(label="Help", menu=help_menu)
        
        self.config(menu=menubar)

    def setup_ui(self):
        # Create a Canvas and a vertical scrollbar for the main window
        self.canvas = tk.Canvas(self, highlightthickness=0)
        self.scrollbar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        
        # Main Frame inside the Canvas
        main_frame = ttk.Frame(self.canvas, padding="15")
        
        # Configure canvas
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        
        # Create a window inside the canvas for the main_frame
        self.canvas_frame = self.canvas.create_window((0, 0), window=main_frame, anchor="nw")
        
        # Bind events to update the scrollregion when the frame size changes
        main_frame.bind("<Configure>", self.on_frame_configure)
        self.canvas.bind("<Configure>", self.on_canvas_configure)
        
        # Mouse wheel scrolling binding
        self.bind_all("<MouseWheel>", self._on_mousewheel)
        
        # --- File Selection ---
        file_frame = ttk.LabelFrame(main_frame, text="Input & Output Settings", padding="10")
        file_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Input row
        ttk.Label(file_frame, text="Input Video/Folder:").grid(row=0, column=0, sticky=tk.W, pady=5)
        ttk.Entry(file_frame, textvariable=self.input_var, width=50).grid(row=0, column=1, padx=10, sticky=tk.EW)
        
        btn_frame1 = ttk.Frame(file_frame)
        btn_frame1.grid(row=0, column=2, sticky=tk.E)
        ttk.Button(btn_frame1, text="Browse File", command=self.browse_input_file).pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(btn_frame1, text="Browse Folder", command=self.browse_input_folder).pack(side=tk.LEFT)
        
        # Output row
        ttk.Label(file_frame, text="Output Folder:").grid(row=1, column=0, sticky=tk.W, pady=5)
        ttk.Entry(file_frame, textvariable=self.output_var, width=50).grid(row=1, column=1, padx=10, sticky=tk.EW)
        ttk.Button(file_frame, text="Browse Folder", command=self.browse_output).grid(row=1, column=2, sticky=tk.E)
        
        file_frame.columnconfigure(1, weight=1)
        
        # --- Options ---
        opts_frame = ttk.LabelFrame(main_frame, text="Encoding Options", padding="10")
        opts_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Options: Qualities on the left
        qualities_frame = ttk.Frame(opts_frame)
        qualities_frame.pack(side=tk.LEFT, fill=tk.Y, padx=(0, 20))
        
        ttk.Label(qualities_frame, text="Resolutions & Bitrates:").pack(anchor=tk.W, pady=(0, 5))
        for q in QUALITIES.keys():
            q_frame = ttk.Frame(qualities_frame)
            q_frame.pack(anchor=tk.W, fill=tk.X, pady=1)
            ttk.Checkbutton(q_frame, text=q, variable=self.quality_vars[q], width=6).pack(side=tk.LEFT)
            ttk.Entry(q_frame, textvariable=self.quality_bitrate_vars[q], width=8).pack(side=tk.LEFT, padx=(5,0))

        # Options: Settings on the right
        settings_frame = ttk.Frame(opts_frame)
        settings_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(settings_frame, text="Advanced Settings:").grid(row=0, column=0, sticky=tk.W, pady=(0, 5), columnspan=2)
        
        ttk.Checkbutton(settings_frame, text="Enable Hardware Acceleration (NVENC)", variable=self.hwaccel_var).grid(row=1, column=0, sticky=tk.W, pady=2, columnspan=2)
        ttk.Checkbutton(settings_frame, text="Run Encodings in Parallel (Faster CPU/GPU processing)", variable=self.parallel_var).grid(row=2, column=0, sticky=tk.W, pady=2, columnspan=2)
        
        ttk.Checkbutton(settings_frame, text="Live Streaming Mode (Delete segments, shorter chunks)", variable=self.live_mode_var).grid(row=3, column=0, sticky=tk.W, pady=2, columnspan=2)

        ttk.Checkbutton(settings_frame, text="Keep All Audio & Subtitles (Batch fallback)", variable=self.keep_all_tracks_var).grid(row=4, column=0, sticky=tk.W, pady=2, columnspan=2)
        
        ttk.Checkbutton(settings_frame, text="Upload to Cloudflare R2 (Requires config)", variable=self.upload_r2_var).grid(row=5, column=0, sticky=tk.W, pady=2, columnspan=2)

        stream_frame = ttk.Frame(settings_frame)
        stream_frame.grid(row=6, column=0, sticky=tk.W, columnspan=2, pady=2)
        ttk.Checkbutton(stream_frame, text="Copy Audio", variable=self.copy_audio_var).pack(side=tk.LEFT, padx=(0, 10))
        ttk.Checkbutton(stream_frame, text="Ultra-Fast", variable=self.fast_mode_var).pack(side=tk.LEFT)

        ttk.Label(settings_frame, text="Codec Selection:").grid(row=7, column=0, sticky=tk.W, pady=(5, 2))
        codec_combo = ttk.Combobox(settings_frame, textvariable=self.codec_var, values=["h264", "h265"], state="readonly", width=8)
        codec_combo.grid(row=7, column=1, sticky=tk.W, pady=(5, 2))
        
        crf_frame = ttk.Frame(settings_frame)
        crf_frame.grid(row=8, column=0, sticky=tk.W, columnspan=2, pady=2)
        ttk.Checkbutton(crf_frame, text="Compress File Size (CRF):", variable=self.use_crf_var).pack(side=tk.LEFT)
        ttk.Entry(crf_frame, textvariable=self.crf_val_var, width=5).pack(side=tk.LEFT, padx=(5, 0))
        
        # Add info button for CRF
        info_btn = ttk.Button(crf_frame, text=" i ", width=3, command=self.show_crf_info)
        info_btn.pack(side=tk.LEFT, padx=(5, 0))
        
        ttk.Label(settings_frame, text="Segment Duration (s):").grid(row=9, column=0, sticky=tk.W, pady=(5, 2))
        ttk.Entry(settings_frame, textvariable=self.segment_time_var, width=10).grid(row=9, column=1, sticky=tk.W, pady=(5, 2))
        
        ttk.Checkbutton(settings_frame, text="Single Folder HLS Structure (Recommended)", variable=self.single_folder_var).grid(row=10, column=0, sticky=tk.W, pady=2, columnspan=2)
        
        # --- Track Selection ---
        self.tracks_frame = ttk.LabelFrame(main_frame, text="Track Selection (Single File Only)", padding="10")
        self.tracks_frame.pack(fill=tk.X, pady=(0, 10))
        
        # Audio Tracks Header
        ttk.Label(self.tracks_frame, text="Audio Tracks (Select & Rename):").grid(row=0, column=0, sticky=tk.W, pady=(0, 5))
        
        # Audio Tracks Container (Scrollable)
        self.audio_canvas = tk.Canvas(self.tracks_frame, height=120, highlightthickness=0)
        self.audio_scrollbar = ttk.Scrollbar(self.tracks_frame, orient="vertical", command=self.audio_canvas.yview)
        self.audio_list_frame = ttk.Frame(self.audio_canvas)
        
        self.audio_canvas.configure(yscrollcommand=self.audio_scrollbar.set)
        self.audio_canvas_frame = self.audio_canvas.create_window((0, 0), window=self.audio_list_frame, anchor="nw")
        
        self.audio_list_frame.bind("<Configure>", lambda e: self.audio_canvas.configure(scrollregion=self.audio_canvas.bbox("all")))
        self.audio_canvas.bind("<Configure>", lambda e: self.audio_canvas.itemconfig(self.audio_canvas_frame, width=e.width))
        
        self.audio_canvas.grid(row=1, column=0, sticky=tk.NSEW, padx=(0, 5))
        self.audio_scrollbar.grid(row=1, column=1, sticky=tk.NS)
        
        self.tracks_frame.columnconfigure(0, weight=1)
        
        ttk.Button(self.tracks_frame, text="Load Tracks", command=self.load_tracks).grid(row=0, column=2, rowspan=2, padx=10, sticky=tk.N)
        
        # --- Action Button ---
        btn_action_frame = ttk.Frame(main_frame)
        btn_action_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.start_btn = ttk.Button(btn_action_frame, text="Start Conversion", command=self.start_conversion, style="Accent.TButton")
        self.start_btn.pack(side=tk.LEFT, ipadx=20, ipady=5)
        
        self.cancel_btn = ttk.Button(btn_action_frame, text="Cancel", command=self.cancel_conversion, state="disabled")
        self.cancel_btn.pack(side=tk.LEFT, ipadx=10, ipady=5, padx=(10, 0))
        
        # --- Logs ---
        log_frame = ttk.LabelFrame(main_frame, text="Console Output", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)

        log_controls = ttk.Frame(log_frame)
        log_controls.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(log_controls, text="Clear Logs", command=self.clear_logs).pack(side=tk.RIGHT)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9), height=10)
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
    def on_frame_configure(self, event):
        """Reset the scroll region to encompass the inner frame"""
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def on_canvas_configure(self, event):
        """When canvas is resized, resize the inner frame to match width"""
        self.canvas.itemconfig(self.canvas_frame, width=event.width)
        
    def _on_mousewheel(self, event):
        """Enable mouse wheel scrolling"""
        # Cross-platform handling (mostly Windows/Mac)
        if event.delta:
            self.canvas.yview_scroll(int(-1*(event.delta/120)), "units")
        
    def clear_logs(self):
        """Clears the log widget."""
        self.log_text.configure(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state='disabled')
        
    def show_crf_info(self):
        info_text = (
            "What is CRF (Constant Rate Factor)?\n\n"
            "Normally, videos are encoded with a 'Target Bitrate' (e.g., 5000 kbps for 1080p). "
            "This forces the file to be a specific size, even if a simpler scene didn't need that much data.\n\n"
            "CRF (Constant Rate Factor) changes this. Instead of aiming for a specific file size, it aims for a specific visual quality. "
            "It gives complex scenes more data, and simple scenes less data. Overall, this usually results in a MUCH smaller file size "
            "while looking just as good!\n\n"
            "How to set it:\n"
            "- Lower number = Better Quality, Larger File (e.g., 18-23)\n"
            "- Higher number = Lower Quality, Smaller File (e.g., 28-32)\n"
            "- Default recommended for H.264 is 23.\n"
            "- Default recommended for H.265 is 28."
        )
        messagebox.showinfo("CRF / Compression Info", info_text)
        
    def show_about(self):
        about_text = f"{TOOL_NAME}\nVersion {VERSION}\nCreated by {AUTHOR}\n\nA tool designed to optimize videos into multiple resolutions for production-ready Adaptive HLS (VOD) streaming."
        messagebox.showinfo("About", about_text)
        
    def show_help(self):
        help_text = (
            "How to use the Converter:\n\n"
            "1. If FFmpeg is missing, click 'Tools -> Install Dependencies (FFmpeg)'\n"
            "2. Input: Select a single Video file (.mp4, .mkv, .mov) OR a Folder containing multiple videos.\n"
            "3. Output: Select an empty destination folder.\n"
            "4. Options:\n"
            "   - Hardware Acceleration: Check this if you have an NVIDIA GPU to encode significantly faster.\n"
            "   - Parallel: Encodes all resolutions (1080p, 720p, etc.) simultaneously for a huge speedup.\n"
            "5. Click 'Start Conversion' and wait for the process to finish.\n\n"
            "The output will contain a 'master.m3u8' playlist file ready for CDN upload and web streaming."
        )
        messagebox.showinfo("Help", help_text)
        
    def install_dependencies(self):
        if shutil.which("ffmpeg") is not None:
            ans = messagebox.askyesno("Already Installed", "FFmpeg appears to be installed already. Do you want to re-download it anyway?")
            if not ans:
                return

        def handle_status(msg, is_error):
            if is_error:
                logging.error(msg)
            else:
                logging.info(msg)

        def on_complete():
            self.set_gui_state(tk.NORMAL)
            if check_dependencies():
                messagebox.showinfo("Success", "FFmpeg has been installed successfully!")

        # Disable GUI during download
        self.set_gui_state(tk.DISABLED)
        logging.info("Initiating FFmpeg download...")
        
        thread = threading.Thread(target=download_ffmpeg, args=(handle_status, on_complete))
        thread.daemon = True
        thread.start()

    def browse_input_file(self):
        file_path = filedialog.askopenfilename(title="Select Video File", filetypes=[("Video Files", "*.mp4 *.mkv *.mov *.avi *.webm *.flv"), ("All Files", "*.*")])
        if file_path:
            self.input_var.set(file_path)
            self.load_tracks()
            
    def browse_input_folder(self):
        folder_path = filedialog.askdirectory(title="Select Folder containing Videos")
        if folder_path:
            self.input_var.set(folder_path)
            for child in self.audio_list_frame.winfo_children():
                child.destroy()
            self.current_audio_tracks = []
            self.audio_vars = []
            self.audio_name_vars = []
            self.current_sub_tracks = []
            
    def browse_output(self):
        folder_path = filedialog.askdirectory(title="Select Output Folder")
        if folder_path:
            self.output_var.set(folder_path)
            
    def load_tracks(self):
        input_path = self.input_var.get().strip()
        if not os.path.isfile(input_path):
            messagebox.showwarning("Not a file", "Please select a single valid video file to load tracks. (Track selection is disabled for folders)")
            return
            
        audio_tracks, _ = get_media_track_details(input_path)
        self.current_audio_tracks = audio_tracks
        
        # Clear existing widgets
        for child in self.audio_list_frame.winfo_children():
            child.destroy()
            
        self.audio_vars = []
        self.audio_name_vars = []
        
        for i, t in enumerate(audio_tracks):
            track_frame = ttk.Frame(self.audio_list_frame)
            track_frame.pack(fill=tk.X, pady=2)
            
            # Checkbox for selection
            var = tk.BooleanVar(value=True)
            self.audio_vars.append(var)
            cb = ttk.Checkbutton(track_frame, variable=var)
            cb.pack(side=tk.LEFT, padx=(0, 5))
            
            # Label for track info
            ttk.Label(track_frame, text=f"Audio {i}:", width=8).pack(side=tk.LEFT)
            
            # Original name label (dimmed)
            orig_name = t['display'].split(': ', 1)[1] if ': ' in t['display'] else t['display']
            ttk.Label(track_frame, text=f"({orig_name})", foreground="gray", width=25).pack(side=tk.LEFT, padx=5)
            
            # Entry for custom name
            name_var = tk.StringVar(value=t['title'] if t['title'] else t['lang'])
            self.audio_name_vars.append(name_var)
            ttk.Entry(track_frame, textvariable=name_var, width=15).pack(side=tk.LEFT, padx=5, fill=tk.X, expand=True)
            
        if not audio_tracks:
            ttk.Label(self.audio_list_frame, text="No audio tracks found.").pack(pady=10)
            
    def set_gui_state(self, state):
        for child in self.winfo_children():
            if isinstance(child, tk.Menu): continue
            for grand_child in child.winfo_children():
                try:
                    grand_child.configure(state=state)
                except:
                    pass
        try:
            self.start_btn.configure(state=state)
        except AttributeError:
            pass

    def start_conversion(self):
        input_path = self.input_var.get().strip()
        output_path = self.output_var.get().strip()
        
        try:
            segment_time = self.segment_time_var.get()
            if segment_time <= 0:
                raise ValueError
        except (ValueError, tk.TclError):
            messagebox.showerror("Error", "Please enter a valid positive integer for Segment Duration.")
            return

        selected_qualities = [q for q, var in self.quality_vars.items() if var.get()]
        if not selected_qualities:
            messagebox.showerror("Error", "Please select at least one resolution quality.")
            return
            
        custom_bitrates = {}
        for q in selected_qualities:
            val = self.quality_bitrate_vars[q].get().strip()
            if val:
                custom_bitrates[q] = val
        
        if not input_path or not output_path:
            messagebox.showerror("Error", "Please specify both input and output paths.")
            return
            
        selected_audio_info = None
        
        if os.path.isfile(input_path) and self.current_audio_tracks:
            selected_audio_info = []
            for i, (var, name_var) in enumerate(zip(self.audio_vars, self.audio_name_vars)):
                if var.get():
                    selected_audio_info.append({
                        'index': self.current_audio_tracks[i]['relative_index'],
                        'name': name_var.get().strip() or f"Audio {i}",
                        'lang': self.current_audio_tracks[i]['lang']
                    })
            
        # Disable UI elements
        self.set_gui_state(tk.DISABLED)
        self.start_btn.configure(text="Converting...", state=tk.DISABLED)
        self.cancel_btn.configure(state=tk.NORMAL)
        
        self.cancel_event = threading.Event()
        
        hwaccel = self.hwaccel_var.get()
        parallel = self.parallel_var.get()
        single_folder = self.single_folder_var.get()
        live_mode = self.live_mode_var.get()
        keep_all_tracks = self.keep_all_tracks_var.get()
        upload_to_r2 = self.upload_r2_var.get()
        copy_audio = self.copy_audio_var.get()
        fast_mode = self.fast_mode_var.get()
        
        def on_complete():
            self.set_gui_state(tk.NORMAL)
            self.start_btn.configure(text="Start Conversion", state=tk.NORMAL)
            self.cancel_btn.configure(state=tk.DISABLED)
            if hasattr(self, 'cancel_event') and self.cancel_event.is_set():
                messagebox.showinfo("Cancelled", "Conversion process was cancelled.")
            else:
                messagebox.showinfo("Done", "Conversion process finished! Check logs for details.")
            
        codec = self.codec_var.get()
        crf = None
        if self.use_crf_var.get():
            try:
                crf = int(self.crf_val_var.get())
            except ValueError:
                messagebox.showerror("Error", "Please enter a valid integer for CRF value.")
                self.set_gui_state(tk.NORMAL)
                self.start_btn.configure(text="Start Conversion", state=tk.NORMAL)
                self.cancel_btn.configure(state=tk.DISABLED)
                return

        # Run conversion in a separate thread
        thread = threading.Thread(target=run_conversion_job, args=(input_path, output_path, hwaccel, parallel, selected_qualities, segment_time, codec, crf, custom_bitrates, on_complete, self.cancel_event, single_folder, live_mode, keep_all_tracks, selected_audio_info, upload_to_r2, copy_audio, fast_mode))
        thread.daemon = True
        thread.start()

    def cancel_conversion(self):
        if hasattr(self, 'cancel_event'):
            self.cancel_event.set()
            self.start_btn.configure(text="Canceling...")
            self.cancel_btn.configure(state=tk.DISABLED)


# ==========================================
# CLI / MAIN
# ==========================================

def main():
    # If no arguments are provided, launch GUI automatically
    if len(sys.argv) == 1:
        if HAS_GUI:
            app = ConverterGUI()
            
            # Simple styling
            style = ttk.Style()
            if 'clam' in style.theme_names():
                style.theme_use('clam')
            style.configure("Accent.TButton", font=("Helvetica", 10, "bold"))
            
            app.mainloop()
        else:
            print("Tkinter is not available in your Python installation.")
            print("Please run with -h for CLI usage.")
        return

    # CLI Parser
    parser = argparse.ArgumentParser(
        description="Convert videos to highly optimized adaptive HLS streaming format (VOD)."
    )
    parser.add_argument("input", help="Source video file OR directory containing videos for batch processing.")
    parser.add_argument("output", help="Destination folder. Each video will get its own subfolder here.")
    parser.add_argument("--hwaccel", action="store_true", help="Enable NVIDIA NVENC hardware acceleration (h264_nvenc / hevc_nvenc).")
    parser.add_argument("--parallel", action="store_true", help="Enable parallel processing of different qualities for a huge speedup.")
    parser.add_argument("--qualities", nargs="+", default=list(QUALITIES.keys()), choices=list(QUALITIES.keys()), help="Specify the resolutions to generate (e.g., 1080p 720p).")
    parser.add_argument("--segment_time", type=int, default=6, help="HLS segment duration in seconds (default: 6).")
    parser.add_argument("--codec", choices=["h264", "h265"], default="h264", help="Video codec to use (h264 or h265).")
    parser.add_argument("--crf", type=int, default=None, help="Use Constant Rate Factor (CRF) for compression instead of strict bitrates (e.g., 28 for H.265).")
    parser.add_argument("--single_folder", action="store_true", help="Put all playlists and segments in a single directory (Recommended).")
    parser.add_argument("--live", action="store_true", help="Live streaming mode (shorter segments, delete old segments).")
    parser.add_argument("--keep_all_tracks", action="store_true", help="Keep all audio and subtitle tracks (Multilingual / SRT support).")
    for q in QUALITIES.keys():
        parser.add_argument(f"--bitrate_{q}", type=str, help=f"Override bitrate for {q} (e.g., 5000k).")
    
    args = parser.parse_args()
    
    custom_bitrates = {}
    for q in QUALITIES.keys():
        arg_val = getattr(args, f"bitrate_{q}", None)
        if arg_val:
            custom_bitrates[q] = arg_val
    
    setup_logger() # Standard stdout logger for CLI
    run_conversion_job(args.input, args.output, args.hwaccel, args.parallel, selected_qualities=args.qualities, segment_time=args.segment_time, codec=args.codec, crf=args.crf, custom_bitrates=custom_bitrates, single_folder=args.single_folder, live_mode=args.live, keep_all_tracks=args.keep_all_tracks)

if __name__ == "__main__":
    main()
