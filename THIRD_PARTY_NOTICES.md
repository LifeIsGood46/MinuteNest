# Third-party notices

MinuteNest itself is released under the [MIT License](LICENSE) (c) 2026 Vitaliy Horoshev.

The Windows release zip also contains third-party software. Each component keeps
its own licence; MinuteNest's MIT licence does not change theirs. This file lists
what ships in the zip and where to get the full licence text and source.

## Components that need special attention

### FFmpeg and ffprobe (GPL-3.0-or-later)
- `ffmpeg.exe` and `ffprobe.exe`, version 9.0.2 "essentials" build by gyan.dev.
- Built with `--enable-gpl --enable-version3` and libraries such as x264, x265 and
  libvpx, so the build as a whole is licensed under the **GNU GPL v3 or later**.
- MinuteNest runs these as separate programs (it does not link against them) and only
  uses them to extract audio. They can be replaced by any compatible ffmpeg.
- Licence: <https://www.gnu.org/licenses/gpl-3.0.html>
- Corresponding source: FFmpeg <https://ffmpeg.org/download.html>; build
  information and sources for this build: <https://www.gyan.dev/ffmpeg/builds/>.

### PyAV and its bundled FFmpeg libraries (BSD-3-Clause for PyAV; FFmpeg libraries under LGPL/GPL terms)
- faster-whisper uses PyAV (`av`) to decode audio. The PyAV Windows wheel bundles
  FFmpeg shared libraries (avcodec, avformat, ... plus codecs such as x264, x265,
  libvpx, dav1d, opus, lame).
- PyAV: <https://github.com/PyAV-Org/PyAV> - the bundled library builds:
  <https://github.com/PyAV-Org/pyav-ffmpeg>. FFmpeg source: <https://ffmpeg.org/>.

### NVIDIA CUDA runtime libraries (NVIDIA proprietary licence)
- `cublas64_*.dll`, `cublasLt64_*.dll`, `nvrtc64_*.dll`, `nvrtc-builtins64_*.dll`
  from the `nvidia-cublas-cu12` and `nvidia-cuda-nvrtc-cu12` packages.
- These are redistributed under the NVIDIA CUDA Toolkit licence agreement, which
  lists them as distributable with an application. The licence text is shipped in
  `licenses/NVIDIA-CUDA-EULA.txt`. They are used only by MinuteNest for GPU
  transcription.

## Python packages (runtime)

| Package | Licence |
|---|---|
| faster-whisper | MIT |
| CTranslate2 | MIT |
| FastAPI, Starlette | MIT, BSD-3-Clause |
| Uvicorn | BSD-3-Clause |
| Pydantic | MIT |
| Jinja2, MarkupSafe | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| comtypes | MIT |
| certifi | MPL-2.0 (source: <https://github.com/certifi/python-certifi>) |
| huggingface_hub, hf-xet | Apache-2.0 |
| tokenizers | Apache-2.0 |
| ONNX Runtime | MIT |
| NumPy | BSD-3-Clause and others |
| PyYAML | MIT |
| Requests, urllib3, idna, charset-normalizer | Apache-2.0, MIT, BSD-3-Clause |
| tqdm | MPL-2.0 and MIT |
| anyio, h11, httptools, websockets, watchfiles, click, filelock, fsspec, packaging, protobuf, flatbuffers, typing-extensions, annotated-types, python-dotenv | MIT / BSD / Apache-2.0 / PSF (permissive) |
| PyInstaller (build tool; the bootloader is bundled in the exe) | GPL-2.0-or-later with a special exception that allows distributing the built program under any licence |

Full licence texts are in each package's `*.dist-info/licenses` folder in the
Python environment the release was built from.

## Speech models (downloaded on demand, not included in the zip)

MinuteNest downloads Whisper models from Hugging Face the first time you pick a model.
The OpenAI Whisper weights and the CTranslate2 conversions used by faster-whisper
are published under the MIT licence (see the model cards on Hugging Face).

## Other

- The icons in the interface are small inline SVG drawings kept in
  `minutenest/webapp/templates/index.html`.
- The MinuteNest logo (`assets/logo.svg`) is original artwork for this project.
