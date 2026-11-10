import ctypes
import json
import os
import sys

from PySide6.QtCore import QEvent, QObject, QTimer, Qt
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication, QComboBox, QHBoxLayout, QLabel, QMainWindow, QPlainTextEdit, QPushButton, QScrollArea, QSplitter, QVBoxLayout, QWidget

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from engine import Engine, Grabber, Peaks, fit_size, probe
from store import NoteStore, new_note, read_json, write_json
from widgets import NoteRow, VideoView, Waveform, stamp

ROOT = os.path.dirname(os.path.dirname(HERE))
HOME = ROOT if os.path.isdir(os.path.join(ROOT, "Productions")) else HERE
CONFIG = os.path.join(HERE, "config.json").replace("\\", "/")
REVIEWS = os.path.join(HOME, "Reviews").replace("\\", "/")
HELP = "\n".join([
	"Space / click on the video: play / pause",
	"X: play from the start",
	"C: pause",
	"Left / Right: 5 s back / forward",
	"Wheel on the waveform: time (down = forward)",
	"Ctrl + wheel: 1 frame · Shift + wheel: 2 s",
	"Click or drag on the waveform: seek",
	"Wheel on the video: volume",
	"Drag on the video: move the window",
	"Ctrl+N: new note at the current time",
	"Ctrl+Enter or Esc in a note: back to the player",
	"Enter in a note: new line",
	"Esc: quit",
	"Drop a video on the player: load and play",
])
WHEEL_STEP = 0.25
ARROW_STEP = 5.0


def force_fg(handle):
	if os.name != "nt":
		return
	from ctypes import wintypes
	u, k = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
	u.GetForegroundWindow.restype = wintypes.HWND
	u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.c_void_p]
	u.GetWindowThreadProcessId.restype = wintypes.DWORD
	u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
	u.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint]
	for fn in (u.SetForegroundWindow, u.BringWindowToTop, u.IsIconic):
		fn.argtypes = [wintypes.HWND]
	u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
	k.GetCurrentThreadId.restype = wintypes.DWORD
	fg = u.GetForegroundWindow()
	if fg == handle:
		return
	a, b = u.GetWindowThreadProcessId(fg, None), k.GetCurrentThreadId()
	attached = bool(fg) and a != b and u.AttachThreadInput(a, b, True)
	u.ShowWindow(handle, 9 if u.IsIconic(handle) else 5)
	u.SetWindowPos(handle, wintypes.HWND(-1), 0, 0, 0, 0, 0x43)
	u.SetWindowPos(handle, wintypes.HWND(-2), 0, 0, 0, 0, 0x43)
	u.BringWindowToTop(handle)
	u.SetForegroundWindow(handle)
	if u.GetForegroundWindow() != handle:
		u.keybd_event(0x12, 0, 0, 0)
		u.keybd_event(0x12, 0, 2, 0)
		u.SetForegroundWindow(handle)
	if attached:
		u.AttachThreadInput(a, b, False)


class Keys(QObject):
	def __init__(self, window):
		super().__init__(window)
		self.window = window

	def eventFilter(self, obj, event):
		if event.type() != QEvent.KeyPress or not self.window.isActiveWindow():
			return False
		focus = QApplication.focusWidget()
		if event.key() == Qt.Key_N and event.modifiers() & Qt.ControlModifier:
			self.window.add_note()
			return True
		if isinstance(focus, QPlainTextEdit):
			commit = event.key() in (Qt.Key_Return, Qt.Key_Enter) and event.modifiers() & Qt.ControlModifier
			if commit or event.key() == Qt.Key_Escape:
				focus.clearFocus()
				return True
			return False
		return self.window.key(event.key())


class Window(QMainWindow):
	def __init__(self):
		super().__init__()
		self.setWindowTitle("Review player")
		self.resize(1500, 820)
		self.path, self.info = None, None
		self.pos = 0.0
		self.size = (1280, 720)
		self.rows = []
		self.engine = Engine(self)
		self.engine.frame.connect(self.on_frame)
		self.engine.ended.connect(self.on_end)
		self.grabber = Grabber(self)
		self.grabber.grabbed.connect(self.on_grab)
		self.peaks = Peaks(self)
		self.peaks.ready.connect(self.on_peaks)
		self.restart = QTimer(self)
		self.restart.setSingleShot(True)
		self.restart.setInterval(110)
		self.restart.timeout.connect(self.resume)
		self.fade = QTimer(self)
		self.fade.setSingleShot(True)
		self.fade.timeout.connect(self.clear_overlay)
		self.pending = False
		self.store = None
		self.version = None
		self.save_timer = QTimer(self)
		self.save_timer.setSingleShot(True)
		self.save_timer.setInterval(250)
		self.save_timer.timeout.connect(self.flush)
		self.poll = QTimer(self)
		self.poll.setInterval(700)
		self.poll.timeout.connect(self.sync)
		self.poll.start()
		self.layout_timer = QTimer(self)
		self.layout_timer.setSingleShot(True)
		self.layout_timer.setInterval(400)
		self.layout_timer.timeout.connect(self.save_layout)
		self.ready = False
		self.build()
		self.load_layout()
		self.ready = True

	def build(self):
		self.view = VideoView()
		self.view.clicked.connect(self.toggle)
		self.view.wheel.connect(self.on_volume)
		self.view.dropped.connect(self.load)
		self.wave = Waveform()
		self.wave.seek.connect(self.seek)
		self.wave.wheel.connect(self.on_scroll)
		self.status = QLabel("Drop a video on the player")
		bar = QHBoxLayout()
		for label, tip, fn in (("⏮", "Play from the start (X)", self.play_start), ("◀ frame", "Previous frame", lambda: self.step(-1)), ("▶ / ⏸", "Play / pause (Space, click on the video)", self.toggle), ("⏸", "Pause (C)", self.pause), ("⏹", "Stop and go back to the start", self.stop), ("frame ▶", "Next frame", lambda: self.step(1))):
			b = QPushButton(label)
			b.setToolTip(tip)
			b.setFocusPolicy(Qt.NoFocus)
			b.clicked.connect(fn)
			bar.addWidget(b)
		bar.addWidget(self.status, 1)
		help_mark = QLabel("?")
		help_mark.setToolTip(HELP)
		help_mark.setAlignment(Qt.AlignCenter)
		help_mark.setFixedSize(24, 24)
		help_mark.setStyleSheet("border:1px solid #707070;border-radius:12px;color:#d0d0d0;font-weight:bold")
		bar.addWidget(help_mark)
		self.left_split = QSplitter(Qt.Vertical)
		self.left_split.addWidget(self.view)
		self.left_split.addWidget(self.wave)
		self.left_split.setStretchFactor(0, 1)
		self.left_split.setSizes([560, 170])
		left = QVBoxLayout()
		left.setContentsMargins(0, 0, 0, 0)
		left.addWidget(self.left_split, 1)
		left.addLayout(bar)
		add = QPushButton("＋ Add note at current time (Ctrl+N)")
		add.setFocusPolicy(Qt.NoFocus)
		add.clicked.connect(lambda: self.add_note())
		copy = QPushButton("Copy all notes")
		copy.setFocusPolicy(Qt.NoFocus)
		copy.clicked.connect(self.copy_all)
		self.copied = QLabel("")
		self.copied.setStyleSheet("color:#80d080")
		self.list_box = QVBoxLayout()
		self.list_box.setContentsMargins(6, 6, 6, 6)
		self.list_box.setSpacing(2)
		self.list_box.addStretch(1)
		holder = QWidget()
		holder.setLayout(self.list_box)
		scroll = QScrollArea()
		scroll.setWidgetResizable(True)
		scroll.setWidget(holder)
		self.scroll = scroll
		self.versions = QComboBox()
		self.versions.setToolTip("Notes version for this video")
		self.versions.setFocusPolicy(Qt.NoFocus)
		self.versions.activated.connect(lambda _: self.set_version(self.versions.currentText()))
		fresh = QPushButton("New version")
		fresh.setToolTip("Start an empty set of notes for this video; older versions stay available")
		fresh.setFocusPolicy(Qt.NoFocus)
		fresh.clicked.connect(self.new_version)
		top = QHBoxLayout()
		top.addWidget(QLabel("Notes"))
		top.addWidget(self.versions, 1)
		top.addWidget(fresh)
		right = QVBoxLayout()
		right.addLayout(top)
		right.addWidget(add)
		right.addWidget(scroll, 1)
		right.addWidget(copy)
		right.addWidget(self.copied)
		side = QWidget()
		side.setLayout(right)
		side.setMinimumWidth(260)
		main = QWidget()
		main.setLayout(left)
		self.main_split = QSplitter(Qt.Horizontal)
		self.main_split.addWidget(main)
		self.main_split.addWidget(side)
		self.main_split.setStretchFactor(0, 1)
		self.main_split.setSizes([1030, 470])
		for sp in (self.main_split, self.left_split):
			sp.setChildrenCollapsible(False)
			sp.splitterMoved.connect(lambda *_: self.layout_timer.start())
		self.setCentralWidget(self.main_split)

	def load_layout(self):
		cfg = read_json(CONFIG, {})
		g = cfg.get("geometry")
		if g and len(g) == 4:
			self.setGeometry(*g)
		for key, sp in (("main", self.main_split), ("left", self.left_split)):
			if cfg.get(key):
				sp.setSizes(cfg[key])
		self.engine.set_volume(float(cfg.get("volume", 1.0)))

	def save_layout(self):
		if not self.ready:
			return
		g = self.geometry()
		cfg = read_json(CONFIG, {})
		cfg.update({"geometry": [g.x(), g.y(), g.width(), g.height()], "main": self.main_split.sizes(), "left": self.left_split.sizes(), "volume": self.engine.volume})
		if self.path:
			cfg["last"] = self.path
			cfg.setdefault("versions", {})[self.video_key()] = self.version
		write_json(CONFIG, cfg)

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self.layout_timer.start()

	def moveEvent(self, event):
		super().moveEvent(event)
		self.layout_timer.start()

	def video_key(self):
		return os.path.splitext(os.path.basename(self.path))[0]

	def folder(self):
		return os.path.join(REVIEWS, self.video_key()).replace("\\", "/")

	def store_path(self, version=None):
		return os.path.join(self.folder(), (version or self.version) + ".json").replace("\\", "/")

	def list_versions(self):
		if not os.path.isdir(self.folder()):
			return []
		names = [f[:-5] for f in os.listdir(self.folder()) if f.endswith(".json")]
		return sorted(names, key=lambda n: (len(n), n))

	def pick_version(self):
		names = self.list_versions()
		if not names:
			write_json(self.store_path("v1"), [])
			names = ["v1"]
		last = read_json(CONFIG, {}).get("versions", {}).get(self.video_key())
		return last if last in names else names[-1]

	def set_version(self, name):
		self.flush()
		self.version = name
		self.store = NoteStore(self.store_path())
		self.versions.blockSignals(True)
		self.versions.clear()
		self.versions.addItems(self.list_versions())
		self.versions.setCurrentText(name)
		self.versions.blockSignals(False)
		self.rebuild_rows()
		self.layout_timer.start()

	def new_version(self):
		if not self.path:
			return
		self.flush()
		names = self.list_versions()
		n = len(names) + 1
		while "v%d" % n in names:
			n += 1
		write_json(self.store_path("v%d" % n), [])
		self.set_version("v%d" % n)

	def export(self):
		if not self.store:
			return
		with open(self.store_path()[:-5] + ".txt", "w", encoding="utf-8") as f:
			f.write(self.notes_text() + "\n")

	def notes_text(self):
		head = "Video: %s (%.3f fps) - notes %s" % (os.path.basename(self.path), self.info["fps"], self.version)
		return "\n".join([head] + [r.line() for r in self.rows])

	def load(self, path, autoplay=True):
		self.engine.stop()
		info = probe(path)
		if not info["width"]:
			self.status.setText("Cannot read " + os.path.basename(path))
			return
		self.path, self.info = path, info
		self.size = fit_size(info["width"], info["height"])
		self.pos = 0.0
		self.setWindowTitle("Review player — " + os.path.basename(path))
		self.wave.set_data(None, info["duration"])
		self.peaks.load(path)
		self.flush()
		self.store = None
		self.set_version(self.pick_version())
		self.refresh_marks()
		force_fg(int(self.winId()))
		if autoplay:
			self.play()
		else:
			self.pause()

	def on_peaks(self, peaks, path):
		if path == self.path:
			self.wave.set_data(peaks, self.info["duration"])

	def frame_of(self, t):
		return int(round(t * self.info["fps"]))

	def show_pos(self):
		self.wave.set_pos(self.pos)
		if self.info:
			self.status.setText("%s / %s   frame %d   %s" % (stamp(self.pos), stamp(self.info["duration"]), self.frame_of(self.pos), "▶" if self.engine.playing or self.pending else "⏸"))

	def on_frame(self, image, t):
		if self.pending:
			return
		self.pos = t
		self.view.show_frame(image)
		self.show_pos()

	def on_grab(self, image, t):
		if self.engine.playing or self.pending:
			return
		self.view.show_frame(image)

	def on_end(self):
		self.show_pos()

	def play(self):
		if not self.path:
			return
		if self.pos >= self.info["duration"] - 0.05:
			self.pos = 0.0
		self.pending = False
		self.restart.stop()
		self.engine.play(self.path, self.info, self.pos, *self.size)
		self.show_pos()

	def resume(self):
		self.pending = False
		self.engine.play(self.path, self.info, self.pos, *self.size)

	def pause(self):
		if not self.path:
			return
		self.restart.stop()
		self.pending = False
		self.engine.stop()
		self.grabber.request(self.path, self.pos, *self.size)
		self.show_pos()

	def toggle(self):
		if self.engine.playing or self.pending:
			self.pause()
		else:
			self.play()

	def stop(self):
		self.pos = 0.0
		self.pause()

	def play_start(self):
		self.pos = 0.0
		self.play()

	def seek(self, t):
		if not self.path:
			return
		self.pos = max(0.0, min(self.info["duration"] - 1.0 / self.info["fps"], t))
		if self.engine.playing or self.pending:
			self.engine.stop()
			self.pending = True
			self.restart.start()
		self.grabber.request(self.path, self.pos, *self.size)
		self.show_pos()

	def step(self, frames):
		if not self.path:
			return
		self.restart.stop()
		self.pending = False
		self.engine.stop()
		self.pos = max(0.0, (self.frame_of(self.pos) + frames) / self.info["fps"])
		self.pos = min(self.pos, self.info["duration"] - 1.0 / self.info["fps"])
		self.grabber.request(self.path, self.pos, *self.size)
		self.show_pos()

	def on_scroll(self, delta, modifiers):
		if not self.path or not delta:
			return
		step = WHEEL_STEP
		if modifiers & Qt.ControlModifier:
			step = 1.0 / self.info["fps"]
		elif modifiers & Qt.ShiftModifier:
			step = 2.0
		self.seek(self.pos + (step if delta < 0 else -step) * max(1, abs(delta) // 120))

	def on_volume(self, delta):
		value = max(0.0, min(1.0, self.engine.volume + (0.05 if delta > 0 else -0.05)))
		self.engine.set_volume(value)
		self.view.overlay = "Volume %d%%" % round(value * 100)
		self.view.update()
		self.fade.start(900)
		self.layout_timer.start()

	def clear_overlay(self):
		self.view.overlay = ""
		self.view.update()

	def key(self, key):
		if key == Qt.Key_Space:
			self.toggle()
		elif key == Qt.Key_X:
			self.play_start()
		elif key == Qt.Key_C:
			self.pause()
		elif key == Qt.Key_Left:
			self.seek(self.pos - ARROW_STEP)
		elif key == Qt.Key_Right:
			self.seek(self.pos + ARROW_STEP)
		elif key == Qt.Key_Escape:
			self.close()
		else:
			return False
		return True

	def add_note(self, t=None, check=False, text=""):
		if not self.store:
			return
		t = self.pos if t is None else t
		note = new_note(t, self.frame_of(t), check, text)
		self.store.put(note)
		self.store.push()
		self.rebuild_rows()
		row = next(r for r in self.rows if r.key == note["id"])
		row.note.setFocus()
		self.reveal(row)

	def reveal(self, row):
		QTimer.singleShot(40, lambda: row in self.rows and self.scroll.ensureWidgetVisible(row, 0, 24))

	def make_row(self, note):
		row = NoteRow(note)
		row.changed.connect(self.row_changed)
		row.removed.connect(self.drop_row)
		row.jump.connect(self.jump)
		return row

	def rebuild_rows(self):
		notes = self.store.alive() if self.store else []
		have = {r.key: r for r in self.rows}
		wanted = [n["id"] for n in notes]
		for row in list(self.rows):
			if row.key not in wanted:
				self.rows.remove(row)
				self.list_box.removeWidget(row)
				row.deleteLater()
		ordered = []
		for n in notes:
			row = have.get(n["id"])
			if row is None:
				row = self.make_row(n)
			else:
				row.apply(n)
			ordered.append(row)
		for i, row in enumerate(ordered):
			if self.list_box.indexOf(row) != i:
				self.list_box.insertWidget(i, row)
		self.rows = ordered
		self.refresh_marks()
		self.export()

	def row_changed(self, row):
		if not self.store:
			return
		self.store.put(row.data())
		self.save_timer.start()
		if row.note.hasFocus():
			self.reveal(row)

	def jump(self, t):
		self.restart.stop()
		self.pending = False
		self.engine.stop()
		self.pos = t
		self.grabber.request(self.path, self.pos, *self.size)
		self.show_pos()

	def drop_row(self, row):
		self.store.remove(row.key)
		self.store.push()
		self.rebuild_rows()

	def refresh_marks(self):
		self.wave.set_marks([r.t for r in self.rows])

	def flush(self):
		self.save_timer.stop()
		if not self.store:
			return
		if self.store.push():
			self.rebuild_rows()
		self.export()

	def sync(self):
		if not self.store or self.save_timer.isActive():
			return
		if self.store.pull():
			self.rebuild_rows()

	def copy_all(self):
		if not self.path:
			return
		self.flush()
		QApplication.clipboard().setText(self.notes_text())
		self.copied.setText("Copied %d note(s)" % len(self.rows))
		QTimer.singleShot(2500, lambda: self.copied.setText(""))

	def closeEvent(self, event):
		self.flush()
		self.save_layout()
		self.engine.stop()
		super().closeEvent(event)


def dark(app):
	app.setStyle("Fusion")
	p = QPalette()
	for role, c in ((QPalette.Window, "#2b2b2b"), (QPalette.WindowText, "#e0e0e0"), (QPalette.Base, "#1e1e1e"), (QPalette.AlternateBase, "#262626"), (QPalette.ToolTipBase, "#2b2b2b"), (QPalette.ToolTipText, "#e0e0e0"), (QPalette.Text, "#e0e0e0"), (QPalette.Button, "#3a3a3a"), (QPalette.ButtonText, "#e0e0e0"), (QPalette.Highlight, "#3d6fb4"), (QPalette.HighlightedText, "#ffffff"), (QPalette.PlaceholderText, "#808080")):
		p.setColor(role, QColor(c))
	app.setPalette(p)


def main():
	app = QApplication(sys.argv)
	dark(app)
	window = Window()
	app.installEventFilter(Keys(window))
	window.show()
	force_fg(int(window.winId()))
	if len(sys.argv) > 1 and os.path.exists(sys.argv[1]):
		window.load(sys.argv[1].replace("\\", "/"))
	else:
		last = read_json(CONFIG, {}).get("last")
		if last and os.path.exists(last):
			window.load(last, autoplay=False)
	sys.exit(app.exec())


if __name__ == "__main__":
	main()
