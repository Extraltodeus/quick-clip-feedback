import json
import os
import time
import uuid


def read_json(path, default):
	try:
		with open(path, encoding="utf-8") as f:
			return json.load(f)
	except (OSError, ValueError):
		return default


def write_json(path, data):
	os.makedirs(os.path.dirname(path), exist_ok=True)
	tmp = "%s.%d.tmp" % (path, os.getpid())
	with open(tmp, "w", encoding="utf-8") as f:
		json.dump(data, f, ensure_ascii=False, indent="\t")
	for _ in range(20):
		try:
			os.replace(tmp, path)
			return True
		except OSError:
			time.sleep(0.03)
	return False


def new_note(t, frame, check=False, text=""):
	return {"id": uuid.uuid4().hex, "t": t, "frame": frame, "check": check, "text": text, "mod": time.time(), "deleted": False}


class NoteStore:
	def __init__(self, path):
		self.path = path
		self.notes = {}
		self.stamp = None
		self.pull()

	def disk(self):
		raw = read_json(self.path, [])
		out = {}
		for i, n in enumerate(raw if isinstance(raw, list) else []):
			if not isinstance(n, dict) or "t" not in n:
				continue
			n.setdefault("id", "legacy%04d" % i)
			n.setdefault("mod", 0.0)
			n.setdefault("deleted", False)
			n.setdefault("check", False)
			n.setdefault("text", "")
			n.setdefault("frame", 0)
			out[n["id"]] = n
		return out

	def merge(self, other):
		changed = False
		for key, n in other.items():
			cur = self.notes.get(key)
			if cur is None or n["mod"] > cur["mod"]:
				self.notes[key] = n
				changed = True
		return changed

	def file_stamp(self):
		try:
			st = os.stat(self.path)
			return (st.st_mtime_ns, st.st_size)
		except OSError:
			return None

	def pull(self):
		stamp = self.file_stamp()
		if stamp == self.stamp:
			return False
		disk = self.disk()
		changed = self.merge(disk)
		self.stamp = stamp
		if any(k not in disk or disk[k]["mod"] < n["mod"] for k, n in self.notes.items()):
			self.push()
		return changed

	def push(self):
		changed = self.merge(self.disk())
		data = sorted(self.notes.values(), key=lambda n: (n["t"], n["id"]))
		if write_json(self.path, data):
			self.stamp = self.file_stamp()
		return changed

	def put(self, note):
		note["mod"] = time.time()
		self.notes[note["id"]] = note

	def remove(self, key):
		if key in self.notes:
			self.notes[key]["deleted"] = True
			self.notes[key]["mod"] = time.time()

	def alive(self):
		return sorted((n for n in self.notes.values() if not n["deleted"]), key=lambda n: (n["t"], n["id"]))
