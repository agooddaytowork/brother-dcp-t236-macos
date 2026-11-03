# Changelog

## 0.1.0

- Native PDF → PWG Raster → USB printing with A4 color and grayscale output.
- Correct PDF point-to-pixel scaling at 300 dpi, including landscape input.
- Local eSCL scanner bridge discoverable by macOS Image Capture.
- Full A4 scan region, JPEG/PNG output, local crop and grayscale conversion.
- Shared USB lock for printing and scanning; per-user login services.
- Device detection, installer, conservative uninstaller and source-complete archives.
- macOS CI builds for arm64 and x86_64, with offline protocol/rendering checks.

Physical print and scan validation: DCP-T236, Apple Silicon, macOS 26.
Intel artifacts are built/tested offline; Intel USB hardware has not been validated.
