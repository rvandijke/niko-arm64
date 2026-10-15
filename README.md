# niko-arm64: run the Niko Home Control programming software natively on Apple Silicon

The Niko Home Control programming software for macOS (Niko Home Control II / NHC2) is Intel-only,
so on an Apple Silicon Mac (M1, M2, M3, M4) it runs under Rosetta 2. It starts slowly, the UI lags
and CPU use is high. This script builds a native arm64 copy of it:

- swaps the Intel Electron and .NET runtimes for the official arm64 builds of the same versions
- precompiles the .NET backend for arm64 (no JIT compilation at startup)
- fixes the backend process staying alive after you quit the app

Niko's own code is left untouched. The original app isn't modified.

Startup time on an M4 Pro (version 2.27, median of 5 runs, from launch until the app is loaded):

| | Startup |
|---|---|
| Original (Intel, Rosetta 2) | 12.1 s |
| Native arm64 | 5.7 s |

## Usage

```sh
./build-niko-arm64.py
```

This finds the Niko app in `/Applications` and writes
`/Applications/Niko Home Control programming software (Apple Silicon).app`. Re-run it after every
Niko update; the previous build is kept in `~/Library/Caches/niko-arm64/previous/`.

See `./build-niko-arm64.py --help` for options (custom source/output, signing identity, skipping steps).

Requires macOS on Apple Silicon and Python 3.9+ (`xcode-select --install` provides it).

## Notes

- Decline in-app updates in the Apple Silicon copy: they install the Intel version. Update the
  original app instead and re-run the script.
- Both apps share settings and data.
- Test with a backup `.nhc2` file before uploading to your installation.
- Not affiliated with Niko. For personal use on your own Mac; don't redistribute the built app.

Built mainly by [Claude](https://claude.com/claude-code).

## License

MIT. See [LICENSE](LICENSE). This covers the script only, not the Niko software it converts.

## Nederlands

De Niko Home Control programmeersoftware (NHC2) voor macOS is Intel-only en draait op Apple Silicon
(M1/M2/M3/M4) onder Rosetta 2. Dit script genereert een native arm64-versie: de Electron- en
.NET-runtimes worden vervangen door de arm64-builds en de .NET-backend wordt vooraf gecompileerd.

## Français

Le logiciel de programmation Niko Home Control (NHC2) pour macOS est Intel-only et tourne sous
Rosetta 2 sur Apple Silicon (M1/M2/M3/M4). Ce script en génère une version arm64 native : les
runtimes Electron et .NET sont remplacés par les builds arm64 et le backend .NET est précompilé.
