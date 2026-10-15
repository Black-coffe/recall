# Whisper UI - Application User Guide

## What Whisper UI is

Whisper UI is a web application for automatically converting speech to text (transcription). The application uses OpenAI Whisper technology — a modern neural speech recognition model trained on 680,000 hours of audio data.

The application provides a convenient web interface for working with Whisper, so the user does not need to use the command line or write code.

---

## Main features

### 1. Audio file transcription

The user can upload an audio file from the computer, and the application automatically converts the speech to text.

**Supported audio formats:**
- MP3
- M4A
- WAV
- FLAC
- OGG
- WebM

### 2. Video file transcription

The application also supports video files. When a video is uploaded, the system automatically extracts the audio track with FFmpeg and then performs the transcription.

**Supported video formats:**
- MP4
- AVI
- MOV
- MKV
- WMV

### 3. Working with YouTube videos

The user can provide a link to a YouTube video. The system downloads the audio track and performs the transcription. Various YouTube link formats are supported:
- Standard videos (youtube.com/watch?v=...)
- Short links (youtu.be/...)
- YouTube Shorts
- YouTube Live
- YouTube Music
- Mobile versions

**Trimming feature:** before downloading, you can specify a time range (start and end) to transcribe only the part of the video you need.

---

## Whisper models

The application supports 5 different models, which differ in size and recognition accuracy:

| Model | Size | Speed | RAM requirements | Quality |
|--------|--------|----------|----------------|----------|
| **Tiny** | 39 MB | ~32x realtime | ~1 GB | Basic |
| **Base** | 74 MB | ~16x realtime | ~1 GB | Good |
| **Small** | 244 MB | ~6x realtime | ~2 GB | Very good |
| **Medium** | 769 MB | ~2x realtime | ~5 GB | Excellent |
| **Large** | 1550 MB | ~1x realtime | ~10 GB | Maximum |

**Model selection recommendations:**
- **Tiny/Base** — for quick draft transcription or when resources are limited
- **Small** — the best balance of speed and quality for most tasks
- **Medium** — for high-quality transcription when time is not critical
- **Large** — maximum accuracy for professional tasks

---

## Language support

Whisper supports automatic language detection and can also work with a specific language you specify:

- Ukrainian
- English
- Russian
- Spanish
- French
- German
- Italian
- Portuguese
- Polish
- Turkish
- Japanese
- Korean
- Chinese
- Auto-detect (the system determines the language itself)

---

## Transcription process

### Normal mode (files up to 10 minutes)

1. The user uploads a file or provides a YouTube link
2. Selects the Whisper model and the language
3. Clicks the "Transcribe" button
4. The model is loaded into memory (on first use)
5. The audio is passed to the Whisper model
6. The result is returned to the user as text and segments with timecodes

### Chunk mode (files longer than 10 minutes)

A special splitting algorithm is applied to long recordings:

1. The audio file is split into fragments (chunks) of 5 minutes
2. A 5-second overlap is created between fragments for correct stitching
3. Each chunk is processed separately
4. The results are merged with timestamp correction
5. Overlapping segments are removed to avoid duplication

This makes it possible to process recordings of any length without running out of memory.

---

## Output of results

### Text result

When the transcription is complete, the user gets:
- **Full text** — all the recognized text as a single block
- **Segments with timecodes** — the text is split into fragments with the start and end of each segment

### Export formats

The result can be exported in various formats:

1. **TXT** — a plain text file without formatting
2. **SRT** — a subtitle format with timecodes (for video players)
3. **JSON** — structured data with full segment information

---

## GPU acceleration

The application automatically detects a compatible NVIDIA graphics card with CUDA support. If a GPU is present:

- The model is loaded into video memory
- Processing runs on the GPU (much faster)
- FP16 (16-bit computation) is used to save memory

**Example performance on an RTX 3090:**
- Tiny model: ~32x realtime (1 hour of audio in ~2 minutes)
- Base model: ~16x realtime
- Medium model: ~2x realtime

Without a GPU, processing runs on the CPU, which is much slower but works on any computer.

---

## Transcription history

All completed transcriptions are saved in a local database. The user can:

- Browse the history with pagination
- Search by title or text content
- Filter by source type (file/YouTube) or language
- Open the full version of any transcription
- Delete individual records or a selected group
- Export several records at once

For each record the following is saved:
- Date and time of creation
- Source file or URL
- Recognized text
- Segments with timecodes
- Model used
- Processing time
- YouTube video information (if applicable)

---

## Application interface

### Three main tabs

1. **Upload a file** — a drag-and-drop zone for uploading audio/video files
2. **YouTube video** — a field for entering a YouTube link with an information preview
3. **Audio library** — a library of previously uploaded audio files

### Settings panel

After a source is selected, a panel with settings appears:
- Whisper model selection
- Language selection
- Information about the selected file
- The button that starts the transcription

### Progress indication

During processing the following is displayed:
- Percentage complete
- Current stage (loading, processing, transcription)
- Processing time
- For long files — the number of the current chunk

---

## YouTube video trimming feature

When working with YouTube videos, an optional trimming feature is available:

1. After the video information is received, the trimming panel appears
2. The user turns on trimming mode
3. Sets the start and end of the needed fragment:
   - Via text fields with timecodes
   - Via a visual slider
   - From the built-in video player (setting the current position)
4. The system shows the resulting duration and how much will be skipped
5. On download, only the selected fragment is downloaded

This makes it possible to:
- Save time on downloading and processing
- Transcribe only the needed part of a long video
- Ignore intros, splash screens and other unneeded parts

---

## Technical limitations

| Parameter | Limit |
|----------|-------------|
| Maximum file size | 2 GB |
| Maximum YouTube video length | 3 hours |
| Concurrent operations | 2 |
| Maximum records in history | Unlimited |

---

## System requirements

### Required
- Python 3.8 or higher
- FFmpeg (for working with video and YouTube)
- 8 GB RAM (more is recommended for the medium/large models)

### Optional
- NVIDIA GPU with CUDA 12.1 support (for acceleration)

---

## Security

The application implements the following security measures:

- **File validation** — checking file extensions and types
- **Path Traversal protection** — control of file save paths
- **Parameterized SQL queries** — protection against SQL injection
- **File size limit** — preventing DoS attacks
- **Safe file names** — cleaning names of dangerous characters

---

## Processing architecture

```
┌─────────────────────────────────────────────────────────────┐
│                         Web interface                        │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ File upload  │  │  YouTube URL │  │  Audio library   │   │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘   │
└─────────┼─────────────────┼────────────────────┼────────────┘
          │                 │                    │
          ▼                 ▼                    ▼
┌─────────────────────────────────────────────────────────────┐
│                      Flask Backend                           │
│  ┌────────────┐  ┌───────────────┐  ┌──────────────────┐    │
│  │ File       │  │ YouTube       │  │ Audio extraction │    │
│  │ validation │  │ download      │  │ from video       │    │
│  └─────┬──────┘  └───────┬───────┘  └────────┬─────────┘    │
└────────┼─────────────────┼───────────────────┼──────────────┘
         │                 │                   │
         └─────────────────┴───────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                    Whisper Manager                           │
│  ┌─────────────────────────────────────────────────────┐    │
│  │  1. Check the audio duration                         │    │
│  │  2. Split into chunks (if > 10 minutes)             │    │
│  │  3. Load the model (GPU/CPU)                        │    │
│  │  4. Transcribe each chunk                           │    │
│  │  5. Merge the results                               │    │
│  └─────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────┘
                           │
                           ▼
┌─────────────────────────────────────────────────────────────┐
│                      Result                                  │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐   │
│  │ Text         │  │ Segments     │  │ Export           │   │
│  │              │  │ with timecodes│ │ TXT/SRT/JSON     │   │
│  └──────────────┘  └──────────────┘  └──────────────────┘   │
└─────────────────────────────────────────────────────────────┘
```

---

## Use cases

### Case 1: Transcribing a lecture

1. Record the lecture on a voice recorder (MP3/M4A)
2. Upload the file through the web interface
3. Select the "Medium" model for good quality
4. Specify the language of the lecture
5. Get the text split into segments
6. Export to TXT for further work

### Case 2: Subtitles for a video

1. Provide a link to a YouTube video
2. Trim the unneeded parts if necessary
3. Select the "Small" or "Medium" model
4. After transcription, export to SRT
5. Use the SRT file as subtitles

### Case 3: Quick search through a video

1. Provide a YouTube link
2. Use the "Base" model for speed
3. Get the text
4. Search the text for the information you need
5. Use the segment timecodes to find the needed moment in the video

---

## Model management

### Loading models

On first use, a model is automatically downloaded from the internet and saved to the cache (`~/.cache/whisper`). Subsequent uses of the model are instant.

You can also download the models you need in advance through the settings interface.

### Switching between models

When the model is changed:
1. The current model is unloaded from memory
2. GPU memory is freed (if it was used)
3. The new model is loaded
4. This takes a few seconds

It is recommended to use one model for a series of transcriptions to avoid repeated loading.

---

*Whisper UI v3.1.0*
