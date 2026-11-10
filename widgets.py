from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen
from PySide6.QtWidgets import QCheckBox, QHBoxLayout, QPlainTextEdit, QPushButton, QSizePolicy, QVBoxLayout, QWidget

EXTS = (".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".wmv", ".mpg", ".mpeg", ".ts")


def stamp(t):
	t = max(0.0, t)
	return "%d:%05.2f" % (int(t // 60), t - 60 * int(t // 60))


def dropped(mime):
	for url in mime.urls():
		path = url.toLocalFile()
		if path.lower().endswith(EXTS):
			return path.replace("\\", "/")
	return None


class VideoView(QWidget):
	clicked = Signal()
	wheel = Signal(int)
	dropped = Signal(str)

	def __init__(self):
		super().__init__()
		self.image = None
		self.press = None
		self.overlay = ""
		self.setAcceptDrops(True)
		self.setMinimumSize(640, 280)

	def show_frame(self, array):
		h, w, _ = array.shape
		self.keep = array
		self.image = QImage(array.data, w, h, w * 3, QImage.Format_RGB888)
		self.update()

	def paintEvent(self, event):
		p = QPainter(self)
		p.fillRect(self.rect(), QColor("#101010"))
		if self.image is None:
			p.setPen(QColor("#808080"))
			p.drawText(self.rect(), Qt.AlignCenter, "Drop a video here")
		else:
			iw, ih = self.image.width(), self.image.height()
			scale = min(self.width() / iw, self.height() / ih)
			w, h = iw * scale, ih * scale
			p.setRenderHint(QPainter.SmoothPixmapTransform)
			p.drawImage(QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), self.image)
		if self.overlay:
			p.setPen(QColor("#ffd24a"))
			f = p.font()
			f.setPointSize(16)
			f.setBold(True)
			p.setFont(f)
			p.drawText(self.rect().adjusted(16, 12, -16, -12), Qt.AlignTop | Qt.AlignLeft, self.overlay)

	def mousePressEvent(self, event):
		if event.button() == Qt.LeftButton:
			self.press = event.position()

	def mouseMoveEvent(self, event):
		if self.press is None or not event.buttons() & Qt.LeftButton:
			return
		if (event.position() - self.press).manhattanLength() < 8:
			return
		self.press = None
		handle = self.window().windowHandle()
		if handle:
			handle.startSystemMove()

	def mouseReleaseEvent(self, event):
		if event.button() == Qt.LeftButton and self.press is not None:
			self.press = None
			self.clicked.emit()

	def wheelEvent(self, event):
		self.wheel.emit(event.angleDelta().y())
		event.accept()

	def dragEnterEvent(self, event):
		if dropped(event.mimeData()):
			event.acceptProposedAction()

	def dragMoveEvent(self, event):
		event.acceptProposedAction()

	def dropEvent(self, event):
		path = dropped(event.mimeData())
		if path:
			self.dropped.emit(path)


class Waveform(QWidget):
	seek = Signal(float)
	wheel = Signal(int, object)

	def __init__(self):
		super().__init__()
		self.peaks = None
		self.duration = 0.0
		self.pos = 0.0
		self.marks = []
		self.setMinimumHeight(70)
		self.setMouseTracking(True)
		self.hover = None

	def set_data(self, peaks, duration):
		self.peaks, self.duration = peaks, duration
		self.update()

	def set_pos(self, t):
		self.pos = t
		self.update()

	def set_marks(self, marks):
		self.marks = marks
		self.update()

	def x_of(self, t):
		return 0 if self.duration <= 0 else t / self.duration * self.width()

	def paintEvent(self, event):
		p = QPainter(self)
		w, h = self.width(), self.height()
		p.fillRect(self.rect(), QColor("#181818"))
		mid = h / 2
		if self.peaks is not None and w > 0:
			p.setPen(QPen(QColor("#4f8fd8"), 1))
			n = len(self.peaks)
			for x in range(w):
				a, b = x * n // w, max(x * n // w + 1, (x + 1) * n // w)
				v = float(self.peaks[a:b].max()) * (mid - 6)
				p.drawLine(x, int(mid - v), x, int(mid + v))
		elif self.duration > 0:
			p.setPen(QColor("#606060"))
			p.drawText(self.rect(), Qt.AlignCenter, "No audio / loading waveform")
		p.setPen(QPen(QColor("#ffb020"), 2))
		for t in self.marks:
			x = int(self.x_of(t))
			p.drawLine(x, 0, x, 14)
		if self.hover is not None and self.duration > 0:
			p.setPen(QPen(QColor("#808080"), 1, Qt.DashLine))
			p.drawLine(self.hover, 0, self.hover, h)
			p.setPen(QColor("#c0c0c0"))
			p.drawText(min(self.hover + 6, w - 70), h - 8, stamp(self.hover / max(1, w) * self.duration))
		p.setPen(QPen(QColor("#ff4040"), 2))
		x = int(self.x_of(self.pos))
		p.drawLine(x, 0, x, h)
		p.setPen(QColor("#f0f0f0"))
		p.drawText(min(x + 6, w - 70), 16, stamp(self.pos))

	def time_at(self, x):
		return max(0.0, min(self.duration, x / max(1, self.width()) * self.duration))

	def mousePressEvent(self, event):
		if event.button() == Qt.LeftButton and self.duration > 0:
			self.seek.emit(self.time_at(event.position().x()))

	def mouseMoveEvent(self, event):
		self.hover = int(event.position().x())
		if event.buttons() & Qt.LeftButton and self.duration > 0:
			self.seek.emit(self.time_at(event.position().x()))
		self.update()

	def leaveEvent(self, event):
		self.hover = None
		self.update()

	def wheelEvent(self, event):
		self.wheel.emit(event.angleDelta().y(), event.modifiers())
		event.accept()


class AutoText(QPlainTextEdit):
	def __init__(self, text=""):
		super().__init__()
		self.setPlaceholderText("note")
		self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
		self.setTabChangesFocus(True)
		self.setPlainText(text)
		self.textChanged.connect(self.fit)
		self.fit()

	def fit(self):
		lines = max(1, min(14, int(self.document().size().height())))
		self.setFixedHeight(lines * self.fontMetrics().lineSpacing() + 14)

	def resizeEvent(self, event):
		super().resizeEvent(event)
		self.fit()

	def wheelEvent(self, event):
		event.ignore()


class NoteRow(QWidget):
	changed = Signal(object)
	removed = Signal(object)
	jump = Signal(float)

	def __init__(self, note):
		super().__init__()
		self.key = note["id"]
		self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
		self.t, self.frame = note["t"], note["frame"]
		self.quiet = False
		head = QHBoxLayout()
		head.setContentsMargins(0, 0, 0, 0)
		head.setSpacing(4)
		self.time = QPushButton(stamp(self.t))
		self.time.setFixedWidth(74)
		self.time.setFocusPolicy(Qt.NoFocus)
		self.time.setToolTip("Jump to %s (frame %d)" % (stamp(self.t), self.frame))
		self.time.clicked.connect(lambda: self.jump.emit(self.t))
		self.check = QCheckBox("check frame")
		self.check.setFocusPolicy(Qt.NoFocus)
		self.check.setToolTip("Ask Claude to look at the exact frame at this timestamp")
		self.check.toggled.connect(self.emit_change)
		self.kill = QPushButton("×")
		self.kill.setFixedWidth(26)
		self.kill.setFocusPolicy(Qt.NoFocus)
		self.kill.setToolTip("Remove this note")
		self.kill.clicked.connect(lambda: self.removed.emit(self))
		head.addWidget(self.time)
		head.addWidget(self.check)
		head.addStretch(1)
		head.addWidget(self.kill)
		self.note = AutoText()
		self.note.textChanged.connect(self.emit_change)
		box = QVBoxLayout(self)
		box.setContentsMargins(0, 2, 0, 6)
		box.setSpacing(3)
		box.addLayout(head)
		box.addWidget(self.note)
		self.apply(note)

	def emit_change(self, *_):
		self.note.setToolTip(self.note.toPlainText())
		if not self.quiet:
			self.changed.emit(self)

	def apply(self, note):
		self.quiet = True
		if self.check.isChecked() != bool(note["check"]):
			self.check.setChecked(bool(note["check"]))
		if self.note.toPlainText() != note["text"]:
			cursor = self.note.textCursor().position()
			self.note.setPlainText(note["text"])
			c = self.note.textCursor()
			c.setPosition(min(cursor, len(note["text"])))
			self.note.setTextCursor(c)
		self.note.setToolTip(note["text"])
		self.quiet = False

	def data(self):
		return {"id": self.key, "t": self.t, "frame": self.frame, "check": self.check.isChecked(), "text": self.note.toPlainText(), "deleted": False}

	def line(self):
		head = "%s (frame %d)" % (stamp(self.t), self.frame) + (" [CHECK FRAME]" if self.check.isChecked() else "")
		text = self.note.toPlainText().strip()
		if not text:
			return head
		lines = text.split(chr(10))
		return chr(10).join([head + " " + lines[0]] + ["    " + l for l in lines[1:]])
