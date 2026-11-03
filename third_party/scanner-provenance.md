# USB scanner protocol attribution

The USB scanner module is adapted from [farafonoff's DCP-T230 scanner](https://github.com/farafonoff/Brother-DCP-T230-MacOS-driver/tree/1c70c5c799f2b18964e137d520ab15c9cd40a075/scanner).

- Original file: `scanner/t230scan.py`.
- Upstream commit: `1c70c5c799f2b18964e137d520ab15c9cd40a075`.
- Original downloaded file SHA-256: `9dc1d78491a575bc90f2e3606cd3f9cdfc27665c8de4e52b494bb443c0e1e82b`.
- License: GPL-2.0-or-later, as declared in the upstream scanner README's License section.
- Local file: `src/scanner/t236scan.py`.
- Local adaptations: USB PID `0x071c`, scan extents aligned to 16 pixels, configured device serial selection.

The eSCL HTTP bridge, native PDF print helper, installer, tests and documentation are supplied in this repository. No Brother binary is redistributed or required at runtime. The eSCL implementation was informed by [AirSane's protocol implementation](https://github.com/SimulPiscator/AirSane); no AirSane source is incorporated.
