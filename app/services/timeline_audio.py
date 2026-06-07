from __future__ import annotations

import wave
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Qt, Signal
from PySide6.QtMultimedia import QAudioFormat, QAudioSink, QMediaDevices


class TimelineAudioRenderer(QObject):
    playbackStateChanged = Signal(bool)
    positionChanged = Signal(float)
    playbackFinished = Signal()
    errorOccurred = Signal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._path: Path | None = None
        self._wave: wave.Wave_read | None = None
        self._sink: QAudioSink | None = None
        self._device = None
        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.TimerType.PreciseTimer)
        self._timer.setInterval(10)
        self._timer.timeout.connect(self._write_available)
        self._channels = 0
        self._sample_rate = 0
        self._sample_width = 0
        self._playing = False
        self._finished = False
        self._play_start_frame = 0

    def set_source(self, path: Path | None) -> None:
        self.stop()
        self._path = Path(path).resolve() if path else None
        if self._path is not None and not self._path.is_file():
            self._path = None

    def play(self, seconds: float = 0.0) -> None:
        if self._path is None:
            return
        try:
            self._open_wave()
            self.seek(seconds)
            if self._sink is not None:
                self._sink.stop()
                self._sink.deleteLater()
                self._sink = None
                self._device = None
            self._create_sink()
            if self._sink is None:
                return
            self._play_start_frame = self._wave.tell() if self._wave is not None else 0
            self._device = self._sink.start()
            self._playing = True
            self._finished = False
            self._timer.start()
            self.playbackStateChanged.emit(True)
        except Exception as e:
            self.errorOccurred.emit(f"WAV 재생 준비 실패: {e}")
            self.stop()

    def pause(self) -> None:
        self._timer.stop()
        if self._sink is not None:
            self._sink.suspend()
        self._playing = False
        self.playbackStateChanged.emit(False)

    def stop(self) -> None:
        self._timer.stop()
        if self._sink is not None:
            self._sink.stop()
            self._sink.deleteLater()
        self._sink = None
        self._device = None
        if self._wave is not None:
            self._wave.close()
        self._wave = None
        self._play_start_frame = 0
        was_playing = self._playing
        self._playing = False
        if was_playing:
            self.playbackStateChanged.emit(False)

    def seek(self, seconds: float) -> None:
        if self._wave is None:
            self._open_wave()
        if self._wave is None or self._sample_rate <= 0:
            return
        frame = max(0, int(float(seconds or 0.0) * self._sample_rate))
        frame = min(frame, self._wave.getnframes())
        self._wave.setpos(frame)
        self.positionChanged.emit(frame / self._sample_rate)

    def is_playing(self) -> bool:
        return self._playing

    def _open_wave(self) -> None:
        if self._path is None:
            return
        if self._wave is not None:
            return
        wf = wave.open(str(self._path), "rb")
        channels = wf.getnchannels()
        sample_width = wf.getsampwidth()
        sample_rate = wf.getframerate()
        if channels not in (1, 2):
            wf.close()
            raise ValueError(f"지원하지 않는 채널 수: {channels}")
        if sample_width != 2:
            wf.close()
            raise ValueError(f"현재는 16-bit PCM WAV만 지원합니다. sample_width={sample_width}")
        if sample_rate <= 0:
            wf.close()
            raise ValueError("WAV sample rate가 올바르지 않습니다.")
        self._wave = wf
        self._channels = channels
        self._sample_width = sample_width
        self._sample_rate = sample_rate

    def _create_sink(self) -> None:
        fmt = QAudioFormat()
        fmt.setSampleRate(self._sample_rate)
        fmt.setChannelCount(self._channels)
        fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
        device = QMediaDevices.defaultAudioOutput()
        if not device.isFormatSupported(fmt):
            preferred = device.preferredFormat()
            if (
                preferred.sampleFormat() != QAudioFormat.SampleFormat.Int16
                or preferred.channelCount() not in (1, 2)
            ):
                raise ValueError("기본 오디오 장치가 16-bit PCM 출력을 지원하지 않습니다.")
            fmt = preferred
        self._sink = QAudioSink(device, fmt, self)
        bytes_per_second = self._sample_rate * self._channels * self._sample_width
        self._sink.setBufferSize(max(16384, bytes_per_second // 2))

    def _write_available(self) -> None:
        if self._wave is None or self._sink is None or self._device is None:
            self.stop()
            return
        bytes_free = self._sink.bytesFree()
        if bytes_free <= 0:
            return
        frame_size = self._channels * self._sample_width
        frames = max(1, bytes_free // frame_size)
        data = self._wave.readframes(frames)
        if not data:
            self._finished = True
            self.positionChanged.emit(self._wave.getnframes() / self._sample_rate)
            self.stop()
            self.playbackFinished.emit()
            return
        self._device.write(data)
        played_seconds = self._play_start_frame / self._sample_rate
        played_seconds += max(0, self._sink.processedUSecs()) / 1_000_000.0
        duration_seconds = self._wave.getnframes() / self._sample_rate
        self.positionChanged.emit(min(duration_seconds, played_seconds))
