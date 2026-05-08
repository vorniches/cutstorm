# Multi Extra Audio Tracks — Implementation Plan

Документ самодостаточен. Чужой LLM/инженер должен суметь по нему сделать фичу, не задавая вопросов автору.

---

## 0. Что это за фича (одним абзацем)

Сейчас под видео висит ОДНА extra-аудиодорожка (`audio.extra_audio_id` на бэке, `audio.extraAudioId` на фронте). Делаем **много дорожек на спрос**: пользователь жмёт «+ Add audio track», грузит файл, получает новый трек со своими ползунком громкости / кнопкой «Generate subs» / иконкой удаления — параллельно с уже подгруженными. У каждого трека свой набор сабов из своего whisper-прохода. В редакторе сабов вкладок столько же сколько треков (плюс «Source»). Loop-режим использует один трек как «driver длительности» (по умолчанию первый). Всё остальное (cancel, прогресс-бар, фоллбэк url, серверный ffmpeg-mix) расширяется с одного трека на массив.

Аналог: GarageBand / любой DAW с N-ными аудиоканалами.

---

## 1. Что было обсуждено и согласовано (контекст для пустого чата)

Это ограничения, которые согласованы с пользователем. Не отступать.

1. **Per-track всё ручное.** Громкость, mute, выбор источника сабов — никакой магии «при добавлении трека вырубить остальные».
2. **Whisper per track.** Каждый трек транскрибится отдельно по кнопке. Запуск whisper НЕ автоматический.
3. **Один whisper за раз.** Если юзер на одном треке нажал Generate, потом на другом — второй preempt'ит первый (это уже реализовано на бэке через `_extra_transcribe_cancels`).
4. **Loop driver — первый трек в массиве.** Если первый удалён, loop выключается с тостом «loop track removed». Не делаем UI-флажка «I'm the loop driver» в первой версии — позже.
5. **Активная вкладка сабов** хранится как `subtitleTrack: "source" | <extra_id>` — конкретный id, а не «extra». При удалении трека, чьи сабы активны, фоллбэк на `"source"`.
6. **Source-аудио** видео — отдельная сущность, как и было. Громкость регулируется отдельно (`sourceVolume`). НЕ путаем source с extras.
7. **Backwards compat для проектов с одной extra-дорожкой.** Старая форма `extra_audio_id`/`extraAudioId` (один) принимается на бэке и фронте — конвертируется в массив из одного элемента. Новые проекты пишутся уже в новой форме.
8. **Никаких архитектурных революций.** Whisper-эндпоинты, cancel-эндпоинты, /api/extra-audio/{id}, /info, WS-фразы `extra_*` — НЕ меняются. Они уже per-id. Просто расширяются места, где «один → массив».
9. **Тестовая среда.** Сохранёнными meta.json пользователей можно жертвовать, но не лениться: при наличии legacy формы — корректно подгрузить.

---

## 2. Что пощупать в проекте ПЕРЕД написанием кода

### Backend

| Файл | Что там |
|------|---------|
| [backend/app/models.py](../backend/app/models.py) | `AudioMix`, `ProjectState`, `ExportRequest`. Добавить `extras: list[ExtraTrackRef]` + сохранить legacy поля как fallback. |
| [backend/app/main.py](../backend/app/main.py) | `_referenced_extra_ids` (текущая читает только `audio.extra_audio_id`); `api_export` (резолв extra_audio_path, loop_active, передача в renderer/simple_export); `_clip_segments_to_trim` гейт на `subtitle_track != "extra"`. |
| [backend/app/renderer.py](../backend/app/renderer.py) | `_ffmpeg_cmd_video` audio_chain — единственный extra `[2:a]`. Расширить на N (`[2:a]`, `[3:a]`, ...). amix N inputs. apad на каждый. |
| [backend/app/simple_export.py](../backend/app/simple_export.py) | Аналогично — `[1:a]` для extra → стало бы `[1:a]`, `[2:a]`, ... watermark тогда уезжает на индекс N+1. |
| [backend/app/loop_segments.py](../backend/app/loop_segments.py) | НЕ меняется. |

### Frontend

| Файл | Что там |
|------|---------|
| [frontend/src/store.ts](../frontend/src/store.ts) | Тип `AudioConfig`, `subtitleTrack`, `segmentsExtra*`, миграция v8→v9, экшены `appendExtraSegment` / `setExtraSegments` (теперь по id), `setExtraTrack` (add/update/remove). |
| [frontend/src/components/Timeline.tsx](../frontend/src/components/Timeline.tsx) | `<ExtraTrack>` сейчас один. Сделать `extras.map(...)` + footer-кнопка «+ Add track». Каждому treck'у пробросить `track` объект. |
| [frontend/src/components/SegmentList.tsx](../frontend/src/components/SegmentList.tsx) | Текущие табы `subtitle-track-source` / `subtitle-track-extra` — заменить на динамические таб-баттоны (Source + по треку). |
| [frontend/src/components/VideoPreview.tsx](../frontend/src/components/VideoPreview.tsx) | `attachAudioMix(v, oneUrl)` → `attachAudioMix(v, extras: ExtraTrackPlaybackInfo[])`. Loop-rAF использует «driver track». |
| [frontend/src/audioMix.ts](../frontend/src/audioMix.ts) | Граф из `srcNode + один extraNode` → `srcNode + Map<id, extraNode/gain/el>`. `syncExtraToVideo`/`syncVideoToLoopedExtra` — обходят все треки. |
| [frontend/src/components/PreviewToolbar.tsx](../frontend/src/components/PreviewToolbar.tsx) | Стоп/scrub/Play в loop — таргетим первый extra (driver). |
| [frontend/src/components/SubtitleOverlay.tsx](../frontend/src/components/SubtitleOverlay.tsx) | Чтение `audio.extras[0].duration` для loop master clock вместо `audio.extraAudioDuration`. |
| [frontend/src/extraBlobs.ts](../frontend/src/extraBlobs.ts) | По id уже работает, не меняется. |
| [frontend/src/api.ts](../frontend/src/api.ts) | `exportVideo` отправляет `audio.extras` массив. `transcribeExtra` уже per-id — не меняется. |
| [frontend/src/autosave.ts](../frontend/src/autosave.ts) | `projectSnapshot` пишет новую форму `extras` + map `extra_segments`. |
| [frontend/src/progress.ts](../frontend/src/progress.ts) | Уже маршрутит по `extra_audio_id` из ws-сообщения. Поправить пуш сегмента: `appendExtraSegment(seg, idx, extraId)`. |
| [frontend/src/components/Uploader.tsx](../frontend/src/components/Uploader.tsx) | Не меняется. |

### Тесты

| Файл | Что там |
|------|---------|
| [backend/tests/test_trim_and_audio.py](../backend/tests/test_trim_and_audio.py) | `AudioMix` валидаторы. Новый тест: `extras` массив парсится из и legacy и новой формы. |
| [backend/tests/test_export_dispatch.py](../backend/tests/test_export_dispatch.py) | Спай `run_filter_only`/`render_export`. Новый тест: 2+ extras — kwargs содержат массив. |
| [backend/tests/test_loop_export.py](../backend/tests/test_loop_export.py) | Loop с одним extra. Дополнить: loop с 2 extras. |
| [frontend/e2e/13-loop-mode.spec.ts](../frontend/e2e/13-loop-mode.spec.ts) | Loop-сценарии. Не трогать (single track loop остаётся валидным).|
| [frontend/e2e/11-trim-and-audio.spec.ts](../frontend/e2e/11-trim-and-audio.spec.ts) | Migration v2→v8 уже там. Дополнить v8→v9. |
| frontend/e2e/14-multi-extra.spec.ts (НОВЫЙ) | Полный сценарий: добавить 2 трека, у каждого свой volume, transcribe каждого, переключатель сабов между ними, экспорт. |

---

## 3. Что нужно сделать (high-level)

### 3.1. Модель данных

**Бэкенд (`models.py`):**
```python
class ExtraTrackRef(BaseModel):
    id: str = Field(..., min_length=16, max_length=16)
    volume: float = Field(default=1.0, ge=0.0, le=2.0)


class AudioMix(BaseModel):
    source_volume: float = Field(default=1.0, ge=0.0, le=2.0)
    extras: list[ExtraTrackRef] = Field(default_factory=list)

    # Legacy fields — kept for backwards compat with already-saved
    # projects. If `extras` is empty AND the legacy fields are set, the
    # validator promotes them to a single-element list.
    extra_audio_id: str | None = None
    extra_volume: float | None = None

    @model_validator(mode="after")
    def _migrate_legacy(self) -> "AudioMix":
        if not self.extras and self.extra_audio_id:
            self.extras = [ExtraTrackRef(
                id=self.extra_audio_id,
                volume=self.extra_volume if self.extra_volume is not None else 1.0,
            )]
        # After migration, do not echo legacy fields back to the caller.
        self.extra_audio_id = None
        self.extra_volume = None
        return self
```

**В `ProjectState`:**
```python
extra_segments: dict[str, list[Segment]] | list[Segment] | None = None
# Accept BOTH:
#   list  — legacy single-track form (associated with audio.extra_audio_id);
#   dict  — new multi-track form keyed by extra_audio_id.
```

**В `ExportRequest`:**
```python
subtitle_track: str = "source"
# "source" or any extra_audio_id present in audio.extras. Backend looks
# the id up in audio.extras to know which track's segments are passed in
# `segments`. If the id is not found in extras, falls back to "source".
```

**Фронтенд (`store.ts`, версия v9):**
```ts
export type ExtraTrack = {
  id: string;
  name: string | null;
  duration: number;
  volume: number;
};

export type AudioConfig = {
  sourceVolume: number;
  extras: ExtraTrack[];          // ordered, first = loop driver
};

type State = {
  // ...
  segmentsExtra: Record<string, Segment[]>;   // keyed by extra id
  subtitleTrack: "source" | string;            // "source" or an extra id
  extraSubsStreamingId: string | null;         // id currently transcribing
  // ...
};
```

Миграция v8 → v9: см. раздел 3.5.

### 3.2. Backend: `_referenced_extra_ids` сканит массив

```python
def _referenced_extra_ids() -> set[str]:
    ids: set[str] = set()
    for f in UPLOADS_DIR.glob("*.json"):
        try:
            data = json.loads(f.read_text())
        except Exception:
            continue
        a = (data.get("project") or {}).get("audio") or {}
        # New form
        for e in a.get("extras") or []:
            eid = e.get("id") if isinstance(e, dict) else None
            if eid:
                ids.add(eid)
        # Legacy form
        legacy = a.get("extra_audio_id")
        if legacy:
            ids.add(legacy)
    return ids
```

Тест: `test_referenced_ids_handles_both_forms`.

### 3.3. Backend: ffmpeg N-ный микс

В [renderer.py](../backend/app/renderer.py) `_ffmpeg_cmd_video`:

- Вход 0 — source video.
- Вход 1 — image2pipe (overlay PNG).
- Входы 2..2+N-1 — extras (по одному на трек).
- Вход 2+N — watermark PNG (если включён).

Filter chain (audio):
```
если source_has_audio:  src_chain = [0:a]volume=src_vol[a_src]
для каждого e_i (i=0..N-1): [2+i:a]volume={e_i.volume},apad[a_e_i]
inputs = [a_src?] + [a_e_0, a_e_1, ...]
amix=inputs={len(inputs)}:duration=longest:normalize=0[a]
```

Если `len(inputs) == 1` — `amix` лишний, просто `[apad]asetpts=N/SR/TB[a]`.

В loop-режиме первый extra — driver: `loop_total_duration = probe(extras[0].path).duration`. Source-аудио и **только первый** extra прогоняются через `aloop`/`atrim` до driver-длины. Остальные extras игнорируют loop, идут через `apad` до video-длины (которая = driver-длина) — естественно совпадают.

В [simple_export.py](../backend/app/simple_export.py) аналогичная развёрнутость, но БЕЗ image2pipe (вход 0 — source video, входы 1..N — extras, вход N+1 — watermark).

**Граничные случаи:**
- N=0 (нет extras): пути сохраняются как сейчас (source-only / silent).
- N=1: один extra — поведение должно быть БИТ-В-БИТ как до фичи (регрессия check). amix=1 → пропускаем amix, прямой mapping.
- source_has_audio=False, N=1, loop=False: текущее поведение (`apad` уже добавил в фиксе).
- source_has_audio=False, N=2, loop=False: amix=2 (без source), оба через apad.

### 3.4. Backend: api_export резолв и ошибки

В `api_export`:
```python
# Resolve extras list to (path, volume) tuples; drop missing files,
# warn for each, raise 410 only if loop=true and the FIRST track is
# missing (it's the driver — without it loop has no length).
resolved_extras: list[tuple[Path, float]] = []
missing_ids: list[str] = []
for e in req.audio.extras:
    p = _find_extra_audio(e.id)
    if p is None:
        missing_ids.append(e.id)
        continue
    resolved_extras.append((p, e.volume))
if missing_ids:
    log.warning("export.extras_missing ids=%s", missing_ids)
if req.trim.loop and (not req.audio.extras or not resolved_extras
                     or req.audio.extras[0].id in missing_ids):
    raise HTTPException(410, "Loop track is missing on the server. "
                             "Re-upload it before exporting.")
```

`subtitle_track` resolve:
```python
if req.subtitle_track != "source":
    if req.subtitle_track not in {e.id for e in req.audio.extras}:
        log.warning("export.unknown_subtitle_track id=%s — falling back to source",
                    req.subtitle_track)
        # treat as source for purposes of expansion / clipping
        effective_subtitle_track = "source"
    else:
        effective_subtitle_track = req.subtitle_track
else:
    effective_subtitle_track = "source"
```

`expand_loop_segments` — без изменений, вызывается только при `effective_subtitle_track == "source" && loop_active`.

`_clip_segments_to_trim` — гейт остаётся: вызывается только при `effective_subtitle_track == "source"`.

### 3.5. Frontend store: миграция v8 → v9

```ts
if (version < 9) {
  const a = (p.audio as any) ?? {};
  const legacyId = a.extraAudioId as string | null | undefined;
  const legacyName = a.extraAudioName as string | null | undefined;
  const legacyDuration = a.extraAudioDuration as number | undefined;
  const legacyVolume = a.extraVolume as number | undefined;
  const extras: ExtraTrack[] = legacyId
    ? [{
        id: legacyId,
        name: legacyName ?? null,
        duration: legacyDuration ?? 0,
        volume: typeof legacyVolume === "number" ? legacyVolume : 1.0,
      }]
    : [];
  p.audio = {
    sourceVolume: typeof a.sourceVolume === "number" ? a.sourceVolume : 1.0,
    extras,
  };
  // segmentsExtra: array → record, keyed by legacyId if present.
  const oldExtraSegs = (p as any).segmentsExtra;
  if (Array.isArray(oldExtraSegs)) {
    p.segmentsExtra = legacyId ? { [legacyId]: oldExtraSegs } : {};
  } else if (oldExtraSegs == null) {
    p.segmentsExtra = {};
  }
  // subtitleTrack: "extra" → legacyId; "source" stays.
  if ((p as any).subtitleTrack === "extra") {
    p.subtitleTrack = legacyId ?? "source";
  }
  p.extraSubsStreamingId = (p as any).extraSubsStreaming ? legacyId ?? null : null;
}
```

`partialize`: persist полей `audio.extras`, `segmentsExtra` (как Record), `subtitleTrack`, `extraSubsStreamingId`.

### 3.6. Store actions

Заменить:
- `setAudio` (общий) — оставить для `sourceVolume`.
- `setExtraTrack(id, patch)` — обновить трек по id.
- `addExtraTrack(track)` — добавить новый.
- `removeExtraTrack(id)` — удалить трек, удалить `segmentsExtra[id]`, если активная вкладка сабов = id → fallback на "source", если loop=ON и удалён первый → выключить loop + setError(...).
- `setExtraSegments(id, segs)` — записать сабы для трека.
- `appendExtraSegment(id, seg, index)` — стрим сегментов.
- `setSubtitleTrack(t)` — теперь принимает `"source" | id`. Селектор активных сегментов:
  ```ts
  function getActiveSegments(s: State): Segment[] {
    if (s.subtitleTrack === "source") return s.segmentsSource;
    return s.segmentsExtra[s.subtitleTrack] ?? [];
  }
  ```
- `setExtraSubsStreamingId(id | null)` — взамен булева `extraSubsStreaming`.

`segments` (top-level алиас) обновляется реактивно через сеттеры.

### 3.7. AudioMix multi-track

Тип `NodeSet`:
```ts
type ExtraNode = {
  id: string;
  el: HTMLAudioElement;
  node: MediaElementAudioSourceNode;
  gain: GainNode;
};
type NodeSet = {
  ctx: AudioContext;
  srcNode: MediaElementAudioSourceNode;
  srcGain: GainNode;
  extras: Map<string, ExtraNode>;
};
```

`attachAudioMix(videoEl, extras: { id, url, volume }[])`:
1. Получить или создать `ctx`, `srcNode`, `srcGain` (как сейчас).
2. Для каждого `id` из старого state: если его нет в новом — disconnect+remove.
3. Для каждого нового extra: если уже есть — обновить gain. Если нет — создать `<audio>` элемент с `src=url`, `MediaElementSource`, `Gain`, connect в `ctx.destination`.
4. Сохранить `current = { ..., extras: Map }`.
5. Дебаг-handle: `(window as any).__cutstorm_mix = current` (тесты используют `mix.extras.get(id).el.currentTime`).

`syncExtraToVideo(video, trimIn)`:
- Для каждого `extras.values()` — повторить старую логику (set `currentTime`, play/pause).

`syncVideoToLoopedExtra(video, trimIn, loopClipDuration, driverId)`:
- Master = `extras.get(driverId).el.currentTime`.
- Видео seek-ается в `trimIn + master % loopClipDuration` как сейчас.
- Остальные extras: синкаются с master, не с video. Тоже seek `extra.currentTime = master` (если расхождение > 0.15с).

### 3.8. UI: Timeline — список треков + кнопка add

Псевдо-JSX:
```tsx
{audio.extras.map((track, i) => (
  <ExtraTrack
    key={track.id}
    track={track}
    isLoopDriver={i === 0}
    onUpdate={(patch) => setExtraTrack(track.id, patch)}
    onRemove={() => removeExtraTrack(track.id)}
    duration={duration}
  />
))}
<button
  data-testid="extra-track-add"
  onClick={() => fileInputRef.current?.click()}
  className="track-row track-row-empty"
>
  + Add audio track
</button>
<input ref={fileInputRef} type="file" data-testid="extra-file-input" ... />
```

`ExtraTrack` уже есть, нужно:
- передавать `track` объект явно
- testid'ы делать с id-суффиксом: `extra-track-${track.id}`, `extra-volume-${track.id}`, `extra-transcribe-button-${track.id}`, `extra-track-remove-${track.id}`.
- Если `isLoopDriver` — показывать маленький бэйдж «Loop».

Кнопка add — отдельный компонент или встроенная footer-row.

### 3.9. UI: SegmentList табы — динамические

```tsx
<div className="subtitle-track-tabs">
  <button
    data-testid="subtitle-track-source"
    className={`subtitle-track-tab${subtitleTrack === "source" ? " active" : ""}`}
    onClick={() => setSubtitleTrack("source")}
  >
    Source <span>{segmentsSource.length}</span>
    {subsStreaming && <span className="subtitle-track-dot" />}
  </button>
  {audio.extras.map((track) => {
    const segs = segmentsExtra[track.id] ?? [];
    const isStreaming = extraSubsStreamingId === track.id;
    const enabled = segs.length > 0 || isStreaming;
    return (
      <button
        key={track.id}
        data-testid={`subtitle-track-extra-${track.id}`}
        disabled={!enabled}
        className={`subtitle-track-tab${subtitleTrack === track.id ? " active" : ""}`}
        onClick={() => setSubtitleTrack(track.id)}
        title={track.name ?? track.id}
      >
        {track.name ?? "extra"} <span>{segs.length}</span>
        {isStreaming && <span className="subtitle-track-dot" />}
      </button>
    );
  })}
</div>
```

### 3.10. WS-progress.ts

Уже маршрутится по `extra_audio_id` из сообщения. Поправить:
```ts
if (msg?.phase === "extra_segment" && msg.segment && typeof msg.index === "number") {
  const id: string = msg.extra_audio_id;
  store.appendExtraSegment(id, msg.segment, msg.index);
  if (typeof msg.percent === "number") store.setProgress("transcribe", msg.percent);
  store.setExtraSubsStreamingId(id);
  return;
}
if (msg?.phase === "extra_transcribe_done" || msg?.phase === "extra_transcribe_cancelled" || msg?.phase === "extra_transcribe_error") {
  store.setExtraSubsStreamingId(null);
  // ...
}
```

### 3.11. API client (`api.ts`)

`exportVideo` сериализует:
```ts
audio: {
  source_volume: state.audio.sourceVolume,
  extras: state.audio.extras.map(e => ({ id: e.id, volume: e.volume })),
}
```

`subtitle_track` уходит как есть (`"source"` или id).

### 3.12. Autosave

`projectSnapshot`:
```ts
audio: {
  source_volume: s.audio.sourceVolume,
  extras: s.audio.extras.map(e => ({ id: e.id, volume: e.volume })),
},
extra_segments: s.segmentsExtra, // dict {id: Segment[]}
subtitle_track: s.subtitleTrack,
```

`loadProject` (в store): принимать обе формы (`extra_segments` массив или dict).

---

## 4. Список новых testid'ов (для Playwright)

| testid | Где | Что делает |
|--------|-----|-----------|
| `extra-track-${id}` | Timeline | Корневой div трека |
| `extra-volume-${id}` | внутри трека | Слайдер громкости |
| `extra-transcribe-button-${id}` | внутри трека | Generate subs |
| `extra-transcribe-cancel-${id}` | внутри трека | Cancel в режиме streaming |
| `extra-track-remove-${id}` | внутри трека | × |
| `extra-track-add` | под последним треком | Кнопка добавить новый |
| `subtitle-track-extra-${id}` | SegmentList | Таб для трека |
| `loop-driver-badge-${id}` | внутри трека (если first) | Бэйдж «Loop» |

Старый `extra-transcribe-button` (без id) — оставить как алиас на `extras[0]` если ровно один трек, чтобы старые тесты `13-loop-mode.spec.ts` не сломались.

---

## 5. Тестирование

Цель: ничего не выкатывается без зелёных тестов.

### 5.1. Backend pytest

**Расширить `test_trim_and_audio.py`:**
- `test_audiomix_accepts_legacy_form` — `AudioMix(extra_audio_id="aaaa...", extra_volume=0.5)` → `extras` равен `[{id: aaaa..., volume: 0.5}]`, legacy поля очищены.
- `test_audiomix_accepts_new_form` — `AudioMix(extras=[{id:..., volume: 0.7}, {id:..., volume: 1.2}])` сохраняет 2 трека.
- `test_audiomix_legacy_loses_to_new` — если переданы и legacy и `extras` — legacy игнорится.

**Новый `test_multi_extra_export.py`:**
- 2 extras + source video → ffmpeg cmd содержит inputs `[2:a]`, `[3:a]` и `amix=inputs=N`.
- 1 extra (через `extras=[{...}]`) → выход идентичен старому single-track пути (контракт сохранён).
- 0 extras → старый путь (`-an` или source-only audio).
- Loop+2 extras: `loop_total_duration = duration of extras[0]`. Driver — первый. Второй extra только padит, не loop'ится.
- subtitle_track: id корректно резолвится в один из extras и пропускает clip; неизвестный id фоллбэкает на source с warning в лог.

**Расширить `test_export_dispatch.py`:**
- 2 extras + has_overlay → `render_export` вызывается, kwargs `extras=[...]` массив.
- 0 extras + watermark → `filter_only` (как сейчас).
- 2 extras без overlay → `filter_only`, kwargs `extras=[...]`.

**Расширить `test_loop_export.py` real ffmpeg:**
- 2 extras (короткий + длинный) loop=true: output длится как первый extra (driver), второй отрезан / padded — длительность = extras[0].

**Все существующие тесты должны остаться зелёными** — это main contract.

### 5.2. Frontend Playwright

**Новый `frontend/e2e/14-multi-extra.spec.ts`:**

```ts
test("add two extra tracks, both visible with own controls", async ({ page }) => {
  // upload sample, add tone1 (5s) via add-button, then add tone2 (3s).
  // Assert extra-track-${id1} and extra-track-${id2} both visible,
  // each with own volume slider, transcribe button, remove button.
  // Assert extra-track-add button is still visible (can keep adding).
});

test("removing a track also removes its subtitle tab", async ({ page }) => {
  // Add 2 extras, transcribe-extra on both, switch sub-tab to second's id,
  // delete second track. Assert subtitleTrack falls back to "source",
  // segmentsExtra[secondId] gone, subtitle-track-extra-${secondId} not present.
});

test("loop uses first extra as driver", async ({ page }) => {
  // tone1=4s tone2=10s. Loop=ON. Export. ffprobe duration ≈ 4s
  // (driver is first). Reorder is not in v1, but if first is removed,
  // loop should auto-disable.
});

test("removing first track in loop mode disables loop and surfaces toast", async ({ page }) => {
  // Add 2 extras, loop ON. Remove extras[0]. Assert loop-toggle is now off,
  // error-toast appears, audio.extras length is now 1 (the former second).
});

test("two transcribes in a row preempt each other", async ({ page }) => {
  // Add 2 extras. Click transcribe on first. Immediately click on second.
  // Verify only second is streaming; first's stream stopped early.
  // segmentsExtra[firstId] may be partial (≥0). segmentsExtra[secondId]
  // grows.
});

test("export mixes source + 2 extras correctly", async ({ page }) => {
  // Source has audio. 2 extras with different volumes. Export.
  // ffprobe -i out.mp4: duration ≈ source duration; audio stream codec=aac.
  // Read first second of audio with ffmpeg -af volumedetect; mean_volume
  // should reflect mix (sanity, ±3dB).
});

test("v8→v9 migration: legacy single extra survives", async ({ page }) => {
  // Seed v8 localStorage state with extraAudioId="aaaa...", extraVolume=0.7,
  // segmentsExtra=[{...}], subtitleTrack="extra". Reload. Assert:
  //   audio.extras = [{id: aaaa..., volume: 0.7, ...}],
  //   segmentsExtra = {aaaa...: [{...}]},
  //   subtitleTrack = "aaaa...".
});
```

**Расширить `13-loop-mode.spec.ts`:**
- Тест «loop=ON + extra» уже стоит — оставить как «loop с одним extra»; этот сценарий должен проходить идентично.

**Не трогать `11-trim-and-audio.spec.ts`** — там contract «1 extra работает».

### 5.3. Ручной smoke (перед маркировкой «готово»)

1. `docker-compose up`, `http://localhost:8000`.
2. Загрузить 30-секундный говорящий клип. Дождаться сабов.
3. `+ Add audio track` → загрузить 5-сек подкаст-обрывок (extras[0]).
4. Снова `+ Add audio track` → загрузить 30-сек инструменталку (extras[1]).
5. У extras[0] поставить громкость 50%, у extras[1] — 70%. Превью: всё слышно как ожидаешь.
6. На extras[0] нажать Generate subs. Дождаться. Переключиться в редакторе сабов на таб этого трека — увидеть стрим.
7. Пока крутится — нажать Generate subs на extras[1]. Прежний должен прерваться (extra-transcribe-cancel state). Затем второй стартует.
8. Source-громкость в 0. Слышно только extras.
9. Удалить extras[0] (×). Loop был выключен — ОК. Проверить что таб с его сабами исчез.
10. Включить loop (extras[1] стал первым → driver). Превью крутит видео под extras[1] (30 сек если оно длиннее trim).
11. Export. Длина файла = 30с (extras[1] driver). Звук в наушниках = extras[1].
12. Hard reload. Загруженные extras сохраняются (server fallback URL), длительность отображается правильно (через /info).

---

## 6. Acceptance criteria (что значит «готово»)

- [ ] Все существующие pytest зелёные (нет регрессий).
- [ ] Все существующие Playwright тесты зелёные.
- [ ] Новый `test_multi_extra_export.py` весь зелёный.
- [ ] Новый `14-multi-extra.spec.ts` весь зелёный.
- [ ] Migration v8→v9 в `11-trim-and-audio.spec.ts` (или `14-multi-extra.spec.ts`) проходит — старые сохранёнки не теряются.
- [ ] Ручной smoke (раздел 5.3) пройден от и до.
- [ ] Single-extra export (через `extras=[{...}]` с одним элементом) даёт байт-в-байт-эквивалентный output по сравнению с предыдущей версией (на одном и том же видео+аудио). Можно проверить ffprobe `-show_entries stream=codec_name,duration,bit_rate` — все совпадают.
- [ ] Удаление трека в loop-режиме (он был driver) корректно отключает loop без падения.
- [ ] WebAudio: 3 extras одновременно играют без артефактов (ручная проверка с наушниками).

---

## 7. Out of scope (НЕ делаем в этой задаче)

- Drag-n-drop переупорядочивание треков. Только append/remove.
- Solo/Mute кнопки рядом с громкостью. Громкость 0 = solo вручную.
- Запись звука с микрофона прямо в трек.
- Mixing presets, automation envelopes — даже не упоминать.
- Multiple loop drivers (микс зацикленных треков разной длины). Один driver — первый.
- Раздельный whisper-модель per-track. Все экстры используют тот же дефолтный model_size, который уже задан в env / store.
- Параллельный whisper на нескольких треках одновременно. Только последовательный с preempt'ом.
- UI выбор «кто из экстра сейчас loop driver». Жёстко первый. Можно потом.

---

## 8. Порядок реализации (предлагаемый)

1. **Backend модель** + legacy migrator + pytest на `AudioMix` валидаторы. Зелёные.
2. **Backend renderer** N-mix + apad + amix=longest. Тест на ffmpeg cmd shape (без реального ffmpeg). Зелёные.
3. **Backend simple_export** N-mix. Тесты. Зелёные.
4. **Backend api_export** резолв extras/subtitle_track + расширение `_referenced_extra_ids`. Тесты dispatch. Зелёные.
5. **Backend real-ffmpeg loop+2-extras** integration test. Зелёный.
6. **Backend полный pytest**. Зелёный.
7. **Frontend store v9 + actions** + миграционный pytest-аналог через Playwright. Зелёные.
8. **Frontend audioMix multi-track**. Smoke в браузере (DevTools): добавил, проверил `__cutstorm_mix.extras.size`.
9. **Frontend Timeline list + add button**. Smoke.
10. **Frontend SegmentList динамические табы**. Smoke.
11. **Frontend api/exportVideo + autosave** новая форма.
12. **Полный Playwright run**, фикс регрессий.
13. **Новый `14-multi-extra.spec.ts`** — добавлять тесты по одному.
14. **Ручной smoke** по разделу 5.3.
15. **Финал**: pytest + playwright + smoke.

---

## 9. Риски и заметки на потом

- **WebAudio MediaElementSource limit.** На практике 30+ работают. Лимит — не реальное ограничение для UI с 3-5 треками.
- **`amix=duration=longest`** + `apad` — корректно генерирует тишину. Альтернативный режим `duration=first` даёт шортест от первого, нам это уже не нужно.
- **Disconnect старых extra-нод при изменении списка** — забыть и получить «фантомные» звуки от уже удалённых треков. Делать tests, проверяющие `mix.extras.size === audio.extras.length` после remove.
- **Migration v8→v9** — единственное место где можно потерять данные пользователя. Юнит-тест Playwright с seeded localStorage — обязателен.
- **subtitleTrack=`<id>` неизвестного трека** — потенциально сохранён в meta, потом юзер удалил трек на бэке (sweep) — фронт должен фоллбэкнуть на "source" без ошибки.
- **Имена треков** — backend `/api/extra-audio` уже возвращает `name` от загрузки. После reload `name` теряется (как и было раньше). `/api/extra-audio/{id}/info` сейчас возвращает только duration+ext. **Расширить /info чтобы вернуть `original_filename`**, если хранить его в отдельном sidecar `extra_<id>.json` (≤1KB). Иначе UI после reload будет показывать «extra.wav» — не катастрофа, но грустно. Опционально, можно отложить.
- **Ordering в массиве** должен быть детерминированный — каждое чтение из store даёт тот же порядок. Map не подходит для extras (порядок теряется в JSON serialize), потому extras — array.

---

## 10. Глоссарий

- **extra track** — extra-аудиодорожка пользователя (НЕ источник видео). Уникально идентифицируется `extra_audio_id` (16 hex chars).
- **driver track** — extra-трек, чья длительность задаёт длительность loop-петли. По умолчанию `extras[0]`.
- **subtitle_track** — `"source"` или id экстра-трека. Указывает чьи сабы рисуются и пишутся в экспорт.
- **multi-mix** — ffmpeg `amix=inputs=N+1` где N+1 = source_audio (если есть) + все extras.
- **legacy form** — старая модель с `extra_audio_id` (одно поле). Принимается на бэке и фронте, конвертируется в массив из одного элемента. Не пишется обратно в новых сохранёнках.
- **persistence v8** — текущая версия store. **persistence v9** — новая, добавляет `extras: ExtraTrack[]`, превращает `segmentsExtra` в Record, ловит legacy.
