from typing import Literal

from pydantic import BaseModel, Field, model_validator

AspectPreset = Literal["source", "9:16", "16:9", "1:1", "4:5"]
CanvasMode = Literal["preset", "custom"]
CropAnchor = Literal["left", "center", "right", "top", "bottom"]


class CustomCrop(BaseModel):
    x_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    y_pct: float = Field(default=0.0, ge=0.0, le=100.0)
    w_pct: float = Field(default=100.0, ge=1.0, le=100.0)
    h_pct: float = Field(default=100.0, ge=1.0, le=100.0)

    @model_validator(mode="after")
    def _bounds(self) -> "CustomCrop":
        # Allow small float drift from UI slider rounding.
        if self.x_pct + self.w_pct > 100.05:
            raise ValueError(
                f"custom crop x+w exceeds 100 ({self.x_pct}+{self.w_pct})"
            )
        if self.y_pct + self.h_pct > 100.05:
            raise ValueError(
                f"custom crop y+h exceeds 100 ({self.y_pct}+{self.h_pct})"
            )
        return self


class Canvas(BaseModel):
    mode: CanvasMode = "preset"
    preset: AspectPreset = "source"
    crop_anchor: CropAnchor = "center"
    custom: CustomCrop = Field(default_factory=CustomCrop)
    bg_color: str = "#000000"


class Word(BaseModel):
    start: float
    end: float
    text: str


class Segment(BaseModel):
    start: float
    end: float
    text: str
    words: list[Word] = Field(default_factory=list)


TranscribeStatus = Literal["pending", "done", "cancelled", "error", "stale"]


class TranscribeResponse(BaseModel):
    video_id: str
    duration: float
    width: int
    height: int
    language: str | None = None
    segments: list[Segment]
    original_filename: str | None = None
    model: str | None = None
    is_audio_only: bool = False
    status: TranscribeStatus | None = None
    percent: int | None = None
    job_id: str | None = None
    error: str | None = None
    project: "ProjectState | None" = None


class FetchUrlRequest(BaseModel):
    url: str = Field(..., min_length=1, max_length=2048)
    language: str | None = "ru"
    model: str | None = None
    generate_subs: bool = True


class TranscriptSummary(BaseModel):
    video_id: str
    original_filename: str | None = None
    language: str | None = None
    model: str | None = None
    duration: float
    width: int
    height: int
    segments_count: int
    updated_at: float
    is_audio_only: bool = False


class UpdateSegmentsRequest(BaseModel):
    """PUT /api/transcripts/{id}. Either field optional — supports partial
    updates. Segments-only callers from before the project-state expansion
    still work."""
    segments: list[Segment] | None = None
    project: "ProjectState | None" = None


DisplayMode = Literal["phrase", "word", "karaoke"]


class Style(BaseModel):
    font_family: str = "DejaVu Sans"
    font_size: int = 48
    bold: bool = False
    italic: bool = False
    uppercase: bool = False
    text_color: str = "#FFFFFF"
    outline_color: str = "#000000"
    outline_width: int = 2
    shadow_offset: int = 0
    shadow_color: str = "#000000"
    bg_color: str = "#000000"
    bg_opacity: float = 0.0
    bg_padding: int = 8
    bg_radius: int = 0
    alignment: Literal["left", "center", "right"] = "center"
    fade_in_ms: int = 0
    fade_out_ms: int = 0
    mode: DisplayMode = "phrase"
    words_per_chunk: int = Field(default=4, ge=1, le=12)
    active_word_color: str = "#FFD400"


class Position(BaseModel):
    x_pct: float = Field(default=10.0, ge=0, le=100)
    y_pct: float = Field(default=80.0, ge=0, le=100)


class Size(BaseModel):
    w_pct: float = Field(default=80.0, ge=1, le=100)
    h_pct: float = Field(default=15.0, ge=1, le=100)


class Trim(BaseModel):
    """Keep-range for the edges of the clip. `out_sec==0` means "to end"."""
    in_sec: float = Field(default=0.0, ge=0.0)
    out_sec: float = Field(default=0.0, ge=0.0)
    # When true and an extra audio track is provided, the [in_sec..out_sec]
    # slice is looped to cover the extra audio's duration (Coub-style).
    loop: bool = False

    @model_validator(mode="after")
    def _bounds(self) -> "Trim":
        if self.out_sec > 0 and self.in_sec >= self.out_sec:
            raise ValueError(
                f"trim: in_sec {self.in_sec} >= out_sec {self.out_sec}"
            )
        return self


class ExtraTrackRef(BaseModel):
    """One row in `AudioMix.extras`. The id matches the file at
    `uploads/extra_<id>.<ext>` written by /api/extra-audio."""
    id: str = Field(..., min_length=16, max_length=16, pattern=r"^[a-f0-9]{16}$")
    volume: float = Field(default=1.0, ge=0.0, le=2.0)


class AudioMix(BaseModel):
    source_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    extras: list[ExtraTrackRef] = Field(default_factory=list)

    # Legacy single-track fields. Pre-multi-extra clients (and saved
    # projects) carry these instead of `extras`. The validator promotes
    # them once and clears the legacy slots so the rest of the backend
    # only ever reads `extras`.
    extra_audio_id: str | None = None
    extra_volume: float | None = None

    @model_validator(mode="after")
    def _absorb_legacy(self) -> "AudioMix":
        if not self.extras and self.extra_audio_id:
            self.extras = [
                ExtraTrackRef(
                    id=self.extra_audio_id,
                    volume=self.extra_volume if self.extra_volume is not None else 1.0,
                )
            ]
        # Don't echo legacy slots back to callers / persistence.
        self.extra_audio_id = None
        self.extra_volume = None
        return self


# Subtitle track is "source" or any extra_audio_id present in audio.extras.
# Kept as a plain string in pydantic so id values aren't restricted.
SubtitleTrack = str


class ProjectState(BaseModel):
    """Full editor state that should survive a reload and be restored when
    reopening a project from the Sidebar. Cosmetic / editorial fields only —
    nothing here should ever invalidate whisper cache_key."""
    style: Style | None = None
    position: Position | None = None
    size: Size | None = None
    canvas: Canvas | None = None
    trim_range: Trim | None = None
    audio: AudioMix | None = None
    use_subs: bool | None = None
    display_mode: str | None = None
    updated_at: float | None = None
    # Persisted per-track. Old projects stored this as a flat list (single
    # extra track); new ones use a {extra_id: list[Segment]} dict. Both
    # forms are accepted on read; new writes always use the dict form.
    extra_segments: dict[str, list[Segment]] | list[Segment] | None = None
    subtitle_track: SubtitleTrack | None = None


UpdateSegmentsRequest.model_rebuild()
TranscribeResponse.model_rebuild()


class ExtraAudioResponse(BaseModel):
    extra_audio_id: str
    duration: float
    name: str


ExportFormat = Literal["mp4", "gif"]
GifQuality = Literal["low", "medium", "high"]


class ExportRequest(BaseModel):
    video_id: str
    segments: list[Segment]
    style: Style
    position: Position
    size: Size
    trim_silences: bool = False
    silence_threshold_sec: float = Field(default=0.4, ge=0.05, le=5.0)
    silence_padding_sec: float = Field(default=0.08, ge=0.0, le=1.0)
    canvas: Canvas = Field(default_factory=Canvas)
    trim: Trim = Field(default_factory=Trim)
    audio: AudioMix = Field(default_factory=AudioMix)
    format: ExportFormat = "mp4"
    gif_quality: GifQuality = "medium"
    watermark: bool = True
    # Which transcript drives burned-in subtitles. "source" = whisper on the
    # original video; "extra" = whisper on the uploaded extra audio track.
    # The caller pre-resolves `segments` to the chosen track's timings; this
    # flag controls expansion under trim.loop=true (source segments need
    # repetition across each loop iteration, extra segments ride the master
    # extra-audio timeline as-is).
    subtitle_track: SubtitleTrack = "source"


class ExportResponse(BaseModel):
    video_id: str
    output_path: str
    output_format: ExportFormat = "mp4"
    original_duration: float | None = None
    output_duration: float | None = None
    cuts: list[tuple[float, float]] | None = None
