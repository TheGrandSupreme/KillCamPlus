import os
import re
import struct
import subprocess
import threading
import time
import queue
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
    from screeninfo import get_monitors
    _HAVE_SCREENINFO = True
except (ImportError, OSError):
    get_monitors = None
    _HAVE_SCREENINFO = False
try:
    import procloop as _procloop
except (ImportError, OSError):
    _procloop = None
try:
    import killcam_core as _ncore  # native capture+feed (opt-in only)
except (ImportError, OSError):
    _ncore = None
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
# buffer length. Snapshot at start(); applies on restart. VBR caps ride
# ~50% above typical CQP averages so brief complex scenes never hit the
# VBV ceiling (visible mush); the byte-budgeted ring absorbs the size.
# RAM caps are sized for DENSE gameplay bitrates (100+ Mbps sustained),
# not averages: an 80 MB ring holds ~6 s of a 30 s window at 13 MB/s, so
# the buffer could never fill. RAM is cheap; the buffer is the product.
# Quality sits softer than reference to bound file sizes: Medium targets
# ~50 Mbps at dense 1080p60 (≈190 MB per 30 s). Budgets and VBV caps are
# untouched (the ring must still hold the full window with headroom).
_COMPRESSION_PROFILES = {
    "High": {"cqp": 31, "crf": 31, "x264_preset": "ultrafast",
             "maxrate_m": 32, "guess_m": 35, "ram_cap_mb": 120},
    "Medium": {"cqp": 29, "crf": 29, "x264_preset": "superfast",
               "maxrate_m": 112, "guess_m": 60, "ram_cap_mb": 512},
    "Low": {"cqp": 19, "crf": 20, "x264_preset": "veryfast",
            "maxrate_m": 128, "guess_m": 100, "ram_cap_mb": 640},
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


def _moof_samples(data, moof_off, moof_size, trex):
    """First traf of a moof. Returns (is_sync_or_None, base_or_None, count)."""
    is_sync, base = _moof_info(data, moof_off, moof_size, trex)
    count = 0
    try:
        for typ, off, size, hdr in _mp4_children(
                data, moof_off + 8, moof_off + moof_size):
            if typ != b"traf":
                continue
            # trun is a direct child of traf; sum every trun present.
            for t2, o2, s2, h2 in _mp4_children(data, off + hdr, off + size):
                if t2 == b"trun":
                    body = o2 + h2
                    if body + 8 <= o2 + s2:
                        count += int.from_bytes(
                            data[body + 4:body + 8], "big")
            break  # first traf only (video-only stream)
    except (IndexError, ValueError):
        pass
    return is_sync, base, count



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
        self._live_transport = None  # "raw" | "compressed" at last probe
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
        self._frag_rate_hist = collections.deque()
        self._frag_pushed_total = 0
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
        # Fresh-feed walls: NVENC skips byte-identical duplicate frames
        # WITHOUT emitting samples, so ring samples are 1:1 with FRESH
        # feeds only (changed pixels always emit). Count-mapping the
        # save window against fresh feeds is therefore exact; mapping
        # against all feeds would drift whenever duplicates are skipped.
        self._fresh_walls = collections.deque(maxlen=1024)
        self._fresh_total = 0

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
        self._preview_thread = None
        self._preview_thread = None
        # Native core (opt-in): owns capture+feed when active; Python
        # loops stay parked. Absent/unbuilt .pyd can never engage it.
        self._nc = None
        self._nc_active = False
        self._nc_drain = None
        self._nc_sup = None
        self._nc_qpc0 = 0
        self._nc_mono0 = 0.0
        self._nc_freq = 1
        self._nc_output = None
        self._nc_region = None
        self._nc_outwh = None
        self._latest_lock = threading.Lock()
        self._latest_frame = None  # freshest raw BGR frame (no-copy handoff)
        self._latest_seq = 0  # capture-side stamp (preview + raw writer)
        # Compressed-transport JPEG state (unused in raw mode).
        self._enc_queue = queue.Queue(maxsize=3)
        self._enc_threads = []  # JPEG worker pool (compressed only)
        self._latest_jpeg = None  # freshest JPEG bytes (compressed writer)
        self._jpeg_seq = 0  # capture-side stamp of the stored JPEG
        self._jpeg_wh = None  # target size pinned at stream launch
        self._enc_drops = 0  # frames discarded: workers slower than capture
        # Continuous session recording (opt-in long-form capture; replay
        # ring keeps rolling independently via tee, so F9 still works).
        # Video + audio spill straight to disk; NOTHING unbounded in RAM.
        self.is_continuous_recording = False
        self._sess_lock = threading.Lock()
        self._sess_dir = None  # per-session scratch dir (video + audio)
        self._sess_video_path = None
        self._sess_video_fh = None
        self._sess_video_bytes = 0
        self._sess_broken = None  # spill failure note (partial kept)
        self._sess_spill_errors = 0  # consecutive spill failures (3 = break)
        self._sess_audio = {}  # kind -> [fh, path] (mic/sys/app_<exe>)
        self._sess_feed_walls = []  # unbounded per-segment wall lists
        self._sess_fresh_walls = []
        self._sess_feed_total = 0
        self._sess_fresh_total = 0
        self._sess_wall_start = 0.0
        self._sess_last_feed = 0.0
        self._sess_dest = None  # final output path (no part suffix if 1 seg)
        self._sess_seg = 0  # segments closed so far
        self._sess_finalizer = None  # background segment-finalize thread
        # re-feeding an unchanged frame (same pixels cost a full
        # encode each slot; a gap reads identically to a duplicate).
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
        self._stream_geom = None  # native pipe geometry at launch
        self._ffmpeg_proc = None

        # Diagnostics
        self._recording_start = 0.0
        self.cap_fps = 60  # configured capture rate (status readout)
        self.capture_frozen = False  # latched by _watch_frozen_capture
        self._frozen_notified_gen = -1
        self._mic_start_time = 0.0
        self._sys_start_time = 0.0
        self.last_stream_error = None
        self.last_save_error = None
        self.last_save_info = None

        # Locks
        self.lock = threading.Lock()

        # Camera
        self.camera = None
        self._pad_wh = None  # cached output size for region padding
        # Capture target state (monitor row vs active-window follow).
        self._cap_output = None
        self._cap_region = None
        self._cap_key = None
        self._cap_label = "Monitor 1"
        self._output_map = None  # dxcam idx -> screeninfo geometry cache

        # Preview thumbnail for UI (JPEG bytes, throttled)
        self.preview_jpeg = None
        self.preview_lock = threading.Lock()
        self.preview_enabled = True  # UI clears when its window loses focus

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

    def _transport_mode(self):
        """Live pipe transport: "raw" (BGR24, default) or "compressed".

        Compressed = JPEG/image2pipe via the encode worker pool (~20x
        less pipe bandwidth at the cost of Python encode CPU + baked-in
        pre-compression artifacts). Applies on next start().
        """
        try:
            if str(self.settings.get("transport_mode", "raw")).strip().lower() == "compressed":
                return "compressed"
        except (AttributeError, TypeError):
            pass
        return "raw"

    def _stream_geometry(self):
        """(native_w, native_h, out_w, out_h) for the live stream.

        native = dxcam output size (pipe -s + pad target: region
        captures are padded up to this, so geometry never changes
        mid-stream); out = settings resolution (scale-filter target).
        """
        try:
            res = str(self.settings.get("resolution", "1920x1080"))
            ow, oh = res.split("x")
            ow, oh = max(16, int(ow)), max(16, int(oh))
        except (AttributeError, TypeError, ValueError):
            ow, oh = 1920, 1080
        nw, nh = ow, oh
        try:
            idx = self._cap_output
            if idx is None:
                idx = max(0, int(self.settings.get("monitor_index", 0)))
            geom = (self._dxcam_output_map() or {}).get(int(idx))
            if geom is not None:
                nw, nh = max(16, int(geom[2])), max(16, int(geom[3]))
        except (AttributeError, TypeError, ValueError):
            pass
        return (nw, nh), (ow, oh)

    def _video_filters(self, native, out, encoder):
        """Scale filter chain (empty when native == target).

        NVENC: format (CPU) + upload + scale_cuda (GPU resample).
        Verified against bundled ffmpeg 8.0.1: `scale_nvenc` does
        not exist (Unknown filter), and bare
        `hwupload_cuda,scale_cuda` fails on RGB input
        (scale_cuda: Unsupported conversion rgb0 -> nv12), while
        `format=nv12,hwupload_cuda,scale_cuda=W:H` exits 0.
        No scale_amf filter exists in this build (-filters lists
        only scale_cuda/scale_d3d11/scale_qsv/scale_vaapi), so AMF
        keeps plain CPU scale; QSV keeps plain scale too (a qsv
        chain needs qsv hardware frames, unverifiable on this box,
        and guessed chains are not shipped).
        Plain CPU scale only otherwise (libx264/AMF/QSV/unknown).

        Compressed transport always returns []: frames are resized to
        the target before JPEG encode, so the pipe already matches.
        """
        try:
            if self._transport_mode() == "compressed":
                return []
            (nw, nh), (ow, oh) = native, out
            if (nw, nh) == (ow, oh):
                return []
            if encoder == "h264_nvenc":
                return ["-vf",
                        "format=nv12,hwupload_cuda,"
                        "scale_cuda=%d:%d" % (ow, oh)]
            return ["-vf", "scale=%d:%d" % (ow, oh)]
        except (AttributeError, TypeError, ValueError):
            return []

    def _live_input_flags(self):
        """Input flag set for the live pipe (raw BGR24 system memory).

        (A GPU-decode variant was trialed and removed: mjpeg_cuvid init
        hangs nondeterministically on some sessions instead of failing,
        stalling startup.)

        Wall-clock arrival timestamps: under gaming load the writer can
        feed fewer frames than wall slots (blocked pipe). Count-based
        timestamps would then compress 30 s of wall into a short sped-up
        clip; arrival timestamps keep stream duration == wall time, so
        shortfalls read as judder and audio stays honest.

        Shallow demux queue (16 packets ~= 0.27 s): timestamps are
        stamped when the demuxer READS, so a deep queue lets them lag
        the wall by up to seconds under load (measured +2.2 s at 128).
        A shallow queue caps the lag; earlier blocking just shows as
        judder, which is the safe direction.

        Transport is raw BGR24 (local RAM pipe): no JPEG roundtrip.
        Size is the NATIVE output size (region captures are padded up
        to it in capture, so geometry never changes mid-stream; the
        scale filter below handles downscaling when configured).

        Compressed transport instead ingests image2pipe/mjpeg (frames
        pre-sized + pre-compressed by the worker pool): no -s needed.
        """
        try:
            if self._transport_mode() == "compressed":
                return ["-thread_queue_size", "16",
                        "-use_wallclock_as_timestamps", "1",
                        "-f", "image2pipe", "-vcodec", "mjpeg"]
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            (nw, nh), _ = self._stream_geometry()
        except (AttributeError, TypeError, ValueError):
            nw, nh = 1920, 1080
        return ["-thread_queue_size", "16",
                "-use_wallclock_as_timestamps", "1",
                "-f", "rawvideo", "-pix_fmt", "bgr24",
                "-s", "%dx%d" % (nw, nh)]

    def _use_gpu(self):
        """GPU encode allowed? User toggle (default on); same quality
        mapping either way, encoder chain just skips GPU entries."""
        try:
            return bool(self.settings.get("use_gpu", True))
        except (AttributeError, TypeError):
            return True

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
        # Cap x264 threads: the default (~1.5x cores) oversubscribes badly
        # next to a game, and contention collapses throughput below what
        # fewer well-fed threads sustain. Still plenty for 60 fps.
        x264_threads = str(min(16, max(4, int(fps) // 10)))
        x264 = ("libx264", ["-preset", prof["x264_preset"], "-tune", "zerolatency",
                            "-threads", x264_threads,
                            "-crf", str(prof["crf"]), "-maxrate", vbr_cap,
                            "-bufsize", bufsize, "-bf", "0"],
                int(prof["maxrate_m"] * 1_000_000 * scale))
        if not self._use_gpu():
            # CPU-only mode: same High/Medium/Low quality mapping, no GPU.
            return [x264]
        # OBS-style lean NVENC: pure CQP (no VBV/maxrate logic at all),
        # p1 single-rapid-pass preset, lookahead explicitly off, forced
        # IDR keyframes. The encoder chip just stamps out fixed-quality
        # frames; maxrate/bufsize caps would only add rate-control work.
        # (Bitrate is unbounded by design; the byte-budgeted ring and
        # ram_cap_mb absorb it. VBR+CQ + p4 was trialed and removed:
        # lookahead + VBV bookkeeping cost GPU cycles for zero quality
        # gain at fixed CQP.)
        return [
            ("h264_nvenc", ["-preset", "p1", "-rc", "constqp",
                            "-qp", str(cqp), "-rc-lookahead", "0",
                            "-forced-idr", "1", "-bf", "0"], int(prof["maxrate_m"] * 1_000_000 * scale)),
            ("h264_amf", ["-quality", "speed", "-rc", "cqp",
                          "-qp_i", str(cqp), "-qp_p", str(cqp)], guess_cap),
            ("h264_qsv", ["-preset", "fast", "-global_quality", str(cqp)], guess_cap),
            x264,
        ]

    def _ensure_live_args(self, fps=60):
        """Validate the full live chain end-to-end; fall back until one works.

        Builds a probe raw-BGR file and runs each candidate encoder through
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
        try:
            tmode_c = (self._transport_mode() == "compressed")
        except (AttributeError, TypeError):
            tmode_c = False
        probe = tempfile.mktemp(suffix=".mjpeg" if tmode_c else ".raw")
        try:
            (pnw, pnh), pout = self._stream_geometry()
            if tmode_c:
                gen = subprocess.run(
                    [self._ffmpeg_path(), "-y",
                     "-f", "lavfi", "-i", "testsrc=size=%dx%d:rate=30:duration=1" % (pout[0], pout[1]),
                     "-c:v", "mjpeg", "-q:v", "3", probe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, creationflags=_NO_WINDOW)
            else:
                gen = subprocess.run(
                    [self._ffmpeg_path(), "-y",
                     "-f", "lavfi", "-i", "testsrc=size=%dx%d:rate=30:duration=1" % (pnw, pnh),
                     "-pix_fmt", "bgr24", "-f", "rawvideo", probe],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=30, creationflags=_NO_WINDOW)
            if gen.returncode != 0 or not os.path.isfile(probe):
                raise RuntimeError("probe build failed")
            for enc, args, cap in self._live_candidates(fps):
                if enc != "libx264" and enc not in available:
                    continue
                cmd = ([self._ffmpeg_path(), "-y"]
                       + self._live_input_flags()
                       + ["-framerate", str(fps), "-i", probe]
                       + self._video_filters((pnw, pnh), pout, enc)
                       + ["-c:v", enc] + args + ["-g", "15",
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
                    self._live_transport = self._transport_mode()
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
        return int(min(prof_cap, derived, 768 * 1024 * 1024))

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
        # Bitrate tracker for honest time-coverage readout (see
        # frag_fill_frac): cumulative bytes vs wall, short window.
        try:
            now = time.monotonic()
            hist = self._frag_rate_hist
            hist.append((now, self._frag_pushed_total + len(chunk)))
            self._frag_pushed_total += len(chunk)
            while len(hist) > 2 and now - hist[0][0] > 10.0:
                hist.popleft()
        except (AttributeError, TypeError):
            pass

    def _resize_to_target(self, frame):
        """Resize frame to the configured resolution (compressed mode).

        Every fed frame is exactly the target size, so the MJPEG pipe
        never changes dims mid-stream (a dims change would corrupt it).
        The target is pinned at stream launch: a settings change mid-run
        stays inert until the next start, like the raw pipe.
        """
        try:
            wh = self._jpeg_wh
        except AttributeError:
            wh = None
        if wh is not None:
            try:
                tw, th = max(16, int(wh[0])), max(16, int(wh[1]))
            except (TypeError, ValueError, IndexError):
                tw, th = 1920, 1080
        else:
            try:
                res = str(self.settings.get("resolution", "1920x1080"))
                tw, th = res.split("x")
                tw, th = max(16, int(tw)), max(16, int(th))
            except (AttributeError, TypeError, ValueError):
                tw, th = 1920, 1080
        try:
            h, w = frame.shape[:2]
        except (AttributeError, TypeError, ValueError, IndexError):
            return frame
        if w == tw and h == th:
            return frame
        return cv2.resize(frame, (tw, th), interpolation=cv2.INTER_LINEAR)

    def _ring_time_span(self):
        """Estimated wall seconds currently held in the ring."""
        try:
            hist = self._frag_rate_hist
            if len(hist) >= 2:
                (t0, b0), (t1, b1) = hist[0], hist[-1]
                if t1 > t0 and b1 > b0:
                    rate = (b1 - b0) / (t1 - t0)  # bytes/sec actual
                    if rate > 0:
                        return self._frag_bytes / rate
            # Cold start / stalled stream: elapsed recording time.
            return time.monotonic() - self._stream_wall_start
        except (AttributeError, TypeError, ValueError, IndexError,
                ZeroDivisionError):
            return 0.0

    def frag_ceiling_mb(self):
        """Current RAM ceiling of the fragment ring in MB (diagnostic)."""
        return self._frag_budget / 1024 / 1024

    def frag_fill_frac(self):
        """0..1 replay-buffer fullness as TIME coverage (wall span held /
        buffer_seconds), not bytes: CQP streams use far less than the
        max bitrate the byte budget assumes, so a byte ratio would crawl
        for minutes while a full time window is already saved. Span comes
        from the measured receive rate, so genuine byte-eviction (dense
        high-bitrate footage outgrowing the budget) still reads <100%."""
        try:
            if not getattr(self, "recording", False):
                return 0.0
            buf_sec = max(1, int(self.settings.get("buffer_seconds", 20)))
            span = self._ring_time_span()
            return min(1.0, max(0.0, span / float(buf_sec)))
        except (AttributeError, TypeError, ValueError):
            return 0.0

    def _pad_to_output(self, frame, wh):
        """Pad a region capture up to the full output size (black bars).

        Keeps pipe geometry constant across window switches so the
        stream never needs relaunching; aspect stays honest (no
        stretching). wh comes from the cached output size.
        """
        try:
            nw, nh = int(wh[0]), int(wh[1])
            h, w = frame.shape[:2]
        except (AttributeError, TypeError, ValueError, IndexError):
            return frame
        if w == nw and h == nh:
            return frame
        if w > nw or h > nh:
            return frame
        try:
            canvas = np.zeros((nh, nw, 3), dtype=np.uint8)
            canvas[:h, :w] = frame
            return canvas
        except (AttributeError, TypeError, ValueError):
            return frame

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
        cmd += ["-framerate", str(fps), "-i", "pipe:0"]
        try:
            native, out = self._stream_geometry()
            cmd += self._video_filters(native, out, self._live_encoder)
        except (AttributeError, TypeError, ValueError):
            pass
        cmd += ["-c:v", self._live_encoder]
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
        try:
            self._stream_geom = self._stream_geometry()[0]
        except (AttributeError, TypeError, ValueError):
            pass
        try:
            self._jpeg_wh = self._stream_geometry()[1]
        except (AttributeError, TypeError, ValueError):
            pass
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

    def _watch_frozen_capture(self, skip_streak, generation):
        """Latch capture_frozen when frames are identical for 90 s+ while
        the user is actively using the PC. Classic cause: an exclusive
        fullscreen game bypassing DWM, so duplication is frozen. The UI
        turns the latch into a one-time hint (borderless mode)."""
        try:
            notified_gen = self._frozen_notified_gen
        except (AttributeError, TypeError):
            notified_gen = -1
            try:
                self._frozen_notified_gen = -1
            except (AttributeError, TypeError):
                pass
        if notified_gen == generation:
            return
        if skip_streak < 900:
            try:
                self.capture_frozen = False
            except (AttributeError, TypeError):
                pass
            return
        try:
            import ctypes
            from ctypes import wintypes

            class _LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", wintypes.UINT),
                            ("dwTime", wintypes.DWORD)]

            lii = _LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(lii)
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return
            idle_ms = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            if idle_ms < 0:
                idle_ms += 2 ** 32
            if idle_ms > 15000:
                return  # idle user + static screen = legit, stay quiet
        except (AttributeError, OSError, ValueError):
            return
        try:
            self.capture_frozen = True
            self._frozen_notified_gen = generation
        except (AttributeError, TypeError):
            pass

    def _capture_loop(self, fps, generation):
        """Grab the freshest screen frame as fast as possible.

        Only grabs + change-detects here; fresh frames go to the encode
        worker (own core), thumbnails to the preview worker (own core).
        cv2 releases the GIL around the heavy ops, so the three stages
        genuinely run on three CPU cores. Identical frames are dropped
        before the queue (zero encode CPU); a forced refresh caps
        staleness on slow fades. Capture always polls at full rate;
        overload shows as judder downstream (duration stays exact),
        never as a slowed file.
        """
        target_interval = 1.0 / max(1, fps)
        idle_interval = min(1.0 / 15.0, target_interval * 4)
        idle_cur = target_interval
        poll_interval = target_interval
        prev_small = None
        skip_streak = 0
        next_tick = time.monotonic()
        last_target = next_tick
        try:
            self.cap_fps = fps
        except (AttributeError, TypeError):
            pass
        try:
            tmode_c = (self._transport_mode() == "compressed")
        except (AttributeError, TypeError):
            tmode_c = False

        while self.recording and generation == self._stream_generation:
            try:
                frame = self.camera.get_latest_frame() if self.camera else None
            except Exception:
                frame = None
                time.sleep(0.05)
            if frame is not None:
                # Pad region captures up to output size first (monitor
                # captures already match; this branch then costs one
                # shape check). Cached size: no map parsing per poll.
                try:
                    pad_wh = self._pad_wh
                except AttributeError:
                    pad_wh = None
                if pad_wh is not None:
                    try:
                        fh, fw = frame.shape[:2]
                        if (fw, fh) != (pad_wh[0], pad_wh[1]):
                            frame = self._pad_to_output(frame, pad_wh)
                    except (AttributeError, TypeError, ValueError,
                            IndexError):
                        pass
                # Cheap change check on a 64x36 stamp (~0.3 ms): static
                # content is dropped before storing.
                small = cv2.resize(frame, (64, 36),
                                   interpolation=cv2.INTER_NEAREST)
                identical = (prev_small is not None and skip_streak < 30
                             and float(cv2.absdiff(small, prev_small).mean()) < 1.0)
                prev_small = small
                del small
                if identical:
                    skip_streak += 1
                    del frame
                    # Static scene: back off polling (detection lag stays
                    # under ~66 ms; full rate resumes on any change)
                    idle_cur = min(idle_cur * 1.5, idle_interval)
                    self._watch_frozen_capture(skip_streak, generation)
                else:
                    skip_streak = 0
                    idle_cur = target_interval
                    try:
                        seq = self._latest_seq + 1
                        with self._latest_lock:
                            self._latest_frame = frame
                            self._latest_seq = seq
                    except Exception:
                        seq = 0
                    if tmode_c and seq:
                        # Compressed pipe: resize to the exact target
                        # (constant pipe geometry) and let the pool
                        # encode it; the writer feeds JPEG bytes.
                        try:
                            if self._enc_queue.full():
                                try:
                                    self._enc_drops += 1
                                except (AttributeError, TypeError):
                                    pass
                            else:
                                try:
                                    resized = self._resize_to_target(frame)
                                except Exception:
                                    resized = frame
                                self._submit_encode(seq, resized)
                        except (AttributeError, TypeError):
                            pass
                    del frame
            poll_interval = idle_cur

            # Follow the configured source ~1 Hz (active window moves).
            now = time.monotonic()
            if now - last_target >= 1.0:
                last_target = now
                self._resolve_capture_target(fps)

            # Pace capture; reset on overrun to avoid catch-up spiral
            next_tick += poll_interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_tick = time.monotonic()

    def _submit_encode(self, seq, frame):
        """Hand a fresh frame to the JPEG workers (compressed transport).

        Drop-oldest when the workers lag (only the latest pixels matter
        downstream). Jobs carry capture-side stamps so workers finishing
        out of order can never regress the stored frame.
        """
        try:
            self._enc_queue.put_nowait((seq, frame))
        except Exception:
            try:
                try:
                    self._enc_queue.get_nowait()
                except Exception:
                    pass
                else:
                    try:
                        self._enc_drops += 1
                    except (AttributeError, TypeError):
                        pass
                self._enc_queue.put_nowait((seq, frame))
            except Exception:
                pass

    def _encode_loop(self, generation):
        """JPEG-encode queued frames on pool threads (compressed only).

        Stores each result only when strictly newer than what's stored,
        so out-of-order completions can never regress the frame.
        """
        encode_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
        while self.recording and generation == self._stream_generation:
            try:
                job_seq, frame = self._enc_queue.get(timeout=0.2)
            except Exception:
                continue
            jpeg = None
            try:
                ok, buf = cv2.imencode(".jpg", frame, encode_params)
                if ok:
                    jpeg = buf.tobytes()
            except (cv2.error, ValueError):
                jpeg = None
            finally:
                try:
                    del frame
                except (NameError, UnboundLocalError):
                    pass
            if jpeg is not None:
                try:
                    with self._latest_lock:
                        if job_seq > self._jpeg_seq:
                            self._latest_jpeg = jpeg
                            self._jpeg_seq = job_seq
                except Exception:
                    pass
                del jpeg

    def _preview_loop(self, generation):
        """Render preview thumbnails on its own core, ~4 Hz.

        Scaled straight from the raw frame (no JPEG roundtrip), plenty
        for a 480px preview. Runs only when a fresh frame landed.
        """
        thumb_params = [cv2.IMWRITE_JPEG_QUALITY, 85]
        last_seq = -1
        while self.recording and generation == self._stream_generation:
            time.sleep(0.25)
            if not self.recording or generation != self._stream_generation:
                break
            try:
                enabled = bool(self.preview_enabled)
            except (AttributeError, TypeError):
                enabled = True
            if not enabled:
                continue  # window unfocused: nobody watches, skip the work
            try:
                with self._latest_lock:
                    seq = self._latest_seq
                    snap = self._latest_frame
            except Exception:
                continue
            if snap is None or seq == last_seq:
                continue
            last_seq = seq
            try:
                h, w = snap.shape[:2]
                thumb_w = 480
                thumb_h = max(1, int(h * thumb_w / w))
                thumb = cv2.resize(
                    snap, (thumb_w, thumb_h),
                    interpolation=cv2.INTER_AREA)
                _, thumb_jpeg = cv2.imencode(".jpg", thumb, thumb_params)
                with self.preview_lock:
                    self.preview_jpeg = thumb_jpeg.tobytes()
                del thumb, thumb_jpeg
            except (cv2.error, ValueError, AttributeError, TypeError):
                pass
            finally:
                try:
                    del snap
                except (NameError, UnboundLocalError):
                    pass

    def _write_loop(self, fps, generation):
        """Emit wall-paced slots to ffmpeg stdin.

        The encoder stamps wall-clock arrival times (live input flag),
        so stream duration always equals wall time: slots skipped under
        load read as judder, never as a sped-up clip, and audio can
        never overhang the video.

        Unchanged frames are NOT re-fed every slot -- EXCEPT on GPU
        encoders (NVENC/AMF/QSV), which buffer output indefinitely on
        sparse input and starve the ring; there every slot is fed so
        fragments stream continuously. A gap still reads identically to
        a duplicate. A 2 Hz minimum cadence keeps fragment/cut
        granularity tight on static scenes (libx264 honors forced keys
        on sparse input, so it can skip freely).
        The first ~1.5 s of a stream always feeds every slot so the
        muxer primes (init + first keyframe) immediately.

        Frames go over the pipe as a raw memory view (no per-slot
        allocation: tobytes() on 6 MB frames churns ~700 MB/s through
        the allocator and dwarfs the encode it replaced).
        """
        target_interval = 1.0 / max(1, fps)
        min_interval = 0.5
        prime_feeds = 90
        # libx264 streams fine on sparse input (honors forced keys);
        # GPU encoders buffer output indefinitely when starved, so only
        # libx264 may skip re-feeding unchanged frames.
        skip_allowed = (self._live_encoder == "libx264")
        try:
            tmode_c = (self._transport_mode() == "compressed")
        except (AttributeError, TypeError):
            tmode_c = False
        next_tick = time.monotonic()
        proc = self._ffmpeg_proc
        last_sent_seq = -1
        last_sent_wall = 0.0
        while self.recording and generation == self._stream_generation:
            if proc is None or proc.poll() is not None:
                break
            if tmode_c:
                with self._latest_lock:
                    payload = self._latest_jpeg
                    seq = self._jpeg_seq
            else:
                with self._latest_lock:
                    payload = self._latest_frame
                    seq = self._latest_seq
            now = time.monotonic()
            priming = self._feed_total < prime_feeds
            is_fresh = (seq != last_sent_seq)
            if payload is not None and (
                    priming or is_fresh or not skip_allowed
                    or now - last_sent_wall >= min_interval):
                try:
                    view = memoryview(payload)
                except TypeError:
                    view = None
                fed = False
                if view is not None:
                    try:
                        while len(view) > 0:
                            written = proc.stdin.write(view)
                            if written is None:
                                break
                            view = view[written:]
                        else:
                            fed = True
                    except (BrokenPipeError, OSError, ValueError):
                        break
                    finally:
                        try:
                            del view
                        except (NameError, UnboundLocalError):
                            pass
                if fed:
                    t_wall = time.monotonic()
                    self._last_feed_wall = t_wall
                    last_sent_wall = t_wall
                    last_sent_seq = seq
                    with self._frag_lock:
                        self._feed_walls.append(t_wall)
                        self._feed_total += 1
                        if is_fresh:
                            self._fresh_walls.append(t_wall)
                            self._fresh_total += 1
                    try:
                        if self.is_continuous_recording:
                            self._sess_note_feed(t_wall, is_fresh)
                    except Exception:
                        pass
                del payload
            next_tick += target_interval
            delay = next_tick - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            elif delay < -0.5:
                next_tick = time.monotonic()  # cap catch-up burst

    def _ingest_block(self, chunk):
        """Fold one stdout block into init-seg / fragment ring.

        Shared by the subprocess reader and the native-core drain loop.
        Active sessions ALSO tee the raw bytes to disk (bounded RAM).
        """
        if self._init_seg is None:
            self._init_pending.extend(chunk)
            if len(self._init_pending) > _INIT_PENDING_MAX:
                self.last_stream_error = "init segment too large"
                return False
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
        try:
            if self.is_continuous_recording:
                self._sess_write_video(chunk)
        except Exception:
            pass
        return True

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
                if not self._ingest_block(chunk):
                    break
                del chunk
        finally:
            try:
                proc.stdout.close()
            except (OSError, ValueError):
                pass
            if (self.recording and generation == self._stream_generation
                    and self._stream_restarts < _RESTART_MAX):
                self._restart_stream("encoder output ended")

    # --------------------------------------------------------
    # NATIVE CORE (opt-in C++ capture+feed; Python loops stay default)
    # --------------------------------------------------------

    def _native_wanted(self):
        """Native path eligible? Flag on + .pyd present + raw transport.

        v1 of the native core only speaks raw BGR24; compressed mode
        stays on the Python worker pool either way.
        """
        try:
            if not bool(self.settings.get("use_native_core", False)):
                return False
        except (AttributeError, TypeError):
            return False
        if _ncore is None:
            return False
        try:
            return self._transport_mode() == "raw"
        except (AttributeError, TypeError):
            return False

    def _nc_map_output(self, out_idx):
        """Map a dxcam output_idx to a DXGI output index by geometry.

        DXGI and dxcam enumerate independently (like screeninfo did),
        so match (x, y, w, h), never position. Returns None on mismatch
        (caller falls back to the Python loops).
        """
        try:
            geom = (self._dxcam_output_map() or {}).get(int(out_idx))
            if geom is None:
                return None
            want = (int(geom[0]), int(geom[1]), int(geom[2]), int(geom[3]))
            for i, (ox, oy, ow, oh) in enumerate(_ncore.RecorderCore.list_outputs()):
                if (int(ox), int(oy), int(ow), int(oh)) == want:
                    return i
        except (AttributeError, TypeError, ValueError):
            pass
        return None

    def _nc_merge_feed_log(self, core):
        """Fold native feed stamps into the wall-clock frame map."""
        try:
            entries = core.drain_feed_log()
        except Exception:
            return 0
        if not entries:
            return 0
        try:
            freq = float(self._nc_freq or 1)
            mono0 = float(self._nc_mono0)
            qpc0 = int(self._nc_qpc0)
        except (AttributeError, TypeError, ValueError):
            return 0
        n = 0
        for tick, fresh in entries:
            try:
                t_wall = mono0 + (int(tick) - qpc0) / freq
            except (TypeError, ValueError):
                continue
            self._last_feed_wall = t_wall
            with self._frag_lock:
                self._feed_walls.append(t_wall)
                self._feed_total += 1
                if fresh:
                    self._fresh_walls.append(t_wall)
                    self._fresh_total += 1
            try:
                if self.is_continuous_recording:
                    self._sess_note_feed(t_wall, fresh)
            except Exception:
                pass
            n += 1
        return n

    def _start_native_stream(self, fps, generation):
        """Launch capture+feed in the native core. Returns core or None.

        None = fall back to the Python loops (state untouched on failure
        beyond last_stream_error, so the caller can proceed normally).
        Requires the capture target already resolved.
        """
        if _ncore is None:
            return None
        try:
            (nw, nh), (ow, oh) = self._stream_geometry()
            out_idx = self._cap_output
            if out_idx is None:
                out_idx = max(0, int(self.settings.get("monitor_index", 0)))
            dxgi_idx = self._nc_map_output(out_idx)
            if dxgi_idx is None:
                self.last_stream_error = "native core: output not found"
                return None
            region = []
            if self._cap_region is not None:
                try:
                    region = [int(v) for v in self._cap_region]
                    if len(region) != 4:
                        region = []
                except (TypeError, ValueError):
                    region = []
            argv = self._live_cmd(fps)
            core = _ncore.RecorderCore()
            if not core.start(argv, int(nw), int(nh), int(ow), int(oh),
                              int(fps), int(dxgi_idx), region):
                try:
                    self.last_stream_error = ("native core: %s" % core.last_error())
                except Exception:
                    self.last_stream_error = "native core start failed"
                try:
                    core.stop()
                except Exception:
                    pass
                return None
            self._nc_qpc0 = int(_ncore.RecorderCore.qpc_now())
            self._nc_mono0 = time.monotonic()
            try:
                self._nc_freq = int(_ncore.RecorderCore.qpc_frequency())
            except Exception:
                self._nc_freq = 1
            self._stream_wall_start = self._nc_mono0
            self._nc = core
            self._nc_active = True
            self._nc_output = out_idx
            self._nc_region = (tuple(region) if region else None)
            self._nc_outwh = (int(ow), int(oh))
            return core
        except Exception as exc:
            self.last_stream_error = "native core: %s" % exc
            try:
                self._nc = None
                self._nc_active = False
            except (AttributeError, TypeError):
                pass
            return None

    def _native_drain_loop(self, core, generation):
        """Pump native stdout into the ring; merge feed stamps; fps meter."""
        sec_count = 0
        sec_start = time.monotonic()
        try:
            while self.recording and generation == self._stream_generation:
                try:
                    chunk = core.read_stdout(_FRAG_BLOCK)
                except Exception:
                    break
                if chunk:
                    if not self._ingest_block(chunk):
                        break
                    del chunk
                else:
                    time.sleep(0.01)
                try:
                    n = self._nc_merge_feed_log(core)
                    sec_count += n
                except Exception:
                    pass
                now = time.monotonic()
                if now - sec_start >= 1.0:
                    try:
                        self.cap_fps = int(sec_count / max(0.25, now - sec_start))
                    except (AttributeError, TypeError):
                        pass
                    sec_count = 0
                    sec_start = now
                try:
                    alive = bool(core.child_alive())
                except Exception:
                    alive = False
                if not alive:
                    try:
                        tail = core.read_stdout(1 << 20)
                    except Exception:
                        tail = b""
                    if tail:
                        try:
                            self._ingest_block(tail)
                        except Exception:
                            pass
                        continue
                    break  # EOF: encoder exited
        finally:
            if (self.recording and generation == self._stream_generation
                    and self._stream_restarts < _RESTART_MAX
                    and self._nc_active):
                self._restart_stream("native encoder output ended")

    def _native_sup_loop(self, core, fps, generation):
        """1 Hz target-follow (reconfigure, no ffmpeg relaunch) + 4 Hz
        preview handoff into the existing _latest_frame machinery."""
        tick = 0
        try:
            ow, oh = self._nc_outwh or (1920, 1080)
        except (AttributeError, TypeError, ValueError):
            ow, oh = 1920, 1080
        while self.recording and generation == self._stream_generation:
            time.sleep(0.25)
            if not self.recording or generation != self._stream_generation:
                break
            tick += 1
            if tick % 4 == 0:
                try:
                    self._resolve_capture_target(fps)
                except Exception:
                    pass
                try:
                    if (self._cap_output != self._nc_output
                            or self._cap_region != self._nc_region):
                        dxgi_idx = self._nc_map_output(self._cap_output)
                        if dxgi_idx is not None:
                            region = ([int(v) for v in self._cap_region]
                                      if self._cap_region is not None else [])
                            try:
                                core.reconfigure(int(dxgi_idx), region)
                            except Exception:
                                pass
                            else:
                                self._nc_output = self._cap_output
                                self._nc_region = self._cap_region
                except (AttributeError, TypeError, ValueError):
                    pass
            try:
                enabled = bool(self.preview_enabled)
            except (AttributeError, TypeError):
                enabled = True
            if not enabled:
                continue
            try:
                frm = core.get_latest_frame()
            except Exception:
                continue
            if not frm:
                continue
            try:
                arr = np.frombuffer(frm, dtype=np.uint8).reshape(oh, ow, 3)
            except (ValueError, TypeError):
                continue
            try:
                with self._latest_lock:
                    self._latest_frame = arr
                    self._latest_seq += 1
            except Exception:
                pass
            del arr, frm

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
        # A stream restart breaks the session timeline: close the current
        # segment and finalize it in the background, then keep recording
        # into a fresh segment (parts land as <stem>_partNNN.mp4).
        try:
            sess_active = bool(self.is_continuous_recording)
        except (AttributeError, TypeError):
            sess_active = False
        if sess_active:
            try:
                snap = self._close_session_segment()
                dest = self._sess_dest
            except Exception:
                snap, dest = None, None
            if snap is not None and dest:
                try:
                    part = self._sess_part_path(dest, snap.get("seg", 0))
                    worker = threading.Thread(
                        target=self._sess_finalize_bg,
                        args=(snap, part), daemon=True)
                    worker.start()
                except (AttributeError, TypeError, RuntimeError):
                    pass
        # Prune dead stream threads (no unbounded growth)
        for attr in ("_cap_thread", "_write_thread", "_read_thread",
                     "_preview_thread", "_nc_drain", "_nc_sup"):
            thread = getattr(self, attr, None)
            if thread is not None and not thread.is_alive():
                setattr(self, attr, None)
        try:
            self._enc_threads = [t for t in self._enc_threads
                                 if t.is_alive()]
        except (AttributeError, TypeError):
            self._enc_threads = []
        try:
            fps = int(self.settings.get("fps", 60))
        except (ValueError, TypeError):
            fps = 60
        try:
            was_native = bool(self._nc_active)
        except (AttributeError, TypeError):
            was_native = False
        if was_native and self._native_wanted():
            old = self._nc
            self._nc = None
            self._nc_active = False
            if old is not None:
                try:
                    old.stop()
                except Exception:
                    pass
            with self._frag_lock:
                self._frag_deque.clear()
                self._frag_bytes = 0
                self._frag_rate_hist.clear()
                self._frag_pushed_total = 0
                self._frag_budget = self._frag_budget_bytes()
                self._init_seg = None
                self._init_pending = bytearray()
                self._last_feed_wall = 0.0
                self._feed_walls.clear()
                self._feed_total = 0
                self._fresh_walls.clear()
                self._fresh_total = 0
                self._ffmpeg_proc = None
            with self._latest_lock:
                self._latest_frame = None
                self._latest_seq = 0
            self._nc_active = True
            try:
                core = self._start_native_stream(fps, generation)
            except Exception:
                core = None
            if core is not None:
                self._nc_drain = threading.Thread(
                    target=self._native_drain_loop,
                    args=(core, generation), daemon=True)
                self._nc_sup = threading.Thread(
                    target=self._native_sup_loop,
                    args=(core, fps, generation), daemon=True)
                preview = threading.Thread(
                    target=self._preview_loop, args=(generation,), daemon=True)
                self._preview_thread = preview
                self._nc_drain.start()
                self._nc_sup.start()
                preview.start()
                return
            self._nc_active = False
            # Native relaunch failed: fall through to the Python path
            # below (buffer is lost either way on a dead encoder).
            # State-only resolves built no camera: force a real rebuild.
            self._cap_output = None
            self._cap_region = None
            self.camera = None
            try:
                self._resolve_capture_target(fps)
            except Exception:
                pass
        if self.camera is None and not self._nc_active:
            # Python relaunch with no camera (e.g. flag flipped mid-run):
            # rebuild before relaunching the encoder.
            self._cap_output = None
            self._cap_region = None
            try:
                self._resolve_capture_target(fps)
            except Exception:
                pass
        try:
            proc, wall_start = self._launch_stream(fps)
        except OSError as exc:
            self.last_stream_error = "encoder relaunch failed: %s" % exc
            return
        with self._frag_lock:
            self._frag_deque.clear()
            self._frag_bytes = 0
            self._frag_rate_hist.clear()
            self._frag_pushed_total = 0
            self._frag_budget = self._frag_budget_bytes()
            self._init_seg = None
            self._init_pending = bytearray()
            self._stream_wall_start = wall_start
            self._last_feed_wall = 0.0
            self._feed_walls.clear()
            self._feed_total = 0
            self._fresh_walls.clear()
            self._fresh_total = 0
            self._ffmpeg_proc = proc
        with self._latest_lock:
            self._latest_frame = None
            self._latest_seq = 0
            self._latest_jpeg = None
            self._jpeg_seq = 0
        self._enc_drops = 0
        try:
            while True:
                self._enc_queue.get_nowait()
        except Exception:
            pass
        cap = threading.Thread(
            target=self._capture_loop, args=(fps, generation), daemon=True)
        writer = threading.Thread(
            target=self._write_loop, args=(fps, generation), daemon=True)
        read = threading.Thread(
            target=self._read_loop, args=(proc, generation), daemon=True)
        preview = threading.Thread(
            target=self._preview_loop, args=(generation,), daemon=True)
        self._cap_thread = cap
        self._write_thread = writer
        self._read_thread = read
        self._preview_thread = preview
        self._enc_threads = []
        try:
            tmode_c = (self._transport_mode() == "compressed")
        except (AttributeError, TypeError):
            tmode_c = False
        if tmode_c:
            for _ in range(2):
                self._enc_threads.append(threading.Thread(
                    target=self._encode_loop, args=(generation,), daemon=True))
        cap.start()
        writer.start()
        read.start()
        preview.start()
        for thread in self._enc_threads:
            thread.start()

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

    @staticmethod
    def _fit_stream_offset(frag, trex, timescale, fresh_walls, fresh_total,
                           feed_walls, feed_total, last_feed, last_base):
        """Median-fit stream-time -> wall offset (replay + session ladder).

        Each sync moof gives two wall estimates: (a) its stream timestamp
        + unknown offset C (relatively exact: wallclock arrival stamps are
        proven wall-paced), (b) count-walk feed wall (exact when samples
        are 1:1 with feeds on the basis). C = median(a - b) rejects
        outliers from duplicate-emit/skip surprises; basis (fresh vs all
        feeds) = tighter MAD. Falls back to end-anchor when too few
        anchors agree (notably stream tails where feeds outrun emits).

        Returns (ts_offset, use_anchor, fits, moofs, total_samples) with
        moofs = [(off, is_sync, base, count)] in stream order.
        """
        def _fit_basis(basis_walls, basis_total):
            try:
                n_walls = len(basis_walls)
                if n_walls == 0:
                    return None
                base0 = basis_total - n_walls
                diffs = []
                running = basis_total - total_samples
                for off, is_sync, base, count in moofs:
                    if is_sync and base is not None:
                        idx = running - base0
                        if 0 <= idx < n_walls:
                            try:
                                diffs.append((basis_walls[idx]
                                              - base / float(timescale),
                                              off, base))
                            except (TypeError, ValueError, ZeroDivisionError,
                                    IndexError):
                                pass
                    running += count
                if len(diffs) < 3:
                    return None
                vals = sorted(d[0] for d in diffs)
                med = vals[len(vals) // 2]
                mad = sorted(abs(v - med) for v in vals)[len(vals) // 2]
                return mad, med
            except Exception:
                return None

        try:
            boxes, _ = _scan_fragments(frag)
        except (TypeError, ValueError):
            return None, True, [], [], 0
        moofs = []
        for typ, off, size in boxes:
            if typ != b"moof":
                continue
            try:
                is_sync, base, count = _moof_samples(frag, off, size, trex)
            except (TypeError, ValueError):
                continue
            moofs.append((off, is_sync, base, count))
        total_samples = sum(m[3] for m in moofs)
        fits = []
        for basis_walls, basis_total in ((fresh_walls, fresh_total),
                                        (feed_walls, feed_total)):
            try:
                fit = _fit_basis(basis_walls, basis_total)
            except Exception:
                fit = None
            if fit is not None:
                fits.append(fit)
        use_anchor = True
        ts_offset = None
        if fits:
            fits.sort(key=lambda f: f[0])
            if fits[0][0] <= 1.0:
                use_anchor = False
                ts_offset = fits[0][1]
        if use_anchor:
            if last_base is not None:
                try:
                    ts_offset = last_feed - last_base / float(timescale)
                except (TypeError, ValueError, ZeroDivisionError):
                    ts_offset = None
        return ts_offset, use_anchor, fits, moofs, total_samples

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
            fresh_walls = list(self._fresh_walls)
            fresh_total = self._fresh_total
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

        # Wall mapping: fit stream-time -> wall offset over sync moofs.
        # Shared ladder (replay + session): median-fit over feed walls
        # with end-anchor fallback; see _fit_stream_offset.
        (ts_offset, use_anchor, fits, moofs,
         total_samples) = Recorder._fit_stream_offset(
            frag, trex, timescale, fresh_walls, fresh_total,
            feed_walls, feed_total, last_feed, last_base)
        if total_samples <= 0:
            self.last_save_error = "no decodable fragment"
            del frag
            return False

        def _wall_of(base):
            if base is None or ts_offset is None:
                return None, None
            try:
                return base / float(timescale) + ts_offset, None
            except (TypeError, ValueError, ZeroDivisionError):
                return None, None

        boxes, _ = _scan_fragments(frag)
        moofs = []  # (off, is_sync, base), ring order
        for typ, off, size in boxes:
            if typ != b"moof":
                continue
            is_sync, base = _moof_info(frag, off, size, trex)
            moofs.append((off, is_sync, base))
        if not moofs:
            self.last_save_error = "no decodable fragment"
            del frag
            return False

        # End = last moof in the ring (tail frames fed but unflushed have
        # no timestamps yet; forced keys keep the tail tiny).
        _end_wall, _end_idx = _wall_of(moofs[-1][2])
        end_wall = _end_wall if _end_wall is not None else last_feed
        end_idx = _end_idx
        last_base = moofs[-1][2]

        # Clip window: trailing buf_sec of wall time. Start at the first
        # keyframe-led fragment at/after (end - buf_sec) so the clip
        # covers exactly the replay window.
        t_ideal = end_wall - buf_sec
        cut_wall = None
        cut_off = None
        cut_idx = None
        cut_base = None
        first_sync = None
        for off, is_sync, base in moofs:
            if is_sync:
                wall, idx = _wall_of(base)
                if first_sync is None:
                    first_sync = (off, wall, base)
                if wall is not None and wall >= t_ideal:
                    cut_off, cut_wall, cut_base = off, wall, base
                    cut_idx = idx
                    break
        if cut_off is None:
            if first_sync is not None:
                cut_off, cut_wall, cut_base = first_sync
                cut_idx = None
            else:
                self.last_save_error = "no decodable fragment"
                del frag
                return False
            if cut_wall is None:
                cut_wall = wall_start  # fallback: stream start
        if SYNC_DEBUG is not None:
            SYNC_DEBUG.update(
                timescale=timescale, fps=fps,
                last_base=last_base, cut_base=cut_base,
                ts_offset=ts_offset, fit_used=not use_anchor,
                fit_mad=fits[0][0] if fits else None,
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
        # Insurance: the clip cannot outrun the video stream's own
        # timestamp span (guards any feed underrun; normally a no-op).
        # Span-based, NOT sample-count-based: under load fewer frames
        # cover the same wall span, and count/fps would wrongly shrink
        # the clip into a sped-up fragment.
        if last_base is not None and cut_base is not None:
            try:
                vid_span = (last_base - cut_base) / float(timescale)
            except (TypeError, ValueError, ZeroDivisionError):
                vid_span = 0.0
            if vid_span > 0.5 and vid_span < duration - 0.25:
                duration = max(0.5, vid_span)

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
            cmd = self._remux_cmd(cache_path, sys_wav, mic_wav)
            return self._remux_clip(cmd, output_path, duration)
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

    def _remux_cmd(self, cache_path, sys_wav, mic_wav):
        """Build the input+map+filter+audio-codec remux command.

        NOTE: [a:0]/[a:1] pad syntax is rejected by this ffmpeg
        ("matches no streams"); [1:a]/[2:a] address the identical
        streams (sys=first audio input, mic=second). Chain and
        options otherwise as specified, plus normalize=0: amix
        defaults to attenuating the sum (measured -6 dB on a
        reference tone); normalize=0 preserves the previous
        straight-sum loudness.
        """
        cmd = [self._ffmpeg_path(), "-y", "-i", cache_path]
        if sys_wav:
            cmd.extend(["-thread_queue_size", "1024", "-i", sys_wav])
        if mic_wav:
            cmd.extend(["-thread_queue_size", "1024", "-i", mic_wav])
        if sys_wav and mic_wav:
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
        return cmd

    def _remux_clip(self, cmd, output_path, duration, timeout=120):
        """Run a -c:v copy remux command (shared save/session tail).

        cmd is the fully built input+map+filter+audio-codec command; this
        appends the copy/faststart/duration tail, runs ffmpeg, and
        validates the output. Does NOT delete inputs (caller owns them).
        """
        cmd = list(cmd)
        try:
            cmd.extend([
                "-c:v", "copy",
                "-async", "1",
                "-avoid_negative_ts", "make_zero",
                "-movflags", "+faststart",
                "-t", "%.3f" % duration,
                output_path])
            completed = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                timeout=timeout, creationflags=_NO_WINDOW)
            if completed.returncode != 0:
                self.last_save_error = "ffmpeg error: %s" % completed.stderr.decode(
                    "utf-8", errors="replace")[-500:]
                return False
            if not (os.path.isfile(output_path)
                    and os.path.getsize(output_path) > 0):
                self.last_save_error = "empty output"
                return False
            return True
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.last_save_error = str(exc)[:300]
            return False

    # --------------------------------------------------------
    # CONTINUOUS SESSION RECORDING (tee: replay ring unaffected)
    # --------------------------------------------------------
    # Long-form capture alongside the rolling buffer: every ingested
    # fragment is ALSO appended to a per-segment disk file (bounded RAM
    # always), and every audio chunk is spilled to sidecar files. The
    # replay ring keeps rolling independently, so F9 mid-session works.
    # Finalize is RAM-held like save_clip (video scan + WAV renders):
    # verified to tens of minutes; beyond that best-effort.
    # Audio spill layout per chunk: >diii (t_start, sr, ch, nbytes) + PCM.

    _SESS_AHDR = struct.Struct(">diii")

    def start_continuous_recording(self, output_path):
        """Begin a long-form session writing to output_path on stop.

        Returns True. Requires the replay buffer running (the session
        tees off its encoder). Safe to call once; second call fails.
        """
        try:
            with self._sess_lock:
                if self.is_continuous_recording:
                    self.last_save_error = "session already recording"
                    return False
                if not self.recording:
                    self.last_save_error = "start the replay buffer first"
                    return False
                if not output_path:
                    self.last_save_error = "no session destination"
                    return False
                base = tempfile.gettempdir()
                stamp = time.strftime("%Y%m%d_%H%M%S")
                self._sess_dir = os.path.join(
                    base, "killcam_session_%d_%s" % (os.getpid(), stamp))
                os.makedirs(self._sess_dir, exist_ok=True)
                self._sess_dest = str(output_path)
                self._sess_seg = 0
                self._sess_video_path = None
                self._sess_video_fh = None
                self._sess_video_bytes = 0
                self._sess_broken = None
                self._sess_spill_errors = 0
                self._sess_audio = {}
                self._sess_feed_walls = []
                self._sess_fresh_walls = []
                self._sess_feed_total = 0
                self._sess_fresh_total = 0
                self._sess_wall_start = time.monotonic()
                self._sess_last_feed = 0.0
                try:
                    if self._init_seg:
                        self._sess_ensure_video()
                        self._sess_video_fh.write(bytes(self._init_seg))
                        self._sess_video_fh.flush()
                        self._sess_video_bytes += len(self._init_seg)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
                self.is_continuous_recording = True
            return True
        except (OSError, TypeError, ValueError) as exc:
            try:
                self.is_continuous_recording = False
                self.last_save_error = str(exc)[:300]
            except Exception:
                pass
            return False

    def _sess_ensure_video(self):
        """Open the current segment video file (caller holds _sess_lock)."""
        if self._sess_video_fh is None:
            if not self._sess_dir:
                raise OSError("no session dir")
            self._sess_video_path = os.path.join(
                self._sess_dir, "seg%03d.mp4" % self._sess_seg)
            self._sess_video_fh = open(self._sess_video_path, "wb")

    def _sess_break(self, reason):
        """Fail the session loud but keep partial files (caller may hold lock)."""
        try:
            with self._sess_lock:
                if not self.is_continuous_recording:
                    return
                self.is_continuous_recording = False
                self._sess_broken = str(reason)[:200]
                try:
                    if self._sess_video_fh is not None:
                        self._sess_video_fh.flush()
                        self._sess_video_fh.close()
                except (OSError, ValueError):
                    pass
                self._sess_video_fh = None
                for _fh, _p in list(self._sess_audio.values()):
                    try:
                        _fh.flush()
                        _fh.close()
                    except (OSError, ValueError):
                        pass
                self._sess_audio = {}
        except Exception:
            pass

    def _sess_note_spill_error(self, reason):
        """Count a spill failure; break the session after 3 consecutive.

        A lone failure is usually a restart-adjacent handle race (the
        next write lazy-opens fresh); sustained failure means the disk
        is actually gone. Partial files are always kept.
        """
        try:
            with self._sess_lock:
                self._sess_spill_errors += 1
                if self._sess_spill_errors >= 3:
                    self._sess_spill_errors = 0
                    self._sess_break("spill failed: %s" % reason)
        except (AttributeError, TypeError):
            pass

    def _sess_write_video(self, chunk):
        """Append one ingested block to the session file (crash-flushed).

        Must NEVER raise into the reader thread (see _sess_spill_audio).
        """
        if not chunk:
            return
        try:
            with self._sess_lock:
                if not self.is_continuous_recording:
                    return
                self._sess_ensure_video()
                self._sess_video_fh.write(chunk)
                self._sess_video_fh.flush()
                self._sess_video_bytes += len(chunk)
                self._sess_spill_errors = 0
        except Exception as exc:
            self._sess_note_spill_error(exc)

    def _sess_spill_audio(self, kind, payload, sr, ch, t_start):
        """Append one audio chunk to its session sidecar (crash-flushed).

        Must NEVER raise: audio capture threads call this inline, and a
        telemetry failure must not kill a live audio tap.
        """
        if not payload:
            return
        try:
            with self._sess_lock:
                if not self.is_continuous_recording:
                    return
                entry = self._sess_audio.get(kind)
                if entry is None:
                    if not self._sess_dir:
                        return
                    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_",
                                  str(kind))[:48] or "stream"
                    path = os.path.join(
                        self._sess_dir,
                        "seg%03d_%s.pcm" % (self._sess_seg, safe))
                    entry = [open(path, "wb"), path]
                    self._sess_audio[kind] = entry
                fh = entry[0]
                fh.write(self._SESS_AHDR.pack(
                    float(t_start), int(sr), int(ch), len(payload)))
                fh.write(payload)
                fh.flush()
                self._sess_spill_errors = 0
        except Exception as exc:
            self._sess_note_spill_error(exc)

    def _sess_note_feed(self, t_wall, is_fresh):
        """Record one fed frame's wall stamp for session mapping.

        Must NEVER raise into the writer thread (see _sess_spill_audio).
        """
        try:
            with self._sess_lock:
                if not self.is_continuous_recording:
                    return
                self._sess_feed_walls.append(t_wall)
                self._sess_feed_total += 1
                if is_fresh:
                    self._sess_fresh_walls.append(t_wall)
                    self._sess_fresh_total += 1
                self._sess_last_feed = t_wall
        except Exception:
            pass

    def _sess_part_path(self, dest, seg):
        root, ext = os.path.splitext(str(dest))
        if not ext:
            ext = ".mp4"
        return "%s_part%03d%s" % (root, seg + 1, ext)

    def _close_session_segment(self):
        """Snapshot the current segment for finalize; reset file state.

        Returns the snapshot dict, or None when the segment holds no
        video (nothing to finalize). Files stay on disk; cleanup happens
        per-segment after successful finalize.
        """
        with self._sess_lock:
            try:
                if self._sess_video_fh is not None:
                    try:
                        self._sess_video_fh.flush()
                        self._sess_video_fh.close()
                    except (OSError, ValueError):
                        pass
                    self._sess_video_fh = None
                for _fh, _p in list(self._sess_audio.values()):
                    try:
                        _fh.flush()
                        _fh.close()
                    except (OSError, ValueError):
                        pass
                audio = {k: p for k, (_f, p) in self._sess_audio.items()}
                self._sess_audio = {}
                snap = None
                if self._sess_video_bytes > 0 and self._sess_video_path:
                    snap = {
                        "video": self._sess_video_path,
                        "audio": audio,
                        "feed_walls": self._sess_feed_walls,
                        "fresh_walls": self._sess_fresh_walls,
                        "feed_total": self._sess_feed_total,
                        "fresh_total": self._sess_fresh_total,
                        "wall_start": self._sess_wall_start,
                        "last_feed": self._sess_last_feed,
                        "seg": self._sess_seg,
                    }
                self._sess_video_path = None
                self._sess_video_bytes = 0
                self._sess_feed_walls = []
                self._sess_fresh_walls = []
                self._sess_feed_total = 0
                self._sess_fresh_total = 0
                self._sess_last_feed = 0.0
                self._sess_wall_start = time.monotonic()
                self._sess_seg += 1
                return snap
            except (AttributeError, TypeError):
                return None

    @staticmethod
    def _sess_read_spill(path):
        """Parse one audio sidecar back into [(bytes, sr, ch, t)] chunks."""
        chunks = []
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return chunks
        off, n = 0, len(data)
        hdrlen = Recorder._SESS_AHDR.size
        while off + hdrlen <= n:
            try:
                t_start, sr, ch, size = Recorder._SESS_AHDR.unpack_from(
                    data, off)
            except struct.error:
                break
            off += hdrlen
            if size < 0 or off + size > n:
                break
            try:
                chunks.append((bytes(data[off:off + size]),
                               int(sr), int(ch), float(t_start)))
            except (TypeError, ValueError):
                break
            off += size
        del data
        return chunks

    def _finalize_session_snapshot(self, snap, dest_path):
        """Remux one closed segment (video file + spilled audio) to dest.

        End-anchored wall mapping (proven save_clip fallback): no full
        feed-wall fit needed. RAM-held like save_clip: fine to tens of
        minutes of footage; beyond that best-effort.
        """
        self.last_save_error = None
        video = (snap or {}).get("video")
        try:
            if not video or not os.path.isfile(video):
                self.last_save_error = "session segment empty"
                return False
            vsize = os.path.getsize(video)
            if vsize <= 0:
                self.last_save_error = "session segment empty"
                return False
            with open(video, "rb") as f:
                head = f.read(4 * 1024 * 1024)
            if not head:
                self.last_save_error = "session segment empty"
                return False
            boxes, _ = _scan_fragments(head)
            init_end = None
            for typ, off, size in boxes:
                if typ == b"moov":
                    init_end = off + size
                    break
            if init_end is None:
                self.last_save_error = "session has no init segment"
                return False
            init = head[:init_end]
            trex = _trex_defaults(init)
            timescale = _video_timescale(init) or 15360
            last_feed = snap.get("last_feed") or time.monotonic()
            cut_base = None
            last_base = None
            # Shared median-fit ladder when the segment fits the RAM-held
            # bound (same mapping replay uses: tail stall anchors become
            # median-rejected outliers instead of the whole story).
            # Beyond the bound, end-anchor only (documented degradation).
            FIT_READ_MAX = 1536 * 1024 * 1024
            if vsize <= FIT_READ_MAX:
                with open(video, "rb") as f:
                    data = f.read()
                if not data:
                    self.last_save_error = "session segment empty"
                    return False
                (ts_offset, _use_anchor, _fits, moofs,
                 _total) = Recorder._fit_stream_offset(
                    data, trex, timescale,
                    list(snap.get("fresh_walls") or []),
                    snap.get("fresh_total") or 0,
                    list(snap.get("feed_walls") or []),
                    snap.get("feed_total") or 0,
                    last_feed, None)
                del head
                if ts_offset is None:
                    for _o, _s, b, _c in moofs:
                        if b is not None:
                            last_base = b
                    if last_base is None:
                        self.last_save_error = "session tail undecodable"
                        return False
                    try:
                        ts_offset = (last_feed
                                     - last_base / float(timescale))
                    except (TypeError, ValueError, ZeroDivisionError):
                        self.last_save_error = "session clock mapping failed"
                        return False
                first = None
                for _o, is_sync, b, _c in moofs:
                    if is_sync and b is not None:
                        first = b
                        break
                if first is None:
                    self.last_save_error = "session has no keyframe"
                    return False
                cut_base = first
                if last_base is None:
                    for _o, _s, b, _c in moofs:
                        if b is not None:
                            last_base = b
                try:
                    cut_wall = cut_base / float(timescale) + ts_offset
                except (TypeError, ValueError, ZeroDivisionError):
                    self.last_save_error = "session clock mapping failed"
                    return False
                end_wall = last_feed
                for _o, _s, b, _c in reversed(moofs):
                    if b is not None:
                        try:
                            end_wall = b / float(timescale) + ts_offset
                        except (TypeError, ValueError, ZeroDivisionError):
                            pass
                        break
                del data
            else:
                # First sync moof at/after init (scan forward if needed).
                first = self._sess_first_sync(video, init_end, trex)
                if first is None:
                    self.last_save_error = "session has no keyframe"
                    return False
                cut_off, cut_base = first
                # Last moof base from the tail (closed file: complete).
                with open(video, "rb") as f:
                    try:
                        f.seek(max(0, vsize - 8 * 1024 * 1024))
                        tail = f.read()
                    except OSError:
                        tail = b""
                last_base = None
                if tail:
                    tboxes, _ = _scan_fragments(tail)
                    for typ, off, size in tboxes:
                        if typ != b"moof":
                            continue
                        try:
                            _sync, base = _moof_info(tail, off, size, trex)
                        except (TypeError, ValueError):
                            continue
                        if base is not None:
                            last_base = base
                if last_base is None:
                    self.last_save_error = "session tail undecodable"
                    return False
                try:
                    ts_offset = last_feed - last_base / float(timescale)
                    cut_wall = cut_base / float(timescale) + ts_offset
                except (TypeError, ValueError, ZeroDivisionError):
                    self.last_save_error = "session clock mapping failed"
                    return False
                end_wall = last_feed
                del head, tail
            duration = end_wall - cut_wall
            if duration < 0.5:
                self.last_save_error = "session too short"
                return False
            try:
                vid_span = (last_base - cut_base) / float(timescale)
            except (TypeError, ValueError, ZeroDivisionError):
                vid_span = 0.0
            if vid_span > 0.5 and vid_span < duration - 0.25:
                duration = max(0.5, vid_span)

            # Audio: rebuild chunk lists from sidecars, same gates as replay.
            mic_chunks, sys_chunks, app_bufs = [], [], []
            for kind, path in (snap.get("audio") or {}).items():
                chunks = self._sess_read_spill(path)
                if not chunks:
                    continue
                if kind == "mic":
                    mic_chunks = chunks
                elif kind == "sys":
                    sys_chunks = chunks
                elif kind.startswith("app_"):
                    app_bufs.append(chunks)
            has_mic = mic_chunks and self._buffer_has_audio(mic_chunks)
            has_sys = sys_chunks and self._buffer_has_audio(sys_chunks)
            if has_mic and not Recorder._stream_is_fresh(mic_chunks, cut_wall):
                has_mic = False
            if has_sys and not Recorder._stream_is_fresh(sys_chunks, cut_wall):
                has_sys = False
            desktop_parts = []
            if has_sys:
                desktop_parts.append(sys_chunks)
            for abuf in app_bufs:
                if (abuf and self._buffer_has_audio(abuf)
                        and Recorder._stream_is_fresh(abuf, cut_wall)):
                    desktop_parts.append(abuf)
            os.makedirs(os.path.dirname(os.path.abspath(dest_path)),
                        exist_ok=True)
            temp_files = []
            try:
                sys_wav = mic_wav = None
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
                timeout = min(3600, max(180, duration * 1.5 + 120))
                ok = self._remux_clip(
                    self._remux_cmd(video, sys_wav, mic_wav),
                    dest_path, duration, timeout=timeout)
            finally:
                for path in temp_files:
                    if path and os.path.isfile(path):
                        try:
                            os.remove(path)
                        except OSError:
                            pass
            if ok:
                try:
                    os.remove(video)
                except OSError:
                    pass
                for path in (snap.get("audio") or {}).values():
                    try:
                        os.remove(path)
                    except OSError:
                        pass
            return ok
        except (OSError, RuntimeError, ValueError, TypeError) as exc:
            self.last_save_error = str(exc)[:300]
            return False

    @staticmethod
    def _sess_first_sync(video_path, start_off, trex):
        """(file_offset, base) of the first sync moof at/after start_off."""
        try:
            scanned = 0
            with open(video_path, "rb") as f:
                f.seek(start_off)
                while scanned < 64 * 1024 * 1024:
                    data = f.read(4 * 1024 * 1024)
                    if not data:
                        return None
                    boxes, _ = _scan_fragments(data)
                    for typ, off, size in boxes:
                        if typ != b"moof":
                            continue
                        try:
                            is_sync, base, _count = _moof_samples(
                                data, off, size, trex)
                        except (TypeError, ValueError):
                            continue
                        if is_sync and base is not None:
                            return start_off + scanned + off, base
                    scanned += len(data)
        except OSError:
            pass
        return None

    def _sess_finalize_bg(self, snap, dest_path):
        """Background segment finalize (restart path): never raises."""
        try:
            ok = self._finalize_session_snapshot(snap, dest_path)
            if not ok and not self.last_save_error:
                self.last_save_error = "background segment finalize failed"
            elif ok:
                try:
                    d = self._sess_dir
                    if d and os.path.isdir(d) and not os.listdir(d):
                        os.rmdir(d)
                except OSError:
                    pass
        except Exception as exc:
            try:
                self.last_save_error = str(exc)[:300]
            except Exception:
                pass

    def stop_continuous_recording(self):
        """Close the session and finalize every segment synchronously.

        Single segment (no restarts) lands exactly on the requested path;
        otherwise segments land as <stem>_partNNN.mp4. Returns True only
        when every segment finalized; scratch is cleaned per success.
        """
        with self._sess_lock:
            active = bool(self.is_continuous_recording)
            dest = self._sess_dest
            closed = int(self._sess_seg)
            self.is_continuous_recording = False
        if not active or not dest:
            self.last_save_error = "no active session"
            return False
        snap = self._close_session_segment()
        if snap is None and closed == 0:
            self.last_save_error = "session too short"
            self._sess_cleanup_dir()
            return False
        ok_all = True
        if snap is not None:
            dest_path = dest if closed == 0 else self._sess_part_path(
                dest, closed)
            if not self._finalize_session_snapshot(snap, dest_path):
                ok_all = False
        self._sess_cleanup_dir()
        if self._sess_broken:
            try:
                self.last_save_info = "session spill issue: %s" % self._sess_broken
            except (AttributeError, TypeError):
                pass
        return ok_all

    def _sess_cleanup_dir(self):
        try:
            d = self._sess_dir
            self._sess_dir = None
            if d and os.path.isdir(d) and not os.listdir(d):
                os.rmdir(d)
        except (OSError, TypeError, AttributeError):
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
        use_gpu = self._use_gpu()
        if (self._live_encoder is None or self._live_fps != fps
                or self._active_profile != self._compression_profile()
                or getattr(self, "_live_use_gpu", None) != use_gpu
                or getattr(self, "_live_transport", None) != self._transport_mode()):
            self._live_encoder = None
            self._active_profile = self._compression_profile()
            self._live_use_gpu = use_gpu
            self._ensure_live_args(fps)
            self._live_fps = fps

        self.recording = True
        self.last_stream_error = None
        self.last_save_error = None
        with self._frag_lock:
            self._frag_deque = collections.deque()
            self._frag_bytes = 0
            self._frag_rate_hist = collections.deque()
            self._frag_pushed_total = 0
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
            self._fresh_walls = collections.deque(maxlen=wall_cap)
            self._fresh_total = 0
        with self._latest_lock:
            self._latest_frame = None
            self._latest_seq = 0
            self._latest_jpeg = None
            self._jpeg_seq = 0
        self._enc_drops = 0
        try:
            while True:
                self._enc_queue.get_nowait()
        except Exception:
            pass
        # Fresh boot: no session can survive across start(); close any
        # leaked handles defensively (normally already finalized).
        try:
            self.is_continuous_recording = False
            if self._sess_video_fh is not None:
                try:
                    self._sess_video_fh.close()
                except (OSError, ValueError):
                    pass
                self._sess_video_fh = None
            for _fh, _p in list(self._sess_audio.values()):
                try:
                    _fh.close()
                except (OSError, ValueError):
                    pass
            self._sess_audio = {}
            self._sess_dir = None
            self._sess_dest = None
        except (AttributeError, TypeError):
            pass

        # Resolve the configured source FIRST so window/pinned modes
        # start on the right output (the stream geometry below must
        # match the camera or the pipe corrupts). Under the native core
        # the target resolve is state-only (no dxcam camera is built).
        self._cap_output = None
        self._cap_region = None
        try:
            want_native = bool(self._native_wanted())
        except (AttributeError, TypeError):
            want_native = False
        if want_native:
            self._nc = None
            self._nc_active = True
            self.camera = None
        try:
            self._resolve_capture_target(fps)
        except Exception as exc:
            self.recording = False
            self.camera = None
            if want_native:
                self._nc_active = False
            raise RuntimeError(f"Could not start screen capture: {exc}") from exc
        if want_native:
            if self._cap_output is None:
                self.recording = False
                self._nc_active = False
                raise RuntimeError("Could not start screen capture: no output")
        elif self.camera is None:
            self.recording = False
            raise RuntimeError("Could not start screen capture: no output")
        try:
            mon_idx = max(0, int(self.settings.get("monitor_index", 0)))
        except (ValueError, TypeError):
            mon_idx = 0
        try:
            if (str(self.settings.get("capture_source", "monitor")).lower()
                    == "monitor" and self._cap_output == 0 and mon_idx != 0):
                self.monitor_index = 0
                self.settings["monitor_index"] = 0
        except (AttributeError, TypeError, ValueError):
            pass

        try:
            native_core = None
            if want_native:
                try:
                    native_core = self._start_native_stream(fps, generation)
                except Exception:
                    native_core = None
            if native_core is not None:
                with self._frag_lock:
                    self._last_feed_wall = 0.0
                    self._ffmpeg_proc = None
                self._cap_thread = None
                self._write_thread = None
                self._read_thread = None
                self._nc_drain = threading.Thread(
                    target=self._native_drain_loop,
                    args=(native_core, generation), daemon=True)
                self._nc_sup = threading.Thread(
                    target=self._native_sup_loop,
                    args=(native_core, fps, generation), daemon=True)
                self._preview_thread = threading.Thread(
                    target=self._preview_loop, args=(generation,), daemon=True)
                self._nc_drain.start()
                self._nc_sup.start()
                self._preview_thread.start()
                self._start_audio_capture()
                return
            self._nc_active = False
            self._nc = None
            # State-only resolve built no camera: force a real dxcam build.
            self._cap_output = None
            self._cap_region = None
            try:
                self._resolve_capture_target(fps)
            except Exception:
                pass
            if self.camera is None:
                self.recording = False
                raise RuntimeError("Could not start screen capture: no output")
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
        self._preview_thread = threading.Thread(
            target=self._preview_loop, args=(generation,), daemon=True)
        self._enc_threads = []
        if self._transport_mode() == "compressed":
            for _ in range(2):
                self._enc_threads.append(threading.Thread(
                    target=self._encode_loop, args=(generation,), daemon=True))
        self._cap_thread.start()
        self._write_thread.start()
        self._read_thread.start()
        self._preview_thread.start()
        for thread in self._enc_threads:
            thread.start()
        self._start_audio_capture()

    def _dxcam_output_map(self):
        """Map dxcam output_idx -> screeninfo geometry.

        screeninfo and dxcam enumerate in DIFFERENT orders (verified:
        screeninfo lists the 1200p panel first, dxcam puts the 1080p
        primary at Output[0]), so align by (resolution, primary flag),
        not position. Cached; rebuilt when the display count changes.
        Returns {idx: (x, y, w, h)} (origins from screeninfo).
        """
        try:
            import re
            mons = list(get_monitors()) if _HAVE_SCREENINFO else []
        except Exception:
            mons = []
        try:
            import dxcam as _dxcam
            info = str(_dxcam.output_info())
            outs = []
            for line in info.splitlines():
                m = re.search(
                    r"Output\[(\d+)\].*?Res:\((\d+)\s*,\s*(\d+)\).*?"
                    r"Primary:(True|False)", line)
                if m:
                    outs.append((int(m.group(1)), int(m.group(2)),
                                 int(m.group(3)), m.group(4) == "True"))
        except Exception:
            outs = []
        mapping = {}
        used = set()
        for mon in mons:
            try:
                mw, mh = int(mon.width), int(mon.height)
                prim = bool(getattr(mon, "is_primary", False))
            except (TypeError, ValueError, AttributeError):
                continue
            for idx, ow, oh, oprim in outs:
                if idx in used:
                    continue
                if ow == mw and oh == mh and oprim == prim:
                    try:
                        mapping[idx] = (int(mon.x), int(mon.y), mw, mh)
                    except (TypeError, ValueError, AttributeError):
                        continue
                    used.add(idx)
                    break
        try:
            if (self._output_map is None
                    or set(self._output_map) != set(mapping)
                    or len(mapping) != len(mons)):
                self._output_map = mapping
        except (AttributeError, TypeError):
            pass
        return mapping or (self._output_map or {})

    def _foreground_target(self):
        """Active-window capture target, or None to keep the current one.

        Returns (output_idx, region, key, label): key is a stable process
        id for switch detection (titles change constantly), label is the
        friendly name for the preview. Skips our own windows,
        minimized/zero-area windows, and shells with no identity.
        """
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            hwnd = user32.GetForegroundWindow()
            if not hwnd:
                return None
            pid = ctypes.c_ulong()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            try:
                own = int(pid.value) == os.getpid()
            except (TypeError, ValueError):
                own = False
            if own:
                return None  # ours (app, popup, dialog): keep target
            try:
                if user32.IsIconic(hwnd):
                    return None  # minimized: keep target
            except (AttributeError, OSError):
                pass
            rect = wintypes.RECT()
            try:
                if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    return None
            except (AttributeError, OSError):
                return None
            w, h = rect.right - rect.left, rect.bottom - rect.top
            if w < 64 or h < 64:
                return None
            key = ""
            try:
                import psutil
                key = str(psutil.Process(int(pid.value)).name() or "")
            except Exception:
                key = ""
            try:
                n = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if n > 0:
                    buf = ctypes.create_unicode_buffer(min(n + 1, 128))
                    user32.GetWindowTextW(hwnd, buf, min(n + 1, 128))
                    title = buf.value or ""
            except (AttributeError, OSError, ValueError):
                title = ""
            label = title.strip() or key
            if not key:
                key = label
            if not key:
                return None
            if len(label) > 40:
                label = label[:37] + "..."
            cx, cy = rect.left + w // 2, rect.top + h // 2
            mapping = self._dxcam_output_map()
            out_idx, origin = None, (0, 0)
            for idx, (ox, oy, ow, oh) in mapping.items():
                if ox <= cx < ox + ow and oy <= cy < oy + oh:
                    out_idx, origin = idx, (ox, oy)
                    break
            if out_idx is None:
                return None
            ox, oy = origin
            region = (max(0, rect.left - ox), max(0, rect.top - oy),
                      rect.right - ox, rect.bottom - oy)
            return out_idx, region, key, label
        except Exception:
            return None

    def _pinned_target(self):
        """Pinned-window target: largest visible window of the pinned exe.

        Returns (output_idx, region, key, label) like _foreground_target,
        or None to keep the current target (app closed/minimized). Unlike
        active-follow, the window need not be foreground -- but it must
        stay unminimized and uncovered (region crops the display).
        """
        try:
            want = str(self.settings.get("capture_window", "") or "")
        except (AttributeError, TypeError):
            want = ""
        want = want.strip().lower()
        if not want:
            return None
        try:
            import ctypes
            from ctypes import wintypes
            user32 = ctypes.windll.user32
            best = None  # (area, hwnd, rect)
            cands = []

            CB = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND,
                                    wintypes.LPARAM)

            def cb(hwnd, _):
                try:
                    if not user32.IsWindowVisible(hwnd):
                        return True
                    if user32.IsIconic(hwnd):
                        return True
                    pid = ctypes.c_ulong()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                    try:
                        if int(pid.value) == os.getpid():
                            return True  # ours: never
                    except (TypeError, ValueError):
                        pass
                    rect = wintypes.RECT()
                    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                        return True
                    w = rect.right - rect.left
                    h = rect.bottom - rect.top
                    if w < 64 or h < 64:
                        return True
                    cands.append((w * h, hwnd,
                                  (rect.left, rect.top, rect.right,
                                   rect.bottom), int(pid.value)))
                except Exception:
                    pass
                return True

            user32.EnumWindows(CB(cb), 0)
            try:
                import psutil
                for area, hwnd, rect, pid in sorted(
                        cands, key=lambda c: -c[0]):
                    try:
                        if str(psutil.Process(pid).name() or "").lower() != want:
                            continue
                    except Exception:
                        continue
                    best = (area, hwnd, rect, pid)
                    break
            except ImportError:
                return None
            if best is None:
                return None
            _, hwnd, (l, t, r, b), pid = best
            w, h = r - l, b - t
            try:
                n = user32.GetWindowTextLengthW(hwnd)
                title = ""
                if n > 0:
                    buf = ctypes.create_unicode_buffer(min(n + 1, 128))
                    user32.GetWindowTextW(hwnd, buf, min(n + 1, 128))
                    title = buf.value or ""
            except (AttributeError, OSError, ValueError):
                title = ""
            label = (title.strip() or want) + " (pinned)"
            if len(label) > 40:
                label = label[:30] + "... (pinned)"
            mapping = self._dxcam_output_map()
            out_idx, origin = None, (0, 0)
            for idx, (ox, oy, ow, oh) in mapping.items():
                if ox <= l + w // 2 < ox + ow and oy <= t + h // 2 < oy + oh:
                    out_idx, origin = idx, (ox, oy)
                    break
            if out_idx is None:
                return None
            ox, oy = origin
            region = (max(0, l - ox), max(0, t - oy), r - ox, b - oy)
            return out_idx, region, "pin:" + want, label
        except Exception:
            return None

    def _resolve_capture_target(self, fps):
        """Point the camera at the configured source (called ~1 Hz).

        Monitor pins (monitor_index, full output); active follows the
        foreground window; pinned tracks a chosen exe wherever it is
        (foreground or not -- but it must stay unminimized and visible:
        covering it records the cover). Rebuilds the camera only on
        change; the writer's min-cadence replay bridges the gap either
        way.
        """
        try:
            want_source = str(self.settings.get("capture_source",
                                                "monitor")).lower()
        except (AttributeError, TypeError):
            want_source = "monitor"
        if want_source == "pinned":
            try:
                want_exe = str(self.settings.get("capture_window", "") or "")
            except (AttributeError, TypeError):
                want_exe = ""
            tgt = self._pinned_target()
            if tgt is None:
                # Nothing to switch to (app closed or never chosen): hold
                # the current target but say so on the label instead of
                # silently keeping a stale window.
                try:
                    self._cap_label = ("Pinned: choose an app" if not want_exe.strip()
                                       else "Pinned: %s (not found)" % want_exe.strip()[:24])
                except (AttributeError, TypeError):
                    pass
                return
            out_idx, region, key, label = tgt
        elif want_source == "active":
            tgt = self._foreground_target()
            if tgt is None:
                return  # keep current target (transient/ours/minimized)
            out_idx, region, key, label = tgt
        else:
            try:
                out_idx = max(0, int(self.settings.get("monitor_index", 0)))
            except (ValueError, TypeError):
                out_idx = 0
            region, key, label = None, None, None
        try:
            same_out = (out_idx == self._cap_output)
            same_key = (key is None or key == self._cap_key)
        except (AttributeError, TypeError):
            same_out, same_key = False, False
        try:
            geom_now = None
            g = (self._dxcam_output_map() or {}).get(out_idx)
            if g is not None:
                geom_now = (int(g[2]), int(g[3]))
        except (AttributeError, TypeError, ValueError):
            geom_now = None
        try:
            geom_changed = (geom_now is not None
                            and self._stream_geom is not None
                            and tuple(geom_now) != tuple(self._stream_geom))
        except (AttributeError, TypeError, ValueError):
            geom_changed = False
        if same_out and same_key and not geom_changed:
            if region is None or self._cap_region is None:
                if region == self._cap_region:
                    return
            else:
                try:
                    drift = max(abs(a - b) for a, b in zip(
                        [int(v) for v in region],
                        [int(v) for v in self._cap_region]))
                except (TypeError, ValueError, AttributeError):
                    drift = 999
                if drift <= 12:
                    # Same window, tiny move: adopt the rect without the
                    # restart churn (crop lags <1 s while dragging).
                    try:
                        self._cap_region = tuple(int(v) for v in region)
                    except (TypeError, ValueError):
                        pass
                    return
        try:
            self._rebuild_camera(out_idx, region, label, fps)
        except Exception:
            pass
        try:
            self._cap_key = key
        except (AttributeError, TypeError):
            pass
        if geom_changed and self.recording:
            # Display mode changed mid-run: raw pipe geometry no longer
            # matches. Relaunch the stream (loses the buffer, rare).
            # Compressed pipe is pre-sized to the target, so monitor
            # geometry never affects it: no relaunch needed.
            try:
                tmode_c = (self._transport_mode() == "compressed")
            except (AttributeError, TypeError):
                tmode_c = False
            if not tmode_c:
                try:
                    self._restart_stream("display geometry changed")
                except Exception:
                    pass

    def _rebuild_camera(self, output_idx, region, label, fps):
        """(Re)create dxcam on an output + region (region None = full).

        Falls back to output 0 on failure. Keeps _cap_* state + label.
        Under the native core, duplication is native-owned: track the
        state only (the sup loop reconfigures the core itself).
        """
        try:
            output_idx = max(0, int(output_idx))
        except (ValueError, TypeError):
            output_idx = 0
        try:
            if bool(self._nc_active):
                self._cap_output = output_idx
                self._cap_region = region
                try:
                    if label:
                        self._cap_label = str(label)
                    elif region is None:
                        self._cap_label = "Monitor %d" % (output_idx + 1)
                    else:
                        self._cap_label = "Monitor %d (region)" % (output_idx + 1)
                except (TypeError, ValueError):
                    pass
                return
        except (AttributeError, TypeError):
            pass
        cam = self.camera
        self.camera = None
        if cam is not None:
            try:
                cam.stop()
            except Exception:
                pass
        try:
            fps_i = int(fps)
        except (ValueError, TypeError):
            fps_i = 60
        try:
            self.camera = dxcam.create(output_idx=output_idx,
                                       output_color="BGR", max_buffer_len=4)
            if region is not None:
                try:
                    self.camera.region = tuple(int(v) for v in region)
                except (TypeError, ValueError, AttributeError):
                    pass
            self.camera.start(target_fps=min(fps_i * 2, 360))
        except Exception:
            if output_idx != 0:
                self.camera = dxcam.create(output_idx=0,
                                           output_color="BGR",
                                           max_buffer_len=4)
                output_idx = 0
                region = None
                self.camera.start(target_fps=min(fps_i * 2, 360))
            else:
                raise
        self._cap_output = output_idx
        self._cap_region = region
        try:
            geom = (self._dxcam_output_map() or {}).get(output_idx)
            self._pad_wh = (int(geom[2]), int(geom[3])) if geom else None
        except (AttributeError, TypeError, ValueError):
            self._pad_wh = None
        try:
            if label:
                self._cap_label = str(label)
            elif region is None:
                self._cap_label = "Monitor %d" % (output_idx + 1)
            else:
                self._cap_label = "Monitor %d (region)" % (output_idx + 1)
        except (TypeError, ValueError):
            pass

    def _restart_camera(self):
        """Recreate the screen capture on the current monitor_index.

        The encoder keeps running (writer repeats the last JPEG across
        the gap), so the stream and buffer survive a monitor switch.
        """
        try:
            fps = int(self.settings.get("fps", 60))
        except (ValueError, TypeError):
            fps = 60
        try:
            mon_idx = max(0, int(self.settings.get("monitor_index", 0)))
        except (ValueError, TypeError):
            mon_idx = 0
        self._rebuild_camera(mon_idx, None, None, fps)
        try:
            if self._cap_output == 0 and mon_idx != 0:
                self.monitor_index = 0
                self.settings["monitor_index"] = 0
        except (AttributeError, TypeError, ValueError):
            pass

    def stop(self):
        if not self.recording:
            return

        self.recording = False
        # An active session finalizes here (files only: no live streams
        # needed), synchronously so nothing is lost on quit. The stop
        # call below flips the session flag first, cutting off new spill
        # writes before teardown joins the threads further down.
        try:
            if bool(self.is_continuous_recording):
                try:
                    self.stop_continuous_recording()
                except Exception:
                    pass
        except (AttributeError, TypeError):
            pass
        try:
            native = bool(self._nc_active)
        except (AttributeError, TypeError):
            native = False
        if native:
            core = self._nc
            self._nc = None
            self._nc_active = False
            if core is not None:
                try:
                    core.stop()
                except Exception:
                    pass
            for thread in (self._nc_drain, self._nc_sup,
                           self._preview_thread):
                if thread is not None and thread.is_alive():
                    thread.join(timeout=5)
            self._nc_drain = None
            self._nc_sup = None
            self._cap_thread = None
            self._write_thread = None
            self._read_thread = None
            self._preview_thread = None
            self._ffmpeg_proc = None
            self._stop_audio_capture()
            self._stop_app_captures()
            self.camera = None
            return
        proc = self._ffmpeg_proc
        # Closing stdin lets ffmpeg finalize the stream and exit cleanly
        if proc is not None:
            try:
                proc.stdin.close()
            except (OSError, ValueError):
                pass
        for thread in (self._cap_thread, self._write_thread,
                       self._read_thread, self._preview_thread,
                       *self._enc_threads):
            if thread is not None and thread.is_alive():
                thread.join(timeout=5)
        self._cap_thread = None
        self._write_thread = None
        self._read_thread = None
        self._preview_thread = None
        self._enc_threads = []
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
                # Timestamp = device time of the chunk start: arrival minus
                # the chunk itself MINUS whatever was still buffered ahead
                # of it. Backlog depth varies with load (deep when busy),
                # so a fixed constant under-corrects exactly when it
                # matters; measuring per chunk self-calibrates. Falls back
                # to arrival-minus-duration when unreadable.
                arrival = time.monotonic()
                try:
                    backlog = max(0, int(stream.get_read_available()))
                except Exception:
                    backlog = -1
                # Clamp: backlog is bounded by the device buffer in any
                # sane implementation; absurd values mean the API reports
                # cumulative/foreign units, and subtracting them would
                # place audio seconds early. 2x frames_per_buffer caps
                # damage while never binding legitimate depths.
                try:
                    backlog_cap = 2 * int(chunk * 2)
                except (TypeError, ValueError):
                    backlog_cap = 19200
                if backlog < 0 or backlog > backlog_cap:
                    if backlog > backlog_cap:
                        try:
                            self._log_audio_error(
                                "loopback backlog implausible: %d" % backlog)
                        except Exception:
                            pass
                    backlog = 0
                try:
                    import collections as _collections
                    _bl = getattr(Recorder, "_backlog_dbg", None)
                    if _bl is None:
                        Recorder._backlog_dbg = _bl = _collections.deque(maxlen=500)
                    _bl.append(backlog)
                except Exception:
                    pass
                if backlog < 0:
                    backlog = 0
                t_start = (arrival - (Recorder._BACKLOG_FACTOR * backlog
                                       + pcm.size // max(1, ch))
                           / float(sr))
                if self._sys_start_time == 0.0:
                    self._sys_start_time = arrival
                audio_replay.append((pcm.tobytes(), sr, ch, t_start))
                try:
                    if self.is_continuous_recording:
                        self._sess_spill_audio(
                            "sys", pcm.tobytes(), sr, ch, t_start)
                except Exception:
                    pass
                # Peak level for the UI bars: single-pass max/min, no
                # float conversion (RMS costed measurable CPU across
                # three capture threads for a display-only number).
                try:
                    _mx = int(pcm.max())
                    _mn = int(pcm.min())
                except (ValueError, TypeError):
                    _mx, _mn = 0, 0
                level = float(_mx if _mx >= -_mn else -_mn)
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
                # Timestamp = device time of the chunk start: arrival minus
                # the chunk itself MINUS whatever was still buffered ahead
                # of it (see system loop: fixed constants under-correct
                # under load; per-chunk measurement self-calibrates).
                arrival = time.monotonic()
                try:
                    backlog = max(0, int(stream.get_read_available()))
                except Exception:
                    backlog = -1
                # Clamp: backlog is bounded by the device buffer in any
                # sane implementation; absurd values mean the API reports
                # cumulative/foreign units, and subtracting them would
                # place audio seconds early. 2x frames_per_buffer caps
                # damage while never binding legitimate depths.
                try:
                    backlog_cap = 2 * int(chunk * 2)
                except (TypeError, ValueError):
                    backlog_cap = 19200
                if backlog < 0 or backlog > backlog_cap:
                    if backlog > backlog_cap:
                        try:
                            self._log_audio_error(
                                "loopback backlog implausible: %d" % backlog)
                        except Exception:
                            pass
                    backlog = 0
                try:
                    import collections as _collections
                    _bl = getattr(Recorder, "_backlog_dbg", None)
                    if _bl is None:
                        Recorder._backlog_dbg = _bl = _collections.deque(maxlen=500)
                    _bl.append(backlog)
                except Exception:
                    pass
                if backlog < 0:
                    backlog = 0
                t_start = (arrival - (Recorder._BACKLOG_FACTOR * backlog
                                       + pcm.size // max(1, ch))
                           / float(sr))
                if self._mic_start_time == 0.0:
                    self._mic_start_time = arrival
                audio_replay.append((pcm.tobytes(), sr, ch, t_start))
                try:
                    if self.is_continuous_recording:
                        self._sess_spill_audio(
                            "mic", pcm.tobytes(), sr, ch, t_start)
                except Exception:
                    pass
                # Peak level for the UI bars (see system loop).
                try:
                    _mx = int(pcm.max())
                    _mn = int(pcm.min())
                except (ValueError, TypeError):
                    _mx, _mn = 0, 0
                level = float(_mx if _mx >= -_mn else -_mn)
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
                    try:
                        if self.is_continuous_recording:
                            self._sess_spill_audio(
                                "app_" + str(exe), pcm.tobytes(),
                                sr, 2, t_start)
                    except Exception:
                        pass
                    with self.lock:
                        if exe in self._app_captures:
                            self._app_captures[exe]["landed"] = arrival
                    if gain > 0.0:
                        # Peak level for the UI bars (see system loop).
                        try:
                            _mx = int(pcm.max())
                            _mn = int(pcm.min())
                        except (ValueError, TypeError):
                            _mx, _mn = 0, 0
                        level = float(_mx if _mx >= -_mn else -_mn)
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
    # Backlog factor: get_read_available() overstates true sample age
    # on this loopback path (full correction lands audibly early,
    # none lands late). 0.5 bisects; user-judged early 2026-10-02 ->
    # 0.25. Adjust by ear only if needed.
    _BACKLOG_FACTOR = 0.25

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
