"""
Adaptive HLS Video Converter
Converts video files into multiple resolutions optimized for HLS streaming (VOD).
Provides both CLI and GUI interfaces with built-in FFmpeg dependency management.
"""

import argparse
import os
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
from pathlib import Path

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, scrolledtext
    HAS_GUI = True
except ImportError:
    HAS_GUI = False

TOOL_NAME = "Adaptive HLS Video Converter"
VERSION = "1.1.0"
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
LOCAL_BIN_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffmpeg_bin")
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
    if shutil.which("ffmpeg") is None:
        logging.error("FFmpeg could not be found. Please ensure it is installed and added to your system PATH.")
        logging.info("Hint: Use the 'Tools -> Install Dependencies (FFmpeg)' menu in the GUI to download it automatically.")
        return False
    return True

def process_resolution(input_file, output_dir, quality, config, use_hwaccel=False, segment_time=6):
    """Spawns an FFmpeg process to convert the input video into a specific HLS resolution."""
    resolution_dir = os.path.join(output_dir, quality)
    os.makedirs(resolution_dir, exist_ok=True)
    
    playlist_path = os.path.join(resolution_dir, 'index.m3u8')
    segment_pattern = os.path.join(resolution_dir, 'segment_%03d.ts')
    
    vcodec = 'h264_nvenc' if use_hwaccel else 'libx264'
    preset = 'p2' if use_hwaccel else 'fast'
    
    # Aspect-safe scaling. Width is automatically calculated (-2 ensures the dimension is divisible by 2 for h264 compliance)
    scale_filter = f"scale=-2:{config['height']}"
    
    bitrate_val = int(config['bitrate'].replace('k', ''))
    bufsize = f"{int(bitrate_val * 1.5)}k"
    
    cmd = [
        'ffmpeg',
        '-hide_banner',
        '-y',
        '-i', input_file,
        '-map', '0:v:0',
        '-map', '0:a:0?', # The '?' means it won't fail if the video has no audio
        '-vf', scale_filter,
        '-c:v', vcodec,
        '-preset', preset,
        '-b:v', config['bitrate'],
        '-maxrate', config['bitrate'],
        '-bufsize', bufsize,
        '-c:a', 'aac',
        '-b:a', config['audio_bitrate'],
        '-ar', '48000',
        '-profile:v', 'main',
        '-g', '48',
        '-keyint_min', '48',
        '-sc_threshold', '0',
        '-hls_time', str(segment_time),
        '-hls_playlist_type', 'vod',
        '-hls_segment_filename', segment_pattern,
        playlist_path
    ]
    
    logging.info(f"[{quality}] Starting encoding...")
    
    try:
        process = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            universal_newlines=True,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        )
        
        if process.returncode != 0:
            logging.error(f"[{quality}] FFmpeg encoding failed.\nError snippet: {process.stderr[-500:]}")
            return False, quality
            
        logging.info(f"[{quality}] Successfully finished encoding!")
        return True, quality
        
    except Exception as e:
        logging.error(f"[{quality}] Exception occurred: {str(e)}")
        return False, quality

def generate_master_playlist(output_dir, successful_qualities):
    """Creates a master.m3u8 playlist combining all generated resolutions."""
    master_playlist_path = os.path.join(output_dir, 'master.m3u8')
    
    bandwidths = {
        '1080p': 5000000,
        '720p': 2800000,
        '480p': 1400000,
        '360p': 800000,
        '240p': 400000
    }
    
    with open(master_playlist_path, 'w') as f:
        f.write("#EXTM3U\n")
        f.write("#EXT-X-VERSION:3\n")
        
        sorted_qualities = sorted(
            successful_qualities, 
            key=lambda x: bandwidths.get(x, 0), 
            reverse=True
        )
        
        for q in sorted_qualities:
            bw = bandwidths.get(q, 800000)
            
            # Since we dynamically scale width based on height to maintain aspect ratio,
            # we write approximate standard 16:9 resolutions into the Master playlist for Apple/HLS player compatibility
            fallback_widths = { '1080p': '1920x1080', '720p': '1280x720', '480p': '854x480', '360p': '640x360', '240p': '426x240' }
            res = fallback_widths.get(q, '1280x720')
            
            f.write(f"#EXT-X-STREAM-INF:BANDWIDTH={bw},RESOLUTION={res}\n")
            f.write(f"{q}/index.m3u8\n")
            
    logging.info(f"[Master] Master playlist created: {master_playlist_path}")

def process_video_file(video_path, output_base_dir, use_hwaccel, parallel, selected_qualities=None, segment_time=6):
    """Runs the conversion flow for a single video."""
    video_name = os.path.splitext(os.path.basename(video_path))[0]
    output_dir = os.path.join(output_base_dir, video_name)
    os.makedirs(output_dir, exist_ok=True)
    
    if selected_qualities is None or len(selected_qualities) == 0:
        selected_qualities = list(QUALITIES.keys())
        
    qualities_to_process = {q: QUALITIES[q] for q in selected_qualities if q in QUALITIES}
    
    logging.info(f"--- Starting task for '{video_name}' ---")
    
    successful_qualities = []
    
    if parallel:
        logging.info("Running encodings in parallel...")
        workers = min(len(qualities_to_process), (os.cpu_count() or 4))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(process_resolution, video_path, output_dir, q, c, use_hwaccel, segment_time) 
                for q, c in qualities_to_process.items()
            ]
            
            for future in concurrent.futures.as_completed(futures):
                success, quality = future.result()
                if success:
                    successful_qualities.append(quality)
    else:
        logging.info("Running encodings sequentially...")
        for q, c in qualities_to_process.items():
            success, quality = process_resolution(video_path, output_dir, q, c, use_hwaccel, segment_time)
            if success:
                successful_qualities.append(quality)
                
    if successful_qualities:
        generate_master_playlist(output_dir, successful_qualities)
        logging.info(f"--- Finished processing '{video_name}' successfully ---\n")
        return True
    else:
        logging.error(f"--- Failed to process '{video_name}' ---\n")
        return False

def run_conversion_job(input_path, output_path, hwaccel, parallel, selected_qualities=None, segment_time=6, on_complete=None):
    """Entry point for conversion job, handles both batch and single files."""
    if not check_dependencies():
        if on_complete: on_complete()
        return

    if os.path.isfile(input_path):
        process_video_file(input_path, output_path, hwaccel, parallel, selected_qualities, segment_time)
        
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
                logging.info(f"Processing video {i+1} of {len(video_files)}...")
                process_video_file(video_file, output_path, hwaccel, parallel, selected_qualities, segment_time)
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
        self.geometry("800x650")
        self.minsize(650, 550)
        
        self.input_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.hwaccel_var = tk.BooleanVar(value=False)
        self.parallel_var = tk.BooleanVar(value=True)
        self.segment_time_var = tk.IntVar(value=6)
        
        # Dictionary to hold the boolean vars for each quality
        self.quality_vars = {}
        for q in QUALITIES.keys():
            self.quality_vars[q] = tk.BooleanVar(value=True)
        
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
        # Main Frame
        main_frame = ttk.Frame(self, padding="15")
        main_frame.pack(fill=tk.BOTH, expand=True)
        
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
        
        ttk.Label(qualities_frame, text="Resolutions:").pack(anchor=tk.W, pady=(0, 5))
        for q in QUALITIES.keys():
            ttk.Checkbutton(qualities_frame, text=q, variable=self.quality_vars[q]).pack(anchor=tk.W)

        # Options: Settings on the right
        settings_frame = ttk.Frame(opts_frame)
        settings_frame.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        ttk.Label(settings_frame, text="Advanced Settings:").grid(row=0, column=0, sticky=tk.W, pady=(0, 5), columnspan=2)
        
        ttk.Checkbutton(settings_frame, text="Enable Hardware Acceleration (NVENC)", variable=self.hwaccel_var).grid(row=1, column=0, sticky=tk.W, pady=2, columnspan=2)
        ttk.Checkbutton(settings_frame, text="Run Encodings in Parallel (Faster CPU/GPU processing)", variable=self.parallel_var).grid(row=2, column=0, sticky=tk.W, pady=2, columnspan=2)
        
        ttk.Label(settings_frame, text="Segment Duration (s):").grid(row=3, column=0, sticky=tk.W, pady=(10, 2))
        ttk.Entry(settings_frame, textvariable=self.segment_time_var, width=10).grid(row=3, column=1, sticky=tk.W, pady=(10, 2))
        
        # --- Action Button ---
        btn_action_frame = ttk.Frame(main_frame)
        btn_action_frame.pack(fill=tk.X, pady=(0, 10))
        
        self.start_btn = ttk.Button(btn_action_frame, text="Start Conversion", command=self.start_conversion, style="Accent.TButton")
        self.start_btn.pack(side=tk.RIGHT, ipadx=20, ipady=5)
        
        # --- Logs ---
        log_frame = ttk.LabelFrame(main_frame, text="Console Output", padding="10")
        log_frame.pack(fill=tk.BOTH, expand=True)

        log_controls = ttk.Frame(log_frame)
        log_controls.pack(fill=tk.X, pady=(0, 5))
        ttk.Button(log_controls, text="Clear Logs", command=self.clear_logs).pack(side=tk.RIGHT)
        
        self.log_text = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#1e1e1e", fg="#d4d4d4", font=("Consolas", 9))
        self.log_text.pack(fill=tk.BOTH, expand=True)
        
    def clear_logs(self):
        """Clears the log widget."""
        self.log_text.configure(state='normal')
        self.log_text.delete(1.0, tk.END)
        self.log_text.configure(state='disabled')
        
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
            
    def browse_input_folder(self):
        folder_path = filedialog.askdirectory(title="Select Folder containing Videos")
        if folder_path:
            self.input_var.set(folder_path)
            
    def browse_output(self):
        folder_path = filedialog.askdirectory(title="Select Output Folder")
        if folder_path:
            self.output_var.set(folder_path)
            
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
        
        if not input_path or not output_path:
            messagebox.showerror("Error", "Please specify both input and output paths.")
            return
            
        # Disable UI elements
        self.set_gui_state(tk.DISABLED)
        self.start_btn.configure(text="Converting...")
        
        hwaccel = self.hwaccel_var.get()
        parallel = self.parallel_var.get()
        
        def on_complete():
            self.set_gui_state(tk.NORMAL)
            self.start_btn.configure(text="Start Conversion")
            messagebox.showinfo("Done", "Conversion process finished! Check logs for details.")
            
        # Run conversion in a separate thread
        thread = threading.Thread(target=run_conversion_job, args=(input_path, output_path, hwaccel, parallel, selected_qualities, segment_time, on_complete))
        thread.daemon = True
        thread.start()


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
    parser.add_argument("--hwaccel", action="store_true", help="Enable NVIDIA NVENC hardware acceleration (h264_nvenc).")
    parser.add_argument("--parallel", action="store_true", help="Enable parallel processing of different qualities for a huge speedup.")
    parser.add_argument("--qualities", nargs="+", default=list(QUALITIES.keys()), choices=list(QUALITIES.keys()), help="Specify the resolutions to generate (e.g., 1080p 720p).")
    parser.add_argument("--segment_time", type=int, default=6, help="HLS segment duration in seconds (default: 6).")
    
    args = parser.parse_args()
    
    setup_logger() # Standard stdout logger for CLI
    run_conversion_job(args.input, args.output, args.hwaccel, args.parallel, selected_qualities=args.qualities, segment_time=args.segment_time)

if __name__ == "__main__":
    main()
