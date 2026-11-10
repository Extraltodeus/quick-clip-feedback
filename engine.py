import json
import queue
import subprocess
import sys
import threading

import numpy as np
from PySide6.QtCore import QElapsedTimer, QObject, QTimer, Qt, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices

CF = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
RATE = 48000


def probe(path):
	cmd = ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,width,height,r_frame_rate:format=duration", "-of", "json", path]
	data = json.loads(subprocess.run(cmd, capture_output=True, creationflags=CF).stdout or b"{}")
	info = {"duration": float(data.get("format", {}).get("duration", 0) or 0), "fps": 24.0, "width": 0, "height": 0, "audio": False}
	for s in data.get("streams", []):
		if s.get("codec_type") == "video" and not info["width"]:
			info["width"], info["height"] = int(s["width"]), int(s["height"])
			num, _, den = s.get("r_frame_rate", "24/1").partition("/")
			info["fps"] = float(num) / float(den or 1) if float(den or 1) else 24.0
		if s.get("codec_type") == "audio":
			info["audio"] = True
	return info


def fit_size(w, h, limit=1280):
	if w <= 0 or h <= 0:
		return 1280, 720
	scale = min(1.0, limit / w)
	return max(2, int(w * scale) // 2 * 2), max(2, int(h * scale) // 2 * 2)


class Pipe(threading.Thread):
	def __init__(self, args, chunk, size, exact):
		super().__init__(daemon=True)
		self.chunk, self.exact = chunk, exact
		self.q = queue.Queue(size)
		self.halt = threading.Event()
		self.done = False
		self.proc = subprocess.Popen(["ffmpeg", "-v", "error", "-nostdin"] + args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, creationflags=CF)

	def run(self):
		try:
			while not self.halt.is_set():
				data = self.proc.stdout.read(self.chunk)
				if not data or (self.exact and len(data) < self.chunk):
					break
				while not self.halt.is_set():
					try:
						self.q.put(data, timeout=0.1)
						break
					except queue.Full:
						pass
		finally:
			self.done = True
			try:
				self.proc.stdout.close()
				self.proc.wait()
			except OSError:
				pass

	def stop(self):
		self.halt.set()
		try:
			self.proc.kill()
		except OSError:
			pass

	def ended(self):
		return self.done and self.q.empty()


def video_pipe(path, start, fps, w, h):
	vf = f"fps={fps:.6f},scale={w}:{h}"
	return Pipe(["-ss", f"{start:.6f}", "-i", path, "-map", "0:v:0", "-an", "-sn", "-vf", vf, "-f", "rawvideo", "-pix_fmt", "rgb24", "-"], w * h * 3, 8, True)


def audio_pipe(path, start):
	return Pipe(["-ss", f"{start:.6f}", "-i", path, "-map", "0:a:0", "-vn", "-sn", "-f", "f32le", "-ac", "2", "-ar", str(RATE), "-"], 16384, 48, False)


class Engine(QObject):
	frame = Signal(object, float)
	ended = Signal()
	PRE = RATE * 8 // 10

	def __init__(self, parent=None):
		super().__init__(parent)
		self.timer = QTimer(self)
		self.timer.setTimerType(Qt.PreciseTimer)
		self.timer.setInterval(4)
		self.timer.timeout.connect(self.tick)
		self.video = self.audio = self.sink = self.io = None
		self.buf = bytearray()
		self.state = 0
		self.volume = 1.0
		self.clock_timer = QElapsedTimer()

	@property
	def playing(self):
		return self.state > 0

	def play(self, path, info, start, w, h):
		self.stop()
		self.fps, self.w, self.h = info["fps"], w, h
		self.start_time, self.duration = start, info["duration"]
		self.video = video_pipe(path, start, self.fps, w, h)
		self.audio = audio_pipe(path, start) if info["audio"] else None
		self.video.start()
		if self.audio:
			self.audio.start()
		self.buf = bytearray()
		self.index = -1
		self.written = 0
		self.state = 1
		self.timer.start()

	def stop(self):
		self.timer.stop()
		for p in (self.video, self.audio):
			if p:
				p.stop()
		self.video = self.audio = None
		if self.sink:
			self.sink.stop()
			self.sink.deleteLater()
		self.sink = self.io = None
		self.state = 0

	def set_volume(self, value):
		self.volume = value
		if self.sink:
			self.sink.setVolume(value)

	def pull(self, need):
		while self.audio and len(self.buf) < need:
			try:
				self.buf.extend(self.audio.q.get_nowait())
			except queue.Empty:
				return

	def begin(self):
		self.state = 2
		if self.audio:
			fmt = QAudioFormat()
			fmt.setSampleRate(RATE)
			fmt.setChannelCount(2)
			fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
			self.sink = QAudioSink(QMediaDevices.defaultAudioOutput(), fmt, self)
			self.sink.setBufferSize(RATE * 4 // 8)
			self.sink.setVolume(self.volume)
			self.io = self.sink.start()
			if self.io is None:
				self.sink.deleteLater()
				self.sink = None
		if not self.sink:
			self.clock_timer.start()

	def clock(self):
		if not self.sink:
			return self.clock_timer.elapsed() / 1000
		pending = (self.sink.bufferSize() - self.sink.bytesFree()) // 4
		return max(self.written - pending, 0) / RATE

	def feed(self):
		free = self.sink.bytesFree() // 4
		if free <= 0:
			return
		self.pull(free * 8)
		count = min(free, len(self.buf) // 8)
		if count <= 0:
			return
		chunk = np.frombuffer(bytes(self.buf[:count * 8]), np.float32)
		del self.buf[:count * 8]
		wrote = self.io.write((np.clip(chunk, -1, 1) * 32767).astype("<i2").tobytes())
		self.written += max(wrote, 0) // 4

	def tick(self):
		if self.state == 1:
			if self.video.q.empty() and not self.video.done:
				return
			self.pull(self.PRE)
			if self.audio and len(self.buf) < self.PRE and not self.audio.done:
				return
			self.begin()
		if self.sink:
			self.feed()
		t = self.clock()
		target = int(t * self.fps + 1e-9)
		image = None
		while self.index < target:
			try:
				data = self.video.q.get_nowait()
			except queue.Empty:
				break
			self.index += 1
			image = np.frombuffer(data, np.uint8).reshape(self.h, self.w, 3)
		if image is not None:
			self.frame.emit(image, self.start_time + self.index / self.fps)
		if self.video.ended() or self.start_time + t >= self.duration:
			self.stop()
			self.ended.emit()


class Grabber(QObject):
	grabbed = Signal(object, float)

	def __init__(self, parent=None):
		super().__init__(parent)
		self.lock = threading.Lock()
		self.want = None
		self.busy = False

	def request(self, path, t, w, h):
		with self.lock:
			self.want = (path, t, w, h)
			if self.busy:
				return
			self.busy = True
		threading.Thread(target=self.work, daemon=True).start()

	def work(self):
		while True:
			with self.lock:
				job, self.want = self.want, None
				if job is None:
					self.busy = False
					return
			path, t, w, h = job
			cmd = ["ffmpeg", "-v", "error", "-nostdin", "-ss", f"{t:.6f}", "-i", path, "-map", "0:v:0", "-frames:v", "1", "-vf", f"scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"]
			raw = subprocess.run(cmd, capture_output=True, creationflags=CF).stdout
			if len(raw) == w * h * 3:
				self.grabbed.emit(np.frombuffer(raw, np.uint8).reshape(h, w, 3), t)


class Peaks(QObject):
	ready = Signal(object, str)

	def load(self, path, bins=4000):
		threading.Thread(target=self.work, args=(path, bins), daemon=True).start()

	def work(self, path, bins):
		cmd = ["ffmpeg", "-v", "error", "-nostdin", "-i", path, "-map", "0:a:0", "-vn", "-ac", "1", "-ar", "8000", "-f", "f32le", "-"]
		raw = subprocess.run(cmd, capture_output=True, creationflags=CF).stdout
		data = np.frombuffer(raw, np.float32)
		if len(data) < bins:
			self.ready.emit(None, path)
			return
		step = len(data) // bins
		block = np.abs(data[:step * bins]).reshape(bins, step)
		peaks = block.max(axis=1)
		top = float(peaks.max()) or 1.0
		self.ready.emit(peaks / top, path)
