import os
import subprocess
import threading
import time
import tempfile
import collections
import wave

import dxcam
import cv2
import numpy as np
import pyaudiowpatch as p
import warnings

try:
    import psutil as _psutil
except ImportError:
    _psutil = None
try:
    import procloop as _procloop
except (ImportError, OSError):
    _procloop = None
warnings.filterwarnings("ignore", category=p.PyAudioWPatchWarning if hasattr(p, 'PyAudioWPatchWarning') else UserWarning)

# ============================================================
#  RECORDER — CONTINUOUS GPU REPLAY BUFFER + WASAPI AUDIO
#
#  Video path: dxcam frames -> JPEG -> live ffmpeg (GPU H.264,
#  fragmented MP4) -> stdout pipe -> background thread reads
#  small blocks into a rolling deque with a strict maxlen.
#  RAM stays flat no matter how long the session runs.
#  Save = flush deque to cache file + instant `-c:v copy` remux.
#
#  Audio path: pyaudiowpatch WASAPI mic + loopback into small
#  timestamped ring buffers, mixed onto the video clock at save.
# ============================================================

# Audio chunk size: 4800 frames = 0.1s at 48000 Hz
_CHUNK_FRAMES = 4_800

# Never show console windows for child ffmpeg processes (the exe itself
# is windowless; without this every ffmpeg spawn flashes a console).
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Compression levels: GPU CQP / CPU CRF+preset / VBR maxrate / RAM cap.
# Higher compression = smaller files = smaller ring needed for the same
# buffer length. Snapshot at start(); applies on restart.
_COMPRESSION_PROFILES = {
    "High": {"cqp": 25, "crf": 25, "x264_preset": "ultrafast",
             "maxrate_m": 12, "guess_m": 25, "ram_cap_mb": 40},
    "Medium": {"cqp": 22, "crf": 22, "x264_preset": "superfast",
               "maxrate_m": 20, "guess_m": 40, "ram_cap_mb": 80},
    "Low": {"cqp": 18, "crf": 19, "x264_preset": "veryfast",
            "maxrate_m": 40, "guess_m": 70, "ram_cap_mb": 140},
}

# Fragment ring I/O
_FRAG_BLOCK = 128 * 1024         # stdout read size / deque unit (fewer syscalls)
_FRAG_HEADROOM = 1.3             # deque capacity vs bitrate-cap estimate
_FRAG_BLOCK_MIN = 64             # minimum deque blocks
_INIT_PENDING_MAX = 2 * 1024 * 1024  # abort if init segment exceeds this

# Stream restart policy
_RESTART_MAX = 3
_RESTART_COOLDOWN = 1.0

# Set to a dict to capture sync-mapping diagnostics on save (tests only)
SYNC_DEBUG = None


# --------------------------------------------------------
# Fragmented-MP4 box helpers
# --------------------------------------------------------
def _mp4_top_boxes(data, start=0):
    """Parse top-level boxes from `start`. Returns (boxes, consumed_end).

    boxes = [(type_bytes, box_offset, box_size)]. Stops at the first
    incomplete/trailing partial box; consumed_end covers complete boxes.
    """
    boxes = []
    i, n = start, len(data)
    while i + 8 <= n:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = bytes(data[i + 4:i + 8])
        if size == 1:
            if i + 16 > n:
                break
            size = int.from_bytes(data[i + 8:i + 16], "big")
            hdr = 16
        elif size == 0:
            break
        else:
            hdr = 8
        if size < hdr or i + size > n:
            break
        boxes.append((typ, i, size))
        i += size
    return boxes, i


def _mp4_children(data, start, end):
    """Yield (type, offset, size, header_len) for boxes in [start, end)."""
    i = start
    while i + 8 <= end:
        size = int.from_bytes(data[i:i + 4], "big")
        typ = bytes(data[i + 4:i + 8])
        if size == 1:
            if i + 16 > end:
                return
            size = int.from_bytes(data[i + 8:i + 16], "big")
            hdr = 16
        elif size == 0:
            return
        else:
            hdr = 8
        if size < hdr or i + size > end:
            return
        yield typ, i, size, hdr
        i += size


def _resync_moof(data):
    """Find first plausible moof box (deque starts mid-box after eviction).

    Returns the box start offset or None.
    """
    n = len(data)
    pos = data.find(b"moof")
    while pos != -1:
        off = pos - 4  # size field precedes the type
        if off >= 0 and off + 8 <= n:
            size = int.from_bytes(data[off:off + 4], "big")
            if 16 <= size <= n - off:
                # Validate: next box must also parse
                nxt = off + size
                if nxt + 8 > n:
                    return off  # moof runs to end of snapshot
                ns = int.from_bytes(data[nxt:nxt + 4], "big")
                if ns == 0 or (8 <= ns <= n - nxt):
                    return off
        pos = data.find(b"moof", pos + 1)
    return None


def _scan_fragments(data):
    """Resync + parse top boxes. Returns (boxes, consumed_end)."""
    boxes, consumed = _mp4_top_boxes(data, 0)
    if any(t == b"moof" for t, _, _ in boxes):
        return boxes, consumed
    off = _resync_moof(data)
    if off is None:
        return [], 0
    boxes, consumed = _mp4_top_boxes(data, off)
    return boxes, consumed


def _find_init_end(data):
    """End offset of the moov box (init segment), or None if incomplete."""
    boxes, _ = _mp4_top_boxes(bytes(data), 0)
    for typ, off, size in boxes:
        if typ == b"moov":
            return off + size
    return None


def _video_timescale(init_seg):
    """Video track timescale from init moov (mdhd of the 'vide' trak)."""
    try:
        for typ, off, size, hdr in _mp4_children(init_seg, 0, len(init_seg)):
            if typ != b"moov":
                continue
            for t2, o2, s2, h2 in _mp4_children(init_seg, off + hdr, off + size):
                if t2 != b"trak":
                    continue
                mdia = None
                for t3, o3, s3, h3 in _mp4_children(init_seg, o2 + h2, o2 + s2):
                    if t3 == b"mdia":
                        mdia = (o3, s3, h3)
                        break
                if mdia is None:
                    continue
                mo, ms, mh = mdia
                handler = None
                timescale = None
                for t4, o4, s4, h4 in _mp4_children(init_seg, mo + mh, mo + ms):
                    body = o4 + h4
                    if t4 == b"hdlr" and body + 12 <= o4 + s4:
                        handler = bytes(init_seg[body + 8:body + 12])
                    elif t4 == b"mdhd" and body + 24 <= o4 + s4:
                        ver = init_seg[body]
                        ts_off = body + (20 if ver == 1 else 12)
                        timescale = int.from_bytes(
                            init_seg[ts_off:ts_off + 4], "big")
                if handler == b"vide" and timescale:
                    return timescale
    except (IndexError, ValueError):
        pass
    return None


def _trex_defaults(init_seg):
    """Map track_ID -> default_sample_flags from moov/mvex/trex."""
    out = {}
    try:
        for typ, off, size, hdr in _mp4_children(init_seg, 0, len(init_seg)):
            if typ != b"moov":
                continue
            for t2, o2, s2, h2 in _mp4_children(init_seg, off + hdr, off + size):
                if t2 != b"mvex":
                    continue
                for t3, o3, s3, h3 in _mp4_children(init_seg, o2 + h2, o2 + s2):
                    if t3 != b"trex":
                        continue
                    body = o3 + h3
                    if body + 20 <= o3 + s3:
                        tid = int.from_bytes(init_seg[body + 4:body + 8], "big")
                        flags = int.from_bytes(init_seg[body + 12:body + 16], "big")
                        out[tid] = flags
    except (IndexError, ValueError):
        pass
    return out


def _moof_info(data, moof_off, moof_size, trex):
    """First traf of a moof. Returns (is_sync_or_None, base_time_or_None)."""
    is_sync = None
    base_time = None
    try:
        for typ, off, size, hdr in _mp4_children(
                data, moof_off + 8, moof_off + moof_size):
            if typ != b"traf":
                continue
            track_id = None
            for t2, o2, s2, h2 in _mp4_children(data, off + hdr, off + size):
                body = o2 + h2
                if t2 == b"tfhd" and body + 8 <= o2 + s2:
                    track_id = int.from_bytes(data[body + 4:body + 8], "big")
                elif t2 == b"tfdt" and body + 4 <= o2 + s2:
                    ver = data[body]
                    if ver == 1 and body + 12 <= o2 + s2:
                        base_time = int.from_bytes(data[body + 4:body + 12], "big")
                    elif body + 8 <= o2 + s2:
                        base_time = int.from_bytes(data[body + 4:body + 8], "big")
                elif t2 == b"trun" and body + 8 <= o2 + s2:
                    flags = int.from_bytes(data[body:body + 4], "big") & 0xFFFFFF
                    count = int.from_bytes(data[body + 4:body + 8], "big")
                    if count == 0:
                        continue
                    pos = body + 8
                    end = o2 + s2
                    if flags & 0x000001:
                        pos += 4
                    first_flags = None
                    if flags & 0x000004:
                        if pos + 4 <= end:
                            first_flags = int.from_bytes(data[pos:pos + 4], "big")
                    elif flags & 0x000020:
                        if flags & 0x000008:
                            pos += 4
                        if flags & 0x000010:
                            pos += 4
                        if pos + 4 <= end:
                            first_flags = int.from_bytes(data[pos:pos + 4], "big")
                    elif track_id in trex:
                        first_flags = trex[track_id]
                    if first_flags is not None:
                        is_sync = (first_flags & 0x00010000) == 0
            break  # first traf only (video-only stream)
    except (IndexError, ValueError):
        pass
    return is_sync, base_time


def _count_video_samples(frag, boxes, cut_off):
    """Total trun sample counts for moofs at/after cut_off."""
    total = 0
    try:
        for typ, off, size in boxes:
            if typ != b"moof" or off < cut_off:
                continue
            for t2, o2, s2, h2 in _mp4_children(frag, off + 8, off + size):
                if t2 != b"traf":
                    continue
                for t3, o3, s3, h3 in _mp4_children(frag, o2 + h2, o2 + s2):
                    if t3 == b"trun":
                        body = o3 + h3
                        if body + 8 <= o3 + s3:
                            total += int.from_bytes(
                                frag[body + 4:body + 8], "big")
    except (IndexError, ValueError):
        pass
    return total


class Recorder:
    def __init__(self, settings, app_dir=None):
        self.settings = settings
        self.app_dir = app_dir or os.path.dirname(os.path.abspath(__file__))

        self.recording = False
        self.mic_enabled = bool(settings.get("record_microphone", True))
        self.system_enabled = bool(settings.get("record_system_audio", True))
        self.mic_device = settings.get("mic_audio_device", "")
        self.system_device = settings.get("system_audio_device", "")
        self.mic_volume = max(0, min(100, int(settings.get("mic_volume", 100))))
        self.system_volume = max(0, min(100, int(settings.get("system_volume", 100))))
        self.last_mic_volume = Recorder._remembered_last(
            settings.get("last_mic_volume"), self.mic_volume)
        self.last_system_volume = Recorder._remembered_last(
            settings.get("last_system_volume"), self.system_volume)
        # Per-app clip-gain intent table {exe: 0-100}, synced from the UI.
        # Informational until per-app capture stems exist: with only a
        # single mixed loopback tap, per-app gains have no separable
        # waveform to scale (scaling the mix would hit every app).
        self.app_volumes = dict(settings.get("app_volumes", {}) or {})
        try:
            self.monitor_index = max(0, int(settings.get("monitor_index", 0)))
        except (ValueError, TypeError):
            self.monitor_index = 0

        # Encoder detection (presence) + smoke-validated live args
        self._encoder, self._encoder_preset, self._encoder_quality = self._detect_encoder()
        self._live_encoder = None
        self._live_args = None
        self._live_cap_bps = None
        self._live_fps = None
        self._live_input = "cpu"
        self._active_profile = dict(_COMPRESSION_PROFILES["Medium"])

        # Fragment ring: byte-blocks of live fMP4 from ffmpeg stdout.
        # RAM is capped by EXACT byte budget (not block count: pipe reads
        # return partial blocks, so a count cap would evict early).
        # Open WASAPI streams (registered by capture threads so stop can
        # close them first: closing unblocks readers; terminating PortAudio
        # with open/blocked streams hangs).
        self._frag_lock = threading.Lock()
        self._frag_deque = collections.deque()
        self._frag_bytes = 0
        self._frag_budget = self._frag_budget_bytes()
        self._init_seg = None          # cached fMP4 init segment (ftyp+moov)
        self._init_pending = bytearray()
        self._stream_wall_start = 0.0  # monotonic() at ffmpeg launch
        self._last_feed_wall = 0.0     # monotonic() of last frame fed in
        # Frame wall clock: monotonic() per accepted stream frame.
        # Maps stream frame index -> wall time exactly (immune to
        # pacing overruns that skew nominal stream_time vs wall).
        self._feed_walls = collections.deque(maxlen=1024)
        self._feed_total = 0           # absolute stream frame counter

        # Audio replay buffers: (pcm bytes, sr, ch, chunk_start_mono).
        # Tiny (~6 MB for 30 s stereo) vs hundreds of MB of video.
        max_audio_chunks = max(1, int(settings.get("buffer_seconds", 20)) * 10)
        self.mic_audio_replay = collections.deque(maxlen=max_audio_chunks)
        self.sys_audio_replay = collections.deque(maxlen=max_audio_chunks)

        # Audio meter buffers (for UI level display)
        self.mic_audio_buffer = collections.deque(maxlen=20)
        self.system_audio_buffer = collections.deque(maxlen=20)

        # Thread control
        self._cap_thread = None
        self._write_thread = None
        self._read_thread = None
        self._latest_lock = threading.Lock()
        self._latest_jpeg = None
        self._audio_threads = []
        self._audio_generation = 0
        # Per-app capture (process loopback stems; empty until the UI
        # supervisor reports sounding sessions)
        self._app_captures = {}    # exe -> {pid, alive, landed, thread}
        self._app_threads = []
        self._app_retry = {}       # exe -> monotonic() cooldown end
        self.app_audio_replay = {}  # exe -> deque[(pcm,sr,ch,t)]
        self._master_thread = None
        self._loopback_dev = None
        self._stream_generation = 0
        self._stream_restarts = 0
        self._ffmpeg_proc = None

        # Diagnostics
        self._recording_start = 0.0
        self._mic_start_time = 0.0
        self._sys_start_time = 0.0
        self.last_stream_error = None
        self.last_save_error = None
        self.last_save_info = None

        # Locks
        self.lock = threading.Lock()

        # Camera
        self.camera = None

        # Preview thumbnail for UI (JPEG bytes, throttled)
        self.preview_jpeg = None
        self.preview_lock = threading.Lock()

        # Audio error visibility
        self.audio_errors = collections.deque(maxlen=50)
        self.last_audio_error = None
        # Dropout counters: empty/short reads + read errors per stream.
        # Nonzero under load = capture starvation (see _boost_audio_thread).
        self.audio_drops = {"mic": 0, "sys": 0}

    # --------------------------------------------------------
    # HELPERS
    # --------------------------------------------------------

    def _ffmpeg_path(self):
        bundled = os.path.join(self.app_dir, "ffmpeg", "bin", "ffmpeg.exe")
        return bundled if os.path.isfile(bundled) else "ffmpeg"

    def icon_path(self):
        """Official app icon inside the bundled icons/ folder.

        Prefers killcam.ico, accepts common alternates by extension.
        Returns the path or None when no asset is present.
        """
        icons_dir = os.path.join(self.app_dir, "icons")
        for name in ("killcam.ico", "icon.ico", "killcam.png", "icon.png"):
            path = os.path.join(icons_dir, name)
            if os.path.isfile(path):
                return path
        return None

    def _detect_encoder(self):
        """Detect best available hardware encoder. Returns (encoder, preset, quality_args)."""
        try:
            completed = subprocess.run(
                [self._ffmpeg_path(), "-encoders"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW)
            encoders = completed.stdout
        except Exception:
            encoders = ""

        # Priority: NVIDIA > AMD > Intel > CPU fallback
        if "h264_nvenc" in encoders:
            return ("h264_nvenc", "p4", ["-rc", "constqp", "-qp", "22"])
        if "h264_amf" in encoders:
            return ("h264_amf", "speed", ["-rc", "constqp", "-qp", "22"])
        if "h264_qsv" in encoders:
            return ("h264_qsv", "fast", ["-global_quality", "22"])
        return ("libx264", "ultrafast", ["-crf", "22"])

    def _live_input_flags(self):
        """Input flag set for the live pipe (CPU MJPEG decode).

        (A GPU-decode variant was trialed and removed: mjpeg_cuvid init
        hangs nondeterministically on some sessions instead of failing,
        stalling startup. CPU decode is verified on all paths.)
        """
        return ["-thread_queue_size", "1024",
                "-f", "image2pipe", "-vcodec", "mjpeg"]

    def _compression_profile(self):
        """Normalized compression profile from settings (default Medium)."""
        name = str(self.settings.get("compression", "Medium")).strip().capitalize()
        if name not in _COMPRESSION_PROFILES:
            name = "Medium"
        return dict(_COMPRESSION_PROFILES[name])

    def _live_candidates(self, fps=60):
        """(encoder, live_args, bitrate_cap_bps) ordered by preference.

        Quality/preset/caps come from the active compression profile.
        Caps scale with fps so 120/240fps get headroom while 30/60fps
        stay lean (deque RAM ceiling follows the cap).
        """
        prof = self._active_profile
        scale = max(0.6, fps / 60.0)
        maxrate_m = max(6, round(prof["maxrate_m"] * scale))
        vbr_cap = "%dM" % maxrate_m
        bufsize = "%dM" % (maxrate_m * 2)
        guess_cap = int(prof["guess_m"] * 1_000_000 * scale)
        cqp = prof["cqp"]
        return [
            ("h264_nvenc", ["-preset", "p4", "-rc", "vbr", "-cq", str(cqp),
                            "-maxrate", vbr_cap, "-bufsize", bufsize,
                            "-bf", "0"], int(prof["maxrate_m"] * 1_000_000 * scale)),
            ("h264_amf", ["-quality", "speed", "-rc", "cqp",
                          "-qp_i", str(cqp), "-qp_p", str(cqp)], guess_cap),
            ("h264_qsv", ["-preset", "fast", "-global_quality", str(cqp)], guess_cap),
            ("libx264", ["-preset", prof["x264_preset"], "-tune", "zerolatency",
                         "-crf", str(prof["crf"]), "-maxrate", vbr_cap,
                         "-bufsize", bufsize, "-bf", "0"],
             int(prof["maxrate_m"] * 1_000_000 * scale)),
        ]

    def _ensure_live_args(self, fps=60):
        """Validate the full live chain end-to-end; fall back until one works.

        Builds a probe MJPEG file and runs each candidate encoder through
        the real fragmented-MP4 pipeline to null.
        """
        if self._live_encoder is not None:
            return
        try:
            completed = subprocess.run(
                [self._ffmpeg_path(), "-encoders"],
                capture_output=True, text=True, timeout=10,
                creationflags=_NO_WINDOW)
            available = completed.stdout
        except Exception:
            available = ""
        last_err = "ffmpeg not found"
        probe = tempfile.mktemp(suffix=".mjpeg")
        try:
            gen = subprocess.run(
                [self._ffmpeg_path(), "-y",
                 "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=30:duration=1",
                 "-c:v", "mjpeg", "-q:v", "3", probe],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, creationflags=_NO_WINDOW)
            if gen.returncode != 0 or not os.path.isfile(probe):
                raise RuntimeError("probe build failed")
            for enc, args, cap in self._live_candidates(fps):
                if enc != "libx264" and enc not in available:
                    continue
                cmd = ([self._ffmpeg_path(), "-y"]
                       + self._live_input_flags()
                       + ["-framerate", str(fps), "-i", probe,
                          "-c:v", enc] + args + ["-g", "15",
                          "-pix_fmt", "yuv420p"])
                cmd += ["-force_key_frames", "expr:gte(t,n_forced*0.25)",
                        "-f", "mp4",
                        "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                        "-t", "0.5", "-f", "null", "-"]
                try:
                    proc = subprocess.run(
                        cmd, stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE, timeout=30,
                        creationflags=_NO_WINDOW)
                except (OSError, subprocess.TimeoutExpired) as exc:
                    last_err = str(exc)
                    continue
                if proc.returncode == 0:
                    self._live_encoder = enc
                    self._live_args = args
                    self._live_cap_bps = cap
                    self._live_input = "cpu"
                    self._encoder = enc  # keep legacy attr in sync
                    return
                last_err = proc.stderr.decode(
                    "utf-8", errors="replace")[-300:]
        finally:
            try:
                if os.path.isfile(probe):
                    os.remove(probe)
            except OSError:
                pass
        raise RuntimeError("No working video pipeline: %s" % last_err)

    def _frag_budget_bytes(self):
        """Exact RAM ceiling for the fragment ring in bytes.

        The compression level's cap governs; the bitrate-derived size
        only tightens it for short buffers that need less.
        """
        prof_cap = self._active_profile.get("ram_cap_mb", 80) * 1024 * 1024
        cap = self._live_cap_bps or 40_000_000
        buf_sec = max(1, int(self.settings.get("buffer_seconds", 20)))
        derived = cap / 8.0 * buf_sec * _FRAG_HEADROOM
        return int(min(prof_cap, derived, 320 * 1024 * 1024))

    def _frag_maxlen(self):
        """Block-count backstop (byte budget in _push_frag is authoritative)."""
        return max(_FRAG_BLOCK_MIN, int(self._frag_budget_bytes() / 8192) + 1)

    def _push_frag(self, chunk):
        """Append a block; evict oldest while over the byte budget."""
        self._frag_deque.append(chunk)
        self._frag_bytes += len(chunk)
        budget = self._frag_budget
        while self._frag_bytes > budget and len(self._frag_deque) > 1:
            self._frag_bytes -= len(self._frag_deque.popleft())

    def frag_ceiling_mb(self):
        """Current RAM ceiling of the fragment ring in MB (diagnostic)."""
        return self._frag_budget / 1024 / 1024

    def frag_fill_frac(self):
        """0..1 ring fullness by bytes (for UI)."""
        try:
            return min(1.0, self._frag_bytes / max(1, self._frag_budget))
        except (AttributeError, TypeError):
            return 0.0

    def _resize_frame(self, frame):
        """Resize frame to the configured resolution."""
        res = self.settings.get("resolution", "1920x1080")
        try:
            tw, th = map(int, res.split("x"))
        except (ValueError, AttributeError):
            tw, th = 1920, 1080
        h, w = frame.shape[:2]
        if w == tw and h == th:
            return frame
        return cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)

    def _reset_buffer_size(self):
        """Apply buffer_seconds live: resize audio rings, wall ring and
        fragment byte budget. Encoder fps/resolution/compression/monitor
        params still require a restart (noted in the UI).

        No-op when sizes already match (called on every settings save,
        including per-tick slider drags). Capture threads re-resolve the
        live deque each iteration, so recreations can never orphan them.
        """
        buf_sec = max(1, int(self.settings.get("buffer_seconds", 20)))
        fps = int(self.settings.get("fps", 60))
        max_audio = max(1, buf_sec * 10)
        if self.mic_audio_replay.maxlen != max_audio:
            self.mic_audio_replay = collections.deque(
                self.mic_audio_replay, maxlen=max_audio)
        if self.sys_audio_replay.maxlen != max_audio:
            self.sys_audio_replay = collections.deque(
                self.sys_audio_replay, maxlen=max_audio)
        wall_cap = fps * buf_sec + 600
        with self._frag_lock:
            self._frag_budget = self._frag_budget_bytes()
            if self._feed_walls.maxlen != wall_cap:
                self._feed_walls = collections.deque(
                    self._feed_walls, maxlen=wall_cap)
            while (self._frag_bytes > self._frag_budget
                   and len(self._frag_deque) > 1):
                self._frag_bytes -= len(self._frag_deque.popleft())

    def _log_audio_error(self, msg):
        ts = time.strftime("%H:%M:%S")
        entry = f"[{ts}] {msg}"
        self.audio_errors.append(entry)
        self.last_audio_error = entry

    @staticmethod
    def _boost_audio_thread():
        """Raise the calling (audio) thread above normal priority.

        Under heavy game load the OS can starve capture threads, causing
        PortAudio overflows = periodic gaps in captured audio. Capture
        threads do ~1 ms of work per 100 ms chunk, so this cannot
        meaningfully steal CPU from the game.
        """
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            # THREAD_PRIORITY_ABOVE_NORMAL = 1
            kernel32.SetThreadPriority(kernel32.GetCurrentThread(), 1)
        except (AttributeError, OSError):
            pass

    @staticmethod
    def _buffer_has_audio(buf):
        """Check if audio buffer contains real (non-silent) data."""
        if not buf:
            return False
        for entry in buf:
            arr = np.frombuffer(entry[0], dtype=np.int16)
            if np.any(arr != 0):
                return True
        return False

    # --------------------------------------------------------
    # LIVE STREAM (continuous GPU encode -> stdout -> ring)
    # --------------------------------------------------------

    def _live_cmd(self, fps):
        gop = max(10, fps // 4)  # ~0.25 s fragments: tight cut granularity
        cmd = [self._ffmpeg_path(), "-y"]
        cmd += self._live_input_flags()
        cmd += ["-framerate", str(fps), "-i", "pipe:0",
                "-c:v", self._live_encoder]
        cmd += self._live_args
        # Belt and suspenders for cut granularity: periodic GOP plus
        # wall-clock forced keyframes (encoder GOP alone proved
        # unreliable on near-static duplicated input).
        cmd += ["-g", str(gop),
                "-force_key_frames", "expr:gte(t,n_forced*0.25)",
                "-pix_fmt", "yuv420p",
                "-f", "mp4",
                "-movflags", "frag_keyframe+empty_moov+default_base_moof",
                "pipe:1"]
        return cmd

    def _launch_stream(self, fps):
        """Spawn the live encoder. Returns (proc, wall_start)."""
        wall_start = time.monotonic()
        proc = subprocess.Popen(
            self._live_cmd(fps),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, bufsize=0,
            creationflags=_NO_WINDOW)
        return proc, wall_start

    def _teardown_proc(self, proc):
        """Close pipes + reap a stream process without leaking handles."""
        if proc is None:
            return
        for pipe in (getattr(proc, "stdin", None),
                     getattr(proc, "stdout", None),
                     getattr(proc, "stderr", None)):
            try:
                if pipe is not None:
                    pipe.close()
            except (OSError, ValueError):
                pass
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass

    def _capture_loop(self, fps, generation):
        """Encode the freshest screen frame to JPEG as fast as possible.

        Stores it as _latest_jpeg; the writer thread emits wall-paced
        slots from it (duplicating under load for exact CFR output).
        Identical frames skip the encode entirely (byte-identical output
        for zero CPU); a forced refresh caps staleness on slow fades.
        """
        target_interval = 1.0 / max(1, fps)
        idle_interval = min(1.0 / 15.0, target_interval * 4)
        poll_interval = target_interval
        last_thumb = 0.0
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 95]
        thumb_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
        prev_small = None
        skip_streak = 0
        next_tick = time.monotonic()

        while self.recording and generation == self._stream_generation:
            try:
                frame = self.camera.get_latest_frame() if self.camera else None
            except Exception:
                frame = None
                time.sleep(0.05)
            if frame is not None:
                resized = self._resize_frame(frame)
                # Cheap change check on a 64x36 stamp (~0.3 ms) vs a full
                # q95 encode (~15 ms): static content skips the encode.
                small = cv2.resize(resized, (64, 36),
                                   interpolation=cv2.INTER_NEAREST)
                identical = (prev_small is not None and skip_streak < 30
                             and float(cv2.absdiff(small, prev_small).mean()) < 1.0)
                prev_small = small
                del small
                if identical:
                    skip_streak += 1
                    del resized, frame
                    # Static scene: back off polling (detection lag stays
                    # under ~66 ms; full rate resumes on any change)
                    poll_interval = min(poll_interval * 1.5, idle_interval)
                else:
                    skip_streak = 0
                    poll_interval = target_interval
                    _, jpeg_bytes = cv2.imencode(".jpg", resized, encode_params)
                    with self._latest_lock:
                        self._latest_jpeg = jpeg_bytes.tobytes()
                    del jpeg_bytes
                    # Preview thumbnail throttled to ~7fps, decoded at half
                    # res (plenty for a 480px preview, ~half the decode cost)
                    t = time.monotonic()
                    if t - last_thumb >= 0.15:
                        last_thumb = t
                        try:
                            with self._latest_lock:
                                snap = self._latest_jpeg
                            thumb_src = cv2.imdecode(
                                np.frombuffer(snap, np.uint8),
                                cv2.IMREAD_REDUCED_COLOR_2)
                            if thumb_src is not None:
                                h, w = thumb_src.shape[:2]
                                thumb_w = 480
                                thumb_h = max(1, int(h * thumb_w / w))
                                thumb = cv2.resize(
                                    thumb_src, (thumb_w, thumb_h),
                                    interpolation=cv2.INTER_LINEAR)
                                _, thumb_jpeg = cv2.imencode(".jpg", thumb, thumb_params)
                                with self.preview_lock:
                                    self.preview_jpeg = thumb_jpeg.tobytes()
                                del thumb, thumb_jpeg, thumb_src
                        except (cv2.error, ValueError):
                            pass
                        del snap
                    del resized, frame

            # Pace capture; reset on overrun to avoid catch-up spiral
            next_tick += poll_interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def _write_loop(self, fps, generation):
        """Emit exactly one frame per 1/fps wall slot to ffmpeg stdin.

        Repeats the latest JPEG when encoding lags, so the stream holds
        a true 60.0 (or target) fps wall rate: video duration always
        equals wall time and audio can never overhang the video.
        """
        target_interval = 1.0 / max(1, fps)
        next_tick = time.monotonic()
        proc = self._ffmpeg_proc
        while self.recording and generation == self._stream_generation:
            if proc is None or proc.poll() is not None:
                break
            with self._latest_lock:
                jpeg = self._latest_jpeg
            if jpeg is not None:
                try:
                    proc.stdin.write(jpeg)
                except (BrokenPipeError, OSError, ValueError):
                    break
                t_wall = time.monotonic()
                self._last_feed_wall = t_wall
                with self._frag_lock:
                    self._feed_walls.append(t_wall)
                    self._feed_total += 1
                del jpeg
            next_tick += target_interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                next_tick = time.monotonic()  # cap catch-up burst

    def _read_loop(self, proc, generation):
        """Read fMP4 blocks from stdout into the rolling ring."""
        try:
            while self.recording and generation == self._stream_generation:
                try:
                    chunk = proc.stdout.read(_FRAG_BLOCK)
                except (OSError, ValueError):
                    break
                if not chunk:
                    break  # EOF: encoder exited
                if self._init_seg is None:
                    self._init_pending.extend(chunk)
                    if len(self._init_pending) > _INIT_PENDING_MAX:
                        self.last_stream_error = "init segment too large"
                        break
                    end = _find_init_end(self._init_pending)
                    if end is not None:
                        with self._frag_lock:
                            self._init_seg = bytes(self._init_pending[:end])
                            rest = bytes(self._init_pending[end:])
                            if rest:
                                self._push_frag(rest)
                        del self._init_pending
                        self._init_pending = bytearray()
                else:
                    with self._frag_lock:
                        self._push_frag(chunk)
                del chunk
        finally:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            if (self.recording and generation == self._stream_generation
                    and self._stream_restarts < _RESTART_MAX):
                self._restart_stream("encoder output ended")

    def _restart_stream(self, reason):
        """Relaunch a dead encoder (bounded); old timeline is discarded."""
        if not self.recording:
            return
        if self._stream_restarts >= _RESTART_MAX:
            self.last_stream_error = "encoder failed: %s" % reason
            return
        time.sleep(_RESTART_COOLDOWN)
        if not self.recording:
            return
        self._stream_generation += 1
        generation = self._stream_generation
        self._stream_restarts += 1
        # Prune dead stream threads (no unbounded growth)
        for attr in ("_cap_thread", "_write_thread", "_read_thread"):
            thread = getattr(self, attr, None)
            if thread is not None and not thread.is_alive():
                setattr(self, attr, None)
        try:
            fps = int(self.settings.get("fps", 60))
            proc, wall_start = self._launch_stream(fps)
        except OSError as exc:
            self.last_stream_error = "encoder relaunch failed: %s" % exc
            return
        with self._frag_lock:
            self._frag_deque.clear()
            self._frag_bytes = 0
            self._frag_budget = self._frag_budget_bytes()
            self._init_seg = None
            self._init_pending = bytearray()
            self._stream_wall_start = wall_start
            self._last_feed_wall = 0.0
            self._feed_walls.clear()
            self._feed_total = 0
            self._ffmpeg_proc = proc
        with self._latest_lock:
            self._latest_jpeg = None
        cap = threading.Thread(
            target=self._capture_loop, args=(fps, generation), daemon=True)
        writer = threading.Thread(
            target=self._write_loop, args=(fps, generation), daemon=True)
        read = threading.Thread(
            target=self._read_loop, args=(proc, generation), daemon=True)
        self._cap_thread = cap
        self._write_thread = writer
        self._read_thread = read
        cap.start()
        writer.start()
        read.start()

    # --------------------------------------------------------
    # SAVE CLIP (flush ring -> cache -> instant copy remux)
    # --------------------------------------------------------

    def _snapshot_usable(self):
        """Join ring blocks; wait for in-flight tail; resync to boxes.

        Returns (usable_bytes, last_moof_base_time_or_None).
        """
        def _snap():
            with self._frag_lock:
                return b"".join(list(self._frag_deque))

        def _parse(data):
            boxes, consumed = _scan_fragments(data)
            if not boxes:
                return None, 0, 0, None
            last_base = None
            for typ, off, size in boxes:
                if typ == b"moof":
                    _, base = _moof_info(data, off, size, {})
                    if base is not None:
                        last_base = base
            first_off = boxes[0][1]
            return data[first_off:consumed], consumed - first_off, consumed, last_base

        data = _snap()
        if not data:
            return None, None
        frag, _, consumed, last_base = _parse(data)
        if frag is None:
            return None, None
        # Wait for the trailing partial fragment to complete (newest ~0.5 s)
        deadline = time.monotonic() + 1.5
        while consumed < len(data) and time.monotonic() < deadline:
            time.sleep(0.05)
            grown = _snap()
            if len(grown) <= len(data):
                break
            data = grown
            del grown
            frag, _, consumed, last_base = _parse(data)
            if frag is None:
                return None, None
        return frag, last_base

    def save_clip(self, output_path):
        """Export the current replay buffer to a clip file.

        Flushes the fragment ring to a cache file (starting at the first
        keyframe-led fragment), mixes audio onto the matching wall-clock
        window, then remuxes with `-c:v copy` — no re-encode, save takes
        seconds regardless of clip length.
        """
        self.last_save_error = None
        self.last_save_info = None
        fps = int(self.settings.get("fps", 60))
        with self._frag_lock:
            init_seg = self._init_seg
            wall_start = self._stream_wall_start
            last_feed = self._last_feed_wall
            feed_walls = list(self._feed_walls)
            feed_total = self._feed_total
            have_data = len(self._frag_deque) > 0
        if init_seg is None or not have_data:
            self.last_save_error = "buffering (no stream data yet)"
            return False

        frag, last_base = self._snapshot_usable()
        if not frag:
            self.last_save_error = "buffering (no complete fragment yet)"
            return False

        trex = _trex_defaults(init_seg)

        timescale = _video_timescale(init_seg) or 15360
        buf_sec = float(int(self.settings.get("buffer_seconds", 20)))

        def _wall_of(base):
            """Map stream baseMediaDecodeTime to feed wall time."""
            if base is None or not feed_walls:
                return None, None
            idx = int(round(base * fps / float(timescale)))
            base_idx = feed_total - len(feed_walls)
            return feed_walls[min(max(idx - base_idx, 0),
                                  len(feed_walls) - 1)], idx

        # Clip window: trailing buf_sec of wall time. Start at the first
        # keyframe-led fragment at/after (end - buf_sec) so the clip
        # covers exactly the replay window.
        _end_wall, _end_idx = _wall_of(last_base)
        end_wall = _end_wall if _end_wall is not None else last_feed
        end_idx = _end_idx
        t_ideal = end_wall - buf_sec
        cut_wall = None
        cut_off = None
        cut_idx = None
        boxes, _ = _scan_fragments(frag)
        first_sync = None
        for typ, off, size in boxes:
            if typ != b"moof":
                continue
            is_sync, base = _moof_info(frag, off, size, trex)
            if not is_sync:
                continue
            wall, idx = _wall_of(base)
            if first_sync is None:
                first_sync = (off, wall, idx)
            if wall is not None and wall >= t_ideal:
                cut_off, cut_wall, cut_idx = off, wall, idx
                break
        if cut_off is None:
            if first_sync is not None:
                cut_off, cut_wall, cut_idx = first_sync
            else:
                self.last_save_error = "no decodable fragment"
                del frag
                return False
            if cut_wall is None:
                cut_wall = wall_start  # fallback: stream start
        if SYNC_DEBUG is not None:
            SYNC_DEBUG.update(
                timescale=timescale, fps=fps,
                feed_total=feed_total, n_walls=len(feed_walls),
                end_idx=end_idx, cut_idx=cut_idx,
                cut_wall=cut_wall, end_wall=end_wall,
                wall_start=wall_start, last_feed=last_feed,
                duration=end_wall - cut_wall)

        duration = end_wall - cut_wall
        if duration < 0.5:
            self.last_save_error = "clip too short"
            del frag
            return False
        duration = min(duration, buf_sec + 2.0)
        # Insurance: audio must never exceed the actual video frame count.
        # (Guards any feed underrun; normally a no-op.) Tolerance is
        # two frame intervals + 50 ms so genuine shortfalls still trim.
        n_vid = _count_video_samples(frag, boxes, cut_off)
        if n_vid > 0:
            vid_dur = n_vid / float(fps)
            if vid_dur < duration - (2.0 / fps + 0.05):
                duration = max(0.5, vid_dur)

        media = init_seg + frag[cut_off:]
        del frag

        with self.lock:
            mic_chunks = list(self.mic_audio_replay)
            sys_chunks = list(self.sys_audio_replay)

        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        temp_files = []
        try:
            # Cache file: init segment + keyframe-aligned media
            cache_path = tempfile.mktemp(suffix=".mp4")
            temp_files.append(cache_path)
            with open(cache_path, "wb") as f:
                f.write(media)
            del media

            # Render mic + desktop to SEPARATE wall-clock WAVs (fixed
            # 48000 Hz stereo, exact duration) for the remux filtergraph.
            # Desktop is input 1 -> [a:0], mic is input 2 -> [a:1].
            # Desktop = master loopback and/or per-app stems, each already
            # gain-scaled at capture; summed wall-aligned here (never
            # subtracted). Freshness gate: a stream whose newest chunk
            # predates the clip window (capture died long ago) is dropped
            # instead of rendering a silent track.
            has_mic = mic_chunks and self._buffer_has_audio(mic_chunks)
            has_sys = sys_chunks and self._buffer_has_audio(sys_chunks)
            if has_mic and not Recorder._stream_is_fresh(mic_chunks, cut_wall):
                has_mic = False
                self.last_save_info = "mic audio stale, skipped"
            if has_sys and not Recorder._stream_is_fresh(sys_chunks, cut_wall):
                has_sys = False
                self.last_save_info = "system audio stale, skipped"
            with self.lock:
                app_bufs = [list(dq) for dq in self.app_audio_replay.values()
                            if dq]
            desktop_parts = []
            if has_sys:
                desktop_parts.append(sys_chunks)
            for abuf in app_bufs:
                if (abuf and self._buffer_has_audio(abuf)
                        and Recorder._stream_is_fresh(abuf, cut_wall)):
                    desktop_parts.append(abuf)
            mic_wav = sys_wav = None
            if desktop_parts:
                sys_wav = tempfile.mktemp(suffix=".wav")
                temp_files.append(sys_wav)
                if not self._render_desktop_wav(
                        desktop_parts, sys_wav, cut_wall, duration):
                    sys_wav = None
                    temp_files.remove(sys_wav)
            if has_mic:
                mic_wav = tempfile.mktemp(suffix=".wav")
                temp_files.append(mic_wav)
                if not self._render_stream_wav(
                        mic_chunks, mic_wav, cut_wall, duration):
                    mic_wav = None
                    temp_files.remove(mic_wav)
            del mic_chunks, sys_chunks, app_bufs, desktop_parts

            # Instant remux: isolated per-clock resample per input, amix
            # blend, stream-copy video. (-thread_queue_size mirrors the
            # live input flags. -ar/-ac are enforced as OUTPUT options:
            # this ffmpeg rejects them on wav inputs, and our WAVs are
            # rendered at exactly 48000/stereo anyway. -async 1
            # start-corrects only. No -af: it cannot coexist with
            # -filter_complex.)
            cmd = [self._ffmpeg_path(), "-y", "-i", cache_path]
            if sys_wav:
                cmd.extend(["-thread_queue_size", "1024", "-i", sys_wav])
            if mic_wav:
                cmd.extend(["-thread_queue_size", "1024", "-i", mic_wav])
            if sys_wav and mic_wav:
                # NOTE: [a:0]/[a:1] pad syntax is rejected by this ffmpeg
                # ("matches no streams"); [1:a]/[2:a] address the identical
                # streams (sys=first audio input, mic=second). Chain and
                # options otherwise as specified, plus normalize=0: amix
                # defaults to attenuating the sum (measured -6 dB on a
                # reference tone); normalize=0 preserves the previous
                # straight-sum loudness.
                cmd.extend(["-map", "0:v",
                            "-filter_complex",
                            "[1:a]aresample=48000:async=1[desktop_clean];"
                            "[2:a]aresample=48000:async=1[mic_clean];"
                            "[desktop_clean][mic_clean]"
                            "amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
                            "-map", "[aout]",
                            "-c:a", "aac", "-b:a", "192k",
                            "-ar", "48000", "-ac", "2"])
            elif sys_wav or mic_wav:
                cmd.extend(["-map", "0:v",
                            "-filter_complex",
                            "[1:a]aresample=48000:async=1[aout]",
                            "-map", "[aout]",
                            "-c:a", "aac", "-b:a", "192k",
                            "-ar", "48000", "-ac", "2"])
            else:
                cmd.extend(["-map", "0:v"])
            cmd.extend([
                "-c:v", "copy",
                "-async", "1",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                "-t", "%.3f" % duration,
                output_path])

            completed = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=120, creationflags=_NO_WINDOW)
            if completed.returncode != 0:
                self.last_save_error = "ffmpeg error: %s" % completed.stderr.decode(
                    "utf-8", errors="replace")[-500:]
                return False
            if not (os.path.isfile(output_path)
                    and os.path.getsize(output_path) > 0):
                self.last_save_error = "empty output"
                return False
            return True
        except (OSError, RuntimeError) as exc:
            self.last_save_error = str(exc)[:300]
            return False
        finally:
            for path in temp_files:
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    # --------------------------------------------------------
    # ENABLE TOGGLES
    # --------------------------------------------------------

    def set_mic_enabled(self, enabled):
        self.mic_enabled = bool(enabled)
        self.settings["record_microphone"] = self.mic_enabled

    def set_system_enabled(self, enabled):
        self.system_enabled = bool(enabled)
        self.settings["record_system_audio"] = self.system_enabled

    @staticmethod
    def _remembered_last(saved_last, current):
        try:
            saved_last = int(saved_last)
            if 1 <= saved_last <= 100:
                return saved_last
        except (TypeError, ValueError):
            pass
        try:
            current = int(current)
            if 1 <= current <= 100:
                return current
        except (TypeError, ValueError):
            pass
        return 100

    def toggle_mic_volume(self):
        """Mute mic to 0, or restore the last nonzero level. Returns vol."""
        if self.mic_volume > 0:
            self.last_mic_volume = self.mic_volume
            self.mic_volume = 0
        else:
            self.mic_volume = self.last_mic_volume or 100
        self.settings["mic_volume"] = self.mic_volume
        self.settings["last_mic_volume"] = self.last_mic_volume
        return self.mic_volume

    def toggle_system_volume(self):
        """Mute system to 0, or restore the last nonzero level. Returns vol."""
        if self.system_volume > 0:
            self.last_system_volume = self.system_volume
            self.system_volume = 0
        else:
            self.system_volume = self.last_system_volume or 100
        self.settings["system_volume"] = self.system_volume
        self.settings["last_system_volume"] = self.last_system_volume
        return self.system_volume

    def set_mic_volume(self, vol):
        self.mic_volume = max(0, min(100, int(vol)))
        self.settings["mic_volume"] = self.mic_volume

    def set_system_volume(self, vol):
        self.system_volume = max(0, min(100, int(vol)))
        self.settings["system_volume"] = self.system_volume

    def _available_outputs(self):
        """Number of dxcam outputs, or None when the query fails."""
        try:
            info = dxcam.output_info()
        except Exception:
            return None
        try:
            lines = [ln for ln in str(info).splitlines() if ln.strip()]
        except Exception:
            return None
        return len(lines) if lines else None

    def _clamp_monitor_index(self):
        """Clamp monitor_index into the live dxcam range.

        A saved index from a docked/multi-monitor setup goes stale on
        relaunch with fewer displays; dxcam.create would then raise and
        the app would boot with recording off. Fall back to 0 and keep
        settings in sync so the next save is honest.
        """
        try:
            idx = max(0, int(self.settings.get("monitor_index",
                                               self.monitor_index)))
        except (ValueError, TypeError):
            idx = 0
        count = self._available_outputs()
        if count is not None and idx >= count:
            idx = 0
        self.monitor_index = idx
        try:
            self.settings["monitor_index"] = idx
        except (TypeError, AttributeError):
            pass
        return idx

    # --------------------------------------------------------
    # START / STOP REPLAY BUFFER
    # --------------------------------------------------------

    def start(self):
        if self.recording:
            return

        self._clamp_monitor_index()
        fps = int(self.settings.get("fps", 60))
        # Smoke-test encoder flags (re-validated when fps changes, since
        # bitrate caps scale with fps)
        if (self._live_encoder is None or self._live_fps != fps
                or self._active_profile != self._compression_profile()):
            self._live_encoder = None
            self._active_profile = self._compression_profile()
            self._ensure_live_args(fps)
            self._live_fps = fps

        self.recording = True
        self.last_stream_error = None
        self.last_save_error = None
        with self._frag_lock:
            self._frag_deque = collections.deque()
            self._frag_bytes = 0
            self._frag_budget = self._frag_budget_bytes()
            self._init_seg = None
            self._init_pending = bytearray()
        self.mic_audio_replay.clear()
        self.sys_audio_replay.clear()
        self._app_captures = {}
        self._app_threads = []
        self._app_retry = {}
        self.app_audio_replay = {}
        self._master_thread = None
        self._loopback_dev = None
        self._recording_start = time.monotonic()
        self._mic_start_time = 0.0
        self._sys_start_time = 0.0
        self._stream_generation += 1
        generation = self._stream_generation
        self._stream_restarts = 0
        buf_sec = max(1, int(self.settings.get("buffer_seconds", 20)))
        wall_cap = fps * buf_sec + 600  # frame wall ring covers the window
        with self._frag_lock:
            self._feed_walls = collections.deque(maxlen=wall_cap)
            self._feed_total = 0

        try:
            # Small dxcam buffer: we consume every slot, so a deep queue
            # only wastes RAM (~6 MB/frame at 1080p, 64 deep = ~400 MB).
            # One retry on output 0: a stale saved index must never leave
            # the app booted with recording off.
            try:
                self.camera = dxcam.create(output_idx=self.monitor_index, output_color="BGR",
                                           max_buffer_len=8)
            except Exception:
                if self.monitor_index != 0:
                    self.monitor_index = 0
                    try:
                        self.settings["monitor_index"] = 0
                    except (TypeError, AttributeError):
                        pass
                    self.camera = dxcam.create(output_idx=0, output_color="BGR",
                                               max_buffer_len=8)
                else:
                    raise
            self.camera.start(target_fps=fps)
        except Exception as exc:
            self.recording = False
            self.camera = None
            raise RuntimeError(f"Could not start screen capture: {exc}") from exc

        try:
            proc, wall_start = self._launch_stream(fps)
        except OSError as exc:
            self.recording = False
            if self.camera:
                self.camera.stop()
                self.camera = None
            raise RuntimeError(f"Could not start video encoder: {exc}") from exc
        with self._frag_lock:
            self._stream_wall_start = wall_start
            self._last_feed_wall = 0.0
            self._ffmpeg_proc = proc

        self._cap_thread = threading.Thread(
            target=self._capture_loop, args=(fps, generation), daemon=True)
        self._write_thread = threading.Thread(
            target=self._write_loop, args=(fps, generation), daemon=True)
        self._read_thread = threading.Thread(
            target=self._read_loop, args=(proc, generation), daemon=True)
        self._cap_thread.start()
        self._write_thread.start()
        self._read_thread.start()
        self._start_audio_capture()

    def _restart_camera(self):
        """Recreate the screen capture on the current monitor_index.

        The encoder keeps running (writer repeats the last JPEG across
        the gap), so the stream and buffer survive a monitor switch.
        """
        cam = self.camera
        self.camera = None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass
        fps = int(self.settings.get("fps", 60))
        try:
            self.camera = dxcam.create(output_idx=self.monitor_index,
                                       output_color="BGR", max_buffer_len=8)
        except Exception:
            if self.monitor_index != 0:
                self.monitor_index = 0
                try:
                    self.settings["monitor_index"] = 0
                except (TypeError, AttributeError):
                    pass
                self.camera = dxcam.create(output_idx=0,
                                           output_color="BGR",
                                           max_buffer_len=8)
            else:
                raise
        self.camera.start(target_fps=fps)

    def stop(self):
        if not self.recording:
            return

        self.recording = False
        proc = self._ffmpeg_proc
        # Closing stdin lets ffmpeg finalize the stream and exit cleanly
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        for thread in (self._cap_thread, self._write_thread,
                       self._read_thread):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
        self._cap_thread = None
        self._write_thread = None
        self._read_thread = None
        self._teardown_proc(proc)
        self._ffmpeg_proc = None

        self._stop_audio_capture()
        self._stop_app_captures()
        if self.camera:
            try:
                self.camera.stop()
            except Exception:
                pass
            self.camera = None

    # --------------------------------------------------------
    # AUDIO CAPTURE (pyaudiowpatch — pure WASAPI)
    # --------------------------------------------------------

    def _poll_read(self, stream, nframes, bytes_per_frame, generation):
        """Read exactly nframes without ever blocking indefinitely.

        Polls available frames and reads only what is ready, assembling
        partial reads. Returns (status, data) with status 'ok', 'stop'
        (generation/recording changed: exit quietly) or 'error'
        (transient failure: caller logs, counts a drop, continues).
        Because no call blocks, stop/restart can never hang or segfault
        on PortAudio teardown races.
        """
        pieces = []
        got = 0
        while got < nframes:
            if not self.recording or generation != self._audio_generation:
                return "stop", b""
            try:
                avail = int(stream.get_read_available())
            except Exception:
                return "error", b""
            if avail <= 0:
                time.sleep(0.02)
                continue
            try:
                data = stream.read(min(avail, nframes - got),
                                   exception_on_overflow=False)
            except Exception:
                return "error", b""
            if not data:
                time.sleep(0.01)
                continue
            pieces.append(data)
            got += len(data) // bytes_per_frame
        return "ok", b"".join(pieces)

    def _capture_wasper_loopback(self, paudio, loopback_dev, audio_replay, generation):
        """Capture system audio via WASAPI loopback."""
        self._boost_audio_thread()
        sr = int(loopback_dev["defaultSampleRate"])
        ch = min(int(loopback_dev.get("maxInputChannels", 2)), 2)
        chunk = _CHUNK_FRAMES
        try:
            stream = paudio.open(
                format=p.paInt16, channels=ch, rate=sr, input=True,
                input_device_index=loopback_dev["index"],
                # 2x device buffer absorbs scheduling jitter under load
                frames_per_buffer=chunk * 2,
            )
        except Exception as exc:
            self._log_audio_error("loopback stream open failed: %s" % exc)
            return

        try:
            while self.recording and generation == self._audio_generation:
                if self._any_app_landed():
                    break  # per-app stems flowing: avoid double capture
                # Re-resolve the live ring: settings saves may recreate it.
                audio_replay = self.sys_audio_replay
                status, data = self._poll_read(
                    stream, chunk, 2 * ch, generation)
                if status == "stop":
                    break  # stopping/restarting: exit quietly
                if status == "error" or not data:
                    self._log_audio_error("loopback read error")
                    self.audio_drops["sys"] += 1
                    time.sleep(0.05)
                    continue
                pcm = np.frombuffer(data, dtype=np.int16)
                if pcm.size == 0:
                    self.audio_drops["sys"] += 1
                    continue
                if pcm.size < chunk * ch:
                    # Short read after overflow: data was lost; count it.
                    self.audio_drops["sys"] += 1
                vol = self.system_volume / 100.0
                if vol < 1.0:
                    pcm = (pcm.astype(np.float64) * vol).astype(np.int16)
                # Timestamp = start of this chunk (arrival minus chunk duration)
                arrival = time.monotonic()
                t_start = arrival - (pcm.size // max(1, ch)) / float(sr)
                if self._sys_start_time == 0.0:
                    self._sys_start_time = arrival
                audio_replay.append((pcm.tobytes(), sr, ch, t_start))
                level = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
                self.system_audio_buffer.append(level)
                del pcm, data
        except Exception as exc:
            self._log_audio_error("loopback capture error: %s" % exc)
        finally:
            # Same-thread teardown only (cross-thread close segfaults).
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    def _capture_wasper_mic(self, paudio, mic_dev, audio_replay, generation):
        """Capture microphone audio via WASAPI."""
        self._boost_audio_thread()
        sr = int(mic_dev["defaultSampleRate"])
        ch = min(int(mic_dev.get("maxInputChannels", 2)), 2)
        chunk = _CHUNK_FRAMES
        try:
            stream = paudio.open(
                format=p.paInt16, channels=ch, rate=sr, input=True,
                input_device_index=mic_dev["index"],
                # 2x device buffer absorbs scheduling jitter under load
                frames_per_buffer=chunk * 2,
            )
        except Exception as exc:
            self._log_audio_error("mic stream open failed: %s" % exc)
            return

        try:
            while self.recording and generation == self._audio_generation:
                # Re-resolve the live ring: settings saves may recreate it.
                audio_replay = self.mic_audio_replay
                status, data = self._poll_read(
                    stream, chunk, 2 * ch, generation)
                if status == "stop":
                    break  # stopping/restarting: exit quietly
                if status == "error" or not data:
                    self._log_audio_error("mic read error")
                    self.audio_drops["mic"] += 1
                    time.sleep(0.05)
                    continue
                pcm = np.frombuffer(data, dtype=np.int16)
                if pcm.size == 0:
                    self.audio_drops["mic"] += 1
                    continue
                vol = self.mic_volume / 100.0
                if vol < 1.0:
                    pcm = (pcm.astype(np.float64) * vol).astype(np.int16)
                # Timestamp = start of this chunk (arrival minus chunk duration)
                arrival = time.monotonic()
                t_start = arrival - (pcm.size // max(1, ch)) / float(sr)
                if self._mic_start_time == 0.0:
                    self._mic_start_time = arrival
                audio_replay.append((pcm.tobytes(), sr, ch, t_start))
                level = float(np.sqrt(np.mean(pcm.astype(np.float64) ** 2)))
                self.mic_audio_buffer.append(level)
                del pcm, data
        except Exception as exc:
            self._log_audio_error("mic capture error: %s" % exc)
        finally:
            # Same-thread teardown only (cross-thread close segfaults).
            try:
                stream.stop_stream()
                stream.close()
            except Exception:
                pass

    def _find_wasapi_devices(self):
        """Use pyaudiowpatch to find the WASAPI mic and loopback devices."""
        paudio = p.PyAudio()
        try:
            wasapi = paudio.get_host_api_info_by_type(p.paWASAPI)
        except Exception as exc:
            self._log_audio_error("WASAPI not available: %s" % exc)
            paudio.terminate()
            return None, None, None

        loopback_dev = None
        default_out = paudio.get_device_info_by_index(wasapi["defaultOutputDevice"])
        if default_out.get("isLoopbackDevice"):
            loopback_dev = default_out
        else:
            for lb in paudio.get_loopback_device_info_generator():
                if self.system_device:
                    if self.system_device in lb["name"]:
                        loopback_dev = lb
                        break
                else:
                    if default_out["name"] in lb["name"]:
                        loopback_dev = lb
                        break
            if loopback_dev is None and self.system_device:
                # Stale device name (e.g. installed on another PC):
                # fall back to the default output instead of silence.
                for lb in paudio.get_loopback_device_info_generator():
                    if default_out["name"] in lb["name"]:
                        loopback_dev = lb
                        break
                self._log_audio_error(
                    "System audio device '%s' not found, using default output"
                    % self.system_device)

        mic_dev = None
        mic_fallback = None
        for i in range(paudio.get_device_count()):
            d = paudio.get_device_info_by_index(i)
            h = paudio.get_host_api_info_by_index(d["hostApi"])["name"]
            if "WASAPI" not in h:
                continue
            if d.get("maxInputChannels", 0) <= 0:
                continue
            if d.get("isLoopbackDevice"):
                continue
            if self.mic_device:
                if mic_fallback is None:
                    mic_fallback = d
                if self.mic_device in d["name"]:
                    mic_dev = d
                    break
            else:
                mic_dev = d
                break
        if mic_dev is None and mic_fallback is not None:
            # Stale mic name (e.g. installed on another PC): use the
            # first available input instead of recording no mic.
            mic_dev = mic_fallback
            self._log_audio_error(
                "Microphone '%s' not found, using '%s'"
                % (self.mic_device, mic_dev["name"]))

        return paudio, loopback_dev, mic_dev

    def _start_audio_capture(self):
        if not self.recording:
            return
        self._audio_generation += 1
        generation = self._audio_generation

        paudio, loopback_dev, mic_dev = self._find_wasapi_devices()
        if paudio is None:
            return
        self._loopback_dev = loopback_dev

        if self.mic_enabled and mic_dev is not None:
            thread = threading.Thread(
                target=self._capture_wasper_mic,
                args=(paudio, mic_dev, self.mic_audio_replay, generation),
                daemon=True)
            thread.start()
            self._audio_threads.append(thread)

        self._paudio = paudio
        self._spawn_master_loopback()

    def _spawn_master_loopback(self):
        """(Re)start the full-mix loopback thread if none is alive."""
        if not self.recording or not self.system_enabled:
            return False
        paudio = getattr(self, "_paudio", None)
        loopback_dev = getattr(self, "_loopback_dev", None)
        if paudio is None or loopback_dev is None:
            return False
        mt = getattr(self, "_master_thread", None)
        if mt is not None and mt.is_alive():
            return True
        self._audio_threads = [t for t in self._audio_threads
                               if t.is_alive()]
        thread = threading.Thread(
            target=self._capture_wasper_loopback,
            args=(paudio, loopback_dev, self.sys_audio_replay,
                  self._audio_generation),
            daemon=True)
        thread.start()
        self._master_thread = thread
        self._audio_threads.append(thread)
        return True

    def _any_app_landed(self):
        """True once any per-app stem is flowing (master must stand down)."""
        try:
            for info in list(self._app_captures.values()):
                if info.get("landed"):
                    return True
        except AttributeError:
            pass
        return False

    @staticmethod
    def _pid_alive(pid):
        try:
            if _psutil is not None:
                return _psutil.pid_exists(int(pid))
        except (ValueError, TypeError, AttributeError):
            pass
        return True

    def sync_app_captures(self, entries):
        """Reconcile per-app capture threads with live sounding sessions.

        entries: {exe: (pid, volume)} mapping (preferred), or a legacy
        [(exe, pid)] list. Volumes feed the engine gain table so one
        call carries PIDs and levels together. Cheap when idle; safe
        from any thread. Master loopback runs only while zero app
        captures exist (graceful fallback); the first flowing stem
        stands it down.
        """
        if not self.recording:
            self._stop_app_captures()
            return
        items = entries.items() if isinstance(entries, dict) else (entries or [])
        want = {}
        for item in items:
            if isinstance(entries, dict):
                exe, val = item
                parts = (list(val) + [None, None, None])[:3] if isinstance(
                    val, (tuple, list)) else (val, None, None)
                pid, vol, active = parts
            else:
                exe, pid = (list(item) + [None, None])[:2]
                vol, active = None, True
            if not exe:
                continue
            # Gains ride along unconditionally (intent storage); capture
            # threads additionally require a live PID below.
            if vol is not None:
                try:
                    vol = max(0, min(100, int(vol)))
                except (TypeError, ValueError):
                    vol = None
                if vol is not None:
                    try:
                        self.app_volumes[exe] = vol
                    except AttributeError:
                        pass
            if not pid:
                continue
            try:
                pid = int(pid)
            except (TypeError, ValueError):
                continue
            if pid <= 0 or not self._pid_alive(pid):
                continue
            if active is not None and not active:
                continue  # idle session: no thread until it sounds
            want.setdefault(exe, pid)
        with self.lock:
            cur = {exe: dict(info)
                   for exe, info in self._app_captures.items()}
        for exe, info in cur.items():
            if exe not in want or want[exe] != info.get("pid"):
                self._stop_app_capture(exe)
        now = time.monotonic()
        for exe, pid in want.items():
            info = cur.get(exe)
            if info is not None and info.get("pid") == pid:
                continue
            if self._app_retry.get(exe, 0.0) > now:
                continue
            self._start_app_capture(exe, pid)
        with self.lock:
            any_apps = bool(self._app_captures)
        mt = getattr(self, "_master_thread", None)
        if not any_apps and (mt is None or not mt.is_alive()):
            self._spawn_master_loopback()

    def _start_app_capture(self, exe, pid):
        with self.lock:
            if exe in self._app_captures:
                return
            max_audio = max(1, int(self.settings.get("buffer_seconds", 20)) * 10)
            self.app_audio_replay.setdefault(
                exe, collections.deque(maxlen=max_audio))
            self._app_captures[exe] = {"pid": pid, "alive": True,
                                       "landed": 0.0, "thread": None}
        thread = threading.Thread(
            target=self._capture_app_loopback, args=(exe, pid), daemon=True)
        with self.lock:
            info = self._app_captures.get(exe)
            if info is None or info.get("pid") != pid:
                return  # raced removal
            info["thread"] = thread
            self._app_threads.append(thread)
        thread.start()

    def _stop_app_capture(self, exe):
        # Flag only (no join: caller may be the UI poll thread). The
        # thread observes the missing entry and exits within ~50 ms.
        with self.lock:
            self._app_captures.pop(exe, None)

    def _stop_app_captures(self):
        with self.lock:
            exes = list(self._app_captures)
            for exe in exes:
                self._app_captures.pop(exe, None)
            threads = list(self._app_threads)
            self._app_threads.clear()
        for thread in threads:
            try:
                thread.join(timeout=3)
            except (AssertionError, RuntimeError):
                pass

    def _capture_app_loopback(self, exe, pid):
        """Capture one app's render mix via process loopback (clip stem).

        The slider gain applies here at append time (live response);
        the mixer sums stems wall-aligned like any other stream.
        """
        self._boost_audio_thread()
        if _procloop is None:
            self._log_audio_error("app capture %s: procloop unavailable" % exe)
            with self.lock:
                self._app_captures.pop(exe, None)
            return
        try:
            stream = _procloop.ProcessLoopbackStream(pid)
            stream.start()
        except Exception as exc:
            self._log_audio_error("app capture %s failed: %s"
                                  % (exe, str(exc)[:150]))
            self._app_retry[exe] = time.monotonic() + 30.0
            with self.lock:
                self._app_captures.pop(exe, None)
            return
        sr, ch, chunk = stream.sample_rate, 2, _CHUNK_FRAMES
        bpf = 4 * 2  # float32 stereo bytes per frame
        spill = bytearray()
        spill_start = 0.0
        try:
            while self.recording:
                with self.lock:
                    info = self._app_captures.get(exe)
                if info is None or not info.get("alive", False):
                    break
                if info.get("pid") != pid:
                    break  # replaced
                try:
                    avail = stream.get_read_available()
                except Exception:
                    time.sleep(0.05)
                    continue
                if avail <= 0 and len(spill) < chunk * bpf:
                    time.sleep(0.02)
                    continue
                try:
                    raw, n = stream.read_frames(chunk)
                except Exception:
                    time.sleep(0.05)
                    continue
                if n > 0:
                    now = time.monotonic()
                    if spill and spill_start and (now - spill_start) > 0.5:
                        # Stale partial split by a long starvation gap:
                        # drop it so old samples never smear into a fresh
                        # timestamp (the gap itself stays honest silence).
                        del spill[:]
                    if not spill:
                        spill_start = now
                    spill.extend(raw)
                    del raw
                assembled = False
                while len(spill) >= chunk * bpf:
                    piece = bytes(spill[:chunk * bpf])
                    del spill[:chunk * bpf]
                    if not spill:
                        spill_start = 0.0
                    try:
                        gain = max(0, min(100, int(
                            self.app_volumes.get(exe, 100)))) / 100.0
                    except (AttributeError, TypeError, ValueError):
                        gain = 1.0
                    if gain <= 0.0:
                        # Muted: zeroed chunk, no math, cadence preserved.
                        pcm = np.zeros(chunk * 2, dtype=np.int16)
                    else:
                        a = np.frombuffer(piece, dtype=np.float32).reshape(-1, 2)
                        pcm = np.clip(a * 32767.0, -32768, 32767)
                        del a
                        if gain < 1.0:
                            pcm = pcm * gain
                        pcm = np.clip(pcm, -32768, 32767).astype(np.int16)
                    arrival = time.monotonic()
                    t_start = arrival - chunk / float(sr)
                    with self.lock:
                        dq = self.app_audio_replay.get(exe)
                        cur = self._app_captures.get(exe)
                    if dq is None or cur is None:
                        break
                    dq.append((pcm.tobytes(), sr, 2, t_start))
                    with self.lock:
                        if exe in self._app_captures:
                            self._app_captures[exe]["landed"] = arrival
                    if gain > 0.0:
                        level = float(np.sqrt(np.mean(
                            pcm.astype(np.float64) ** 2)))
                        self.system_audio_buffer.append(level)
                    del pcm
                    assembled = True
                if not assembled:
                    time.sleep(0.01)
        except Exception as exc:
            self._log_audio_error("app capture %s error: %s"
                                  % (exe, str(exc)[:150]))
        finally:
            # Same-thread teardown only (cross-thread close segfaults).
            try:
                stream.close()
            except Exception:
                pass

    def _stop_audio_capture(self):
        # Capture threads never block indefinitely (poll-based reads wake
        # at least every ~20 ms), so generation bump + bounded join always
        # completes. Threads close their own streams; PortAudio is
        # terminated only with no survivors (terminate/close against a
        # blocked reader hangs or segfaults; a survivor's paudio is left
        # for the OS to reclaim on process exit).
        self._audio_generation += 1
        threads = list(self._audio_threads)
        self._audio_threads.clear()
        survived = False
        for thread in threads:
            thread.join(timeout=5)
            if thread.is_alive():
                survived = True
        paudio = getattr(self, '_paudio', None)
        self._paudio = None
        if paudio is not None and not survived:
            try:
                paudio.terminate()
            except Exception:
                pass

    def _restart_audio_capture(self):
        """Restart WASAPI capture (e.g. after a device settings change)."""
        if not self.recording:
            return
        self._stop_audio_capture()
        time.sleep(0.2)
        # Drop pre-restart chunks: their timeline belongs to the old
        # device session and must not mix with the new one.
        self.mic_audio_replay.clear()
        self.sys_audio_replay.clear()
        self._start_audio_capture()

    # --------------------------------------------------------
    # AUDIO MIXING (rendered onto the video wall clock)
    # --------------------------------------------------------

    @staticmethod
    def _resample_cubic(arr, new_len):
        """Catmull-Rom resample of (N, C) float32 to (new_len, C)."""
        n = int(len(arr))
        new_len = int(new_len)
        if n == 0 or new_len <= 0:
            return np.zeros((max(0, new_len), arr.shape[1]), dtype=np.float32)
        if n < 4 or new_len == n:
            if new_len == n:
                return arr.astype(np.float32, copy=True)
            base = np.arange(n)
            idx = np.linspace(0, n - 1, new_len)
            return np.column_stack([
                np.interp(idx, base, arr[:, c])
                for c in range(arr.shape[1])
            ]).astype(np.float32)
        pos = np.linspace(0, n - 1, new_len)
        i = np.floor(pos).astype(np.int64)
        f = (pos - i).astype(np.float64)
        i0 = np.clip(i - 1, 0, n - 1)
        i1 = np.clip(i, 0, n - 1)
        i2 = np.clip(i + 1, 0, n - 1)
        i3 = np.clip(i + 2, 0, n - 1)
        f2 = f * f
        f3 = f2 * f
        w0 = -0.5 * f3 + f2 - 0.5 * f
        w1 = 1.5 * f3 - 2.5 * f2 + 1.0
        w2 = -1.5 * f3 + 2.0 * f2 + 0.5 * f
        w3 = 0.5 * f3 - 0.5 * f2
        a = arr.astype(np.float64)
        out = (w0[:, None] * a[i0] + w1[:, None] * a[i1]
               + w2[:, None] * a[i2] + w3[:, None] * a[i3])
        del a
        return np.clip(out, -32768.0, 32767.0).astype(np.float32)

    @staticmethod
    def _stream_to_clock(buf, video_t0, video_duration, out_sr, out_ch):
        """Render one audio stream onto the video clock.

        Data flows SEQUENTIALLY within gapless runs: consecutive reads
        are contiguous in stream time by construction, so concatenation
        has no splice seams, no overlap doubling, no resample edge
        clicks. Capture timestamps are used ONLY for (a) splitting runs
        at genuine dropouts/blocked-loopback gaps (>150 ms of missing
        time starts a new run, so later audio keeps wall position and
        never squeezes early), (b) one skew-correcting resample per run,
        and (c) wall placement per run. No per-chunk splicing, no
        zero-multiplies, no unbounded allocations: every placed sample
        comes from captured data or honest leading/trailing silence.
        Output is exactly video_duration long. Uses float32.
        """
        total = max(1, int(round(video_duration * out_sr)))
        out = np.zeros((total, out_ch), dtype=np.float32)
        if not buf:
            return out
        native_sr = int(buf[0][1]) if len(buf[0]) > 1 and buf[0][1] else out_sr
        native_sr = max(1, native_sr)
        native_ch = int(buf[0][2]) if len(buf[0]) > 2 and buf[0][2] else out_ch
        native_ch = max(1, min(2, native_ch))
        # 1. Decode + channel-shape (no resampling yet).
        decoded = []
        for entry in buf:
            pcm = entry[0]
            ch = int(entry[2]) if len(entry) > 2 and entry[2] else native_ch
            if ch != native_ch:
                continue
            t_start = entry[3] if len(entry) > 3 else None
            arr = np.frombuffer(pcm, dtype=np.int16).astype(np.float32)
            frames = arr.size // max(1, ch)
            if frames <= 0:
                continue
            arr = arr[:frames * ch].reshape(frames, ch)
            if arr.shape[1] > 2:
                arr = arr[:, :2]
            if arr.shape[1] == 1 and out_ch == 2:
                arr = np.column_stack([arr[:, 0], arr[:, 0]])
            elif arr.shape[1] == 2 and out_ch == 1:
                arr = arr.mean(axis=1, keepdims=True)
            decoded.append((arr, t_start))
        if not decoded:
            return out
        # 2. Split into gapless runs; untimed chunks join the open run.
        runs = []
        cur, cur_t0, cur_t1 = [], None, None
        for arr, t_start in decoded:
            if t_start is not None:
                dur = len(arr) / float(native_sr)
                if cur and cur_t1 is not None and (t_start - cur_t1) > 0.150:
                    runs.append((cur, cur_t0, cur_t1))
                    cur, cur_t0, cur_t1 = [], None, None
                if cur_t0 is None:
                    cur_t0 = t_start
                cur_t1 = t_start + dur
            cur.append(arr)
        if cur:
            runs.append((cur, cur_t0, cur_t1))
        del decoded
        # 3. Render each run: seamless concat, one skew resample to its
        #    exact wall span, wall placement. Runs are wall-disjoint, so
        #    placement never overlaps by construction.
        cursor = 0
        for parts, t0, t1 in runs:
            seq = np.concatenate(parts) if len(parts) > 1 else parts[0]
            del parts
            if t0 is not None and t1 is not None and t1 > t0:
                target = max(1, int(round((t1 - t0) * out_sr)))
            else:
                target = max(1, int(round(len(seq) * out_sr / float(native_sr))))
                t0 = None
            if target != len(seq):
                seq = Recorder._resample_cubic(seq, target)
            if t0 is None:
                o0 = cursor
            else:
                o0 = int(round((t0 - video_t0) * out_sr)) if video_t0 else 0
            i0 = 0
            if o0 < 0:
                i0 = -o0
                o0 = 0
            o1 = o0 + (len(seq) - i0)
            if o1 > total:
                o1 = total
            if o1 > o0 and i0 < len(seq):
                out[o0:o1] = seq[i0:i0 + (o1 - o0)]
                if o1 > cursor:
                    cursor = o1
            del seq
        return out

    @staticmethod
    def _stream_is_fresh(buf, cut_wall, margin=1.0):
        """True if the stream's newest chunk reaches the clip window.

        Stale buffers (capture died long ago) must not render as a
        silent track: the caller should drop the stream instead.
        """
        if not buf:
            return False
        last = buf[-1]
        if len(last) <= 3 or not last[3]:
            return True  # untimed legacy buffer: assume fresh
        try:
            sr = max(1, int(last[1]))
            ch = max(1, int(last[2]))
        except (TypeError, ValueError):
            return True
        dur = len(last[0]) // (2 * ch) / float(sr)
        return (last[3] + dur) >= cut_wall - margin

    @staticmethod
    def _render_desktop_wav(part_bufs, destination, video_t0, video_duration):
        """Sum master-loopback and/or per-app stems wall-aligned to one WAV.

        Fixed 48000 Hz stereo int16, exactly video_duration long.
        Additive only: streams are summed, never subtracted, and a
        silent (muted) app contributes nothing. Returns True/False.
        """
        parts = [b for b in (part_bufs or []) if b]
        if not parts or not video_duration or video_duration <= 0:
            return False
        total = max(1, int(round(video_duration * 48000)))
        mix = np.zeros((total, 2), dtype=np.float32)
        try:
            for buf in parts:
                mix += Recorder._stream_to_clock(
                    buf, video_t0, video_duration, 48000, 2)
        except (ValueError, MemoryError):
            return False
        pcm = np.clip(mix, -32768, 32767).astype(np.int16)
        del mix
        try:
            with wave.open(destination, "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(pcm.tobytes())
        except (OSError, wave.Error):
            return False
        del pcm
        return True

    @staticmethod
    def _render_stream_wav(buf, destination, video_t0, video_duration):
        """Render one audio stream onto the video clock as a WAV.

        Fixed 48000 Hz stereo int16, exactly video_duration long, so the
        remux filtergraph receives uniform inputs. Returns True/False.
        """
        if not buf or not video_duration or video_duration <= 0:
            return False
        arr = Recorder._stream_to_clock(buf, video_t0, video_duration, 48000, 2)
        pcm = np.clip(arr, -32768, 32767).astype(np.int16)
        del arr
        try:
            with wave.open(destination, "wb") as output:
                output.setnchannels(2)
                output.setsampwidth(2)
                output.setframerate(48000)
                output.writeframes(pcm.tobytes())
        except (OSError, wave.Error):
            return False
        del pcm
        return True
