from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import random
import struct
import subprocess
import sys
import tempfile
from typing import Callable, Iterable, Sequence

import numpy as np
import parselmouth
from PySide6.QtMultimedia import QSoundEffect
from PySide6.QtCore import QSettings, QThread, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QCloseEvent, QDragEnterEvent, QDropEvent, QFont, QFontDatabase, QIcon, QKeyEvent, QPainter, QPalette, QColor, QPen, QPixmap, QTextCursor
from PySide6.QtWidgets import (
	QApplication,
	QCheckBox,
	QComboBox,
	QDialog,
	QDoubleSpinBox,
	QFileDialog,
	QFormLayout,
	QGridLayout,
	QGroupBox,
	QHBoxLayout,
	QLabel,
	QLineEdit,
	QMainWindow,
	QMessageBox,
	QProgressBar,
	QPushButton,
	QSpinBox,
	QSlider,
	QStyle,
	QTabWidget,
	QTextEdit,
	QToolButton,
	QVBoxLayout,
	QWidget,
)


NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
OCTAVES = [str(value) for value in range(0, 9)]
SAMPLE_RATES = ["48000", "44100"]
ORDER_MODES = ["sequential", "shuffle", "random"]
AUDIO_STYLES = {
	"Current": "Hybrid pitch - current ChromaKit behavior",
	"OG App": "Original app pitch - Praat formula at 48 kHz",
	"Praat": "Praat pitch - no low-note FFT fallback",
	"Formant corrected": "Praat Change gender - preserve formants while shifting pitch",
	"Vocal strain": "Formant-aware, restrained vocal effort for upward notes",
	"Bright belt": "Praat pitch with bright, supported high-note presence",
	"Yell / shout": "Natural-formant high-note shout that grows with pitch",
	"Scream / belt": "Natural-formant scream and belt for high notes",
	"Rasp": "Clean, human-style vocal rasp without synthetic flutter",
}

# Expressive styles start building effort only after a moderate upward shift.  This
# leaves low notes clean and makes the effect follow the actual interval rather
# than an arbitrary absolute octave.
STRAIN_START_SEMITONES = 4.0
STRAIN_FULL_SEMITONES = 19.0
DEFAULT_SAMPLE_RATE = 48000
LOW_NOTE_FFT_THRESHOLD = 60.0
BRAND_CHROMA_COLOR = "#0D1524"
BRAND_KIT_COLOR = "#0A8CE6"
BRAND_CHROMA_DARK_COLOR = "#F7FBFF"


def asset_path(name: str) -> Path:
	candidates = []
	bundle_root = getattr(sys, "_MEIPASS", None)
	if bundle_root:
		candidates.append(Path(bundle_root) / "assets" / name)
	candidates.extend(
		(
			Path(__file__).resolve().parents[2] / "assets" / name,
			Path.cwd() / "assets" / name,
		)
	)
	for candidate in candidates:
		if candidate.exists():
			return candidate
	return candidates[-1]


def load_brand_font() -> None:
	for name in ("Sora-Bold.ttf", "Sora-Variable.ttf"):
		font_path = asset_path(name)
		if font_path.exists():
			QFontDatabase.addApplicationFont(str(font_path))


@dataclass(frozen=True)
class GenerationSettings:
	sample_path: Path
	start_note_index: int
	start_octave: int
	semitones: int
	gap_seconds: float
	pitch_samples: bool
	dump_samples: bool
	order_mode: str
	audio_style: str
	trim_silence: bool
	normalize: bool
	fade_ms: int
	fixed_note_length: float
	output_sample_rate: int
	slicex_markers: bool


@dataclass(frozen=True)
class PrepareSettings:
	source_paths: tuple[Path, ...]
	output_dir: Path
	threshold_db: float
	min_region_ms: int
	min_silence_ms: int
	padding_ms: int
	output_sample_rate: int


@dataclass(frozen=True)
class LoopSettings:
	source_paths: tuple[Path, ...]
	output_dir: Path
	crossfade_ms: int
	trim_silence: bool
	output_sample_rate: int
	loop_start_ms: float
	loop_end_ms: float
	render_length_seconds: float
	render_mode: str


@dataclass(frozen=True)
class SliceMarker:
	offset: int
	label: str


class WaveformView(QWidget):
	"""A lightweight editable waveform with draggable loop-start/end markers."""
	range_changed = Signal(float, float)

	def __init__(self) -> None:
		super().__init__()
		self.setMinimumHeight(170)
		self.values = np.empty(0, dtype=np.float64)
		self.sample_rate = DEFAULT_SAMPLE_RATE
		self.start_frame = 0
		self.end_frame = 0
		self._drag_marker: str | None = None

	def set_sound(self, sound: parselmouth.Sound) -> None:
		self.values = np.asarray(sound.values[0], dtype=np.float64)
		self.sample_rate = int(sound.sampling_frequency)
		self.start_frame = 0
		self.end_frame = len(self.values)
		self.update()

	def set_range_ms(self, start_ms: float, end_ms: float, emit: bool = False) -> None:
		frames = len(self.values)
		if not frames:
			return
		self.start_frame = int(np.clip(round(start_ms * self.sample_rate / 1000.0), 0, frames - 1))
		self.end_frame = int(np.clip(round(end_ms * self.sample_rate / 1000.0), self.start_frame + 1, frames))
		self.update()
		if emit:
			self.range_changed.emit(self.start_frame * 1000.0 / self.sample_rate, self.end_frame * 1000.0 / self.sample_rate)

	def _frame_at_x(self, x: float) -> int:
		if not len(self.values):
			return 0
		return int(np.clip(round(x / max(1, self.width() - 1) * len(self.values)), 0, len(self.values)))

	def paintEvent(self, _event: object) -> None:
		painter = QPainter(self)
		painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
		if not len(self.values):
			painter.drawText(self.rect(), Qt.AlignCenter, "Select a WAV sample to view and adjust its loop region")
			return
		width, height = max(1, self.width()), max(1, self.height())
		mid = height / 2
		painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Mid), 1))
		painter.drawLine(0, int(mid), width, int(mid))
		painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Highlight), 1))
		for x in range(width):
			left = int(x * len(self.values) / width)
			right = max(left + 1, int((x + 1) * len(self.values) / width))
			chunk = self.values[left:right]
			low = mid - float(np.min(chunk)) * (height * 0.43)
			high = mid - float(np.max(chunk)) * (height * 0.43)
			painter.drawLine(x, int(low), x, int(high))
		for frame, color, text in ((self.start_frame, "#35c759", "Start"), (self.end_frame, "#ff9f0a", "End")):
			x = int(frame / len(self.values) * width)
			painter.setPen(QPen(QColor(color), 2))
			painter.drawLine(x, 0, x, height)
			painter.drawText(x + 3, 15, text)

	def mousePressEvent(self, event: object) -> None:
		if not len(self.values):
			return
		frame = self._frame_at_x(event.position().x())
		self._drag_marker = "start" if abs(frame - self.start_frame) <= abs(frame - self.end_frame) else "end"
		self.mouseMoveEvent(event)

	def mouseMoveEvent(self, event: object) -> None:
		if self._drag_marker is None or not len(self.values):
			return
		frame = self._frame_at_x(event.position().x())
		if self._drag_marker == "start":
			self.start_frame = min(frame, self.end_frame - 1)
		else:
			self.end_frame = max(frame, self.start_frame + 1)
		self.update()
		self.range_changed.emit(self.start_frame * 1000.0 / self.sample_rate, self.end_frame * 1000.0 / self.sample_rate)

	def mouseReleaseEvent(self, _event: object) -> None:
		self._drag_marker = None


@dataclass(frozen=True)
class PianoRollNote:
	step: int
	pitch: int
	velocity: int = 100


class PianoRollView(QWidget):
	"""A compact step piano-roll for auditioning an exported chromatic."""
	notes_changed = Signal()

	def __init__(self, steps: int = 16, pitches: int = 24) -> None:
		super().__init__()
		self.steps = steps
		self.pitches = pitches
		self.notes: dict[tuple[int, int], int] = {}
		self.playhead = -1
		self.setMinimumHeight(260)
		self.setMinimumWidth(580)

	def set_playhead(self, step: int) -> None:
		self.playhead = step
		self.update()

	def clear_notes(self) -> None:
		self.notes.clear()
		self.update()
		self.notes_changed.emit()

	def add_note(self, step: int, pitch: int, velocity: int) -> None:
		self.notes[(max(0, min(self.steps - 1, step)), max(0, min(self.pitches - 1, pitch)))] = velocity
		self.update()
		self.notes_changed.emit()

	def notes_at(self, step: int) -> list[PianoRollNote]:
		return [PianoRollNote(note_step, pitch, velocity) for (note_step, pitch), velocity in self.notes.items() if note_step == step]

	def _cell_at(self, x: float, y: float) -> tuple[int, int]:
		step = int(np.clip(x / max(1, self.width()) * self.steps, 0, self.steps - 1))
		# Lowest note is drawn at the bottom, like a conventional piano roll.
		pitch = self.pitches - 1 - int(np.clip(y / max(1, self.height()) * self.pitches, 0, self.pitches - 1))
		return step, pitch

	def mousePressEvent(self, event: object) -> None:
		step, pitch = self._cell_at(event.position().x(), event.position().y())
		key = (step, pitch)
		if key in self.notes:
			del self.notes[key]
		else:
			self.notes[key] = 100
		self.update()
		self.notes_changed.emit()

	def paintEvent(self, _event: object) -> None:
		painter = QPainter(self)
		painter.fillRect(self.rect(), self.palette().color(QPalette.ColorRole.Base))
		width, height = max(1, self.width()), max(1, self.height())
		cell_w, cell_h = width / self.steps, height / self.pitches
		grid_pen = QPen(self.palette().color(QPalette.ColorRole.Mid), 1)
		painter.setPen(grid_pen)
		for step in range(self.steps + 1):
			x = int(step * cell_w)
			painter.drawLine(x, 0, x, height)
		for pitch in range(self.pitches + 1):
			y = int(pitch * cell_h)
			painter.drawLine(0, y, width, y)
		# Shade black-key lanes to make the grid read like a piano roll.
		for pitch in range(self.pitches):
			if NOTES[pitch % 12] in {"C#", "D#", "F#", "G#", "A#"}:
				painter.fillRect(0, int((self.pitches - pitch - 1) * cell_h), width, max(1, int(cell_h)), QColor(0, 0, 0, 25))
		for (step, pitch), velocity in self.notes.items():
			y = int((self.pitches - pitch - 1) * cell_h)
			color = QColor("#0A8CE6")
			color.setAlpha(120 + int(velocity * 1.3))
			painter.fillRect(int(step * cell_w) + 2, y + 2, max(1, int(cell_w) - 3), max(1, int(cell_h) - 3), color)
		if 0 <= self.playhead < self.steps:
			painter.setPen(QPen(QColor("#ff9f0a"), 2))
			x = int(self.playhead * cell_w)
			painter.drawLine(x, 0, x, height)


class CancelledError(Exception):
	pass


def list_source_files(folder: Path) -> list[Path]:
	numbered: list[Path] = []
	index = 1
	while (folder / f"{index}.wav").exists():
		numbered.append(folder / f"{index}.wav")
		index += 1
	if numbered:
		return numbered

	return [
		path
		for path in sorted(folder.glob("*.wav"))
		if path.name.lower() != "chromatic.wav"
	]


def note_label(start_note_index: int, start_octave: int, offset: int) -> str:
	total = start_note_index + offset
	return f"{NOTES[total % 12]}{start_octave + (total // 12)}"


def note_frequency(start_note_index: int, start_octave: int, offset: int) -> float:
	base_midi = (start_octave + 1) * 12 + start_note_index
	midi_note = base_midi + offset
	return 440.0 * math.pow(2.0, (midi_note - 69) / 12.0)


def make_silence(seconds: float, sample_rate: int) -> parselmouth.Sound:
	duration = max(0.0, float(seconds))
	return parselmouth.praat.call(
		"Create Sound from formula",
		"silence",
		1,
		0,
		duration,
		sample_rate,
		"0",
	)


def load_mono(path: Path, sample_rate: int = DEFAULT_SAMPLE_RATE) -> parselmouth.Sound:
	sound = parselmouth.Sound(str(path))
	sound = parselmouth.praat.call(sound, "Resample", sample_rate, 1)
	return parselmouth.praat.call(sound, "Convert to mono")


def resample_if_needed(sound: parselmouth.Sound, sample_rate: int) -> parselmouth.Sound:
	if int(sound.sampling_frequency) == int(sample_rate):
		return sound
	return parselmouth.praat.call(sound, "Resample", sample_rate, 1)


def peak_normalize(sound: parselmouth.Sound, target_peak: float = 0.98) -> parselmouth.Sound:
	values = np.asarray(sound.values, dtype=np.float64)
	peak = float(np.max(np.abs(values))) if values.size else 0.0
	if peak <= 1e-9:
		return sound
	return parselmouth.Sound(values * (target_peak / peak), sound.sampling_frequency)


def trim_edge_silence(sound: parselmouth.Sound, threshold_db: float = -40.0, padding_ms: int = 5) -> parselmouth.Sound:
	values = np.asarray(sound.values, dtype=np.float64)
	if values.size == 0:
		return sound
	mono = np.max(np.abs(values), axis=0)
	active = mono >= db_to_amplitude(threshold_db)
	active_indexes = np.flatnonzero(active)
	if active_indexes.size == 0:
		return sound
	padding = int(round(padding_ms * sound.sampling_frequency / 1000.0))
	start = max(0, int(active_indexes[0]) - padding)
	end = min(values.shape[1], int(active_indexes[-1]) + 1 + padding)
	if start == 0 and end == values.shape[1]:
		return sound
	return parselmouth.Sound(values[:, start:end], sound.sampling_frequency)


def apply_fade(sound: parselmouth.Sound, fade_ms: int) -> parselmouth.Sound:
	if fade_ms <= 0:
		return sound
	values = np.asarray(sound.values, dtype=np.float64).copy()
	total_frames = values.shape[1]
	fade_frames = min(int(round(fade_ms * sound.sampling_frequency / 1000.0)), total_frames // 2)
	if fade_frames <= 0:
		return sound
	fade_in = np.linspace(0.0, 1.0, fade_frames, endpoint=True)
	fade_out = np.linspace(1.0, 0.0, fade_frames, endpoint=True)
	values[:, :fade_frames] *= fade_in
	values[:, total_frames - fade_frames:] *= fade_out
	return parselmouth.Sound(values, sound.sampling_frequency)


def pad_or_trim(sound: parselmouth.Sound, length_seconds: float, sample_rate: int) -> parselmouth.Sound:
	if length_seconds <= 0:
		return sound
	target_frames = max(0, int(round(length_seconds * sample_rate)))
	current = resample_if_needed(sound, sample_rate)
	current_frames = int(current.get_number_of_samples())
	if current_frames == target_frames:
		return current
	if current_frames > target_frames:
		end_time = target_frames / float(sample_rate)
		return parselmouth.praat.call(current, "Extract part", 0, end_time, "rectangular", 1, "yes")

	padding = make_silence((target_frames - current_frames) / float(sample_rate), sample_rate)
	return parselmouth.Sound.concatenate([current, padding])


def retune_sound(sound: parselmouth.Sound, target_frequency: float, audio_style: str) -> parselmouth.Sound:
	if audio_style == "OG App":
		return retune_with_og_app(sound, target_frequency)
	if audio_style == "Formant corrected":
		return retune_with_formant_correction(sound, target_frequency)
	if audio_style == "Praat":
		return retune_with_praat(sound, target_frequency)
	if audio_style == "Vocal strain":
		return retune_with_expressive_voice(sound, target_frequency, maximum_drive=0.38)
	if audio_style == "Bright belt":
		return retune_with_praat_expression(sound, target_frequency, maximum_drive=0.34)
	if audio_style == "Yell / shout":
		return retune_with_human_scream(sound, target_frequency, maximum_drive=0.42)
	if audio_style == "Scream / belt":
		return retune_with_human_scream(sound, target_frequency, maximum_drive=0.58)
	if audio_style == "Rasp":
		return retune_with_human_scream(sound, target_frequency, maximum_drive=0.46)
	if target_frequency < LOW_NOTE_FFT_THRESHOLD:
		retuned = retune_with_fft(sound, target_frequency)
		if retuned is not None:
			return retuned
	return retune_with_praat(sound, target_frequency)


def retune_with_praat(sound: parselmouth.Sound, target_frequency: float) -> parselmouth.Sound:
	pitch_floor = 37.5 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 60.0
	pitch_ceiling = 1200.0 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 600.0
	manipulation = parselmouth.praat.call(sound, "To Manipulation", 0.03, pitch_floor, pitch_ceiling)
	pitch_tier = parselmouth.praat.call(manipulation, "Extract pitch tier")
	parselmouth.praat.call(pitch_tier, "Remove points between", sound.xmin, sound.xmax)
	parselmouth.praat.call(pitch_tier, "Add point", sound.xmin, target_frequency)
	parselmouth.praat.call(pitch_tier, "Add point", max(sound.xmax, sound.xmin + 0.000001), target_frequency)
	parselmouth.praat.call([pitch_tier, manipulation], "Replace pitch tier")
	return parselmouth.praat.call(manipulation, "Get resynthesis (overlap-add)")


def retune_with_og_app(sound: parselmouth.Sound, target_frequency: float) -> parselmouth.Sound:
	manipulation = parselmouth.praat.call(sound, "To Manipulation", 0.05, 60.0, 600.0)
	pitch_tier = parselmouth.praat.call(manipulation, "Extract pitch tier")
	parselmouth.praat.call(pitch_tier, "Formula", f"{target_frequency:.12g}")
	parselmouth.praat.call([pitch_tier, manipulation], "Replace pitch tier")
	return parselmouth.praat.call(manipulation, "Get resynthesis (overlap-add)")


def retune_with_formant_correction(sound: parselmouth.Sound, target_frequency: float) -> parselmouth.Sound:
	"""Shift the pitch median while leaving the formant frequencies unchanged.

	Praat's Change gender resynthesis applies the pitch change through overlap-add
	and accepts a formant shift ratio separately. A ratio of 1.0 means no formant
	shift, while the new pitch median moves the source into the requested note.
	"""
	pitch_floor = 37.5 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 60.0
	pitch_ceiling = 1200.0 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 600.0
	return parselmouth.praat.call(
		sound,
		"Change gender...",
		pitch_floor,
		pitch_ceiling,
		1.0,
		target_frequency,
		1.0,
		1.0,
	)


def estimated_voice_frequency(sound: parselmouth.Sound) -> float | None:
	"""Return a conservative median F0 for interval-aware vocal styles."""
	try:
		pitch = parselmouth.praat.call(sound, "To Pitch", 0.0, 50.0, 900.0)
		frequency = float(parselmouth.praat.call(pitch, "Get quantile", 0, 0, 0.5, "Hertz"))
	except Exception:
		return None
	if not math.isfinite(frequency) or frequency <= 0:
		return None
	return frequency


def vocal_effort_for_interval(source_frequency: float | None, target_frequency: float) -> float:
	"""Map an upward pitch interval to a smooth 0--1 vocal-effort amount."""
	if source_frequency is None or source_frequency <= 0 or target_frequency <= 0:
		return 0.0
	semitones = 12.0 * math.log2(target_frequency / source_frequency)
	return float(np.clip(
		(semitones - STRAIN_START_SEMITONES) / (STRAIN_FULL_SEMITONES - STRAIN_START_SEMITONES),
		0.0,
		1.0,
	))


def apply_vocal_drive(sound: parselmouth.Sound, effort: float, maximum_drive: float) -> parselmouth.Sound:
	"""Add restrained harmonic drive and presence without changing note length.

	This is deliberately a post-process, not generated noise: it retains the
	speaker's articulation while a soft saturation and a little pre-emphasis make
	high notes read more like a supported belt.  It cannot turn a non-vocal source
	into a real human scream, but avoids a sudden/artificial switch at one note.
	"""
	amount = float(np.clip(effort * maximum_drive, 0.0, 0.85))
	if amount <= 0:
		return sound
	values = np.asarray(sound.values, dtype=np.float64)
	if values.size == 0:
		return sound
	peak = float(np.max(np.abs(values)))
	if peak <= 1e-9:
		return sound

	# Level-independent soft clipping supplies upper harmonics.  Pre-emphasis is
	# mixed in lightly to preserve the brighter, open quality of a high belt.
	normalized = values / peak
	drive = 1.0 + 5.0 * amount
	saturated = np.tanh(normalized * drive) / math.tanh(drive)
	previous = np.concatenate((normalized[:, :1], normalized[:, :-1]), axis=1)
	presence = normalized - 0.90 * previous
	# Keep the natural scale of the differentiated signal.  Normalizing it to its
	# single highest sample exaggerates clicks and makes the old Rasp preset hiss.
	mixed = (1.0 - amount) * normalized + amount * saturated + (0.30 * amount) * presence
	# Keep the source peak (and therefore downstream normalization behavior) stable.
	mixed_peak = max(float(np.max(np.abs(mixed))), 1e-9)
	return parselmouth.Sound(np.clip(mixed * (peak / mixed_peak), -1.0, 1.0), sound.sampling_frequency)


def retune_with_expressive_voice(
	sound: parselmouth.Sound, target_frequency: float, maximum_drive: float,
) -> parselmouth.Sound:
	"""Pitch a voice and increase effort smoothly as the target goes higher."""
	source_frequency = estimated_voice_frequency(sound)
	effort = vocal_effort_for_interval(source_frequency, target_frequency)
	pitch_floor = 37.5 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 60.0
	pitch_ceiling = 1200.0 if target_frequency < LOW_NOTE_FFT_THRESHOLD else 600.0
	# Singers tend to raise formants slightly and use a wider pitch range as they
	# belt.  The bounded values stay considerably subtler than chipmunk shifting.
	retuned = parselmouth.praat.call(
		sound,
		"Change gender...",
		pitch_floor,
		pitch_ceiling,
		1.0 + 0.16 * effort,
		target_frequency,
		1.0 + 0.30 * effort,
		1.0,
	)
	return apply_vocal_drive(retuned, effort, maximum_drive)


def retune_with_human_scream(sound: parselmouth.Sound, target_frequency: float, maximum_drive: float) -> parselmouth.Sound:
	"""Use the higher-quality formant-aware path for human-style yells and screams.

	Real vocal fry/rasp cannot be recovered from a clean sample by adding a fixed
	modulator.  This keeps the source's own vocal detail, shifts formants only as
	the note climbs, and applies restrained harmonic compression instead.
	"""
	return retune_with_expressive_voice(sound, target_frequency, maximum_drive)


def retune_with_praat_expression(
	sound: parselmouth.Sound, target_frequency: float, maximum_drive: float,
) -> parselmouth.Sound:
	"""Build expressive styles on ChromaKit's direct Praat resynthesis path."""
	effort = vocal_effort_for_interval(estimated_voice_frequency(sound), target_frequency)
	# Unlike the formant-corrected style, this starts with the direct Praat pitch
	# tier.  It keeps the familiar FNF pitch character before adding expression.
	retuned = retune_with_praat(sound, target_frequency)
	return apply_vocal_drive(retuned, effort, maximum_drive)


def retune_with_fft(sound: parselmouth.Sound, target_frequency: float) -> parselmouth.Sound | None:
	try:
		pitch = parselmouth.praat.call(sound, "To Pitch", 0.0, 37.5, 200.0)
		current = float(parselmouth.praat.call(pitch, "Get quantile", 0, 0, 0.5, "Hertz"))
	except Exception:
		return None

	if not current or math.isnan(current):
		return None
	ratio = target_frequency / current
	if not math.isfinite(ratio) or ratio <= 0:
		return None
	if abs(math.log2(ratio)) < 0.0001:
		return sound

	values = np.asarray(sound.values, dtype=np.float64)
	sample_rate = int(sound.sampling_frequency)
	_, frames = values.shape
	padded_length = max(frames * 8, 1)
	frequencies = np.fft.rfftfreq(padded_length, d=1.0 / sample_rate)
	channels = []

	for channel in values:
		padded = np.pad(channel, (0, padded_length - frames))
		spectrum = np.fft.rfft(padded)
		magnitude = np.abs(spectrum)
		phase = np.angle(spectrum)
		scaled = frequencies / ratio
		shifted = np.interp(scaled, frequencies, magnitude, left=0.0, right=0.0)
		shifted_phase = np.interp(scaled, frequencies, phase, left=0.0, right=0.0)
		rendered = np.fft.irfft(shifted * np.exp(1j * shifted_phase), padded_length)
		channels.append(rendered[:frames])

	return parselmouth.Sound(np.clip(np.vstack(channels), -1.0, 1.0), sample_rate)


def sound_to_int16(sound: parselmouth.Sound, sample_rate: int) -> bytes:
	rendered = resample_if_needed(sound, sample_rate)
	values = np.asarray(rendered.values, dtype=np.float64)
	if values.ndim == 1:
		values = values.reshape(1, -1)
	clipped = np.clip(values, -1.0, 1.0)
	pcm = np.rint(clipped * 32767.0).astype("<i2")
	interleaved = np.ascontiguousarray(np.transpose(pcm))
	return interleaved.tobytes()


def pack_chunk(chunk_id: bytes, body: bytes) -> bytes:
	chunk = chunk_id + struct.pack("<I", len(body)) + body
	if len(body) % 2:
		chunk += b"\x00"
	return chunk


class WavStreamWriter:
	def __init__(self, path: Path, sample_rate: int, channels: int = 1) -> None:
		self.path = path
		self.sample_rate = sample_rate
		self.channels = channels
		self.data_size = 0
		self.handle = path.open("wb")
		self.handle.write(b"RIFF\x00\x00\x00\x00WAVE")
		byte_rate = sample_rate * channels * 2
		block_align = channels * 2
		fmt_body = struct.pack("<HHIIHH", 1, channels, sample_rate, byte_rate, block_align, 16)
		self.handle.write(pack_chunk(b"fmt ", fmt_body))
		self.data_size_pos = self.handle.tell() + 4
		self.handle.write(b"data\x00\x00\x00\x00")

	def write_sound(self, sound: parselmouth.Sound) -> int:
		data = sound_to_int16(sound, self.sample_rate)
		self.handle.write(data)
		self.data_size += len(data)
		return len(data) // (self.channels * 2)

	def close(self, markers: Sequence[SliceMarker] = ()) -> None:
		if self.data_size % 2:
			self.handle.write(b"\x00")
		if markers:
			self.handle.write(build_marker_chunks(markers))
		file_size = self.handle.tell() - 8
		self.handle.seek(4)
		self.handle.write(struct.pack("<I", file_size))
		self.handle.seek(self.data_size_pos)
		self.handle.write(struct.pack("<I", self.data_size))
		self.handle.close()


def build_marker_chunks(markers: Sequence[SliceMarker]) -> bytes:
	cue_body = struct.pack("<I", len(markers))
	list_body = b"adtl"
	for cue_id, marker in enumerate(markers, start=1):
		offset = max(0, int(marker.offset))
		cue_body += struct.pack("<II4sIII", cue_id, offset, b"data", 0, 0, offset)
		label_body = struct.pack("<I", cue_id) + marker.label.encode("utf-8") + b"\x00"
		list_body += pack_chunk(b"labl", label_body)
	return pack_chunk(b"cue ", cue_body) + pack_chunk(b"LIST", list_body)


def ordered_sample_indexes(count: int, total: int, mode: str) -> list[int]:
	if mode == "random":
		return [random.randrange(count) for _ in range(total)]
	order = list(range(count))
	if mode == "shuffle":
		random.shuffle(order)
	return [order[index % count] for index in range(total)]


def generate_chromatic(
	settings: GenerationSettings,
	on_progress: Callable[[int, int, str], None],
	on_log: Callable[[str], None],
	should_cancel: Callable[[], bool],
) -> Path:
	if not settings.sample_path.is_dir():
		raise ValueError("Choose a valid sample folder.")

	files = list_source_files(settings.sample_path)
	if not files:
		raise ValueError("No WAV files found in the folder.")

	output_path = settings.sample_path / "chromatic.wav"
	gap = make_silence(settings.gap_seconds, DEFAULT_SAMPLE_RATE) if settings.gap_seconds > 0 else None
	indexes = ordered_sample_indexes(len(files), settings.semitones, settings.order_mode)
	writer = WavStreamWriter(output_path, settings.output_sample_rate, 1)
	markers: list[SliceMarker] = []
	current_offset = 0
	dump_dir: Path | None = None

	if settings.dump_samples:
		dump_dir = settings.sample_path / ("pitched_samples" if settings.pitch_samples else "samples")
		dump_dir.mkdir(exist_ok=True)

	try:
		on_log(f"Found {len(files)} source WAV file(s).")
		on_log(
			f"Semitones: {settings.semitones} | Gap: {settings.gap_seconds:.3f}s | "
			f"Order: {settings.order_mode} | Style: {settings.audio_style} | Output: {settings.output_sample_rate} Hz"
		)
		for offset, source_index in enumerate(indexes):
			if should_cancel():
				raise CancelledError()

			source_path = files[source_index]
			label = note_label(settings.start_note_index, settings.start_octave, offset)
			on_log(f"[{offset + 1}/{settings.semitones}] Loading {source_path.name} -> {label}")
			sound = load_mono(source_path, DEFAULT_SAMPLE_RATE)

			if settings.trim_silence:
				sound = trim_edge_silence(sound)
				on_log("  trimmed silence")
			if settings.normalize:
				sound = peak_normalize(sound)
				on_log("  normalized")
			if settings.pitch_samples:
				sound = retune_sound(sound, note_frequency(settings.start_note_index, settings.start_octave, offset), settings.audio_style)
				on_log(f"  pitched ({settings.audio_style})")
			if settings.fade_ms > 0:
				sound = apply_fade(sound, settings.fade_ms)
				on_log(f"  fade {settings.fade_ms} ms")
			if settings.fixed_note_length > 0:
				sound = pad_or_trim(sound, settings.fixed_note_length, DEFAULT_SAMPLE_RATE)
				on_log(f"  fixed length {settings.fixed_note_length:.3f}s")

			if settings.slicex_markers:
				markers.append(SliceMarker(current_offset, label))

			frames = writer.write_sound(sound)
			current_offset += frames

			if dump_dir is not None:
				dump_path = dump_dir / f"note_{offset + 1}.wav"
				resample_if_needed(sound, settings.output_sample_rate).save(str(dump_path), "WAV")

			if gap is not None and offset < settings.semitones - 1:
				current_offset += writer.write_sound(gap)

			on_progress(offset + 1, settings.semitones, label)

		writer.close(markers if settings.slicex_markers else ())
	except Exception:
		if not writer.handle.closed:
			try:
				writer.close(())
			except Exception:
				writer.handle.close()
		raise

	on_log(f"Saved: {output_path}")
	return output_path


def db_to_amplitude(db_value: float) -> float:
	return math.pow(10.0, db_value / 20.0)


def find_audio_regions(
	values: np.ndarray,
	sample_rate: int,
	threshold_db: float,
	min_region_ms: int,
	min_silence_ms: int,
	padding_ms: int,
) -> list[tuple[int, int]]:
	if values.size == 0:
		return []

	threshold = db_to_amplitude(threshold_db)
	min_region = max(1, int(round(min_region_ms * sample_rate / 1000.0)))
	min_silence = max(1, int(round(min_silence_ms * sample_rate / 1000.0)))
	padding = max(0, int(round(padding_ms * sample_rate / 1000.0)))
	active = np.abs(values) >= threshold
	regions: list[tuple[int, int]] = []
	start: int | None = None
	last_active: int | None = None

	for index, is_active in enumerate(active):
		if is_active:
			if start is None:
				start = index
			last_active = index
		elif start is not None and last_active is not None and index - last_active >= min_silence:
			if last_active - start + 1 >= min_region:
				regions.append((max(0, start - padding), min(len(values), last_active + 1 + padding)))
			start = None
			last_active = None

	if start is not None and last_active is not None and last_active - start + 1 >= min_region:
		regions.append((max(0, start - padding), min(len(values), last_active + 1 + padding)))

	return regions


def prepare_samples(
	settings: PrepareSettings,
	on_progress: Callable[[int, int, str], None],
	on_log: Callable[[str], None],
	should_cancel: Callable[[], bool],
) -> Path:
	sources = [path for path in settings.source_paths if path.suffix.lower() == ".wav" and path.is_file()]
	if not sources:
		raise ValueError("Choose one or more WAV files or a folder containing WAV files.")

	settings.output_dir.mkdir(exist_ok=True)
	output_index = 1
	total_sources = len(sources)

	for source_number, source in enumerate(sources, start=1):
		if should_cancel():
			raise CancelledError()
		on_log(f"[{source_number}/{total_sources}] Scanning {source.name}")
		sound = load_mono(source, settings.output_sample_rate)
		values = np.asarray(sound.values[0], dtype=np.float64)
		regions = find_audio_regions(
			values,
			settings.output_sample_rate,
			settings.threshold_db,
			settings.min_region_ms,
			settings.min_silence_ms,
			settings.padding_ms,
		)
		if not regions:
			on_log("  no regions found")
			on_progress(source_number, total_sources, source.name)
			continue

		for start, end in regions:
			if should_cancel():
				raise CancelledError()
			part_values = values[start:end].reshape(1, -1)
			part = parselmouth.Sound(part_values, settings.output_sample_rate)
			output = settings.output_dir / f"{output_index}.wav"
			part.save(str(output), "WAV")
			on_log(f"  wrote {output.name}")
			output_index += 1

		on_progress(source_number, total_sources, source.name)

	if output_index == 1:
		raise ValueError("No non-silent sample regions were found.")

	on_log(f"Prepared {output_index - 1} sample(s) in: {settings.output_dir}")
	return settings.output_dir


def make_seamless_loop(sound: parselmouth.Sound, crossfade_ms: int) -> parselmouth.Sound:
	"""Create a repeatable loop by wrapping and crossfading its tail into its head.

	The last rendered sample becomes the sample immediately before the loop start,
	so consecutive plays meet without a discontinuity (the usual source of clicks).
	"""
	values = np.asarray(sound.values, dtype=np.float64)
	if values.ndim == 1:
		values = values.reshape(1, -1)
	frames = values.shape[1]
	if frames < 4:
		raise ValueError("A sample needs at least four frames to be looped.")
	crossfade = int(round(crossfade_ms * sound.sampling_frequency / 1000.0))
	crossfade = min(max(1, crossfade), max(1, frames // 3))
	# Begin after the head used at the seam.  The final blended frame therefore
	# flows directly into output frame zero when the file repeats.
	loop = values[:, crossfade:].copy()
	fade_in = np.linspace(0.0, 1.0, crossfade, endpoint=True)
	fade_out = 1.0 - fade_in
	loop[:, -crossfade:] = values[:, -crossfade:] * fade_out + values[:, :crossfade] * fade_in
	return parselmouth.Sound(np.clip(loop, -1.0, 1.0), sound.sampling_frequency)


def snap_to_zero_crossing(values: np.ndarray, frame: int, radius: int) -> int:
	"""Choose the nearest low-amplitude sign crossing for a click-resistant edit."""
	if len(values) < 2:
		return frame
	left, right = max(1, frame - radius), min(len(values) - 1, frame + radius)
	candidates = [index for index in range(left, right + 1) if values[index - 1] * values[index] <= 0]
	if not candidates:
		return int(np.clip(frame, 0, len(values) - 1))
	return min(candidates, key=lambda index: abs(values[index - 1]) + abs(values[index]) + abs(index - frame) * 1e-5)


def detect_best_loop_region(sound: parselmouth.Sound) -> tuple[float, float]:
	"""Find a long loop with similar waveform and slope on both sides of its seam."""
	mono = np.asarray(sound.values[0], dtype=np.float64)
	frames = len(mono)
	if frames < 16:
		raise ValueError("Sample is too short to analyse for a loop.")
	minimum = min(max(int(sound.sampling_frequency * 0.12), 8), max(2, frames // 3))
	window = min(max(int(sound.sampling_frequency * 0.018), 8), max(4, frames // 12))
	# Candidate locations are evenly distributed so analysis stays responsive for
	# long recordings, while the final crossfade handles sub-sample imperfections.
	points = np.unique(np.linspace(0, frames - 1, min(72, frames), dtype=int))
	best: tuple[float, int, int] | None = None
	for start in points:
		for end in points:
			if end - start < minimum or start + window >= frames or end < window:
				continue
			head = mono[start:start + window]
			tail = mono[end - window:end]
			if len(head) != window or len(tail) != window:
				continue
			scale = max(float(np.sqrt(np.mean(head * head))), float(np.sqrt(np.mean(tail * tail))), 1e-7)
			shape_error = float(np.mean(((head - tail) / scale) ** 2))
			slope_error = float(abs((head[1] - head[0]) - (tail[-1] - tail[-2])) / scale)
			boundary_error = float(abs(head[0] - tail[-1]) / scale)
			score = shape_error + 0.35 * slope_error + 0.8 * boundary_error
			if best is None or score < best[0]:
				best = (score, int(start), int(end))
	if best is None:
		return 0.0, frames * 1000.0 / sound.sampling_frequency
	return best[1] * 1000.0 / sound.sampling_frequency, best[2] * 1000.0 / sound.sampling_frequency


def render_loop_duration(sound: parselmouth.Sound, duration_seconds: float) -> parselmouth.Sound:
	"""Repeat a prepared seamless cycle to an exact exported note duration."""
	frames = sound.get_number_of_samples()
	target_frames = max(1, int(round(duration_seconds * sound.sampling_frequency)))
	if frames <= 0:
		raise ValueError("Cannot render an empty loop.")
	repeats = int(math.ceil(target_frames / frames))
	values = np.tile(np.asarray(sound.values, dtype=np.float64), (1, repeats))[:, :target_frames]
	return parselmouth.Sound(values, sound.sampling_frequency)


def loop_samples(
	settings: LoopSettings,
	on_progress: Callable[[int, int, str], None],
	on_log: Callable[[str], None],
	should_cancel: Callable[[], bool],
) -> Path:
	sources = [path for path in settings.source_paths if path.suffix.lower() == ".wav" and path.is_file()]
	if not sources:
		raise ValueError("Choose one or more WAV files to loop.")
	settings.output_dir.mkdir(exist_ok=True)
	for index, source in enumerate(sources, start=1):
		if should_cancel():
			raise CancelledError()
		on_log(f"[{index}/{len(sources)}] Looping {source.name}")
		sound = load_mono(source, settings.output_sample_rate)
		if settings.trim_silence:
			sound = trim_edge_silence(sound)
		frames = sound.get_number_of_samples()
		start = int(np.clip(round(settings.loop_start_ms * sound.sampling_frequency / 1000.0), 0, max(0, frames - 1)))
		end = int(np.clip(round(settings.loop_end_ms * sound.sampling_frequency / 1000.0), start + 1, frames))
		selected = parselmouth.Sound(np.asarray(sound.values[:, start:end], dtype=np.float64), sound.sampling_frequency)
		looped = make_seamless_loop(selected, settings.crossfade_ms)
		if settings.render_mode == "Render exact duration":
			looped = render_loop_duration(looped, settings.render_length_seconds)
		output = settings.output_dir / f"{source.stem}_loop.wav"
		looped.save(str(output), "WAV")
		on_log(f"  wrote {output.name}")
		on_progress(index, len(sources), source.name)
	on_log(f"Saved {len(sources)} looped sample(s) in: {settings.output_dir}")
	return settings.output_dir


class GenerationWorker(QThread):
	progress = Signal(int, int, str)
	log = Signal(str)
	done = Signal(str)
	failed = Signal(str)
	cancelled = Signal(str)

	def __init__(self, settings: GenerationSettings) -> None:
		super().__init__()
		self.settings = settings
		self._cancel = False

	def request_cancel(self) -> None:
		self._cancel = True

	def run(self) -> None:
		try:
			output = generate_chromatic(self.settings, self.progress.emit, self.log.emit, lambda: self._cancel)
		except CancelledError:
			self.cancelled.emit("Generation cancelled.")
		except Exception as error:
			self.failed.emit(str(error))
		else:
			self.done.emit(str(output))


class PrepareWorker(QThread):
	progress = Signal(int, int, str)
	log = Signal(str)
	done = Signal(str)
	failed = Signal(str)
	cancelled = Signal(str)

	def __init__(self, settings: PrepareSettings) -> None:
		super().__init__()
		self.settings = settings
		self._cancel = False

	def request_cancel(self) -> None:
		self._cancel = True

	def run(self) -> None:
		try:
			output = prepare_samples(self.settings, self.progress.emit, self.log.emit, lambda: self._cancel)
		except CancelledError:
			self.cancelled.emit("Sample preparation cancelled.")
		except Exception as error:
			self.failed.emit(str(error))
		else:
			self.done.emit(str(output))


class LoopWorker(QThread):
	progress = Signal(int, int, str)
	log = Signal(str)
	done = Signal(str)
	failed = Signal(str)
	cancelled = Signal(str)

	def __init__(self, settings: LoopSettings) -> None:
		super().__init__()
		self.settings = settings
		self._cancel = False

	def request_cancel(self) -> None:
		self._cancel = True

	def run(self) -> None:
		try:
			output = loop_samples(self.settings, self.progress.emit, self.log.emit, lambda: self._cancel)
		except CancelledError:
			self.cancelled.emit("Looping cancelled.")
		except Exception as error:
			self.failed.emit(str(error))
		else:
			self.done.emit(str(output))


class GeneratorWindow(QMainWindow):
	def __init__(self) -> None:
		super().__init__()
		self.settings = QSettings("immalloy", "ChromaKit")
		self.setWindowTitle("ChromaKit")
		icon_path = asset_path("icon.ico")
		if icon_path.exists():
			self.setWindowIcon(QIcon(str(icon_path)))
		self.setMinimumSize(920, 620)
		self.setAcceptDrops(True)

		self.worker: GenerationWorker | PrepareWorker | LoopWorker | None = None
		self.last_output_path: Path | None = None
		self.prepare_sources: tuple[Path, ...] = ()
		self.loop_sources: tuple[Path, ...] = ()

		self.tabs = QTabWidget()
		self.setCentralWidget(self.tabs)
		self.tabs.addTab(self.build_generate_tab(), "Generate")
		self.tabs.addTab(self.build_prepare_tab(), "Prepare Samples")
		self.tabs.addTab(self.build_loop_tab(), "Loop Samples")
		self.tabs.addTab(self.build_keyboard_tab(), "Keyboard")

		self.statusBar().showMessage("Idle")
		self.apply_system_brand_theme()
		self.restore_autosaved_options()
		self.refresh_generation_validation()

	def build_generate_tab(self) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(10)

		title_font = self.font()
		title_font.setFamily("Sora")
		title_font.setPointSize(24)
		title_font.setWeight(QFont.Weight.Black)
		title_chroma = QLabel("Chroma")
		title_chroma.setFont(title_font)
		self.title_chroma = title_chroma
		title_kit = QLabel("Kit")
		title_kit.setFont(title_font)
		self.title_kit = title_kit
		mark = QLabel()
		icon_path = asset_path("chromakit-icon.png")
		pixmap = QPixmap(str(icon_path))
		if not pixmap.isNull():
			mark.setPixmap(pixmap.scaled(46, 46, Qt.KeepAspectRatio, Qt.SmoothTransformation))
		header = QHBoxLayout()
		header.setSpacing(10)
		header.addWidget(mark)
		header.addWidget(title_chroma)
		header.addWidget(title_kit)
		header.addStretch(1)
		self.settings_button = QToolButton()
		self.settings_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView))
		self.settings_button.setToolTip("Settings, import/export, and credits")
		self.settings_button.clicked.connect(self.show_settings_modal)
		header.addWidget(self.settings_button)
		layout.addLayout(header)

		content = QHBoxLayout()
		content.setSpacing(14)
		layout.addLayout(content, 1)

		controls = QVBoxLayout()
		controls.setSpacing(10)
		content.addLayout(controls, 0)

		self.generate_settings_tabs = QTabWidget()
		controls.addWidget(self.generate_settings_tabs, 1)

		self.folder_input = QLineEdit()
		self.folder_input.setPlaceholderText("Choose or drop a folder with WAV samples")
		self.folder_input.textChanged.connect(self.refresh_generation_validation)
		self.browse_button = QPushButton("Browse")
		self.browse_button.clicked.connect(self.choose_folder)
		folder_row = QHBoxLayout()
		folder_row.addWidget(self.folder_input)
		folder_row.addWidget(self.browse_button)

		self.validation_label = QLabel("")
		self.validation_label.setStyleSheet("color: #a33;")

		source_group = QGroupBox("Source")
		source_layout = QVBoxLayout(source_group)
		source_layout.addLayout(folder_row)
		source_layout.addWidget(self.validation_label)
		source_tab = QWidget()
		source_tab_layout = QVBoxLayout(source_tab)
		source_tab_layout.setContentsMargins(8, 8, 8, 8)
		source_tab_layout.addWidget(source_group)
		source_tab_layout.addStretch(1)
		self.generate_settings_tabs.addTab(source_tab, "Source")

		self.start_note_input = QComboBox()
		self.start_note_input.addItems(NOTES)
		self.start_octave_input = QComboBox()
		self.start_octave_input.addItems(OCTAVES)
		self.start_octave_input.setCurrentText("2")
		self.range_input = QLineEdit("24")
		self.range_input.textChanged.connect(self.refresh_generation_validation)
		self.gap_input = QLineEdit("0.1")
		self.gap_input.textChanged.connect(self.refresh_generation_validation)
		self.order_input = QComboBox()
		self.order_input.addItems(ORDER_MODES)
		self.audio_style_input = QComboBox()
		self.audio_style_input.addItems(list(AUDIO_STYLES))
		self.audio_style_input.setCurrentText("Formant corrected")
		self.audio_style_input.setToolTip("Choose how notes are retuned. Expressive styles react to the upward interval from each source sample.")
		self.audio_style_description = QLabel()
		self.audio_style_description.setWordWrap(True)
		self.audio_style_description.setStyleSheet("color: palette(mid);")
		self.audio_style_input.currentTextChanged.connect(self.update_audio_style_description)
		self.update_audio_style_description(self.audio_style_input.currentText())

		pitch_group = QGroupBox("Pitch and Range")
		form = QFormLayout(pitch_group)
		form.setLabelAlignment(Qt.AlignRight)
		form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		form.addRow("Starting note:", self.start_note_input)
		form.addRow("Starting octave:", self.start_octave_input)
		form.addRow("Range:", self.range_input)
		form.addRow("Sample gap:", self.gap_input)
		form.addRow("Sample order:", self.order_input)
		form.addRow("Audio style:", self.audio_style_input)
		form.addRow("", self.audio_style_description)
		pitch_tab = QWidget()
		pitch_tab_layout = QVBoxLayout(pitch_tab)
		pitch_tab_layout.setContentsMargins(8, 8, 8, 8)
		pitch_tab_layout.addWidget(pitch_group)
		pitch_tab_layout.addStretch(1)
		self.generate_settings_tabs.addTab(pitch_tab, "Pitch / Range")

		self.pitch_input = QCheckBox("Pitch samples")
		self.pitch_input.setChecked(True)
		self.dump_input = QCheckBox("Dump individual samples")
		self.dump_input.setChecked(True)
		self.trim_silence_input = QCheckBox("Trim silence from samples")
		self.normalize_input = QCheckBox("Peak normalize before pitch")
		self.slicex_input = QCheckBox("Embed FL Studio Slicex markers")
		self.fade_input = QLineEdit("0")
		self.fixed_length_input = QLineEdit("0")
		self.sample_rate_input = QComboBox()
		self.sample_rate_input.addItems(SAMPLE_RATES)

		options_group = QGroupBox("Processing")
		options = QGridLayout(options_group)
		options.addWidget(self.pitch_input, 0, 0)
		options.addWidget(self.dump_input, 0, 1)
		options.addWidget(self.trim_silence_input, 1, 0)
		options.addWidget(self.normalize_input, 1, 1)
		options.addWidget(self.slicex_input, 2, 0, 1, 2)
		options.addWidget(QLabel("Fade in/out (ms):"), 3, 0)
		options.addWidget(self.fade_input, 3, 1)
		options.addWidget(QLabel("Fixed note length (s):"), 4, 0)
		options.addWidget(self.fixed_length_input, 4, 1)
		options.addWidget(QLabel("Output sample rate:"), 5, 0)
		options.addWidget(self.sample_rate_input, 5, 1)
		processing_tab = QWidget()
		processing_tab_layout = QVBoxLayout(processing_tab)
		processing_tab_layout.setContentsMargins(8, 8, 8, 8)
		processing_tab_layout.addWidget(options_group)
		processing_tab_layout.addStretch(1)
		self.generate_settings_tabs.addTab(processing_tab, "Processing")

		self.generate_button = QPushButton("Generate Chromatic")
		self.generate_button.clicked.connect(self.generate)
		self.cancel_button = QPushButton("Cancel")
		self.cancel_button.setEnabled(False)
		self.cancel_button.clicked.connect(self.cancel_worker)
		self.open_output_button = QPushButton("Open Output")
		self.open_output_button.setEnabled(False)
		self.open_output_button.clicked.connect(self.open_last_output)

		button_row = QHBoxLayout()
		button_row.addWidget(self.generate_button)
		button_row.addWidget(self.cancel_button)
		button_row.addWidget(self.open_output_button)
		controls.addLayout(button_row)
		controls.addStretch(1)

		self.progress = QProgressBar()
		self.progress.setRange(0, 1)
		self.progress.setValue(0)
		self.log_output = QTextEdit()
		self.log_output.setReadOnly(True)
		self.log_output.setPlaceholderText("Generation logs will appear here.")

		output_group = QGroupBox("Output")
		output_layout = QVBoxLayout(output_group)
		output_layout.addWidget(self.progress)
		output_layout.addWidget(self.log_output, 1)
		content.addWidget(output_group, 1)

		return tab

	def build_prepare_tab(self) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(10)

		content = QHBoxLayout()
		content.setSpacing(14)
		layout.addLayout(content, 1)

		controls = QVBoxLayout()
		controls.setSpacing(10)
		content.addLayout(controls, 0)

		self.prepare_settings_tabs = QTabWidget()
		controls.addWidget(self.prepare_settings_tabs, 1)

		self.prepare_source_input = QLineEdit()
		self.prepare_source_input.setReadOnly(True)
		self.prepare_source_input.setPlaceholderText("Choose WAV files or a folder to split by silence")
		self.prepare_files_button = QPushButton("Choose WAV Files")
		self.prepare_files_button.clicked.connect(self.choose_prepare_files)
		self.prepare_folder_button = QPushButton("Choose Folder")
		self.prepare_folder_button.clicked.connect(self.choose_prepare_folder)
		source_buttons = QHBoxLayout()
		source_buttons.addWidget(self.prepare_files_button)
		source_buttons.addWidget(self.prepare_folder_button)
		source_buttons.addStretch(1)
		source_group = QGroupBox("Source")
		source_layout = QVBoxLayout(source_group)
		source_layout.addWidget(self.prepare_source_input)
		source_layout.addLayout(source_buttons)
		source_tab = QWidget()
		source_tab_layout = QVBoxLayout(source_tab)
		source_tab_layout.setContentsMargins(8, 8, 8, 8)
		source_tab_layout.addWidget(source_group)
		source_tab_layout.addStretch(1)
		self.prepare_settings_tabs.addTab(source_tab, "Source")

		self.threshold_input = QDoubleSpinBox()
		self.threshold_input.setRange(-90.0, -1.0)
		self.threshold_input.setValue(-40.0)
		self.threshold_input.setSuffix(" dB")
		self.min_region_input = QSpinBox()
		self.min_region_input.setRange(1, 5000)
		self.min_region_input.setValue(80)
		self.min_region_input.setSuffix(" ms")
		self.min_silence_input = QSpinBox()
		self.min_silence_input.setRange(1, 5000)
		self.min_silence_input.setValue(120)
		self.min_silence_input.setSuffix(" ms")
		self.padding_input = QSpinBox()
		self.padding_input.setRange(0, 1000)
		self.padding_input.setValue(20)
		self.padding_input.setSuffix(" ms")
		self.prepare_sample_rate_input = QComboBox()
		self.prepare_sample_rate_input.addItems(SAMPLE_RATES)

		prepare_group = QGroupBox("Silence Detection")
		prepare_form = QFormLayout(prepare_group)
		prepare_form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
		prepare_form.addRow("Silence threshold:", self.threshold_input)
		prepare_form.addRow("Minimum sample length:", self.min_region_input)
		prepare_form.addRow("Minimum silence gap:", self.min_silence_input)
		prepare_form.addRow("Padding:", self.padding_input)
		prepare_form.addRow("Output sample rate:", self.prepare_sample_rate_input)
		silence_tab = QWidget()
		silence_tab_layout = QVBoxLayout(silence_tab)
		silence_tab_layout.setContentsMargins(8, 8, 8, 8)
		silence_tab_layout.addWidget(prepare_group)
		silence_tab_layout.addStretch(1)
		self.prepare_settings_tabs.addTab(silence_tab, "Silence")

		self.prepare_button = QPushButton("Prepare Samples")
		self.prepare_button.setEnabled(False)
		self.prepare_button.clicked.connect(self.prepare)
		self.prepare_cancel_button = QPushButton("Cancel")
		self.prepare_cancel_button.setEnabled(False)
		self.prepare_cancel_button.clicked.connect(self.cancel_worker)
		self.prepare_open_button = QPushButton("Open Prepared Folder")
		self.prepare_open_button.setEnabled(False)
		self.prepare_open_button.clicked.connect(self.open_last_output)

		prepare_buttons = QHBoxLayout()
		prepare_buttons.addWidget(self.prepare_button)
		prepare_buttons.addWidget(self.prepare_cancel_button)
		prepare_buttons.addWidget(self.prepare_open_button)
		controls.addLayout(prepare_buttons)
		controls.addStretch(1)

		self.prepare_progress = QProgressBar()
		self.prepare_progress.setRange(0, 1)
		self.prepare_log_output = QTextEdit()
		self.prepare_log_output.setReadOnly(True)
		self.prepare_log_output.setPlaceholderText("Preparation logs will appear here.")
		prepare_output_group = QGroupBox("Output")
		prepare_output_layout = QVBoxLayout(prepare_output_group)
		prepare_output_layout.addWidget(self.prepare_progress)
		prepare_output_layout.addWidget(self.prepare_log_output, 1)
		content.addWidget(prepare_output_group, 1)

		return tab

	def build_loop_tab(self) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(16, 14, 16, 14)
		content = QHBoxLayout()
		layout.addLayout(content, 1)
		controls = QVBoxLayout()
		content.addLayout(controls, 0)

		self.loop_source_input = QLineEdit()
		self.loop_source_input.setReadOnly(True)
		self.loop_source_input.setPlaceholderText("Choose one or more WAV samples to make seamless loops")
		self.loop_files_button = QPushButton("Add WAV Samples")
		self.loop_files_button.clicked.connect(self.choose_loop_files)
		self.loop_folder_button = QPushButton("Add Folder")
		self.loop_folder_button.clicked.connect(self.choose_loop_folder)
		source_group = QGroupBox("Samples")
		source_layout = QVBoxLayout(source_group)
		source_layout.addWidget(self.loop_source_input)
		source_buttons = QHBoxLayout()
		source_buttons.addWidget(self.loop_files_button)
		source_buttons.addWidget(self.loop_folder_button)
		source_layout.addLayout(source_buttons)
		controls.addWidget(source_group)

		self.loop_file_selector = QComboBox()
		self.loop_file_selector.currentIndexChanged.connect(self.select_loop_source)
		self.loop_waveform = WaveformView()
		self.loop_waveform.range_changed.connect(self.on_loop_waveform_range_changed)
		self.loop_start_input = QDoubleSpinBox()
		self.loop_start_input.setRange(0.0, 1_000_000.0)
		self.loop_start_input.setDecimals(2)
		self.loop_start_input.setSuffix(" ms")
		self.loop_start_input.valueChanged.connect(self.on_loop_range_inputs_changed)
		self.loop_end_input = QDoubleSpinBox()
		self.loop_end_input.setRange(0.01, 1_000_000.0)
		self.loop_end_input.setDecimals(2)
		self.loop_end_input.setSuffix(" ms")
		self.loop_end_input.valueChanged.connect(self.on_loop_range_inputs_changed)
		self.loop_auto_button = QPushButton("Auto-detect Best Loop")
		self.loop_auto_button.clicked.connect(self.auto_detect_loop)
		self.loop_crossfade_input = QSpinBox()
		self.loop_crossfade_input.setRange(1, 1000)
		self.loop_crossfade_input.setValue(30)
		self.loop_crossfade_input.setSuffix(" ms")
		self.loop_crossfade_input.valueChanged.connect(self.clear_loop_preview_cache)
		self.loop_trim_input = QCheckBox("Trim quiet edges before looping")
		self.loop_trim_input.setChecked(True)
		self.loop_zero_snap_input = QCheckBox("Snap handles to zero crossings")
		self.loop_zero_snap_input.setChecked(True)
		self.loop_sample_rate_input = QComboBox()
		self.loop_sample_rate_input.addItems(SAMPLE_RATES)
		self.loop_render_mode_input = QComboBox()
		self.loop_render_mode_input.addItems(["Render exact duration", "Save one seamless cycle"])
		self.loop_render_length_input = QDoubleSpinBox()
		self.loop_render_length_input.setRange(0.05, 120.0)
		self.loop_render_length_input.setValue(2.0)
		self.loop_render_length_input.setDecimals(3)
		self.loop_render_length_input.setSuffix(" s")
		loop_group = QGroupBox("Loop Region & Render")
		loop_form = QFormLayout(loop_group)
		loop_form.addRow("Preview sample:", self.loop_file_selector)
		loop_form.addRow("Loop start:", self.loop_start_input)
		loop_form.addRow("Loop end:", self.loop_end_input)
		loop_form.addRow("", self.loop_auto_button)
		loop_form.addRow("Crossfade:", self.loop_crossfade_input)
		loop_form.addRow("Output sample rate:", self.loop_sample_rate_input)
		loop_form.addRow("Export mode:", self.loop_render_mode_input)
		loop_form.addRow("Final rendered length:", self.loop_render_length_input)
		loop_form.addRow("", self.loop_trim_input)
		loop_form.addRow("", self.loop_zero_snap_input)
		help_label = QLabel("Drag the green Start and orange End markers on the waveform, enter exact milliseconds, or use Auto-detect. Choose an exact final render length to create held FNF notes, or save one clean loop cycle for a sampler. The selected tail is crossfaded into its head to avoid a waveform jump. Output is saved as <name>_loop.wav in looped_samples.")
		help_label.setWordWrap(True)
		loop_form.addRow(help_label)
		controls.addWidget(loop_group)
		controls.addWidget(self.loop_waveform)
		preview_group = QGroupBox("Loop Preview Piano")
		preview_layout = QGridLayout(preview_group)
		self.loop_piano_buttons: list[QPushButton] = []
		for offset in range(24):
			button = QPushButton(note_label(0, 4, offset))
			button.clicked.connect(lambda _checked=False, note_offset=offset: self.play_loop_piano(note_offset))
			preview_layout.addWidget(button, offset // 12, offset % 12)
			self.loop_piano_buttons.append(button)
		self.loop_play_source_button = QPushButton("A: Play Selected Source")
		self.loop_play_source_button.clicked.connect(lambda: self.play_loop_preview(looped=False))
		self.loop_play_loop_button = QPushButton("B: Play Seamless Loop")
		self.loop_play_loop_button.clicked.connect(lambda: self.play_loop_preview(looped=True))
		preview_layout.addWidget(self.loop_play_source_button, 2, 0, 1, 4)
		preview_layout.addWidget(self.loop_play_loop_button, 2, 4, 1, 4)
		self.loop_stop_button = QPushButton("Stop Preview")
		self.loop_stop_button.clicked.connect(self.stop_loop_preview)
		preview_layout.addWidget(self.loop_stop_button, 2, 8, 1, 4)
		controls.addWidget(preview_group)

		self.loop_button = QPushButton("Save Looped Samples")
		self.loop_button.setEnabled(False)
		self.loop_button.clicked.connect(self.loop_selected_samples)
		self.loop_cancel_button = QPushButton("Cancel")
		self.loop_cancel_button.setEnabled(False)
		self.loop_cancel_button.clicked.connect(self.cancel_worker)
		self.loop_open_button = QPushButton("Open Looped Folder")
		self.loop_open_button.setEnabled(False)
		self.loop_open_button.clicked.connect(self.open_last_output)
		buttons = QHBoxLayout()
		buttons.addWidget(self.loop_button)
		buttons.addWidget(self.loop_cancel_button)
		buttons.addWidget(self.loop_open_button)
		controls.addLayout(buttons)
		controls.addStretch(1)

		self.loop_progress = QProgressBar()
		self.loop_log_output = QTextEdit()
		self.loop_log_output.setReadOnly(True)
		self.loop_log_output.setPlaceholderText("Looping logs will appear here.")
		output_group = QGroupBox("Output")
		output_layout = QVBoxLayout(output_group)
		output_layout.addWidget(self.loop_progress)
		output_layout.addWidget(self.loop_log_output, 1)
		content.addWidget(output_group, 1)
		return tab

	def build_keyboard_tab(self) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(16, 14, 16, 14)
		layout.setSpacing(12)
		intro = QLabel("Test exported chromatic notes from your keyboard or by clicking a key. Select the folder that contains pitched_samples (or the exported WAV notes).")
		intro.setWordWrap(True)
		layout.addWidget(intro)

		self.keyboard_folder_input = QLineEdit()
		self.keyboard_folder_input.setReadOnly(True)
		self.keyboard_folder_input.setPlaceholderText("Choose a chromatic or pitched_samples folder")
		self.keyboard_folder_button = QPushButton("Choose Chromatic Folder")
		self.keyboard_folder_button.clicked.connect(self.choose_keyboard_folder)
		folder_row = QHBoxLayout()
		folder_row.addWidget(self.keyboard_folder_input)
		folder_row.addWidget(self.keyboard_folder_button)
		layout.addLayout(folder_row)

		self.keyboard_start_note_input = QComboBox()
		self.keyboard_start_note_input.addItems(NOTES)
		self.keyboard_start_note_input.setCurrentText("C")
		self.keyboard_start_octave_input = QComboBox()
		self.keyboard_start_octave_input.addItems(OCTAVES)
		self.keyboard_start_octave_input.setCurrentText("2")
		self.keyboard_start_note_input.currentTextChanged.connect(self.refresh_keyboard_labels)
		self.keyboard_start_octave_input.currentTextChanged.connect(self.refresh_keyboard_labels)
		self.keyboard_volume_input = QSlider(Qt.Horizontal)
		self.keyboard_volume_input.setRange(0, 100)
		self.keyboard_volume_input.setValue(80)
		controls_group = QGroupBox("Keyboard Setup")
		controls_form = QFormLayout(controls_group)
		controls_form.addRow("First exported note:", self.keyboard_start_note_input)
		controls_form.addRow("First exported octave:", self.keyboard_start_octave_input)
		controls_form.addRow("Volume:", self.keyboard_volume_input)
		layout.addWidget(controls_group)

		self.keyboard_status_label = QLabel("Add a folder, then click a key. Physical keys cover two octaves: Z/S/X/D/C/V/G/B/H/N/J/M, then Q/2/W/3/E/R/5/T/6/Y/7/U.")
		self.keyboard_status_label.setWordWrap(True)
		layout.addWidget(self.keyboard_status_label)
		keys_group = QGroupBox("Chromatic Test Keyboard")
		keys_layout = QGridLayout(keys_group)
		self.keyboard_buttons: list[QPushButton] = []
		for offset in range(24):
			button = QPushButton()
			button.setMinimumHeight(58)
			button.clicked.connect(lambda _checked=False, note_offset=offset: self.play_keyboard_note(note_offset))
			keys_layout.addWidget(button, offset // 12, offset % 12)
			self.keyboard_buttons.append(button)
		layout.addWidget(keys_group)
		piano_roll_group = QGroupBox("Piano Roll — click cells to add/remove notes")
		piano_roll_layout = QVBoxLayout(piano_roll_group)
		self.piano_roll = PianoRollView()
		self.piano_roll.notes_changed.connect(self.update_piano_roll_status)
		piano_roll_layout.addWidget(self.piano_roll)
		self.piano_roll_bpm = QSpinBox()
		self.piano_roll_bpm.setRange(40, 300)
		self.piano_roll_bpm.setValue(120)
		self.piano_roll_velocity = QSlider(Qt.Horizontal)
		self.piano_roll_velocity.setRange(1, 127)
		self.piano_roll_velocity.setValue(100)
		self.piano_roll_record = QCheckBox("Record played keys into piano roll")
		self.piano_roll_play_button = QPushButton("Play Pattern")
		self.piano_roll_play_button.clicked.connect(self.toggle_piano_roll_playback)
		self.piano_roll_clear_button = QPushButton("Clear Pattern")
		self.piano_roll_clear_button.clicked.connect(self.piano_roll.clear_notes)
		transport = QHBoxLayout()
		transport.addWidget(QLabel("BPM:"))
		transport.addWidget(self.piano_roll_bpm)
		transport.addWidget(QLabel("Velocity:"))
		transport.addWidget(self.piano_roll_velocity, 1)
		transport.addWidget(self.piano_roll_record)
		transport.addWidget(self.piano_roll_play_button)
		transport.addWidget(self.piano_roll_clear_button)
		piano_roll_layout.addLayout(transport)
		layout.addWidget(piano_roll_group, 1)
		self.keyboard_files: list[Path] = []
		self.keyboard_sounds: dict[Path, QSoundEffect] = {}
		self.keyboard_transport = QTimer(self)
		self.keyboard_transport.timeout.connect(self.advance_piano_roll)
		self.keyboard_step = 0
		self.refresh_keyboard_labels()
		return tab

	def refresh_keyboard_labels(self, _unused: str = "") -> None:
		if not hasattr(self, "keyboard_buttons"):
			return
		start_index = self.keyboard_start_note_input.currentIndex()
		start_octave = int(self.keyboard_start_octave_input.currentText())
		for offset, button in enumerate(self.keyboard_buttons):
			button.setText(note_label(start_index, start_octave, offset))
			button.setEnabled(bool(self.keyboard_files) and offset < len(self.keyboard_files))

	def choose_keyboard_folder(self) -> None:
		folder = QFileDialog.getExistingDirectory(self, "Choose chromatic or pitched_samples folder")
		if not folder:
			return
		path = Path(folder)
		# Selecting the generation folder is convenient; prefer its pitched export.
		source_dir = path / "pitched_samples" if (path / "pitched_samples").is_dir() else path
		self.keyboard_files = list_source_files(source_dir)
		self.keyboard_sounds.clear()
		self.keyboard_folder_input.setText(str(source_dir))
		if self.keyboard_files:
			self.keyboard_status_label.setText(f"Loaded {len(self.keyboard_files)} note file(s). Click a key or use the two-octave Z–M / Q–U key layout.")
		else:
			self.keyboard_status_label.setText("No note WAV files found. Generate with ‘Dump individual samples’ enabled, then choose its pitched_samples folder.")
		self.refresh_keyboard_labels()

	def play_keyboard_note(self, offset: int, velocity: float = 1.0, record: bool = True) -> None:
		if offset >= len(self.keyboard_files):
			return
		path = self.keyboard_files[offset]
		sound = self.keyboard_sounds.get(path)
		if sound is None:
			sound = QSoundEffect(self)
			sound.setSource(QUrl.fromLocalFile(str(path)))
			sound.setLoopCount(1)
			self.keyboard_sounds[path] = sound
		sound.setVolume(self.keyboard_volume_input.value() / 100.0 * float(np.clip(velocity, 0.0, 1.0)))
		if record and self.piano_roll_record.isChecked():
			self.piano_roll.add_note(self.keyboard_step, offset, self.piano_roll_velocity.value())
		sound.stop()
		sound.play()
		self.keyboard_status_label.setText(f"Playing {self.keyboard_buttons[offset].text()} — {path.name}")

	def update_piano_roll_status(self) -> None:
		if hasattr(self, "piano_roll"):
			self.keyboard_status_label.setText(f"Piano roll: {len(self.piano_roll.notes)} note(s). Click a cell to edit; Play Pattern to audition.")

	def toggle_piano_roll_playback(self) -> None:
		if self.keyboard_transport.isActive():
			self.keyboard_transport.stop()
			self.piano_roll.set_playhead(-1)
			self.piano_roll_play_button.setText("Play Pattern")
			return
		self.keyboard_step = 0
		self.keyboard_transport.setInterval(max(1, round(60000 / self.piano_roll_bpm.value() / 4)))
		self.keyboard_transport.start()
		self.piano_roll_play_button.setText("Stop Pattern")
		self.advance_piano_roll()

	def advance_piano_roll(self) -> None:
		if not self.keyboard_transport.isActive():
			return
		self.piano_roll.set_playhead(self.keyboard_step)
		for note in self.piano_roll.notes_at(self.keyboard_step):
			self.play_keyboard_note(note.pitch, note.velocity / 127.0, record=False)
		self.keyboard_step = (self.keyboard_step + 1) % self.piano_roll.steps

	def keyPressEvent(self, event: QKeyEvent) -> None:
		if self.tabs.currentIndex() in (2, 3) and not event.isAutoRepeat():
			keys = (
				Qt.Key.Key_Z, Qt.Key.Key_S, Qt.Key.Key_X, Qt.Key.Key_D, Qt.Key.Key_C, Qt.Key.Key_V,
				Qt.Key.Key_G, Qt.Key.Key_B, Qt.Key.Key_H, Qt.Key.Key_N, Qt.Key.Key_J, Qt.Key.Key_M,
				Qt.Key.Key_Q, Qt.Key.Key_2, Qt.Key.Key_W, Qt.Key.Key_3, Qt.Key.Key_E, Qt.Key.Key_R,
				Qt.Key.Key_5, Qt.Key.Key_T, Qt.Key.Key_6, Qt.Key.Key_Y, Qt.Key.Key_7, Qt.Key.Key_U,
			)
			try:
				offset = keys.index(event.key())
			except ValueError:
				pass
			else:
				if self.tabs.currentIndex() == 2:
					self.play_loop_piano(offset)
				else:
					self.play_keyboard_note(offset)
				event.accept()
				return
		super().keyPressEvent(event)

	def system_theme_is_dark(self) -> bool:
		scheme = QApplication.styleHints().colorScheme()
		if scheme == Qt.ColorScheme.Dark:
			return True
		if scheme == Qt.ColorScheme.Light:
			return False
		return QApplication.palette().color(QPalette.ColorRole.Window).lightness() < 128

	def apply_system_brand_theme(self) -> None:
		dark = self.system_theme_is_dark()
		self.title_chroma.setStyleSheet(f"color: {BRAND_CHROMA_DARK_COLOR if dark else BRAND_CHROMA_COLOR};")
		self.title_kit.setStyleSheet(f"color: {BRAND_KIT_COLOR};")
		self.validation_label.setStyleSheet(f"color: {'#ff8d8d' if dark else '#a33'};")

	def show_settings_modal(self) -> None:
		dialog = QDialog(self)
		dialog.setWindowTitle("ChromaKit Settings")
		dialog.setMinimumSize(520, 360)
		layout = QVBoxLayout(dialog)
		tabs = QTabWidget()
		tabs.addTab(self.build_import_export_tab(dialog), "Import / Export")
		tabs.addTab(self.build_credits_tab(), "Credits")
		layout.addWidget(tabs)
		close_button = QPushButton("Close")
		close_button.clicked.connect(dialog.accept)
		button_row = QHBoxLayout()
		button_row.addStretch(1)
		button_row.addWidget(close_button)
		layout.addLayout(button_row)
		dialog.exec()

	def build_import_export_tab(self, dialog: QDialog) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(12, 12, 12, 12)
		layout.setSpacing(10)

		options_group = QGroupBox("Options Backup")
		options_layout = QVBoxLayout(options_group)
		options_note = QLabel(
			"ChromaKit autosaves your current options while you use the app. "
			"Use these buttons when you want a backup file or want to move options to another install."
		)
		options_note.setWordWrap(True)
		options_layout.addWidget(options_note)
		button_row = QHBoxLayout()
		export_button = QPushButton("Export Options")
		export_button.clicked.connect(lambda: self.export_options(dialog))
		import_button = QPushButton("Import Options")
		import_button.clicked.connect(lambda: self.import_options(dialog))
		button_row.addWidget(export_button)
		button_row.addWidget(import_button)
		options_layout.addLayout(button_row)
		layout.addWidget(options_group)
		layout.addStretch(1)
		return tab

	def build_credits_tab(self) -> QWidget:
		tab = QWidget()
		layout = QVBoxLayout(tab)
		layout.setContentsMargins(12, 12, 12, 12)
		credits = QLabel(
			"<b>ChromaKit</b><br>"
			"Created by immalloy.<br><br>"
			"<b>Based on</b><br>"
			"Chromatic Scale Generator by ChillSpace.<br><br>"
			"<b>Inspired by previous versions</b><br>"
			"Chromatic Scale Generator PLUS! (REVIVED).<br>"
			"(CANCELLED) Chromatic Scale Generator DELUXE."
		)
		credits.setTextFormat(Qt.RichText)
		credits.setWordWrap(True)
		layout.addWidget(credits)
		layout.addStretch(1)
		return tab

	def update_audio_style_description(self, style: str) -> None:
		self.audio_style_description.setText(AUDIO_STYLES.get(style, ""))

	def current_options(self) -> dict[str, object]:
		return {
			"version": 1,
			"generate": {
				"folder": self.folder_input.text(),
				"start_note": self.start_note_input.currentText(),
				"start_octave": self.start_octave_input.currentText(),
				"range": self.range_input.text(),
				"gap": self.gap_input.text(),
				"order": self.order_input.currentText(),
				"pitch_samples": self.pitch_input.isChecked(),
				"dump_samples": self.dump_input.isChecked(),
				"audio_style": self.audio_style_input.currentText(),
				"trim_silence": self.trim_silence_input.isChecked(),
				"normalize": self.normalize_input.isChecked(),
				"slicex_markers": self.slicex_input.isChecked(),
				"fade_ms": self.fade_input.text(),
				"fixed_note_length": self.fixed_length_input.text(),
				"sample_rate": self.sample_rate_input.currentText(),
			},
			"prepare": {
				"source": self.prepare_source_input.text(),
				"threshold_db": self.threshold_input.value(),
				"min_region_ms": self.min_region_input.value(),
				"min_silence_ms": self.min_silence_input.value(),
				"padding_ms": self.padding_input.value(),
				"sample_rate": self.prepare_sample_rate_input.currentText(),
			},
		}

	def export_options(self, parent: QWidget) -> None:
		path, _ = QFileDialog.getSaveFileName(parent, "Export ChromaKit options", "chromakit-options.json", "JSON files (*.json)")
		if not path:
			return
		try:
			Path(path).write_text(json.dumps(self.current_options(), indent=2), encoding="utf-8")
		except Exception as error:
			QMessageBox.critical(parent, "ChromaKit", f"Could not export options: {error}")
			return
		QMessageBox.information(parent, "ChromaKit", f"Exported options to {path}")

	def import_options(self, parent: QWidget) -> None:
		path, _ = QFileDialog.getOpenFileName(parent, "Import ChromaKit options", "", "JSON files (*.json)")
		if not path:
			return
		try:
			data = json.loads(Path(path).read_text(encoding="utf-8"))
			if not isinstance(data, dict):
				raise ValueError("Options file must contain a JSON object.")
			self.apply_imported_options(data)
			self.save_autosaved_options()
		except Exception as error:
			QMessageBox.critical(parent, "ChromaKit", f"Could not import options: {error}")
			return
		QMessageBox.information(parent, "ChromaKit", "Imported options.")

	def apply_imported_options(self, data: dict[str, object]) -> None:
		generate = data.get("generate", {})
		if isinstance(generate, dict):
			self.folder_input.setText(str(generate.get("folder", self.folder_input.text())))
			self.start_note_input.setCurrentText(str(generate.get("start_note", self.start_note_input.currentText())))
			self.start_octave_input.setCurrentText(str(generate.get("start_octave", self.start_octave_input.currentText())))
			self.range_input.setText(str(generate.get("range", self.range_input.text())))
			self.gap_input.setText(str(generate.get("gap", self.gap_input.text())))
			self.order_input.setCurrentText(str(generate.get("order", self.order_input.currentText())))
			self.pitch_input.setChecked(bool(generate.get("pitch_samples", self.pitch_input.isChecked())))
			self.dump_input.setChecked(bool(generate.get("dump_samples", self.dump_input.isChecked())))
			self.audio_style_input.setCurrentText(str(generate.get("audio_style", self.audio_style_input.currentText())))
			self.trim_silence_input.setChecked(bool(generate.get("trim_silence", self.trim_silence_input.isChecked())))
			self.normalize_input.setChecked(bool(generate.get("normalize", self.normalize_input.isChecked())))
			self.slicex_input.setChecked(bool(generate.get("slicex_markers", self.slicex_input.isChecked())))
			self.fade_input.setText(str(generate.get("fade_ms", self.fade_input.text())))
			self.fixed_length_input.setText(str(generate.get("fixed_note_length", self.fixed_length_input.text())))
			self.sample_rate_input.setCurrentText(str(generate.get("sample_rate", self.sample_rate_input.currentText())))

		prepare = data.get("prepare", {})
		if isinstance(prepare, dict):
			self.threshold_input.setValue(float(prepare.get("threshold_db", self.threshold_input.value())))
			self.min_region_input.setValue(int(prepare.get("min_region_ms", self.min_region_input.value())))
			self.min_silence_input.setValue(int(prepare.get("min_silence_ms", self.min_silence_input.value())))
			self.padding_input.setValue(int(prepare.get("padding_ms", self.padding_input.value())))
			self.prepare_sample_rate_input.setCurrentText(str(prepare.get("sample_rate", self.prepare_sample_rate_input.currentText())))

		self.refresh_generation_validation()

	def restore_autosaved_options(self) -> None:
		raw = self.settings.value("options")
		if not raw:
			return
		try:
			data = json.loads(str(raw))
			if isinstance(data, dict):
				self.apply_imported_options(data)
		except Exception:
			pass

	def save_autosaved_options(self) -> None:
		self.settings.setValue("options", json.dumps(self.current_options()))
		self.settings.sync()

	def closeEvent(self, event: QCloseEvent) -> None:
		self.save_autosaved_options()
		super().closeEvent(event)

	def dragEnterEvent(self, event: QDragEnterEvent) -> None:
		if event.mimeData().hasUrls():
			event.acceptProposedAction()

	def dropEvent(self, event: QDropEvent) -> None:
		urls = event.mimeData().urls()
		if not urls:
			return
		path = Path(urls[0].toLocalFile())
		if path.is_dir():
			if self.tabs.currentIndex() == 1:
				self.set_prepare_sources(tuple(sorted(path.glob("*.wav"))), path / "prepared_samples")
			elif self.tabs.currentIndex() == 2:
				self.set_loop_sources(tuple(sorted(path.glob("*.wav"))), path / "looped_samples")
			elif self.tabs.currentIndex() == 3:
				source_dir = path / "pitched_samples" if (path / "pitched_samples").is_dir() else path
				self.keyboard_files = list_source_files(source_dir)
				self.keyboard_folder_input.setText(str(source_dir))
				self.refresh_keyboard_labels()
			else:
				self.folder_input.setText(str(path))
		elif path.suffix.lower() == ".wav" and self.tabs.currentIndex() == 1:
			self.set_prepare_sources(tuple(Path(url.toLocalFile()) for url in urls), path.parent / "prepared_samples")
		elif path.suffix.lower() == ".wav" and self.tabs.currentIndex() == 2:
			self.set_loop_sources(tuple(Path(url.toLocalFile()) for url in urls), path.parent / "looped_samples")

	def choose_folder(self) -> None:
		folder = QFileDialog.getExistingDirectory(self, "Select sample folder")
		if folder:
			self.folder_input.setText(folder)

	def choose_prepare_files(self) -> None:
		files, _ = QFileDialog.getOpenFileNames(self, "Choose WAV files", "", "WAV files (*.wav)")
		if files:
			paths = tuple(Path(path) for path in files)
			self.set_prepare_sources(paths, paths[0].parent / "prepared_samples")

	def choose_prepare_folder(self) -> None:
		folder = QFileDialog.getExistingDirectory(self, "Choose folder with WAV files")
		if folder:
			path = Path(folder)
			self.set_prepare_sources(tuple(sorted(path.glob("*.wav"))), path / "prepared_samples")

	def choose_loop_files(self) -> None:
		files, _ = QFileDialog.getOpenFileNames(self, "Add WAV samples to loop", "", "WAV files (*.wav)")
		if files:
			paths = tuple(Path(path) for path in files)
			self.set_loop_sources(paths, paths[0].parent / "looped_samples")

	def choose_loop_folder(self) -> None:
		folder = QFileDialog.getExistingDirectory(self, "Add a folder of WAV samples")
		if folder:
			path = Path(folder)
			self.set_loop_sources(tuple(sorted(path.glob("*.wav"))), path / "looped_samples")

	def set_loop_sources(self, paths: tuple[Path, ...], output_dir: Path) -> None:
		self.loop_sources = tuple(path for path in paths if path.suffix.lower() == ".wav")
		self.loop_output_dir = output_dir
		if len(self.loop_sources) == 1:
			self.loop_source_input.setText(str(self.loop_sources[0]))
		else:
			self.loop_source_input.setText(f"{len(self.loop_sources)} WAV file(s) -> {output_dir}")
		self.loop_file_selector.blockSignals(True)
		self.loop_file_selector.clear()
		for path in self.loop_sources:
			self.loop_file_selector.addItem(path.name, path)
		self.loop_file_selector.blockSignals(False)
		self.loop_preview_cache: dict[int, QSoundEffect] = {}
		if self.loop_sources:
			self.select_loop_source(0)
		self.loop_button.setEnabled(bool(self.loop_sources) and self.worker is None)

	def select_loop_source(self, index: int) -> None:
		if index < 0 or index >= len(self.loop_sources):
			return
		try:
			self.loop_preview_sound = load_mono(self.loop_sources[index], int(self.loop_sample_rate_input.currentText()))
		except Exception as error:
			QMessageBox.warning(self, "ChromaKit", f"Could not load preview: {error}")
			return
		duration_ms = self.loop_preview_sound.xmax * 1000.0
		self.loop_start_input.blockSignals(True)
		self.loop_end_input.blockSignals(True)
		self.loop_start_input.setMaximum(max(0.0, duration_ms - 0.01))
		self.loop_end_input.setMaximum(duration_ms)
		self.loop_start_input.setValue(0.0)
		self.loop_end_input.setValue(duration_ms)
		self.loop_start_input.blockSignals(False)
		self.loop_end_input.blockSignals(False)
		self.loop_waveform.set_sound(self.loop_preview_sound)
		self.loop_preview_cache = {}

	def on_loop_range_inputs_changed(self, _value: float) -> None:
		if not hasattr(self, "loop_preview_sound"):
			return
		start = self.loop_start_input.value()
		end = max(self.loop_end_input.value(), start + 0.01)
		if self.loop_zero_snap_input.isChecked():
			values = np.asarray(self.loop_preview_sound.values[0], dtype=np.float64)
			radius = max(1, int(self.loop_preview_sound.sampling_frequency * 0.006))
			start = snap_to_zero_crossing(values, int(start * self.loop_preview_sound.sampling_frequency / 1000.0), radius) * 1000.0 / self.loop_preview_sound.sampling_frequency
			end = snap_to_zero_crossing(values, int(end * self.loop_preview_sound.sampling_frequency / 1000.0), radius) * 1000.0 / self.loop_preview_sound.sampling_frequency
			end = max(end, start + 0.01)
		self.loop_start_input.blockSignals(True)
		self.loop_end_input.blockSignals(True)
		self.loop_start_input.setValue(start)
		self.loop_end_input.setValue(end)
		self.loop_start_input.blockSignals(False)
		self.loop_end_input.blockSignals(False)
		self.loop_waveform.set_range_ms(start, end)
		self.loop_preview_cache = {}

	def on_loop_waveform_range_changed(self, start_ms: float, end_ms: float) -> None:
		self.loop_start_input.blockSignals(True)
		self.loop_end_input.blockSignals(True)
		self.loop_start_input.setValue(start_ms)
		self.loop_end_input.setValue(end_ms)
		self.loop_start_input.blockSignals(False)
		self.loop_end_input.blockSignals(False)
		self.loop_preview_cache = {}

	def auto_detect_loop(self) -> None:
		if not hasattr(self, "loop_preview_sound"):
			QMessageBox.information(self, "ChromaKit", "Add a WAV sample first.")
			return
		start, end = detect_best_loop_region(self.loop_preview_sound)
		self.loop_start_input.setValue(start)
		self.loop_end_input.setValue(end)
		self.on_loop_range_inputs_changed(0.0)
		self.statusBar().showMessage(f"Best loop region found: {start:.1f}–{end:.1f} ms", 6000)

	def clear_loop_preview_cache(self, _value: int = 0) -> None:
		self.loop_preview_cache = {}

	def selected_loop_segment(self) -> parselmouth.Sound | None:
		if not hasattr(self, "loop_preview_sound"):
			return None
		sound = self.loop_preview_sound
		frames = sound.get_number_of_samples()
		start = int(round(self.loop_start_input.value() * sound.sampling_frequency / 1000.0))
		end = int(round(self.loop_end_input.value() * sound.sampling_frequency / 1000.0))
		start = int(np.clip(start, 0, max(0, frames - 1)))
		end = int(np.clip(end, start + 1, frames))
		return parselmouth.Sound(sound.values[:, start:end], sound.sampling_frequency)

	def selected_loop_sound(self) -> parselmouth.Sound | None:
		segment = self.selected_loop_segment()
		if segment is None:
			return None
		cycle = make_seamless_loop(segment, int(self.loop_crossfade_input.value()))
		if self.loop_render_mode_input.currentText() == "Render exact duration":
			return render_loop_duration(cycle, self.loop_render_length_input.value())
		return cycle

	def play_loop_preview(self, looped: bool) -> None:
		sound = self.selected_loop_sound() if looped else self.selected_loop_segment()
		if sound is None:
			return
		path = Path(tempfile.gettempdir()) / f"chromakit-loop-ab-{id(self)}-{'loop' if looped else 'source'}.wav"
		sound.save(str(path), "WAV")
		self.loop_ab_sound = QSoundEffect(self)
		self.loop_ab_sound.setSource(QUrl.fromLocalFile(str(path)))
		self.loop_ab_sound.setVolume(0.8)
		# B repeats the final render twice to make the seam easy to judge; A is a
		# one-shot of the unprocessed selected region.
		self.loop_ab_sound.setLoopCount(2 if looped else 1)
		self.loop_ab_sound.play()

	def stop_loop_preview(self) -> None:
		if hasattr(self, "loop_ab_sound"):
			self.loop_ab_sound.stop()
		for sound in getattr(self, "loop_preview_cache", {}).values():
			sound.stop()

	def play_loop_piano(self, note_offset: int) -> None:
		looped = self.selected_loop_sound()
		if looped is None:
			return
		# Cache each preview note after range changes. C4 starts the compact piano.
		sound = self.loop_preview_cache.get(note_offset)
		if sound is None:
			preview = retune_with_praat(looped, note_frequency(0, 4, note_offset))
			path = Path(tempfile.gettempdir()) / f"chromakit-loop-preview-{id(self)}-{note_offset}.wav"
			preview.save(str(path), "WAV")
			sound = QSoundEffect(self)
			sound.setSource(QUrl.fromLocalFile(str(path)))
			sound.setLoopCount(1)
			self.loop_preview_cache[note_offset] = sound
		sound.setVolume(0.8)
		sound.stop()
		sound.play()

	def set_prepare_sources(self, paths: tuple[Path, ...], output_dir: Path) -> None:
		self.prepare_sources = tuple(path for path in paths if path.suffix.lower() == ".wav")
		self.prepare_output_dir = output_dir
		if len(self.prepare_sources) == 1:
			self.prepare_source_input.setText(str(self.prepare_sources[0]))
		else:
			self.prepare_source_input.setText(f"{len(self.prepare_sources)} WAV file(s) -> {output_dir}")
		self.prepare_button.setEnabled(bool(self.prepare_sources) and self.worker is None)

	def parse_generation_settings(self) -> GenerationSettings:
		folder = Path(self.folder_input.text().strip())
		try:
			semitones = int(self.range_input.text().strip())
			gap_seconds = float(self.gap_input.text().strip())
			fade_ms = int(float(self.fade_input.text().strip()))
			fixed_length = float(self.fixed_length_input.text().strip())
		except ValueError as error:
			raise ValueError("Range, gap, fade, and fixed length must be valid numbers.") from error

		if semitones < 1 or semitones > 128:
			raise ValueError("Range must be between 1 and 128.")
		if gap_seconds < 0:
			raise ValueError("Sample gap cannot be negative.")
		if fade_ms < 0:
			raise ValueError("Fade cannot be negative.")
		if fixed_length < 0:
			raise ValueError("Fixed note length cannot be negative.")

		return GenerationSettings(
			sample_path=folder,
			start_note_index=self.start_note_input.currentIndex(),
			start_octave=int(self.start_octave_input.currentText()),
			semitones=semitones,
			gap_seconds=gap_seconds,
			pitch_samples=self.pitch_input.isChecked(),
			dump_samples=self.dump_input.isChecked(),
			order_mode=self.order_input.currentText(),
			audio_style=self.audio_style_input.currentText(),
			trim_silence=self.trim_silence_input.isChecked(),
			normalize=self.normalize_input.isChecked(),
			fade_ms=fade_ms,
			fixed_note_length=fixed_length,
			output_sample_rate=int(self.sample_rate_input.currentText()),
			slicex_markers=self.slicex_input.isChecked(),
		)

	def refresh_generation_validation(self) -> None:
		message = ""
		enabled = True
		folder_text = self.folder_input.text().strip()
		if not folder_text:
			enabled = False
		else:
			folder = Path(folder_text)
			if not folder.is_dir():
				message = "Folder not found."
				enabled = False
			elif not list_source_files(folder):
				message = "No WAV files found."
				enabled = False

		try:
			if self.range_input.text().strip():
				value = int(self.range_input.text().strip())
				if value < 1 or value > 128:
					message = "Range must be between 1 and 128."
					enabled = False
			if self.gap_input.text().strip() and float(self.gap_input.text().strip()) < 0:
				message = "Sample gap cannot be negative."
				enabled = False
		except ValueError:
			message = "Range and sample gap must be valid numbers."
			enabled = False

		self.validation_label.setText(message)
		self.generate_button.setEnabled(enabled and self.worker is None)

	def generate(self) -> None:
		try:
			settings = self.parse_generation_settings()
		except Exception as error:
			QMessageBox.critical(self, "ChromaKit", str(error))
			return

		output_path = settings.sample_path / "chromatic.wav"
		if output_path.exists():
			answer = QMessageBox.question(
				self,
				"ChromaKit",
				"'chromatic.wav' already exists. Overwrite it?",
				QMessageBox.Yes | QMessageBox.No,
				QMessageBox.No,
			)
			if answer != QMessageBox.Yes:
				self.statusBar().showMessage("Generation cancelled.")
				return

		self.log_output.clear()
		self.progress.setRange(0, settings.semitones)
		self.progress.setValue(0)
		self.save_autosaved_options()
		self.start_worker(GenerationWorker(settings), "Generating...")

	def prepare(self) -> None:
		if not self.prepare_sources:
			QMessageBox.critical(self, "ChromaKit", "Choose WAV files or a folder first.")
			return
		output_dir = getattr(self, "prepare_output_dir", self.prepare_sources[0].parent / "prepared_samples")
		if output_dir.exists() and any(output_dir.glob("*.wav")):
			answer = QMessageBox.question(
				self,
				"ChromaKit",
				"'prepared_samples' already contains WAV files. Overwrite matching numbered files?",
				QMessageBox.Yes | QMessageBox.No,
				QMessageBox.No,
			)
			if answer != QMessageBox.Yes:
				self.statusBar().showMessage("Preparation cancelled.")
				return

		settings = PrepareSettings(
			source_paths=self.prepare_sources,
			output_dir=output_dir,
			threshold_db=float(self.threshold_input.value()),
			min_region_ms=int(self.min_region_input.value()),
			min_silence_ms=int(self.min_silence_input.value()),
			padding_ms=int(self.padding_input.value()),
			output_sample_rate=int(self.prepare_sample_rate_input.currentText()),
		)
		self.prepare_log_output.clear()
		self.prepare_progress.setRange(0, max(1, len(self.prepare_sources)))
		self.prepare_progress.setValue(0)
		self.save_autosaved_options()
		self.start_worker(PrepareWorker(settings), "Preparing samples...")

	def loop_selected_samples(self) -> None:
		if not self.loop_sources:
			QMessageBox.critical(self, "ChromaKit", "Add WAV samples or a folder first.")
			return
		output_dir = getattr(self, "loop_output_dir", self.loop_sources[0].parent / "looped_samples")
		if output_dir.exists() and any(output_dir.glob("*_loop.wav")):
			answer = QMessageBox.question(
				self, "ChromaKit", "'looped_samples' already contains looped WAV files. Overwrite matching files?",
				QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
			)
			if answer != QMessageBox.Yes:
				self.statusBar().showMessage("Looping cancelled.")
				return
		settings = LoopSettings(
			source_paths=self.loop_sources,
			output_dir=output_dir,
			crossfade_ms=int(self.loop_crossfade_input.value()),
			trim_silence=self.loop_trim_input.isChecked(),
			output_sample_rate=int(self.loop_sample_rate_input.currentText()),
			loop_start_ms=self.loop_start_input.value(),
			loop_end_ms=self.loop_end_input.value(),
			render_length_seconds=self.loop_render_length_input.value(),
			render_mode=self.loop_render_mode_input.currentText(),
		)
		self.loop_log_output.clear()
		self.loop_progress.setRange(0, max(1, len(self.loop_sources)))
		self.loop_progress.setValue(0)
		self.start_worker(LoopWorker(settings), "Creating seamless loops...")

	def start_worker(self, worker: GenerationWorker | PrepareWorker | LoopWorker, status: str) -> None:
		self.worker = worker
		worker.log.connect(self.append_log)
		worker.progress.connect(self.on_progress)
		worker.done.connect(self.on_done)
		worker.failed.connect(self.on_failed)
		worker.cancelled.connect(self.on_cancelled)
		worker.finished.connect(self.on_worker_finished)
		self.set_busy(True)
		self.statusBar().showMessage(status)
		worker.start()

	def set_busy(self, busy: bool) -> None:
		generation_inputs: Iterable[QWidget] = (
			self.folder_input,
			self.start_note_input,
			self.start_octave_input,
			self.range_input,
			self.gap_input,
			self.order_input,
			self.pitch_input,
			self.dump_input,
			self.audio_style_input,
			self.trim_silence_input,
			self.normalize_input,
			self.slicex_input,
			self.fade_input,
			self.fixed_length_input,
			self.sample_rate_input,
			self.browse_button,
			self.settings_button,
		)
		prepare_inputs: Iterable[QWidget] = (
			self.prepare_source_input,
			self.prepare_files_button,
			self.prepare_folder_button,
			self.threshold_input,
			self.min_region_input,
			self.min_silence_input,
			self.padding_input,
			self.prepare_sample_rate_input,
		)
		loop_inputs: Iterable[QWidget] = (
			self.loop_source_input, self.loop_files_button, self.loop_folder_button,
			self.loop_file_selector, self.loop_crossfade_input, self.loop_trim_input, self.loop_sample_rate_input,
			self.loop_start_input, self.loop_end_input, self.loop_auto_button, self.loop_play_source_button, self.loop_play_loop_button, self.loop_stop_button, self.loop_zero_snap_input, self.loop_render_mode_input, self.loop_render_length_input,
			*self.loop_piano_buttons,
		)
		for widget in [*generation_inputs, *prepare_inputs, *loop_inputs]:
			widget.setEnabled(not busy)
		self.cancel_button.setEnabled(busy)
		self.prepare_cancel_button.setEnabled(busy)
		self.generate_button.setEnabled(False if busy else self.generate_button.isEnabled())
		self.prepare_button.setEnabled(False if busy else bool(self.prepare_sources))
		self.loop_button.setEnabled(False if busy else bool(self.loop_sources))
		self.loop_cancel_button.setEnabled(busy)

	def cancel_worker(self) -> None:
		if self.worker:
			self.worker.request_cancel()
			self.statusBar().showMessage("Cancelling...")

	def append_log(self, message: str) -> None:
		if isinstance(self.worker, PrepareWorker):
			target = self.prepare_log_output
		elif isinstance(self.worker, LoopWorker):
			target = self.loop_log_output
		else:
			target = self.log_output
		target.append(message)
		target.moveCursor(QTextCursor.End)

	def on_progress(self, done: int, total: int, label: str) -> None:
		text = f"Note {done}/{total} - {label}" if isinstance(self.worker, GenerationWorker) else f"{done}/{total} - {label}"
		if isinstance(self.worker, PrepareWorker):
			self.prepare_progress.setRange(0, total)
			self.prepare_progress.setValue(done)
		elif isinstance(self.worker, LoopWorker):
			self.loop_progress.setRange(0, total)
			self.loop_progress.setValue(done)
		else:
			self.progress.setRange(0, total)
			self.progress.setValue(done)
		self.statusBar().showMessage(text)

	def on_done(self, output: str) -> None:
		self.last_output_path = Path(output)
		self.open_output_button.setEnabled(True)
		self.prepare_open_button.setEnabled(True)
		self.loop_open_button.setEnabled(True)
		self.statusBar().showMessage(f"Done: {output}", 8000)
		QMessageBox.information(self, "ChromaKit", f"Created {output}")

	def on_failed(self, message: str) -> None:
		self.statusBar().showMessage("Error", 8000)
		QMessageBox.critical(self, "ChromaKit", message)

	def on_cancelled(self, message: str) -> None:
		self.statusBar().showMessage(message, 8000)
		if isinstance(self.worker, PrepareWorker):
			self.prepare_progress.setValue(0)
		elif isinstance(self.worker, LoopWorker):
			self.loop_progress.setValue(0)
		else:
			self.progress.setValue(0)

	def on_worker_finished(self) -> None:
		self.worker = None
		self.set_busy(False)
		self.refresh_generation_validation()
		self.prepare_button.setEnabled(bool(self.prepare_sources))
		self.loop_button.setEnabled(bool(self.loop_sources))

	def open_last_output(self) -> None:
		if not self.last_output_path:
			return
		path = self.last_output_path
		target = path if path.is_file() else path
		try:
			if sys.platform.startswith("win"):
				if target.is_file():
					subprocess.Popen(["explorer", f"/select,{target}"])
				else:
					subprocess.Popen(["explorer", str(target)])
			else:
				subprocess.Popen(["xdg-open", str(target if target.is_dir() else target.parent)])
		except Exception as error:
			QMessageBox.warning(self, "ChromaKit", f"Could not open output: {error}")


def main() -> None:
	app = QApplication(sys.argv)
	load_brand_font()
	icon_path = asset_path("icon.ico")
	if icon_path.exists():
		app.setWindowIcon(QIcon(str(icon_path)))
	window = GeneratorWindow()
	window.show()
	sys.exit(app.exec())


if __name__ == "__main__":
	main()
