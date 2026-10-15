@echo off
REM Build portable MinuteNest folder: onedir (no self-extraction),
REM both exes sharing one runtime, all app data beside the exes.
cd /d "%~dp0"
set FOLDER=MinuteNest
set VENDOR=vendor
set VERSION=0.4.0

REM ---------------------------------------------------------------------------
REM Vendored binaries: ffmpeg/ffprobe (audio extraction) + WebView2 window.
REM ffmpeg ships inside the app so a clean machine needs nothing on PATH.
REM ---------------------------------------------------------------------------
if not exist "%VENDOR%\ffmpeg\ffmpeg.exe" (
  echo Downloading ffmpeg essentials build ...
  powershell -NoProfile -Command ^
    "$ErrorActionPreference='Stop';" ^
    "$u='https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip';" ^
    "$z='%VENDOR%\ffmpeg.zip';" ^
    "New-Item -ItemType Directory -Force '%VENDOR%' | Out-Null;" ^
    "Invoke-WebRequest -Uri $u -OutFile $z;" ^
    "Expand-Archive -Path $z -DestinationPath '%VENDOR%\ffmpeg-tmp' -Force;" ^
    "$bin=Get-ChildItem '%VENDOR%\ffmpeg-tmp' -Recurse -Filter ffmpeg.exe | Select-Object -First 1;" ^
    "$probe=Get-ChildItem '%VENDOR%\ffmpeg-tmp' -Recurse -Filter ffprobe.exe | Select-Object -First 1;" ^
    "New-Item -ItemType Directory -Force '%VENDOR%\ffmpeg' | Out-Null;" ^
    "Copy-Item $bin.FullName '%VENDOR%\ffmpeg\ffmpeg.exe' -Force;" ^
    "Copy-Item $probe.FullName '%VENDOR%\ffmpeg\ffprobe.exe' -Force;" ^
    "$lic=Get-ChildItem '%VENDOR%\ffmpeg-tmp' -Recurse -Filter LICENSE | Select-Object -First 1;" ^
    "if($lic){Copy-Item $lic.FullName '%VENDOR%\ffmpeg\LICENSE' -Force};" ^
    "Remove-Item -Recurse -Force '%VENDOR%\ffmpeg-tmp','%VENDOR%\ffmpeg.zip'"
  if errorlevel 1 (
    echo ffmpeg download FAILED. Get ffmpeg.exe + ffprobe.exe into %VENDOR%\ffmpeg\ manually.
    pause
    exit /b 1
  )
)

echo Building GUI exe (onedir) ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed --name MinuteNest ^
  --icon "assets\logo.ico" ^
  --distpath dist ^
  --workpath build ^
  --add-data "minutenest\webapp\templates;minutenest\webapp\templates" ^
  --add-data "minutenest\webapp\static;minutenest\webapp\static" ^
  --add-data "minutenest\profiles;minutenest\profiles" ^
  --add-data "minutenest\native;minutenest\native" ^
  --add-data "assets;assets" ^
  --add-data "%VENDOR%\ffmpeg;ffmpeg" ^
  --collect-data faster_whisper ^
  --collect-binaries ctranslate2 ^
  --collect-all nvidia.cublas ^
  --collect-all nvidia.cuda_nvrtc ^
  --collect-data certifi ^
  --hidden-import comtypes --collect-all comtypes ^
  launcher.py
if errorlevel 1 (
  echo GUI build FAILED.
  pause
  exit /b 1
)

echo Building CLI exe into the same folder ...
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --console --name minutenest-cli ^
  --icon "assets\logo.ico" ^
  --distpath dist ^
  --workpath build ^
  --paths "dist\%FOLDER%\_internal" ^
  --add-data "minutenest\profiles;minutenest\profiles" ^
  --add-data "%VENDOR%\ffmpeg;ffmpeg" ^
  --collect-data faster_whisper ^
  --collect-binaries ctranslate2 ^
  --collect-all nvidia.cublas ^
  --collect-all nvidia.cuda_nvrtc ^
  --collect-data certifi ^
  cli_entry.py
if errorlevel 1 (
  echo CLI build FAILED.
  pause
  exit /b 1
)

echo Assembling portable folder ...
REM Both exes live in dist\MinuteNest\ side by side, sharing _internal.
if not exist "dist\%FOLDER%\minutenest-cli.exe" move "dist\minutenest-cli\minutenest-cli.exe" "dist\%FOLDER%\" >nul
robocopy "dist\minutenest-cli\_internal" "dist\%FOLDER%\_internal" /E /NFL /NDL /NJH /NJS >nul
rd /s /q "dist\minutenest-cli" 2>nul
REM Portable layout skeleton (app fills the rest on first run).
mkdir "dist\%FOLDER%\results" 2>nul
REM Licences travel with the binaries (MIT app, GPL ffmpeg, NVIDIA CUDA EULA).
mkdir "dist\%FOLDER%\licenses" 2>nul
copy /y LICENSE "dist\%FOLDER%\licenses\LICENSE.txt" >nul
copy /y THIRD_PARTY_NOTICES.md "dist\%FOLDER%\licenses\THIRD_PARTY_NOTICES.md" >nul
for /d %%D in (".venv\Lib\site-packages\nvidia_cublas*.dist-info") do copy /y "%%D\licenses\License.txt" "dist\%FOLDER%\licenses\NVIDIA-CUDA-EULA.txt" >nul
if exist "%VENDOR%\ffmpeg\LICENSE" copy /y "%VENDOR%\ffmpeg\LICENSE" "dist\%FOLDER%\licenses\FFMPEG-GPL-3.0.txt" >nul


echo Zipping release package ...
powershell -NoProfile -Command "Compress-Archive -Path 'dist\%FOLDER%\*' -DestinationPath 'dist\MinuteNest-%VERSION%-win64.zip' -Force"

echo Post-build smoke gate ...
".venv\Scripts\python.exe" "scripts\smoke_release.py" "dist\MinuteNest-%VERSION%-win64.zip"
if errorlevel 1 (
  echo SMOKE GATE FAILED - do not ship this build.
  pause
  exit /b 1
)

echo.
echo Done: dist\%FOLDER%\ (run MinuteNest.exe) + dist\MinuteNest-%VERSION%-win64.zip
endlocal
