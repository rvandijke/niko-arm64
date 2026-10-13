# niko-arm64

The Niko Home Control programming software for macOS is Intel-only, so on Apple Silicon it runs
under Rosetta 2 and is slow. This script builds a native arm64 copy of it:

- swaps the Intel Electron and .NET runtimes for the official arm64 builds of the same versions
- precompiles the .NET backend for arm64 (no JIT compilation at startup)
- fixes the backend process staying alive after you quit the app

Niko's own code is left untouched. The original app isn't modified.

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
